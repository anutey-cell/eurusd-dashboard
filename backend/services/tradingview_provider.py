# -*- coding: utf-8 -*-
"""
TradingView Candle Provider
============================
Fetches real OHLCV candle data from TradingView using the tvDatafeed library.
Sits between the MT5 bridge and the synthetic fallback in the candle chain:

  Priority order (highest → lowest):
    1. MT5 Windows Bridge         (live broker tick data)
    2. TradingView (this module)  (real market OHLCV, free with account)
    3. Synthetic generator        (seeded demo data, always available)

Environment variables:
  TRADINGVIEW_ENABLED   true | false   (default: false)
  TRADINGVIEW_USERNAME  TradingView username
  TRADINGVIEW_PASSWORD  TradingView password

Symbol mapping:
  eurusd → EURUSD on FX_IDC exchange
  xauusd → XAUUSD on TVC exchange (spot gold)

Timeframe mapping:
  H4 → Interval.in_4_hour
  H1 → Interval.in_1_hour
  D1 → Interval.in_daily
  M15→ Interval.in_15_minute
"""
from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

log = logging.getLogger(__name__)

# ── Config ────────────────────────────────────────────────────────────────────

TV_ENABLED  = os.getenv("TRADINGVIEW_ENABLED", "false").lower() in ("1", "true", "yes")
TV_USERNAME = os.getenv("TRADINGVIEW_USERNAME", "")
TV_PASSWORD = os.getenv("TRADINGVIEW_PASSWORD", "")

CACHE_TTL   = 300       # 5 min — TradingView rate-limits frequent requests
PROBE_TTL   = 600       # re-test connectivity every 10 min

# TradingView exchange + symbol per pair code
# OANDA verified working for XAUUSD (real price ~4700, confirmed 2026-05-14)
# TVC was returning empty for XAUUSD — do not use TVC for gold
_TV_SYMBOLS: dict[str, tuple[str, str]] = {
    "eurusd": ("EURUSD", "FX_IDC"),
    "xauusd": ("XAUUSD", "OANDA"),
    "gbpusd": ("GBPUSD", "FX_IDC"),
    "usdjpy": ("USDJPY", "FX_IDC"),
}

# Map timeframe strings to tvDatafeed Interval enum values
_TF_MAP: dict[str, str] = {
    "M1":  "in_1_minute",
    "M5":  "in_5_minute",
    "M15": "in_15_minute",
    "M30": "in_30_minute",
    "H1":  "in_1_hour",
    "H2":  "in_2_hour",
    "H4":  "in_4_hour",
    "D1":  "in_daily",
    "W1":  "in_weekly",
}


# ── Internal state ────────────────────────────────────────────────────────────

@dataclass
class _CacheEntry:
    candles:    list[dict]
    fetched_at: float


_cache:         dict[str, _CacheEntry] = {}
_tv_client:     Any   = None
_tv_ok:         bool  = False
_last_probe:    float = 0.0


def _get_client():
    """Return a cached tvDatafeed client, creating it if necessary."""
    global _tv_client, _tv_ok, _last_probe

    now = time.monotonic()
    if now - _last_probe < PROBE_TTL and _tv_client is not None:
        return _tv_client if _tv_ok else None

    _last_probe = now

    if not TV_ENABLED:
        log.debug("[tv_provider] Disabled via TRADINGVIEW_ENABLED=false")
        _tv_ok = False
        return None

    try:
        from tvDatafeed import TvDatafeed
        if TV_USERNAME and TV_PASSWORD:
            client = TvDatafeed(TV_USERNAME, TV_PASSWORD)
            log.info("[tv_provider] Authenticated as %s", TV_USERNAME)
        else:
            # Anonymous access — works but limited to free data
            client = TvDatafeed()
            log.info("[tv_provider] Using TradingView anonymously (no credentials)")
        _tv_client = client
        _tv_ok = True
        return client
    except ImportError:
        log.warning("[tv_provider] tvDatafeed not installed — run: pip install tvDatafeed")
        _tv_ok = False
        return None
    except Exception as exc:
        log.warning("[tv_provider] TradingView connection failed: %s", exc)
        _tv_ok = False
        return None


