"""Validate execution_gates.py — every quality/headwind check + aggregator."""
from __future__ import annotations
import os, sys
from datetime import datetime, timezone, timedelta
from types import SimpleNamespace

try:
    import services.execution_gates  # noqa
except ImportError:
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from services.execution_gates import (
    estimate_win_rate_from_score, positive_ev,
    check_setup_score, check_expected_value,
    check_session_penalty, check_post_loss_cooldown,
    check_direction_flip_cooldown, check_spread_penalty,
    check_news_proximity,
    mark_direction_flip,
    evaluate_execution_quality,
)
from services import execution_gates as gates_mod


TOTAL, PASSED = 0, 0


def _hr(t): print("\n" + "-"*78 + f"\n {t}\n" + "-"*78)
def _run(name, fn):
    global TOTAL, PASSED
    TOTAL += 1
    try:
        fn(); print(f"  OK   {name}"); PASSED += 1
    except AssertionError as e:
        print(f"  FAIL {name}: {e}")
    except Exception as e:
        print(f"  FAIL {name}: {type(e).__name__}: {e}")


# ── Estimator ───────────────────────────────────────────────────────────────

def test_estimator_bands():
    label, wr, r = estimate_win_rate_from_score(95)
    assert wr == 0.30 and r > 0
    label, wr, r = estimate_win_rate_from_score(85)
    assert wr == 0.25
    label, wr, r = estimate_win_rate_from_score(70)
    assert wr < 0.25
    label, wr, r = estimate_win_rate_from_score(50)
    assert wr == 0.0


def test_positive_ev_math():
    ok, ev = positive_ev(0.30, 2.5)
    assert ok and abs(ev - 0.05) < 1e-6
    ok, ev = positive_ev(0.21, 2.5)
    assert not ok and ev < 0


# ── Q1 min setup score ─────────────────────────────────────────────────────

def test_q1_score_gate():
    ok, _ = check_setup_score(90, 85);  assert ok
    ok, _ = check_setup_score(84, 85);  assert not ok
    ok, _ = check_setup_score(85, 85);  assert ok


# ── Q2 EV filter ────────────────────────────────────────────────────────────

def test_q2_ev_positive():
    ok, why = check_expected_value(90, 2.5)
    assert ok, why

def test_q2_ev_negative_low_score():
    ok, why = check_expected_value(80, 2.5)
    assert not ok


# ── H1 session penalty ─────────────────────────────────────────────────────

def test_h1_ny_kz_penalty():
    ok, _ = check_session_penalty("ny_kz", 87, 85);   assert not ok, "NY needs 90"
    ok, _ = check_session_penalty("ny_kz", 90, 85);   assert ok
    ok, _ = check_session_penalty("london_kz", 85, 85);  assert ok
    ok, _ = check_session_penalty(None, 90, 85);      assert ok


def test_h1_late_ny_blocked():
    ok, _ = check_session_penalty("late_ny", 99, 85);  assert not ok
    ok, _ = check_session_penalty("late_ny_kz", 100, 85);  assert not ok


# ── H2 post-loss cooldown ──────────────────────────────────────────────────

def test_h2_no_recent_stop_passes():
    class FakeQ:
        def __init__(self): pass
        def filter(self, *a, **k): return self
        def order_by(self, *a, **k): return self
        def first(self): return None
    class FakeDB:
        def query(self, model): return FakeQ()
    ok, _ = check_post_loss_cooldown(FakeDB(), 60)
    assert ok


def test_h2_cooldown_disabled():
    ok, _ = check_post_loss_cooldown(None, 0)   # zero = disabled
    assert ok


# ── H3 direction-flip cooldown ─────────────────────────────────────────────

def test_h3_no_flip_recorded():
    gates_mod._last_flip_at = None
    gates_mod._last_flip_direction = None
    ok, _ = check_direction_flip_cooldown("BUY", 120)
    assert ok


def test_h3_flip_within_cooldown_blocks():
    mark_direction_flip("SELL")
    ok, why = check_direction_flip_cooldown("SELL", 120)
    assert not ok, why


def test_h3_flip_expired_passes():
    from datetime import datetime, timezone, timedelta
    gates_mod._last_flip_at = datetime.now(timezone.utc) - timedelta(hours=3)
    gates_mod._last_flip_direction = "SELL"
    ok, _ = check_direction_flip_cooldown("SELL", 120)
    assert ok


def test_h3_different_direction_passes():
    mark_direction_flip("SELL")
    ok, _ = check_direction_flip_cooldown("BUY", 120)
    assert ok


# ── H4 spread penalty ──────────────────────────────────────────────────────

def test_h4_clean_spread_passes():
    ok, _ = check_spread_penalty(2.0, 3.0, 5.0);   assert ok

def test_h4_penalty_band_blocks():
    ok, _ = check_spread_penalty(4.0, 3.0, 5.0);   assert not ok

def test_h4_hard_max_blocks():
    ok, _ = check_spread_penalty(6.0, 3.0, 5.0);   assert not ok

def test_h4_none_passes():
    ok, _ = check_spread_penalty(None);            assert ok


# ── H5 news proximity ─────────────────────────────────────────────────────

def test_h5_no_news_passes():
    class FakeQ:
        def filter(self, *a, **k): return self
        def order_by(self, *a, **k): return self
        def first(self): return None
    class FakeDB:
        def query(self, model): return FakeQ()
    ok, _ = check_news_proximity(FakeDB(), 30)
    assert ok


