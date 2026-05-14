# -*- coding: utf-8 -*-
"""
Live Feed Consumer
==================
Connects to the MT5 Windows Bridge (mt5_bridge/mt5_bridge.py) running on the
Windows host machine. Inside Docker, the host is reachable via the special
DNS name `host.docker.internal`.

Behaviour:
  - On every candle request, tries the bridge first.
  - If the bridge is unreachable (connection refused, timeout), falls back to
    synthetic demo candles so the signal engine never hard-fails.
  - Results cached for `CACHE_TTL_SECONDS` to avoid hammering the bridge.
  - Bridge availability is re-tested every `PROBE_INTERVAL_SECONDS` so the
    system automatically re-connects when the bridge comes online.

Environment:
  MT5_BRIDGE_HOST   default: host.docker.internal
  MT5_BRIDGE_PORT   default: 8765
  MT5_BRIDGE_TIMEOUT_S   default: 3
"""
from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import requests

log = logging.getLogger(__name__)

# ── Config ────────────────────────────────────────────────────────────────────

BRIDGE_HOST    = os.getenv("MT5_BRIDGE_HOST", "host.docker.internal")
BRIDGE_PORT    = int(os.getenv("MT5_BRIDGE_PORT", "8765"))
BRIDGE_TIMEOUT = float(os.getenv("MT5_BRIDGE_TIMEOUT_S", "3"))
BRIDGE_BASE    = f"http://{BRIDGE_HOST}:{BRIDGE_PORT}"

CACHE_TTL_SECONDS    = 60          # candle list cached 60 s
PROBE_INTERVAL_SECONDS = 30        # re-probe bridge availability every 30 s


# ── Internal cache ────────────────────────────────────────────────────────────

@dataclass
class _CacheEntry:
    candles:    list[dict]
    fetched_at: float


_candle_cache: dict[str, _CacheEntry] = {}    # key: "pair:timeframe"
_bridge_ok:     bool  = False
_last_probe:    float = 0.0


def _probe_bridge() -> bool:
    """Check if the MT5 bridge is reachable. Updates module-level state."""
    global _bridge_ok, _last_probe
    now = time.monotonic()
    if now - _last_probe < PROBE_INTERVAL_SECONDS:
        return _bridge_ok
    _last_probe = now
    try:
        r = requests.get(f"{BRIDGE_BASE}/health", timeout=BRIDGE_TIMEOUT)
        _bridge_ok = r.status_code == 200
    except Exception:
        _bridge_ok = False
    if _bridge_ok:
        log.info("[live_feed] MT5 bridge online at %s", BRIDGE_BASE)
    else:
        log.debug("[live_feed] MT5 bridge not reachable at %s — using synthetic data", BRIDGE_BASE)
    return _bridge_ok


def bridge_status() -> dict:
    """Public status dict for the /engine/status endpoint."""
    return {
        "bridge_url":  BRIDGE_BASE,
        "connected":   _probe_bridge(),
        "last_probe":  datetime.fromtimestamp(_last_probe, tz=timezone.utc).isoformat()
                       if _last_probe else None,
    }


# ── Public API ────────────────────────────────────────────────────────────────

def get_live_candles(pair: str, timeframe: str = "H4", limit: int = 300) -> list[dict] | None:
    """
    Fetch OHLCV candle list from the best available live source.

    Priority:
      1. MT5 Windows Bridge  (live broker prices — highest quality)
      2. TradingView         (real market data via tvDatafeed)
      3. Returns None        (caller falls back to synthetic)

    Returns:
        list of dicts with keys: time (ISO str), open, high, low, close, volume
        None if all live sources unavailable
    """
    # ── Priority 1: MT5 Windows Bridge ────────────────────────────────────
    if _probe_bridge():
        cache_key = f"mt5:{pair}:{timeframe}"
        entry = _candle_cache.get(cache_key)
        if entry and (time.monotonic() - entry.fetched_at) < CACHE_TTL_SECONDS:
            return entry.candles
        try:
            url = f"{BRIDGE_BASE}/candles/{pair}/{timeframe}"
            r = requests.get(url, params={"limit": limit}, timeout=BRIDGE_TIMEOUT)
            if r.status_code == 200:
                candles = r.json()["data"]["candles"]
                _candle_cache[cache_key] = _CacheEntry(candles=candles, fetched_at=time.monotonic())
                log.info("[live_feed] MT5 bridge: %d %s candles for %s", len(candles), timeframe, pair)
                return candles
        except Exception as exc:
            log.warning("[live_feed] MT5 bridge fetch failed: %s", exc)

    # ── Priority 2: TradingView ────────────────────────────────────────────
    try:
        from services.tradingview_provider import get_tv_candles
        tv_candles = get_tv_candles(pair, timeframe=timeframe, limit=limit)
        if tv_candles:
            return tv_candles
    except Exception as exc:
        log.debug("[live_feed] TradingView unavailable: %s", exc)

    return None


def get_multi_candles(pair: str) -> dict[str, list[dict]] | None:
    """
    Fetch all timeframes (D1, H4, H1, M15) in one call from the bridge.
    Returns dict: { "D1": [...], "H4": [...], "H1": [...], "M15": [...] }
    Returns None if bridge unavailable.
    """
    if not _probe_bridge():
        return None

    cache_key = f"{pair}:MULTI"
    entry = _candle_cache.get(cache_key)
    if entry and (time.monotonic() - entry.fetched_at) < CACHE_TTL_SECONDS:
        return entry.candles  # type: ignore[return-value]

    try:
        url = f"{BRIDGE_BASE}/multi/{pair}"
        r = requests.get(url, timeout=BRIDGE_TIMEOUT * 2)   # multi takes longer
        if r.status_code != 200:
            return None
        data = r.json()
        tf_data = data["data"]["timeframes"]
        _candle_cache[cache_key] = _CacheEntry(candles=tf_data, fetched_at=time.monotonic())  # type: ignore[arg-type]
        log.info("[live_feed] Fetched multi-TF live candles for %s", pair)
        return tf_data
    except Exception as exc:
        log.warning("[live_feed] Multi-candle fetch failed for %s: %s", pair, exc)
        return None


def get_live_tick(pair: str) -> dict | None:
    """
    Latest bid/ask from the bridge.
    Returns dict with keys: bid, ask, spread, time — or None.
    """
    if not _probe_bridge():
        return None
    try:
        r = requests.get(f"{BRIDGE_BASE}/tick/{pair}", timeout=BRIDGE_TIMEOUT)
        if r.status_code == 200:
            return r.json()["data"]
    except Exception:
        pass
    return None


def invalidate_cache(pair: str | None = None) -> None:
    """Force-expire cached candle data so next call hits the bridge."""
    if pair is None:
        _candle_cache.clear()
    else:
        for key in list(_candle_cache.keys()):
            if key.startswith(pair):
                del _candle_cache[key]
