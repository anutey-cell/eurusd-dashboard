"""
Candlestick Reader — Pattern Recognition as Setup-Score Bonus
==============================================================

Scores the current M15 (or H1) tape for institutional-conviction
candlestick patterns AT the entry zone. Purely additive: returns
0-10 points that add to setup_score. Never subtracts, never blocks
a signal.

Rationale: the mandate's C3 checks "3-bar majority direction" which is
colour, not pattern. A perfect pin bar rejecting the previous-day POC
prints exactly the same 1-red / 2-green signature as three random
candles drifting. This module surfaces the actual pattern so a
2-bar rejection at a mapped liquidity level actually counts.

Patterns detected (per direction):

  BUY (bullish rejection at entry):
    - Bullish pin bar / hammer     — long lower wick, close in upper 1/3
    - Bullish engulfing            — current green wraps prior red body
    - Inside-bar break-up          — inside bar followed by higher-high close
    - Two-bar swing failure (buy)  — prior red made a low, current green
                                      closes above prior red's open
    - Green marubozu               — body >= 80% of range, closes near high

  SELL (bearish rejection at entry): mirror

Each detected pattern contributes fixed points (see PATTERN_POINTS).
Points cap at MAX_BONUS. Every pattern requires at_zone=True to count —
a pin bar in mid-air is worth nothing.

Zone matching: entry_zone_low ± zone_tolerance_pts (default 3.0)
OR any of `liquidity_zones` (PDH / PDL / POC / VAH / VAL / VWAP) within
the same tolerance.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Optional, Iterable

log = logging.getLogger(__name__)


MAX_BONUS         = 10
DEFAULT_ZONE_TOL  = 3.0    # points; scaled by ATR at call site if provided


PATTERN_POINTS = {
    "pin_bar":              4,
    "engulfing":            4,
    "inside_break":         3,
    "two_bar_reversal":     3,
    "marubozu":             2,
    # cumulative cap = MAX_BONUS
}


# ── Pure per-pattern detectors ─────────────────────────────────────────────

def _body(c) -> float:
    return abs(c.close - c.open)

def _range(c) -> float:
    return max(c.high - c.low, 1e-9)

def _upper_wick(c) -> float:
    return c.high - max(c.open, c.close)

def _lower_wick(c) -> float:
    return min(c.open, c.close) - c.low

def _is_bull(c) -> bool: return c.close > c.open
def _is_bear(c) -> bool: return c.close < c.open


def detect_pin_bar(c, direction: str,
                    wick_body_ratio: float = 2.0,
                    close_position: float = 0.33) -> bool:
    """Long-wick rejection candle. wick >= 2 × body, close in far 1/3."""
    body = _body(c)
    if body <= 0:
        return False
    rng = _range(c)
    if direction == "BUY":
        lower = _lower_wick(c)
        if lower < wick_body_ratio * body:
            return False
        # Close in upper third of range
        close_pos = (c.close - c.low) / rng
        return close_pos >= (1.0 - close_position)
    if direction == "SELL":
        upper = _upper_wick(c)
        if upper < wick_body_ratio * body:
            return False
        close_pos = (c.close - c.low) / rng
        return close_pos <= close_position
    return False


def detect_engulfing(prev, cur, direction: str,
                      min_body_ratio: float = 1.0) -> bool:
    """Current body engulfs prior body in the trade direction."""
    prev_body = _body(prev)
    cur_body  = _body(cur)
    if prev_body <= 0 or cur_body < min_body_ratio * prev_body:
        return False
    if direction == "BUY":
        return (_is_bear(prev) and _is_bull(cur)
                and cur.close > prev.open and cur.open <= prev.close)
    if direction == "SELL":
        return (_is_bull(prev) and _is_bear(cur)
                and cur.close < prev.open and cur.open >= prev.close)
    return False


def detect_inside_bar_break(bars: list, direction: str) -> bool:
    """
    3-bar sequence: [mother][inside][breakout].
      - inside bar range strictly inside mother bar range
      - breakout closes above (BUY) or below (SELL) mother's extreme
    """
    if len(bars) < 3:
        return False
    mother, inside, breakout = bars[-3], bars[-2], bars[-1]
    inside_ok = inside.high <= mother.high and inside.low >= mother.low
    if not inside_ok:
        return False
    if direction == "BUY":
        return breakout.close > mother.high
    if direction == "SELL":
        return breakout.close < mother.low
    return False


def detect_two_bar_reversal(prev, cur, direction: str,
                              wick_atr_ratio: float = 0.3,
                              atr: float = 0.0) -> bool:
    """
    Two-bar swing-failure pattern:
      BUY:  prior red made a new low with a wick below, current green
            closes above prior red's OPEN (invalidating the down-move)
      SELL: prior green made a new high, current red closes below
            prior green's OPEN.

    ATR-aware wick check: skipped when atr is 0.
    """
    if direction == "BUY":
        if not (_is_bear(prev) and _is_bull(cur)):
            return False
        if atr > 0 and _lower_wick(prev) < wick_atr_ratio * atr:
            return False
        return cur.close > prev.open
    if direction == "SELL":
        if not (_is_bull(prev) and _is_bear(cur)):
            return False
        if atr > 0 and _upper_wick(prev) < wick_atr_ratio * atr:
            return False
        return cur.close < prev.open
    return False


def detect_marubozu(c, direction: str,
                     body_range_ratio: float = 0.8) -> bool:
    """Big-body, small-wick candle in the trade direction."""
    rng = _range(c)
    if rng <= 0:
        return False
    body_ratio = _body(c) / rng
    if body_ratio < body_range_ratio:
        return False
    if direction == "BUY":
        return _is_bull(c)
    if direction == "SELL":
        return _is_bear(c)
    return False


# ── Zone proximity ────────────────────────────────────────────────────────

def _at_zone(price: float, zones: Iterable[float], tol: float) -> bool:
    for z in zones:
        if z is None:
            continue
        if abs(price - z) <= tol:
            return True
    return False


# ── Public evaluator ──────────────────────────────────────────────────────

@dataclass(frozen=True)
class CandlestickVerdict:
    bonus:    int
    patterns: tuple
    detail:   str
    at_zone:  bool


def evaluate_candlestick_confluence(
    candles: list,
    direction: str,
    *,
    entry_zone_low:  Optional[float] = None,
    entry_zone_high: Optional[float] = None,
    liquidity_zones: Optional[Iterable[float]] = None,
    atr: float = 0.0,
    zone_tolerance:  Optional[float] = None,
) -> CandlestickVerdict:
    """
    Public entry. Returns a CandlestickVerdict with bonus in [0, MAX_BONUS].

    - `candles` : list of Candle objects (must have open/high/low/close).
                   Uses only the last 3 bars.
    - `direction` : "BUY" | "SELL"
    - `entry_zone_low/high` : if provided, current close within [low-tol, high+tol]
                               marks at_zone=True
    - `liquidity_zones` : optional extra reference levels (PDH/PDL/POC/VAH/VAL)
                          that also count as at_zone when close is within tol
    - `atr` : H1 or M15 ATR; used to scale the zone tolerance if
              `zone_tolerance` isn't provided (tol = max(3, 0.3*atr))

    Never raises. Empty inputs → bonus=0 with detail="insufficient data".
    """
    if direction not in ("BUY", "SELL"):
        return CandlestickVerdict(0, (), "direction n/a", False)
    if not candles or len(candles) < 3:
        return CandlestickVerdict(0, (), "insufficient candles (<3)", False)

    tol = zone_tolerance
    if tol is None:
        tol = max(DEFAULT_ZONE_TOL, 0.3 * float(atr or 0.0))

    cur    = candles[-1]
    prev   = candles[-2]
    last3  = candles[-3:]

    # Build zone list
    zones = []
    if entry_zone_low is not None:  zones.append(entry_zone_low)
    if entry_zone_high is not None: zones.append(entry_zone_high)
    if liquidity_zones:             zones.extend([z for z in liquidity_zones if z is not None])

    at_zone = _at_zone(cur.close, zones, tol) if zones else False

    # Detect each pattern
    patterns: list[dict] = []
    if detect_pin_bar(cur, direction):
        patterns.append({"name": "pin_bar",          "points": PATTERN_POINTS["pin_bar"]})
    if detect_engulfing(prev, cur, direction):
        patterns.append({"name": "engulfing",        "points": PATTERN_POINTS["engulfing"]})
    if detect_inside_bar_break(last3, direction):
        patterns.append({"name": "inside_break",     "points": PATTERN_POINTS["inside_break"]})
    if detect_two_bar_reversal(prev, cur, direction, atr=atr):
        patterns.append({"name": "two_bar_reversal", "points": PATTERN_POINTS["two_bar_reversal"]})
    if detect_marubozu(cur, direction):
        patterns.append({"name": "marubozu",         "points": PATTERN_POINTS["marubozu"]})

    # Only patterns AT ZONE contribute to bonus (rejection at a level > rejection in space)
    scored = patterns if at_zone else []
    bonus  = min(MAX_BONUS, sum(p["points"] for p in scored))

    if not patterns:
        detail = "no pattern"
    elif not at_zone:
        detail = f"pattern present but not at zone: " \
                  + ", ".join(p["name"] for p in patterns)
    else:
        detail = "at zone · " + ", ".join(f"{p['name']}(+{p['points']})" for p in scored)

    return CandlestickVerdict(
        bonus=bonus,
        patterns=tuple(p["name"] for p in patterns),
        detail=detail,
        at_zone=at_zone,
    )


__all__ = [
    "evaluate_candlestick_confluence",
    "CandlestickVerdict",
    "detect_pin_bar", "detect_engulfing", "detect_inside_bar_break",
    "detect_two_bar_reversal", "detect_marubozu",
    "MAX_BONUS", "PATTERN_POINTS",
]
