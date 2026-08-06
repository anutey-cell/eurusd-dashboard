"""
Unit tests for Opportunity State Machine (Phase 7).

Focuses on the PURE decide_next_state() function — no DB required.
"""
import sys, os
from types import SimpleNamespace
from datetime import datetime, timezone, timedelta

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from services.opportunity_state import (
    decide_next_state,
    S_BULL_OBSERVING, S_BULL_EARLY_WARNING, S_BULL_TRANSITION,
    S_BULL_CONFIRMED, S_BULL_PULLBACK_PENDING, S_BULL_ENTRY_AVAILABLE,
    S_BULL_EXTENDED, S_BULL_INVALIDATED,
    S_BEAR_OBSERVING, S_BEAR_EARLY_WARNING, S_BEAR_TRANSITION,
    S_BEAR_CONFIRMED, S_BEAR_EXTENDED, S_BEAR_INVALIDATED,
    S_BALANCED_RANGE, S_EVENT_RISK, S_INSUFFICIENT_DATA,
)


def _snap_dummy(price=4100.0):
    """Minimal snapshot-like object satisfying the state machine's reads."""
    from services.canonical_market_data import (
        Bar, TimeframeSlice, LevelBundle, CanonicalSnapshot, SessionInfo,
    )
    base = datetime(2026, 8, 1, 0, 0, tzinfo=timezone.utc)
    bars = [Bar(time=base + timedelta(minutes=15 * i),
                 open=price, high=price+1, low=price-1, close=price, volume=1)
             for i in range(30)]
    return CanonicalSnapshot(
        ts=datetime.now(timezone.utc), instrument="XAU/USD",
        bid=price - 0.1, ask=price + 0.1, spread=0.2,
        timeframes={"M15": TimeframeSlice("M15", bars)},
        levels=LevelBundle(pdh=price+10, pdl=price-10),
        session=SessionInfo("NY_OPEN", "NY open"), data_quality_score=100,
    )


def _ev(*, dbias="NEUTRAL", bull=0, bear=0, contra=0, dq=100,
         event=0, ext=0, dc=0, bull_items=None, bear_items=None, contra_items=None):
    return SimpleNamespace(
        dominant_direction=dbias,
        bull_evidence_score=bull, bear_evidence_score=bear,
        contradiction_score=contra, data_quality_score=dq,
        event_risk_score=event, extension_risk_score=ext,
        directional_confidence=dc,
        bull_items=bull_items or [], bear_items=bear_items or [],
        contradictions=contra_items or [],
    )


def _regime(label=None, bias="NEUTRAL", invalidation=None):
    return SimpleNamespace(
        regime=label, directional_bias=bias,
        invalidation_price=invalidation,
    )


def _htf(direction="NEUTRAL", strength="NONE"):
    return SimpleNamespace(direction=direction, strength=strength)


def _bo(direction, classification):
    return SimpleNamespace(direction=direction, classification=classification)


# ─────────────────────────────────────────────────────────────────────────────
# Guard states
# ─────────────────────────────────────────────────────────────────────────────

def test_insufficient_data_when_snapshot_none():
    s, trig, conf, inv = decide_next_state(
        None, snapshot=None, regime=None,
        htf_alignment=None, evidence=_ev(dq=0),
    )
    assert s == S_INSUFFICIENT_DATA


def test_insufficient_data_when_dq_low():
    s, trig, conf, inv = decide_next_state(
        None, snapshot=_snap_dummy(), regime=_regime(),
        htf_alignment=_htf(), evidence=_ev(dq=40),
    )
    assert s == S_INSUFFICIENT_DATA


def test_event_risk_when_event_score_high():
    s, trig, conf, inv = decide_next_state(
        None, snapshot=_snap_dummy(), regime=_regime(),
        htf_alignment=_htf(), evidence=_ev(dq=100, event=90),
    )
    assert s == S_EVENT_RISK


# ─────────────────────────────────────────────────────────────────────────────
# Bull progression
# ─────────────────────────────────────────────────────────────────────────────

