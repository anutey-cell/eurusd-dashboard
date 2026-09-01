"""
Predator persistent-setup registry.

A "setup" is a persistent market hypothesis identified by a deterministic
composite key that does NOT include bar_time. Many observations (repeated
M5 evaluations, developing-level shifts, proximity retests) collapse into
ONE setup row so the notification gateway can dedup cleanly and the
trader is never interrupted twice for the same hypothesis.

Public surface:
    setup_id_for(...)              → deterministic string
    upsert_observation(...)        → PredatorSetup (creates or updates)
    mark_notification(...)         → record a Telegram send in state + history
    record_suppression(...)        → append-only diagnostic log
    was_notified(setup, msg_type)  → bool

The registry never sends Telegram itself. That is the gateway's job.
"""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy.orm import Session

from db_models import PredatorSetup

log = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Identity
# ─────────────────────────────────────────────────────────────────────────────

_REF_LEVEL_TYPE_BY_ARCHETYPE = {
    "ASIAN_BREAKDOWN":    "ASIAN_LOW",
    "PDL_BREAK":          "PDL",
    "VOL_CONTINUATION":   "COMPOSITE",
    "APPROACHING_LEVEL":  "COMPOSITE",
}


def _classify_session(hour_utc: int) -> str:
    """Match predator_execution_manager.create_batch's convention for parity."""
    if 0 <= hour_utc < 7:      return "ASIA"
    if 7 <= hour_utc < 12:     return "LONDON"
    if 12 <= hour_utc < 16:    return "NY_OPEN"
    if 16 <= hour_utc < 22:    return "NY_PM"
    return "ROLLOVER"


def _trading_date(now_utc: Optional[datetime] = None) -> str:
    """Rollover boundary is 22:00 UTC (matches session convention)."""
    now_utc = now_utc or datetime.now(timezone.utc)
    if now_utc.hour < 22:
        d = now_utc.date()
    else:
        # roll to next trading day
        from datetime import timedelta
        d = (now_utc + timedelta(days=1)).date()
    return d.isoformat()


def setup_id_for(
    *,
    direction: str,
    archetype: str,
    key_level: Optional[float],
    bucket_pts: float = 5.0,
    now_utc: Optional[datetime] = None,
    strategy: str = "PREDATOR",
    instrument: str = "XAUUSD",
) -> str:
    """
    Deterministic setup identity. Same hypothesis → same string, regardless of
    bar_time, price fluctuations within bucket, or restart.

    Format:
        {strategy}-{instrument}-{direction}-{archetype}-{ref_type}-{bucket}-{session}-{date}
    """
    now_utc = now_utc or datetime.now(timezone.utc)
    ref_type = _REF_LEVEL_TYPE_BY_ARCHETYPE.get(archetype, "COMPOSITE")
    if key_level is None:
        bucket = 0.0
    else:
        bucket = round(float(key_level) / bucket_pts) * bucket_pts
    session = _classify_session(now_utc.hour)
    tdate   = _trading_date(now_utc)
    return (f"{strategy}-{instrument}-{direction}-{archetype}-{ref_type}-"
            f"{bucket:.0f}-{session}-{tdate.replace('-','')}")


# ─────────────────────────────────────────────────────────────────────────────
# Persistence
# ─────────────────────────────────────────────────────────────────────────────