def _interval(timeframe: str):
    """Return the tvDatafeed Interval enum member for the given timeframe string."""
    from tvDatafeed import Interval
    attr = _TF_MAP.get(timeframe.upper())
    if attr is None:
        raise ValueError(f"Unsupported timeframe: {timeframe}")
    return getattr(Interval, attr)


# ── Public API ────────────────────────────────────────────────────────────────

def get_tv_candles(pair: str, timeframe: str = "H4", limit: int = 300) -> list[dict] | None:
    """
    Fetch OHLCV candles from TradingView.

    Returns:
        list of dicts: [{time, open, high, low, close, volume}, ...]   (oldest first)
        None if unavailable (caller should try next fallback)
    """
    cache_key = f"{pair}:{timeframe}"
    entry = _cache.get(cache_key)
    if entry and (time.monotonic() - entry.fetched_at) < CACHE_TTL:
        log.debug("[tv_provider] Cache hit for %s/%s (%d bars)", pair, timeframe, len(entry.candles))
        return entry.candles

    client = _get_client()
    if client is None:
        return None

    sym_info = _TV_SYMBOLS.get(pair.lower())
    if sym_info is None:
        log.warning("[tv_provider] Unknown pair '%s' — no TradingView symbol mapping", pair)
        return None

    symbol, exchange = sym_info

    try:
        interval = _interval(timeframe)
        df = client.get_hist(
            symbol=symbol,
            exchange=exchange,
            interval=interval,
            n_bars=limit + 5,   # fetch a few extra; drop incomplete last bar
        )

        if df is None or df.empty:
            log.warning("[tv_provider] No data returned for %s/%s on %s", symbol, timeframe, exchange)
            return None

        # Drop last bar (may be incomplete)
        df = df.iloc[:-1]
        df = df.tail(limit)

        candles: list[dict] = []
        for ts, row in df.iterrows():
            # Index is a timezone-aware or naive datetime from tvDatafeed
            if hasattr(ts, "to_pydatetime"):
                dt = ts.to_pydatetime()
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
            else:
                dt = datetime.fromtimestamp(float(ts), tz=timezone.utc)

            candles.append({
                "time":   dt.isoformat(),
                "open":   float(row["open"]),
                "high":   float(row["high"]),
                "low":    float(row["low"]),
                "close":  float(row["close"]),
                "volume": int(row.get("volume", 0)),
                "source": "tradingview",
            })

        _cache[cache_key] = _CacheEntry(candles=candles, fetched_at=time.monotonic())
        log.info("[tv_provider] Fetched %d live %s candles for %s from TradingView",
                 len(candles), timeframe, pair.upper())
        return candles

    except Exception as exc:
        log.warning("[tv_provider] Fetch failed for %s/%s: %s", pair, timeframe, exc)
        # Reset client so next probe attempts reconnect
        global _tv_ok
        _tv_ok = False
        return None


def get_tv_multi(pair: str) -> dict[str, list[dict]] | None:
    """
    Fetch all ICT-relevant timeframes in one pass: D1, H4, H1, M15.
    Returns dict keyed by timeframe name, or None if unavailable.
    """
    client = _get_client()
    if client is None:
        return None

    limits = {"D1": 100, "H4": 300, "H1": 200, "M15": 100}
    result: dict[str, list[dict]] = {}
    any_success = False

    for tf, lim in limits.items():
        candles = get_tv_candles(pair, timeframe=tf, limit=lim)
        if candles is not None:
            result[tf] = candles
            any_success = True
        else:
            result[tf] = []

    return result if any_success else None


def tv_status() -> dict:
    """Status dict for health / engine endpoints."""
    client = _get_client()
    return {
        "enabled":   TV_ENABLED,
        "connected": _tv_ok,
        "username":  TV_USERNAME or "(anonymous)",
        "cached_pairs": list(_cache.keys()),
    }


def invalidate_cache(pair: str | None = None) -> None:
    """Flush cached candle data."""
    if pair is None:
        _cache.clear()
    else:
        for key in list(_cache.keys()):
            if key.startswith(pair.lower()):
                del _cache[key]
