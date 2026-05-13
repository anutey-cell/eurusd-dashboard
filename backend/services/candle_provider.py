"""
EUR/USD candle data provider abstraction.

Routing:
  DATA_MODE=demo   → deterministic seeded candles (no API key)
  DATA_MODE=live   → FX_DATA_PROVIDER selects the real provider

Supported live providers
  twelvedata     https://twelvedata.com/docs
  alpha_vantage  https://www.alphavantage.co/documentation/#fx
  oanda          https://developer.oanda.com/rest-live-v20/instrument-ep/
  polygon        https://polygon.io/docs/forex
  fmp            https://financialmodelingprep.com/developer/docs/historical-chart

.env keys needed for live mode:
  DATA_MODE=live
  FX_DATA_PROVIDER=twelvedata          # or alpha_vantage | oanda | polygon | fmp
  FX_API_KEY=<your_key>
  FX_OANDA_ACCOUNT_ID=<id>             # only for oanda
  FX_OANDA_ENVIRONMENT=practice        # practice | live  (oanda only)

pip dependency for live mode:
  pip install httpx
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from config import settings
from models.candle import Candle, CandleResponse

logger = logging.getLogger(__name__)

# ── Timeframe lookup tables ────────────────────────────────────────────────────

_TWELVEDATA_TF: dict[str, str] = {
    "M5":  "5min",  "M15": "15min", "M30": "30min",
    "H1":  "1h",    "H4":  "4h",
    "D1":  "1day",  "W1":  "1week",
}

_OANDA_GRANULARITY: dict[str, str] = {
    "M5":  "M5",  "M15": "M15", "M30": "M30",
    "H1":  "H1",  "H4":  "H4",
    "D1":  "D",   "W1":  "W",
}

_POLYGON_PARAMS: dict[str, dict[str, Any]] = {
    "M5":  {"multiplier": 5,  "timespan": "minute"},
    "M15": {"multiplier": 15, "timespan": "minute"},
    "M30": {"multiplier": 30, "timespan": "minute"},
    "H1":  {"multiplier": 1,  "timespan": "hour"},
    "H4":  {"multiplier": 4,  "timespan": "hour"},
    "D1":  {"multiplier": 1,  "timespan": "day"},
    "W1":  {"multiplier": 1,  "timespan": "week"},
}

_FMP_TF: dict[str, str] = {
    "M5":  "5min",  "M15": "15min", "M30": "30min",
    "H1":  "1hour", "H4":  "4hour", "D1":  "1day",
}

# Alpha Vantage has no 4h endpoint — H4 falls back to H1 data
_ALPHA_VANTAGE_TF: dict[str, tuple[str, str | None]] = {
    "M5":  ("FX_INTRADAY",  "5min"),
    "M15": ("FX_INTRADAY",  "15min"),
    "M30": ("FX_INTRADAY",  "30min"),
    "H1":  ("FX_INTRADAY",  "60min"),
    "H4":  ("FX_INTRADAY",  "60min"),   # no native H4; consumer must aggregate 4×H1
    "D1":  ("FX_DAILY",     None),
    "W1":  ("FX_WEEKLY",    None),
}


# ── HTTP utility ──────────────────────────────────────────────────────────────

def _http_get(
    url: str,
    params: dict | None = None,
    headers: dict | None = None,
    provider: str = "unknown",
) -> Any:
    """Synchronous GET using httpx with classified error handling."""
    try:
        import httpx
    except ImportError:
        raise RuntimeError(
            "httpx is not installed. "
            "Run `pip install httpx` then restart the backend."
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


# ── Demo mode ─────────────────────────────────────────────────────────────────

def _get_demo_candles(interval: str, lookback: int) -> CandleResponse:
    """Returns the deterministic seeded mock candles — no network call, no key needed."""
    from data.candles import get_candles
    return get_candles(interval=interval, limit=lookback)


# ── Twelve Data ───────────────────────────────────────────────────────────────
# Docs    : https://twelvedata.com/docs#time-series
# Sign-up : https://twelvedata.com  (free tier: 8 req/min, 800/day)
# Key env : FX_API_KEY

def _parse_twelvedata(raw: dict, interval: str) -> list[Candle]:
    candles = []
    for bar in reversed(raw.get("values", [])):
        try:
            candles.append(Candle(
                time=datetime.fromisoformat(bar["datetime"]).replace(tzinfo=timezone.utc),
                open=float(bar["open"]),
                high=float(bar["high"]),
                low=float(bar["low"]),
                close=float(bar["close"]),
                volume=int(float(bar.get("volume") or 0)),
            ))
        except (KeyError, ValueError):
            continue
    return candles


def get_twelvedata_candles(interval: str, lookback: int, symbol: str = "EUR/USD") -> CandleResponse:
    from exceptions import AuthError, EmptyResponseError

    tf  = _TWELVEDATA_TF.get(interval, "4h")
    raw = _http_get(
        "https://api.twelvedata.com/time_series",
        params={
            "symbol":     symbol,
            "interval":   tf,
            "outputsize": min(lookback, 5000),
            "apikey":     settings.fx_api_key,
        },
        provider="Twelve Data",
    )
    if raw.get("status") == "error":
        msg = raw.get("message", "")
        if "api" in msg.lower() and "key" in msg.lower():
            raise AuthError("Twelve Data")
        raise ValueError(f"Twelve Data error: {msg}")
    candles = _parse_twelvedata(raw, interval)
    if not candles:
        raise EmptyResponseError("Twelve Data")
    return CandleResponse(symbol=symbol, interval=interval,
                          count=len(candles), candles=candles)


# ── Alpha Vantage ─────────────────────────────────────────────────────────────
# Docs    : https://www.alphavantage.co/documentation/#fx-intraday
# Sign-up : https://www.alphavantage.co/support/#api-key  (free tier: 25 req/day)
# Key env : FX_API_KEY
# Note    : No native H4 bar; H4 maps to 60min — caller must aggregate 4 bars.

def _parse_alpha_vantage(raw: dict, function: str) -> list[Candle]:
    # Intraday data is under a key like "Time Series FX (60min)"
    series_key = next((k for k in raw if "Time Series" in k), None)
    if not series_key:
        raise ValueError(f"Unexpected Alpha Vantage response: {list(raw.keys())}")
    candles = []
    for ts, bar in sorted(raw[series_key].items()):
        try:
            candles.append(Candle(
                time=datetime.fromisoformat(ts).replace(tzinfo=timezone.utc),
                open=float(bar["1. open"]),
                high=float(bar["2. high"]),
                low=float(bar["3. low"]),
                close=float(bar["4. close"]),
                volume=int(float(bar.get("5. volume", 0))),
            ))
        except (KeyError, ValueError):
            continue
    return candles


def get_alpha_vantage_candles(
    interval: str, lookback: int,
    from_sym: str = "EUR", to_sym: str = "USD",
) -> CandleResponse:
    from exceptions import AuthError, EmptyResponseError

    function, av_interval = _ALPHA_VANTAGE_TF.get(interval, ("FX_INTRADAY", "60min"))
    symbol = f"{from_sym}/{to_sym}"
    params: dict[str, Any] = {
        "function":    function,
        "from_symbol": from_sym,
        "to_symbol":   to_sym,
        "outputsize":  "full",
        "apikey":      settings.fx_api_key,
    }
    if av_interval:
        params["interval"] = av_interval
    if interval == "H4":
        logger.warning(
            "Alpha Vantage has no native H4 bar. "
            "Returning 60-min data — aggregate 4 bars client-side for H4."
        )
    raw = _http_get("https://www.alphavantage.co/query", params=params,
                    provider="Alpha Vantage")
    if "Error Message" in raw or "Note" in raw:
        note = raw.get("Note", raw.get("Error Message", ""))
        if "api" in note.lower() or "key" in note.lower():
            raise AuthError("Alpha Vantage")
        if "rate" in note.lower() or "limit" in note.lower():
            from exceptions import RateLimitError
            raise RateLimitError("Alpha Vantage")
        raise ValueError(f"Alpha Vantage error: {note}")
    candles = _parse_alpha_vantage(raw, function)[-lookback:]
    if not candles:
        raise EmptyResponseError("Alpha Vantage")
    return CandleResponse(symbol=symbol, interval=interval,
                          count=len(candles), candles=candles)


# ── OANDA v20 ─────────────────────────────────────────────────────────────────
# Docs    : https://developer.oanda.com/rest-live-v20/instrument-ep/#collapse_ep_GET_v3_instruments__instrument__candles
# Sign-up : https://www.oanda.com/us-en/trading/demo-account/  (free demo account)
# Key env : FX_API_KEY (= OANDA API token)
#           FX_OANDA_ACCOUNT_ID
#           FX_OANDA_ENVIRONMENT  (practice | live)

def get_oanda_candles(
    interval: str, lookback: int,
    instrument: str = "EUR_USD",
) -> CandleResponse:
    from exceptions import EmptyResponseError

    env         = settings.fx_oanda_environment
    base_url    = (
        "https://api-fxtrade.oanda.com"    if env == "live"
        else "https://api-fxpractice.oanda.com"
    )
    granularity = _OANDA_GRANULARITY.get(interval, "H4")
    symbol      = instrument.replace("_", "/")
    raw = _http_get(
        f"{base_url}/v3/instruments/{instrument}/candles",
        params={
            "granularity": granularity,
            "count":       min(lookback, 5000),
            "price":       "M",
        },
        headers={"Authorization": f"Bearer {settings.fx_api_key}"},
        provider="OANDA",
    )
    candles = []
    for bar in raw.get("candles", []):
        if not bar.get("complete", True):
            continue
        mid = bar.get("mid", {})
        try:
            candles.append(Candle(
                time=datetime.fromisoformat(bar["time"].rstrip("Z")).replace(tzinfo=timezone.utc),
                open=float(mid["o"]),
                high=float(mid["h"]),
                low=float(mid["l"]),
                close=float(mid["c"]),
                volume=int(bar.get("volume", 0)),
            ))
        except (KeyError, ValueError):
            continue
    if not candles:
        raise EmptyResponseError("OANDA")
    return CandleResponse(symbol=symbol, interval=interval,
                          count=len(candles), candles=candles)


# ── Polygon.io ────────────────────────────────────────────────────────────────
# Docs    : https://polygon.io/docs/forex/get_v2_aggs_ticker__forexticker__range__multiplier__timespan__from__to
# Sign-up : https://polygon.io/dashboard/signup  (free tier: 5 req/min, delayed 15 min)
# Note    : Forex data requires the "Currencies" subscription for real-time
# Key env : FX_API_KEY

def get_polygon_candles(
    interval: str, lookback: int,
    ticker: str = "C:EURUSD",
) -> CandleResponse:
    from exceptions import EmptyResponseError

    tf        = _POLYGON_PARAMS.get(interval, {"multiplier": 4, "timespan": "hour"})
    symbol    = ticker.replace("C:", "").replace("USD", "/USD").replace("EUR/", "EUR/")
    now       = datetime.now(timezone.utc)
    from_date = (now - timedelta(days=lookback * 2)).strftime("%Y-%m-%d")
    to_date   = now.strftime("%Y-%m-%d")
    raw = _http_get(
        f"https://api.polygon.io/v2/aggs/ticker/{ticker}/range"
        f"/{tf['multiplier']}/{tf['timespan']}/{from_date}/{to_date}",
        params={
            "adjusted": "true",
            "sort":     "asc",
            "limit":    min(lookback, 50_000),
            "apiKey":   settings.fx_api_key,
        },
        provider="Polygon.io",
    )
    candles = []
    for bar in (raw.get("results") or []):
        try:
            candles.append(Candle(
                time=datetime.fromtimestamp(bar["t"] / 1000, tz=timezone.utc),
                open=float(bar["o"]),
                high=float(bar["h"]),
                low=float(bar["l"]),
                close=float(bar["c"]),
                volume=int(bar.get("v", 0)),
            ))
        except (KeyError, ValueError):
            continue
    candles = candles[-lookback:]
    if not candles:
        raise EmptyResponseError("Polygon.io")
    return CandleResponse(symbol=symbol, interval=interval,
                          count=len(candles), candles=candles)


# ── Financial Modeling Prep (FMP) ─────────────────────────────────────────────
# Docs    : https://financialmodelingprep.com/developer/docs/historical-chart
# Sign-up : https://financialmodelingprep.com/register  (free tier: 250 req/day)
# Note    : W1 is not available; use D1 and aggregate 5 bars client-side.
# Key env : FX_API_KEY

def get_fmp_candles(
    interval: str, lookback: int,
    symbol: str = "EURUSD",
) -> CandleResponse:
    from exceptions import EmptyResponseError

    if interval == "W1":
        logger.warning("FMP has no native W1 Forex endpoint. Falling back to D1 data.")
        interval = "D1"
    tf         = _FMP_TF.get(interval, "4hour")
    disp_symbol = symbol[:3] + "/" + symbol[3:]  # "EURUSD" → "EUR/USD"
    raw = _http_get(
        f"https://financialmodelingprep.com/api/v3/historical-chart/{tf}/{symbol}",
        params={"apikey": settings.fx_api_key},
        provider="FMP",
    )
    if isinstance(raw, dict) and raw.get("Error Message"):
        from exceptions import AuthError
        raise AuthError("FMP")
    candles = []
    for bar in reversed(raw if isinstance(raw, list) else []):
        try:
            candles.append(Candle(
                time=datetime.fromisoformat(bar["date"]).replace(tzinfo=timezone.utc),
                open=float(bar["open"]),
                high=float(bar["high"]),
                low=float(bar["low"]),
                close=float(bar["close"]),
                volume=int(bar.get("volume", 0)),
            ))
        except (KeyError, ValueError):
            continue
    candles = candles[-lookback:]
    if not candles:
        raise EmptyResponseError("FMP")
    return CandleResponse(symbol=disp_symbol, interval=interval,
                          count=len(candles), candles=candles)


# ── Public router ─────────────────────────────────────────────────────────────

def get_eurusd_candles(
    timeframe: str = "H4",
    lookback: int = 200,
    pair: str = "EUR/USD",
) -> CandleResponse:
    """
    Single entry point for all callers.

    pair       – trading pair; provider functions translate this to their symbol format
    Demo mode  → seeded mock data, reproducible, no network call.
    Live mode  → routes to FX_DATA_PROVIDER; requires FX_API_KEY.
    """
    from pair_config import get_pair as _get_pair_cfg
    pair_cfg = _get_pair_cfg(pair)   # validates pair; raises ValueError if unknown

    interval = timeframe.upper()

    if settings.data_mode == "demo":
        return _get_demo_candles(interval, lookback)

    provider = settings.fx_data_provider.lower()

    if not settings.fx_api_key and provider != "oanda":
        raise ValueError(
            f"DATA_MODE=live but FX_API_KEY is not set. "
            f"Add FX_API_KEY=<your_key> to .env and restart."
        )
    if provider == "oanda" and not settings.fx_api_key:
        raise ValueError(
            "DATA_MODE=live with OANDA requires FX_API_KEY (= OANDA API token). "
            "Set it in .env and restart."
        )

    if provider == "twelvedata":
        return get_twelvedata_candles(interval, lookback, symbol=pair_cfg["td_symbol"])
    if provider == "alpha_vantage":
        return get_alpha_vantage_candles(interval, lookback,
                                         from_sym=pair_cfg["av_from"],
                                         to_sym=pair_cfg["av_to"])
    if provider == "oanda":
        return get_oanda_candles(interval, lookback,
                                  instrument=pair_cfg["oanda_instrument"])
    if provider == "polygon":
        return get_polygon_candles(interval, lookback,
                                    ticker=pair_cfg["polygon_ticker"])
    if provider == "fmp":
        return get_fmp_candles(interval, lookback, symbol=pair_cfg["fmp_symbol"])

    raise ValueError(
        f"Unsupported FX_DATA_PROVIDER='{settings.fx_data_provider}'. "
        f"Valid options: twelvedata | alpha_vantage | oanda | polygon | fmp"
    )
