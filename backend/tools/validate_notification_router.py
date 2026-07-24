"""Validate notification_router.py — thresholds, mute, quiet hours, modes."""
from __future__ import annotations

import os, sys
from datetime import datetime, timezone, timedelta

try:
    import services.canonical_signal  # noqa
except ImportError:
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from services.canonical_signal import (
    CanonicalSignal, signal_fingerprint, make_signal_id,
    STATE_MONITORING, STATE_ARMED, STATE_ACTIVE, STATE_STOPPED,
    STATE_DETECTED,
    DIRECTION_BUY, STRATEGY_MANDATE, STRATEGY_VP_TRAP,
)
from services.notification_router import route
from services.telegram_client import TelegramClient
from database import SessionLocal
from db_models import (
    Signal as SM, SignalStateTransition as ST, TelegramNotification as TN,
)


TOTAL, PASSED = 0, 0


def _hr(t): print("\n" + "─"*78 + f"\n {t}\n" + "─"*78)
def _run(name, fn):
    global TOTAL, PASSED
    TOTAL += 1
    try: fn(); print(f"  ✓ {name}"); PASSED += 1
    except AssertionError as e: print(f"  ✗ {name}: {e}")
    except Exception as e: print(f"  ✗ {name}: {type(e).__name__}: {e}")


class _R:
    def __init__(self, sc=200): self.status_code = sc; self.text = "ok"
    def json(self): return {"ok": True, "result": {"message_id": 1, "username": "b", "id": 1}}


class _HTTP:
    def __init__(self): self.posts = []
    def post(self, url, json=None, timeout=None):
        self.posts.append(json); return _R()
    def get(self, url, timeout=None): return _R()


class _Settings:
    telegram_bot_token = "T"; telegram_chat_id = "C"; telegram_alerts_enabled = True
    notification_canonical_enabled = True
    notification_mode = "standard"
    # Optional per-strategy overrides can be attached ad-hoc via setattr


def _mk_signal(strategy=STRATEGY_MANDATE, confidence=82, state=STATE_ARMED,
                seq=100) -> CanonicalSignal:
    now = datetime.now(timezone.utc)
    fp = signal_fingerprint(
        instrument="XAUUSD", direction=DIRECTION_BUY, strategy_id=strategy,
        entry_zone_low=4018+seq, entry_zone_high=4020+seq,
        stop_loss=4014+seq, session="London KZ", created_at=now,
    )
    return CanonicalSignal(
        signal_id=make_signal_id(strategy, seq, "XAU", now),
        fingerprint=fp, strategy_id=strategy, strategy_name="Test",
        instrument="XAUUSD", direction=DIRECTION_BUY, confidence=confidence,
        entry_zone_low=4018.0+seq, entry_zone_high=4020.0+seq,
        stop_loss=4014.0+seq, current_stop=4014.0+seq,
        invalidation="Close below 4012.5",
        tp1=4025.0+seq, tp2=4030.0+seq, rr_tp1=1.5, rr_tp2=2.5,
        session="London KZ", state=state, created_at=now,
    )


def _cleanup(db):
    db.query(TN).delete(); db.query(ST).delete(); db.query(SM).delete(); db.commit()


# ── Tests ────────────────────────────────────────────────────────────────────

def test_silent_transition_returns_none():
    fake = _HTTP()
    c = TelegramClient(_Settings(), dry_run=True, session=fake)
    db = SessionLocal()
    try:
        _cleanup(db)
        sig = _mk_signal(state=STATE_ACTIVE, seq=1)
        # TRIGGERED → ACTIVE is marked silent
        r = route(db, sig, "TRIGGERED", "ACTIVE", client=c, settings_override=_Settings())
        assert r is None
    finally:
        _cleanup(db); db.close()


def test_below_threshold_suppresses():
    fake = _HTTP()
    c = TelegramClient(_Settings(), dry_run=False, session=fake)
    db = SessionLocal()
    try:
        _cleanup(db)
        sig = _mk_signal(state=STATE_ARMED, confidence=50, seq=2)   # below actionable 80
        r = route(db, sig, STATE_MONITORING, STATE_ARMED, client=c,
                   settings_override=_Settings())
        assert r["result"] == "suppressed"
        assert "below_threshold" in (r.get("reason") or "")
        assert len(fake.posts) == 0
    finally:
        _cleanup(db); db.close()


