"""
Phase 12 — Entry-rule regression suite.

The brief requires: "Do not weaken trade execution requirements merely to
generate more alerts." Every entry gate the mandate strategist enforces
must survive the Phase 2-11 additions unchanged.

This suite exercises each gate from `services.execution_gates` and each
separated-verdict entry-status guard from Phase 8. A failure here means
a downstream phase regressed a real trade rule.
"""
import sys, os
from types import SimpleNamespace
from datetime import datetime, timezone, timedelta

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from services.execution_gates import (
    check_setup_score, check_expected_value,
    check_session_penalty, check_spread_penalty,
    check_news_proximity, check_direction_flip_cooldown,
    check_post_loss_cooldown, mark_direction_flip,
    estimate_win_rate_from_score, positive_ev,
    evaluate_execution_quality,
)
from services.separated_verdicts import (
    compute_separated_verdict,
    ES_ENTRY_CONFIRMED, ES_ENTRY_DEVELOPING, ES_NO_COMPLIANT_ENTRY,
    ES_ENTRY_INVALID, ES_RR_INADEQUATE, ES_SPREAD_TOO_HIGH,
    ES_NEWS_BLOCKED, ES_DATA_STALE,
)


# ─────────────────────────────────────────────────────────────────────────────
# 1. Fresh data — Phase 2 canonical + freshness thresholds
# ─────────────────────────────────────────────────────────────────────────────

def test_freshness_thresholds_intact():
    """Per-TF thresholds from Pre-Phase 0 must not have drifted."""
    from services.data_freshness import STALENESS_MIN_BY_TF
    assert STALENESS_MIN_BY_TF["M1"]  == 3
    assert STALENESS_MIN_BY_TF["M5"]  == 10
    assert STALENESS_MIN_BY_TF["M15"] == 20
    assert STALENESS_MIN_BY_TF["H1"]  == 70
    assert STALENESS_MIN_BY_TF["H4"]  == 300
    assert STALENESS_MIN_BY_TF["D1"]  == 1560


def test_data_quality_score_bounds():
    """data_quality_score always ∈ [0, 100]."""
    from services.data_freshness import data_quality_score
    # Perfect
    d1 = {"M15": {"age_min": 10}, "H1": {"age_min": 30}}
    assert data_quality_score(d1) <= 100
    # Empty
    assert data_quality_score({}) == 0
    # Way stale
    d2 = {"M15": {"age_min": 500}}
    assert data_quality_score(d2) == 0


def test_separated_verdict_gates_entry_on_stale_data():
    """Phase 8 must refuse ENTRY_CONFIRMED when data_quality_score < 70."""
    evidence = SimpleNamespace(
        data_quality_score=40, dominant_direction="BULL",
        bull_evidence_score=70, bear_evidence_score=10,
        contradictions=[], extension_risk_score=15,
        event_risk_score=0, directional_confidence=50,
        entry_quality_confidence=20,
    )
    state = SimpleNamespace(new_state="BULLISH_ENTRY_AVAILABLE", trigger_condition="test")
    v = compute_separated_verdict(snapshot=SimpleNamespace(),
                                    htf_alignment=None, evidence=evidence,
                                    state_transition=state)
    assert v.entry_status == ES_DATA_STALE


# ─────────────────────────────────────────────────────────────────────────────
# 2. Stop-loss + invalidation — separated-verdict layer
# ─────────────────────────────────────────────────────────────────────────────

def test_state_invalidated_maps_to_entry_invalid():
    """After thesis invalidation, entry status must be INVALID."""
    evidence = SimpleNamespace(
        data_quality_score=100, dominant_direction="BEAR",
        bull_evidence_score=10, bear_evidence_score=50,
        contradictions=[], extension_risk_score=10,
        event_risk_score=0, directional_confidence=40,
        entry_quality_confidence=40,
    )
    state = SimpleNamespace(new_state="BULLISH_INVALIDATED",
                             trigger_condition="regime flipped")
    v = compute_separated_verdict(snapshot=SimpleNamespace(),
                                    htf_alignment=None, evidence=evidence,
                                    state_transition=state)
    assert v.entry_status == ES_ENTRY_INVALID


