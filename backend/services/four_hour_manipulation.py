"""
4H Manipulation Detection Layer
================================

Higher-timeframe trap filter. Detects when Gold sweeps the previous
completed 4H high or low, fails to hold beyond, and reclaims into the
prior range on M15 — trapping breakout traders on the wrong side.

Confluence layer only:
  - Never creates a trade
  - Only adjusts setup_score (±10)
  - Never bypasses news / RR / freshness
  - Fails silent on missing data (detected=False, adjustment=0)

The −10 case is the critical asymmetry: a sweep that DOESN'T reclaim
is a genuine breakout / continuation, and pattern-matching it as
"failed sweep" would fade the trend. We penalize that state explicitly.

Public API:
    detect_4h_manipulation(h4_prev, h4_current, candles_m15,
                             candles_m5, atr_h1, liquidity_map)
        Returns the four_hour_manipulation dict per operator brief.
"""
from __future__ import annotations

import logging
from typing import Optional

log = logging.getLogger(__name__)


# ── Tunable parameters ─────────────────────────────────────────────────────

DEFAULT_MIN_SWEEP_PTS       = 1.5    # sweep must exceed prev-hi/lo by at least this many pts
DEFAULT_CONTINUATION_ATR_MULT = 0.75 # if price is > 0.75×ATR beyond the level → continuation
DEFAULT_M15_STRUCTURE_LOOKBACK = 3   # last N M15 candles for structure-shift check
DEFAULT_M5_LOOKBACK         = 4      # last N M5 candles for LTF confirmation


# ── Helpers ────────────────────────────────────────────────────────────────

def _ohlc(candle) -> dict:
    """Coerce any candle-like (dataclass/dict) into a plain dict."""
    if candle is None:
        return {}
    if isinstance(candle, dict):
        return candle
    return {
        "open":  float(getattr(candle, "open", 0) or 0),
        "high":  float(getattr(candle, "high", 0) or 0),
        "low":   float(getattr(candle, "low",  0) or 0),
        "close": float(getattr(candle, "close", 0) or 0),
    }


def _last_prev_4h(h4_candles: list) -> tuple[Optional[dict], Optional[dict]]:
    """Return (prev_completed, current_forming) 4H candles.
    Assumes h4_candles is time-ordered with the LATEST at the end."""
    if not h4_candles or len(h4_candles) < 2:
        return (None, None)
    current = _ohlc(h4_candles[-1])
    prev    = _ohlc(h4_candles[-2])
    return (prev, current)


def _m15_close_reclaimed(candles_m15: list, direction: str,
                          sweep_level: float) -> bool:
    """After the sweep, has an M15 CLOSE fallen back inside the 4H range?"""
    if not candles_m15:
        return False
    last_close = float(getattr(candles_m15[-1], "close", 0) or 0)
    if direction == "bearish":
        return last_close < sweep_level
    if direction == "bullish":
        return last_close > sweep_level
    return False


