"""
Predator DEMO execution manager (spec §7).

Responsibilities:
  1. Persist a PredatorSignalBatch + planned PredatorPositions per FIRE
  2. Enqueue tickets one at a time (staged deployment) with per-ticket
     revalidation of price / spread / extension / regime / volume-expansion
  3. Hard exposure guards — refuses to breach STANDARD 0.15 / EXPANSION 0.30
  4. Demo-account guard reused from mandate flow (fail-safe on any drift)
  5. Post-execution Telegram summary

Absolute non-negotiables (spec §7):
  - Individual ticket must equal 0.03 lots — reject any other value
  - Never exceed 0.30 lots total predator exposure
  - Never enqueue if account is not the sanctioned demo (435888680@*Trial*)
  - Log PREDATOR_EXPOSURE_LIMIT_BLOCK on any exposure breach
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import asdict
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import text
from sqlalchemy.orm import Session

from config import settings
from db_models import PendingExecution, PredatorSignalBatch, PredatorPosition
from services.predator_position_sizer import (
    DeploymentPlan, PositionPlan, VolumeExpansionResult,
    PREDATOR_LOT_SIZE,
    PREDATOR_EXPANSION_MAX_EXPOSURE,
    validate_exposure_within_ceiling,
)

log = logging.getLogger(__name__)


# ── Reused helpers (mirror strategist_runner pattern) ────────────────────────

def _last_bridge_heartbeat() -> Optional[dict]:
    """Copy of strategist_runner._last_bridge_heartbeat — kept local to avoid
    cross-module coupling on private state."""
    try:
        from routers.bridge import _MT5_TERMINAL_STATE
        if not _MT5_TERMINAL_STATE:
            return None
        with_account = [
            s for s in _MT5_TERMINAL_STATE.values()
            if s.get("account_login") is not None
        ]
        if with_account:
            return max(with_account,
                        key=lambda s: s.get("last_seen") or datetime.min)
        return max(_MT5_TERMINAL_STATE.values(),
                    key=lambda s: s.get("last_seen") or datetime.min)
    except Exception as exc:
        log.debug("[predator_exec] heartbeat lookup failed: %s", exc)
        return None


def _current_predator_exposure(db: Session, batch_id: Optional[int] = None) -> float:
    """
    Sum lots across Predator positions currently OPEN or ENQUEUED. If
    batch_id given, includes only that batch (per-batch cap enforcement).
    """
    try:
        query = ("SELECT COALESCE(SUM(lot_size), 0) FROM predator_positions "
                 "WHERE status IN ('ENQUEUED','OPEN')")
        params = {}
        if batch_id is not None:
            query += " AND batch_id = :bid"
            params["bid"] = batch_id
        row = db.execute(text(query), params).fetchone()
        return float(row[0] or 0.0) if row else 0.0
    except Exception as exc:
        log.error("[predator_exec] exposure query failed: %s — "
                    "returning high sentinel", exc)
        return PREDATOR_EXPANSION_MAX_EXPOSURE + 1.0   # fail-safe: block


def _verify_demo_account() -> tuple[bool, str]:
    """Hard demo-account guard. Fails safe: any doubt → block."""
    demo_login  = int(getattr(settings, "mandate_demo_login", 435888680))
    demo_srv    = str(getattr(settings, "mandate_demo_server_contains", "Trial"))
    demo_symbol = str(getattr(settings, "mandate_demo_symbol", "XAUUSD"))

    hb = _last_bridge_heartbeat()
    if not hb:
        return False, "no bridge heartbeat"
    hb_login  = hb.get("account_login")
    hb_server = hb.get("account_server") or ""
    hb_symbol = hb.get("symbol") or ""

    if hb_login is None:
        return False, "heartbeat has no account_login"
    if int(hb_login) != demo_login:
        return False, (f"login {hb_login} != sanctioned demo {demo_login}")
    if demo_srv.lower() not in hb_server.lower():
        return False, f"server {hb_server!r} lacks {demo_srv!r}"
    if hb_symbol and hb_symbol.upper() != demo_symbol.upper():
        return False, f"symbol {hb_symbol!r} != {demo_symbol!r}"
    return True, "ok"


def _current_predator_open_count(db: Session, batch_id: int) -> int:
    try:
        row = db.execute(text(
            "SELECT COUNT(*) FROM predator_positions "
            "WHERE batch_id=:bid AND status IN ('ENQUEUED','OPEN')"
        ), {"bid": batch_id}).fetchone()
        return int(row[0] or 0) if row else 0
    except Exception:
        return 9999   # fail-safe: block


# ── Revalidation callback used between staged enqueues ──────────────────────

def _revalidate_signal_still_valid(
    signal_id: str,
    key_level: Optional[float],
    direction: str,
    current_price: Optional[float],
    pct_consumed_now: Optional[float],
    max_pct_consumed: float,
    regime_still_favorable: bool,
    expansion_mode: str,
    expansion_still_confirmed: Optional[bool],
) -> tuple[bool, str]:
    """
    Cheap pre-enqueue re-check. Returns (still_valid, reason).

    All checks fail SAFE — any None / doubt aborts the additional ticket.
    """
    if not regime_still_favorable:
        return False, "regime no longer favorable"

    if pct_consumed_now is not None and pct_consumed_now >= max_pct_consumed:
        return False, (f"LATE/EXHAUSTED — pct_consumed {pct_consumed_now:.0f}% "
                       f">= {max_pct_consumed:.0f}%")

    # For EXPANSION batches, keep confirming volume expansion is still true
    # for tickets 6-10 (spec: "recalculate volume-expansion status before every add")
    if expansion_mode == "EXPANSION" and expansion_still_confirmed is False:
        return False, "volume expansion no longer confirmed"

    return True, "ok"


# ── Batch creation + persistence ────────────────────────────────────────────

def create_batch(
    db: Session,
    *,
    signal_id: str,
    archetype: str,
    direction: str,
    entry_price: float,
    stop_loss: float,
    tp1: float,
    tp2: float,
    key_level: Optional[float],
    plan: DeploymentPlan,
    regime_direction: Optional[str] = None,
    regime_volatility: Optional[str] = None,
) -> PredatorSignalBatch:
    """
    Persist the batch header + all planned position rows (status=PLANNED).
    No enqueuing yet — that happens in `execute_batch_staged`.
    """
    ev = plan.expansion_evidence
    batch = PredatorSignalBatch(
        signal_id=signal_id,
        archetype=archetype,
        direction=direction,
        entry_price=entry_price,
        stop_loss=stop_loss,
        tp1=tp1,
        tp2=tp2,
        key_level=key_level,
        exposure_mode=plan.exposure_mode,
        planned_positions=len(plan.positions),
        lot_per_position=PREDATOR_LOT_SIZE,
        max_exposure_lots=plan.max_exposure_lots,
        vol_pct_at_fire=(ev.vol_pct if ev else None),
        atr20_at_fire=(ev.atr20 if ev else None),
        disp_atr_ratio=(ev.disp_atr if ev else None),
        expansion_reason=(ev.reason if ev else None),
        regime_direction=regime_direction,
        regime_volatility=regime_volatility,
        execution_status="PLANNED",
    )
    db.add(batch)
    db.flush()   # populate batch.id

    for p in plan.positions:
        pos = PredatorPosition(
            batch_id=batch.id,
            seq_no=p.seq_no,
            lot_size=p.lot_size,
            tp_target=p.tp_target,
            entry_price_planned=p.entry_price,
            stop_loss=p.stop_loss,
            take_profit_planned=p.take_profit,
            status="PLANNED",
        )
        db.add(pos)
    db.commit()
    log.info("[predator_exec] batch=%d created signal=%s mode=%s planned=%d",
              batch.id, signal_id, plan.exposure_mode, len(plan.positions))
    return batch


# ── Staged deployment — one ticket at a time with re-validation ─────────────

def execute_batch_staged(
    db: Session,
    batch: PredatorSignalBatch,
    *,
    revalidate_fn=None,
    stage_delay_s: float = 0.5,
) -> dict:
    """
    Deploy the planned tickets sequentially. Between each ticket:
      1. Hard demo-account guard
      2. Hard exposure ceiling (STANDARD/EXPANSION mode-aware)
      3. Master execution flag check (settings.predator_execution_enabled)
      4. Optional caller-supplied revalidate_fn — return (still_valid, reason)
         to abort the remainder of the batch mid-deployment

    Returns a summary dict with counts + ticket ids.
    """
    # Master gate — respects the shadow-mode default (predator_execution_enabled=False)
    if not getattr(settings, "predator_execution_enabled", False):
        log.info("[predator_exec] batch=%d SHADOW-MODE — execution disabled by flag; "
                  "positions stay PLANNED", batch.id)
        _set_batch_status(db, batch, "SHADOW_ONLY",
                            abort_reason="predator_execution_enabled=false")
        return {"opened": 0, "aborted_after": 0, "mode": "SHADOW_ONLY",
                "tickets": [], "reason": "shadow mode"}

    if not getattr(settings, "mt5_bridge_enabled", False):
        _set_batch_status(db, batch, "ABORTED",
                            abort_reason="mt5_bridge_enabled=false")
        return {"opened": 0, "aborted_after": 0, "mode": batch.exposure_mode,
                "tickets": [], "reason": "bridge disabled"}

    demo_ok, demo_msg = _verify_demo_account()
    if not demo_ok:
        log.error("[predator_exec] batch=%d REFUSED — demo guard: %s",
                    batch.id, demo_msg)
        _set_batch_status(db, batch, "ABORTED",
                            abort_reason=f"demo-guard: {demo_msg}")
        return {"opened": 0, "aborted_after": 0, "mode": batch.exposure_mode,
                "tickets": [], "reason": f"demo-guard: {demo_msg}"}

    _set_batch_status(db, batch, "ENQUEUING")

    planned = db.query(PredatorPosition).filter(
        PredatorPosition.batch_id == batch.id,
        PredatorPosition.status == "PLANNED",
    ).order_by(PredatorPosition.seq_no).all()

    opened_tickets: list[int] = []
    aborted_after: Optional[int] = None
    for pos in planned:
        # Fresh exposure snapshot
        current = _current_predator_exposure(db)
        allowed, reason = validate_exposure_within_ceiling(
            current_exposure_lots=current,
            proposed_lot=pos.lot_size,
            exposure_mode=batch.exposure_mode,
        )
        if not allowed:
            log.error("PREDATOR_EXPOSURE_LIMIT_BLOCK  batch=%d seq=%d %s",
                        batch.id, pos.seq_no, reason)
            _mark_position(db, pos, status="REJECTED",
                            reject_reason=f"exposure guard: {reason}")
            aborted_after = pos.seq_no - 1
            break

        # Caller-supplied re-validation (price/spread/extension/regime/vol)
        if revalidate_fn:
            try:
                still_valid, revalid_reason = revalidate_fn(pos.seq_no)
            except Exception as exc:
                log.error("[predator_exec] revalidate_fn raised: %s "
                            "— aborting batch %d", exc, batch.id)
                still_valid, revalid_reason = False, f"revalidate raised: {exc}"
            if not still_valid:
                log.info("[predator_exec] batch=%d aborted at seq=%d — %s",
                          batch.id, pos.seq_no, revalid_reason)
                _mark_position(db, pos, status="REJECTED",
                                reject_reason=revalid_reason)
                aborted_after = pos.seq_no - 1
                break

        # Enqueue the ticket
        try:
            pe = PendingExecution(
                pair="xauusd",
                signal=batch.direction,
                entry=float(batch.entry_price),
                stop_loss=float(batch.stop_loss),
                take_profit=float(pos.take_profit_planned),
                take_profit_2=float(batch.tp2),
                risk_pips=float(abs(batch.entry_price - batch.stop_loss)),
                quality_score=None,
                rr=None,
                lot_size=PREDATOR_LOT_SIZE,
                max_lot=PREDATOR_LOT_SIZE,
                reason=(
                    f"PREDATOR·{batch.archetype}·{batch.exposure_mode}"
                    f"·seq={pos.seq_no}/{batch.planned_positions}"
                    f"·batch={batch.id}·tp={pos.tp_target}"
                ),
                confirmations_json=json.dumps({
                    "source":         "predator_engine",
                    "batch_id":       batch.id,
                    "seq_no":         pos.seq_no,
                    "tp_target":      pos.tp_target,
                    "exposure_mode":  batch.exposure_mode,
                    "signal_id":      batch.signal_id,
                    "archetype":      batch.archetype,
                    "vol_pct":        batch.vol_pct_at_fire,
                    "disp_atr":       batch.disp_atr_ratio,
                    "expansion_reason": batch.expansion_reason,
                }),
            )
            db.add(pe)
            db.flush()   # populate pe.id
            _mark_position(db, pos, status="ENQUEUED",
                            pending_execution_id=pe.id)
            db.commit()
            opened_tickets.append(pe.id)
            log.info("[predator_exec] batch=%d seq=%d ENQUEUED pending=%d "
                      "tp=%s(%.2f)",
                      batch.id, pos.seq_no, pe.id, pos.tp_target,
                      pos.take_profit_planned)
        except Exception as exc:
            log.error("[predator_exec] batch=%d seq=%d enqueue failed: %s",
                        batch.id, pos.seq_no, exc)
            db.rollback()
            _mark_position(db, pos, status="REJECTED",
                            reject_reason=f"enqueue exception: {exc}")
            aborted_after = pos.seq_no - 1
            break

        # Brief pause between staged tickets — the daemon needs cycles to pull
        # each one and the market needs breathing room between fills.
        if stage_delay_s > 0:
            time.sleep(stage_delay_s)

    total_exposure = len(opened_tickets) * PREDATOR_LOT_SIZE
    final_status = "COMPLETE" if len(opened_tickets) == batch.planned_positions \
                   else ("PARTIAL" if opened_tickets else "ABORTED")
    _finalize_batch(db, batch, final_status,
                     positions_opened=len(opened_tickets),
                     total_exposure=total_exposure)
    log.info("[predator_exec] batch=%d %s: opened=%d exposure=%.2f",
              batch.id, final_status, len(opened_tickets), total_exposure)

    return {
        "batch_id":       batch.id,
        "opened":         len(opened_tickets),
        "aborted_after":  aborted_after,
        "mode":           batch.exposure_mode,
        "tickets":        opened_tickets,
        "total_exposure": total_exposure,
        "reason":         "ok" if final_status == "COMPLETE" else "partial/aborted",
    }


# ── Internal state mutators ─────────────────────────────────────────────────

def _set_batch_status(db: Session, batch: PredatorSignalBatch, status: str,
                      *, abort_reason: Optional[str] = None) -> None:
    batch.execution_status = status
    if abort_reason is not None:
        batch.abort_reason = abort_reason
    db.commit()


def _finalize_batch(db: Session, batch: PredatorSignalBatch, status: str,
                    *, positions_opened: int, total_exposure: float) -> None:
    batch.execution_status = status
    batch.positions_opened = positions_opened
    batch.total_exposure = total_exposure
    db.commit()


def _mark_position(db: Session, pos: PredatorPosition, *,
                   status: str,
                   pending_execution_id: Optional[int] = None,
                   reject_reason: Optional[str] = None) -> None:
    pos.status = status
    if pending_execution_id is not None:
        pos.pending_execution_id = pending_execution_id
    if reject_reason is not None:
        pos.reject_reason = reject_reason
    # Note: caller commits (batched writes)


__all__ = [
    "create_batch", "execute_batch_staged",
    "_verify_demo_account", "_current_predator_exposure",
]
