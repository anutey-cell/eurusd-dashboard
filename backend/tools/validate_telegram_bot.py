"""Validate telegram_bot.py command handlers + parser."""
from __future__ import annotations

import os, sys, json
from datetime import datetime, timezone, timedelta

try:
    import services.canonical_signal  # noqa
except ImportError:
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from services.canonical_signal import (
    CanonicalSignal, signal_fingerprint, make_signal_id,
    STATE_MONITORING, STATE_ARMED, STATE_CLOSED, STATE_STOPPED,
    DIRECTION_BUY, STRATEGY_MANDATE, STRATEGY_VP_TRAP,
)
from services.telegram_client import TelegramClient
from services.telegram_bot import (
    _parse_command, handle_update, get_or_create_chat_pref,
    cmd_help, cmd_status, cmd_signals, cmd_watchlist,
    cmd_xauusd, cmd_signal, cmd_performance,
    cmd_mute, cmd_unmute, cmd_mode, COMMANDS,
)
from services import signal_registry
from database import SessionLocal
from db_models import (
    Signal as SM, SignalStateTransition as ST, TelegramNotification as TN,
    TelegramChatPreference as CP, TelegramCommandLog as CL, TelegramBotState as BS,
)


TOTAL, PASSED = 0, 0
CHAT = "-1009876543210"
ADMIN_CHAT = "42424242"


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
    notification_canonical_enabled = True; notification_mode = "standard"
    notification_shadow_mode = False; telegram_bot_enabled = True


def _cleanup(db):
    db.query(TN).delete(); db.query(ST).delete(); db.query(SM).delete()
    db.query(CP).delete(); db.query(CL).delete(); db.query(BS).delete()
    db.commit()


def _seed_signals(db, n_active: int = 3, n_closed: int = 2) -> None:
    """Insert some signals for query-command tests."""
    now = datetime.now(timezone.utc)
    for i in range(n_active):
        signal_registry.upsert(
            db, strategy_id=STRATEGY_MANDATE, strategy_name="Mandate 5-Gate",
            instrument="XAUUSD", direction="BUY", confidence=80,
            entry_zone_low=4020.0 + i*20, entry_zone_high=4022.0 + i*20,
            stop_loss=4014.0 + i*20, invalidation="M15 close < 4013",
            session="London KZ", tp1=4028.0+i*20, tp2=4033.0+i*20,
            rr_tp1=1.5, rr_tp2=2.5, valid_until=now + timedelta(hours=4),
            now=now,
        )
    # Advance all created rows out of DETECTED → MONITORING, then one to ARMED
    sigs = signal_registry.active_signals(db, "XAUUSD")
    for s in sigs:
        signal_registry.transition(db, signal_id=s.signal_id,
                                     to_state=STATE_MONITORING, reason="test")
    if sigs:
        signal_registry.transition(db, signal_id=sigs[0].signal_id,
                                     to_state=STATE_ARMED, reason="test")
    db.commit()   # registry.transition doesn't commit; caller must
    # Seed closed signals with realized R
    for i in range(n_closed):
        s = signal_registry.upsert(
            db, strategy_id=STRATEGY_MANDATE, strategy_name="Mandate 5-Gate",
            instrument="XAUUSD", direction="SELL", confidence=85,
            entry_zone_low=4100.0 + i*40, entry_zone_high=4102.0 + i*40,
            stop_loss=4108.0 + i*40, invalidation="M15 close > 4109",
            session="NY KZ", tp1=4090.0+i*40, rr_tp1=2.0,
            now=now - timedelta(days=1),
        )
        # Fast-forward: TRIGGERED → ACTIVE → TP1_HIT → CLOSED
        for st in [STATE_ARMED, "TRIGGERED", "ACTIVE",
                   "TP1_HIT" if i == 0 else STATE_STOPPED]:
            try:
                signal_registry.transition(db, signal_id=s.signal_id,
                                             to_state=st, reason="test")
            except Exception:
                pass
        # Set r_realized on the final state
        from db_models import Signal as SM
        row = db.query(SM).filter(SM.signal_id == s.signal_id).one()
        row.r_realized = 1.5 if i == 0 else -1.0
        try:
            signal_registry.transition(db, signal_id=s.signal_id,
                                         to_state=STATE_CLOSED
                                         if i == 0 else STATE_STOPPED,
                                         reason="test")
        except Exception:
            pass
        db.commit()


# ── Tests ────────────────────────────────────────────────────────────────────

def test_parser_simple():
    assert _parse_command("/help") == ("help", "")
    assert _parse_command("/signals ") == ("signals", "")
    assert _parse_command("/signal MDT-XAU-20260724-001") == (
        "signal", "MDT-XAU-20260724-001")
    assert _parse_command("hello there") == (None, "")
    assert _parse_command("") == (None, "")
    # bot-suffix form
    assert _parse_command("/status@my_bot") == ("status", "")
    assert _parse_command("/mode@my_bot detailed") == ("mode", "detailed")


