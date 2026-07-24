"""Validate telegram_client.py — idempotency, dry-run, rate-limit, audit rows."""
from __future__ import annotations

import os
import sys
import time
from datetime import datetime, timezone

# Bootstrap path
try:
    import services.canonical_signal  # noqa: F401
except ImportError:
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from services.canonical_signal import (
    CanonicalSignal, signal_fingerprint, make_signal_id,
    STATE_ARMED, DIRECTION_BUY, STRATEGY_MANDATE,
)
from services.telegram_templates import render, MODE_STANDARD
from services.telegram_client import (
    TelegramClient, chat_id_hash, reset_client_for_test,
    RESULT_DRY_RUN, RESULT_DELIVERED, RESULT_FAILED, RESULT_SUPPRESSED,
)
from database import SessionLocal
from db_models import TelegramNotification as TN


TOTAL = 0
PASSED = 0


def _hr(title: str) -> None:
    print("\n" + "─" * 78 + f"\n {title}\n" + "─" * 78)


def _run(name: str, fn) -> None:
    global TOTAL, PASSED
    TOTAL += 1
    try:
        fn()
        print(f"  ✓ {name}")
        PASSED += 1
    except AssertionError as exc:
        print(f"  ✗ {name}: {exc}")
    except Exception as exc:
        print(f"  ✗ {name}: unexpected {type(exc).__name__}: {exc}")


# ── Fake settings + fake HTTP session ────────────────────────────────────────

class _FakeSettings:
    telegram_bot_token       = "TEST-TOKEN-abc123"
    telegram_chat_id         = "-1001234567890"
    telegram_alerts_enabled  = True


class _FakeResp:
    def __init__(self, status_code: int, body: dict = None):
        self.status_code = status_code
        self._body = body or {"ok": status_code == 200, "result": {"message_id": 12345}}
        self.text = str(self._body)

    def json(self):
        return self._body


class _FakeHTTP:
    """Records outbound HTTP; can be programmed to return specific responses."""
    def __init__(self, responses=None):
        self.calls = []
        self._responses = responses or [_FakeResp(200)]
        self._i = 0

    def post(self, url, json=None, timeout=None):
        self.calls.append({"url": url, "body": dict(json or {}), "at": time.monotonic()})
        r = self._responses[min(self._i, len(self._responses) - 1)]
        self._i += 1
        return r

    def get(self, url, timeout=None):
        self.calls.append({"url": url, "get": True})
        return _FakeResp(200, {"ok": True, "result": {"username": "test_bot", "id": 42}})


def _make_signal(seq: int = 1, direction: str = DIRECTION_BUY,
                 strategy: str = STRATEGY_MANDATE) -> CanonicalSignal:
    now = datetime.now(timezone.utc)
    fp = signal_fingerprint(
        instrument="XAUUSD", direction=direction, strategy_id=strategy,
        entry_zone_low=4018.0 + seq * 15,  # spread out so fingerprints differ
        entry_zone_high=4020.0 + seq * 15,
        stop_loss=4014.0 + seq * 15,
        session="London KZ", created_at=now,
    )
    return CanonicalSignal(
        signal_id=make_signal_id(strategy, seq, "XAU", now),
        fingerprint=fp, strategy_id=strategy, strategy_name="Mandate 5-Gate",
        instrument="XAUUSD", direction=direction, confidence=82,
        entry_zone_low=4018.0 + seq * 15, entry_zone_high=4020.0 + seq * 15,
        stop_loss=4014.0 + seq * 15, current_stop=4014.0 + seq * 15,
        invalidation="Close M15 below 4012.5",
        tp1=4025.0 + seq * 15, tp2=4030.0 + seq * 15,
        rr_tp1=1.5, rr_tp2=2.5, session="London KZ",
        state=STATE_ARMED, created_at=now,
    )


def _cleanup(db) -> None:
    """Wipe test-run rows."""
    db.query(TN).delete()
    db.commit()


# ── Tests ────────────────────────────────────────────────────────────────────

def test_chat_id_hash_is_deterministic():
    a = chat_id_hash("-1001234567890")
    b = chat_id_hash("-1001234567890")
    c = chat_id_hash("-1001234567891")
    assert a == b and len(a) == 16, f"unstable: {a} vs {b}"
    assert a != c, "different IDs collided"
    assert chat_id_hash("") == ""


def test_dry_run_auto_when_alerts_disabled():
    class S: telegram_bot_token = "x"; telegram_chat_id = "y"; telegram_alerts_enabled = False
    c = TelegramClient(S(), session=_FakeHTTP())
    assert c.dry_run is True