def test_above_threshold_delivers():
    fake = _HTTP()
    c = TelegramClient(_Settings(), dry_run=False, session=fake)
    db = SessionLocal()
    try:
        _cleanup(db)
        sig = _mk_signal(state=STATE_ARMED, confidence=85, seq=3)
        r = route(db, sig, STATE_MONITORING, STATE_ARMED, client=c,
                   settings_override=_Settings())
        assert r["result"] == "delivered"
        assert len(fake.posts) == 1
    finally:
        _cleanup(db); db.close()


def test_monitoring_lower_threshold():
    fake = _HTTP()
    c = TelegramClient(_Settings(), dry_run=False, session=fake)
    db = SessionLocal()
    try:
        _cleanup(db)
        sig = _mk_signal(state=STATE_MONITORING, confidence=70, seq=4)  # 70 >= 65
        r = route(db, sig, STATE_DETECTED, STATE_MONITORING, client=c,
                   settings_override=_Settings())
        assert r["result"] == "delivered", f"expected delivered, got {r}"
    finally:
        _cleanup(db); db.close()


def test_stop_hit_bypasses_threshold():
    fake = _HTTP()
    c = TelegramClient(_Settings(), dry_run=False, session=fake)
    db = SessionLocal()
    try:
        _cleanup(db)
        # Even low-confidence signal must emit stop-hit
        sig = _mk_signal(state=STATE_STOPPED, confidence=30, seq=5)
        r = route(db, sig, STATE_ACTIVE, STATE_STOPPED, client=c,
                   settings_override=_Settings())
        assert r["result"] == "delivered"
        assert len(fake.posts) == 1
    finally:
        _cleanup(db); db.close()


def test_muted_strategy_suppressed():
    fake = _HTTP()
    c = TelegramClient(_Settings(), dry_run=False, session=fake)
    s = _Settings()
    setattr(s, f"notification_mute_{STRATEGY_MANDATE}", True)
    db = SessionLocal()
    try:
        _cleanup(db)
        sig = _mk_signal(state=STATE_ARMED, confidence=95, seq=6)
        r = route(db, sig, STATE_MONITORING, STATE_ARMED, client=c,
                   settings_override=s)
        assert r["result"] == "suppressed"
        assert "strategy_muted" in (r.get("reason") or "")
        assert len(fake.posts) == 0
    finally:
        _cleanup(db); db.close()


def test_canonical_disabled_globally():
    fake = _HTTP()
    c = TelegramClient(_Settings(), dry_run=False, session=fake)
    s = _Settings(); s.notification_canonical_enabled = False
    db = SessionLocal()
    try:
        _cleanup(db)
        sig = _mk_signal(state=STATE_ARMED, confidence=95, seq=7)
        r = route(db, sig, STATE_MONITORING, STATE_ARMED, client=c,
                   settings_override=s)
        assert r["result"] == "suppressed"
        assert "canonical_layer_disabled" in (r.get("reason") or "")
    finally:
        _cleanup(db); db.close()


def test_quiet_hours_suppresses_non_critical():
    fake = _HTTP()
    c = TelegramClient(_Settings(), dry_run=False, session=fake)
    db = SessionLocal()
    try:
        _cleanup(db)
        # 01:00 UTC = 04:00 EAT — inside quiet window
        quiet_now = datetime(2026, 7, 24, 1, 0, 0, tzinfo=timezone.utc)
        sig = _mk_signal(state=STATE_ARMED, confidence=95, seq=8)
        r = route(db, sig, STATE_MONITORING, STATE_ARMED, client=c,
                   settings_override=_Settings(), now=quiet_now)
        assert r["result"] == "suppressed"
        assert "quiet_hours" in (r.get("reason") or "")
    finally:
        _cleanup(db); db.close()


def test_quiet_hours_stop_hit_still_fires():
    fake = _HTTP()
    c = TelegramClient(_Settings(), dry_run=False, session=fake)
    db = SessionLocal()
    try:
        _cleanup(db)
        quiet_now = datetime(2026, 7, 24, 1, 0, 0, tzinfo=timezone.utc)
        sig = _mk_signal(state=STATE_STOPPED, confidence=95, seq=9)
        r = route(db, sig, STATE_ACTIVE, STATE_STOPPED, client=c,
                   settings_override=_Settings(), now=quiet_now)
        assert r["result"] == "delivered", f"stop_hit should fire in quiet hours: {r}"
    finally:
        _cleanup(db); db.close()