# ─────────────────────────────────────────────────────────────────────────────
# 3. RR / EV gates — execution_gates.check_expected_value
# ─────────────────────────────────────────────────────────────────────────────

def test_check_expected_value_rejects_low_rr():
    """Low RR + low WR = negative EV → gate blocks."""
    ok, reason = check_expected_value(setup_score=70, rr=1.0)
    assert not ok and "negative EV" in reason


def test_check_expected_value_accepts_strong_rr():
    """Score >= 90 band with RR >= 2.5 must clear the EV gate."""
    ok, reason = check_expected_value(setup_score=92, rr=3.0)
    assert ok and "positive EV" in reason


def test_win_rate_estimator_bands():
    """The four calibrated bands remain: >=90, 85-89, 80-84, 70-79, <70."""
    _, wr90, r90 = estimate_win_rate_from_score(92)
    _, wr85, r85 = estimate_win_rate_from_score(87)
    _, wr70, r70 = estimate_win_rate_from_score(72)
    assert wr90 > wr85 > wr70    # monotonic
    assert r90 > r85 > r70       # monotonic


# ─────────────────────────────────────────────────────────────────────────────
# 4. Setup-score gate — execution_gates.check_setup_score
# ─────────────────────────────────────────────────────────────────────────────

def test_setup_score_gate_rejects_below_threshold():
    ok, reason = check_setup_score(setup_score=60, threshold=85)
    assert not ok and "60" in reason and "85" in reason


def test_setup_score_gate_accepts_at_threshold():
    ok, reason = check_setup_score(setup_score=85, threshold=85)
    assert ok


# ─────────────────────────────────────────────────────────────────────────────
# 5. Spread gate — execution_gates.check_spread_penalty
# ─────────────────────────────────────────────────────────────────────────────

def test_spread_gate_accepts_tight():
    ok, reason = check_spread_penalty(spread_pts=1.5, tight_max=3.0, hard_max=5.0)
    assert ok


def test_spread_gate_rejects_in_penalty_band():
    """3-5 pt band demotes to signal-only (returns False)."""
    ok, reason = check_spread_penalty(spread_pts=4.0, tight_max=3.0, hard_max=5.0)
    assert not ok and "penalty band" in reason


def test_spread_gate_rejects_over_hard_max():
    ok, reason = check_spread_penalty(spread_pts=6.5, tight_max=3.0, hard_max=5.0)
    assert not ok and "hard max" in reason


def test_spread_gate_unknown_spread_passes():
    """Unknown spread is not a rejection reason — we can't gate on absent data."""
    ok, _ = check_spread_penalty(spread_pts=None)
    assert ok


def test_verdict_spread_too_high_when_contradiction_present():
    """Phase 8 must map EXCESSIVE_SPREAD contradiction to entry_status=SPREAD_TOO_HIGH."""
    evidence = SimpleNamespace(
        data_quality_score=100, dominant_direction="BULL",
        bull_evidence_score=70, bear_evidence_score=10,
        contradictions=[SimpleNamespace(name="EXCESSIVE_SPREAD",
                                          weight=7, description="spread 6.2 > 5")],
        extension_risk_score=15, event_risk_score=0,
        directional_confidence=60, entry_quality_confidence=60,
    )
    state = SimpleNamespace(new_state="BULLISH_ENTRY_AVAILABLE",
                             trigger_condition="test")
    v = compute_separated_verdict(snapshot=SimpleNamespace(),
                                    htf_alignment=None, evidence=evidence,
                                    state_transition=state)
    assert v.entry_status == ES_SPREAD_TOO_HIGH


# ─────────────────────────────────────────────────────────────────────────────
# 6. News gate — execution_gates.check_news_proximity
# ─────────────────────────────────────────────────────────────────────────────

def test_news_gate_no_db_returns_safe_pass():
    """When DB unavailable, gate returns True (fail-safe, alert elsewhere)."""
    ok, _ = check_news_proximity(db=None, lookahead_min=30)
    assert ok


