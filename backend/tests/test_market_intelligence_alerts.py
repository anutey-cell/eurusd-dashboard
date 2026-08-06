"""Unit tests for Market Intelligence Alerts (Phase 11)."""
import sys, os
from types import SimpleNamespace
from datetime import datetime, timezone, timedelta

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from services.market_intelligence_alerts import (
    detect_alert_candidates, build_intel_body, _fingerprint,
    ALERT_BULLISH_CONDITIONS_BUILDING, ALERT_BEARISH_CONDITIONS_BUILDING,
    ALERT_BULLISH_TRANSITION_DETECTED, ALERT_BEARISH_TRANSITION_DETECTED,
    ALERT_PDH_BROKEN, ALERT_PDL_BROKEN,
    ALERT_BULLISH_BREAKOUT_ACCEPTANCE, ALERT_BEARISH_BREAKDOWN_ACCEPTANCE,
    ALERT_BULLISH_PULLBACK_ZONE, ALERT_BEARISH_PULLBACK_ZONE,
    ALERT_BULLISH_MOVE_EXTENDED, ALERT_BEARISH_MOVE_EXTENDED,
    ALERT_BULLISH_THESIS_INVALIDATED, ALERT_BEARISH_THESIS_INVALIDATED,
    ALERT_MARKET_RETURNED_TO_BALANCE, ALERT_HIGH_IMPACT_EVENT_RISK,
    ALERT_ASIAN_HIGH_UNDER_PRESSURE, ALERT_ASIAN_LOW_UNDER_PRESSURE,
    ALL_ALERT_TYPES,
)
from services.canonical_market_data import (
    Bar, CanonicalSnapshot, TimeframeSlice, LevelBundle, SessionInfo,
)


def _bar(ts, o, h, l, c):
    return Bar(time=ts, open=o, high=h, low=l, close=c, volume=1)


def _snap(price=4200, asian_hi=None, asian_lo=None, atr_h1=5.0):
    """Snapshot with M15 + H1 bars around `price`."""
    base = datetime(2026, 8, 1, 0, 0, tzinfo=timezone.utc)
    m15 = [_bar(base + timedelta(minutes=i*15), price, price+1, price-1, price)
            for i in range(20)]
    h1 = [_bar(base + timedelta(hours=i), price, price+atr_h1/2, price-atr_h1/2, price)
           for i in range(20)]
    return CanonicalSnapshot(
        ts=datetime.now(timezone.utc), instrument="XAU/USD",
        bid=price - 0.1, ask=price + 0.1, spread=0.2,
        timeframes={"M15": TimeframeSlice("M15", m15),
                     "H1": TimeframeSlice("H1", h1)},
        levels=LevelBundle(pdh=price+20, pdl=price-20,
                             asian_high=asian_hi, asian_low=asian_lo,
                             daily_open=price - 5),
        session=SessionInfo("NY_OPEN", "NY open", is_active=True,
                             session_open=datetime.now(timezone.utc)),
        data_quality_score=100,
    )


def _bo(direction, level, level_name, classification):
    return SimpleNamespace(
        direction=direction, level=level, level_name=level_name,
        classification=classification,
    )


def _macro(event_risk="NONE", minutes=None):
    return SimpleNamespace(event_risk_level=event_risk, minutes_to_next_event=minutes,
                            dxy_direction="DOWN", dxy_move_pct=-0.3,
                            yield_10y_direction="DOWN", yield_10y_delta_bp=-5,
                            correlation_state="ACTIVE_INVERSE",
                            macro_alignment="SUPPORTIVE",
                            real_yield_direction="DOWN",
                            gold_dxy_correlation=-0.72,
                            macro_alignment_reason="test",
                            move_driver="MACRO_DRIVEN",
                            next_high_impact_event=None,
                            warnings=[])


# ─────────────────────────────────────────────────────────────────────────────
# State transition detection
# ─────────────────────────────────────────────────────────────────────────────

def test_bull_early_warning_state_triggers_conditions_building():
    cands = detect_alert_candidates(
        prev_state="BULLISH_OBSERVING", new_state="BULLISH_EARLY_WARNING",
        trigger_condition="test", trigger_price=4200,
    )
    assert any(c.alert_type == ALERT_BULLISH_CONDITIONS_BUILDING for c in cands)


def test_bear_early_warning_state_triggers_conditions_building():
    cands = detect_alert_candidates(
        prev_state="BEARISH_OBSERVING", new_state="BEARISH_EARLY_WARNING",
        trigger_condition="test", trigger_price=4200,
    )
    assert any(c.alert_type == ALERT_BEARISH_CONDITIONS_BUILDING for c in cands)