def test_dry_run_auto_when_no_token():
    class S: telegram_bot_token = ""; telegram_chat_id = "y"; telegram_alerts_enabled = True
    c = TelegramClient(S(), session=_FakeHTTP())
    assert c.dry_run is True


def test_dry_run_explicit_override():
    fake = _FakeHTTP()
    c = TelegramClient(_FakeSettings(), dry_run=True, session=fake)
    assert c.dry_run is True
    # It should NOT actually POST
    db = SessionLocal()
    try:
        _cleanup(db)
        sig = _make_signal(seq=1)
        p = render("actionable", sig, mode=MODE_STANDARD)
        res = c.send_notification(db, signal_id=sig.signal_id, strategy_id=sig.strategy_id,
                                   from_state="MONITORING", to_state="ARMED", payload=p)
        assert res["delivered"] is False
        assert res["result"] == RESULT_DRY_RUN
        # No HTTP POST should have happened
        posts = [x for x in fake.calls if not x.get("get")]
        assert len(posts) == 0, f"dry-run posted {len(posts)} messages"
        # But the audit row must exist
        row = db.query(TN).filter(TN.message_fingerprint == p["message_fingerprint"]).one()
        assert row.delivery_result == RESULT_DRY_RUN
        assert row.chat_id_hash == chat_id_hash(_FakeSettings.telegram_chat_id)
    finally:
        _cleanup(db)
        db.close()


def test_live_mode_actually_posts():
    fake = _FakeHTTP()
    c = TelegramClient(_FakeSettings(), dry_run=False, session=fake)
    assert c.dry_run is False
    db = SessionLocal()
    try:
        _cleanup(db)
        sig = _make_signal(seq=2)
        p = render("actionable", sig, mode=MODE_STANDARD)
        res = c.send_notification(db, signal_id=sig.signal_id, strategy_id=sig.strategy_id,
                                   from_state="MONITORING", to_state="ARMED", payload=p)
        assert res["delivered"] is True, f"expected delivered, got {res}"
        assert res["result"] == RESULT_DELIVERED
        posts = [x for x in fake.calls if not x.get("get")]
        assert len(posts) == 1
        body = posts[0]["body"]
        assert body["parse_mode"] == "MarkdownV2"
        assert body["disable_web_page_preview"] is True
        assert body["chat_id"] == _FakeSettings.telegram_chat_id
        assert "ACTIONABLE SETUP" in body["text"]
    finally:
        _cleanup(db)
        db.close()


def test_idempotent_by_message_fingerprint():
    fake = _FakeHTTP()
    c = TelegramClient(_FakeSettings(), dry_run=False, session=fake)
    db = SessionLocal()
    try:
        _cleanup(db)
        sig = _make_signal(seq=3)
        p = render("actionable", sig, mode=MODE_STANDARD)
        r1 = c.send_notification(db, signal_id=sig.signal_id, strategy_id=sig.strategy_id,
                                  from_state="MONITORING", to_state="ARMED", payload=p)
        r2 = c.send_notification(db, signal_id=sig.signal_id, strategy_id=sig.strategy_id,
                                  from_state="MONITORING", to_state="ARMED", payload=p)
        assert r1["delivered"] is True
        assert r2["delivered"] is True   # already-delivered rows are reported as delivered
        assert r2.get("reason") == "duplicate_fingerprint"
        posts = [x for x in fake.calls if not x.get("get")]
        assert len(posts) == 1, f"idempotent send hit HTTP twice: {len(posts)}"
        # Only one audit row
        rows = db.query(TN).filter(TN.message_fingerprint == p["message_fingerprint"]).all()
        assert len(rows) == 1
    finally:
        _cleanup(db)
        db.close()


def test_suppression_records_reason():
    fake = _FakeHTTP()
    c = TelegramClient(_FakeSettings(), dry_run=False, session=fake)
    db = SessionLocal()
    try:
        _cleanup(db)
        sig = _make_signal(seq=4)
        p = render("actionable", sig, mode=MODE_STANDARD)
        res = c.send_notification(db, signal_id=sig.signal_id, strategy_id=sig.strategy_id,
                                   from_state="MONITORING", to_state="ARMED", payload=p,
                                   suppression_reason="user_muted_strategy")
        assert res["result"] == RESULT_SUPPRESSED
        assert res["delivered"] is False
        posts = [x for x in fake.calls if not x.get("get")]
        assert len(posts) == 0, "suppressed message should not POST"
        row = db.query(TN).filter(TN.message_fingerprint == p["message_fingerprint"]).one()
        assert row.suppression_reason == "user_muted_strategy"
        assert row.delivery_result == RESULT_SUPPRESSED
    finally:
        _cleanup(db)
        db.close()