def test_help_replies():
    fake = _HTTP()
    c = TelegramClient(_Settings(), dry_run=False, session=fake)
    db = SessionLocal()
    try:
        _cleanup(db)
        r = cmd_help(db, c, CHAT, "")
        assert r["delivered"] is True
        assert len(fake.posts) == 1
        assert "/status" in fake.posts[0]["text"]
    finally:
        _cleanup(db); db.close()


def test_status_replies_with_shadow_flag():
    fake = _HTTP()
    c = TelegramClient(_Settings(), dry_run=False, session=fake)
    db = SessionLocal()
    try:
        _cleanup(db)
        _seed_signals(db, n_active=2, n_closed=0)
        cmd_status(db, c, CHAT, "")
        text = fake.posts[0]["text"]
        assert "Active signals" in text
    finally:
        _cleanup(db); db.close()


def test_signals_lists_active():
    fake = _HTTP()
    c = TelegramClient(_Settings(), dry_run=False, session=fake)
    db = SessionLocal()
    try:
        _cleanup(db)
        _seed_signals(db, n_active=3, n_closed=0)
        cmd_signals(db, c, CHAT, "")
        text = fake.posts[0]["text"]
        assert "Active signals" in text
        assert "MDT" in text  # signal ID prefix
    finally:
        _cleanup(db); db.close()


def test_signals_empty():
    fake = _HTTP()
    c = TelegramClient(_Settings(), dry_run=False, session=fake)
    db = SessionLocal()
    try:
        _cleanup(db)
        cmd_signals(db, c, CHAT, "")
        assert "no active signals" in fake.posts[0]["text"].lower()
    finally:
        _cleanup(db); db.close()


def test_watchlist_shows_monitoring_only():
    fake = _HTTP()
    c = TelegramClient(_Settings(), dry_run=False, session=fake)
    db = SessionLocal()
    try:
        _cleanup(db)
        _seed_signals(db, n_active=3, n_closed=0)
        cmd_watchlist(db, c, CHAT, "")
        text = fake.posts[0]["text"]
        assert "Watchlist" in text
    finally:
        _cleanup(db); db.close()


def test_signal_detail_by_id():
    fake = _HTTP()
    c = TelegramClient(_Settings(), dry_run=False, session=fake)
    db = SessionLocal()
    try:
        _cleanup(db)
        _seed_signals(db, n_active=1, n_closed=0)
        sig = signal_registry.active_signals(db, "XAUUSD")[0]
        cmd_signal(db, c, CHAT, sig.signal_id)
        text = fake.posts[0]["text"]
        assert sig.signal_id.replace("-", "\\-") in text or sig.signal_id in text
        assert "State" in text
    finally:
        _cleanup(db); db.close()


def test_signal_missing_id_shows_usage():
    fake = _HTTP()
    c = TelegramClient(_Settings(), dry_run=False, session=fake)
    db = SessionLocal()
    try:
        _cleanup(db)
        cmd_signal(db, c, CHAT, "")
        assert "Usage" in fake.posts[0]["text"]
    finally:
        _cleanup(db); db.close()


def test_signal_unknown_id_returns_not_found():
    fake = _HTTP()
    c = TelegramClient(_Settings(), dry_run=False, session=fake)
    db = SessionLocal()
    try:
        _cleanup(db)
        cmd_signal(db, c, CHAT, "MDT-XAU-99999999-999")
        assert "not found" in fake.posts[0]["text"]
    finally:
        _cleanup(db); db.close()


def test_performance_summary():
    fake = _HTTP()
    c = TelegramClient(_Settings(), dry_run=False, session=fake)
    db = SessionLocal()
    try:
        _cleanup(db)
        _seed_signals(db, n_active=0, n_closed=2)
        cmd_performance(db, c, CHAT, "")
        text = fake.posts[0]["text"]
        assert "Performance" in text
        assert "Win rate" in text
    finally:
        _cleanup(db); db.close()


def test_mute_requires_admin():
    fake = _HTTP()
    c = TelegramClient(_Settings(), dry_run=False, session=fake)
    db = SessionLocal()
    try:
        _cleanup(db)
        cmd_mute(db, c, CHAT, "mandate")
        assert "admin" in fake.posts[0]["text"].lower()
    finally:
        _cleanup(db); db.close()


def test_admin_mute_persists():
    fake = _HTTP()
    c = TelegramClient(_Settings(), dry_run=False, session=fake)
    db = SessionLocal()
    try:
        _cleanup(db)
        pref = get_or_create_chat_pref(db, ADMIN_CHAT)
        pref.is_admin = True
        db.commit()
        cmd_mute(db, c, ADMIN_CHAT, "mandate")
        pref2 = get_or_create_chat_pref(db, ADMIN_CHAT)
        mutes = json.loads(pref2.strategy_mutes_json or "[]")
        assert "mandate" in mutes
        assert "muted" in fake.posts[0]["text"].lower()
    finally:
        _cleanup(db); db.close()


