"""
Unified account-level XAUUSD portfolio governor with atomic reservation.

ONE authoritative gate above BOTH engines (Predator + Strategist).
Enforces GROSS exposure cap across all XAUUSD execution paths.

GROSS exposure = sum(|lot_size|) across every open+enqueued+reserved position
regardless of direction. Buys and sells add, they do NOT net.

Non-negotiable ceiling: MAX_GROSS_LOTS = 0.15.

Atomicity contract (2026-08-21 v2):
  Global RLock serializes check→reserve→release across schedulers.
  Reservations expire automatically after RESERVATION_TTL_SEC to prevent
  a crashed enqueue from permanently consuming capacity.

MT5 authoritative reconciliation:
  Bridge heartbeat is polled; if broker view disagrees with DB by more
  than TOLERANCE_LOTS, or heartbeat is stale beyond STALE_HEARTBEAT_SEC,
  governor SAFE-FAILS all new orders.

Never automatically closes positions. Never dynamically resizes requests.
"""
from __future__ import annotations

import logging
import threading
import time
import uuid
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import text
from sqlalchemy.orm import Session

log = logging.getLogger(__name__)

MAX_GROSS_LOTS = 0.15
TOLERANCE_LOTS = 0.005
STALE_HEARTBEAT_SEC = 180
RESERVATION_TTL_SEC = 30

# Atomic gate — serializes concurrent Predator + Strategist submissions
_GOVERNOR_LOCK = threading.RLock()

# Reservation state machine:
#   RESERVED  → capacity claimed but no MT5 order yet (TTL applies)
#   SENT      → order submitted to broker, awaiting fill (TTL DOES NOT apply)
#   FILLED    → position exists in DB → reservation retired
#   REJECTED  → broker rejected → reservation released
#   ABANDONED → post-reconciliation cleanup for expired-RESERVED entries
#
# In-memory: {reservation_id: (engine, direction, lots, state, expires_at, mt5_ticket)}
_RESERVATIONS: dict[str, list] = {}


def _prune_expired_reservations() -> None:
    """Remove RESERVED (not-yet-SENT) reservations that outlived TTL.
    SENT reservations are NEVER auto-expired — a delayed broker fill must
    not release capacity that may still land as a real position.
    Only reconciliation against MT5 state can retire SENT reservations."""
    now_ts = time.time()
    stale = []
    for rid, row in _RESERVATIONS.items():
        engine, direction, lots, state, expires_at, ticket = row
        if state == "RESERVED" and expires_at < now_ts:
            stale.append(rid)
    for rid in stale:
        row = _RESERVATIONS.pop(rid)
        engine, direction, lots, state, expires_at, ticket = row
        log.warning("[portfolio_governor] RESERVED expired (auto-abandon) rid=%s "
                    "%s %s lots=%.4f", rid[:8], engine, direction, lots)


def _sum_reservations() -> float:
    """Sum lots across ALL active reservations (RESERVED + SENT).
    SENT reservations are counted because the broker may still fill them."""
    _prune_expired_reservations()
    return sum(row[2] for row in _RESERVATIONS.values())


def mark_sent(reservation_id: str, mt5_ticket: Optional[int] = None) -> bool:
    """Transition RESERVED → SENT. TTL no longer applies after this."""
    with _GOVERNOR_LOCK:
        row = _RESERVATIONS.get(reservation_id)
        if not row: return False
        row[3] = "SENT"
        if mt5_ticket is not None: row[5] = mt5_ticket
        log.info("[portfolio_governor] rid=%s → SENT (ticket=%s)", reservation_id[:8], mt5_ticket)
        return True


def _sum_predator_open(db: Session) -> float:
    try:
        row = db.execute(text(
            "SELECT COALESCE(SUM(lot_size), 0) FROM predator_positions "
            "WHERE status IN ('ENQUEUED','OPEN')"
        )).fetchone()
        return float(row[0]) if row else 0.0
    except Exception as exc:
        log.warning("[portfolio_governor] predator sum failed: %s", exc)
        return -1.0


def _sum_strategist_open(db: Session) -> float:
    try:
        row = db.execute(text(
            "SELECT COALESCE(SUM(lot_size), 0) FROM pending_executions "
            "WHERE status IN ('PENDING','SENT','OPEN') "
            "  AND created_at >= datetime('now', '-2 days')"
        )).fetchone()
        return float(row[0]) if row else 0.0
    except Exception as exc:
        log.warning("[portfolio_governor] strategist sum failed: %s", exc)
        return -1.0


