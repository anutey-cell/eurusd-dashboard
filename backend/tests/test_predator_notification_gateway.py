"""
Tests for the Predator Notification Gateway (P233).

Covers all 12 required cases from the notification-architecture spec plus
the acceptance-criterion regression fixture reproducing the user's real
noise pattern (14 events across a morning session, zero Telegrams expected
until an actionable FIRE qualifies).
"""
import os
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from typing import Optional
from unittest.mock import patch

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from database import Base
import db_models  # noqa: F401  ensure model classes register with Base metadata
from db_models import PredatorSetup, PredatorNotificationEvent
from services import predator_setup_registry as registry
from services import predator_notification_gateway as gateway


# ─────────────────────────────────────────────────────────────────────────────
# Test scaffolding
# ─────────────────────────────────────────────────────────────────────────────

@pytest.fixture
def db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine)
    s = SessionLocal()
    yield s
    s.close()


class FakeSettings:
    def __init__(self, mode="gateway", min_rr=1.2, bucket=5.0, max_bar_age=15):
        self.predator_notification_architecture = mode
        self.predator_notification_mode = "ACTIONABLE_ONLY"
        self.predator_notification_min_rr = min_rr
        self.predator_setup_price_bucket = bucket
        self.predator_notification_max_bar_age_min = max_bar_age


@dataclass
class FakeSignal:
    """Minimal predator signal for gateway tests."""
    state: str
    archetype: str = "PDL_BREAK"
    direction: str = "SELL"
    entry: float = 4430.0
    stop_loss: float = 4438.0
    tp1: float = 4410.0
    tp2: float = 4390.0
    rr: float = 2.5
    confidence: str = "HIGH"
    bar_time: Optional[datetime] = None
    overextended: bool = False

    def __post_init__(self):
        if self.bar_time is None:
            self.bar_time = datetime.now(timezone.utc)


def _msg_builder(sig, setup):
    return f"FIRE {setup.setup_id} entry={sig.entry:.2f}"


def _inv_builder(setup, reason):
    return f"INVALIDATED {setup.setup_id} — {reason}"


# ─────────────────────────────────────────────────────────────────────────────
# Must NOT send
# ─────────────────────────────────────────────────────────────────────────────

def test_1_approaching_level_no_send(db):
    """ARMED signal → gateway must not send."""
    sig = FakeSignal(state="ARMED", archetype="APPROACHING_LEVEL")
    with patch.object(gateway, "_send_via_telegram") as mock:
        r = gateway.route_signal(db, sig, key_level=4430.0,
                                  message_builder=_msg_builder,
                                  settings=FakeSettings())
        mock.assert_not_called()
    assert r["sent"] is False
    assert r["reason"] == "not_actionable"


def test_2_repeated_proximity_same_bucket(db):
    """10 ARMED evaluations at same bucket → 1 setup row, 0 sends."""
    sig = FakeSignal(state="ARMED", archetype="APPROACHING_LEVEL", entry=4430.0)
    with patch.object(gateway, "_send_via_telegram") as mock:
        for _ in range(10):
            gateway.route_signal(db, sig, key_level=4430.0,
                                  message_builder=_msg_builder,
                                  settings=FakeSettings())
        mock.assert_not_called()
    setups = db.query(PredatorSetup).all()
    assert len(setups) == 1
    assert setups[0].observation_count == 10


def test_3_developing_asian_low_shifts(db):
    """Level shifts within one 5pt bucket → still one setup, 0 sends."""
    with patch.object(gateway, "_send_via_telegram") as mock:
        for shift in (0.0, 0.5, 1.2, -0.8, 2.1):
            level = 4430.0 + shift
            sig = FakeSignal(state="ARMED", archetype="ASIAN_BREAKDOWN",
                              entry=level)
            gateway.route_signal(db, sig, key_level=level,
                                  message_builder=_msg_builder,
                                  settings=FakeSettings())
        mock.assert_not_called()
    assert db.query(PredatorSetup).count() == 1


