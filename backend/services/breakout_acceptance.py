"""
Breakout Acceptance Model — Phase 6
======================================

Replaces the current "every sweep = manipulation" heuristic with a
9-classification evaluator that judges each breakout on: close-through
strength, follow-through bars, time-outside, retest behaviour, ATR/vol
expansion, distance-from-level, session, and HTF supportiveness.

Nine classifications (symmetric bull/bear):

  LIQUIDITY_PROBE       wick past level, closed back — no acceptance
  FAILED_BREAKOUT       breached and closed beyond, but returned quickly
  BREAKOUT_DEVELOPING   1 close beyond, waiting for confirmation
  BREAKOUT_CONFIRMED    2+ closes beyond, no return yet
  BREAKOUT_ACCEPTANCE   3+ closes + ≥45 min outside range
  BREAKOUT_RETEST       broke, pulled back to level, held (or holding)
  CONTINUATION          breakout > 90 min old, still trending in break dir
  EXHAUSTED_BREAKOUT    far from level + displacement stalled
  BREAKOUT_INVALIDATED  retest failed → close back inside prior range

Behind `xauusd_breakout_acceptance_enabled`. Off by default. Exposed only
via /api/v1/diagnostics/breakout-acceptance for shadow-mode observation.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone, timedelta
from typing import Optional

log = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Classifications
# ─────────────────────────────────────────────────────────────────────────────

BK_LIQUIDITY_PROBE     = "LIQUIDITY_PROBE"
BK_FAILED              = "FAILED_BREAKOUT"
BK_DEVELOPING          = "BREAKOUT_DEVELOPING"
BK_CONFIRMED           = "BREAKOUT_CONFIRMED"
BK_ACCEPTED            = "BREAKOUT_ACCEPTANCE"
BK_RETEST              = "BREAKOUT_RETEST"
BK_CONTINUATION        = "CONTINUATION"
BK_EXHAUSTED           = "EXHAUSTED_BREAKOUT"
BK_INVALIDATED         = "BREAKOUT_INVALIDATED"
BK_NONE                = "NO_BREAKOUT"

# Thresholds
_MIN_BODY_PCT_BEYOND     = 0.30    # >= 30% of body beyond level = real breach
_FOLLOWTHROUGH_CONFIRMED = 2
_FOLLOWTHROUGH_ACCEPTED  = 3
_ACCEPTED_MIN_MINUTES    = 45
_CONTINUATION_MIN_MINUTES = 90
_EXHAUSTED_ATR_MULT       = 4.0
_RETEST_DEPTH_MAX_PCT     = 1.10   # <= 110% back to level (small overshoot ok)
_FAILED_RETURN_WITHIN_BARS = 3


# ─────────────────────────────────────────────────────────────────────────────
# Output
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class BreakoutAssessment:
    level:                  float
    level_name:             str
    direction:              str            # UP | DOWN
    classification:         str
    breakout_bar_time:      Optional[datetime] = None
    breakout_bar_close:     Optional[float]    = None
    close_beyond:           bool  = False
    body_pct_beyond:        float = 0.0
    followthrough_bars:     int   = 0
    time_outside_min:       int   = 0
    returned_to_range:      bool  = False
    retest_depth_pct:       float = 0.0
    higher_low_after:       bool  = False
    lower_high_after:       bool  = False
    atr_expansion_ratio:    float = 1.0
    volume_expansion_ratio: float = 1.0
    distance_from_level_atr: float = 0.0
    session_kz:             str = "?"
    htf_direction_supportive: bool = False
    confidence:             int  = 0
    warnings:               list[str] = field(default_factory=list)
    generated_at:           datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def to_dict(self) -> dict:
        d = asdict(self)
        d["breakout_bar_time"] = self.breakout_bar_time.isoformat() if self.breakout_bar_time else None
        d["generated_at"]      = self.generated_at.isoformat()
        return d


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _atr(bars, n=14):
    if len(bars) < n + 1:
        return None
    trs = []
    for i in range(1, len(bars)):
        h, l, pc = bars[i].high, bars[i].low, bars[i-1].close
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    if len(trs) < n:
        return None
    a = sum(trs[:n]) / n
    for tr in trs[n:]:
        a = ((a * (n - 1)) + tr) / n
    return a


def _body_pct(bar):
    rng = bar.high - bar.low
    return 0.0 if rng <= 0 else abs(bar.close - bar.open) / rng


def _find_last_breakout_up(bars_m15, level, lookback=48):
    """Return (index, bar) of the most recent M15 bar that FIRST crossed above
    level and closed above. `lookback` limits how far back we look."""
    if not bars_m15 or level is None:
        return (None, None)
    start = max(0, len(bars_m15) - lookback)
    for i in range(start, len(bars_m15)):
        b = bars_m15[i]
        if b.close > level:
            # Was the prior bar's close at or below?
            if i == 0 or bars_m15[i - 1].close <= level:
                return (i, b)
    return (None, None)


def _find_last_breakout_down(bars_m15, level, lookback=48):
    if not bars_m15 or level is None:
        return (None, None)
    start = max(0, len(bars_m15) - lookback)
    for i in range(start, len(bars_m15)):
        b = bars_m15[i]
        if b.close < level:
            if i == 0 or bars_m15[i - 1].close >= level:
                return (i, b)
    return (None, None)


def _wicked_past_but_closed_back_up(bars_m15, level, lookback=6):
    """True if any recent bar's high > level BUT close <= level (rejection)."""
    if not bars_m15 or level is None:
        return False
    for b in bars_m15[-lookback:]:
        if b.high > level and b.close <= level:
            return True
    return False