def _bridge_heartbeat() -> Optional[dict]:
    try:
        from routers.bridge import _MT5_TERMINAL_STATE
        if not _MT5_TERMINAL_STATE: return None
        return max(
            _MT5_TERMINAL_STATE.values(),
            key=lambda s: s.get("last_seen") or datetime.min,
        )
    except Exception:
        return None


def _mt5_authoritative_lots() -> tuple[float, bool, str]:
    """Return (mt5_gross_lots, is_authoritative, reason).
    is_authoritative=True means the bridge state is fresh enough to trust as truth.
    """
    hb = _bridge_heartbeat()
    if not hb:
        return -1.0, False, "no bridge heartbeat"
    last_seen = hb.get("last_seen")
    if last_seen is None:
        return -1.0, False, "heartbeat missing last_seen"
    now = datetime.now(timezone.utc)
    try:
        age = (now - last_seen).total_seconds() if hasattr(last_seen, "total_seconds") \
              else (now - last_seen).total_seconds()
    except Exception:
        return -1.0, False, "heartbeat timestamp unparseable"
    if age > STALE_HEARTBEAT_SEC:
        return -1.0, False, f"heartbeat {age:.0f}s > {STALE_HEARTBEAT_SEC}s"

    positions = hb.get("open_positions") or []
    total = 0.0
    for p in positions:
        if str(p.get("symbol","")).upper().startswith("XAU"):
            total += abs(float(p.get("volume", 0)))
    return total, True, "fresh"


def snapshot(db: Session) -> dict:
    pred = _sum_predator_open(db)
    strat = _sum_strategist_open(db)
    reserved = _sum_reservations()
    mt5_lots, mt5_auth, mt5_reason = _mt5_authoritative_lots()

    db_gross = 0.0
    if pred >= 0: db_gross += pred
    if strat >= 0: db_gross += strat
    unknown = (pred < 0 or strat < 0)

    # MT5 vs DB reconciliation — mismatch is dangerous
    mt5_mismatch = False
    if mt5_auth and mt5_lots >= 0:
        mt5_mismatch = abs(mt5_lots - db_gross) > TOLERANCE_LOTS

    # Total committed = db_gross + reservations (pending enqueue)
    committed = db_gross + reserved
    remaining = max(0.0, MAX_GROSS_LOTS - committed)

    return dict(
        predator_lots=pred if pred >= 0 else None,
        strategist_lots=strat if strat >= 0 else None,
        reserved_lots=round(reserved, 4),
        mt5_actual_lots=mt5_lots if mt5_auth else None,
        mt5_authoritative=mt5_auth,
        mt5_reason=mt5_reason,
        db_gross=round(db_gross, 4),
        committed_gross=round(committed, 4),
        max_gross=MAX_GROSS_LOTS,
        remaining_gross=round(remaining, 4),
        mt5_mismatch=mt5_mismatch,
        state_unknown=unknown,
        active_reservations=len(_RESERVATIONS),
        within_limit=(committed <= MAX_GROSS_LOTS + 1e-6 and not unknown and not mt5_mismatch),
    )