def test_4_candidate_invalidated_never_actionable(db):
    """ARMED then invalidated with no prior FIRE Telegram → silent."""
    sig = FakeSignal(state="ARMED", archetype="ASIAN_BREAKDOWN")
    with patch.object(gateway, "_send_via_telegram") as mock:
        gateway.route_signal(db, sig, key_level=4430.0,
                              message_builder=_msg_builder,
                              settings=FakeSettings())
        setup_id = registry.setup_id_for(direction="SELL",
                                          archetype="ASIAN_BREAKDOWN",
                                          key_level=4430.0)
        r = gateway.route_invalidation(db, setup_id=setup_id,
                                        reason_text="test",
                                        message_builder=_inv_builder,
                                        settings=FakeSettings())
        mock.assert_not_called()
    assert r["sent"] is False
    assert r["reason"] == "never_actionable_invalidation"


def test_5_restart_preserves_notification_state(db):
    """Simulate restart by re-instantiating a session against the same DB;
    setup + notification_state must persist and prevent resend (gateway mode)."""
    engine = db.bind
    from sqlalchemy.orm import sessionmaker
    SessionLocal = sessionmaker(bind=engine)

    # Session A — send one actionable
    sess_a = SessionLocal()
    sig = FakeSignal(state="FIRE")
    with patch.object(gateway, "_send_via_telegram") as mock:
        gateway.route_signal(sess_a, sig, key_level=4430.0,
                              message_builder=_msg_builder,
                              settings=FakeSettings())
        assert mock.call_count == 1
    sess_a.commit()
    sess_a.close()

    # Session B (post-restart) — same signal must NOT resend
    sess_b = SessionLocal()
    with patch.object(gateway, "_send_via_telegram") as mock:
        r = gateway.route_signal(sess_b, sig, key_level=4430.0,
                                  message_builder=_msg_builder,
                                  settings=FakeSettings())
        mock.assert_not_called()
    assert r["reason"] == "duplicate_setup"


def test_shadow_never_advances_real_notification_state(db):
    """Shadow mode must NEVER touch the real notification_state column."""
    sig = FakeSignal(state="FIRE")
    with patch.object(gateway, "_send_via_telegram") as mock:
        r = gateway.route_signal(db, sig, key_level=4430.0,
                                  message_builder=_msg_builder,
                                  settings=FakeSettings(mode="shadow"))
        mock.assert_not_called()
    assert r["would_send"] is True
    setup = db.query(PredatorSetup).one()
    assert setup.notification_state == "NOT_SENT"           # untouched
    assert setup.shadow_notification_state == "ACTIONABLE_SENT"


def test_shadow_duplicate_uses_shadow_state_not_real_state(db):
    """Second FIRE in shadow reports duplicate_setup off shadow state."""
    sig = FakeSignal(state="FIRE")
    with patch.object(gateway, "_send_via_telegram"):
        r1 = gateway.route_signal(db, sig, key_level=4430.0,
                                    message_builder=_msg_builder,
                                    settings=FakeSettings(mode="shadow"))
        r2 = gateway.route_signal(db, sig, key_level=4432.0,
                                    message_builder=_msg_builder,
                                    settings=FakeSettings(mode="shadow"))
    assert r1["would_send"] is True
    assert r2["would_send"] is False
    assert r2["reason"] == "duplicate_setup"
    setup = db.query(PredatorSetup).one()
    assert setup.notification_state == "NOT_SENT"           # still untouched


