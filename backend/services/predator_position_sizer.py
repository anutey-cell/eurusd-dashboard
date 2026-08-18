"""
Predator DEMO position sizer (spec §7).

Pure functions. No DB, no HTTP, no Telegram. Testable in isolation.

Non-negotiable limits (spec §7):
  - Individual ticket: 0.03 lots (never varies with confidence)
  - STANDARD max:      5 tickets = 0.15 lots
  - EXPANSION max:    10 tickets = 0.30 lots
  - Absolute ceiling:  0.30 lots

VOLUME_EXPANSION_CONFIRMED thresholds — empirically derived from 247 M5-close
FIRE events across ~5.2 months (see scripts/vol_expansion_analysis output).
Per-archetype because the two edges react to different signals:

  ASIAN_BREAKDOWN: vol_pct >= 90 AND disp_atr >= 1.0
    → n=38  expct +14.43pt  lift +6.82  (clears 36-event power threshold)

  PDL_BREAK: vol_pct >= 85 (vol alone; disp_atr filter destroys the sample)
    → n=25  expct +17.60pt  lift +8.01  (below power threshold — noted)

  VOL_CONTINUATION: no historical bucket — EXPANSION disabled for now.

TP distribution across the 5 STANDARD tickets:
  seq 1-2 → TP1  (front-load probability capture)
  seq 3-4 → TP2  (structural target)
  seq 5   → TP2  (final structural)
EXPANSION adds 5 more tickets:
  seq 6-8 → TP2  (extend the structural bet)
  seq 9-10 → RUNNER  (only if historical MFE supports; SL to breakeven at TP1)
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

# ── Non-negotiable constants (spec §7 — do not parameterize away) ────────────

PREDATOR_LOT_SIZE: float                = 0.03
PREDATOR_STANDARD_MAX_POSITIONS: int    = 5
PREDATOR_EXPANSION_MAX_POSITIONS: int   = 10
PREDATOR_STANDARD_MAX_EXPOSURE: float   = 0.15   # 5 × 0.03
PREDATOR_EXPANSION_MAX_EXPOSURE: float  = 0.30   # 10 × 0.03
PREDATOR_ABSOLUTE_EXPOSURE_CEILING: float = 0.30


# ── Empirical VOLUME_EXPANSION thresholds (per-archetype, from historical data)
# Sourced from scratchpad/vol_expansion_v2.py output on 5.2-month sample.
# DO NOT tune arbitrarily — re-run the analysis and revise.

_EXPANSION_THRESHOLDS: dict[str, dict] = {
    "ASIAN_BREAKDOWN": {
        "min_vol_pct":     90.0,
        "min_disp_atr":    1.0,
        "sample_n":        38,
        "expct_uplift":    6.82,
        "power_note":      "clears 36-event power threshold",
    },
    "PDL_BREAK": {
        "min_vol_pct":     85.0,
        "min_disp_atr":    None,   # vol alone; disp filter drops sample to n=8
        "sample_n":        25,
        "expct_uplift":    8.01,
        "power_note":      "below 36-event power threshold — monitor live",
    },
    "VOL_CONTINUATION": None,      # no dedicated historical bucket — no EXPANSION
}


@dataclass
class VolumeExpansionResult:
    confirmed: bool
    vol_pct: Optional[float]      # 0-100
    disp_atr: Optional[float]
    atr20: Optional[float]
    reason: str                   # human-readable explanation for logs/Telegram


@dataclass
class PositionPlan:
    """One planned ticket within a batch."""
    seq_no: int                   # 1-based within batch
    lot_size: float               # always PREDATOR_LOT_SIZE
    tp_target: str                # "TP1" | "TP2" | "RUNNER"
    take_profit: float            # concrete price
    entry_price: float            # from signal
    stop_loss: float              # from signal


@dataclass
class DeploymentPlan:
    """Full batch plan produced by plan_deployment()."""
    exposure_mode: str            # "STANDARD" | "EXPANSION"
    positions: list[PositionPlan] = field(default_factory=list)
    max_exposure_lots: float = 0.0
    expansion_evidence: Optional[VolumeExpansionResult] = None


# ─────────────────────────────────────────────────────────────────────────────
# Volume expansion evaluation
# ─────────────────────────────────────────────────────────────────────────────

def evaluate_volume_expansion(
    m5_bars: list[tuple],
    archetype: str,
    *,
    vol_window: int = 100,
    atr_window: int = 20,
) -> VolumeExpansionResult:
    """
    Assess whether the most-recent M5 bar meets VOLUME_EXPANSION_CONFIRMED for
    the given archetype. `m5_bars` must be ordered ASCENDING by time, tuples of
    (time, open, high, low, close, volume). The LAST element is the fire bar.
    """
    thresholds = _EXPANSION_THRESHOLDS.get(archetype)
    if thresholds is None:
        return VolumeExpansionResult(
            confirmed=False, vol_pct=None, disp_atr=None, atr20=None,
            reason=f"{archetype} has no EXPANSION threshold defined",
        )

    if len(m5_bars) < max(vol_window, atr_window) + 1:
        return VolumeExpansionResult(
            confirmed=False, vol_pct=None, disp_atr=None, atr20=None,
            reason=f"insufficient bars ({len(m5_bars)}) for vol/ATR windows",
        )

    fire = m5_bars[-1]
    fire_open, fire_high, fire_low, fire_close, fire_vol = (
        fire[1], fire[2], fire[3], fire[4], fire[5]
    )
    if fire_vol is None or fire_vol <= 0:
        return VolumeExpansionResult(
            confirmed=False, vol_pct=None, disp_atr=None, atr20=None,
            reason="fire bar has no volume",
        )

    # Rolling vol percentile
    window = m5_bars[-(vol_window + 1):-1]
    prior_vols = [b[5] for b in window if b[5] is not None and b[5] > 0]
    if not prior_vols:
        return VolumeExpansionResult(
            confirmed=False, vol_pct=None, disp_atr=None, atr20=None,
            reason="no prior volume in window",
        )
    rank = sum(1 for v in prior_vols if v <= fire_vol)
    vol_pct = 100.0 * rank / len(prior_vols)

    # ATR20
    atr_bars = m5_bars[-(atr_window + 1):-1]
    trs = []
    prev_c = None
    for b in atr_bars:
        h, l, c = b[2], b[3], b[4]
        tr = h - l
        if prev_c is not None:
            tr = max(tr, abs(h - prev_c), abs(l - prev_c))
        trs.append(tr)
        prev_c = c
    atr20 = sum(trs) / len(trs) if trs else None

    disp_pts = abs(fire_close - fire_open)
    disp_atr = (disp_pts / atr20) if (atr20 and atr20 > 0) else None

    # Threshold check
    min_vol = thresholds["min_vol_pct"]
    min_disp = thresholds["min_disp_atr"]
    passes_vol  = vol_pct is not None and vol_pct >= min_vol
    passes_disp = (min_disp is None) or (disp_atr is not None and disp_atr >= min_disp)
    confirmed = passes_vol and passes_disp

    if confirmed:
        parts = [f"vol_pct={vol_pct:.0f} >= {min_vol:.0f}"]
        if min_disp is not None:
            parts.append(f"disp/atr={disp_atr:.2f} >= {min_disp:.1f}")
        reason = " AND ".join(parts) + f"  (n={thresholds['sample_n']}, +{thresholds['expct_uplift']:.1f}pt lift)"
    else:
        misses = []
        if not passes_vol:
            misses.append(f"vol_pct={vol_pct:.0f} < {min_vol:.0f}")
        if not passes_disp and min_disp is not None:
            misses.append(f"disp/atr={(disp_atr or 0):.2f} < {min_disp:.1f}")
        reason = "; ".join(misses)

    return VolumeExpansionResult(
        confirmed=confirmed,
        vol_pct=vol_pct,
        disp_atr=disp_atr,
        atr20=atr20,
        reason=reason,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Deployment planner
# ─────────────────────────────────────────────────────────────────────────────

def plan_deployment(
    *,
    archetype: str,
    direction: str,
    entry: float,
    stop_loss: float,
    tp1: float,
    tp2: float,
    expansion: VolumeExpansionResult,
    expansion_mode_allowed: bool = False,
) -> DeploymentPlan:
    """
    Produce the ticket-level plan for a single Predator FIRE.

    Args:
      expansion_mode_allowed — global master flag. Even if VOLUME_EXPANSION_CONFIRMED
        is true, EXPANSION mode only activates when this is also true.
    """
    use_expansion = bool(expansion.confirmed and expansion_mode_allowed)
    mode = "EXPANSION" if use_expansion else "STANDARD"
    max_positions = (
        PREDATOR_EXPANSION_MAX_POSITIONS if use_expansion
        else PREDATOR_STANDARD_MAX_POSITIONS
    )
    max_exposure = (
        PREDATOR_EXPANSION_MAX_EXPOSURE if use_expansion
        else PREDATOR_STANDARD_MAX_EXPOSURE
    )

    # TP distribution (5 STANDARD + optional 5 EXPANSION extensions).
    # Runner target = 1.5× the TP1→TP2 distance beyond TP2, same sign.
    tp2_extension = tp2 + (0.5 * (tp2 - tp1))   # for SELL: tp2 already < tp1, so this pushes further

    tp_map = {
        1: ("TP1", tp1),
        2: ("TP1", tp1),
        3: ("TP2", tp2),
        4: ("TP2", tp2),
        5: ("TP2", tp2),
        6: ("TP2", tp2),
        7: ("TP2", tp2),
        8: ("TP2", tp2),
        9: ("RUNNER", tp2_extension),
        10: ("RUNNER", tp2_extension),
    }

    positions = []
    for seq in range(1, max_positions + 1):
        tp_label, tp_price = tp_map[seq]
        positions.append(PositionPlan(
            seq_no=seq,
            lot_size=PREDATOR_LOT_SIZE,
            tp_target=tp_label,
            take_profit=round(tp_price, 2),
            entry_price=entry,
            stop_loss=stop_loss,
        ))

    return DeploymentPlan(
        exposure_mode=mode,
        positions=positions,
        max_exposure_lots=max_exposure,
        expansion_evidence=expansion,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Hard exposure guard — must be checked before EVERY enqueue
# ─────────────────────────────────────────────────────────────────────────────

def validate_exposure_within_ceiling(
    *,
    current_exposure_lots: float,
    proposed_lot: float,
    exposure_mode: str,
) -> tuple[bool, str]:
    """
    Return (allowed, reason). Enforce mode-specific ceiling AND absolute ceiling.
    """
    if abs(proposed_lot - PREDATOR_LOT_SIZE) > 1e-6:
        return False, f"proposed_lot {proposed_lot} != required {PREDATOR_LOT_SIZE}"

    projected = current_exposure_lots + proposed_lot

    if projected > PREDATOR_ABSOLUTE_EXPOSURE_CEILING + 1e-6:
        return False, (f"projected {projected:.2f} exceeds absolute ceiling "
                       f"{PREDATOR_ABSOLUTE_EXPOSURE_CEILING:.2f}")

    mode_cap = (PREDATOR_EXPANSION_MAX_EXPOSURE if exposure_mode == "EXPANSION"
                else PREDATOR_STANDARD_MAX_EXPOSURE)
    if projected > mode_cap + 1e-6:
        return False, (f"projected {projected:.2f} exceeds {exposure_mode} cap "
                       f"{mode_cap:.2f}")

    return True, "ok"


__all__ = [
    "PREDATOR_LOT_SIZE",
    "PREDATOR_STANDARD_MAX_POSITIONS", "PREDATOR_EXPANSION_MAX_POSITIONS",
    "PREDATOR_STANDARD_MAX_EXPOSURE", "PREDATOR_EXPANSION_MAX_EXPOSURE",
    "PREDATOR_ABSOLUTE_EXPOSURE_CEILING",
    "VolumeExpansionResult", "PositionPlan", "DeploymentPlan",
    "evaluate_volume_expansion", "plan_deployment",
    "validate_exposure_within_ceiling",
    "_EXPANSION_THRESHOLDS",
]
