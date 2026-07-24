"""Validate mandate_adapter.py end-to-end against the live DB."""
from __future__ import annotations

import os
import sys
from datetime import datetime, timezone, timedelta

try:
    import services.canonical_signal  # noqa
except ImportError:
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from services.canonical_signal import (
    STATE_MONITORING, STATE_ARMED, STATE_INVALIDATED,
    STRATEGY_MANDATE,
)
from services.signal_adapters.mandate_adapter import (
    mandate_verdict_to_signal, on_mandate_verdict,
)
from services.telegram_client import TelegramClient
from database import SessionLocal
from db_models import (
    Signal as SM, SignalStateTransition as ST, TelegramNotification as TN,
)


TOTAL, PASSED = 0, 0


def _hr(t):
    print("\n" + "─" * 78 + f"\n {t}\n" + "─" * 78)


def _run(name, fn):
    global TOTAL, PASSED
    TOTAL += 1
    try:
        fn()
        print(f"  ✓ {name}")
        PASSED += 1
    except AssertionError as e:
        print(f"  ✗ {name}: {e}")
    except Exception as e:
        print(f"  ✗ {name}: unexpected {type(e).__name__}: {e}")


class _FakeResp:
    def __init__(self, sc=200): self.status_code = sc; self.text = "ok"
    def json(self): return {"ok": self.status_code == 200,
                             "result": {"message_id": 1, "username": "test", "id": 42}}


class _FakeHTTP:
    def __init__(self): self.posts = []
    def post(self, url, json=None, timeout=None):
        self.posts.append(json); return _FakeResp(200)
    def get(self, url, timeout=None): return _FakeResp(200)


class _FakeSettings:
    telegram_bot_token = "T"; telegram_chat_id = "C"; telegram_alerts_enabled = True


def _cleanup(db):
    db.query(TN).delete(); db.query(ST).delete(); db.query(SM).delete(); db.commit()


def _make_verdict(decision="BUY", cp=4, entry=4020.0, sl=4014.0,
                  tp1=4028.0, tp2=4033.0, tp3=4040.0, rr=2.5,
                  session="London KZ", setup_score=78) -> dict:
    return {
        "decision":                 decision,
        "conditions_passed":        cp,
        "conditions": {
            "C1": {"label": "HTF alignment", "passed": cp >= 1},
            "C2": {"label": "Killzone", "passed": cp >= 2},
            "C3": {"label": "CISD sniper", "passed": cp >= 3},
            "C4": {"label": "Liquidity clean", "passed": cp >= 4},
            "C5": {"label": "Momentum burst", "passed": cp >= 5},
        },
        "estimated_win_rate_range": "55-65%",
        "execution_status":         "DEMO_TRADE_PLACED" if cp >= 5 else
                                    "SIGNAL_ONLY" if cp == 3 else "SIGNAL_ONLY",
        "execution_status_reason":  "Test",
        "setup_score":              setup_score,
        "market_sentiment":         "Bullish",
        "session_classification":   session,
        "trade_plan": {
            "entry":            entry,
            "entry_tolerance":  1.5,
            "stop_loss":        sl,
            "tp1":              tp1, "tp2": tp2, "tp3": tp3,
            "risk_reward":      rr,
            "invalidation":     f"Close M15 through {sl}",
        },
        "timeframe_alignment": {"alignment_summary": "STRONG_BULL"},
        "final_verdict":  f"{decision} · {cp}/5 · pullback into VAH",
        "stand_aside_reason": "" if decision != "STAND ASIDE" else "Setup below threshold",
    }


# ── Tests ────────────────────────────────────────────────────────────────────

def test_verdict_to_signal_basic():
    v = _make_verdict(cp=4)
    p = mandate_verdict_to_signal(v)
    assert p is not None
    assert p["direction"] == "BUY"
    assert p["strategy_id"] == STRATEGY_MANDATE
    assert p["entry_zone_low"] == 4018.5 and p["entry_zone_high"] == 4021.5
    assert p["tp1"] == 4028.0 and p["tp3"] == 4040.0
    assert p["_desired_state"] == STATE_ARMED
    assert p["session"] == "London KZ"
    assert p["confidence"] == 78


