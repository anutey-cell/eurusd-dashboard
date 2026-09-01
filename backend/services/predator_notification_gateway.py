"""
Predator Notification Gateway.

Sole path from internal PREDATOR strategy events to Telegram. Enforces:

  * ACTIONABLE_ONLY default — internal ARMED/CANDIDATE/APPROACHING states
    never reach Telegram. Only qualifying FIREs do.
  * NOT_SENT precondition — a setup that has never been notified as
    actionable cannot emit lifecycle events (INVALIDATED / EXPIRED /
    STOPPED etc.) to Telegram.
  * Persistent deduplication by setup_id + msg_type — same setup + msg
    combination cannot be sent twice, even across container restarts.
  * Fail-closed actionability gate — missing entry / stop / target /
    RR / confirmation / freshness → silent.
  * Architecture mode switch (legacy | shadow | gateway) governs behaviour:
      - legacy  → gateway does nothing, existing direct-send paths run
      - shadow  → gateway records what it WOULD do; existing paths still send
      - gateway → gateway is the only sender; direct-send paths become no-op

Every decision (SENT / SUPPRESSED / WOULD_SEND / WOULD_SUPPRESS) is appended
to predator_notification_events for the diagnostics endpoint.
"""

from __future__ import annotations

import hashlib
import logging
from datetime import datetime, timezone, timedelta
from typing import Optional, Callable, Any

from sqlalchemy.orm import Session

from db_models import PredatorSetup, PredatorNotificationEvent
from services import predator_setup_registry as registry

log = logging.getLogger(__name__)


# Notification states that permit lifecycle sends (INVALIDATED, TP, SL, CLOSED)
_LIFECYCLE_ELIGIBLE_STATES = frozenset({
    "ACTIONABLE_SENT", "ENTRY_SENT",
    "TP1_SENT", "TP2_SENT", "TP3_SENT",
    "BREAKEVEN_SENT", "TRAILING_SENT",
})