def test_cutover_shadow_only_setup_is_treated_as_unsent_by_gateway(db):
    """
    ACCEPTANCE — the user-specified cutover regression.
    Setup A observed as FIRE while mode=SHADOW → WOULD_SEND recorded.
    Same setup observed again in SHADOW → WOULD_SUPPRESS duplicate_setup.
    Mode flips to GATEWAY.
    Same setup observed again → gateway SENDS (real notification_state was
    never touched by shadow, so the first real actionable qualification
    post-cutover is genuinely new to the user).
    """
    sig = FakeSignal(state="FIRE", entry=4430.0)

    # 1. FIRE while mode=SHADOW
    with patch.object(gateway, "_send_via_telegram") as mock:
        r1 = gateway.route_signal(db, sig, key_level=4430.0,
                                    message_builder=_msg_builder,
                                    settings=FakeSettings(mode="shadow"))
        mock.assert_not_called()
    assert r1["would_send"] is True
    assert r1["sent"] is False

    setup = db.query(PredatorSetup).one()
    assert setup.notification_state == "NOT_SENT"
    assert setup.shadow_notification_state == "ACTIONABLE_SENT"

    # 2. Same FIRE again in shadow → duplicate
    with patch.object(gateway, "_send_via_telegram") as mock:
        r2 = gateway.route_signal(db, sig, key_level=4431.0,
                                    message_builder=_msg_builder,
                                    settings=FakeSettings(mode="shadow"))
        mock.assert_not_called()
    assert r2["would_send"] is False
    assert r2["reason"] == "duplicate_setup"

    # 3. FLIP mode to gateway. Real state still NOT_SENT.
    setup = db.query(PredatorSetup).one()
    assert setup.notification_state == "NOT_SENT", \
        "shadow observations must NOT have advanced real state"

    # 4. Same FIRE now qualifies under real gateway — must SEND
    with patch.object(gateway, "_send_via_telegram") as mock:
        r3 = gateway.route_signal(db, sig, key_level=4430.5,
                                    message_builder=_msg_builder,
                                    settings=FakeSettings(mode="gateway"))
        mock.assert_called_once()
    assert r3["sent"] is True

    # 5. Real state advances now — first time
    setup = db.query(PredatorSetup).one()
    assert setup.notification_state == "ACTIONABLE_SENT"

    # 6. Same setup again in gateway → real duplicate now
    with patch.object(gateway, "_send_via_telegram") as mock:
        r4 = gateway.route_signal(db, sig, key_level=4431.0,
                                    message_builder=_msg_builder,
                                    settings=FakeSettings(mode="gateway"))
        mock.assert_not_called()
    assert r4["reason"] == "duplicate_setup"


def test_6_minor_confidence_shift_no_send(db):
    """Confidence changes on an ARMED signal must not create new sends."""
    with patch.object(gateway, "_send_via_telegram") as mock:
        for conf in ("MED", "HIGH", "MED", "HIGH", "MED"):
            sig = FakeSignal(state="ARMED", archetype="APPROACHING_LEVEL",
                              confidence=conf)
            gateway.route_signal(db, sig, key_level=4430.0,
                                  message_builder=_msg_builder,
                                  settings=FakeSettings())
        mock.assert_not_called()
    setups = db.query(PredatorSetup).all()
    assert len(setups) == 1
    assert setups[0].observation_count == 5


def test_7_M5_recalc_same_hypothesis(db):
    """12 M5 evaluations on same hypothesis → 1 setup, 0 sends."""
    now = datetime.now(timezone.utc)
    with patch.object(gateway, "_send_via_telegram") as mock:
        for i in range(12):
            sig = FakeSignal(state="ARMED", archetype="PDL_BREAK",
                              bar_time=now - timedelta(minutes=i * 5))
            gateway.route_signal(db, sig, key_level=4430.0,
                                  message_builder=_msg_builder,
                                  settings=FakeSettings())
        mock.assert_not_called()
    assert db.query(PredatorSetup).count() == 1


# ─────────────────────────────────────────────────────────────────────────────
# Must send
# ─────────────────────────────────────────────────────────────────────────────

def test_8_first_qualified_fire_sends(db):
    sig = FakeSignal(state="FIRE")
    with patch.object(gateway, "_send_via_telegram") as mock:
        r = gateway.route_signal(db, sig, key_level=4430.0,
                                  message_builder=_msg_builder,
                                  settings=FakeSettings())
        mock.assert_called_once()
    assert r["sent"] is True
    setup = db.query(PredatorSetup).one()
    assert setup.notification_state == "ACTIONABLE_SENT"


def test_9_invalidation_after_actionable_sends(db):
    """FIRE sent → INVALIDATION delivered."""
    sig = FakeSignal(state="FIRE")
    with patch.object(gateway, "_send_via_telegram") as mock:
        gateway.route_signal(db, sig, key_level=4430.0,
                              message_builder=_msg_builder,
                              settings=FakeSettings())
        setup_id = db.query(PredatorSetup).one().setup_id
        r = gateway.route_invalidation(db, setup_id=setup_id,
                                        reason_text="stop hit",
                                        message_builder=_inv_builder,
                                        settings=FakeSettings())
        assert mock.call_count == 2
    assert r["sent"] is True
    assert db.query(PredatorSetup).one().notification_state == "INVALIDATED_SENT"