def test_verdict_to_signal_watchlist_3of5():
    v = _make_verdict(cp=3)
    p = mandate_verdict_to_signal(v)
    assert p["_desired_state"] == STATE_MONITORING


def test_verdict_to_signal_skipped_when_stand_aside():
    v = _make_verdict(decision="STAND ASIDE", cp=2)
    assert mandate_verdict_to_signal(v) is None


def test_verdict_to_signal_skipped_when_no_levels():
    v = _make_verdict(entry=None)
    assert mandate_verdict_to_signal(v) is None


def test_end_to_end_creates_monitoring_signal_in_dry_run():
    fake = _FakeHTTP()
    c = TelegramClient(_FakeSettings(), dry_run=True, session=fake)
    db = SessionLocal()
    try:
        _cleanup(db)
        v = _make_verdict(cp=3)
        r = on_mandate_verdict(db, v, client=c, force_dry_run=False)
        assert r["action"] in ("created", "transitioned"), f"unexpected action: {r}"
        assert r["state"] == STATE_MONITORING
        assert r["signal_id"].startswith("MDT-XAU-")
        assert r["notification"] is not None
        assert r["notification"]["result"] == "dry_run"
        assert len(fake.posts) == 0   # dry-run does not POST
        # Persisted row exists
        row = db.query(SM).filter(SM.signal_id == r["signal_id"]).one()
        assert row.state == STATE_MONITORING
        assert row.strategy_id == STRATEGY_MANDATE
    finally:
        _cleanup(db); db.close()


def test_end_to_end_creates_armed_signal_and_transitions_from_monitoring():
    fake = _FakeHTTP()
    c = TelegramClient(_FakeSettings(), dry_run=True, session=fake)
    db = SessionLocal()
    try:
        _cleanup(db)
        # First tick: 3/5 → MONITORING
        v3 = _make_verdict(cp=3, entry=4020.0, sl=4014.0)
        r1 = on_mandate_verdict(db, v3, client=c, force_dry_run=False)
        assert r1["state"] == STATE_MONITORING
        # Second tick: 4/5 with SAME zone → same fingerprint → transition to ARMED
        v4 = _make_verdict(cp=4, entry=4020.0, sl=4014.0)
        r2 = on_mandate_verdict(db, v4, client=c, force_dry_run=False)
        assert r2["signal_id"] == r1["signal_id"], "same setup should reuse row"
        assert r2["state"] == STATE_ARMED
        assert r2["action"] == "transitioned"
        # 2 transitions total: MONITORING then MONITORING→ARMED (plus initial DETECTED→MONITORING)
        transitions = db.query(ST).filter(ST.signal_id == r1["signal_id"]).all()
        # signal_created (DETECTED), DETECTED→MONITORING, MONITORING→ARMED
        assert len(transitions) >= 2
    finally:
        _cleanup(db); db.close()


def test_regression_to_stand_aside_invalidates_active_signal():
    fake = _FakeHTTP()
    c = TelegramClient(_FakeSettings(), dry_run=True, session=fake)
    db = SessionLocal()
    try:
        _cleanup(db)
        # Create ARMED signal
        v4 = _make_verdict(cp=4)
        r = on_mandate_verdict(db, v4, client=c, force_dry_run=False)
        assert r["state"] == STATE_ARMED
        # Regress: STAND ASIDE
        v_none = _make_verdict(decision="STAND ASIDE", cp=1)
        r2 = on_mandate_verdict(db, v_none, client=c, force_dry_run=False)
        assert r2["action"] == "invalidated"
        assert r2["count"] == 1
        # DB reflects INVALIDATED
        row = db.query(SM).filter(SM.signal_id == r["signal_id"]).one()
        assert row.state == STATE_INVALIDATED
    finally:
        _cleanup(db); db.close()


def test_idempotency_same_verdict_no_double_notification():
    fake = _FakeHTTP()
    c = TelegramClient(_FakeSettings(), dry_run=False, session=fake)
    db = SessionLocal()
    try:
        _cleanup(db)
        v = _make_verdict(cp=4)
        on_mandate_verdict(db, v, client=c, force_dry_run=False)
        on_mandate_verdict(db, v, client=c, force_dry_run=False)
        # Only one POST (second call = unchanged state = no new template render)
        assert len(fake.posts) == 1, f"expected 1 POST, got {len(fake.posts)}"
        # Only one notification row
        n_rows = db.query(TN).count()
        assert n_rows == 1
    finally:
        _cleanup(db); db.close()


