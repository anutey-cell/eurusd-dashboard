"""
Signal Registry — State Machine + Persistence
==============================================

The one place that owns signal lifecycle. Adapters call `upsert()` and
`transition()`; downstream consumers (Telegram router, dashboard v2)
read the persisted state.

Guarantees:
  1. Fingerprint dedup — same setup upserts to same row
  2. Transition validation — invalid state jumps raise ValueError
  3. Every transition writes SignalStateTransition (audit)
  4. Idempotent — upserting an unchanged signal is a no-op
  5. Restart-safe — state lives in DB, module has zero cached state
  6. Sequence numbering per (strategy, date) — signal IDs stay stable
     across restarts

The registry does NOT decide about notifications — that's the router.
It just tracks WHAT the signal state is right now.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import func
from sqlalchemy.orm import Session

from services.canonical_signal import (
    CanonicalSignal,
    ALL_STATES, TERMINAL_STATES,
    is_valid_transition, make_signal_id, signal_fingerprint,
    STATE_DETECTED,
)

log = logging.getLogger(__name__)


# ── Sequence-number helper ─────────────────────────────────────────────────

def _next_sequence(db: Session, strategy_id: str,
                   date_str: str, instrument: str = "XAUUSD") -> int:
    """Return the next sequence number for (strategy, YYYYMMDD)."""
    from db_models import Signal as SM
    prefix_pattern = f"%-{instrument[:3]}-{date_str}-%"
    row = (db.query(func.count(SM.id))
             .filter(SM.strategy_id == strategy_id)
             .filter(SM.signal_id.like(prefix_pattern))
             .scalar())
    return int(row or 0) + 1


# ── Row ↔ CanonicalSignal conversion ───────────────────────────────────────

def _as_utc(dt):
    """Coerce a DB-returned datetime to tz-aware UTC. SQLite stores naive."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _row_to_signal(row) -> CanonicalSignal:
    """Materialize DB row into a CanonicalSignal (immutable)."""
    return CanonicalSignal(
        signal_id=row.signal_id,
        fingerprint=row.fingerprint,
        strategy_id=row.strategy_id,
        strategy_name=row.strategy_name,
        instrument=row.instrument,
        direction=row.direction,
        confidence=row.confidence,
        entry_zone_low=row.entry_zone_low,
        entry_zone_high=row.entry_zone_high,
        stop_loss=row.stop_loss,
        current_stop=row.current_stop,
        invalidation=row.invalidation or "",
        no_chase_price=row.no_chase_price,
        tp1=row.tp1, tp2=row.tp2, tp3=row.tp3,
        tp1_label=row.tp1_label, tp2_label=row.tp2_label, tp3_label=row.tp3_label,
        rr_tp1=row.rr_tp1, rr_tp2=row.rr_tp2, rr_tp3=row.rr_tp3,
        session=row.session or "",
        market_regime=row.market_regime,
        htf_bias=row.htf_bias,
        trap_side=row.trap_side,
        reference_zone_low=row.reference_zone_low,
        reference_zone_high=row.reference_zone_high,
        conditions_met=tuple(json.loads(row.conditions_met_json or "[]")),
        conditions_missing=tuple(json.loads(row.conditions_missing_json or "[]")),
        rationale=row.rationale or "",
        data_source=row.data_source,
        confluence=tuple(json.loads(row.confluence_json or "[]")),
        state=row.state,
        previous_state=row.previous_state,
        created_at=_as_utc(row.created_at),
        valid_until=_as_utc(row.valid_until),
        triggered_at=_as_utc(row.triggered_at),
        closed_at=_as_utc(row.closed_at),
        is_broker_confirmed=row.is_broker_confirmed,
        r_realized=row.r_realized,
        partial_taken=row.partial_taken,
    )