def test_bull_transition_detected():
    cands = detect_alert_candidates(
        prev_state=None, new_state="BULLISH_TRANSITION",
        trigger_condition="regime=BULLISH_TRANSITION", trigger_price=4200,
    )
    assert any(c.alert_type == ALERT_BULLISH_TRANSITION_DETECTED for c in cands)


def test_bear_transition_detected():
    cands = detect_alert_candidates(
        prev_state=None, new_state="BEARISH_TRANSITION",
        trigger_condition="regime=BEARISH_TRANSITION", trigger_price=4200,
    )
    assert any(c.alert_type == ALERT_BEARISH_TRANSITION_DETECTED for c in cands)


def test_pullback_states_trigger_zone_alerts():
    cands_bull = detect_alert_candidates(
        prev_state="BULLISH_CONFIRMED", new_state="BULLISH_PULLBACK_PENDING",
        trigger_condition="test", trigger_price=4200,
    )
    assert any(c.alert_type == ALERT_BULLISH_PULLBACK_ZONE for c in cands_bull)
    cands_bear = detect_alert_candidates(
        prev_state="BEARISH_CONFIRMED", new_state="BEARISH_PULLBACK_PENDING",
        trigger_condition="test", trigger_price=4200,
    )
    assert any(c.alert_type == ALERT_BEARISH_PULLBACK_ZONE for c in cands_bear)


def test_extended_states_trigger_extended_alerts():
    for new, expected in [("BULLISH_EXTENDED", ALERT_BULLISH_MOVE_EXTENDED),
                            ("BEARISH_EXTENDED", ALERT_BEARISH_MOVE_EXTENDED)]:
        cands = detect_alert_candidates(
            prev_state="BULLISH_CONFIRMED", new_state=new,
            trigger_condition="test", trigger_price=4200,
        )
        assert any(c.alert_type == expected for c in cands)


def test_invalidated_states_trigger_invalidation_alerts():
    for new, expected in [("BULLISH_INVALIDATED", ALERT_BULLISH_THESIS_INVALIDATED),
                            ("BEARISH_INVALIDATED", ALERT_BEARISH_THESIS_INVALIDATED)]:
        cands = detect_alert_candidates(
            prev_state="BULLISH_CONFIRMED", new_state=new,
            trigger_condition="test", trigger_price=4200,
        )
        assert any(c.alert_type == expected for c in cands)


def test_balanced_range_only_from_directional_state():
    """Coming from BULLISH_TRANSITION → BALANCED_RANGE fires. From nothing → doesn't."""
    cands_from_dir = detect_alert_candidates(
        prev_state="BULLISH_TRANSITION", new_state="BALANCED_RANGE",
        trigger_condition="test", trigger_price=4200,
    )
    assert any(c.alert_type == ALERT_MARKET_RETURNED_TO_BALANCE for c in cands_from_dir)

    cands_from_none = detect_alert_candidates(
        prev_state=None, new_state="BALANCED_RANGE",
        trigger_condition="test", trigger_price=4200,
    )
    assert not any(c.alert_type == ALERT_MARKET_RETURNED_TO_BALANCE for c in cands_from_none)


# ─────────────────────────────────────────────────────────────────────────────
# Breakout-driven alerts
# ─────────────────────────────────────────────────────────────────────────────

def test_pdh_confirmed_breakout_triggers_pdh_broken():
    cands = detect_alert_candidates(
        prev_state=None, new_state="BULLISH_CONFIRMED",
        trigger_condition="test", trigger_price=4270,
        breakouts=[_bo("UP", 4267, "PDH", "BREAKOUT_CONFIRMED")],
    )
    assert any(c.alert_type == ALERT_PDH_BROKEN for c in cands)


def test_pdl_confirmed_breakout_triggers_pdl_broken():
    cands = detect_alert_candidates(
        prev_state=None, new_state="BEARISH_CONFIRMED",
        trigger_condition="test", trigger_price=4100,
        breakouts=[_bo("DOWN", 4110, "PDL", "BREAKOUT_CONFIRMED")],
    )
    assert any(c.alert_type == ALERT_PDL_BROKEN for c in cands)


