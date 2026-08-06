"""
Separated Verdicts — Phase 8
=============================

The brief:
"The dashboard must publish three separate conclusions:
  1. Directional assessment (Strong bullish … Strong bearish)
  2. Opportunity status (Conditions developing … Thesis invalidated)
  3. Entry status (No compliant entry … Entry confirmed)
 Do not collapse these into one WAIT or STAND ASIDE verdict."

This module computes the three fields from Phase 2-7 outputs. It does
NOT run any entry logic (Phase 12 preserves the mandate strategist's
entry rules unchanged) — it INTERPRETS the existing signals into the
three-part vocabulary.

Behind `xauusd_separated_verdicts_enabled`. Exposed via
/api/v1/diagnostics/separated-verdicts.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Optional

log = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Vocabulary — spec strings
# ─────────────────────────────────────────────────────────────────────────────

# Directional assessment
DA_STRONG_BULL      = "Strong bullish"
DA_BULL             = "Bullish"
DA_NEUTRAL_TO_BULL  = "Neutral-to-bullish"
DA_BALANCED         = "Balanced"
DA_NEUTRAL_TO_BEAR  = "Neutral-to-bearish"
DA_BEAR             = "Bearish"
DA_STRONG_BEAR      = "Strong bearish"

# Opportunity status
OS_CONDITIONS_DEVELOPING = "Conditions developing"
OS_DIRECTION_DEVELOPING  = "Direction developing"
OS_DIRECTION_CONFIRMED   = "Direction confirmed"
OS_PULLBACK_PENDING      = "Pullback pending"
OS_ENTRY_ZONE_APPROACHING = "Entry zone approaching"
OS_MOVE_EXTENDED         = "Move extended"
OS_THESIS_INVALIDATED    = "Thesis invalidated"
OS_STAND_ASIDE_EVENT_RISK = "Stand aside due to event risk"
OS_BALANCED_RANGE        = "No opportunity — balanced range"
OS_DATA_INSUFFICIENT     = "Data quality insufficient"

# Entry status
ES_NO_COMPLIANT_ENTRY    = "No compliant entry"
ES_ENTRY_DEVELOPING      = "Entry developing"
ES_ENTRY_CONFIRMED       = "Entry confirmed"
ES_ENTRY_INVALID         = "Entry invalid"
ES_RR_INADEQUATE         = "RR inadequate"
ES_SPREAD_TOO_HIGH       = "Spread too high"
ES_NEWS_BLOCKED          = "News blocked"
ES_DATA_STALE            = "Data stale"


# ─────────────────────────────────────────────────────────────────────────────
# Output
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class SeparatedVerdict:
    directional_assessment: str
    opportunity_status:     str
    entry_status:           str
    directional_reason:     str
    opportunity_reason:     str
    entry_reason:           str
    ready_to_alert:         bool            # True if any of the three is materially new/actionable
    confidence:             int             # 0-100
    warnings:               list[str] = field(default_factory=list)
    generated_at:           datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def to_dict(self) -> dict:
        return {**asdict(self), "generated_at": self.generated_at.isoformat()}


# ─────────────────────────────────────────────────────────────────────────────
# Directional assessment — from HTF alignment + evidence
# ─────────────────────────────────────────────────────────────────────────────

def _directional_assessment(htf_alignment, evidence) -> tuple[str, str]:
    """Return (assessment_label, one-line-reason)."""
    if htf_alignment is None:
        return (DA_BALANCED, "no HTF alignment computed")

    htf_dir = getattr(htf_alignment, "direction", "NEUTRAL")
    htf_str = getattr(htf_alignment, "strength", "NONE")
    htf_score = getattr(htf_alignment, "score", 0)

    ev_dbias = getattr(evidence, "dominant_direction", "NEUTRAL") if evidence else "NEUTRAL"
    bull_ev = getattr(evidence, "bull_evidence_score", 0) if evidence else 0
    bear_ev = getattr(evidence, "bear_evidence_score", 0) if evidence else 0

    # STRONG buckets require BOTH htf strong AND evidence agrees
    if htf_dir == "BULL" and htf_str == "STRONG" and ev_dbias in ("BULL", "NEUTRAL"):
        return (DA_STRONG_BULL,
                f"HTF STRONG bull (score {htf_score:+.0f}), evidence bull={bull_ev}")
    if htf_dir == "BEAR" and htf_str == "STRONG" and ev_dbias in ("BEAR", "NEUTRAL"):
        return (DA_STRONG_BEAR,
                f"HTF STRONG bear (score {htf_score:+.0f}), evidence bear={bear_ev}")

    # MEDIUM buckets
    if htf_dir == "BULL" and htf_str in ("MEDIUM", "STRONG"):
        return (DA_BULL,
                f"HTF {htf_str} bull (score {htf_score:+.0f}), evidence bull={bull_ev}")
    if htf_dir == "BEAR" and htf_str in ("MEDIUM", "STRONG"):
        return (DA_BEAR,
                f"HTF {htf_str} bear (score {htf_score:+.0f}), evidence bear={bear_ev}")

    # WEAK — lean but not committed
    if htf_dir == "BULL" or ev_dbias == "BULL":
        return (DA_NEUTRAL_TO_BULL,
                f"HTF={htf_dir}/{htf_str}, evidence bull={bull_ev} vs bear={bear_ev}")
    if htf_dir == "BEAR" or ev_dbias == "BEAR":
        return (DA_NEUTRAL_TO_BEAR,
                f"HTF={htf_dir}/{htf_str}, evidence bear={bear_ev} vs bull={bull_ev}")

    return (DA_BALANCED,
            f"HTF neutral, evidence tied (bull={bull_ev} bear={bear_ev})")


# ─────────────────────────────────────────────────────────────────────────────
# Opportunity status — from opportunity state machine
# ─────────────────────────────────────────────────────────────────────────────

_STATE_TO_OPPORTUNITY = {
    "BULLISH_OBSERVING":       OS_CONDITIONS_DEVELOPING,
    "BULLISH_EARLY_WARNING":   OS_CONDITIONS_DEVELOPING,
    "BULLISH_TRANSITION":      OS_DIRECTION_DEVELOPING,
    "BULLISH_CONFIRMED":       OS_DIRECTION_CONFIRMED,
    "BULLISH_PULLBACK_PENDING": OS_PULLBACK_PENDING,
    "BULLISH_ENTRY_AVAILABLE": OS_ENTRY_ZONE_APPROACHING,
    "BULLISH_EXTENDED":        OS_MOVE_EXTENDED,
    "BULLISH_INVALIDATED":     OS_THESIS_INVALIDATED,
    "BEARISH_OBSERVING":       OS_CONDITIONS_DEVELOPING,
    "BEARISH_EARLY_WARNING":   OS_CONDITIONS_DEVELOPING,
    "BEARISH_TRANSITION":      OS_DIRECTION_DEVELOPING,
    "BEARISH_CONFIRMED":       OS_DIRECTION_CONFIRMED,
    "BEARISH_PULLBACK_PENDING": OS_PULLBACK_PENDING,
    "BEARISH_ENTRY_AVAILABLE": OS_ENTRY_ZONE_APPROACHING,
    "BEARISH_EXTENDED":        OS_MOVE_EXTENDED,
    "BEARISH_INVALIDATED":     OS_THESIS_INVALIDATED,
    "BALANCED_RANGE":          OS_BALANCED_RANGE,
    "EVENT_RISK":              OS_STAND_ASIDE_EVENT_RISK,
    "INSUFFICIENT_DATA":       OS_DATA_INSUFFICIENT,
}


def _opportunity_status(state_transition) -> tuple[str, str]:
    if state_transition is None:
        return (OS_DATA_INSUFFICIENT, "no state transition available")
    new_state = getattr(state_transition, "new_state", None)
    trigger = getattr(state_transition, "trigger_condition", "")
    label = _STATE_TO_OPPORTUNITY.get(new_state, OS_BALANCED_RANGE)
    return (label, f"state={new_state} ({trigger})")


# ─────────────────────────────────────────────────────────────────────────────
# Entry status — inspects evidence contradictions + data quality + state
# ─────────────────────────────────────────────────────────────────────────────

def _entry_status(evidence, state_transition,
                    *, spread_threshold: float = 5.0,
                    min_data_quality: int = 70) -> tuple[str, str]:
    """
    Preserves Phase 12: entry rules unchanged. This just LABELS whether
    the existing gates would let a signal through.
    """
    if evidence is None:
        return (ES_NO_COMPLIANT_ENTRY, "no evidence assessment")

    dq = getattr(evidence, "data_quality_score", 0) or 0
    if dq < min_data_quality:
        return (ES_DATA_STALE, f"data_quality_score={dq}<{min_data_quality}")

    contra_names = {c.name for c in (getattr(evidence, "contradictions", []) or [])}
    if "NEWS_APPROACHING" in contra_names:
        return (ES_NEWS_BLOCKED, "high-impact event within 30 minutes")
    if "EXCESSIVE_SPREAD" in contra_names:
        return (ES_SPREAD_TOO_HIGH, "spread above configured threshold")

    # Extension-heavy → RR inadequate
    ext = getattr(evidence, "extension_risk_score", 0) or 0
    if ext >= 90:
        return (ES_RR_INADEQUATE, f"extension_risk_score={ext} — likely RR<1.5")

    # Map from state machine
    state_label = getattr(state_transition, "new_state", None) if state_transition else None
    if state_label in ("BULLISH_ENTRY_AVAILABLE", "BEARISH_ENTRY_AVAILABLE"):
        return (ES_ENTRY_CONFIRMED, f"state={state_label}")
    if state_label in ("BULLISH_PULLBACK_PENDING", "BEARISH_PULLBACK_PENDING"):
        return (ES_ENTRY_DEVELOPING, f"state={state_label}")
    if state_label in ("BULLISH_INVALIDATED", "BEARISH_INVALIDATED"):
        return (ES_ENTRY_INVALID, f"state={state_label}")
    if state_label == "BALANCED_RANGE":
        return (ES_NO_COMPLIANT_ENTRY, "balanced range — no directional setup")
    if state_label in ("BULLISH_EXTENDED", "BEARISH_EXTENDED"):
        return (ES_ENTRY_DEVELOPING, "extended — wait for pullback")

    return (ES_NO_COMPLIANT_ENTRY,
            f"state={state_label or 'unknown'} — no entry trigger yet")


# ─────────────────────────────────────────────────────────────────────────────
# Public entry point
# ─────────────────────────────────────────────────────────────────────────────

def compute_separated_verdict(
    *,
    snapshot=None,
    htf_alignment=None,
    regime=None,
    evidence=None,
    breakouts=None,
    state_transition=None,
) -> SeparatedVerdict:
    """
    Given the outputs of Phases 2-7, produce the three-part verdict.
    Fails open — always returns a SeparatedVerdict.
    """
    warnings: list[str] = []

    if snapshot is None:
        warnings.append("snapshot is None")
        return SeparatedVerdict(
            directional_assessment=DA_BALANCED,
            opportunity_status=OS_DATA_INSUFFICIENT,
            entry_status=ES_DATA_STALE,
            directional_reason="no snapshot",
            opportunity_reason="no snapshot",
            entry_reason="no snapshot",
            ready_to_alert=False, confidence=0, warnings=warnings,
        )

    da, da_reason = _directional_assessment(htf_alignment, evidence)
    os_, os_reason = _opportunity_status(state_transition)
    es, es_reason = _entry_status(evidence, state_transition)

    # Ready-to-alert heuristic: something is materially actionable.
    #   directional not balanced OR
    #   opportunity status past "conditions developing" OR
    #   entry status is ENTRY_CONFIRMED or DEVELOPING
    ready = (
        da not in (DA_BALANCED,)
        or os_ not in (OS_CONDITIONS_DEVELOPING, OS_BALANCED_RANGE,
                        OS_DATA_INSUFFICIENT)
        or es in (ES_ENTRY_CONFIRMED, ES_ENTRY_DEVELOPING)
    )

    # Composite confidence: directional_confidence if evidence provided
    conf = int(getattr(evidence, "directional_confidence", 0) or 0)

    return SeparatedVerdict(
        directional_assessment=da,
        opportunity_status=os_,
        entry_status=es,
        directional_reason=da_reason,
        opportunity_reason=os_reason,
        entry_reason=es_reason,
        ready_to_alert=ready, confidence=conf, warnings=warnings,
    )


__all__ = [
    "compute_separated_verdict", "SeparatedVerdict",
    "DA_STRONG_BULL", "DA_BULL", "DA_NEUTRAL_TO_BULL",
    "DA_BALANCED", "DA_NEUTRAL_TO_BEAR", "DA_BEAR", "DA_STRONG_BEAR",
    "OS_CONDITIONS_DEVELOPING", "OS_DIRECTION_DEVELOPING", "OS_DIRECTION_CONFIRMED",
    "OS_PULLBACK_PENDING", "OS_ENTRY_ZONE_APPROACHING", "OS_MOVE_EXTENDED",
    "OS_THESIS_INVALIDATED", "OS_STAND_ASIDE_EVENT_RISK",
    "OS_BALANCED_RANGE", "OS_DATA_INSUFFICIENT",
    "ES_NO_COMPLIANT_ENTRY", "ES_ENTRY_DEVELOPING", "ES_ENTRY_CONFIRMED",
    "ES_ENTRY_INVALID", "ES_RR_INADEQUATE", "ES_SPREAD_TOO_HIGH",
    "ES_NEWS_BLOCKED", "ES_DATA_STALE",
]
