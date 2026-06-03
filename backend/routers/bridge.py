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
    """
    Reported by the bridge daemon after attempting the order. Carries the
    post-trade learning fields the mandate requires (MFE, MAE, result).
    """
    status:         str            = Field(..., description="ACCEPTED | REJECTED | FAILED | CLOSED")
    ticket:         Optional[int]   = Field(default=None)
    lot_executed:   Optional[float] = Field(default=None)
    error:          Optional[str]   = Field(default=None)

    # Mandate post-trade fields (filled when the position closes) ─────────
    result:         Optional[str]   = Field(default=None, description="WIN | LOSS | BREAKEVEN")
    pips_outcome:   Optional[float] = Field(default=None)
    mfe_pts:        Optional[float] = Field(default=None, description="Max Favorable Excursion")
    mae_pts:        Optional[float] = Field(default=None, description="Max Adverse Excursion")
    rules_followed: Optional[bool]  = Field(default=None)
    post_trade_note: Optional[str]  = Field(default=None)


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
        "takeProfit":    row.take_profit,                          # = TP1 (initial target)
        "takeProfit2":   getattr(row, "take_profit_2", None),      # TP2 (BE-trigger / stretch)
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

    # CLOSED = the daemon is reporting the position's terminal outcome (post-fill)
    # ACCEPTED = order was filled by MT5 but trade is still open (initial fill report)
    valid = {"ACCEPTED", "REJECTED", "FAILED", "CLOSED"}
    if result.status not in valid:
        raise HTTPException(status_code=422, detail=f"status must be one of {valid}")

    # Don't overwrite a terminal status with another partial. CLOSED can transition
    # from ACCEPTED; everything else is one-shot.
    if row.status == "ACCEPTED" and result.status == "CLOSED":
        pass    # legitimate transition to terminal outcome
    elif row.status not in ("EXECUTING", "PENDING"):
        raise HTTPException(
            status_code=409,
            detail=f"Cannot report result on {row.status} order",
        )

    row.status         = result.status
    row.resolved_at    = datetime.now(timezone.utc)
    row.ticket         = result.ticket or row.ticket
    row.lot_executed   = result.lot_executed or row.lot_executed
    if result.error:
        row.execution_error = result.error
    db.commit()
    db.refresh(row)

    log.info(
        "[bridge] order %d resolved status=%s ticket=%s lot=%s mfe=%s mae=%s result=%s err=%s",
        order_id, result.status, result.ticket, result.lot_executed,
        result.mfe_pts, result.mae_pts, result.result, result.error,
    )

    # ── Mandate post-trade writeback to strategist_verdicts ──────────────
    # Back-link via pending_execution_id so the learning curve sees outcome.
    try:
        from db_models import StrategistVerdict
        sv = (
            db.query(StrategistVerdict)
            .filter(StrategistVerdict.pending_execution_id == row.id)
            .order_by(StrategistVerdict.created_at.desc())
            .first()
        )
        if sv is not None:
            if result.ticket is not None:
                sv.mt5_ticket = result.ticket
            if result.result:                sv.result        = result.result
            if result.pips_outcome is not None: sv.pips_outcome = result.pips_outcome
            if result.mfe_pts is not None:   sv.mfe_pts       = result.mfe_pts
            if result.mae_pts is not None:   sv.mae_pts       = result.mae_pts
            if result.rules_followed is not None: sv.rules_followed = 1 if result.rules_followed else 0
            if result.post_trade_note:       sv.post_trade_note = result.post_trade_note
            db.commit()
    except Exception as exc:
        log.warning("[bridge] strategist_verdicts post-trade writeback failed: %s", exc)

    # On ACCEPTED, also write to MT5TradeLog so the analytics + daily counter
    # are consistent with what they'd have seen if MT5 ran locally.
    if result.status == "ACCEPTED":
        try:
            from db_models import MT5TradeLog
            db.add(MT5TradeLog(
                # MANDATE: bridge-driven trades are demo-only. Live execution is
                # hard-disabled in the strategist regardless of LIVE_TRADING_AUTHORIZED.
                mode="demo",
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
    "/unresolved-fills",
    response_model=APIResponse[dict],
    summary="ACCEPTED orders whose trade outcome was never reported back",
)
@limiter.limit("60/minute")
def unresolved_fills(
    request: Request,
    max_age_hours: int = 72,
    _: None = Depends(_require_bridge_secret),
    db: Session = Depends(get_db),
) -> APIResponse[dict]:
    """
    Returns PendingExecution rows where status=ACCEPTED but the linked
    strategist_verdict still has result=PENDING (no CLOSED writeback).
    The daemon polls this on startup to find orphaned trades — those
    whose monitor thread died with a previous daemon process.
    """
    from db_models import StrategistVerdict
    cutoff = datetime.now(timezone.utc) - timedelta(hours=max_age_hours)
    rows = (
        db.query(PendingExecution)
        .filter(PendingExecution.status == "ACCEPTED")
        .filter(PendingExecution.created_at >= cutoff)
        .order_by(PendingExecution.created_at.desc())
        .all()
    )
    orphans = []
    for r in rows:
        sv = (
            db.query(StrategistVerdict)
            .filter(StrategistVerdict.pending_execution_id == r.id)
            .first()
        )
        # Skip if outcome already recorded
        if sv and sv.result and sv.result != "PENDING":
            continue
        orphans.append({
            "id":            r.id,
            "ticket":        r.ticket,
            "signal":        r.signal,
            "entry":         r.entry,
            "stop_loss":     r.stop_loss,
            "take_profit":   r.take_profit,
            "take_profit_2": getattr(r, "take_profit_2", None),
            "lot_executed":  r.lot_executed,
            "created_at":    r.created_at.isoformat() if r.created_at else None,
            "claimed_by":    r.claimed_by,
            "verdict_id":    sv.id if sv else None,
        })
    return APIResponse(data={"count": len(orphans), "orphans": orphans},
                       source="mt5_bridge")


@router.get(
    "/health",
    response_model=APIResponse[dict],
    summary="Bridge daemon heartbeat + MT5 terminal state (laptop pings every minute)",
)
@limiter.limit("120/minute")
def bridge_health(
    request: Request,
    bridge_daemon_id:     str = Header(default="unknown", alias="X-Bridge-Daemon-Id"),
    # MT5 terminal state — daemon sends these so we know AutoTrading status
    # without waiting for an order to fail with retcode=10027.
    mt5_trade_allowed:    str | None = Header(default=None, alias="X-MT5-Trade-Allowed"),
    mt5_dlls_allowed:     str | None = Header(default=None, alias="X-MT5-DLLs-Allowed"),
    mt5_connected:        str | None = Header(default=None, alias="X-MT5-Connected"),
    mt5_company:          str | None = Header(default=None, alias="X-MT5-Company"),
    mt5_account_login:    str | None = Header(default=None, alias="X-MT5-Account-Login"),
    mt5_account_server:   str | None = Header(default=None, alias="X-MT5-Account-Server"),
    mt5_account_demo:     str | None = Header(default=None, alias="X-MT5-Account-Demo"),
    mt5_balance:          str | None = Header(default=None, alias="X-MT5-Balance"),
    _: None = Depends(_require_bridge_secret),
) -> APIResponse[dict]:
    """Records heartbeat + MT5 terminal snapshot. /status endpoint reads both."""
    now = datetime.now(timezone.utc)
    _BRIDGE_HEARTBEAT[bridge_daemon_id] = now

    # Stash the MT5 terminal state (None for legacy daemons that don't send headers)
    def _as_bool(s: str | None) -> bool | None:
        if s is None: return None
        return s.lower() in ("true", "1", "yes")
    def _as_float(s: str | None) -> float | None:
        try: return float(s) if s else None
        except Exception: return None

    _MT5_TERMINAL_STATE[bridge_daemon_id] = {
        "trade_allowed":   _as_bool(mt5_trade_allowed),
        "dlls_allowed":    _as_bool(mt5_dlls_allowed),
        "connected":       _as_bool(mt5_connected),
        "company":         mt5_company,
        "account_login":   mt5_account_login,
        "account_server":  mt5_account_server,
        "account_demo":    _as_bool(mt5_account_demo),
        "balance":         _as_float(mt5_balance),
        "last_seen":       now,
    }
    return APIResponse(data={"ok": True, "now": now.isoformat()})


# Module-level caches (single-process VPS — fine for our scale)
_BRIDGE_HEARTBEAT:      dict[str, datetime] = {}
_MT5_TERMINAL_STATE:    dict[str, dict]     = {}


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

    heartbeats = {}
    for daemon_id, ts in _BRIDGE_HEARTBEAT.items():
        mt5_state = _MT5_TERMINAL_STATE.get(daemon_id, {})
        heartbeats[daemon_id] = {
            "lastSeen":   ts.isoformat(),
            "ageSeconds": int((now - ts).total_seconds()),
            "isFresh":    (now - ts).total_seconds() < 120,
            "mt5": {
                "tradeAllowed":   mt5_state.get("trade_allowed"),
                "dllsAllowed":    mt5_state.get("dlls_allowed"),
                "connected":      mt5_state.get("connected"),
                "company":        mt5_state.get("company"),
                "accountLogin":   mt5_state.get("account_login"),
                "accountServer":  mt5_state.get("account_server"),
                "accountDemo":    mt5_state.get("account_demo"),
                "balance":        mt5_state.get("balance"),
            } if mt5_state else None,
        }

    # Single rolled-up flag: at least one fresh daemon with AutoTrading on
    any_autotrading = any(
        h["isFresh"] and (h.get("mt5") or {}).get("tradeAllowed") is True
        for h in heartbeats.values()
    )

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
            "anyAutoTradingEnabled": any_autotrading,
            "now": now.isoformat(),
        },
        source="mt5_bridge",
    )