def _copy_signal_to_row(sig: CanonicalSignal, row) -> None:
    """Apply a CanonicalSignal to an existing DB row (mutates row)."""
    row.strategy_name       = sig.strategy_name
    row.instrument          = sig.instrument
    row.direction           = sig.direction
    row.confidence          = sig.confidence
    row.entry_zone_low      = sig.entry_zone_low
    row.entry_zone_high     = sig.entry_zone_high
    row.stop_loss           = sig.stop_loss
    row.current_stop        = sig.current_stop
    row.invalidation        = sig.invalidation[:255]
    row.no_chase_price      = sig.no_chase_price
    row.tp1                 = sig.tp1
    row.tp2                 = sig.tp2
    row.tp3                 = sig.tp3
    row.tp1_label           = sig.tp1_label
    row.tp2_label           = sig.tp2_label
    row.tp3_label           = sig.tp3_label
    row.rr_tp1              = sig.rr_tp1
    row.rr_tp2              = sig.rr_tp2
    row.rr_tp3              = sig.rr_tp3
    row.session             = sig.session
    row.market_regime       = sig.market_regime
    row.htf_bias            = sig.htf_bias
    row.trap_side           = sig.trap_side
    row.reference_zone_low  = sig.reference_zone_low
    row.reference_zone_high = sig.reference_zone_high
    row.conditions_met_json = json.dumps(list(sig.conditions_met))
    row.conditions_missing_json = json.dumps(list(sig.conditions_missing))
    row.rationale           = sig.rationale
    row.data_source         = sig.data_source
    row.confluence_json     = json.dumps([dict(c) for c in sig.confluence])
    row.state               = sig.state
    row.previous_state      = sig.previous_state
    row.valid_until         = sig.valid_until
    row.triggered_at        = sig.triggered_at
    row.closed_at           = sig.closed_at
    row.is_broker_confirmed = sig.is_broker_confirmed
    row.r_realized          = sig.r_realized
    row.partial_taken       = sig.partial_taken


# ── Public API ─────────────────────────────────────────────────────────────

