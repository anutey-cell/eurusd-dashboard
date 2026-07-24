"""
Validate the Telegram P1 foundation — CanonicalSignal + Registry.

Exercises:
  1. CanonicalSignal construction + validation
  2. Fingerprint determinism + bucketing
  3. Registry upsert idempotency
  4. State machine transitions (valid + invalid)
  5. Sequence numbering + signal_id format
  6. Expiry sweep
  7. Read helpers

Uses an in-container SQLite DB by default (whatever the app is using).
Run:
  ssh doxau 'docker exec -e PYTHONPATH=/app xauusd-backend python /app/tools/validate_signal_registry.py'
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone


def _hdr(msg: str) -> None:
    print()
    print("─" * 78)
    print(" " + msg)
    print("─" * 78)


def test_canonical_signal_basic():
    _hdr("1. CanonicalSignal construction")
    from services.canonical_signal import (
        CanonicalSignal, STATE_DETECTED, DIRECTION_BUY, STRATEGY_MANDATE,
    )
    sig = CanonicalSignal(
        signal_id="TEST-XAU-20260723-001",
        fingerprint="abc123def456",
        strategy_id=STRATEGY_MANDATE,
        strategy_name="Mandate Strategist",
        instrument="XAUUSD",
        direction=DIRECTION_BUY,
        confidence=82,
        entry_zone_low=4010.0,
        entry_zone_high=4012.0,
        stop_loss=4005.0,
        current_stop=4005.0,
        invalidation="5-minute close below 4005.00",
        state=STATE_DETECTED,
        created_at=datetime.now(timezone.utc),
    )
    assert sig.entry_midpoint() == 4011.0
    assert sig.risk_points() == 6.0
    assert sig.is_pre_entry() is True
    assert sig.is_terminal() is False
    print(f"  entry_midpoint={sig.entry_midpoint()}  risk_points={sig.risk_points()}")
    print("  ✓ constructed OK · invariants pass")


def test_canonical_signal_invalid():
    _hdr("2. CanonicalSignal rejects invalid inputs")
    from services.canonical_signal import CanonicalSignal, DIRECTION_BUY

    tests = [
        ("bad state",
         dict(state="INVALID_STATE")),
        ("bad direction",
         dict(direction="MAYBE")),
        ("bad confidence high",
         dict(confidence=150)),
        ("bad confidence low",
         dict(confidence=-1)),
        ("inverted entry zone",
         dict(entry_zone_low=4020.0, entry_zone_high=4010.0)),
        ("naive datetime",
         dict(created_at=datetime.now())),  # no tzinfo
    ]
    base = dict(
        signal_id="X", fingerprint="X", strategy_id="mandate",
        strategy_name="X", instrument="XAUUSD", direction=DIRECTION_BUY,
        confidence=50, entry_zone_low=4010.0, entry_zone_high=4012.0,
        stop_loss=4005.0, current_stop=4005.0, invalidation="X",
        state="DETECTED",
        created_at=datetime.now(timezone.utc),
    )
    for name, override in tests:
        args = dict(base, **override)
        try:
            CanonicalSignal(**args)
            print(f"  ✗ {name} did NOT raise — BUG")
            sys.exit(1)
        except (ValueError, TypeError):
            print(f"  ✓ {name} rejected")


def test_fingerprint_determinism():
    _hdr("3. Fingerprint determinism + bucketing")
    from services.canonical_signal import signal_fingerprint

    base = dict(
        instrument="XAUUSD", direction="SELL", strategy_id="vp_trap",
        entry_zone_low=4020.0, entry_zone_high=4022.0,
        stop_loss=4028.0, session="london_kz",
        created_at=datetime(2026, 7, 23, 8, 30, tzinfo=timezone.utc),
    )
    fp1 = signal_fingerprint(**base)
    fp2 = signal_fingerprint(**base)
    assert fp1 == fp2, "same inputs must give same fingerprint"
    print(f"  Deterministic: {fp1} == {fp2}")

    # 2pt drift in entry should NOT change fingerprint (5pt buckets)
    drifted = dict(base, entry_zone_low=4021.0, entry_zone_high=4023.0)
    fp3 = signal_fingerprint(**drifted)
    assert fp1 == fp3, f"2pt drift changed fingerprint: {fp1} != {fp3}"
    print(f"  2pt drift ignored: {fp3}")

    # 10pt drift SHOULD change fingerprint
    big_drift = dict(base, entry_zone_low=4030.0, entry_zone_high=4032.0)
    fp4 = signal_fingerprint(**big_drift)
    assert fp1 != fp4, f"10pt drift did NOT change fingerprint: {fp1} == {fp4}"
    print(f"  10pt drift → different fingerprint: {fp4}")

    # Different direction → different fingerprint
    buy = dict(base, direction="BUY")
    fp5 = signal_fingerprint(**buy)
    assert fp1 != fp5
    print(f"  Different direction → different fingerprint: {fp5}")


def test_transitions():
    _hdr("4. Transition rules")
    from services.canonical_signal import (
        is_valid_transition, PERMITTED_TRANSITIONS,
        STATE_DETECTED, STATE_MONITORING, STATE_ARMED,
        STATE_TRIGGERED, STATE_ACTIVE, STATE_TP1_HIT,
        STATE_BREAKEVEN, STATE_STOPPED, STATE_INVALIDATED,
        STATE_EXPIRED, STATE_CLOSED,
    )
    # Valid
    valid = [
        (STATE_DETECTED, STATE_MONITORING),
        (STATE_MONITORING, STATE_ARMED),
        (STATE_ARMED, STATE_TRIGGERED),
        (STATE_TRIGGERED, STATE_ACTIVE),
        (STATE_ACTIVE, STATE_TP1_HIT),
        (STATE_TP1_HIT, STATE_BREAKEVEN),
        (STATE_ACTIVE, STATE_STOPPED),
        (STATE_MONITORING, STATE_INVALIDATED),
        (STATE_ARMED, STATE_EXPIRED),
    ]
    for f, t in valid:
        assert is_valid_transition(f, t), f"expected valid: {f} → {t}"
    print(f"  ✓ {len(valid)} valid transitions pass")

    # Invalid
    invalid = [
        (STATE_DETECTED, STATE_TP1_HIT),        # skipping states
        (STATE_STOPPED, STATE_ACTIVE),          # from terminal
        (STATE_CLOSED, STATE_TRAILING := "TRAILING"),
        (STATE_INVALIDATED, STATE_ARMED),       # from terminal
        (STATE_EXPIRED, STATE_MONITORING),
    ]
    for f, t in invalid:
        assert not is_valid_transition(f, t), f"expected INVALID: {f} → {t}"
    print(f"  ✓ {len(invalid)} invalid transitions rejected")


def test_message_types():
    _hdr("5. State-to-message mapping")
    from services.canonical_signal import message_type_for
    cases = [
        (("DETECTED", "MONITORING"),  "monitoring"),
        (("MONITORING", "ARMED"),     "actionable"),
        (("ARMED", "TRIGGERED"),      "entry_triggered"),
        (("ACTIVE", "TP1_HIT"),       "tp1_hit"),
        (("TP1_HIT", "BREAKEVEN"),    "breakeven"),
        (("TP2_HIT", "TP3_HIT"),      "final_target"),
        (("ACTIVE", "STOPPED"),       "stop_hit"),
        (("ARMED", "INVALIDATED"),    "invalidated"),
        (("ARMED", "EXPIRED"),        "expired"),
        # Silent transitions
        (("TRIGGERED", "ACTIVE"),     None),
        (("DETECTED", "EXPIRED"),     None),
        (("MONITORING", "EXPIRED"),   None),
    ]
    for (f, t), expected in cases:
        actual = message_type_for(f, t)
        assert actual == expected, f"{f}→{t}: expected {expected!r} got {actual!r}"
        marker = "silent" if expected is None else expected
        print(f"  ✓ {f:<12} → {t:<12}  → {marker}")


def test_registry_upsert_and_transition():
    _hdr("6. Registry upsert + transition (against live DB)")
    from database import SessionLocal
    from services import signal_registry as reg
    from services.canonical_signal import (
        STATE_DETECTED, STATE_MONITORING, STATE_ARMED, STATE_TRIGGERED,
    )
    with SessionLocal() as db:
        now = datetime.now(timezone.utc)
        sig1 = reg.upsert(
            db,
            strategy_id="mandate", strategy_name="Mandate",
            instrument="XAUUSD", direction="SELL", confidence=68,
            entry_zone_low=4020.0, entry_zone_high=4022.0,
            stop_loss=4028.0,
            invalidation="5-min close above 4028",
            session="london_kz",
            valid_until=now + timedelta(hours=4),
            initial_state=STATE_DETECTED,
            now=now,
        )
        print(f"  Created signal: {sig1.signal_id} (fp {sig1.fingerprint})")
        assert sig1.state == STATE_DETECTED

        # Upsert same fingerprint → same signal (idempotent)
        sig2 = reg.upsert(
            db,
            strategy_id="mandate", strategy_name="Mandate",
            instrument="XAUUSD", direction="SELL", confidence=75,  # score changed
            entry_zone_low=4020.5, entry_zone_high=4022.5,          # < 5pt drift
            stop_loss=4028.0,
            invalidation="5-min close above 4028",
            session="london_kz",
            valid_until=now + timedelta(hours=4),
            now=now,
        )
        assert sig1.signal_id == sig2.signal_id, "idempotent upsert failed"
        assert sig2.confidence == 75, "confidence didn't update"
        assert sig2.state == STATE_DETECTED, "state must remain — upsert doesn't transition"
        print(f"  Idempotent upsert: same signal_id, confidence updated 68 → 75")

        # Transition through valid path
        reg.transition(db, signal_id=sig1.signal_id, to_state=STATE_MONITORING,
                       reason="score crossed 65")
        s = reg.get_by_signal_id(db, sig1.signal_id)
        assert s.state == STATE_MONITORING
        assert s.previous_state == STATE_DETECTED
        print(f"  → MONITORING ✓")

        reg.transition(db, signal_id=sig1.signal_id, to_state=STATE_ARMED,
                       reason="all mandatory conditions met")
        s = reg.get_by_signal_id(db, sig1.signal_id)
        assert s.state == STATE_ARMED
        print(f"  → ARMED ✓")

        # Invalid transition should raise
        try:
            reg.transition(db, signal_id=sig1.signal_id, to_state="TP1_HIT")
            print(f"  ✗ ARMED → TP1_HIT did NOT raise (BUG)")
            sys.exit(1)
        except ValueError as e:
            print(f"  ✓ ARMED → TP1_HIT rejected: {e}")

        # Idempotent same-state transition
        s_before = reg.get_by_signal_id(db, sig1.signal_id)
        reg.transition(db, signal_id=sig1.signal_id, to_state=STATE_ARMED)
        s_after = reg.get_by_signal_id(db, sig1.signal_id)
        assert s_before.state == s_after.state
        print(f"  ✓ same-state transition is a no-op")

        db.commit()


def test_sequence_numbering():
    _hdr("7. Sequence numbering per (strategy, date)")
    from database import SessionLocal
    from services import signal_registry as reg
    from datetime import datetime, timezone
    with SessionLocal() as db:
        now = datetime.now(timezone.utc)
        ids = []
        for i in range(3):
            # Different fingerprints so they're distinct signals
            s = reg.upsert(
                db,
                strategy_id="vp_trap", strategy_name="VP Trap",
                instrument="XAUUSD", direction="BUY", confidence=70,
                entry_zone_low=3990.0 + i*10,   # different levels
                entry_zone_high=3992.0 + i*10,
                stop_loss=3985.0 + i*10,
                invalidation="X",
                session="asian",
                now=now,
            )
            ids.append(s.signal_id)
            print(f"  {s.signal_id}")
        # Sequence numbers should be increasing within the day
        seq_nums = [int(s.split("-")[-1]) for s in ids]
        assert seq_nums == sorted(seq_nums), f"non-monotonic: {seq_nums}"
        assert len(set(seq_nums)) == len(seq_nums), f"duplicate seq: {seq_nums}"
        print(f"  ✓ sequence: {seq_nums}")
        db.commit()


def test_expiry_sweep():
    _hdr("8. Expiry sweep")
    from database import SessionLocal
    from services import signal_registry as reg
    from services.canonical_signal import STATE_ARMED, STATE_EXPIRED
    with SessionLocal() as db:
        # Create a signal that's already past its valid_until
        past = datetime.now(timezone.utc) - timedelta(minutes=5)
        # Use unique price so a fresh fingerprint
        sig = reg.upsert(
            db,
            strategy_id="momentum", strategy_name="Momentum",
            instrument="XAUUSD", direction="SELL", confidence=82,
            entry_zone_low=3900.0, entry_zone_high=3902.0,
            stop_loss=3910.0,
            invalidation="X",
            session="ny_kz",
            valid_until=past,
            initial_state=STATE_ARMED,
            now=past - timedelta(minutes=30),
        )
        print(f"  Created ARMED signal past expiry: {sig.signal_id}")

        expired_list = reg.sweep_expired(db)
        assert sig.signal_id in expired_list, "sweep did not expire the signal"
        s = reg.get_by_signal_id(db, sig.signal_id)
        assert s.state == STATE_EXPIRED
        assert s.closed_at is not None
        print(f"  ✓ swept → EXPIRED at {s.closed_at.isoformat()}")
        db.commit()


def test_read_helpers():
    _hdr("9. Read helpers")
    from database import SessionLocal
    from services import signal_registry as reg
    with SessionLocal() as db:
        active = reg.active_signals(db)
        monitored = reg.monitored_signals(db)
        transitions = reg.recent_transitions(db, limit=5)
        print(f"  active signals (non-terminal):  {len(active)}")
        print(f"  monitored signals (MONITORING): {len(monitored)}")
        print(f"  recent transitions:              {len(transitions)}")
        for t in transitions:
            f = t.get("from_state") or "—"
            print(f"    {t['at'][:19]}  {t['signal_id']}  {f:<12} → {t['to_state']}")


def main():
    print("=" * 78)
    print(" TELEGRAM NOTIFICATION P1 — VALIDATION")
    print("=" * 78)

    test_canonical_signal_basic()
    test_canonical_signal_invalid()
    test_fingerprint_determinism()
    test_transitions()
    test_message_types()
    test_registry_upsert_and_transition()
    test_sequence_numbering()
    test_expiry_sweep()
    test_read_helpers()

    print()
    print("=" * 78)
    print(" ALL PASS · Foundation ready for P2 (templates + client)")
    print("=" * 78)


if __name__ == "__main__":
    main()