def test_force_dry_run_overrides_delivery():
    fake = _HTTP()
    c = TelegramClient(_Settings(), dry_run=False, session=fake)
    db = SessionLocal()
    try:
        _cleanup(db)
        sig = _mk_signal(state=STATE_ARMED, confidence=95, seq=10)
        r = route(db, sig, STATE_MONITORING, STATE_ARMED, client=c,
                   force_dry_run=True, settings_override=_Settings())
        assert r["result"] == "suppressed"
        assert r.get("reason") in (None, "shadow_mode_dry_run") or "shadow" in str(r.get("reason"))
        assert len(fake.posts) == 0
    finally:
        _cleanup(db); db.close()


def test_per_strategy_mode_override():
    """Custom mode setting takes effect end-to-end (bytes differ)."""
    fake = _HTTP()
    c = TelegramClient(_Settings(), dry_run=False, session=fake)
    s_min = _Settings(); s_min.notification_mode = "minimal"
    s_det = _Settings(); s_det.notification_mode = "detailed"
    db = SessionLocal()
    try:
        _cleanup(db)
        sig = _mk_signal(state=STATE_ARMED, confidence=95, seq=11)
        r_min = route(db, sig, STATE_MONITORING, STATE_ARMED, client=c,
                       settings_override=s_min)
        _cleanup(db)
        sig = _mk_signal(state=STATE_ARMED, confidence=95, seq=12)
        r_det = route(db, sig, STATE_MONITORING, STATE_ARMED, client=c,
                       settings_override=s_det)
        min_bytes = fake.posts[0]["text"].__len__()
        det_bytes = fake.posts[1]["text"].__len__()
        assert min_bytes < det_bytes, f"minimal({min_bytes}) should be < detailed({det_bytes})"
    finally:
        _cleanup(db); db.close()


def test_persistent_audit_on_suppressed():
    """Even suppressed messages must leave an audit row."""
    fake = _HTTP()
    c = TelegramClient(_Settings(), dry_run=False, session=fake)
    db = SessionLocal()
    try:
        _cleanup(db)
        sig = _mk_signal(state=STATE_ARMED, confidence=40, seq=13)  # below threshold
        route(db, sig, STATE_MONITORING, STATE_ARMED, client=c,
               settings_override=_Settings())
        n_rows = db.query(TN).filter(TN.delivery_result == "suppressed").count()
        assert n_rows == 1
    finally:
        _cleanup(db); db.close()


# ── Main ─────────────────────────────────────────────────────────────────────

def main() -> int:
    print("=" * 78)
    print(" TELEGRAM NOTIFICATION P4 — ROUTER VALIDATION")
    print("=" * 78)

    _hr("1. Silent transitions")
    _run("TRIGGERED → ACTIVE returns None", test_silent_transition_returns_none)

    _hr("2. Score thresholds")
    _run("below actionable threshold → suppressed", test_below_threshold_suppresses)
    _run("above actionable threshold → delivered", test_above_threshold_delivers)
    _run("monitoring uses lower threshold (65)", test_monitoring_lower_threshold)
    _run("stop_hit bypasses threshold (always send)", test_stop_hit_bypasses_threshold)

    _hr("3. Mute rules")
    _run("per-strategy mute suppresses", test_muted_strategy_suppressed)
    _run("global canonical disable suppresses all", test_canonical_disabled_globally)

    _hr("4. Quiet hours (EAT 00:00 – 05:00)")
    _run("non-critical in quiet hours suppressed", test_quiet_hours_suppresses_non_critical)
    _run("stop_hit still fires in quiet hours", test_quiet_hours_stop_hit_still_fires)

    _hr("5. Shadow mode overlay")
    _run("force_dry_run=True short-circuits delivery", test_force_dry_run_overrides_delivery)

    _hr("6. Mode selection")
    _run("per-tick mode setting sizes messages differently", test_per_strategy_mode_override)

    _hr("7. Audit trail")
    _run("suppressed calls still persist a TelegramNotification row",
         test_persistent_audit_on_suppressed)

    print("\n" + "=" * 78)
    if PASSED == TOTAL:
        print(f" ALL PASS · {PASSED}/{TOTAL} · Router ready for adapter integration")
        return 0
    print(f" FAIL · {PASSED}/{TOTAL}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
