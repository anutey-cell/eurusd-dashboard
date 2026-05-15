"""
FRED (Federal Reserve Economic Data) provider.

Fetches real US macro data series that drive gold pricing:

  DGS10    — 10-Year Treasury Constant Maturity Rate (nominal yield)
  T10YIE   — 10-Year Breakeven Inflation Rate
  REAL_10Y — DGS10 - T10YIE (computed real yield — strongest gold inverse)
  DTWEXBGS — Broad Trade-Weighted USD Index (alternative to DXY)

Free FRED API key required: https://fred.stlouisfed.org/docs/api/api_key.html
Set FRED_API_KEY=<key> in .env. If unset, returns available=False and the
predictor falls back to its DXY-based fundamental proxy.

API endpoint: https://api.stlouisfed.org/fred/series/observations

Rate limits: 120 requests/min (we cache 1 hour per series).
"""
from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

import httpx

log = logging.getLogger(__name__)

# ── Config ────────────────────────────────────────────────────────────────────

FRED_API_KEY = os.getenv("FRED_API_KEY", "").strip()
BASE_URL = "https://api.stlouisfed.org/fred/series/observations"
CACHE_TTL = 3600   # 1 hour — daily data, no need to hammer

REQUEST_TIMEOUT = 10

# Series IDs of interest
SERIES = {
    "DGS10":    "10-Year Treasury Yield",
    "T10YIE":   "10-Year Breakeven Inflation",
    "DTWEXBGS": "Broad Trade-Weighted USD Index",
}


# ── Internal cache ────────────────────────────────────────────────────────────

@dataclass
class _CacheEntry:
    value:       float
    prev_value:  float
    delta:       float           # signed change vs prev observation
    series_id:   str
    obs_date:    str             # YYYY-MM-DD of latest observation
    fetched_at:  float           # epoch seconds


_cache: dict[str, _CacheEntry] = {}


def _fetch_series(series_id: str, limit: int = 5) -> Optional[_CacheEntry]:
    """Pull recent observations for a FRED series and return cached entry."""
    if not FRED_API_KEY:
        return None
    now = time.time()
    cached = _cache.get(series_id)
    if cached and (now - cached.fetched_at) < CACHE_TTL:
        return cached

    try:
        resp = httpx.get(BASE_URL, params={
            "series_id":          series_id,
            "api_key":            FRED_API_KEY,
            "file_type":          "json",
            "sort_order":         "desc",      # newest first
            "limit":              limit,
        }, timeout=REQUEST_TIMEOUT)
        if resp.status_code != 200:
            log.warning("[fred] %s returned HTTP %s", series_id, resp.status_code)
            return None
        data = resp.json()
        obs = data.get("observations", [])
        # FRED occasionally returns "." for missing data — filter those
        good = [o for o in obs if o.get("value", ".") not in (".", None, "")]
        if len(good) < 2:
            log.warning("[fred] %s insufficient observations", series_id)
            return None
        latest = float(good[0]["value"])
        prev   = float(good[1]["value"])
        entry = _CacheEntry(
            value=latest, prev_value=prev, delta=latest - prev,
            series_id=series_id, obs_date=good[0].get("date", ""),
            fetched_at=now,
        )
        _cache[series_id] = entry
        log.info(
            "[fred] %s = %.3f (Δ %+.3f) on %s",
            series_id, latest, entry.delta, entry.obs_date,
        )
        return entry
    except Exception as exc:
        log.warning("[fred] %s fetch failed: %s", series_id, exc)
        return None


# ═══════════════════════════════════════════════════════════════════════════════
# Public API
# ═══════════════════════════════════════════════════════════════════════════════

def get_yields_context() -> dict:
    """
    Return a structured macro-context dict driven by real FRED data.

    Output keys:
      available:        bool — True if all required series fetched OK
      dgs10:            10Y nominal yield (latest)
      dgs10Delta:       signed change vs previous observation
      t10yie:           10Y breakeven inflation (latest)
      t10yieDelta:      signed change
      realYield10y:     DGS10 - T10YIE — the strongest gold inverse correlate
      realYieldDelta:   signed change in real yield
      yieldsTrend:      "rising" | "falling" | "flat"
      goldImpact:       "bearish" | "bullish" | "neutral"
      lastUpdate:       ISO date of latest observation
      source:           "fred"
    """
    out = {
        "available":      False,
        "source":         "fred",
        "yieldsTrend":    "unknown",
        "goldImpact":     "neutral",
    }
    if not FRED_API_KEY:
        out["error"] = "FRED_API_KEY not configured — set it in .env to enable real yields"
        return out

    dgs10  = _fetch_series("DGS10")
    t10yie = _fetch_series("T10YIE")

    if dgs10 is None or t10yie is None:
        out["error"] = "FRED fetch failed (network or invalid key)"
        return out

    real_yield      = dgs10.value - t10yie.value
    real_yield_prev = dgs10.prev_value - t10yie.prev_value
    real_delta      = real_yield - real_yield_prev

    # Trend classification — based on real yield direction (the strongest gold driver)
    if real_delta > 0.05:
        trend = "rising"
        gold_impact = "bearish"      # rising real yields are bearish for gold
    elif real_delta < -0.05:
        trend = "falling"
        gold_impact = "bullish"      # falling real yields are bullish for gold
    else:
        trend = "flat"
        gold_impact = "neutral"

    out.update({
        "available":       True,
        "dgs10":           round(dgs10.value, 3),
        "dgs10Delta":      round(dgs10.delta, 3),
        "t10yie":          round(t10yie.value, 3),
        "t10yieDelta":     round(t10yie.delta, 3),
        "realYield10y":    round(real_yield, 3),
        "realYieldDelta":  round(real_delta, 3),
        "yieldsTrend":     trend,
        "goldImpact":      gold_impact,
        "lastUpdate":      dgs10.obs_date,
    })
    return out


def fred_status() -> dict:
    """Health check for the FRED integration."""
    return {
        "enabled":      bool(FRED_API_KEY),
        "cachedSeries": list(_cache.keys()),
        "cacheAgeS":    {sid: round(time.time() - e.fetched_at) for sid, e in _cache.items()},
    }