def test_verdict_news_blocked_when_contradiction_present():
    """Phase 8 maps NEWS_APPROACHING contradiction to entry_status=NEWS_BLOCKED."""
    evidence = SimpleNamespace(
        data_quality_score=100, dominant_direction="BULL",
        bull_evidence_score=70, bear_evidence_score=10,
        contradictions=[SimpleNamespace(name="NEWS_APPROACHING",
                                          weight=12, description="NFP in 8m")],
        extension_risk_score=15, event_risk_score=80,
        directional_confidence=40, entry_quality_confidence=40,
    )
    state = SimpleNamespace(new_state="BULLISH_ENTRY_AVAILABLE",
                             trigger_condition="test")
    v = compute_separated_verdict(snapshot=SimpleNamespace(),
                                    htf_alignment=None, evidence=evidence,
                                    state_transition=state)
    assert v.entry_status == ES_NEWS_BLOCKED


# ─────────────────────────────────────────────────────────────────────────────
# 7. Session penalty gate — execution_gates.check_session_penalty
# ─────────────────────────────────────────────────────────────────────────────

def test_session_penalty_ny_kz_needs_higher_score():
    """NY killzone requires setup_score >= base + 5."""
    ok, _ = check_session_penalty(session_label="ny_kz", setup_score=87,
                                     base_threshold=85)   # 87 < 85+5=90
    assert not ok


def test_session_penalty_asia_needs_higher_score():
    ok, _ = check_session_penalty(session_label="asian_range", setup_score=88,
                                     base_threshold=85)   # 88 < 90
    assert not ok


def test_session_penalty_late_ny_effectively_blocks():
    """late_ny bumps 99 — effectively unreachable."""
    ok, _ = check_session_penalty(session_label="late_ny", setup_score=98,
                                     base_threshold=85)
    assert not ok


def test_session_penalty_clean_session_passes():
    ok, _ = check_session_penalty(session_label="london_kz", setup_score=85,
                                     base_threshold=85)
    assert ok


# ─────────────────────────────────────────────────────────────────────────────
# 8. Extension risk — RR inadequate when very extended
# ─────────────────────────────────────────────────────────────────────────────

def test_verdict_rr_inadequate_when_extended_over_90():
    evidence = SimpleNamespace(
        data_quality_score=100, dominant_direction="BULL",
        bull_evidence_score=70, bear_evidence_score=10,
        contradictions=[],
        extension_risk_score=95,
        event_risk_score=0, directional_confidence=40,
        entry_quality_confidence=40,
    )
    state = SimpleNamespace(new_state="BULLISH_EXTENDED",
                             trigger_condition="test")
    v = compute_separated_verdict(snapshot=SimpleNamespace(),
                                    htf_alignment=None, evidence=evidence,
                                    state_transition=state)
    assert v.entry_status == ES_RR_INADEQUATE


# ─────────────────────────────────────────────────────────────────────────────
# 9. Direction-flip cooldown
# ─────────────────────────────────────────────────────────────────────────────

def test_direction_flip_cooldown_fresh_flip_blocked():
    """Just-flipped BUY → next BUY signal blocked within cooldown."""
    mark_direction_flip("BUY")
    ok, reason = check_direction_flip_cooldown("BUY", cooldown_min=120)
    assert not ok and "cooldown" in reason


def test_direction_flip_cooldown_opposite_direction_passes():
    mark_direction_flip("BUY")
    ok, _ = check_direction_flip_cooldown("SELL", cooldown_min=120)
    assert ok


# ─────────────────────────────────────────────────────────────────────────────
# 10. Post-loss cooldown (contract: gate exists + returns tuple)
# ─────────────────────────────────────────────────────────────────────────────

def test_post_loss_cooldown_returns_tuple():
    """Must return (bool, str) — even with no db."""
    result = check_post_loss_cooldown(db=None, cooldown_min=60)
    assert isinstance(result, tuple) and len(result) == 2
    assert isinstance(result[0], bool) and isinstance(result[1], str)


# ─────────────────────────────────────────────────────────────────────────────
# 11. Aggregator — evaluate_execution_quality shape
# ─────────────────────────────────────────────────────────────────────────────

def test_aggregator_returns_expected_keys():
    r = evaluate_execution_quality(
        db=None, setup_score=90, rr=2.5,
        session_label="london_kz", direction="BUY", spread_pts=1.0,
    )
    for k in ("allow_execution", "should_demote", "reasons", "wr_label"):
        assert k in r
    assert isinstance(r["reasons"], list)


