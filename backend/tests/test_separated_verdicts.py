"""
Unit tests for Separated Verdicts (Phase 8).

Focus: this is the CORE fix for "STAND ASIDE hides direction". Verify that
direction, opportunity, and entry are separately reported even when entry
isn't compliant.
"""
import sys, os
from types import SimpleNamespace
from datetime import datetime, timezone

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from services.separated_verdicts import (
    compute_separated_verdict,
    DA_STRONG_BULL, DA_BULL, DA_NEUTRAL_TO_BULL,
    DA_BALANCED, DA_NEUTRAL_TO_BEAR, DA_BEAR, DA_STRONG_BEAR,
    OS_CONDITIONS_DEVELOPING, OS_DIRECTION_DEVELOPING, OS_DIRECTION_CONFIRMED,
    OS_PULLBACK_PENDING, OS_ENTRY_ZONE_APPROACHING, OS_MOVE_EXTENDED,
    OS_THESIS_INVALIDATED, OS_STAND_ASIDE_EVENT_RISK,
    OS_BALANCED_RANGE, OS_DATA_INSUFFICIENT,
    ES_NO_COMPLIANT_ENTRY, ES_ENTRY_DEVELOPING, ES_ENTRY_CONFIRMED,
    ES_ENTRY_INVALID, ES_RR_INADEQUATE, ES_SPREAD_TOO_HIGH,
    ES_NEWS_BLOCKED, ES_DATA_STALE,
)


def _snap(): return SimpleNamespace(data_quality_score=100)
def _htf(direction="NEUTRAL", strength="NONE", score=0):
    return SimpleNamespace(direction=direction, strength=strength, score=score)
def _ev(dbias="NEUTRAL", bull=0, bear=0, contra_names=None,
         dq=100, event=0, ext=0, dc=0):
    contradictions = [SimpleNamespace(name=n) for n in (contra_names or [])]
    return SimpleNamespace(
        dominant_direction=dbias, bull_evidence_score=bull,
        bear_evidence_score=bear, contradictions=contradictions,
        data_quality_score=dq, event_risk_score=event,
        extension_risk_score=ext, directional_confidence=dc,
    )
def _regime(label=None, bias="NEUTRAL"):
    return SimpleNamespace(regime=label, directional_bias=bias)
def _state(new): return SimpleNamespace(new_state=new, trigger_condition="test")


# ─────────────────────────────────────────────────────────────────────────────
# Directional assessment mapping
# ─────────────────────────────────────────────────────────────────────────────

def test_da_strong_bull():
    v = compute_separated_verdict(
        snapshot=_snap(),
        htf_alignment=_htf("BULL", "STRONG", 75),
        evidence=_ev(dbias="BULL", bull=80),
        state_transition=_state("BULLISH_CONFIRMED"),
    )
    assert v.directional_assessment == DA_STRONG_BULL

def test_da_bull_medium():
    v = compute_separated_verdict(
        snapshot=_snap(),
        htf_alignment=_htf("BULL", "MEDIUM", 40),
        evidence=_ev(dbias="BULL", bull=50),
        state_transition=_state("BULLISH_CONFIRMED"),
    )
    assert v.directional_assessment == DA_BULL

def test_da_neutral_to_bull_when_weak():
    v = compute_separated_verdict(
        snapshot=_snap(),
        htf_alignment=_htf("BULL", "WEAK", 18),
        evidence=_ev(dbias="BULL", bull=25),
        state_transition=_state("BULLISH_EARLY_WARNING"),
    )
    assert v.directional_assessment == DA_NEUTRAL_TO_BULL

def test_da_balanced_when_no_direction():
    v = compute_separated_verdict(
        snapshot=_snap(),
        htf_alignment=_htf("NEUTRAL", "NONE", 5),
        evidence=_ev(dbias="NEUTRAL", bull=10, bear=10),
        state_transition=_state("BALANCED_RANGE"),
    )
    assert v.directional_assessment == DA_BALANCED

