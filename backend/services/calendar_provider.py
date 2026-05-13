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

def get_macro_calendar(date: str | None = None, pair: str = "eurusd") -> list[dict]:
    """
    Single entry point for all callers.
    pair  — pair code, used to filter relevant currencies

    Demo mode  → static mock events, no network.
    Live mode  → routes to CALENDAR_PROVIDER; requires CALENDAR_API_KEY
                 (or TE_CLIENT + TE_SECRET for trading_economics).
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
        raise ValueError(
            f"DATA_MODE=live with CALENDAR_PROVIDER={provider} requires "
            "CALENDAR_API_KEY in .env."
        )

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

    raise ValueError(
        f"Unsupported CALENDAR_PROVIDER='{settings.calendar_provider}'. "
        f"Valid options: fmp | trading_economics | eodhd | broker"
    )