def test_aggregator_blocks_low_score():
    r = evaluate_execution_quality(
        db=None, setup_score=50, rr=2.5,
        session_label="london_kz", direction="BUY", spread_pts=1.0,
        settings=SimpleNamespace(min_setup_score_for_execution=85,
                                   require_positive_ev=True,
                                   session_penalty_enabled=False,
                                   post_loss_cooldown_min=0,
                                   direction_flip_cooldown_min=0),
    )
    assert not r["allow_execution"]
    assert any("Q1_BLOCK" in reason for reason in r["reasons"])


# ─────────────────────────────────────────────────────────────────────────────
# 12. Phase 2-11 feature flags — must default OFF (no accidental production impact)
# ─────────────────────────────────────────────────────────────────────────────

def test_phase_flags_default_false():
    """Prevent accidental promotion — each Phase 2-11 flag must ship OFF."""
    from config import Settings
    s = Settings()
    for flag in (
        "xauusd_canonical_data_enabled",
        "xauusd_market_regime_enabled",
        "xauusd_weighted_htf_alignment_enabled",
        "xauusd_directional_intelligence_enabled",
        "xauusd_breakout_acceptance_enabled",
        "xauusd_opportunity_state_machine_enabled",
        "xauusd_separated_verdicts_enabled",
        "xauusd_key_level_ranking_enabled",
        "xauusd_macro_interpretation_enabled",
        "xauusd_market_intelligence_telegram_enabled",
        "xauusd_opportunity_coverage_enabled",
        "xauusd_replay_validation_enabled",
    ):
        assert getattr(s, flag) is False, f"{flag} must default False"


def test_shadow_mode_defaults_true():
    """Even if telegram enabled were flipped on, shadow_mode gates the send."""
    from config import Settings
    s = Settings()
    assert s.xauusd_market_intel_shadow_mode is True


# ─────────────────────────────────────────────────────────────────────────────
# 13. Guardrail: the intel body always identifies itself as NOT a trade signal
# ─────────────────────────────────────────────────────────────────────────────

def test_intel_body_always_marked_not_a_trade_signal():
    """Phase 11 template must never look like an entry signal."""
    from services.market_intelligence_alerts import build_intel_body
    from services.canonical_market_data import Bar, CanonicalSnapshot, TimeframeSlice, LevelBundle, SessionInfo
    snap = CanonicalSnapshot(
        ts=datetime.now(timezone.utc), instrument="XAU/USD",
        bid=4200.0, ask=4200.2, spread=0.2,
        timeframes={"M15": TimeframeSlice("M15", [
            Bar(datetime.now(timezone.utc), 4200, 4201, 4199, 4200)])},
        levels=LevelBundle(), session=SessionInfo("NY_OPEN", "NY open"),
        data_quality_score=100,
    )
    verdict = SimpleNamespace(
        directional_assessment="Bullish", opportunity_status="Direction confirmed",
        entry_status="Entry developing", directional_reason="", opportunity_reason="",
        entry_reason="", ready_to_alert=True, confidence=60, warnings=[],
    )
    evidence = SimpleNamespace(
        dominant_direction="BULL", bull_evidence_score=50, bear_evidence_score=10,
        contradiction_score=0, data_quality_score=100, event_risk_score=0,
        extension_risk_score=15, directional_confidence=50, entry_quality_confidence=50,
        bull_items=[], bear_items=[], contradictions=[],
    )
    ranking = SimpleNamespace(tier1=[], tier2=[], tier3=[])
    st = SimpleNamespace(new_state="BULLISH_CONFIRMED", invalidation_price=None, price=4200)
    body = build_intel_body(
        alert_type="BULLISH_TRANSITION_DETECTED", trigger_reason="test",
        snapshot=snap, verdict=verdict, ranking=ranking, macro=None,
        evidence=evidence, state_transition=st,
    )
    assert "Not a trade signal" in body


if __name__ == "__main__":
    import subprocess
    subprocess.run(["python", "-m", "pytest", __file__, "-v"], check=False)