def test_da_strong_bear():
    v = compute_separated_verdict(
        snapshot=_snap(),
        htf_alignment=_htf("BEAR", "STRONG", -75),
        evidence=_ev(dbias="BEAR", bear=80),
        state_transition=_state("BEARISH_CONFIRMED"),
    )
    assert v.directional_assessment == DA_STRONG_BEAR


# ─────────────────────────────────────────────────────────────────────────────
# Opportunity status mapping
# ─────────────────────────────────────────────────────────────────────────────

def test_os_conditions_developing_from_observing():
    v = compute_separated_verdict(
        snapshot=_snap(), htf_alignment=_htf("BULL", "WEAK", 18),
        evidence=_ev(bull=10, dc=15),
        state_transition=_state("BULLISH_OBSERVING"),
    )
    assert v.opportunity_status == OS_CONDITIONS_DEVELOPING

def test_os_direction_confirmed():
    v = compute_separated_verdict(
        snapshot=_snap(), htf_alignment=_htf("BULL", "STRONG", 70),
        evidence=_ev(bull=70, dc=65),
        state_transition=_state("BULLISH_CONFIRMED"),
    )
    assert v.opportunity_status == OS_DIRECTION_CONFIRMED

def test_os_pullback_pending():
    v = compute_separated_verdict(
        snapshot=_snap(), htf_alignment=_htf("BULL", "STRONG", 70),
        evidence=_ev(bull=45),
        state_transition=_state("BULLISH_PULLBACK_PENDING"),
    )
    assert v.opportunity_status == OS_PULLBACK_PENDING

def test_os_entry_zone_approaching():
    v = compute_separated_verdict(
        snapshot=_snap(), htf_alignment=_htf("BULL", "STRONG", 70),
        evidence=_ev(bull=50, dc=60),
        state_transition=_state("BULLISH_ENTRY_AVAILABLE"),
    )
    assert v.opportunity_status == OS_ENTRY_ZONE_APPROACHING

def test_os_move_extended():
    v = compute_separated_verdict(
        snapshot=_snap(), htf_alignment=_htf("BULL", "STRONG", 70),
        evidence=_ev(bull=70, ext=85),
        state_transition=_state("BULLISH_EXTENDED"),
    )
    assert v.opportunity_status == OS_MOVE_EXTENDED

def test_os_thesis_invalidated():
    v = compute_separated_verdict(
        snapshot=_snap(), htf_alignment=_htf("BEAR", "MEDIUM", -40),
        evidence=_ev(dbias="BEAR", bear=50),
        state_transition=_state("BULLISH_INVALIDATED"),
    )
    assert v.opportunity_status == OS_THESIS_INVALIDATED

def test_os_stand_aside_event_risk():
    v = compute_separated_verdict(
        snapshot=_snap(), htf_alignment=_htf("BULL", "STRONG", 70),
        evidence=_ev(event=90),
        state_transition=_state("EVENT_RISK"),
    )
    assert v.opportunity_status == OS_STAND_ASIDE_EVENT_RISK


# ─────────────────────────────────────────────────────────────────────────────
# Entry status mapping
# ─────────────────────────────────────────────────────────────────────────────

def test_es_data_stale_when_dq_low():
    v = compute_separated_verdict(
        snapshot=_snap(), htf_alignment=_htf("BULL", "STRONG", 70),
        evidence=_ev(bull=70, dq=40, dc=30),
        state_transition=_state("BULLISH_CONFIRMED"),
    )
    assert v.entry_status == ES_DATA_STALE

def test_es_news_blocked_when_news_contradiction():
    v = compute_separated_verdict(
        snapshot=_snap(), htf_alignment=_htf("BULL", "STRONG", 70),
        evidence=_ev(bull=70, contra_names=["NEWS_APPROACHING"]),
        state_transition=_state("BULLISH_CONFIRMED"),
    )
    assert v.entry_status == ES_NEWS_BLOCKED