def _wicked_past_but_closed_back_down(bars_m15, level, lookback=6):
    if not bars_m15 or level is None:
        return False
    for b in bars_m15[-lookback:]:
        if b.low < level and b.close >= level:
            return True
    return False


def _pivot_high_low(bars, k=2):
    """Return (last_swing_high, last_swing_low) using k-bar pivot rule."""
    if len(bars) < 2 * k + 1:
        return (None, None)
    hi = lo = None
    for i in range(k, len(bars) - k):
        h, l = bars[i].high, bars[i].low
        if all(bars[j].high <= h for j in range(i - k, i + k + 1) if j != i):
            hi = h
        if all(bars[j].low >= l for j in range(i - k, i + k + 1) if j != i):
            lo = l
    return (hi, lo)


def _tf_supportive_up(htf_alignment) -> bool:
    if htf_alignment is None:
        return False
    d = getattr(htf_alignment, "direction", None)
    return d == "BULL"


def _tf_supportive_down(htf_alignment) -> bool:
    if htf_alignment is None:
        return False
    d = getattr(htf_alignment, "direction", None)
    return d == "BEAR"


# ─────────────────────────────────────────────────────────────────────────────
# Core classifier: one direction × one level
# ─────────────────────────────────────────────────────────────────────────────

def classify_breakout(
    snapshot,
    level: float,
    direction: str,
    *,
    level_name: str = "?",
    htf_alignment=None,
) -> BreakoutAssessment:
    """
    Classify the current state of a potential breakout of `level` in
    `direction` ∈ {"UP", "DOWN"}. Fails open — always returns an assessment.
    """
    warnings: list[str] = []
    if snapshot is None or level is None:
        return BreakoutAssessment(
            level=level or 0.0, level_name=level_name, direction=direction,
            classification=BK_NONE, warnings=["snapshot/level missing"],
        )

    tfs = snapshot.timeframes or {}
    m15 = tfs.get("M15", None).candles if tfs.get("M15") else []
    h1  = tfs.get("H1",  None).candles if tfs.get("H1")  else []

    if len(m15) < 8:
        return BreakoutAssessment(
            level=level, level_name=level_name, direction=direction,
            classification=BK_NONE, warnings=["insufficient M15 bars"],
        )

    session_kz = snapshot.session.kz_label if snapshot.session else "?"
    atr_h1 = _atr(h1, 14) or (max(b.high for b in m15[-14:]) - min(b.low for b in m15[-14:])) / 4
    current_price = m15[-1].close
    now = m15[-1].time

    # ── Locate the breakout bar ─────────────────────────────────────────────
    if direction == "UP":
        idx, bo_bar = _find_last_breakout_up(m15, level)
    elif direction == "DOWN":
        idx, bo_bar = _find_last_breakout_down(m15, level)
    else:
        return BreakoutAssessment(
            level=level, level_name=level_name, direction=direction,
            classification=BK_NONE, warnings=[f"unknown direction {direction!r}"],
        )

    # ── No fresh breakout in lookback window ─────────────────────────────────
    if idx is None:
        # Was there just a wick past?
        if direction == "UP" and _wicked_past_but_closed_back_up(m15, level):
            return _finalize(level, level_name, direction, BK_LIQUIDITY_PROBE,
                              session_kz, atr_h1, current_price, warnings,
                              htf_alignment=htf_alignment, confidence=60)
        if direction == "DOWN" and _wicked_past_but_closed_back_down(m15, level):
            return _finalize(level, level_name, direction, BK_LIQUIDITY_PROBE,
                              session_kz, atr_h1, current_price, warnings,
                              htf_alignment=htf_alignment, confidence=60)
        return _finalize(level, level_name, direction, BK_NONE,
                          session_kz, atr_h1, current_price, warnings,
                          htf_alignment=htf_alignment, confidence=0)

    # ── Compute metrics for this breakout ─────────────────────────────────────
    body_high = max(bo_bar.open, bo_bar.close)
    body_low  = min(bo_bar.open, bo_bar.close)
    body_size = max(0.0001, body_high - body_low)
    if direction == "UP":
        body_beyond = max(0.0, min(body_high, bo_bar.high) - max(level, body_low))
    else:
        body_beyond = max(0.0, min(body_high, level) - max(bo_bar.low, body_low))
    body_pct_beyond = min(1.0, body_beyond / body_size)

    # Follow-through bars
    followthrough = 0
    for j in range(idx + 1, len(m15)):
        b = m15[j]
        if direction == "UP" and b.close > level:
            followthrough += 1
        elif direction == "DOWN" and b.close < level:
            followthrough += 1
        else:
            break

    # Time-outside
    time_outside_min = int((now - bo_bar.time).total_seconds() / 60)

    # Returned to range?
    tail_after = m15[idx + 1: idx + 1 + _FAILED_RETURN_WITHIN_BARS]
    returned_to_range = False
    if direction == "UP":
        returned_to_range = any(b.close <= level for b in tail_after)
    else:
        returned_to_range = any(b.close >= level for b in tail_after)

    # Retest depth (how deep did price come back to level)
    retest_depth_pct = 0.0
    after = m15[idx + 1:]
    if after:
        if direction == "UP":
            min_low_after = min(b.low for b in after)
            depth = max(0.0, level - min_low_after)
            retest_depth_pct = depth / max(0.1, atr_h1)
        else:
            max_high_after = max(b.high for b in after)
            depth = max(0.0, max_high_after - level)
            retest_depth_pct = depth / max(0.1, atr_h1)

    # Higher low / lower high after breakout
    hi_after, lo_after = _pivot_high_low(m15[idx:], k=2)
    higher_low_after = False
    lower_high_after = False
    if direction == "UP" and lo_after and lo_after > level:
        higher_low_after = True
    if direction == "DOWN" and hi_after and hi_after < level:
        lower_high_after = True

    # ATR expansion (post-breakout ATR vs prior)
    atr_before = _atr(m15[max(0, idx - 20): idx], 10) if idx >= 12 else None
    atr_after  = _atr(m15[idx:], 10) if len(m15) - idx >= 12 else None
    atr_expansion_ratio = (atr_after / atr_before) if (atr_before and atr_after and atr_before > 0) else 1.0

    # Volume expansion
    vol_before = sum(b.volume for b in m15[max(0, idx - 20): idx]) / max(1, min(20, idx))
    vol_after  = sum(b.volume for b in m15[idx: idx + 6]) / max(1, min(6, len(m15) - idx))
    volume_expansion_ratio = (vol_after / vol_before) if vol_before > 0 else 1.0

    # Distance from level (in ATRs)
    distance_from_level_atr = abs(current_price - level) / max(0.1, atr_h1)

    # ── Classification decision tree ────────────────────────────────────────
    supportive = _tf_supportive_up(htf_alignment) if direction == "UP" \
                 else _tf_supportive_down(htf_alignment)

    # Failed: closed beyond but returned within N bars AND currently back inside
    inside_now = (direction == "UP" and current_price <= level) \
                 or (direction == "DOWN" and current_price >= level)

    if returned_to_range and inside_now:
        cls = BK_FAILED
        conf = 65
    elif body_pct_beyond < _MIN_BODY_PCT_BEYOND:
        # Weak breakout — wick more than body
        cls = BK_LIQUIDITY_PROBE
        conf = 55
    else:
        # Strong-enough breakout — evaluate follow-through + time
        if distance_from_level_atr >= _EXHAUSTED_ATR_MULT and atr_expansion_ratio < 1.1:
            cls = BK_EXHAUSTED
            conf = 65
        elif time_outside_min >= _CONTINUATION_MIN_MINUTES and followthrough >= 3:
            cls = BK_CONTINUATION
            conf = 80
        elif retest_depth_pct >= 0.6 and inside_now:
            # Retest overshoot back below level = invalidated
            cls = BK_INVALIDATED
            conf = 65
        elif retest_depth_pct > 0.15 and retest_depth_pct <= _RETEST_DEPTH_MAX_PCT and not inside_now:
            # Retested and held
            cls = BK_RETEST
            conf = 75
        elif followthrough >= _FOLLOWTHROUGH_ACCEPTED and time_outside_min >= _ACCEPTED_MIN_MINUTES:
            cls = BK_ACCEPTED
            conf = 85
        elif followthrough >= _FOLLOWTHROUGH_CONFIRMED:
            cls = BK_CONFIRMED
            conf = 75
        else:
            cls = BK_DEVELOPING
            conf = 60

    # HTF bonus/penalty
    if supportive and cls in (BK_ACCEPTED, BK_CONFIRMED, BK_CONTINUATION, BK_RETEST):
        conf = min(100, conf + 10)
    elif not supportive and cls in (BK_ACCEPTED, BK_CONFIRMED, BK_CONTINUATION, BK_RETEST):
        conf = max(0, conf - 10)

    return BreakoutAssessment(
        level=level, level_name=level_name, direction=direction,
        classification=cls,
        breakout_bar_time=bo_bar.time, breakout_bar_close=bo_bar.close,
        close_beyond=True, body_pct_beyond=round(body_pct_beyond, 3),
        followthrough_bars=followthrough,
        time_outside_min=time_outside_min,
        returned_to_range=returned_to_range,
        retest_depth_pct=round(retest_depth_pct, 3),
        higher_low_after=higher_low_after,
        lower_high_after=lower_high_after,
        atr_expansion_ratio=round(atr_expansion_ratio, 2),
        volume_expansion_ratio=round(volume_expansion_ratio, 2),
        distance_from_level_atr=round(distance_from_level_atr, 2),
        session_kz=session_kz,
        htf_direction_supportive=supportive,
        confidence=conf,
        warnings=warnings,
    )


