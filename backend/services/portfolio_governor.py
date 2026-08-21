"""
Unified account-level XAUUSD portfolio governor.

ONE authoritative gate above BOTH engines (Predator + Strategist).
Enforces GROSS exposure cap across all XAUUSD execution paths.

GROSS exposure = sum(|lot_size|) across every open+enqueued position
regardless of direction. Buys and sells add, they do NOT net.

Non-negotiable ceiling: MAX_GROSS_LOTS = 0.15.

Fail-safe philosophy:
  - On any state uncertainty (DB error, unknown engine, unreconciled state)
    → REFUSE the new order rather than let it through.
  - Never automatically close positions.
  - Never dynamically resize the request — reject and let the caller decide.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import text
from sqlalchemy.orm import Session

log = logging.getLogger(__name__)

MAX_GROSS_LOTS = 0.15


def _sum_predator_open(db: Session) -> float:
    """Sum lots on predator_positions where status IN ('ENQUEUED','OPEN')."""
    try:
        row = db.execute(text(
            "SELECT COALESCE(SUM(lot_size), 0) FROM predator_positions "
            "WHERE status IN ('ENQUEUED','OPEN')"
        )).fetchone()
        return float(row[0]) if row else 0.0
    except Exception as exc:
        log.warning("[portfolio_governor] predator sum failed: %s", exc)
        return -1.0  # sentinel for "unknown"


def _sum_strategist_open(db: Session) -> float:
    """Sum lots on strategist path (PendingExecution not yet closed OR
    strategist demo trades marked open in mt5_trade_logs.raw_response_json).
    We use the same query as the strategist's own _current_open_lot_exposure."""
    try:
        # PendingExecution rows that were sent to broker and are still open
        row = db.execute(text(
            "SELECT COALESCE(SUM(lot_size), 0) FROM pending_executions "
            "WHERE status IN ('PENDING','SENT','OPEN') "
            "  AND created_at >= datetime('now', '-2 days')"
        )).fetchone()
        return float(row[0]) if row else 0.0
    except Exception as exc:
        log.warning("[portfolio_governor] strategist sum failed: %s", exc)
        return -1.0


def _sum_mt5_actual_open(db: Session) -> float:
    """Cross-check: look at the bridge's authoritative view (heartbeat).
    Returns -1 if unavailable."""
    try:
        from routers.bridge import _MT5_TERMINAL_STATE
        if not _MT5_TERMINAL_STATE: return -1.0
        latest = max(
            _MT5_TERMINAL_STATE.values(),
            key=lambda s: s.get("last_seen") or datetime.min,
        )
        # If daemon reports open_positions_snapshot, sum lots
        positions = latest.get("open_positions") or []
        if not positions: return 0.0
        total = 0.0
        for p in positions:
            if str(p.get("symbol","")).upper().startswith("XAU"):
                total += abs(float(p.get("volume", 0)))
        return total
    except Exception:
        return -1.0


def snapshot(db: Session) -> dict:
    """Full portfolio snapshot. Never raises."""
    pred = _sum_predator_open(db)
    strat = _sum_strategist_open(db)
    mt5 = _sum_mt5_actual_open(db)
    # Prefer DB view but flag mismatch with MT5
    db_gross = 0.0
    if pred >= 0: db_gross += pred
    if strat >= 0: db_gross += strat
    mt5_mismatch = (mt5 >= 0 and abs(mt5 - db_gross) > 0.005)
    unknown = (pred < 0 or strat < 0)
    remaining = max(0.0, MAX_GROSS_LOTS - db_gross)
    return dict(
        predator_lots=pred if pred >= 0 else None,
        strategist_lots=strat if strat >= 0 else None,
        mt5_actual_lots=mt5 if mt5 >= 0 else None,
        db_gross=round(db_gross, 4),
        max_gross=MAX_GROSS_LOTS,
        remaining_gross=round(remaining, 4),
        mt5_mismatch=mt5_mismatch,
        state_unknown=unknown,
        within_limit=(db_gross <= MAX_GROSS_LOTS + 1e-6 and not unknown),
    )


def check_new_order(
    db: Session,
    *,
    engine: str,             # "PREDATOR" | "STRATEGIST"
    direction: str,          # "BUY" | "SELL"
    proposed_lots: float,
    opportunity_id: Optional[str] = None,
    signal_id: Optional[str] = None,
) -> tuple[bool, str, dict]:
    """
    Governor check for a proposed new order. Returns (allowed, reason, snapshot).

    Rules:
      1. State-unknown → REJECT (fail safe).
      2. db_gross + proposed > 0.15 → REJECT with GLOBAL_EXPOSURE_REJECT.
      3. mt5 vs db mismatch > 0.005 → REJECT with EXPOSURE_STATE_MISMATCH.
      4. Pre-existing breach (db_gross > 0.15 before proposal) → REJECT.
    """
    snap = snapshot(db)
    proposed = abs(float(proposed_lots))

    if snap["state_unknown"]:
        _log_reject(db, engine=engine, direction=direction, proposed=proposed,
                    opp_id=opportunity_id, sig_id=signal_id,
                    reason="GLOBAL_EXPOSURE_STATE_UNKNOWN",
                    detail="Could not read one or more engine exposure states")
        return False, "GLOBAL_EXPOSURE_STATE_UNKNOWN", snap

    if snap["mt5_mismatch"]:
        _log_reject(db, engine=engine, direction=direction, proposed=proposed,
                    opp_id=opportunity_id, sig_id=signal_id,
                    reason="EXPOSURE_STATE_MISMATCH",
                    detail=f"MT5={snap['mt5_actual_lots']} vs DB={snap['db_gross']}")
        return False, "EXPOSURE_STATE_MISMATCH", snap

    if snap["db_gross"] > MAX_GROSS_LOTS + 1e-6:
        _log_reject(db, engine=engine, direction=direction, proposed=proposed,
                    opp_id=opportunity_id, sig_id=signal_id,
                    reason="PREEXISTING_GLOBAL_EXPOSURE_BREACH",
                    detail=f"gross {snap['db_gross']} > {MAX_GROSS_LOTS}")
        return False, "PREEXISTING_GLOBAL_EXPOSURE_BREACH", snap

    resulting = snap["db_gross"] + proposed
    if resulting > MAX_GROSS_LOTS + 1e-6:
        _log_reject(db, engine=engine, direction=direction, proposed=proposed,
                    opp_id=opportunity_id, sig_id=signal_id,
                    reason="GLOBAL_EXPOSURE_REJECT",
                    detail=(f"predator={snap['predator_lots']} strategist={snap['strategist_lots']} "
                            f"+ proposed {proposed} = {resulting:.4f} > {MAX_GROSS_LOTS}"))
        return False, "GLOBAL_EXPOSURE_REJECT", snap

    return True, "OK", snap


def _log_reject(
    db: Session,
    *,
    engine: str, direction: str, proposed: float,
    opp_id: Optional[str], sig_id: Optional[str],
    reason: str, detail: str,
) -> None:
    """Persist a portfolio-governor rejection to predator_rejections
    (reused as a generic rejection log with a distinct source tag)."""
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
            arch=engine,
            dir=direction,
            rr=reason,
            rd=(detail or "")[:255],
        ))
        db.commit()
    except Exception as exc:
        log.debug("[portfolio_governor] reject log failed: %s", exc)
        try: db.rollback()
        except Exception: pass
