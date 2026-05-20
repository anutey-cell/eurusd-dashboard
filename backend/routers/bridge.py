"""
MT5 Bridge API
==============

The VPS produces signed BUY/SELL orders when all 3 confirmation layers
agree. The Windows laptop runs `deploy/mt5_bridge_daemon.py` which polls
this API, executes orders on MetaTrader5 locally, and reports results back.

This architecture lets the dashboard run 24/7 on Linux (where MT5 doesn't
work) while keeping real broker execution on the user's Windows laptop.

Endpoints
---------
GET    /api/v1/bridge/pending-orders   List PENDING orders (laptop polls this)
POST   /api/v1/bridge/claim/{id}       Atomically claim an order (set EXECUTING)
POST   /api/v1/bridge/result/{id}      Report execution result (ACCEPTED/REJECTED/FAILED)
GET    /api/v1/bridge/health           Bridge daemon heartbeat (laptop pings this)
GET    /api/v1/bridge/status           Operator-facing: queue depth, last claim, etc.

Auth
----
Every write endpoint requires header `X-Bridge-Secret: <MT5_BRIDGE_SHARED_SECRET>`.
The bridge daemon includes it on every call. Read endpoints (status, health)
are open so the dashboard UI can show queue depth.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from config import settings
from database import get_db
from db_models import PendingExecution
from models.common import APIResponse
from rate_limit import limiter

log = logging.getLogger(__name__)
router = APIRouter(prefix="/bridge", tags=["bridge"])

# Orders that haven't been claimed within this window are auto-expired by the
# scheduler. Keeps the queue lean if the laptop is offline.
PENDING_TTL_SECONDS = 300   # 5 minutes


# ── Auth dependency ──────────────────────────────────────────────────────────

def _require_bridge_secret(x_bridge_secret: str | None = Header(default=None)) -> None:
    expected = (settings.mt5_bridge_shared_secret or "").strip()
    if not expected:
        raise HTTPException(
            status_code=503,
            detail="MT5_BRIDGE_SHARED_SECRET not configured on server.",
        )
    if not x_bridge_secret or x_bridge_secret != expected:
        raise HTTPException(
            status_code=401,
            detail="Invalid or missing X-Bridge-Secret header.",
        )


# ── Schemas ──────────────────────────────────────────────────────────────────

class ExecutionResult(BaseModel):
    """Reported by the bridge daemon after attempting the order."""
    status:       str   = Field(..., description="ACCEPTED | REJECTED | FAILED")
    ticket:       Optional[int]    = Field(default=None)
    lot_executed: Optional[float]  = Field(default=None)
    error:        Optional[str]    = Field(default=None, description="Human-readable failure reason")


# ── Helpers ──────────────────────────────────────────────────────────────────

def _serialise(row: PendingExecution) -> dict:
    return {
        "id":            row.id,
        "createdAt":     row.created_at.isoformat() if row.created_at else None,
        "expiresAt":     row.expires_at.isoformat() if row.expires_at else None,
        "pair":          row.pair,
        "signal":        row.signal,
        "entry":         row.entry,
        "stopLoss":      row.stop_loss,
        "takeProfit":    row.take_profit,
        "riskPips":      row.risk_pips,
        "qualityScore":  row.quality_score,
        "rr":            row.rr,
        "maxLot":        row.max_lot,
        "reason":        row.reason,
        "confirmations": json.loads(row.confirmations_json) if row.confirmations_json else None,
        "status":        row.status,
        "claimedAt":     row.claimed_at.isoformat()  if row.claimed_at else None,
        "claimedBy":     row.claimed_by,
        "resolvedAt":    row.resolved_at.isoformat() if row.resolved_at else None,
        "ticket":        row.ticket,
        "lotExecuted":   row.lot_executed,
        "executionError": row.execution_error,
    }


# ── Endpoints ────────────────────────────────────────────────────────────────

@router.get(
    "/pending-orders",
    response_model=APIResponse[dict],
    summary="List PENDING orders (laptop bridge polls this)",
)
@limiter.limit("120/minute")
def pending_orders(
    request: Request,
    _: None = Depends(_require_bridge_secret),
    db: Session = Depends(get_db),
) -> APIResponse[dict]:
    # Auto-expire stale orders
    now = datetime.now(timezone.utc)
    stale = (
        db.query(PendingExecution)
        .filter(PendingExecution.status == "PENDING")
        .filter(PendingExecution.expires_at < now)
        .all()
    )
    for s in stale:
        s.status = "EXPIRED"
        s.resolved_at = now
        s.execution_error = "Not claimed within TTL"
    if stale:
        db.commit()

    rows = (
        db.query(PendingExecution)
        .filter(PendingExecution.status == "PENDING")
        .order_by(PendingExecution.created_at.asc())
        .all()
    )
    return APIResponse(
        data={
            "count":  len(rows),
            "orders": [_serialise(r) for r in rows],
            "now":    now.isoformat(),
        },
        source="mt5_bridge",
    )


@router.post(
    "/claim/{order_id}",
    response_model=APIResponse[dict],
    summary="Claim a pending order (transition PENDING -> EXECUTING)",
)
@limiter.limit("60/minute")
def claim_order(
    request: Request,
    order_id: int,
    bridge_daemon_id: str = Header(default="unknown", alias="X-Bridge-Daemon-Id"),
    _: None = Depends(_require_bridge_secret),
    db: Session = Depends(get_db),
) -> APIResponse[dict]:
    row = db.query(PendingExecution).filter(PendingExecution.id == order_id).first()
    if row is None:
        raise HTTPException(status_code=404, detail="Order not found")
    if row.status != "PENDING":
        raise HTTPException(status_code=409, detail=f"Order is {row.status}, not PENDING")

    row.status      = "EXECUTING"
    row.claimed_at  = datetime.now(timezone.utc)
    row.claimed_by  = bridge_daemon_id
    db.commit()
    db.refresh(row)
    log.info("[bridge] order %d claimed by %s", order_id, bridge_daemon_id)
    return APIResponse(data=_serialise(row), source="mt5_bridge")


@router.post(
    "/result/{order_id}",
    response_model=APIResponse[dict],
    summary="Report execution result for a claimed order",
)
@limiter.limit("60/minute")
def report_result(
    request: Request,
    order_id: int,
    result: ExecutionResult,
    _: None = Depends(_require_bridge_secret),
    db: Session = Depends(get_db),
) -> APIResponse[dict]:
    row = db.query(PendingExecution).filter(PendingExecution.id == order_id).first()
    if row is None:
        raise HTTPException(status_code=404, detail="Order not found")
    if row.status not in ("EXECUTING", "PENDING"):
        raise HTTPException(
            status_code=409,
            detail=f"Cannot report result on {row.status} order",
        )

    valid = {"ACCEPTED", "REJECTED", "FAILED"}
    if result.status not in valid:
        raise HTTPException(status_code=422, detail=f"status must be one of {valid}")

    row.status         = result.status
    row.resolved_at    = datetime.now(timezone.utc)
    row.ticket         = result.ticket
    row.lot_executed   = result.lot_executed
    row.execution_error = result.error
    db.commit()
    db.refresh(row)

    log.info(
        "[bridge] order %d resolved status=%s ticket=%s lot=%s err=%s",
        order_id, result.status, result.ticket, result.lot_executed, result.error,
    )

    # On ACCEPTED, also write to MT5TradeLog so the analytics + daily counter
    # are consistent with what they'd have seen if MT5 ran locally.
    if result.status == "ACCEPTED":
        try:
            from db_models import MT5TradeLog
            from config import settings as _cfg
            db.add(MT5TradeLog(
                mode="live" if _cfg.live_trading_authorized else "demo",
                pair=row.pair,
                broker_symbol="XAUUSD",
                signal=row.signal,
                order_type="MARKET",
                volume=float(result.lot_executed or 0),
                entry=row.entry,
                stop_loss=row.stop_loss,
                take_profit=row.take_profit,
                risk_percent=None,
                risk_amount=None,
                spread=None,
                ticket=result.ticket,
                status="accepted",
                rejection_reason=None,
                reason=f"BRIDGE | {row.reason or ''}",
                raw_response_json=None,
            ))
            db.commit()
        except Exception as exc:
            log.warning("[bridge] MT5TradeLog mirror failed: %s", exc)

    # Fire Telegram alert for the operator
    try:
        from services.telegram_service import send_text_alert
        if result.status == "ACCEPTED":
            send_text_alert(
                text=(
                    f"<b>BRIDGE ACCEPTED</b>\n"
                    f"{row.signal} XAU/USD · lot {result.lot_executed}\n"
                    f"Entry {row.entry} · SL {row.stop_loss} · TP {row.take_profit}\n"
                    f"Ticket #{result.ticket}"
                ),
            )
        elif result.status == "REJECTED":
            send_text_alert(
                text=(
                    f"<b>BRIDGE REJECTED</b>\n"
                    f"{row.signal} XAU/USD · order #{order_id}\n"
                    f"Reason: {result.error or 'unknown'}"
                ),
            )
        else:
            send_text_alert(
                text=(
                    f"<b>BRIDGE FAILED</b>\n"
                    f"{row.signal} XAU/USD · order #{order_id}\n"
                    f"Error: {result.error or 'unknown'}"
                ),
            )
    except Exception:
        pass

    return APIResponse(data=_serialise(row), source="mt5_bridge")


@router.get(
    "/health",
    response_model=APIResponse[dict],
    summary="Bridge daemon heartbeat (laptop pings every minute)",
)
@limiter.limit("120/minute")
def bridge_health(
    request: Request,
    bridge_daemon_id: str = Header(default="unknown", alias="X-Bridge-Daemon-Id"),
    _: None = Depends(_require_bridge_secret),
) -> APIResponse[dict]:
    # Just record the heartbeat in memory; the /status endpoint reads it
    _BRIDGE_HEARTBEAT[bridge_daemon_id] = datetime.now(timezone.utc)
    return APIResponse(data={"ok": True, "now": datetime.now(timezone.utc).isoformat()})


# Module-level heartbeat cache (single-process VPS — fine for our scale)
_BRIDGE_HEARTBEAT: dict[str, datetime] = {}


@router.get(
    "/status",
    response_model=APIResponse[dict],
    summary="Bridge queue depth + last claim + daemon heartbeats (no auth)",
)
@limiter.limit("60/minute")
def bridge_status(
    request: Request,
    db: Session = Depends(get_db),
) -> APIResponse[dict]:
    now = datetime.now(timezone.utc)
    by_status = {}
    for st in ("PENDING", "EXECUTING", "ACCEPTED", "REJECTED", "FAILED", "EXPIRED"):
        by_status[st] = db.query(PendingExecution).filter(PendingExecution.status == st).count()

    last_pending = (
        db.query(PendingExecution)
        .filter(PendingExecution.status == "PENDING")
        .order_by(PendingExecution.created_at.desc())
        .first()
    )
    last_accepted = (
        db.query(PendingExecution)
        .filter(PendingExecution.status == "ACCEPTED")
        .order_by(PendingExecution.resolved_at.desc())
        .first()
    )

    heartbeats = {
        daemon_id: {
            "lastSeen":  ts.isoformat(),
            "ageSeconds": int((now - ts).total_seconds()),
            "isFresh":    (now - ts).total_seconds() < 120,
        }
        for daemon_id, ts in _BRIDGE_HEARTBEAT.items()
    }

    return APIResponse(
        data={
            "config": {
                "enabled":      settings.mt5_bridge_enabled,
                "secretSet":    bool(settings.mt5_bridge_shared_secret),
                "ttlSeconds":   PENDING_TTL_SECONDS,
            },
            "queue": by_status,
            "lastPending":  _serialise(last_pending)  if last_pending  else None,
            "lastAccepted": _serialise(last_accepted) if last_accepted else None,
            "daemons":      heartbeats,
            "anyDaemonFresh": any(h["isFresh"] for h in heartbeats.values()),
            "now": now.isoformat(),
        },
        source="mt5_bridge",
    )