def reserve_capacity(
    db: Session,
    *,
    engine: str,
    direction: str,
    proposed_lots: float,
    opportunity_id: Optional[str] = None,
    signal_id: Optional[str] = None,
) -> tuple[bool, str, dict, Optional[str]]:
    """
    ATOMIC check → reserve. Returns (allowed, reason, snapshot, reservation_id).
    If allowed, capacity is RESERVED — caller must either call release_reservation()
    on failure or commit_reservation() after the broker order lands.
    Reservation auto-expires after RESERVATION_TTL_SEC as a safety net.
    """
    with _GOVERNOR_LOCK:
        snap = snapshot(db)
        proposed = abs(float(proposed_lots))

        if snap["state_unknown"]:
            _log_reject(db, engine, direction, proposed, opportunity_id, signal_id,
                        "GLOBAL_EXPOSURE_STATE_UNKNOWN",
                        "One or more engine exposure sums returned sentinel")
            return False, "GLOBAL_EXPOSURE_STATE_UNKNOWN", snap, None

        if not snap["mt5_authoritative"]:
            _log_reject(db, engine, direction, proposed, opportunity_id, signal_id,
                        "MT5_STATE_STALE",
                        snap["mt5_reason"])
            return False, "MT5_STATE_STALE", snap, None

        if snap["mt5_mismatch"]:
            _log_reject(db, engine, direction, proposed, opportunity_id, signal_id,
                        "EXPOSURE_STATE_MISMATCH",
                        f"MT5={snap['mt5_actual_lots']} vs DB={snap['db_gross']}")
            return False, "EXPOSURE_STATE_MISMATCH", snap, None

        if snap["committed_gross"] > MAX_GROSS_LOTS + 1e-6:
            _log_reject(db, engine, direction, proposed, opportunity_id, signal_id,
                        "PREEXISTING_GLOBAL_EXPOSURE_BREACH",
                        f"committed {snap['committed_gross']} > {MAX_GROSS_LOTS}")
            return False, "PREEXISTING_GLOBAL_EXPOSURE_BREACH", snap, None

        resulting = snap["committed_gross"] + proposed
        if resulting > MAX_GROSS_LOTS + 1e-6:
            _log_reject(db, engine, direction, proposed, opportunity_id, signal_id,
                        "GLOBAL_EXPOSURE_REJECT",
                        (f"pred={snap['predator_lots']} strat={snap['strategist_lots']} "
                         f"reserved={snap['reserved_lots']} + {proposed} = "
                         f"{resulting:.4f} > {MAX_GROSS_LOTS}"))
            return False, "GLOBAL_EXPOSURE_REJECT", snap, None

        rid = uuid.uuid4().hex
        _RESERVATIONS[rid] = [engine, direction, proposed, "RESERVED",
                              time.time() + RESERVATION_TTL_SEC, None]
        log.info("[portfolio_governor] RESERVED %s %s %.4f lots (rid=%s) — "
                 "committed after=%.4f/%.2f",
                 engine, direction, proposed, rid[:8],
                 resulting, MAX_GROSS_LOTS)
        return True, "OK", snap, rid


def release_reservation(reservation_id: str, reason: str = "release") -> None:
    """Release a reservation (broker rejected the order, or execution errored)."""
    with _GOVERNOR_LOCK:
        row = _RESERVATIONS.pop(reservation_id, None)
        if row:
            log.info("[portfolio_governor] RELEASED rid=%s reason=%s",
                     reservation_id[:8], reason)


def commit_reservation(reservation_id: str, mt5_ticket: Optional[int] = None) -> None:
    """Replace reservation with actual broker fill.
    The DB now shows the position so we can drop the reservation."""
    with _GOVERNOR_LOCK:
        row = _RESERVATIONS.pop(reservation_id, None)
        if row:
            log.info("[portfolio_governor] COMMITTED rid=%s ticket=%s",
                     reservation_id[:8], mt5_ticket)


# Legacy non-atomic check_new_order — kept for backwards compatibility
def check_new_order(
    db: Session,
    *,
    engine: str, direction: str, proposed_lots: float,
    opportunity_id: Optional[str] = None,
    signal_id: Optional[str] = None,
) -> tuple[bool, str, dict]:
    """Non-reserving check. Prefer reserve_capacity() for real submissions.
    Kept as compat wrapper."""
    allowed, reason, snap, rid = reserve_capacity(
        db, engine=engine, direction=direction, proposed_lots=proposed_lots,
        opportunity_id=opportunity_id, signal_id=signal_id,
    )
    if allowed and rid:
        # Immediately release; this is a non-reserving check
        release_reservation(rid, "check_only")
    return allowed, reason, snap


def _log_reject(
    db: Session, engine: str, direction: str, proposed: float,
    opp_id: Optional[str], sig_id: Optional[str],
    reason: str, detail: str,
) -> None:
    try:
        db.execute(text("""
            INSERT INTO predator_rejections
              (created_at, opportunity_id, predator_version, archetype, direction,
               rejection_reason, rejection_detail)
            VALUES (:ca, :oi, :ver, :arch, :dir, :rr, :rd)
        """), dict(
            ca=datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
            oi=opp_id or f"{engine}-{sig_id or 'unknown'}",
            ver=f"GOVERNOR_v1.0_{engine}",
            arch=engine, dir=direction, rr=reason, rd=(detail or "")[:255],
        ))
        db.commit()
    except Exception as exc:
        log.debug("[portfolio_governor] reject log failed: %s", exc)
        try: db.rollback()
        except Exception: pass