def test_acceptance_up_triggers_bullish_acceptance():
    cands = detect_alert_candidates(
        prev_state=None, new_state="BULLISH_CONFIRMED",
        trigger_condition="test", trigger_price=4300,
        breakouts=[_bo("UP", 4267, "PDH", "BREAKOUT_ACCEPTANCE")],
    )
    assert any(c.alert_type == ALERT_BULLISH_BREAKOUT_ACCEPTANCE for c in cands)
    assert any(c.alert_type == ALERT_PDH_BROKEN for c in cands)


def test_acceptance_down_triggers_bearish_acceptance():
    cands = detect_alert_candidates(
        prev_state=None, new_state="BEARISH_CONFIRMED",
        trigger_condition="test", trigger_price=4050,
        breakouts=[_bo("DOWN", 4100, "PDL", "BREAKOUT_ACCEPTANCE")],
    )
    assert any(c.alert_type == ALERT_BEARISH_BREAKDOWN_ACCEPTANCE for c in cands)


def test_no_breakout_alert_when_no_breakouts():
    cands = detect_alert_candidates(
        prev_state=None, new_state="BULLISH_CONFIRMED",
        trigger_condition="test", trigger_price=4200,
        breakouts=None,
    )
    assert not any(c.alert_type in (ALERT_PDH_BROKEN, ALERT_PDL_BROKEN,
                                     ALERT_BULLISH_BREAKOUT_ACCEPTANCE)
                    for c in cands)


# ─────────────────────────────────────────────────────────────────────────────
# Asian pressure alerts
# ─────────────────────────────────────────────────────────────────────────────

def test_asian_high_pressure_when_within_half_atr_and_pushing():
    """Price 4198 with asian_hi 4200 (< 0.5 × 5 ATR = 2.5pt away) and rising."""
    snap = _snap(price=4198, asian_hi=4200, asian_lo=4150, atr_h1=5.0)
    # Make last M15 have higher high than previous
    m15 = snap.timeframes["M15"].candles
    m15[-2] = _bar(m15[-2].time, 4197, 4198, 4196, 4197)
    m15[-1] = _bar(m15[-1].time, 4197, 4199, 4197, 4198)
    cands = detect_alert_candidates(
        prev_state=None, new_state="BULLISH_TRANSITION",
        trigger_condition="test", trigger_price=4198,
        snapshot=snap,
    )
    assert any(c.alert_type == ALERT_ASIAN_HIGH_UNDER_PRESSURE for c in cands)


def test_asian_low_pressure_when_within_half_atr_and_pushing():
    snap = _snap(price=4152, asian_hi=4300, asian_lo=4150, atr_h1=5.0)
    m15 = snap.timeframes["M15"].candles
    m15[-2] = _bar(m15[-2].time, 4153, 4154, 4152, 4153)
    m15[-1] = _bar(m15[-1].time, 4153, 4153, 4151, 4152)
    cands = detect_alert_candidates(
        prev_state=None, new_state="BEARISH_TRANSITION",
        trigger_condition="test", trigger_price=4152,
        snapshot=snap,
    )
    assert any(c.alert_type == ALERT_ASIAN_LOW_UNDER_PRESSURE for c in cands)


# ─────────────────────────────────────────────────────────────────────────────
# Macro / system alerts
# ─────────────────────────────────────────────────────────────────────────────

def test_high_impact_event_triggers_event_risk_alert():
    cands = detect_alert_candidates(
        prev_state=None, new_state="BALANCED_RANGE",
        trigger_condition="test", trigger_price=4200,
        macro=_macro(event_risk="HIGH", minutes=8),
    )
    assert any(c.alert_type == ALERT_HIGH_IMPACT_EVENT_RISK for c in cands)


def test_no_event_alert_when_risk_low():
    cands = detect_alert_candidates(
        prev_state=None, new_state="BALANCED_RANGE",
        trigger_condition="test", trigger_price=4200,
        macro=_macro(event_risk="LOW", minutes=180),
    )
    assert not any(c.alert_type == ALERT_HIGH_IMPACT_EVENT_RISK for c in cands)


# ─────────────────────────────────────────────────────────────────────────────
# Fingerprinting
# ─────────────────────────────────────────────────────────────────────────────

def test_fingerprint_same_for_same_input():
    fp1 = _fingerprint(ALERT_PDH_BROKEN, 4267.13)
    fp2 = _fingerprint(ALERT_PDH_BROKEN, 4267.14)
    # Rounded to nearest 5 pts → same bucket
    assert fp1 == fp2


def test_fingerprint_differs_for_far_prices():
    fp1 = _fingerprint(ALERT_PDH_BROKEN, 4267)
    fp2 = _fingerprint(ALERT_PDH_BROKEN, 4300)
    assert fp1 != fp2


