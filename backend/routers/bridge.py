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
from sqlalchemy.exc import IntegrityError
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
    """
    Accepts the primary shared secret, or (during a rotation window) a previous
    one via MT5_BRIDGE_SHARED_SECRET_PREV. Set the prev var while rolling out a
    new secret to the daemon; clear it once the daemon is confirmed on the new
    value. Fail-safe: neither set → 503.
    """
    current = (settings.mt5_bridge_shared_secret or "").strip()
    prev    = (getattr(settings, "mt5_bridge_shared_secret_prev", "") or "").strip()
    if not current and not prev:
        raise HTTPException(
            status_code=503,
            detail="MT5_BRIDGE_SHARED_SECRET not configured on server.",
        )
    if not x_bridge_secret:
        raise HTTPException(
            status_code=401,
            detail="Missing X-Bridge-Secret header.",
        )
    if x_bridge_secret != current and (not prev or x_bridge_secret != prev):
        raise HTTPException(
            status_code=401,
            detail="Invalid X-Bridge-Secret header.",
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

    # CLOSED = the daemon is reporting the position's terminal outcome (post-fill)
    # ACCEPTED = order was filled by MT5 but trade is still open (initial fill report)
    valid = {"ACCEPTED", "REJECTED", "FAILED", "CLOSED"}
    if result.status not in valid:
        raise HTTPException(status_code=422, detail=f"status must be one of {valid}")

    # Allowed transitions:
    #   PENDING/EXECUTING → any of {ACCEPTED, REJECTED, FAILED}   (initial outcome)
    #   ACCEPTED          → CLOSED                                  (post-fill close)
    # Anything else rejects with 409. The two-stage check below is the
    # ONLY place that decides whether to accept the report — there was a
    # redundant earlier check here that blocked ACCEPTED→CLOSED before
    # this logic ran.
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

    # ── Predator post-trade writeback to predator_positions ──────────────
    # ACCEPTED  → position OPEN (mt5_ticket + opened_at)
    # CLOSED    → position CLOSED (closed_at + realized_pts/mfe/mae/outcome)
    # REJECTED/FAILED/EXPIRED → position REJECTED (with reason)
    # Without this, exposure ghosts accumulate and PREDATOR_EXPOSURE_LIMIT_BLOCK
    # blocks legitimate new batches.
    try:
        from db_models import PredatorPosition
        pp = (
            db.query(PredatorPosition)
            .filter(PredatorPosition.pending_execution_id == row.id)
            .first()
        )
        if pp is not None:
            now_utc = datetime.now(timezone.utc)
            if result.status == "ACCEPTED":
                pp.status = "OPEN"
                if result.ticket is not None:
                    pp.mt5_ticket = result.ticket
                if pp.opened_at is None:
                    pp.opened_at = now_utc
            elif result.status == "CLOSED":
                pp.status = "CLOSED"
                pp.closed_at = now_utc
                if result.ticket is not None:
                    pp.mt5_ticket = result.ticket
                if result.pips_outcome is not None:
                    pp.realized_pts = float(result.pips_outcome)
                if result.mfe_pts is not None:
                    pp.mfe_pts = float(result.mfe_pts)
                if result.mae_pts is not None:
                    pp.mae_pts = float(result.mae_pts)
                if result.result:
                    # Map WIN/LOSS/BREAKEVEN → TP/SL/BE (predator uses TP1|TP2|SL|MANUAL|TIMEOUT)
                    outcome_map = {"WIN": pp.tp_target,   # TP1 or TP2 — depends on pos
                                    "LOSS": "SL",
                                    "BREAKEVEN": "MANUAL"}
                    pp.outcome = outcome_map.get(result.result.upper(), result.result)
            elif result.status in ("REJECTED", "FAILED", "EXPIRED"):
                pp.status = "REJECTED"
                pp.reject_reason = (result.error or f"bridge status={result.status}")[:255]
            db.commit()

            # Also update batch-level counters when a position closes so
            # positions_opened / total_exposure reflect reality.
            if result.status in ("CLOSED", "REJECTED", "FAILED", "EXPIRED"):
                from db_models import PredatorSignalBatch
                from sqlalchemy import text as _text
                agg = db.execute(_text(
                    "SELECT COUNT(*) AS n_open, COALESCE(SUM(lot_size),0) AS lots "
                    "FROM predator_positions "
                    "WHERE batch_id=:bid AND status IN ('ENQUEUED','OPEN')"
                ), {"bid": pp.batch_id}).fetchone()
                batch = db.query(PredatorSignalBatch).filter(
                    PredatorSignalBatch.id == pp.batch_id
                ).first()
                if batch is not None:
                    batch.positions_opened = int(agg[0] or 0)
                    batch.total_exposure = float(agg[1] or 0.0)
                    db.commit()
    except Exception as exc:
        log.warning("[bridge] predator_positions post-trade writeback failed: %s", exc)

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
    mt5_equity:           str | None = Header(default=None, alias="X-MT5-Equity"),
    # Open-position snapshot — drives the position-cap risk gate
    mt5_open_positions:   str | None = Header(default=None, alias="X-MT5-Open-Positions"),
    mt5_open_tickets:     str | None = Header(default=None, alias="X-MT5-Open-Tickets"),
    mt5_floating_pnl:     str | None = Header(default=None, alias="X-MT5-Floating-PnL"),
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

    def _as_int(s: str | None) -> int | None:
        try: return int(s) if s else None
        except Exception: return None

    open_tickets: list[int] = []
    if mt5_open_tickets:
        for t in mt5_open_tickets.split(","):
            try: open_tickets.append(int(t.strip()))
            except ValueError: pass

    _MT5_TERMINAL_STATE[bridge_daemon_id] = {
        "trade_allowed":          _as_bool(mt5_trade_allowed),
        "dlls_allowed":           _as_bool(mt5_dlls_allowed),
        "connected":              _as_bool(mt5_connected),
        "company":                mt5_company,
        "account_login":          mt5_account_login,
        "account_server":         mt5_account_server,
        "account_demo":           _as_bool(mt5_account_demo),
        "balance":                _as_float(mt5_balance),
        "equity":                 _as_float(mt5_equity),
        "open_positions_count":   _as_int(mt5_open_positions),
        "open_position_tickets":  open_tickets,
        "floating_pnl":           _as_float(mt5_floating_pnl),
        "last_seen":              now,
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
                "tradeAllowed":         mt5_state.get("trade_allowed"),
                "dllsAllowed":          mt5_state.get("dlls_allowed"),
                "connected":            mt5_state.get("connected"),
                "company":              mt5_state.get("company"),
                "accountLogin":         mt5_state.get("account_login"),
                "accountServer":        mt5_state.get("account_server"),
                "accountDemo":          mt5_state.get("account_demo"),
                "balance":              mt5_state.get("balance"),
                "equity":               mt5_state.get("equity"),
                "openPositionsCount":   mt5_state.get("open_positions_count"),
                "openPositionTickets":  mt5_state.get("open_position_tickets"),
                "floatingPnl":          mt5_state.get("floating_pnl"),
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


# ── MT5 candle push (daemon → droplet) ──────────────────────────────────────
#
# The daemon on the operator's laptop calls mt5.copy_rates_from_pos() every
# 30s and POSTs the batch here. Bars are upserted into historical_candles
# with source='mt5'. This is the free-tier primary candle source, matching
# the daemon-push architecture the tick pipeline already uses. Never trades.

class MT5CandleRecord(BaseModel):
    """Single OHLCV bar as reported by MT5. Timestamps must already be UTC."""
    time:         str   = Field(..., description="ISO-8601 UTC")
    open:         float
    high:         float
    low:          float
    close:        float
    tick_volume:  Optional[int]  = Field(default=0)
    spread:       Optional[int]  = Field(default=None, description="MT5 raw spread, points")
    real_volume:  Optional[int]  = Field(default=None)


class MT5CandleBatch(BaseModel):
    """Batch of bars for a single (symbol, timeframe) pushed by the daemon."""
    symbol:     str  = Field(..., description="MT5 broker symbol (e.g. XAUUSD)")
    timeframe:  str  = Field(..., description="M5 | M15 | H1 | H4 | D1")
    count:      int
    candles:    list[MT5CandleRecord]


_MT5_ACCEPTED_TIMEFRAMES = {"M5", "M15", "H1", "H4", "D1"}
_MT5_STORED_INSTRUMENT   = "XAU/USD"    # match historical_candles.instrument convention


@router.post(
    "/candles/receive",
    response_model=APIResponse[dict],
    summary="Receive OHLCV bars pushed by the local MT5 daemon (free-tier primary source)",
)
@limiter.limit("120/minute")
def receive_mt5_candles(
    request: Request,
    batch: MT5CandleBatch,
    bridge_daemon_id: str = Header(default="unknown", alias="X-Bridge-Daemon-Id"),
    _: None = Depends(_require_bridge_secret),
    db: Session = Depends(get_db),
) -> APIResponse[dict]:
    """
    Upsert a batch of MT5 candles into historical_candles. The daemon pushes
    every 30s per timeframe. Bars are idempotent via the unique index on
    (instrument, timeframe, candle_time) — duplicates are silently skipped.

    Guardrails:
      - Symbol MUST be XAUUSD (matches the demo mandate)
      - Timeframe MUST be in {M5, M15, H1, H4, D1}
      - Batch size capped at 500 bars (200 is the typical daemon payload)
      - Timestamps MUST already be UTC when pushed
    """
    from db_models import HistoricalCandle

    if (batch.symbol or "").upper() != "XAUUSD":
        raise HTTPException(status_code=400,
            detail=f"Symbol must be XAUUSD, got {batch.symbol!r}")
    if batch.timeframe not in _MT5_ACCEPTED_TIMEFRAMES:
        raise HTTPException(status_code=400,
            detail=f"Timeframe must be one of {_MT5_ACCEPTED_TIMEFRAMES}, got {batch.timeframe!r}")
    if len(batch.candles) > 500:
        raise HTTPException(status_code=400,
            detail=f"Batch too large ({len(batch.candles)} > 500)")

    inserted = 0
    duplicates = 0
    errors = 0
    latest_ts: Optional[datetime] = None

    for c in batch.candles:
        try:
            ts = datetime.fromisoformat(c.time.replace("Z", "+00:00"))
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            row = HistoricalCandle(
                instrument=_MT5_STORED_INSTRUMENT,
                timeframe=batch.timeframe,
                candle_time=ts,
                open=float(c.open),
                high=float(c.high),
                low=float(c.low),
                close=float(c.close),
                volume=int(c.tick_volume or 0),
                source="mt5",
            )
            db.add(row)
            db.commit()
            inserted += 1
            if latest_ts is None or ts > latest_ts:
                latest_ts = ts
        except IntegrityError:
            db.rollback()
            duplicates += 1
        except Exception as exc:
            db.rollback()
            errors += 1
            if errors <= 3:
                log.warning("[bridge/candles/receive] insert failed for %s %s @ %s: %s",
                              batch.symbol, batch.timeframe, c.time, exc)

    if inserted:
        log.info("[bridge/candles/receive] %s %s [%s]: inserted=%d dup=%d errors=%d latest=%s",
                    batch.symbol, batch.timeframe, bridge_daemon_id,
                    inserted, duplicates, errors,
                    latest_ts.isoformat() if latest_ts else None)

    return APIResponse(
        data={
            "accepted":   inserted,
            "duplicates": duplicates,
            "errors":     errors,
            "latest":     latest_ts.isoformat() if latest_ts else None,
            "symbol":     batch.symbol,
            "timeframe":  batch.timeframe,
        },
        source="mt5_bridge",
    )


# ── Track A — MT5 tick capture (research-only ingestion) ─────────────────────
#
# Daemon uses mt5.copy_ticks_from() to retrieve every broker tick since the
# last confirmed cursor and POSTs the batch here. Nothing in production
# trading depends on this dataset. See design brief 2026-09-03.

class MT5TickRecord(BaseModel):
    """One broker tick, fields preserved verbatim from MetaTrader5."""
    time_msc:      int                   = Field(..., description="ms since Unix epoch, UTC")
    bid:           float
    ask:           float
    last:          Optional[float]       = Field(default=None)
    volume_real:   Optional[float]       = Field(default=None)
    flags:         int                   = Field(default=0)


class MT5TickBatch(BaseModel):
    """Batch of ticks for a single symbol."""
    symbol:        str                   = Field(..., description="broker symbol as MT5 returns it, e.g. XAUUSD")
    broker:        str                   = Field(default="exness")
    account:       str                   = Field(default="unknown")
    count:         int
    ticks:         list[MT5TickRecord]


class MT5TickGapReport(BaseModel):
    """Explicit acknowledgement of an unrecoverable data gap from the daemon."""
    symbol:        str
    start_msc:     int
    end_msc:       int
    reason:        str                   = Field(..., description="e.g. mt5_disconnect | mt5_returned_empty | daemon_offline")
    detail:        Optional[str]         = Field(default=None)


def _tick_content_hash(rec: MT5TickRecord) -> str:
    import hashlib
    payload = (
        f"{rec.time_msc}|{rec.bid:.6f}|{rec.ask:.6f}"
        f"|{'' if rec.last is None else f'{rec.last:.6f}'}"
        f"|{'' if rec.volume_real is None else f'{rec.volume_real:.6f}'}"
        f"|{rec.flags}"
    )
    return hashlib.sha1(payload.encode("ascii")).hexdigest()[:24]


@router.post(
    "/ticks/receive",
    response_model=APIResponse[dict],
    summary="[TRACK A — RESEARCH ONLY] Receive raw MT5 broker ticks. No production consumer.",
)
@limiter.limit("240/minute")
def receive_mt5_ticks(
    request: Request,
    batch: MT5TickBatch,
    bridge_daemon_id: str = Header(default="unknown", alias="X-Bridge-Daemon-Id"),
    _: None = Depends(_require_bridge_secret),
    db: Session = Depends(get_db),
) -> APIResponse[dict]:
    from db_models import MT5Tick
    from datetime import datetime as _dt, timezone as _tz

    inserted = 0
    duplicates = 0
    errors = 0
    latest_msc: int | None = None

    for t in batch.ticks:
        try:
            content_hash = _tick_content_hash(t)
            row = MT5Tick(
                tick_time_msc=int(t.time_msc),
                tick_time_utc=_dt.fromtimestamp(t.time_msc / 1000.0, tz=_tz.utc),
                symbol=batch.symbol,
                bid=float(t.bid),
                ask=float(t.ask),
                last=(None if t.last is None else float(t.last)),
                volume_real=(None if t.volume_real is None else float(t.volume_real)),
                flags=int(t.flags),
                content_hash=content_hash,
                broker=batch.broker,
                account=batch.account,
                daemon_id=bridge_daemon_id,
            )
            db.add(row)
            db.commit()
            inserted += 1
            if latest_msc is None or t.time_msc > latest_msc:
                latest_msc = t.time_msc
        except Exception as exc:
            db.rollback()
            # Uniqueness violation on (symbol, content_hash) → duplicate
            if "unique" in str(exc).lower() or "UNIQUE" in str(exc):
                duplicates += 1
            else:
                errors += 1
                if errors <= 3:
                    log.warning("[bridge/ticks/receive] insert failed: %s", exc)

    if inserted or duplicates or errors:
        log.info("[bridge/ticks/receive] %s [%s]: inserted=%d dup=%d err=%d latest_msc=%s",
                  batch.symbol, bridge_daemon_id, inserted, duplicates, errors, latest_msc)

    return APIResponse(
        data={
            "accepted":   inserted,
            "duplicates": duplicates,
            "errors":     errors,
            "latest_msc": latest_msc,
            "symbol":     batch.symbol,
        },
        source="mt5_bridge_ticks",
    )


@router.post(
    "/ticks/gap",
    response_model=APIResponse[dict],
    summary="[TRACK A — RESEARCH ONLY] Record an unrecoverable tick data gap.",
)
@limiter.limit("30/minute")
def report_mt5_tick_gap(
    request: Request,
    gap: MT5TickGapReport,
    bridge_daemon_id: str = Header(default="unknown", alias="X-Bridge-Daemon-Id"),
    _: None = Depends(_require_bridge_secret),
    db: Session = Depends(get_db),
) -> APIResponse[dict]:
    from db_models import MT5TickGap
    from datetime import datetime as _dt, timezone as _tz

    try:
        row = MT5TickGap(
            symbol=gap.symbol,
            start_msc=int(gap.start_msc),
            end_msc=int(gap.end_msc),
            start_utc=_dt.fromtimestamp(gap.start_msc / 1000.0, tz=_tz.utc),
            end_utc=_dt.fromtimestamp(gap.end_msc / 1000.0, tz=_tz.utc),
            reason=gap.reason,
            detail=gap.detail,
            daemon_id=bridge_daemon_id,
        )
        db.add(row)
        db.commit()
        log.warning("[bridge/ticks/gap] %s [%s]: %d → %d reason=%s",
                    gap.symbol, bridge_daemon_id, gap.start_msc, gap.end_msc, gap.reason)
        return APIResponse(data={"recorded": True, "id": row.id}, source="mt5_bridge_ticks")
    except Exception as exc:
        db.rollback()
        log.warning("[bridge/ticks/gap] insert failed: %s", exc)
        return APIResponse(data={"recorded": False, "error": str(exc)[:120]},
                            source="mt5_bridge_ticks")