def test_http_failure_records_error():
    fake = _FakeHTTP(responses=[_FakeResp(400, {"ok": False, "description": "Bad Request: chat not found"})])
    c = TelegramClient(_FakeSettings(), dry_run=False, session=fake)
    db = SessionLocal()
    try:
        _cleanup(db)
        sig = _make_signal(seq=5)
        p = render("actionable", sig, mode=MODE_STANDARD)
        res = c.send_notification(db, signal_id=sig.signal_id, strategy_id=sig.strategy_id,
                                   from_state="MONITORING", to_state="ARMED", payload=p)
        assert res["delivered"] is False
        assert res["result"] == RESULT_FAILED
        assert "400" in (res.get("error") or "")
        row = db.query(TN).filter(TN.message_fingerprint == p["message_fingerprint"]).one()
        assert row.delivery_result == RESULT_FAILED
        assert row.error_message and "400" in row.error_message
    finally:
        _cleanup(db)
        db.close()


def test_health_check_ok():
    fake = _FakeHTTP()
    c = TelegramClient(_FakeSettings(), dry_run=False, session=fake)
    h = c.health_check()
    assert h["ok"] is True
    assert h["username"] == "test_bot"


def test_health_check_when_dry_run():
    c = TelegramClient(_FakeSettings(), dry_run=True, session=_FakeHTTP())
    h = c.health_check()
    assert h["ok"] is False
    assert h["reason"] == "dry_run_or_no_token"


def test_rate_limit_serializes_within_chat():
    """Two rapid sends to same chat must be spaced ≥ ~1s apart."""
    fake = _FakeHTTP(responses=[_FakeResp(200), _FakeResp(200)])
    c = TelegramClient(_FakeSettings(), dry_run=False, session=fake)
    db = SessionLocal()
    try:
        _cleanup(db)
        sig1 = _make_signal(seq=6)
        sig2 = _make_signal(seq=7)
        p1 = render("actionable", sig1, mode=MODE_STANDARD)
        p2 = render("actionable", sig2, mode=MODE_STANDARD)
        c.send_notification(db, signal_id=sig1.signal_id, strategy_id=sig1.strategy_id,
                            from_state="MONITORING", to_state="ARMED", payload=p1)
        t0 = time.monotonic()
        c.send_notification(db, signal_id=sig2.signal_id, strategy_id=sig2.strategy_id,
                            from_state="MONITORING", to_state="ARMED", payload=p2)
        t1 = time.monotonic()
        elapsed = t1 - t0
        assert elapsed >= 0.9, f"second send too fast: {elapsed:.2f}s (expected ≥ 1s)"
    finally:
        _cleanup(db)
        db.close()


# ── Main ─────────────────────────────────────────────────────────────────────

def main() -> int:
    print("=" * 78)
    print(" TELEGRAM NOTIFICATION P2 — CLIENT VALIDATION")
    print("=" * 78)

    _hr("1. chat_id_hash")
    _run("deterministic + masks empty", test_chat_id_hash_is_deterministic)

    _hr("2. Dry-run resolution")
    _run("auto dry-run when alerts disabled", test_dry_run_auto_when_alerts_disabled)
    _run("auto dry-run when token missing", test_dry_run_auto_when_no_token)
    _run("dry-run does NOT POST; audit row persisted", test_dry_run_explicit_override)

    _hr("3. Live send path")
    _run("live mode actually POSTs to Telegram", test_live_mode_actually_posts)

    _hr("4. Idempotency")
    _run("same fingerprint → single POST + single row", test_idempotent_by_message_fingerprint)

    _hr("5. Suppression")
    _run("suppression_reason blocks POST + records reason", test_suppression_records_reason)

    _hr("6. HTTP failure handling")
    _run("400 response → RESULT_FAILED + error stored", test_http_failure_records_error)

    _hr("7. Health check")
    _run("getMe ok when live", test_health_check_ok)
    _run("health returns dry_run reason when dry-run", test_health_check_when_dry_run)

    _hr("8. Rate limiting")
    _run("two sends to same chat serialized ≥ ~1s", test_rate_limit_serializes_within_chat)

    print("\n" + "=" * 78)
    if PASSED == TOTAL:
        print(f" ALL PASS · {PASSED}/{TOTAL} · Client ready for P3 (mandate adapter)")
        return 0
    print(f" FAIL · {PASSED}/{TOTAL}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