def upsert(
    db: Session,
    *,
    strategy_id:      str,
    strategy_name:    str,
    instrument:       str,
    direction:        str,
    confidence:       int,
    entry_zone_low:   float,
    entry_zone_high:  float,
    stop_loss:        float,
    invalidation:     str,
    session:          str,
    tp1:  Optional[float] = None,
    tp2:  Optional[float] = None,
    tp3:  Optional[float] = None,
    tp1_label: Optional[str] = None,
    tp2_label: Optional[str] = None,
    tp3_label: Optional[str] = None,
    rr_tp1: Optional[float] = None,
    rr_tp2: Optional[float] = None,
    rr_tp3: Optional[float] = None,
    no_chase_price: Optional[float] = None,
    market_regime: Optional[str] = None,
    htf_bias: Optional[str] = None,
    trap_side: Optional[str] = None,
    reference_zone_low:  Optional[float] = None,
    reference_zone_high: Optional[float] = None,
    conditions_met:     Optional[list] = None,
    conditions_missing: Optional[list] = None,
    rationale: str = "",
    data_source: str = "tick_proxy",
    confluence: Optional[list] = None,
    valid_until: Optional[datetime] = None,
    is_broker_confirmed: bool = False,
    initial_state: str = STATE_DETECTED,
    now: Optional[datetime] = None,
) -> CanonicalSignal:
    """
    Create-or-update the canonical signal identified by fingerprint.

    Returns the persisted CanonicalSignal. If the fingerprint already
    exists, updates mutable fields (confidence, conditions, rationale)
    but LEAVES STATE ALONE — state changes go through transition().

    Idempotent: two identical calls yield the same signal.
    """
    from db_models import Signal as SM

    if now is None:
        now = datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)

    fp = signal_fingerprint(
        instrument=instrument,
        direction=direction,
        strategy_id=strategy_id,
        entry_zone_low=entry_zone_low,
        entry_zone_high=entry_zone_high,
        stop_loss=stop_loss,
        session=session,
        created_at=now,
    )

    existing = db.query(SM).filter(SM.fingerprint == fp).one_or_none()

    if existing is not None:
        # Update mutable fields. Leave state, signal_id, created_at alone.
        existing.confidence = confidence
        existing.rationale  = rationale
        existing.confluence_json = json.dumps([dict(c) for c in (confluence or [])])
        existing.conditions_met_json     = json.dumps(list(conditions_met or []))
        existing.conditions_missing_json = json.dumps(list(conditions_missing or []))
        existing.no_chase_price = no_chase_price
        existing.market_regime  = market_regime
        existing.htf_bias       = htf_bias
        # If price data drifted enough to change zones, still update — but the
        # fingerprint bucketed it to same identity, so it's the same signal
        existing.entry_zone_low  = entry_zone_low
        existing.entry_zone_high = entry_zone_high
        existing.tp1, existing.tp2, existing.tp3 = tp1, tp2, tp3
        existing.rr_tp1, existing.rr_tp2, existing.rr_tp3 = rr_tp1, rr_tp2, rr_tp3
        db.flush()
        return _row_to_signal(existing)

    # New signal — allocate sequence + signal_id
    date_str = now.strftime("%Y%m%d")
    seq = _next_sequence(db, strategy_id, date_str, instrument)
    sig_id = make_signal_id(strategy_id, seq, instrument=instrument[:3], at=now)

    if initial_state not in ALL_STATES:
        raise ValueError(f"invalid initial_state {initial_state!r}")

    row = SM(
        signal_id=sig_id,
        fingerprint=fp,
        strategy_id=strategy_id,
        strategy_name=strategy_name,
        instrument=instrument,
        direction=direction,
        confidence=confidence,
        entry_zone_low=entry_zone_low,
        entry_zone_high=entry_zone_high,
        stop_loss=stop_loss,
        current_stop=stop_loss,          # starts at stop_loss; risk engine mutates
        invalidation=invalidation[:255],
        no_chase_price=no_chase_price,
        tp1=tp1, tp2=tp2, tp3=tp3,
        tp1_label=tp1_label, tp2_label=tp2_label, tp3_label=tp3_label,
        rr_tp1=rr_tp1, rr_tp2=rr_tp2, rr_tp3=rr_tp3,
        session=session,
        market_regime=market_regime,
        htf_bias=htf_bias,
        trap_side=trap_side,
        reference_zone_low=reference_zone_low,
        reference_zone_high=reference_zone_high,
        conditions_met_json=json.dumps(list(conditions_met or [])),
        conditions_missing_json=json.dumps(list(conditions_missing or [])),
        rationale=rationale,
        data_source=data_source,
        confluence_json=json.dumps([dict(c) for c in (confluence or [])]),
        state=initial_state,
        previous_state=None,
        created_at=now,
        valid_until=valid_until,
        is_broker_confirmed=is_broker_confirmed,
    )
    db.add(row)
    db.flush()

    # Record initial creation as a transition (from None → initial_state)
    _write_transition(db, sig_id, from_state=None, to_state=initial_state,
                      reason="signal_created", at=now)

    return _row_to_signal(row)


def transition(
    db: Session,
    *,
    signal_id: str,
    to_state:  str,
    reason:    str = "",
    price_at_transition: Optional[float] = None,
    payload:   Optional[dict] = None,
    now:       Optional[datetime] = None,
    # Optional updates that happen atomically with the transition
    current_stop: Optional[float] = None,
    r_realized:   Optional[float] = None,
    triggered_at: Optional[datetime] = None,
    closed_at:    Optional[datetime] = None,
    partial_taken: Optional[bool] = None,
) -> CanonicalSignal:
    """
    Advance signal to `to_state`. Validates the transition. Persists.

    Idempotent for same-state transitions (returns without writing).
    Raises ValueError for invalid transitions per PERMITTED_TRANSITIONS.
    """
    from db_models import Signal as SM

    if now is None:
        now = datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)

    row = db.query(SM).filter(SM.signal_id == signal_id).one_or_none()
    if row is None:
        raise KeyError(f"signal {signal_id!r} not found")

    if row.state == to_state:
        # Idempotent no-op
        return _row_to_signal(row)

    if not is_valid_transition(row.state, to_state):
        raise ValueError(
            f"invalid transition {row.state} → {to_state} for signal {signal_id}"
        )

    row.previous_state = row.state
    row.state = to_state

    if current_stop is not None:
        row.current_stop = current_stop
    if r_realized is not None:
        row.r_realized = r_realized
    if partial_taken is not None:
        row.partial_taken = partial_taken
    if triggered_at is not None:
        row.triggered_at = triggered_at
    if closed_at is not None:
        row.closed_at = closed_at

    # Auto-set triggered_at / closed_at for common transitions
    if to_state == "TRIGGERED" and row.triggered_at is None:
        row.triggered_at = now
    if to_state in TERMINAL_STATES and row.closed_at is None:
        row.closed_at = now

    _write_transition(
        db, signal_id,
        from_state=row.previous_state, to_state=to_state,
        reason=reason, at=now,
        price_at_transition=price_at_transition, payload=payload,
    )

    return _row_to_signal(row)


