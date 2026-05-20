"""
Institutional Flow API
======================

Returns LIVE institutional positioning data for XAU/USD:

  - CFTC COT (Commitments of Traders) — weekly real positioning from cot_provider
  - Computed price levels from recent live candles:
      * Swing highs / lows (institutional liquidity pools)
      * Recent FVG (Fair Value Gaps)
      * Daily / weekly key levels
  - Optional MyFxBook retail sentiment (when credentials work)

Anything that doesn't have a real data source is OMITTED rather than mocked.
This replaces the stale hard-coded bank reference levels (1.09000, 1.08780,
etc.) that the old InstitutionalPanel was displaying — those were EUR/USD-era
mock values with no provider behind them.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Query, Request

from models.common import APIResponse
from rate_limit import limiter

router = APIRouter(prefix="/institutional", tags=["institutional"])
log = logging.getLogger(__name__)


def _compute_key_levels(limit: int = 200) -> dict:
    """
    Compute institutional price levels from live H4 candles:
      - swing_highs / swing_lows: recent significant pivots
      - last_fvg_bull / last_fvg_bear: most recent unfilled fair-value gaps
      - daily_open, daily_high, daily_low: today's session boundaries
      - prev_daily_high, prev_daily_low: yesterday's range (liquidity above/below)
    """
    out = {
        "current_price":   None,
        "daily_open":      None,
        "daily_high":      None,
        "daily_low":       None,
        "prev_daily_high": None,
        "prev_daily_low":  None,
        "swing_highs":     [],
        "swing_lows":      [],
        "last_fvg_bull":   None,
        "last_fvg_bear":   None,
        "data_source":     None,
    }
    try:
        from data.candles import get_candles
        resp = get_candles(interval="H4", limit=limit, pair="xauusd")
        candles = resp.candles
        out["data_source"] = getattr(resp, "source", "unknown")
    except Exception as exc:
        log.warning("[institutional] candle fetch failed: %s", exc)
        return out

    if not candles:
        return out

    # Refuse to compute levels from synthetic data
    if out["data_source"] not in {"tradingview", "mt5", "tradingview-cached", "mt5-cached"}:
        out["error"] = (
            f"Refusing to compute institutional levels from {out['data_source']} data — "
            "live provider not available."
        )
        return out

    closes = [c.close for c in candles]
    highs  = [c.high  for c in candles]
    lows   = [c.low   for c in candles]
    opens  = [c.open  for c in candles]

    out["current_price"] = round(closes[-1], 2)

    # Pivots: a swing-high candle has higher high than neighbours on both sides
    PIVOT_LOOKBACK = 3   # how many bars on each side to qualify as a pivot
    swing_highs: list[dict] = []
    swing_lows: list[dict]  = []
    for i in range(PIVOT_LOOKBACK, len(candles) - PIVOT_LOOKBACK):
        h = highs[i]
        l = lows[i]
        is_pivot_high = all(h >= highs[i + j] and h >= highs[i - j] for j in range(1, PIVOT_LOOKBACK + 1))
        is_pivot_low  = all(l <= lows[i  + j] and l <= lows[i  - j] for j in range(1, PIVOT_LOOKBACK + 1))
        if is_pivot_high:
            swing_highs.append({"price": round(h, 2), "time": candles[i].time.isoformat()})
        if is_pivot_low:
            swing_lows.append({"price": round(l, 2), "time": candles[i].time.isoformat()})

    # Keep only the most recent 5 of each, sorted newest first
    out["swing_highs"] = swing_highs[-5:][::-1]
    out["swing_lows"]  = swing_lows[-5:][::-1]

    # Fair Value Gap detection: 3-candle pattern where candle[i+2].low > candle[i].high
    # (bullish FVG — gap between bar i high and bar i+2 low) or vice versa
    for i in range(len(candles) - 3, max(len(candles) - 50, 1), -1):
        # Bullish FVG: gap above bar i
        if i + 2 < len(candles) and candles[i + 2].low > candles[i].high:
            out["last_fvg_bull"] = {
                "lower": round(candles[i].high, 2),
                "upper": round(candles[i + 2].low, 2),
                "mid":   round((candles[i].high + candles[i + 2].low) / 2, 2),
                "time":  candles[i + 1].time.isoformat(),
                "filled": closes[-1] <= candles[i].high,   # filled if price came back through
            }
            break

    for i in range(len(candles) - 3, max(len(candles) - 50, 1), -1):
        # Bearish FVG: gap below bar i
        if i + 2 < len(candles) and candles[i + 2].high < candles[i].low:
            out["last_fvg_bear"] = {
                "lower": round(candles[i + 2].high, 2),
                "upper": round(candles[i].low, 2),
                "mid":   round((candles[i].low + candles[i + 2].high) / 2, 2),
                "time":  candles[i + 1].time.isoformat(),
                "filled": closes[-1] >= candles[i].low,
            }
            break

    # Today's vs yesterday's daily range (group H4 candles by UTC date)
    by_date: dict[str, list] = {}
    for c in candles:
        ct = c.time if c.time.tzinfo else c.time.replace(tzinfo=timezone.utc)
        day = ct.astimezone(timezone.utc).strftime("%Y-%m-%d")
        by_date.setdefault(day, []).append(c)

    days_sorted = sorted(by_date.keys())
    if days_sorted:
        today_bars = by_date[days_sorted[-1]]
        out["daily_open"] = round(today_bars[0].open, 2)
        out["daily_high"] = round(max(c.high for c in today_bars), 2)
        out["daily_low"]  = round(min(c.low  for c in today_bars), 2)
        if len(days_sorted) >= 2:
            prev_bars = by_date[days_sorted[-2]]
            out["prev_daily_high"] = round(max(c.high for c in prev_bars), 2)
            out["prev_daily_low"]  = round(min(c.low  for c in prev_bars), 2)

    return out


def _get_sentiment_safe() -> dict | None:
    """Pull MyFxBook sentiment if available; return None if not configured / fails."""
    try:
        from config import settings
        if not settings.myfxbook_enabled:
            return None
        from services.myfxbook_provider import get_xauusd_sentiment
        return get_xauusd_sentiment()
    except Exception as exc:
        log.debug("[institutional] sentiment unavailable: %s", exc)
        return None


@router.get(
    "",
    response_model=APIResponse[dict],
    summary="Live institutional flow for XAU/USD (COT + computed price levels)",
)
@limiter.limit("30/minute")
def institutional(
    request: Request,
    levels_limit: int = Query(default=200, ge=50, le=1000,
                              description="H4 candles used to compute swing levels"),
) -> APIResponse[dict]:
    """
    Combines:
      - Live CFTC COT data for XAU/USD (commercials, large specs, small specs)
      - Computed price levels (swing highs/lows, FVGs, daily/prev-daily ranges)
      - Optional MyFxBook sentiment

    If a data source is unavailable we surface that explicitly rather than
    serving stale mock values.
    """
    from services.cot_provider import get_cot_data

    # COT (live CFTC fetch)
    try:
        cot = get_cot_data("XAU/USD")
    except Exception as exc:
        log.warning("[institutional] COT fetch failed: %s", exc)
        cot = {"pair": "XAU/USD", "source": "unavailable", "error": str(exc)}

    levels    = _compute_key_levels(limit=levels_limit)
    sentiment = _get_sentiment_safe()

    return APIResponse(
        data={
            "instrument":   "XAU/USD",
            "cot":          cot,
            "levels":       levels,
            "sentiment":    sentiment,    # None when provider unavailable
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "providers": {
                "cot":       cot.get("source", "unknown"),
                "levels":    levels.get("data_source", "unknown"),
                "sentiment": "myfxbook" if sentiment else "unavailable",
            },
        },
        source="institutional_flow",
    )