def test_es_spread_too_high_when_spread_contradiction():
    v = compute_separated_verdict(
        snapshot=_snap(), htf_alignment=_htf("BULL", "STRONG", 70),
        evidence=_ev(bull=70, contra_names=["EXCESSIVE_SPREAD"]),
        state_transition=_state("BULLISH_CONFIRMED"),
    )
    assert v.entry_status == ES_SPREAD_TOO_HIGH

def test_es_entry_confirmed_when_state_entry_available():
    v = compute_separated_verdict(
        snapshot=_snap(), htf_alignment=_htf("BULL", "STRONG", 70),
        evidence=_ev(bull=60, dc=65),
        state_transition=_state("BULLISH_ENTRY_AVAILABLE"),
    )
    assert v.entry_status == ES_ENTRY_CONFIRMED

def test_es_entry_developing_when_pullback_pending():
    v = compute_separated_verdict(
        snapshot=_snap(), htf_alignment=_htf("BULL", "STRONG", 70),
        evidence=_ev(bull=45),
        state_transition=_state("BULLISH_PULLBACK_PENDING"),
    )
    assert v.entry_status == ES_ENTRY_DEVELOPING

def test_es_entry_invalid_when_state_invalidated():
    v = compute_separated_verdict(
        snapshot=_snap(), htf_alignment=_htf("BEAR", "MEDIUM", -40),
        evidence=_ev(dbias="BEAR", bear=50),
        state_transition=_state("BULLISH_INVALIDATED"),
    )
    assert v.entry_status == ES_ENTRY_INVALID

def test_es_no_compliant_entry_when_balanced():
    v = compute_separated_verdict(
        snapshot=_snap(), htf_alignment=_htf("NEUTRAL", "NONE", 5),
        evidence=_ev(bull=10, bear=10),
        state_transition=_state("BALANCED_RANGE"),
    )
    assert v.entry_status == ES_NO_COMPLIANT_ENTRY

def test_es_rr_inadequate_when_extended_very_high():
    v = compute_separated_verdict(
        snapshot=_snap(), htf_alignment=_htf("BULL", "STRONG", 70),
        evidence=_ev(bull=70, ext=95),
        state_transition=_state("BULLISH_EXTENDED"),
    )
    assert v.entry_status == ES_RR_INADEQUATE


# ─────────────────────────────────────────────────────────────────────────────
# THE CORE FIX — direction reported even when no entry
# ─────────────────────────────────────────────────────────────────────────────

def test_direction_visible_when_no_entry_available():
    """The bug: old strategist collapsed BULL+no-entry into STAND ASIDE.
    New verdict: direction=BULLISH, opportunity=CONFIRMED, entry=NO_COMPLIANT_ENTRY."""
    v = compute_separated_verdict(
        snapshot=_snap(),
        htf_alignment=_htf("BULL", "STRONG", 70),
        evidence=_ev(dbias="BULL", bull=70, bear=10, dc=55),
        # State machine says CONFIRMED but not yet ENTRY_AVAILABLE
        state_transition=_state("BULLISH_CONFIRMED"),
    )
    # Direction is loud
    assert v.directional_assessment == DA_STRONG_BULL
    # Opportunity says direction confirmed
    assert v.opportunity_status == OS_DIRECTION_CONFIRMED
    # Entry says "developing" — but direction is not hidden
    assert v.entry_status in (ES_NO_COMPLIANT_ENTRY, ES_ENTRY_DEVELOPING)
    assert v.ready_to_alert is True


# ─────────────────────────────────────────────────────────────────────────────
# Failing-open
# ─────────────────────────────────────────────────────────────────────────────

def test_no_snapshot_returns_balanced_data_stale():
    v = compute_separated_verdict()
    assert v.directional_assessment == DA_BALANCED
    assert v.opportunity_status == OS_DATA_INSUFFICIENT
    assert v.entry_status == ES_DATA_STALE
    assert v.ready_to_alert is False


if __name__ == "__main__":
    import subprocess
    subprocess.run(["python", "-m", "pytest", __file__, "-v"], check=False)