def _m5_confirmation(candles_m5: Optional[list], direction: str,
                      sweep_level: float) -> bool:
    """LTF confirmation — last N M5 closes on the reclaim side."""
    if not candles_m5 or len(candles_m5) < DEFAULT_M5_LOOKBACK:
        return False
    recent = candles_m5[-DEFAULT_M5_LOOKBACK:]
    if direction == "bearish":
        below = sum(1 for c in recent
                     if float(getattr(c, "close", 0) or 0) < sweep_level)
        return below >= (DEFAULT_M5_LOOKBACK // 2 + 1)
    if direction == "bullish":
        above = sum(1 for c in recent
                     if float(getattr(c, "close", 0) or 0) > sweep_level)
        return above >= (DEFAULT_M5_LOOKBACK // 2 + 1)
    return False


def _m15_structure_shift(candles_m15: list, direction: str) -> bool:
    """Simplified: last 3 M15 majority close in direction of the reclaim."""
    if not candles_m15 or len(candles_m15) < DEFAULT_M15_STRUCTURE_LOOKBACK:
        return False
    recent = candles_m15[-DEFAULT_M15_STRUCTURE_LOOKBACK:]
    if direction == "bearish":
        down = sum(1 for c in recent
                    if float(getattr(c, "close", 0)) < float(getattr(c, "open", 0)))
        return down >= (DEFAULT_M15_STRUCTURE_LOOKBACK // 2 + 1)
    if direction == "bullish":
        up = sum(1 for c in recent
                  if float(getattr(c, "close", 0)) > float(getattr(c, "open", 0)))
        return up >= (DEFAULT_M15_STRUCTURE_LOOKBACK // 2 + 1)
    return False


def _empty(reason: str = "insufficient data") -> dict:
    return {
        "detected":               False,
        "direction":              "none",
        "swept_level":            None,
        "sweep_type":             "none",
        "reclaimed":              False,
        "m15_confirmation":       False,
        "m5_confirmation":        False,
        "trapped_participants":   "none",
        "confidence_adjustment":  0,
        "trade_bias":             "STAND_ASIDE",
        "reason":                 reason,
    }


# ── Public API ─────────────────────────────────────────────────────────────

def detect_4h_manipulation(
    h4_candles: list,                         # time-ordered H4 candle list
    *,
    candles_m15: list,
    candles_m5:  Optional[list] = None,
    atr_h1:      float = 0.0,
    liquidity_map=None,                       # optional; used for `matched` levels
    min_sweep_pts: float = DEFAULT_MIN_SWEEP_PTS,
    continuation_atr_mult: float = DEFAULT_CONTINUATION_ATR_MULT,
) -> dict:
    """
    Returns the four_hour_manipulation dict per the operator brief.
    Never raises. Fails silent as `_empty(...)` on any missing data.
    """
    try:
        prev, cur = _last_prev_4h(h4_candles)
        if prev is None or cur is None:
            return _empty("need at least 2 H4 candles")
        if not candles_m15 or len(candles_m15) < DEFAULT_M15_STRUCTURE_LOOKBACK:
            return _empty("insufficient M15 candles")

        prev_hi = prev.get("high")
        prev_lo = prev.get("low")
        if prev_hi is None or prev_lo is None:
            return _empty("prev-4H OHLC incomplete")

        # Sweep check — current-4H wick vs prev-4H hi/lo, plus a
        # minimum-excursion floor to filter noise ticks.
        cur_hi = cur.get("high", prev_hi)
        cur_lo = cur.get("low",  prev_lo)
        swept_hi = (cur_hi - prev_hi) > min_sweep_pts
        swept_lo = (prev_lo - cur_lo) > min_sweep_pts

        # Ambiguous (extremely rare on 4H): swept both — skip
        if swept_hi and swept_lo:
            return _empty("both sides swept — ambiguous")
        if not (swept_hi or swept_lo):
            return _empty("no sweep of prev-4H hi/lo")

        # Set up the manipulation candidate
        if swept_hi:
            direction    = "bearish"
            sweep_type   = "previous_4h_high"
            swept_level  = prev_hi
            trapped      = "buyers"
            trade_bias   = "SELL"
        else:  # swept_lo
            direction    = "bullish"
            sweep_type   = "previous_4h_low"
            swept_level  = prev_lo
            trapped      = "sellers"
            trade_bias   = "BUY"

        reclaimed = _m15_close_reclaimed(candles_m15, direction, swept_level)

        # −10 case: continuation, not manipulation. If price is well past
        # the level AND has NOT reclaimed, we're in a breakout hold.
        current_price = float(getattr(candles_m15[-1], "close", 0) or 0)
        beyond_pts = (current_price - prev_hi) if swept_hi else (prev_lo - current_price)
        continuation_thresh = max(3.0, (atr_h1 or 0) * continuation_atr_mult)
        if not reclaimed and beyond_pts > continuation_thresh:
            return {
                "detected":              True,
                "direction":             direction,
                "swept_level":           round(swept_level, 2),
                "sweep_type":            sweep_type,
                "reclaimed":             False,
                "m15_confirmation":      False,
                "m5_confirmation":       False,
                "trapped_participants":  "none",
                "confidence_adjustment": -10,
                "trade_bias":            "STAND_ASIDE",
                "reason":                (f"Continuation — price held {beyond_pts:.1f} pts "
                                           f"past prev-4H {'high' if swept_hi else 'low'} "
                                           f"({swept_level:.2f}); "
                                           "penalize opposing setups"),
            }

        # Not-yet-reclaimed but not held-beyond either — waiting state
        if not reclaimed:
            return {
                "detected":              True,
                "direction":             direction,
                "swept_level":           round(swept_level, 2),
                "sweep_type":            sweep_type,
                "reclaimed":             False,
                "m15_confirmation":      False,
                "m5_confirmation":       False,
                "trapped_participants":  trapped,
                "confidence_adjustment": 0,
                "trade_bias":            "STAND_ASIDE",
                "reason":                (f"Sweep of prev-4H {'high' if swept_hi else 'low'} "
                                           f"@ {swept_level:.2f} not yet reclaimed on M15"),
            }

        # Reclaimed — full manipulation signal. Grade the LTF confirmation.
        m15_conf = _m15_structure_shift(candles_m15, direction)
        m5_conf  = _m5_confirmation(candles_m5, direction, swept_level)

        if m15_conf:
            adj    = 10
            reason = (f"4H manipulation — swept {sweep_type} @ {swept_level:.2f}, "
                       f"reclaimed on M15 with structure shift · {trapped} trapped")
        else:
            adj    = 5
            reason = (f"4H manipulation — swept {sweep_type} @ {swept_level:.2f}, "
                       f"M15 reclaimed but structure shift weak · {trapped} trapped")

        return {
            "detected":              True,
            "direction":             direction,
            "swept_level":           round(swept_level, 2),
            "sweep_type":            sweep_type,
            "reclaimed":             True,
            "m15_confirmation":      bool(m15_conf),
            "m5_confirmation":       bool(m5_conf),
            "trapped_participants":  trapped,
            "confidence_adjustment": adj,
            "trade_bias":            trade_bias,
            "reason":                reason,
        }

    except Exception as exc:
        log.debug("[4h_manipulation] unexpected: %s", exc)
        return _empty(f"error: {exc}")


__all__ = ["detect_4h_manipulation"]