def test_fingerprint_differs_by_alert_type():
    fp1 = _fingerprint(ALERT_PDH_BROKEN, 4200)
    fp2 = _fingerprint(ALERT_PDL_BROKEN, 4200)
    assert fp1 != fp2


# ─────────────────────────────────────────────────────────────────────────────
# Template rendering
# ─────────────────────────────────────────────────────────────────────────────

def test_body_contains_required_headers():
    snap = _snap(price=4200)
    verdict = SimpleNamespace(
        directional_assessment="Bullish", opportunity_status="Direction confirmed",
        entry_status="Entry developing", directional_reason="", opportunity_reason="",
        entry_reason="", ready_to_alert=True, confidence=65, warnings=[],
    )
    evidence = SimpleNamespace(
        dominant_direction="BULL", bull_evidence_score=60, bear_evidence_score=10,
        contradiction_score=0, data_quality_score=100, event_risk_score=0,
        extension_risk_score=15, directional_confidence=55, entry_quality_confidence=55,
        bull_items=[SimpleNamespace(description="M15 BOS up")],
        bear_items=[], contradictions=[],
    )
    ranking = SimpleNamespace(tier1=[
        SimpleNamespace(price=4210, label="PDH", side="ABOVE", distance_atr=1),
        SimpleNamespace(price=4190, label="Daily open", side="BELOW", distance_atr=1),
    ], tier2=[], tier3=[])
    macro = _macro()
    st = SimpleNamespace(new_state="BULLISH_CONFIRMED",
                          invalidation_price=4180, price=4200)
    body = build_intel_body(
        alert_type=ALERT_BULLISH_TRANSITION_DETECTED,
        trigger_reason="test trigger", snapshot=snap, verdict=verdict,
        ranking=ranking, macro=macro, evidence=evidence, state_transition=st,
    )
    # Required sections per brief
    assert "XAUUSD MARKET INTELLIGENCE" in body
    assert "Time:" in body
    assert "Price:" in body
    assert "Session:" in body
    assert "Directional assessment:" in body
    assert "Market regime:" in body
    assert "Opportunity state:" in body
    assert "Directional confidence:" in body
    assert "Entry confidence:" in body
    assert "Data quality:" in body
    assert "What changed:" in body
    assert "Basis of direction:" in body
    assert "Contradictions:" in body
    assert "Tier 1 levels:" in body
    assert "Macro context:" in body
    assert "Current interpretation:" in body
    assert "Action:" in body
    # NOT a trade signal
    assert "Not a trade signal" in body


def test_body_action_maps_from_state():
    snap = _snap(price=4200)
    verdict = SimpleNamespace(
        directional_assessment="Bullish", opportunity_status="Move extended",
        entry_status="RR inadequate", directional_reason="", opportunity_reason="",
        entry_reason="", ready_to_alert=True, confidence=65, warnings=[],
    )
    evidence = SimpleNamespace(
        dominant_direction="BULL", bull_evidence_score=60, bear_evidence_score=10,
        contradiction_score=0, data_quality_score=100, event_risk_score=0,
        extension_risk_score=90, directional_confidence=40, entry_quality_confidence=40,
        bull_items=[], bear_items=[], contradictions=[],
    )
    ranking = SimpleNamespace(tier1=[], tier2=[], tier3=[])
    st = SimpleNamespace(new_state="BULLISH_EXTENDED",
                          invalidation_price=None, price=4200)
    body = build_intel_body(
        alert_type=ALERT_BULLISH_MOVE_EXTENDED, trigger_reason="test",
        snapshot=snap, verdict=verdict, ranking=ranking, macro=None,
        evidence=evidence, state_transition=st,
    )
    assert "Action: Do not chase" in body


# ─────────────────────────────────────────────────────────────────────────────
# Sanity
# ─────────────────────────────────────────────────────────────────────────────

def test_all_18_alert_types_exist():
    assert len(ALL_ALERT_TYPES) == 19  # 18 spec + DATA_QUALITY_DEGRADED (added)


def test_no_candidates_when_no_state_change_and_nothing_else():
    cands = detect_alert_candidates(
        prev_state="BULLISH_OBSERVING", new_state="BULLISH_OBSERVING",
        trigger_condition="no change", trigger_price=4200,
    )
    assert cands == []


if __name__ == "__main__":
    import subprocess
    subprocess.run(["python", "-m", "pytest", __file__, "-v"], check=False)