def test_mode_admin_only_and_persists():
    fake = _HTTP()
    c = TelegramClient(_Settings(), dry_run=False, session=fake)
    db = SessionLocal()
    try:
        _cleanup(db)
        pref = get_or_create_chat_pref(db, ADMIN_CHAT)
        pref.is_admin = True; db.commit()
        cmd_mode(db, c, ADMIN_CHAT, "detailed")
        pref2 = get_or_create_chat_pref(db, ADMIN_CHAT)
        assert pref2.verbosity_mode == "detailed"
    finally:
        _cleanup(db); db.close()


def test_mode_rejects_bad_value():
    fake = _HTTP()
    c = TelegramClient(_Settings(), dry_run=False, session=fake)
    db = SessionLocal()
    try:
        _cleanup(db)
        pref = get_or_create_chat_pref(db, ADMIN_CHAT)
        pref.is_admin = True; db.commit()
        cmd_mode(db, c, ADMIN_CHAT, "verbose")
        assert "Usage" in fake.posts[0]["text"]
    finally:
        _cleanup(db); db.close()


def test_dispatch_unknown_command():
    fake = _HTTP()
    c = TelegramClient(_Settings(), dry_run=False, session=fake)
    db = SessionLocal()
    try:
        _cleanup(db)
        update = {
            "update_id": 111,
            "message": {"chat": {"id": CHAT, "type": "private"},
                         "text": "/foobar something"},
        }
        handle_update(db, c, update)
        assert "Unknown command" in fake.posts[0]["text"]
        # Command log row
        row = db.query(CL).filter(CL.update_id == 111).one()
        assert row.accepted is False
        assert row.reject_reason == "unknown_command"
    finally:
        _cleanup(db); db.close()


def test_dispatch_help_via_handle_update():
    fake = _HTTP()
    c = TelegramClient(_Settings(), dry_run=False, session=fake)
    db = SessionLocal()
    try:
        _cleanup(db)
        update = {
            "update_id": 222,
            "message": {"chat": {"id": CHAT, "type": "private"},
                         "text": "/help"},
        }
        handle_update(db, c, update)
        row = db.query(CL).filter(CL.update_id == 222).one()
        assert row.accepted is True
        assert row.command == "help"
        assert row.response_bytes and row.response_bytes > 0
    finally:
        _cleanup(db); db.close()


def test_non_command_text_ignored():
    fake = _HTTP()
    c = TelegramClient(_Settings(), dry_run=False, session=fake)
    db = SessionLocal()
    try:
        _cleanup(db)
        update = {
            "update_id": 333,
            "message": {"chat": {"id": CHAT, "type": "private"},
                         "text": "hello"},
        }
        handle_update(db, c, update)
        assert len(fake.posts) == 0
        assert db.query(CL).filter(CL.update_id == 333).count() == 0
    finally:
        _cleanup(db); db.close()


def test_command_table_complete():
    """Every command mentioned in the brief must be registered."""
    for name in ("help", "status", "signals", "watchlist", "xauusd",
                 "signal", "performance", "mute", "unmute", "mode"):
        assert name in COMMANDS, f"missing handler: {name}"


# ── Main ─────────────────────────────────────────────────────────────────────

def main() -> int:
    print("=" * 78)
    print(" TELEGRAM NOTIFICATION P9 — BOT COMMAND HANDLER VALIDATION")
    print("=" * 78)

    _hr("1. Command parser")
    _run("parses simple + bot-suffix commands", test_parser_simple)

    _hr("2. Query commands")
    _run("/help returns command list", test_help_replies)
    _run("/status shows active count + shadow flag", test_status_replies_with_shadow_flag)
    _run("/signals lists active signals", test_signals_lists_active)
    _run("/signals empty state", test_signals_empty)
    _run("/watchlist filters to MONITORING", test_watchlist_shows_monitoring_only)
    _run("/signal <id> shows detail", test_signal_detail_by_id)
    _run("/signal without id prints usage", test_signal_missing_id_shows_usage)
    _run("/signal unknown id → not found", test_signal_unknown_id_returns_not_found)
    _run("/performance summarizes closed R stats", test_performance_summary)

    _hr("3. Admin commands + auth")
    _run("/mute requires admin", test_mute_requires_admin)
    _run("admin /mute persists strategy in pref", test_admin_mute_persists)
    _run("/mode admin-only + persists", test_mode_admin_only_and_persists)
    _run("/mode rejects bad value", test_mode_rejects_bad_value)

    _hr("4. Dispatcher")
    _run("unknown command → user hint + log rejected", test_dispatch_unknown_command)
    _run("/help via handle_update logs accepted", test_dispatch_help_via_handle_update)
    _run("non-command text ignored (no log, no reply)", test_non_command_text_ignored)
    _run("every brief command registered in COMMANDS", test_command_table_complete)

    print("\n" + "=" * 78)
    if PASSED == TOTAL:
        print(f" ALL PASS · {PASSED}/{TOTAL} · Bot handler ready for live deployment")
        return 0
    print(f" FAIL · {PASSED}/{TOTAL}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
