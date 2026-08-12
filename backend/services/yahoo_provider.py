"""
Yahoo Finance candle provider — free, unlimited, no API key.

Uses Yahoo's public query1 chart endpoint directly (no yfinance library).
Symbol: `GC=F` (gold futures continuous). Note: this is FUTURES, not spot
XAU/USD — expect a ~$5-10 basis vs the spot broker price. Fine for HTF
structure / market state; may be imperfect for exact entry precision.

Serves as backup in the candle_ingestion fallback chain — engaged only
when the primary source (MT5 push → historical_candles with source=mt5)
is stale AND TradingView returns empty.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

log = logging.getLogger(__name__)

# Yahoo chart interval strings — no M5 offered, closest is 5m.
_TF_MAP: dict[str, tuple[str, str]] = {
    # tf → (yahoo_interval, yahoo_range)
    "M5":  ("5m",  "5d"),        # 5-day window covers TV's 200-bar depth
    "M15": ("15m", "5d"),
    "H1":  ("60m", "1mo"),
    "H4":  ("1h",  "3mo"),       # yahoo has no 4h — daemon-side resample below
    "D1":  ("1d",  "6mo"),
}

_SYMBOL: str = "GC=F"     # gold futures continuous
CACHE_TTL_S = 60          # short cache — Yahoo rate-limits repeat hits


@dataclass
class _CacheEntry:
    candles:    list[dict]
    fetched_at: float


_cache: dict[str, _CacheEntry] = {}


def _yahoo_url(interval: str, rng: str, symbol: str = _SYMBOL) -> str:
    return (f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
            f"?interval={interval}&range={rng}&includePrePost=false")


def get_yahoo_candles(pair: str, timeframe: str = "H1",
                        limit: int = 200) -> list[dict] | None:
    """
    Fetch gold-futures candles from Yahoo. Returns list of dicts matching
    the TradingView provider shape:
        [{time (ISO UTC), open, high, low, close, volume, source}, ...]
    Returns None on any failure (caller should try next fallback).
    """
    if pair.lower() != "xauusd":
        log.debug("[yahoo] pair %s not supported (only xauusd via GC=F)", pair)
        return None

    tf_info = _TF_MAP.get(timeframe.upper())
    if tf_info is None:
        log.debug("[yahoo] timeframe %s not supported", timeframe)
        return None
    interval, rng = tf_info

    cache_key = f"{pair}:{timeframe}"
    entry = _cache.get(cache_key)
    if entry and (time.time() - entry.fetched_at) < CACHE_TTL_S:
        return entry.candles

    try:
        import httpx
        url = _yahoo_url(interval, rng)
        r = httpx.get(url, timeout=10.0, headers={
            "User-Agent": "Mozilla/5.0 (xauusd-dashboard)",
        })
        if r.status_code != 200:
            log.warning("[yahoo] status=%s body=%s", r.status_code, r.text[:200])
            return None
        d = r.json()
    except Exception as exc:
        log.warning("[yahoo] fetch failed: %s", exc)
        return None

    try:
        result = (d.get("chart", {}).get("result") or [None])[0]
        if not result:
            log.warning("[yahoo] no result in response")
            return None
        timestamps = result.get("timestamp") or []
        quote = (result.get("indicators", {}).get("quote") or [{}])[0]
        opens  = quote.get("open")  or []
        highs  = quote.get("high")  or []
        lows   = quote.get("low")   or []
        closes = quote.get("close") or []
        volumes = quote.get("volume") or []

        if not timestamps:
            return None

        candles: list[dict] = []
        for i, ts in enumerate(timestamps):
            # Skip bars with missing OHLC (Yahoo returns nulls for gaps)
            o = opens[i] if i < len(opens) else None
            h = highs[i] if i < len(highs) else None
            lo = lows[i] if i < len(lows) else None
            c = closes[i] if i < len(closes) else None
            if o is None or h is None or lo is None or c is None:
                continue
            candles.append({
                "time":   datetime.fromtimestamp(int(ts), tz=timezone.utc).isoformat(),
                "open":   float(o),
                "high":   float(h),
                "low":    float(lo),
                "close":  float(c),
                "volume": int(volumes[i]) if i < len(volumes) and volumes[i] else 0,
                "source": "yahoo",
            })

        # Drop last bar (may be incomplete) then trim to requested limit
        if len(candles) > 1:
            candles = candles[:-1]
        candles = candles[-limit:]

        _cache[cache_key] = _CacheEntry(candles=candles, fetched_at=time.time())
        log.info("[yahoo] fetched %d %s candles from GC=F", len(candles), timeframe)
        return candles

    except Exception as exc:
        log.warning("[yahoo] parse failed: %s", exc)
        return None


def yahoo_status() -> dict:
    """Status dict for the provider-health diagnostics endpoint."""
    return {
        "enabled":       True,        # always available, no key
        "symbol":        _SYMBOL,
        "note":          "gold futures continuous (not spot XAU/USD)",
        "cached_pairs":  list(_cache.keys()),
        "cache_ttl_s":   CACHE_TTL_S,
    }


def invalidate_cache(pair: str | None = None) -> None:
    if pair is None:
        _cache.clear()
    else:
        for k in list(_cache.keys()):
            if k.startswith(pair.lower()):
                del _cache[k]


__all__ = ["get_yahoo_candles", "yahoo_status", "invalidate_cache"]