def _finalize(level, level_name, direction, cls, session_kz, atr_h1,
              current_price, warnings, *, htf_alignment=None, confidence=0):
    return BreakoutAssessment(
        level=level, level_name=level_name, direction=direction,
        classification=cls,
        distance_from_level_atr=round(abs(current_price - level) / max(0.1, atr_h1 or 1), 2),
        session_kz=session_kz,
        htf_direction_supportive=(_tf_supportive_up(htf_alignment) if direction == "UP"
                                    else _tf_supportive_down(htf_alignment)),
        confidence=confidence, warnings=warnings,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Convenience: scan the key levels in a snapshot
# ─────────────────────────────────────────────────────────────────────────────

def scan_key_levels(snapshot, htf_alignment=None) -> list[BreakoutAssessment]:
    """
    Automatically evaluate breakouts of PDH/PDL/PWH/PWL/Asian high/low.
    Returns one assessment per level in `direction` implied by its side.
    """
    out: list[BreakoutAssessment] = []
    if snapshot is None or snapshot.levels is None:
        return out
    lb = snapshot.levels
    # UP breakouts: levels above are BUY-side liquidity
    for name, val in (("PDH", lb.pdh), ("PWH", lb.pwh), ("ASIAN_HIGH", lb.asian_high)):
        if val is None: continue
        out.append(classify_breakout(snapshot, val, "UP",
                                       level_name=name, htf_alignment=htf_alignment))
    # DOWN breakouts: levels below are SELL-side liquidity
    for name, val in (("PDL", lb.pdl), ("PWL", lb.pwl), ("ASIAN_LOW", lb.asian_low)):
        if val is None: continue
        out.append(classify_breakout(snapshot, val, "DOWN",
                                       level_name=name, htf_alignment=htf_alignment))
    return out


__all__ = [
    "classify_breakout", "scan_key_levels", "BreakoutAssessment",
    "BK_LIQUIDITY_PROBE", "BK_FAILED", "BK_DEVELOPING", "BK_CONFIRMED",
    "BK_ACCEPTED", "BK_RETEST", "BK_CONTINUATION", "BK_EXHAUSTED",
    "BK_INVALIDATED", "BK_NONE",
]