def test_bull_observing_when_only_bias_set():
    s, *_ = decide_next_state(
        None, snapshot=_snap_dummy(), regime=_regime(bias="BULL"),
        htf_alignment=_htf("BULL", "WEAK"), evidence=_ev(dbias="BULL", bull=10, dc=5),
    )
    assert s == S_BULL_OBSERVING


def test_bull_early_warning_when_bull_ev_25():
    s, *_ = decide_next_state(
        None, snapshot=_snap_dummy(), regime=_regime(bias="BULL"),
        htf_alignment=_htf("BULL", "WEAK"), evidence=_ev(dbias="BULL", bull=30, dc=20),
    )
    assert s == S_BULL_EARLY_WARNING


def test_bull_early_warning_when_regime_accumulation():
    s, *_ = decide_next_state(
        None, snapshot=_snap_dummy(),
        regime=_regime(label="BULLISH_ACCUMULATION", bias="BULL"),
        htf_alignment=_htf("BULL", "WEAK"), evidence=_ev(dbias="BULL", bull=10, dc=15),
    )
    assert s == S_BULL_EARLY_WARNING


def test_bull_transition_from_regime():
    s, *_ = decide_next_state(
        None, snapshot=_snap_dummy(),
        regime=_regime(label="BULLISH_TRANSITION", bias="BULL"),
        htf_alignment=_htf("NEUTRAL", "NONE"),
        evidence=_ev(dbias="BULL", bull=30, dc=45),
    )
    assert s == S_BULL_TRANSITION


def test_bull_transition_from_htf_medium_plus_evidence():
    s, *_ = decide_next_state(
        None, snapshot=_snap_dummy(),
        regime=_regime(label="BULLISH_ACCUMULATION", bias="BULL"),
        htf_alignment=_htf("BULL", "MEDIUM"),
        evidence=_ev(dbias="BULL", bull=50, dc=55),
    )
    assert s == S_BULL_TRANSITION


def test_bull_confirmed_when_regime_strong():
    s, *_ = decide_next_state(
        None, snapshot=_snap_dummy(),
        regime=_regime(label="STRONG_BULLISH_EXPANSION", bias="BULL"),
        htf_alignment=_htf("BULL", "STRONG"),
        evidence=_ev(dbias="BULL", bull=80, dc=75),
    )
    assert s == S_BULL_CONFIRMED


def test_bull_confirmed_when_evidence_plus_bo_accepted():
    s, *_ = decide_next_state(
        None, snapshot=_snap_dummy(),
        regime=_regime(label="BULLISH_ACCUMULATION", bias="BULL"),
        htf_alignment=_htf("BULL", "MEDIUM"),
        evidence=_ev(dbias="BULL", bull=60, dc=65),
        breakouts=[_bo("UP", "BREAKOUT_ACCEPTANCE")],
    )
    assert s == S_BULL_CONFIRMED


def test_bull_pullback_pending_when_regime_pullback():
    s, *_ = decide_next_state(
        S_BULL_CONFIRMED, snapshot=_snap_dummy(),
        regime=_regime(label="BULLISH_PULLBACK", bias="BULL"),
        htf_alignment=_htf("BULL", "STRONG"),
        evidence=_ev(dbias="BULL", bull=45, dc=55),
    )
    assert s == S_BULL_PULLBACK_PENDING


def test_bull_entry_available_when_pullback_plus_retest():
    s, *_ = decide_next_state(
        S_BULL_PULLBACK_PENDING, snapshot=_snap_dummy(),
        regime=_regime(label="BULLISH_PULLBACK", bias="BULL"),
        htf_alignment=_htf("BULL", "STRONG"),
        evidence=_ev(dbias="BULL", bull=45, dc=55),
        breakouts=[_bo("UP", "BREAKOUT_RETEST")],
    )
    assert s == S_BULL_ENTRY_AVAILABLE