def upsert_observation(
    db: Session,
    *,
    setup_id: str,
    direction: str,
    archetype: str,
    key_level: Optional[float],
    bucket_pts: float,
    internal_state: str,
    latest_price: Optional[float] = None,
    latest_distance: Optional[float] = None,
    latest_confidence: Optional[str] = None,
    latest_regime: Optional[str] = None,
    latest_score: Optional[int] = None,
    last_evaluated_bar: Optional[datetime] = None,
    now_utc: Optional[datetime] = None,
) -> PredatorSetup:
    """
    Create the setup row if new, otherwise bump observation counters and
    refresh the latest-snapshot fields. Internal state may advance
    (DETECTED → CANDIDATE → ARMED → FIRE) but this alone never sends Telegram.
    """
    now_utc = now_utc or datetime.now(timezone.utc)
    setup = db.query(PredatorSetup).filter(
        PredatorSetup.setup_id == setup_id
    ).first()

    if setup is None:
        session = _classify_session(now_utc.hour)
        tdate   = _trading_date(now_utc)
        ref_type = _REF_LEVEL_TYPE_BY_ARCHETYPE.get(archetype, "COMPOSITE")
        bucket = 0.0 if key_level is None else round(float(key_level) / bucket_pts) * bucket_pts
        setup = PredatorSetup(
            setup_id=setup_id,
            direction=direction,
            archetype=archetype,
            reference_level_type=ref_type,
            normalized_price_bucket=bucket,
            session=session,
            trading_date=tdate,
            internal_state=internal_state,
            notification_state="NOT_SENT",
            observation_count=1,
            first_seen_at=now_utc,
            last_seen_at=now_utc,
            last_evaluated_bar=last_evaluated_bar,
            latest_price=latest_price,
            latest_distance=latest_distance,
            latest_confidence=latest_confidence,
            latest_regime=latest_regime,
            latest_score=latest_score,
            notifications_sent="[]",
            suppressions="[]",
        )
        db.add(setup)
        db.flush()
        log.debug("[predator/registry] new setup %s state=%s", setup_id, internal_state)
    else:
        setup.observation_count = (setup.observation_count or 0) + 1
        setup.last_seen_at = now_utc
        if last_evaluated_bar is not None:
            setup.last_evaluated_bar = last_evaluated_bar
        if latest_price is not None:      setup.latest_price = latest_price
        if latest_distance is not None:   setup.latest_distance = latest_distance
        if latest_confidence is not None: setup.latest_confidence = latest_confidence
        if latest_regime is not None:     setup.latest_regime = latest_regime
        if latest_score is not None:      setup.latest_score = latest_score
        # Advance internal_state only monotonically toward FIRE (never regress on
        # a per-tick basis). Terminal states (INVALIDATED, EXPIRED) may only be
        # set by explicit calls to invalidate_setup / expire_setup.
        _order = {"DETECTED": 0, "CANDIDATE": 1, "APPROACHING": 2, "ARMED": 3,
                   "CONFIRMING": 4, "FIRE": 5}
        cur = _order.get(setup.internal_state, 0)
        nxt = _order.get(internal_state, cur)
        if nxt > cur:
            setup.internal_state = internal_state

    return setup


def _append_json_list(field_value: Optional[str], entry: dict, max_len: int = 50) -> str:
    """Safely append to a JSON-list Text column, capping length."""
    try:
        arr = json.loads(field_value or "[]")
        if not isinstance(arr, list):
            arr = []
    except Exception:
        arr = []
    arr.append(entry)
    if len(arr) > max_len:
        arr = arr[-max_len:]
    return json.dumps(arr, default=str)


def mark_notification(
    db: Session,
    setup: PredatorSetup,
    *,
    new_state: str,
    msg_type: str,
    message_hash: str,
    opportunity_id: Optional[str] = None,
    now_utc: Optional[datetime] = None,
) -> None:
    """Record that a Telegram message was actually sent for this setup."""
    now_utc = now_utc or datetime.now(timezone.utc)
    setup.notification_state = new_state
    setup.notifications_sent = _append_json_list(
        setup.notifications_sent,
        {
            "msg_type": msg_type,
            "sent_at": now_utc.isoformat(),
            "message_hash": message_hash,
            "opportunity_id": opportunity_id,
            "state_after": new_state,
        },
    )


def record_suppression(
    db: Session,
    setup: PredatorSetup,
    *,
    reason: str,
    msg_type: Optional[str] = None,
    now_utc: Optional[datetime] = None,
) -> None:
    """Append to diagnostic suppression log."""
    now_utc = now_utc or datetime.now(timezone.utc)
    setup.suppressions = _append_json_list(
        setup.suppressions,
        {"reason": reason, "msg_type": msg_type, "at": now_utc.isoformat()},
    )


def was_notified(setup: PredatorSetup, msg_type: str) -> bool:
    """True if a Telegram of the given msg_type was previously sent for this setup."""
    try:
        arr = json.loads(setup.notifications_sent or "[]")
    except Exception:
        return False
    return any((e.get("msg_type") == msg_type) for e in arr if isinstance(e, dict))