def _write_transition(
    db: Session, signal_id: str,
    from_state: Optional[str], to_state: str,
    reason: str, at: datetime,
    price_at_transition: Optional[float] = None,
    payload: Optional[dict] = None,
) -> None:
    from db_models import SignalStateTransition as ST
    row = ST(
        at=at, signal_id=signal_id,
        from_state=from_state or "", to_state=to_state,
        reason=reason[:255] if reason else None,
        price_at_transition=price_at_transition,
        payload_json=json.dumps(payload) if payload else None,
    )
    db.add(row)


# ── Read helpers ────────────────────────────────────────────────────────────

def get_by_signal_id(db: Session, signal_id: str) -> Optional[CanonicalSignal]:
    from db_models import Signal as SM
    row = db.query(SM).filter(SM.signal_id == signal_id).one_or_none()
    return _row_to_signal(row) if row else None


def get_by_fingerprint(db: Session, fingerprint: str) -> Optional[CanonicalSignal]:
    from db_models import Signal as SM
    row = db.query(SM).filter(SM.fingerprint == fingerprint).one_or_none()
    return _row_to_signal(row) if row else None


def active_signals(db: Session, instrument: str = "XAUUSD",
                   directions: Optional[list] = None) -> list[CanonicalSignal]:
    """Signals in a non-terminal state, newest first."""
    from db_models import Signal as SM
    q = (db.query(SM)
           .filter(SM.instrument == instrument)
           .filter(~SM.state.in_(list(TERMINAL_STATES))))
    if directions:
        q = q.filter(SM.direction.in_(directions))
    rows = q.order_by(SM.updated_at.desc()).limit(50).all()
    return [_row_to_signal(r) for r in rows]


def monitored_signals(db: Session, instrument: str = "XAUUSD") -> list[CanonicalSignal]:
    """Signals in MONITORING state (watchlist)."""
    from db_models import Signal as SM
    rows = (db.query(SM)
              .filter(SM.instrument == instrument)
              .filter(SM.state == "MONITORING")
              .order_by(SM.updated_at.desc())
              .limit(50).all())
    return [_row_to_signal(r) for r in rows]


def recent_transitions(db: Session, limit: int = 20) -> list[dict]:
    """Latest state transitions for debug / dashboard."""
    from db_models import SignalStateTransition as ST
    rows = db.query(ST).order_by(ST.at.desc()).limit(limit).all()
    return [{
        "at":                r.at.isoformat() if r.at else None,
        "signal_id":         r.signal_id,
        "from_state":        r.from_state or None,
        "to_state":          r.to_state,
        "reason":            r.reason,
        "price_at_transition": r.price_at_transition,
    } for r in rows]


def sweep_expired(db: Session, now: Optional[datetime] = None) -> list[str]:
    """
    Transition every non-terminal signal past its valid_until into EXPIRED.

    Returns list of signal_ids that were expired. Called by background
    scheduler on a periodic sweep (once per minute is enough).
    """
    from db_models import Signal as SM
    if now is None:
        now = datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)

    expired = []
    rows = (db.query(SM)
              .filter(~SM.state.in_(list(TERMINAL_STATES)))
              .filter(SM.valid_until.isnot(None))
              .filter(SM.valid_until <= now)
              .all())
    for row in rows:
        if is_valid_transition(row.state, "EXPIRED"):
            try:
                transition(db, signal_id=row.signal_id, to_state="EXPIRED",
                           reason="valid_until reached", now=now)
                expired.append(row.signal_id)
            except Exception as exc:
                log.warning("[registry] sweep_expired: %s → EXPIRED failed: %s",
                            row.signal_id, exc)
    return expired