def test_shadow_mode_suppresses_notification():
    fake = _FakeHTTP()
    c = TelegramClient(_FakeSettings(), dry_run=False, session=fake)
    db = SessionLocal()
    try:
        _cleanup(db)
        v = _make_verdict(cp=4)
        r = on_mandate_verdict(db, v, client=c, force_dry_run=True)
        assert r["notification"]["result"] == "suppressed"
        assert len(fake.posts) == 0
        n = db.query(TN).one()
        assert n.suppression_reason == "shadow_mode_dry_run"
    finally:
        _cleanup(db); db.close()


def test_no_raise_on_broken_verdict():
    fake = _FakeHTTP()
    c = TelegramClient(_FakeSettings(), dry_run=True, session=fake)
    db = SessionLocal()
    try:
        _cleanup(db)
        # None verdict
        r = on_mandate_verdict(db, None, client=c, force_dry_run=False)
        assert r["action"] == "skipped"
        # Verdict missing trade_plan
        r2 = on_mandate_verdict(db, {"decision": "BUY", "conditions_passed": 4},
                                 client=c, force_dry_run=False)
        assert r2["action"] == "skipped"
    finally:
        _cleanup(db); db.close()


def test_different_zone_creates_new_signal():
    fake = _FakeHTTP()
    c = TelegramClient(_FakeSettings(), dry_run=True, session=fake)
    db = SessionLocal()
    try:
        _cleanup(db)
        v1 = _make_verdict(cp=4, entry=4020.0)
        v2 = _make_verdict(cp=4, entry=4080.0)   # different bucket
        r1 = on_mandate_verdict(db, v1, client=c, force_dry_run=False)
        r2 = on_mandate_verdict(db, v2, client=c, force_dry_run=False)
        assert r1["signal_id"] != r2["signal_id"]
    finally:
        _cleanup(db); db.close()


# ── Main ─────────────────────────────────────────────────────────────────────

def main() -> int:
    print("=" * 78)
    print(" TELEGRAM NOTIFICATION P3 — MANDATE ADAPTER VALIDATION")
    print("=" * 78)

    _hr("1. Pure verdict → params conversion")
    _run("basic 4/5 verdict maps to ARMED params", test_verdict_to_signal_basic)
    _run("3/5 verdict maps to MONITORING", test_verdict_to_signal_watchlist_3of5)
    _run("STAND ASIDE returns None", test_verdict_to_signal_skipped_when_stand_aside)
    _run("missing entry/SL returns None", test_verdict_to_signal_skipped_when_no_levels)

    _hr("2. End-to-end pipeline")
    _run("3/5 creates MONITORING signal, dry-run notification stored",
         test_end_to_end_creates_monitoring_signal_in_dry_run)
    _run("upgrade 3/5 → 4/5 transitions same signal to ARMED",
         test_end_to_end_creates_armed_signal_and_transitions_from_monitoring)

    _hr("3. Regression handling")
    _run("STAND ASIDE after ARMED → invalidates prior signal",
         test_regression_to_stand_aside_invalidates_active_signal)

    _hr("4. Idempotency")
    _run("same verdict twice → single POST + single notification row",
         test_idempotency_same_verdict_no_double_notification)

    _hr("5. Shadow mode")
    _run("force_dry_run=True → suppression_reason set, no POST",
         test_shadow_mode_suppresses_notification)

    _hr("6. Robustness")
    _run("broken/None verdict does not raise", test_no_raise_on_broken_verdict)
    _run("different price zones create different signals",
         test_different_zone_creates_new_signal)

    print("\n" + "=" * 78)
    if PASSED == TOTAL:
        print(f" ALL PASS · {PASSED}/{TOTAL} · Mandate adapter ready for shadow deployment")
        return 0
    print(f" FAIL · {PASSED}/{TOTAL}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