def test_bull_extended_when_regime_exhaustion():
    s, *_ = decide_next_state(
        S_BULL_CONFIRMED, snapshot=_snap_dummy(),
        regime=_regime(label="EXHAUSTION_OVEREXTENSION", bias="BULL"),
        htf_alignment=_htf("BULL", "STRONG"),
        evidence=_ev(dbias="BULL", bull=70, ext=90, dc=55),
    )
    assert s == S_BULL_EXTENDED


def test_bull_extended_when_extension_risk_high():
    s, *_ = decide_next_state(
        S_BULL_CONFIRMED, snapshot=_snap_dummy(),
        regime=_regime(label="BULLISH_CONTINUATION", bias="BULL"),
        htf_alignment=_htf("BULL", "STRONG"),
        evidence=_ev(dbias="BULL", bull=70, ext=85, dc=55),
    )
    assert s == S_BULL_EXTENDED


def test_bull_invalidated_when_regime_flips_bear():
    s, *_ = decide_next_state(
        S_BULL_CONFIRMED, snapshot=_snap_dummy(),
        regime=_regime(label="BEARISH_CONFIRMED", bias="BEAR"),
        htf_alignment=_htf("BEAR", "MEDIUM"),
        evidence=_ev(dbias="BEAR", bear=60, dc=50),
    )
    assert s == S_BULL_INVALIDATED


# ─────────────────────────────────────────────────────────────────────────────
# Bear symmetry
# ─────────────────────────────────────────────────────────────────────────────

def test_bear_early_warning_when_bear_ev_25():
    s, *_ = decide_next_state(
        None, snapshot=_snap_dummy(), regime=_regime(bias="BEAR"),
        htf_alignment=_htf("BEAR", "WEAK"),
        evidence=_ev(dbias="BEAR", bear=30, dc=20),
    )
    assert s == S_BEAR_EARLY_WARNING


def test_bear_confirmed_when_regime_strong_bear():
    s, *_ = decide_next_state(
        None, snapshot=_snap_dummy(),
        regime=_regime(label="STRONG_BEARISH_EXPANSION", bias="BEAR"),
        htf_alignment=_htf("BEAR", "STRONG"),
        evidence=_ev(dbias="BEAR", bear=80, dc=75),
    )
    assert s == S_BEAR_CONFIRMED


def test_bear_invalidated_when_regime_flips_bull():
    s, *_ = decide_next_state(
        S_BEAR_CONFIRMED, snapshot=_snap_dummy(),
        regime=_regime(label="BULLISH_CONFIRMED", bias="BULL"),
        htf_alignment=_htf("BULL", "MEDIUM"),
        evidence=_ev(dbias="BULL", bull=60, dc=50),
    )
    assert s == S_BEAR_INVALIDATED


# ─────────────────────────────────────────────────────────────────────────────
# Balanced
# ─────────────────────────────────────────────────────────────────────────────

def test_balanced_range_when_no_directional_edge():
    s, *_ = decide_next_state(
        None, snapshot=_snap_dummy(), regime=_regime(bias="NEUTRAL"),
        htf_alignment=_htf("NEUTRAL", "NONE"),
        evidence=_ev(dbias="NEUTRAL", bull=15, bear=14, dc=10),
    )
    assert s == S_BALANCED_RANGE


# ─────────────────────────────────────────────────────────────────────────────
# Confidence + trigger propagation
# ─────────────────────────────────────────────────────────────────────────────

def test_returns_confidence_and_trigger_string():
    s, trig, conf, inv = decide_next_state(
        None, snapshot=_snap_dummy(),
        regime=_regime(label="STRONG_BULLISH_EXPANSION", bias="BULL"),
        htf_alignment=_htf("BULL", "STRONG"),
        evidence=_ev(dbias="BULL", bull=80, dc=75),
    )
    assert s == S_BULL_CONFIRMED
    assert isinstance(trig, str) and len(trig) > 0
    assert 0 <= conf <= 100


if __name__ == "__main__":
    import subprocess
    subprocess.run(["python", "-m", "pytest", __file__, "-v"], check=False)
