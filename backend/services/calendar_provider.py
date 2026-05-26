"""
Economic calendar data provider abstraction.

Routing:
  DATA_MODE=demo   → returns static mock calendar events (no API key)
  DATA_MODE=live   → CALENDAR_PROVIDER selects the real provider

Supported live providers
  fmp                https://financialmodelingprep.com/developer/docs/economic-calendar
  trading_economics  https://docs.tradingeconomics.com/#calendars
  eodhd              https://eodhd.com/financial-apis/macroeconomic-data-and-events/
  broker             Broker-hosted economic calendar REST endpoint (generic)

.env keys needed for live mode:
  DATA_MODE=live
  CALENDAR_PROVIDER=fmp              # or trading_economics | eodhd | broker
  CALENDAR_API_KEY=<your_key>

  # Trading Economics uses client:secret authentication
  CALENDAR_TE_CLIENT=<client_key>
  CALENDAR_TE_SECRET=<secret_key>

pip dependency for live mode:
  pip install httpx

Returned type: list[dict] matching CalendarEvent fields so it can be passed
directly to the Pydantic constructor in the router.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from config import settings

logger = logging.getLogger(__name__)

# Events we care about for EUR/USD
_RELEVANT_CURRENCIES = {"EUR", "USD"}

# Extended blackout keywords (mirrors signal_engine.py)
_MAJOR_KEYWORDS = {"CPI", "NFP", "FOMC", "ECB", "RATE DECISION", "GDP", "PCE"}


# ── HTTP utility ──────────────────────────────────────────────────────────────

def _http_get(
    url: str,
    params: dict | None = None,
    headers: dict | None = None,
    provider: str = "unknown",
) -> Any:
    """Synchronous GET with classified error handling."""
    try:
        import httpx
    except ImportError:
        raise RuntimeError(
            "httpx is not installed. Run `pip install httpx` then restart."
        )

    from exceptions import AuthError, RateLimitError, ProviderUnavailableError

    try:
        resp = httpx.get(url, params=params or {}, headers=headers or {}, timeout=15.0)
    except httpx.TimeoutException:
        raise ProviderUnavailableError(provider, "request timed out after 15 s")
    except httpx.ConnectError as exc:
        raise ProviderUnavailableError(provider, f"connection failed: {exc}")
    except httpx.RequestError as exc:
        raise ProviderUnavailableError(provider, str(exc))

    if resp.status_code in (401, 403):
        raise AuthError(provider)
    if resp.status_code == 429:
        raise RateLimitError(provider)
    if not resp.is_success:
        raise ProviderUnavailableError(provider, f"HTTP {resp.status_code}")

    return resp.json()


# ── Normalisation helpers ─────────────────────────────────────────────────────

def _impact_label(raw: str | None) -> str:
    """Normalise provider-specific impact strings to 'high' | 'medium' | 'low'."""
    r = (raw or "").lower().strip()
    if r in ("high", "3", "red", "***", "high impact"):
        return "high"
    if r in ("medium", "2", "orange", "**", "moderate"):
        return "medium"
    return "low"


def _parse_iso(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


# ── Demo mode ─────────────────────────────────────────────────────────────────

def _get_demo_calendar(date: str | None = None) -> list[dict]:
    """Returns static mock events from data/calendar.py — no network, no key."""
    from data.calendar import get_calendar
    resp = get_calendar(date)
    return [e.model_dump() for e in resp.events]


# ── Financial Modeling Prep (FMP) ─────────────────────────────────────────────
# Docs    : https://financialmodelingprep.com/developer/docs/economic-calendar
# Sign-up : https://financialmodelingprep.com/register  (free tier: 250 req/day)
# Note    : Returns global events; filter to EUR + USD on our side.
# Key env : CALENDAR_API_KEY

def get_fmp_calendar(date: str | None = None) -> list[dict]:
    from exceptions import EmptyResponseError

    now  = datetime.now(timezone.utc)
    from_dt = (now - timedelta(hours=12)).strftime("%Y-%m-%d")
    to_dt   = (now + timedelta(hours=36)).strftime("%Y-%m-%d")
    raw = _http_get(
        "https://financialmodelingprep.com/api/v3/economic_calendar",
        params={
            "from":   from_dt,
            "to":     to_dt,
            "apikey": settings.calendar_api_key,
        },
        provider="FMP Calendar",
    )
    if isinstance(raw, dict) and raw.get("Error Message"):
        from exceptions import AuthError
        raise AuthError("FMP Calendar")
    events, eid = [], 1
    for ev in (raw if isinstance(raw, list) else []):
        currency = str(ev.get("country", "")).upper()
        if currency not in _RELEVANT_CURRENCIES:
            continue
        ev_time = _parse_iso(ev.get("date"))
        if not ev_time:
            continue
        pending  = ev_time > now
        actual   = str(ev["actual"])   if ev.get("actual")   not in (None, "") else None
        forecast = str(ev["estimate"]) if ev.get("estimate") not in (None, "") else None
        previous = str(ev["previous"]) if ev.get("previous") not in (None, "") else None
        beat     = (
            None if (actual is None or forecast is None)
            else _to_float(actual, 0) > _to_float(forecast, 0)
        )
        events.append({
            "id": eid, "time": ev_time, "currency": currency,
            "event": ev.get("event", ""),
            "impact": _impact_label(ev.get("impact")),
            "forecast": forecast, "previous": previous, "actual": actual,
            "pending": pending, "beat": beat,
        })
        eid += 1
    return events


# ── Trading Economics ─────────────────────────────────────────────────────────
# Docs    : https://docs.tradingeconomics.com/#calendars
# Sign-up : https://tradingeconomics.com/api/  (paid plans only)
# Auth    : client:secret query params (not a bearer token)
# Key env : CALENDAR_TE_CLIENT  CALENDAR_TE_SECRET
# Note    : Returns up to 1 000 events per call for the specified countries.

def get_trading_economics_calendar(date: str | None = None) -> list[dict]:
    client = settings.calendar_te_client
    secret = settings.calendar_te_secret
    if not client or not secret:
        raise ValueError(
            "CALENDAR_PROVIDER=trading_economics requires both "
            "CALENDAR_TE_CLIENT and CALENDAR_TE_SECRET in .env."
        )
    now = datetime.now(timezone.utc)
    raw = _http_get(
        "https://api.tradingeconomics.com/calendar/country/united states,euro area",
        params={"c": f"{client}:{secret}"},
        provider="Trading Economics",
    )
    events, eid = [], 1
    for ev in raw:
        currency = "USD" if "United States" in str(ev.get("Country", "")) else "EUR"
        ev_time  = _parse_iso(ev.get("Date"))
        if not ev_time:
            continue
        actual   = str(ev["Actual"])   if ev.get("Actual")   not in (None, "") else None
        forecast = str(ev["Forecast"]) if ev.get("Forecast") not in (None, "") else None
        previous = str(ev["Previous"]) if ev.get("Previous") not in (None, "") else None
        events.append({
            "id": eid, "time": ev_time, "currency": currency,
            "event": ev.get("Event", ""),
            "impact": _impact_label(ev.get("Importance")),
            "forecast": forecast, "previous": previous, "actual": actual,
            "pending": ev_time > now,
            "beat": (None if (actual is None or forecast is None)
                     else _to_float(actual, 0) > _to_float(forecast, 0)),
        })
        eid += 1
    return events


# ── EODHD ─────────────────────────────────────────────────────────────────────
# Docs    : https://eodhd.com/financial-apis/macroeconomic-data-and-events/
# Sign-up : https://eodhd.com/register  (free tier: 20 req/day)
# Note    : Economic events endpoint returns all countries; filter client-side.
# Key env : CALENDAR_API_KEY

def get_eodhd_calendar(date: str | None = None) -> list[dict]:
    now = datetime.now(timezone.utc)
    raw = _http_get(
        "https://eodhd.com/api/economic-events",
        params={
            "api_token": settings.calendar_api_key,
            "fmt":       "json",
            "from":      (now - timedelta(hours=12)).strftime("%Y-%m-%d"),
            "to":        (now + timedelta(hours=36)).strftime("%Y-%m-%d"),
            "country":   "US,EU",
        },
        provider="EODHD",
    )
    events, eid = [], 1
    for ev in (raw or []):
        currency = str(ev.get("country", "")).upper()
        # EODHD uses country codes; map EU → EUR, US → USD
        if currency == "US":
            currency = "USD"
        elif currency in ("EU", "DE", "FR", "IT", "ES"):
            currency = "EUR"
        else:
            continue
        ev_time = _parse_iso(ev.get("date"))
        if not ev_time:
            continue
        actual   = str(ev["actual"])   if ev.get("actual")   not in (None, "") else None
        forecast = str(ev["estimate"]) if ev.get("estimate") not in (None, "") else None
        previous = str(ev["previous"]) if ev.get("previous") not in (None, "") else None
        events.append({
            "id": eid, "time": ev_time, "currency": currency,
            "event": ev.get("event", ""),
            "impact": _impact_label(ev.get("type")),
            "forecast": forecast, "previous": previous, "actual": actual,
            "pending": ev_time > now,
            "beat": (None if (actual is None or forecast is None)
                     else _to_float(actual, 0) > _to_float(forecast, 0)),
        })
        eid += 1
    return events


# ── Broker calendar (generic REST) ────────────────────────────────────────────
# Some brokers expose their own calendar REST API, e.g. IC Markets, OANDA.
# This placeholder expects a generic JSON array in a standard-ish shape.
# Configure the endpoint URL via CALENDAR_API_KEY (treated as the bearer token)
# and hard-code the URL for your specific broker.

_BROKER_CALENDAR_URL = "https://your-broker.com/api/calendar"   # override for your broker

def get_broker_calendar(date: str | None = None) -> list[dict]:
    logger.warning(
        "Broker calendar is a generic placeholder. "
        "Set _BROKER_CALENDAR_URL in calendar_provider.py to your broker's endpoint."
    )
    now = datetime.now(timezone.utc)
    raw = _http_get(
        _BROKER_CALENDAR_URL,
        headers={"Authorization": f"Bearer {settings.calendar_api_key}"},
        provider="Broker Calendar",
    )
    events, eid = [], 1
    for ev in (raw if isinstance(raw, list) else raw.get("events", [])):
        currency = str(ev.get("currency", ev.get("country", ""))).upper()
        if currency not in _RELEVANT_CURRENCIES:
            continue
        ev_time = _parse_iso(ev.get("time") or ev.get("date"))
        if not ev_time:
            continue
        events.append({
            "id": eid, "time": ev_time, "currency": currency,
            "event": ev.get("event") or ev.get("name", ""),
            "impact": _impact_label(ev.get("impact")),
            "forecast": str(ev["forecast"]) if ev.get("forecast") is not None else None,
            "previous": str(ev["previous"]) if ev.get("previous") is not None else None,
            "actual":   str(ev["actual"])   if ev.get("actual")   is not None else None,
            "pending":  ev_time > now,
            "beat": None,
        })
        eid += 1
    return events


# ── Utility ───────────────────────────────────────────────────────────────────

def _to_float(s: str | None, default: float) -> float:
    try:
        return float(str(s or "").replace("%", "").replace("K", "000").strip())
    except ValueError:
        return default


# ── Public router ─────────────────────────────────────────────────────────────

def get_macro_calendar(
    date: str | None = None,
    pair: str = "xauusd",
    *,
    force_refresh: bool = False,
) -> list[dict]:
    """
    Single entry point for all callers.
    pair          — pair code, used to filter relevant currencies
    force_refresh — bypass provider-side cache (currently honoured by ForexFactory)

    Demo mode  → static mock events, no network.
    Live mode  → routes to CALENDAR_PROVIDER:
                   fmp / eodhd / broker  — require CALENDAR_API_KEY
                   trading_economics     — requires CALENDAR_TE_CLIENT + SECRET
                   forexfactory          — FREE, no key required
    """
    from pair_config import get_pair_config
    try:
        pair_cfg = get_pair_config(pair)
        relevant_currencies = set(pair_cfg["news_currencies"])
    except ValueError:
        relevant_currencies = {"EUR", "USD"}

    if settings.data_mode == "demo":
        events = _get_demo_calendar(date)
        # Filter to pair-relevant currencies
        return [e for e in events if e.get("currency", "").upper() in relevant_currencies]

    provider = settings.calendar_provider.lower()

    key_required = provider in ("fmp", "eodhd", "broker")
    if key_required and not settings.calendar_api_key:
        # Graceful auto-fallback: rather than 502 the dashboard, fall through
        # to the free ForexFactory provider so the user always sees a live
        # calendar. We log a warning so the operator knows to set the key.
        logger.warning(
            "CALENDAR_PROVIDER=%s requires CALENDAR_API_KEY which is missing. "
            "Falling back to free ForexFactory feed.", provider,
        )
        provider = "forexfactory"

    if provider == "fmp":
        events = get_fmp_calendar(date)
        return [e for e in events if e.get("currency", "").upper() in relevant_currencies]
    if provider == "trading_economics":
        events = get_trading_economics_calendar(date)
        return [e for e in events if e.get("currency", "").upper() in relevant_currencies]
    if provider == "eodhd":
        events = get_eodhd_calendar(date)
        return [e for e in events if e.get("currency", "").upper() in relevant_currencies]
    if provider == "broker":
        events = get_broker_calendar(date)
        return [e for e in events if e.get("currency", "").upper() in relevant_currencies]
    if provider == "forexfactory":
        events = get_forexfactory_calendar(date, force_refresh=force_refresh)
        return [e for e in events if e.get("currency", "").upper() in relevant_currencies]

    raise ValueError(
        f"Unsupported CALENDAR_PROVIDER='{settings.calendar_provider}'. "
        f"Valid options: fmp | trading_economics | eodhd | broker | forexfactory"
    )


# ── ForexFactory free calendar (no API key required) ──────────────────────────

_FF_THISWEEK_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
# Cache for 30 min. The published JSON is updated infrequently and faireconomy
# rate-limits aggressive callers, so erring on the side of fewer fetches.
_FF_CACHE: dict[str, tuple[float, list[dict]]] = {}
_FF_CACHE_TTL_SEC = 30 * 60
# When rate-limited, accept cached data up to this much older than the normal TTL.
_FF_STALE_TOLERANCE_SEC = 12 * 60 * 60   # 12 h


def get_forexfactory_calendar(date: str | None = None, *, force_refresh: bool = False) -> list[dict]:
    """
    Free public economic calendar from faireconomy.media (ForexFactory mirror).
    NO API KEY required — uses the same JSON feed many trading platforms ship with.

    Caching:
      • Fresh cache: 30 min TTL — normal path.
      • Stale-tolerant fallback: if the upstream rate-limits us, fall back to
        cached data up to 12 h old rather than failing the whole request.
      • Cache miss + upstream error → propagate the exception.
    """
    import time as _time
    from exceptions import RateLimitError, ProviderUnavailableError

    cache_key = "thisweek"
    now_ts = _time.time()
    cached = _FF_CACHE.get(cache_key)

    if not force_refresh and cached and (now_ts - cached[0]) < _FF_CACHE_TTL_SEC:
        return _filter_ff_by_date(cached[1], date)

    try:
        raw = _http_get(_FF_THISWEEK_URL, provider="forexfactory")
    except (RateLimitError, ProviderUnavailableError) as exc:
        # Upstream rate-limit / outage. Use stale cache if we have one.
        if cached and (now_ts - cached[0]) < _FF_STALE_TOLERANCE_SEC:
            logger.warning(
                "[forexfactory] upstream %s — serving cache from %.0f min ago",
                type(exc).__name__, (now_ts - cached[0]) / 60.0,
            )
            return _filter_ff_by_date(cached[1], date)
        raise

    if not isinstance(raw, list):
        raise ValueError("ForexFactory returned non-list payload")

    events: list[dict] = []
    next_id = 1
    for item in raw:
        try:
            ts_str = item.get("date") or item.get("dateline")
            if not ts_str:
                continue
            # FF uses ISO-8601 with offset, e.g. "2026-05-20T08:30:00-04:00"
            try:
                ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
            except Exception:
                continue
            ts_utc = ts.astimezone(timezone.utc)

            impact_raw = (item.get("impact") or "").lower()
            impact = "high" if impact_raw == "high" else ("medium" if impact_raw == "medium" else "low")

            currency = (item.get("country") or "").upper()
            actual   = item.get("actual")
            forecast = item.get("forecast")
            previous = item.get("previous")

            pending = actual in (None, "", "null")
            beat = None
            if not pending and forecast not in (None, "", "null"):
                try:
                    beat = _to_float(actual, 0) > _to_float(forecast, 0)
                except Exception:
                    beat = None

            events.append({
                "id":       next_id,
                "time":     ts_utc,
                "currency": currency,
                "event":    item.get("title") or item.get("event") or "—",
                "impact":   impact,
                "forecast": forecast if forecast not in ("", None, "null") else None,
                "previous": previous if previous not in ("", None, "null") else None,
                "actual":   actual   if actual   not in ("", None, "null") else None,
                "pending":  pending,
                "beat":     beat,
            })
            next_id += 1
        except Exception as exc:
            logger.debug("FF parse skip: %s (%s)", exc, item)
            continue

    _FF_CACHE[cache_key] = (_time.time(), events)
    return _filter_ff_by_date(events, date)


def _filter_ff_by_date(events: list[dict], date: str | None) -> list[dict]:
    """Filter the weekly events to a single UTC date (default: today)."""
    target = date or datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return [
        e for e in events
        if e["time"].strftime("%Y-%m-%d") == target
    ]