def _hash_message(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8", "ignore")).hexdigest()[:16]


def _persist_event(
    db: Session, *,
    setup_id: str, mode: str, decision: str, reason: Optional[str],
    msg_type: Optional[str], internal_state: Optional[str],
    notification_state: Optional[str], message_hash: Optional[str],
) -> None:
    try:
        db.add(PredatorNotificationEvent(
            setup_id=setup_id, architecture_mode=mode, decision=decision,
            reason=reason, msg_type=msg_type,
            internal_state=internal_state,
            notification_state=notification_state,
            message_hash=message_hash,
        ))
    except Exception as exc:
        log.debug("[predator/gateway] event persist failed: %s", exc)


def _send_via_telegram(text: str) -> None:
    """Late import to avoid circular dependency; also lets tests stub."""
    from services.strategist_runner import _send_plain
    _send_plain(text)


# ─────────────────────────────────────────────────────────────────────────────
# Actionability gate — fail-closed
# ─────────────────────────────────────────────────────────────────────────────

def check_actionability(
    signal: Any, setup: PredatorSetup, *,
    effective_notification_state: str = "NOT_SENT",
    min_rr: float = 1.2,
    max_bar_age_min: int = 15,
    now_utc: Optional[datetime] = None,
) -> tuple[bool, str]:
    """
    Returns (eligible, reason). If eligible is False, reason names the
    first failing check. Ordering is stable so diagnostics can trend.

    The caller passes the STATE THIS MODE OWNS:
      - gateway mode → setup.notification_state (real deliveries)
      - shadow mode  → setup.shadow_notification_state (projected)
    This keeps the gate mode-agnostic; the caller decides which column
    represents "already sent" for the current mode.
    """
    if effective_notification_state != "NOT_SENT":
        return False, "duplicate_setup"

    if getattr(signal, "state", None) != "FIRE":
        return False, "not_actionable"

    entry = getattr(signal, "entry", None)
    stop  = getattr(signal, "stop_loss", None)
    tp1   = getattr(signal, "tp1", None)
    if not entry:  return False, "no_entry"
    if not stop:   return False, "no_stop"
    if not tp1:    return False, "no_target"

    rr = getattr(signal, "rr", 0.0) or 0.0
    if rr < min_rr:
        return False, "rr_below_min"

    # Data-freshness — last M5 bar must be recent
    now_utc = now_utc or datetime.now(timezone.utc)
    bar_t = getattr(signal, "bar_time", None)
    if bar_t is not None:
        try:
            if isinstance(bar_t, str):
                bar_dt = datetime.fromisoformat(bar_t.replace("Z", ""))
            else:
                bar_dt = bar_t
            if bar_dt.tzinfo is None:
                bar_dt = bar_dt.replace(tzinfo=timezone.utc)
            age_min = (now_utc - bar_dt).total_seconds() / 60.0
            if age_min > max_bar_age_min:
                return False, "stale_data"
        except Exception:
            return False, "stale_data"

    # Overextension — populated by extension filter upstream
    if getattr(signal, "overextended", False):
        return False, "overextended"

    return True, "ok"


# ─────────────────────────────────────────────────────────────────────────────
# Main entry points
# ─────────────────────────────────────────────────────────────────────────────

def route_signal(
    db: Session,
    signal: Any,
    *,
    key_level: Optional[float],
    message_builder: Callable[[Any, PredatorSetup], str],
    opportunity_id: Optional[str] = None,
    settings=None,
) -> dict:
    """
    Called once per PREDATOR signal (both ARMED and FIRE). Upserts the setup,
    then runs the actionability gate, then decides send/suppress per mode.

    Returns a dict describing the decision — the scheduler logs this but
    should never take action based on it (the gateway performs the send itself
    when mode == "gateway").
    """
    if settings is None:
        from config import settings as _settings
        settings = _settings

    mode           = getattr(settings, "predator_notification_architecture", "shadow")
    min_rr         = float(getattr(settings, "predator_notification_min_rr", 1.2))
    bucket_pts     = float(getattr(settings, "predator_setup_price_bucket", 5.0))
    max_bar_age    = int(getattr(settings, "predator_notification_max_bar_age_min", 15))
    now_utc        = datetime.now(timezone.utc)

    setup_id = registry.setup_id_for(
        direction=getattr(signal, "direction", "SELL"),
        archetype=getattr(signal, "archetype", "APPROACHING_LEVEL"),
        key_level=key_level,
        bucket_pts=bucket_pts,
        now_utc=now_utc,
    )

    # Parse bar_time for the "last evaluated bar" snapshot
    _last_bar = None
    _bt = getattr(signal, "bar_time", None)
    if _bt:
        try:
            _last_bar = (_bt if isinstance(_bt, datetime)
                         else datetime.fromisoformat(str(_bt).replace("Z", "")))
        except Exception:
            _last_bar = None

    setup = registry.upsert_observation(
        db,
        setup_id=setup_id,
        direction=getattr(signal, "direction", "SELL"),
        archetype=getattr(signal, "archetype", "APPROACHING_LEVEL"),
        key_level=key_level,
        bucket_pts=bucket_pts,
        internal_state=("FIRE" if getattr(signal, "state", None) == "FIRE"
                        else "ARMED"),
        latest_price=getattr(signal, "entry", None),
        latest_confidence=getattr(signal, "confidence", None),
        last_evaluated_bar=_last_bar,
        now_utc=now_utc,
    )
    if opportunity_id:
        setup.linked_opportunity_id = opportunity_id

    # Pick the state column this mode owns. Shadow observations must NEVER
    # be treated as real deliveries — that is the whole point of shadow mode.
    if mode == "shadow":
        effective_state = setup.shadow_notification_state or "NOT_SENT"
    else:
        effective_state = setup.notification_state or "NOT_SENT"

    eligible, reason = check_actionability(
        signal, setup,
        effective_notification_state=effective_state,
        min_rr=min_rr, max_bar_age_min=max_bar_age, now_utc=now_utc,
    )

    result = {
        "setup_id": setup_id, "mode": mode,
        "eligible": eligible, "reason": reason,
        "sent": False, "would_send": eligible,
    }

    # Legacy mode → gateway is inert; existing code paths still send.
    if mode == "legacy":
        _persist_event(db, setup_id=setup_id, mode=mode,
                       decision="LEGACY_PASSTHROUGH", reason=None,
                       msg_type="ACTIONABLE",
                       internal_state=setup.internal_state,
                       notification_state=setup.notification_state,
                       message_hash=None)
        db.flush()
        return result

    # Shadow mode → gateway records the projected decision but does not send.
    # Advances shadow_notification_state so a second observation of the same
    # setup correctly reports duplicate_setup. Real notification_state is
    # NEVER touched — the trader has received nothing, so from production's
    # point of view the setup remains NOT_SENT.
    if mode == "shadow":
        decision = "WOULD_SEND" if eligible else "WOULD_SUPPRESS"
        _persist_event(db, setup_id=setup_id, mode=mode, decision=decision,
                       reason=None if eligible else reason,
                       msg_type="ACTIONABLE",
                       internal_state=setup.internal_state,
                       notification_state=setup.notification_state,
                       message_hash=None)
        if eligible:
            registry.mark_shadow_notification(
                db, setup, new_state="ACTIONABLE_SENT",
                msg_type="ACTIONABLE",
                opportunity_id=opportunity_id, now_utc=now_utc,
            )
        else:
            registry.record_suppression(db, setup, reason=reason,
                                         msg_type="ACTIONABLE",
                                         now_utc=now_utc)
        db.flush()
        return result

    # Gateway mode → the sole sender.
    if not eligible:
        registry.record_suppression(db, setup, reason=reason,
                                     msg_type="ACTIONABLE",
                                     now_utc=now_utc)
        _persist_event(db, setup_id=setup_id, mode=mode,
                       decision="SUPPRESSED", reason=reason,
                       msg_type="ACTIONABLE",
                       internal_state=setup.internal_state,
                       notification_state=setup.notification_state,
                       message_hash=None)
        db.flush()
        return result

    try:
        message = message_builder(signal, setup)
    except Exception as exc:
        log.warning("[predator/gateway] message_builder raised: %s", exc)
        registry.record_suppression(db, setup, reason="builder_error",
                                     msg_type="ACTIONABLE",
                                     now_utc=now_utc)
        _persist_event(db, setup_id=setup_id, mode=mode,
                       decision="SUPPRESSED", reason="builder_error",
                       msg_type="ACTIONABLE",
                       internal_state=setup.internal_state,
                       notification_state=setup.notification_state,
                       message_hash=None)
        db.flush()
        return result

    mhash = _hash_message(message)
    try:
        _send_via_telegram(message)
    except Exception as exc:
        log.warning("[predator/gateway] telegram send failed: %s", exc)
        registry.record_suppression(db, setup, reason="send_error",
                                     msg_type="ACTIONABLE",
                                     now_utc=now_utc)
        _persist_event(db, setup_id=setup_id, mode=mode,
                       decision="SUPPRESSED", reason="send_error",
                       msg_type="ACTIONABLE",
                       internal_state=setup.internal_state,
                       notification_state=setup.notification_state,
                       message_hash=mhash)
        db.flush()
        return result

    registry.mark_notification(db, setup, new_state="ACTIONABLE_SENT",
                                msg_type="ACTIONABLE",
                                message_hash=mhash,
                                opportunity_id=opportunity_id,
                                now_utc=now_utc)
    _persist_event(db, setup_id=setup_id, mode=mode, decision="SENT",
                    reason=None, msg_type="ACTIONABLE",
                    internal_state=setup.internal_state,
                    notification_state=setup.notification_state,
                    message_hash=mhash)
    result["sent"] = True
    db.flush()
    return result


def route_invalidation(
    db: Session,
    *,
    setup_id: str,
    reason_text: str,
    message_builder: Callable[[PredatorSetup, str], str],
    settings=None,
) -> dict:
    """
    Called when an internal candidate goes stale, is rejected, expires, or
    is explicitly invalidated. Only sends Telegram if we previously sent
    an actionable/lifecycle Telegram for the same setup_id.
    """
    if settings is None:
        from config import settings as _settings
        settings = _settings
    mode = getattr(settings, "predator_notification_architecture", "shadow")
    now_utc = datetime.now(timezone.utc)

    setup = db.query(PredatorSetup).filter(
        PredatorSetup.setup_id == setup_id
    ).first()

    if setup is None:
        # No registered setup → nothing to invalidate. Silent.
        _persist_event(db, setup_id=setup_id, mode=mode,
                        decision="SUPPRESSED", reason="no_setup",
                        msg_type="INVALIDATION",
                        internal_state=None, notification_state=None,
                        message_hash=None)
        db.flush()
        return {"setup_id": setup_id, "sent": False, "reason": "no_setup"}

    setup.internal_state = "INVALIDATED"
    # Pick the state column this mode owns. Shadow invalidations must be
    # judged against shadow_notification_state so the projection is
    # accurate; real invalidations against notification_state so we never
    # accidentally send an invalidation for a setup no user ever heard of.
    if mode == "shadow":
        _effective_ns = setup.shadow_notification_state or "NOT_SENT"
    else:
        _effective_ns = setup.notification_state or "NOT_SENT"
    if _effective_ns not in _LIFECYCLE_ELIGIBLE_STATES:
        registry.record_suppression(db, setup,
                                     reason="never_actionable_invalidation",
                                     msg_type="INVALIDATION", now_utc=now_utc)
        _persist_event(db, setup_id=setup_id, mode=mode,
                        decision=("WOULD_SUPPRESS" if mode == "shadow"
                                  else "SUPPRESSED"),
                        reason="never_actionable_invalidation",
                        msg_type="INVALIDATION",
                        internal_state=setup.internal_state,
                        notification_state=_effective_ns,
                        message_hash=None)
        db.flush()
        return {"setup_id": setup_id, "sent": False,
                "reason": "never_actionable_invalidation"}

    if mode == "legacy":
        _persist_event(db, setup_id=setup_id, mode=mode,
                       decision="LEGACY_PASSTHROUGH", reason=None,
                       msg_type="INVALIDATION",
                       internal_state=setup.internal_state,
                       notification_state=setup.notification_state,
                       message_hash=None)
        db.flush()
        return {"setup_id": setup_id, "sent": False, "reason": "legacy"}

    if mode == "shadow":
        _persist_event(db, setup_id=setup_id, mode=mode,
                       decision="WOULD_SEND", reason=None,
                       msg_type="INVALIDATION",
                       internal_state=setup.internal_state,
                       notification_state=setup.notification_state,
                       message_hash=None)
        # Advance shadow lifecycle state; real state untouched.
        registry.mark_shadow_notification(
            db, setup, new_state="INVALIDATED_SENT",
            msg_type="INVALIDATION", now_utc=now_utc,
        )
        db.flush()
        return {"setup_id": setup_id, "sent": False, "reason": "shadow"}

    # gateway mode → send
    try:
        msg = message_builder(setup, reason_text)
        mhash = _hash_message(msg)
        _send_via_telegram(msg)
    except Exception as exc:
        log.warning("[predator/gateway] invalidation send failed: %s", exc)
        _persist_event(db, setup_id=setup_id, mode=mode,
                       decision="SUPPRESSED", reason="send_error",
                       msg_type="INVALIDATION",
                       internal_state=setup.internal_state,
                       notification_state=setup.notification_state,
                       message_hash=None)
        db.flush()
        return {"setup_id": setup_id, "sent": False, "reason": "send_error"}

    registry.mark_notification(db, setup, new_state="INVALIDATED_SENT",
                                msg_type="INVALIDATION",
                                message_hash=mhash, now_utc=now_utc)
    _persist_event(db, setup_id=setup_id, mode=mode, decision="SENT",
                    reason=None, msg_type="INVALIDATION",
                    internal_state=setup.internal_state,
                    notification_state=setup.notification_state,
                    message_hash=mhash)
    db.flush()
    return {"setup_id": setup_id, "sent": True}


# ─────────────────────────────────────────────────────────────────────────────
# Metrics for /diagnostics/predator-notifications
# ─────────────────────────────────────────────────────────────────────────────

def notification_metrics(db: Session, *, hours: int = 24) -> dict:
    """
    Aggregate the gateway's decision log over the last N hours.
    """
    from sqlalchemy import func
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)

    q = db.query(PredatorNotificationEvent).filter(
        PredatorNotificationEvent.created_at >= cutoff
    )
    events = q.all()

    counts = {"SENT": 0, "SUPPRESSED": 0, "WOULD_SEND": 0,
              "WOULD_SUPPRESS": 0, "LEGACY_PASSTHROUGH": 0}
    reasons: dict[str, int] = {}
    msg_types: dict[str, int] = {}
    for e in events:
        counts[e.decision] = counts.get(e.decision, 0) + 1
        if e.reason:
            reasons[e.reason] = reasons.get(e.reason, 0) + 1
        if e.msg_type:
            msg_types[e.msg_type] = msg_types.get(e.msg_type, 0) + 1

    setup_ids = {e.setup_id for e in events}
    setups = db.query(PredatorSetup).filter(
        PredatorSetup.setup_id.in_(setup_ids)
    ).all() if setup_ids else []
    setup_ids_created = len({s.setup_id for s in setups})
    total_observations = sum(int(s.observation_count or 0) for s in setups)
    average_obs = (total_observations / setup_ids_created) if setup_ids_created else 0.0

    actionable_sent = counts.get("SENT", 0)
    actionable_suppressed = counts.get("SUPPRESSED", 0)
    would_send = counts.get("WOULD_SEND", 0)
    would_suppress = counts.get("WOULD_SUPPRESS", 0)

    suppression_rate = 0.0
    denom_supp = actionable_sent + actionable_suppressed + would_send + would_suppress
    if denom_supp > 0:
        suppression_rate = (actionable_suppressed + would_suppress) / denom_supp

    # actionable_signals_suppressed = SENT that WOULD have gone but were
    # blocked by legitimate gate (rr_below_min, stale_data, overextended,
    # duplicate_setup, send_error). "never_actionable_invalidation" and
    # "no_setup" are not "suppressed actionable signals" — they are
    # by-design silence.
    actionable_gate_failures = sum(
        n for r, n in reasons.items()
        if r in {"rr_below_min", "stale_data", "overextended", "send_error"}
    )

    ratio_events_to_sent = ((len(events) / actionable_sent)
                             if actionable_sent > 0 else None)

    return {
        "window_hours": hours,
        "internal_candidates_detected": len(events),
        "setup_ids_created": setup_ids_created,
        "total_observations": total_observations,
        "average_observations_per_setup": round(average_obs, 2),
        "telegram_messages_sent": actionable_sent,
        "telegram_messages_would_send_shadow": would_send,
        "telegram_messages_suppressed": actionable_suppressed + would_suppress,
        "telegram_suppression_rate": round(suppression_rate, 4),
        "candidate_to_actionable_ratio": (
            round(len(events) / max(actionable_sent + would_send, 1), 2)
        ),
        "actionable_signals_suppressed": actionable_gate_failures,
        "suppression_reason_breakdown": reasons,
        "msg_type_breakdown": msg_types,
        "decision_breakdown": counts,
        "alerts_per_actionable_signal": (
            round(ratio_events_to_sent, 2) if ratio_events_to_sent else None
        ),
    }
