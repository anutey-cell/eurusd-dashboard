"""
Weighted HTF Alignment — Phase 4
=================================

Replaces the current STRONG-only unanimity rule with a weighted per-timeframe
score. The old code required D1 + H4 + H1 to ALL be "Strong bull" (or all
"Strong bear") before C1 would pass. That's why bullish transitions where
D1 was neutral but H1 broke structure never registered — the strategist
suppressed direction for 954 verdicts in a row on Aug 5.

Weights (from brief):
  D1  20  — broader context
  H4  30  — structural bias
  H1  30  — active directional control
  M15 15  — transition, displacement, confirmation
  M5   5  — optional execution refinement

Direction per TF: EMA20 vs EMA50 (or EMA8 vs EMA21 for LTFs), with an
explicit slope check for "strengthening / weakening".

Aggregate score ∈ [-100, +100] with signs mapping to bull/bear:
  score >= +60 → STRONG_BULL
  score >= +30 → MEDIUM_BULL
  score >= +15 → WEAK_BULL
  score in [-15, +15] → NEUTRAL
  ... symmetric for bear ...

Behind `xauusd_weighted_htf_alignment_enabled`. Off by default until
replay proves value. Currently exposed only via
/api/v1/diagnostics/htf-alignment for shadow observation.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Optional

log = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Config: weights + strength thresholds
# ─────────────────────────────────────────────────────────────────────────────

# Weights per TF (must sum to 100)
_WEIGHTS: dict[str, int] = {
    "D1":  20,
    "H4":  30,
    "H1":  30,
    "M15": 15,
    "M5":   5,
}

# EMA pairs used to score each TF's direction
_EMA_PAIRS: dict[str, tuple[int, int]] = {
    "D1":  (20, 50),
    "H4":  (20, 50),
    "H1":  (20, 50),
    "M15": (8, 21),
    "M5":  (8, 21),
}

# Slope lookback (bars). Used to check whether the fast EMA is trending.
_SLOPE_LOOKBACK = 5

# Score bands
_BAND_STRONG = 60
_BAND_MEDIUM = 30
_BAND_WEAK   = 15


# ─────────────────────────────────────────────────────────────────────────────
# Output dataclass
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class TFContribution:
    tf:           str
    direction:    str          # BULL | BEAR | NEUTRAL
    strength:     float        # 0.0 to 1.0
    weight:       int
    contribution: float        # signed: direction_sign * strength * weight
    ema_fast:     Optional[float] = None
    ema_slow:     Optional[float] = None
    slope_up:     bool = False
    slope_down:   bool = False
    evidence:     str = ""

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class HTFAlignment:
    direction:         str            # BULL | BEAR | NEUTRAL
    strength:          str            # STRONG | MEDIUM | WEAK | NONE
    score:             float          # signed aggregate ∈ [-100, +100]
    per_tf:            dict           # tf → TFContribution.to_dict()
    unanimous:         bool           # all evaluated TFs agree
    bull_tfs:          list[str]      # TFs currently bullish
    bear_tfs:          list[str]      # TFs currently bearish
    neutral_tfs:       list[str]      # TFs currently neutral or insufficient
    context_score:     float          # D1 contribution
    structural_score:  float          # H4 contribution
    control_score:     float          # H1 contribution
    intraday_score:    float          # M15 contribution
    execution_score:   float          # M5 contribution
    warnings:          list[str]      = field(default_factory=list)
    generated_at:      datetime       = field(default_factory=lambda: datetime.now(timezone.utc))

    def to_dict(self) -> dict:
        d = asdict(self)
        d["generated_at"] = self.generated_at.isoformat()
        return d


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _ema(vals: list[float], n: int) -> Optional[float]:
    if len(vals) < n or n <= 0:
        return None
    k = 2 / (n + 1)
    ema = sum(vals[:n]) / n
    for v in vals[n:]:
        ema = v * k + ema * (1 - k)
    return ema


def _slope(vals: list[float], n: int, lookback: int) -> tuple[bool, bool]:
    """Returns (slope_up, slope_down) based on EMA(n) now vs `lookback` bars ago."""
    if len(vals) < n + lookback:
        return (False, False)
    now = _ema(vals, n)
    prior = _ema(vals[:-lookback], n)
    if now is None or prior is None:
        return (False, False)
    if now > prior:
        return (True, False)
    if now < prior:
        return (False, True)
    return (False, False)


def _score_timeframe(bars: list, tf: str) -> TFContribution:
    """
    Compute direction + strength for a single TF.

    Direction:
      BULL    if fast EMA > slow EMA
      BEAR    if fast EMA < slow EMA
      NEUTRAL if EMAs unresolved OR insufficient data

    Strength ∈ [0.0, 1.0]:
      Base: 0.5 when EMAs are just crossed
      +0.25 if slope agrees with direction (strengthening)
      +0.25 if separation ≥ 0.5% (wide, confirmed)
      cap at 1.0
    """
    weight = _WEIGHTS.get(tf, 0)
    fast_n, slow_n = _EMA_PAIRS.get(tf, (0, 0))

    if not bars or len(bars) < slow_n + _SLOPE_LOOKBACK:
        return TFContribution(
            tf=tf, direction="NEUTRAL", strength=0.0, weight=weight,
            contribution=0.0, evidence=f"insufficient bars (< {slow_n + _SLOPE_LOOKBACK})",
        )

    closes = [b.close for b in bars]
    fast = _ema(closes, fast_n)
    slow = _ema(closes, slow_n)
    if fast is None or slow is None:
        return TFContribution(
            tf=tf, direction="NEUTRAL", strength=0.0, weight=weight,
            contribution=0.0, ema_fast=fast, ema_slow=slow,
            evidence="EMA computation failed",
        )

    slope_up, slope_down = _slope(closes, fast_n, _SLOPE_LOOKBACK)

    # Separation between EMAs as % of price. Below the DEAD_ZONE we treat
    # them as unresolved (NEUTRAL) even if fast > slow by 0.001% — because
    # in flat markets random noise decides the sign.
    separation_pct = abs(fast - slow) / max(slow, 1e-6)
    DEAD_ZONE = 0.0005            # 0.05% — anything smaller = noise
    NARROW    = 0.0015            # 0.15% — resolved but tight
    WIDE      = 0.005             # 0.5%  — confirmed wide separation

    if separation_pct < DEAD_ZONE:
        direction = "NEUTRAL"
    elif fast > slow:
        direction = "BULL"
    else:
        direction = "BEAR"

    # Strength ∈ [0, 1] scales with separation + slope agreement.
    strength = 0.0
    if direction != "NEUTRAL":
        # Base 0.5 at NARROW, grows linearly toward 1.0 at WIDE
        if separation_pct >= WIDE:
            strength = 1.0
        elif separation_pct >= NARROW:
            strength = 0.5 + 0.5 * (separation_pct - NARROW) / (WIDE - NARROW)
        else:
            # Between DEAD_ZONE and NARROW: 0.0 → 0.5 linearly
            strength = 0.5 * (separation_pct - DEAD_ZONE) / (NARROW - DEAD_ZONE)
        # Slope agreement bonus (small — separation is the primary signal)
        if (direction == "BULL" and slope_up) or (direction == "BEAR" and slope_down):
            strength = min(1.0, strength + 0.15)

    direction_sign = 1.0 if direction == "BULL" else (-1.0 if direction == "BEAR" else 0.0)
    contribution = direction_sign * strength * weight

    ev_parts = []
    ev_parts.append(f"fast_ema={fast:.2f} slow_ema={slow:.2f}")
    ev_parts.append(f"sep_pct={separation_pct*100:.2f}%")
    if slope_up:   ev_parts.append("slope_up")
    if slope_down: ev_parts.append("slope_down")

    return TFContribution(
        tf=tf, direction=direction, strength=round(strength, 2), weight=weight,
        contribution=round(contribution, 2),
        ema_fast=round(fast, 2), ema_slow=round(slow, 2),
        slope_up=slope_up, slope_down=slope_down,
        evidence="; ".join(ev_parts),
    )


def _classify_strength(score: float) -> str:
    m = abs(score)
    if m >= _BAND_STRONG:
        return "STRONG"
    if m >= _BAND_MEDIUM:
        return "MEDIUM"
    if m >= _BAND_WEAK:
        return "WEAK"
    return "NONE"


def _classify_direction(score: float) -> str:
    if score >= _BAND_WEAK:
        return "BULL"
    if score <= -_BAND_WEAK:
        return "BEAR"
    return "NEUTRAL"


# ─────────────────────────────────────────────────────────────────────────────
# Public: compute_htf_alignment(snapshot)
# ─────────────────────────────────────────────────────────────────────────────

def compute_htf_alignment(snapshot) -> HTFAlignment:
    """
    Compute the weighted HTF alignment from a CanonicalSnapshot.

    Fails open — always returns an HTFAlignment, never raises. Missing/short
    timeframes contribute 0 to the score and appear in `neutral_tfs`.
    """
    warnings: list[str] = []
    per_tf: dict[str, TFContribution] = {}
    total_score = 0.0
    total_weight_evaluated = 0

    if snapshot is None:
        warnings.append("snapshot is None")
        return HTFAlignment(
            direction="NEUTRAL", strength="NONE", score=0.0, per_tf={},
            unanimous=False, bull_tfs=[], bear_tfs=[], neutral_tfs=list(_WEIGHTS.keys()),
            context_score=0.0, structural_score=0.0, control_score=0.0,
            intraday_score=0.0, execution_score=0.0, warnings=warnings,
        )

    tfs = snapshot.timeframes or {}
    for tf in _WEIGHTS.keys():
        slice_ = tfs.get(tf)
        bars = slice_.candles if slice_ else []
        contribution = _score_timeframe(bars, tf)
        per_tf[tf] = contribution
        total_score += contribution.contribution
        total_weight_evaluated += _WEIGHTS[tf]

    # Normalise: score is a percentage of the total possible ±100.
    # If we skipped any TF entirely, the max is smaller — but keep the
    # nominal-100 scale so bands are stable across data-quality states.
    bull_tfs = [tf for tf, c in per_tf.items() if c.direction == "BULL"]
    bear_tfs = [tf for tf, c in per_tf.items() if c.direction == "BEAR"]
    neutral_tfs = [tf for tf, c in per_tf.items() if c.direction == "NEUTRAL"]

    unanimous = (
        len(bull_tfs) == len([tf for tf in _WEIGHTS if per_tf[tf].strength > 0])
        and len(bull_tfs) > 0
    ) or (
        len(bear_tfs) == len([tf for tf in _WEIGHTS if per_tf[tf].strength > 0])
        and len(bear_tfs) > 0
    )

    direction = _classify_direction(total_score)
    strength = _classify_strength(total_score)

    return HTFAlignment(
        direction=direction, strength=strength,
        score=round(total_score, 2),
        per_tf={tf: c.to_dict() for tf, c in per_tf.items()},
        unanimous=unanimous,
        bull_tfs=bull_tfs, bear_tfs=bear_tfs, neutral_tfs=neutral_tfs,
        context_score=per_tf["D1"].contribution,
        structural_score=per_tf["H4"].contribution,
        control_score=per_tf["H1"].contribution,
        intraday_score=per_tf["M15"].contribution,
        execution_score=per_tf["M5"].contribution,
        warnings=warnings,
    )


__all__ = [
    "compute_htf_alignment", "HTFAlignment", "TFContribution",
    "_WEIGHTS", "_EMA_PAIRS",
]
