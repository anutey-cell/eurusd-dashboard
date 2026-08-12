"""Unit tests for signal grading (A+/A/B/C/STAND_ASIDE)."""
import sys, os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from services.signal_grading import (
    grade_verdict, format_signal_grade_body, format_stand_aside_body,
    GRADE_APLUS, GRADE_A, GRADE_B, GRADE_C, GRADE_ASIDE,
    ALERT_GRADES, WATCHLIST_GRADES, SUPPRESS_GRADES,
)


def _verdict(*, decision="BUY", cp=4, setup_score=85, rr=2.7,
              entry=4200, sl=4180, tp1=4230, tp2=4260,
              invalidation=None, lm_type="liquidity_sweep",
              execution_status="SIGNAL_ONLY"):
    return {
        "decision": decision,
        "conditions_passed": cp,
        "setup_score": setup_score,
        "execution_status": execution_status,
        "liquidity_model": {"type": lm_type} if lm_type else {},
        "trade_plan": {
            "entry": entry, "stop_loss": sl,
            "tp1": tp1, "tp2": tp2, "risk_reward": rr,
            "invalidation": invalidation,
        },
    }


# ─────────────────────────────────────────────────────────────────────────────
# Grade classification
# ─────────────────────────────────────────────────────────────────────────────

def test_a_plus_when_top_tier():
    r = grade_verdict(_verdict(setup_score=92, rr=3.5))
    assert r.grade == GRADE_APLUS
    assert r.should_alert and not r.should_suppress


def test_a_when_normal_alert_tier():
    r = grade_verdict(_verdict(setup_score=85, rr=2.8))
    assert r.grade == GRADE_A
    assert r.should_alert


def test_a_downgraded_when_rr_below_threshold():
    r = grade_verdict(_verdict(setup_score=85, rr=1.9))
    # score OK but RR too low for A — falls to B (watchlist) since score >= 70
    assert r.grade == GRADE_B


def test_b_watchlist_grade():
    r = grade_verdict(_verdict(setup_score=72, rr=2.0, cp=3))
    assert r.grade == GRADE_B
    assert r.should_watchlist and not r.should_alert


def test_c_when_score_too_low():
    r = grade_verdict(_verdict(setup_score=60, rr=3.0))
    assert r.grade == GRADE_C
    assert r.should_suppress


# ─────────────────────────────────────────────────────────────────────────────
# STAND_ASIDE guards
# ─────────────────────────────────────────────────────────────────────────────

def test_stand_aside_when_decision_not_buy_sell():
    r = grade_verdict(_verdict(decision="STAND ASIDE"))
    assert r.grade == GRADE_ASIDE


def test_stand_aside_when_sl_missing():
    r = grade_verdict(_verdict(sl=None))
    assert r.grade == GRADE_ASIDE and "missing" in r.reason.lower()


def test_stand_aside_when_tp1_missing():
    r = grade_verdict(_verdict(tp1=None))
    assert r.grade == GRADE_ASIDE


def test_stand_aside_when_execution_status_news_blocked():
    r = grade_verdict(_verdict(execution_status="NEWS_BLOCKED"))
    assert r.grade == GRADE_ASIDE and "NEWS_BLOCKED" in r.reason


def test_stand_aside_when_execution_status_spread_high():
    r = grade_verdict(_verdict(execution_status="SPREAD_HIGH"))
    assert r.grade == GRADE_ASIDE


def test_stand_aside_when_cp_below_3():
    r = grade_verdict(_verdict(cp=2))
    assert r.grade == GRADE_ASIDE


def test_stand_aside_when_rr_zero():
    r = grade_verdict(_verdict(rr=0))
    assert r.grade == GRADE_ASIDE


# ─────────────────────────────────────────────────────────────────────────────
# A+ requires liquidity confirmation
# ─────────────────────────────────────────────────────────────────────────────

def test_a_plus_downgrades_to_a_without_liquidity_confirm():
    """Score 92 + RR 3.5 but no liquidity_model.type → falls to A."""
    r = grade_verdict(_verdict(setup_score=92, rr=3.5, lm_type=None))
    assert r.grade == GRADE_A


# ─────────────────────────────────────────────────────────────────────────────
# Threshold overrides
# ─────────────────────────────────────────────────────────────────────────────

def test_custom_thresholds_respected():
    """Loosen thresholds — score 75 + RR 2.0 should now qualify as A."""
    r = grade_verdict(_verdict(setup_score=75, rr=2.0),
                        min_score_a=70, min_rr_a=1.8)
    assert r.grade == GRADE_A


# ─────────────────────────────────────────────────────────────────────────────
# Template rendering
# ─────────────────────────────────────────────────────────────────────────────

def test_signal_body_contains_all_required_fields():
    v = _verdict(setup_score=92, rr=3.5)
    r = grade_verdict(v)
    body = format_signal_grade_body(v, r, spread_pts=1.8)
    for field in ("XAUUSD SIGNAL", "A+", "Bias:", "Setup:", "Entry Zone:",
                   "Stop Loss:", "Take Profit 1:", "Take Profit 2:",
                   "Take Profit 3:", "Risk/Reward:", "Setup Score:",
                   "Session:", "Spread:", "Data Source:", "Invalidation:",
                   "Reason:", "Signal-only mode"):
        assert field in body, f"missing '{field}' in body"


def test_signal_body_uses_grade_in_header():
    v = _verdict(setup_score=85, rr=2.8)
    r = grade_verdict(v)
    body = format_signal_grade_body(v, r)
    assert "XAUUSD SIGNAL — A\n" in body


def test_stand_aside_body_has_reason():
    v = _verdict(decision="STAND ASIDE")
    r = grade_verdict(v)
    body = format_stand_aside_body(v, r)
    assert "STAND ASIDE" in body and "Reason:" in body


# ─────────────────────────────────────────────────────────────────────────────
# Sets
# ─────────────────────────────────────────────────────────────────────────────

def test_alert_grades_only_alertable():
    assert GRADE_APLUS in ALERT_GRADES and GRADE_A in ALERT_GRADES
    assert GRADE_B not in ALERT_GRADES
    assert GRADE_C not in ALERT_GRADES
    assert GRADE_ASIDE not in ALERT_GRADES


def test_watchlist_grades():
    assert WATCHLIST_GRADES == {GRADE_B}


def test_suppress_grades():
    assert GRADE_C in SUPPRESS_GRADES and GRADE_ASIDE in SUPPRESS_GRADES


if __name__ == "__main__":
    import subprocess
    subprocess.run(["python", "-m", "pytest", __file__, "-v"], check=False)