# ── Aggregator ─────────────────────────────────────────────────────────────

def test_aggregator_top_grade_signal_passes():
    """Score 92, rr 2.5, London, no headwinds → allow_execution=True."""
    gates_mod._last_flip_at = None
    gates_mod._last_flip_direction = None
    class FakeQ:
        def filter(self, *a, **k): return self
        def order_by(self, *a, **k): return self
        def first(self): return None
    class FakeDB:
        def query(self, model): return FakeQ()
    r = evaluate_execution_quality(
        db=FakeDB(), setup_score=92, rr=2.5, session_label="london_kz",
        direction="BUY", spread_pts=2.0,
    )
    assert r["allow_execution"] is True, r["reasons"]


def test_aggregator_low_score_blocks():
    gates_mod._last_flip_at = None
    class FakeQ:
        def filter(self, *a, **k): return self
        def order_by(self, *a, **k): return self
        def first(self): return None
    class FakeDB:
        def query(self, model): return FakeQ()
    r = evaluate_execution_quality(
        db=FakeDB(), setup_score=78, rr=2.5, session_label="london_kz",
        direction="BUY",
    )
    assert r["allow_execution"] is False


def test_aggregator_ny_needs_higher_score():
    gates_mod._last_flip_at = None
    class FakeQ:
        def filter(self, *a, **k): return self
        def order_by(self, *a, **k): return self
        def first(self): return None
    class FakeDB:
        def query(self, model): return FakeQ()
    # Score 86 — passes Q1 base 85, but H1 NY penalty needs 90
    r = evaluate_execution_quality(
        db=FakeDB(), setup_score=86, rr=2.5, session_label="ny_kz",
        direction="BUY",
    )
    assert r["allow_execution"] is False


def test_aggregator_wide_spread_demotes():
    gates_mod._last_flip_at = None
    class FakeQ:
        def filter(self, *a, **k): return self
        def order_by(self, *a, **k): return self
        def first(self): return None
    class FakeDB:
        def query(self, model): return FakeQ()
    r = evaluate_execution_quality(
        db=FakeDB(), setup_score=92, rr=2.5, session_label="london_kz",
        direction="BUY", spread_pts=4.0,
    )
    assert r["should_demote"] is True
    assert r["allow_execution"] is False


def test_aggregator_settings_can_disable_gates():
    gates_mod._last_flip_at = None
    class FakeQ:
        def filter(self, *a, **k): return self
        def order_by(self, *a, **k): return self
        def first(self): return None
    class FakeDB:
        def query(self, model): return FakeQ()
    s = SimpleNamespace(
        min_setup_score_for_execution=50,      # loosen
        require_positive_ev=False,             # off
        session_penalty_enabled=False,
        post_loss_cooldown_min=0,
        direction_flip_cooldown_min=0,
        execution_tight_spread_pts=999,
        execution_hard_spread_pts=9999,
        news_proximity_lookahead_min=0,
    )
    r = evaluate_execution_quality(
        db=FakeDB(), setup_score=60, rr=1.5, session_label="ny_kz",
        direction="SELL", spread_pts=4.0, settings=s,
    )
    assert r["allow_execution"] is True


# ── Main ────────────────────────────────────────────────────────────────────

def main() -> int:
    print("=" * 78)
    print(" EXECUTION-GATES VALIDATION")
    print("=" * 78)

    _hr("1. Estimator + EV math")
    _run("estimator bands", test_estimator_bands)
    _run("positive_ev math", test_positive_ev_math)

    _hr("2. Q1 min setup score")
    _run("score gate", test_q1_score_gate)

    _hr("3. Q2 positive EV")
    _run("positive at 90+", test_q2_ev_positive)
    _run("negative below 85", test_q2_ev_negative_low_score)

    _hr("4. H1 session penalty")
    _run("NY KZ needs +5", test_h1_ny_kz_penalty)
    _run("late_NY hard-blocked", test_h1_late_ny_blocked)

    _hr("5. H2 post-loss cooldown")
    _run("no recent stop passes", test_h2_no_recent_stop_passes)
    _run("disabled by 0 passes", test_h2_cooldown_disabled)

    _hr("6. H3 direction-flip cooldown")
    _run("no flip recorded → pass", test_h3_no_flip_recorded)
    _run("flip within cooldown blocks", test_h3_flip_within_cooldown_blocks)
    _run("expired flip passes", test_h3_flip_expired_passes)
    _run("different direction passes", test_h3_different_direction_passes)

    _hr("7. H4 spread penalty")
    _run("clean spread", test_h4_clean_spread_passes)
    _run("penalty band blocks", test_h4_penalty_band_blocks)
    _run("hard max blocks", test_h4_hard_max_blocks)
    _run("None passes", test_h4_none_passes)

    _hr("8. H5 news proximity")
    _run("no upcoming news", test_h5_no_news_passes)

    _hr("9. Aggregator")
    _run("top-grade signal passes", test_aggregator_top_grade_signal_passes)
    _run("low score blocks", test_aggregator_low_score_blocks)
    _run("NY session gate", test_aggregator_ny_needs_higher_score)
    _run("wide spread demotes", test_aggregator_wide_spread_demotes)
    _run("settings can disable", test_aggregator_settings_can_disable_gates)

    print("\n" + "=" * 78)
    if PASSED == TOTAL:
        print(f" ALL PASS {PASSED}/{TOTAL} — gates ready")
        return 0
    print(f" FAIL {PASSED}/{TOTAL}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