def test_10_no_entry_no_send(db):
    sig = FakeSignal(state="FIRE", entry=0.0)
    with patch.object(gateway, "_send_via_telegram") as mock:
        r = gateway.route_signal(db, sig, key_level=4430.0,
                                  message_builder=_msg_builder,
                                  settings=FakeSettings())
        mock.assert_not_called()
    assert r["reason"] == "no_entry"


def test_11_rr_below_min_no_send(db):
    sig = FakeSignal(state="FIRE", rr=0.5)
    with patch.object(gateway, "_send_via_telegram") as mock:
        r = gateway.route_signal(db, sig, key_level=4430.0,
                                  message_builder=_msg_builder,
                                  settings=FakeSettings(min_rr=1.2))
        mock.assert_not_called()
    assert r["reason"] == "rr_below_min"


def test_12_stale_bar_no_send(db):
    old_bar = datetime.now(timezone.utc) - timedelta(minutes=45)
    sig = FakeSignal(state="FIRE", bar_time=old_bar)
    with patch.object(gateway, "_send_via_telegram") as mock:
        r = gateway.route_signal(db, sig, key_level=4430.0,
                                  message_builder=_msg_builder,
                                  settings=FakeSettings(max_bar_age=15))
        mock.assert_not_called()
    assert r["reason"] == "stale_data"


# ─────────────────────────────────────────────────────────────────────────────
# ACCEPTANCE regression — user's real noise pattern
# ─────────────────────────────────────────────────────────────────────────────

def test_acceptance_real_noise_pattern_zero_alerts(db):
    """
    Reproduces the user's actual noisy morning:
      14 candidate observations against the same Asian-low hypothesis,
      including invalidations that had no prior actionable send.
    Expected Telegram messages under new architecture: 0.
    """
    base = datetime(2026, 9, 1, 5, 30, tzinfo=timezone.utc)
    kl = 4430.0
    events = [
        ("ARMED", 0),   ("ARMED", 5),   ("INVALIDATED", 10),
        ("ARMED", 15),  ("INVALIDATED", 15), ("INVALIDATED", 25),
        ("ARMED", 56),  ("ARMED", 61),  ("INVALIDATED", 71),
        ("INVALIDATED", 76), ("ARMED", 100), ("ARMED", 115),
        ("INVALIDATED", 125), ("INVALIDATED", 130),
    ]
    with patch.object(gateway, "_send_via_telegram") as mock:
        for kind, off in events:
            when = base + timedelta(minutes=off)
            if kind == "ARMED":
                sig = FakeSignal(state="ARMED", archetype="ASIAN_BREAKDOWN",
                                  bar_time=when)
                gateway.route_signal(db, sig, key_level=kl,
                                      message_builder=_msg_builder,
                                      settings=FakeSettings())
            else:
                sid = registry.setup_id_for(direction="SELL",
                                             archetype="ASIAN_BREAKDOWN",
                                             key_level=kl, now_utc=when)
                gateway.route_invalidation(db, setup_id=sid,
                                            reason_text="stale window",
                                            message_builder=_inv_builder,
                                            settings=FakeSettings())
        assert mock.call_count == 0

    # Then one genuinely actionable FIRE later → 1 Telegram
    sig = FakeSignal(state="FIRE", archetype="ASIAN_BREAKDOWN",
                      bar_time=base + timedelta(minutes=125))
    with patch.object(gateway, "_send_via_telegram") as mock:
        r = gateway.route_signal(db, sig, key_level=kl,
                                  message_builder=_msg_builder,
                                  settings=FakeSettings())
        assert mock.call_count == 1
    assert r["sent"] is True


def test_metrics_endpoint_counts_correctly(db):
    """notification_metrics reports realistic numbers after a mixed run."""
    with patch.object(gateway, "_send_via_telegram"):
        for _ in range(5):
            gateway.route_signal(db, FakeSignal(state="ARMED"),
                                  key_level=4430.0,
                                  message_builder=_msg_builder,
                                  settings=FakeSettings())
        gateway.route_signal(db, FakeSignal(state="FIRE"),
                              key_level=4500.0,
                              message_builder=_msg_builder,
                              settings=FakeSettings())
    m = gateway.notification_metrics(db, hours=24)
    assert m["telegram_messages_sent"] == 1
    assert m["telegram_messages_suppressed"] >= 5
    assert m["setup_ids_created"] == 2
