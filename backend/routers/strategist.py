"""
Institutional Demo-Mandate Strategist API
=========================================

GET /api/v1/strategist/decision   — Current unified verdict (60s cached).
GET /api/v1/strategist/refresh    — Force fresh; bypasses cache.

Implements the institutional demo-execution mandate:

  • Output strict JSON containing decision (BUY/SELL/STAND ASIDE),
    conditions_passed (0-5), estimated_win_rate_range, execution_status,
    mt5_execution_object (when DEMO_TRADE_PLACED), and a full trade plan.
  • Fire Telegram alerts using the EXACT mandate format (📈 / ⚪).
    Dedupe by fingerprint with 10-min cooldown.
  • When execution_status == DEMO_TRADE_PLACED AND demo_auto_enqueue=ON,
    insert a PendingExecution row at lot=0.01 for the MT5 bridge to pick up.
  • Live execution stays hard-disabled. Lot size is always 0.01.
"""
from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from config import settings
from database import get_db
from db_models import PendingExecution
from models.common import APIResponse
from rate_limit import limiter
from services.strategist import (
    format_mandate_signal_message,
    format_mandate_stand_aside_message,
    make_decision,
    persist_verdict,
)

router = APIRouter(prefix="/strategist", tags=["strategist"])
log = logging.getLogger(__name__)

# Module-level cache (single-process VPS — fine at this scale)
_cache: dict = {"verdict": None, "cached_at": 0.0}
_CACHE_TTL = 60.0   # seconds

# Telegram dedupe — same actionable verdict within COOLDOWN_S only alerts once
_last_alert_fingerprint: str = ""
_last_alert_at:          float = 0.0
_last_standby_fingerprint: str = ""
_last_standby_at:        float = 0.0
_ALERT_COOLDOWN_S        = 600.0    # 10 min for BUY/SELL alerts
_STANDBY_COOLDOWN_S      = 3600.0   # 60 min for STAND ASIDE info alerts

# Demo-enqueue dedupe — same plan inside this window won't re-enqueue
_last_enqueue_fingerprint: str = ""
_last_enqueue_at:          float = 0.0
_ENQUEUE_COOLDOWN_S      = 600.0    # 10 min


@router.get(
    "/decision",
    response_model=APIResponse[dict],
    summary="Current institutional verdict (BUY / SELL / STAND ASIDE) + full mandate fields",
)
@limiter.limit("60/minute")
def strategist_decision(
    request: Request,
    db: Session = Depends(get_db),
) -> APIResponse[dict]:
    """
    Returns the strict mandate JSON. Cached 60s; use /strategist/refresh
    to bypass. Side-effects (Telegram alert + MT5 auto-enqueue) only fire
    on a FRESH compute, never on a cache hit.
    """
    age = time.time() - _cache.get("cached_at", 0)
    if _cache.get("verdict") and age < _CACHE_TTL:
        return APIResponse(data=_cache["verdict"], source="strategist:cached")

    verdict = make_decision(db)
    _cache["verdict"]   = verdict
    _cache["cached_at"] = time.time()

    # Side-effects run only on fresh computes (never on cache hits)
    _maybe_fire_alert(verdict)
    pending_id = _maybe_enqueue_demo_order(db, verdict)

    # Mandate logging requirement: persist every fresh verdict (append-only).
    # If an order was enqueued, back-link the pending_execution_id so the
    # learning curve can pair signal -> outcome.
    try:
        persist_verdict(db, verdict, pending_execution_id=pending_id)
    except Exception as exc:
        log.debug("[strategist] verdict persistence skipped: %s", exc)

    return APIResponse(data=verdict, source="strategist:fresh")


@router.get(
    "/refresh",
    response_model=APIResponse[dict],
    summary="Force-fresh strategist verdict (bypass 60s cache)",
)
@limiter.limit("6/minute")
def strategist_refresh(
    request: Request,
    db: Session = Depends(get_db),
) -> APIResponse[dict]:
    _cache["cached_at"] = 0.0
    return strategist_decision(request=request, db=db)


@router.get(
    "/log",
    response_model=APIResponse[dict],
    summary="Recent strategist verdicts (mandate signal log)",
)
@limiter.limit("30/minute")
def strategist_log(
    request: Request,
    limit: int = 100,
    decision: str | None = None,         # filter: BUY | SELL | "STAND ASIDE"
    execution_status: str | None = None,  # filter: DEMO_TRADE_PLACED | SIGNAL_ONLY | ...
    db: Session = Depends(get_db),
) -> APIResponse[dict]:
    """
    Returns the most recent N strategist verdicts (default 100, max 1000).
    Each row is the full snapshot the mandate requires logged. Filter
    optionally by decision and/or execution_status.
    """
    from db_models import StrategistVerdict

    limit = max(1, min(limit, 1000))
    q = db.query(StrategistVerdict)
    if decision:
        q = q.filter(StrategistVerdict.decision == decision)
    if execution_status:
        q = q.filter(StrategistVerdict.execution_status == execution_status)
    rows = q.order_by(StrategistVerdict.created_at.desc()).limit(limit).all()

    def _serialise(r) -> dict:
        return {
            "id":                       r.id,
            "createdAt":                r.created_at.isoformat() if r.created_at else None,
            "decision":                 r.decision,
            "conditionsPassed":         r.conditions_passed,
            "estimatedWinRateRange":    r.estimated_win_rate_range,
            "executionStatus":          r.execution_status,
            "executionStatusReason":    r.execution_status_reason,
            "setupScore":               r.setup_score,
            "qualityBand":              r.quality_band,
            "marketState":              r.market_state,
            "session":                  r.session_classification,
            "tfAlignment":              r.tf_alignment_label,
            "liquidityBehaviour":       r.liquidity_behaviour,
            "entry":                    r.entry,
            "stopLoss":                 r.stop_loss,
            "tp1":                      r.tp1,
            "tp2":                      r.tp2,
            "rr":                       r.risk_reward,
            "lotSize":                  r.lot_size,
            "rsiH1":                    r.rsi_h1,
            "atrH1":                    r.atr_h1,
            "goldMacroBias":            r.gold_macro_bias,
            "newsRisk":                 r.news_risk,
            "improvementNote":          r.improvement_note,
            "finalVerdict":             r.final_verdict_text,
            "pendingExecutionId":       r.pending_execution_id,
            "mt5Ticket":                r.mt5_ticket,
            "result":                   r.result,
            "mfePts":                   r.mfe_pts,
            "maePts":                   r.mae_pts,
        }

    return APIResponse(
        data={
            "count": len(rows),
            "verdicts": [_serialise(r) for r in rows],
        },
        source="strategist_log",
    )


# ─────────────────────────────────────────────────────────────────────────────
# Telegram side-effect: alert on actionable verdict + optional STAND ASIDE info
# ─────────────────────────────────────────────────────────────────────────────

def _maybe_fire_alert(verdict: dict) -> None:
    """Fire the mandate-format Telegram alert if cooldown allows."""
    global _last_alert_fingerprint, _last_alert_at
    global _last_standby_fingerprint, _last_standby_at

    decision = verdict.get("decision")
    es       = verdict.get("execution_status")
    tp       = verdict.get("trade_plan") or {}

    try:
        if decision in ("BUY", "SELL"):
            # Actionable: send when ALERT permission set and cooldown passed
            if not (verdict.get("execution_permission") or {}).get("allow_alert"):
                return
            fp = f"{decision}|{tp.get('entry')}|{tp.get('stop_loss')}|{es}"
            if fp != _last_alert_fingerprint or (time.time() - _last_alert_at) > _ALERT_COOLDOWN_S:
                msg = format_mandate_signal_message(verdict)
                _send_plain(msg)
                _last_alert_fingerprint = fp
                _last_alert_at          = time.time()
        elif decision == "STAND ASIDE":
            # Informational standby — only fire if explicitly enabled
            if not getattr(settings, "telegram_standby_alerts", False):
                return
            fp = f"STANDBY|{verdict.get('execution_status_reason')}|{verdict.get('conditions_passed')}"
            if fp != _last_standby_fingerprint or (time.time() - _last_standby_at) > _STANDBY_COOLDOWN_S:
                msg = format_mandate_stand_aside_message(verdict)
                _send_plain(msg)
                _last_standby_fingerprint = fp
                _last_standby_at          = time.time()
    except Exception as exc:
        log.debug("[strategist] alert hook failed (non-fatal): %s", exc)


def _send_plain(text: str) -> None:
    """
    Send a Telegram message in plain-text mode (mandate format has no HTML).
    Falls back to the HTML sender if a plain sender isn't available.
    """
    try:
        import httpx
        if not (settings.telegram_bot_token and settings.telegram_chat_id):
            return
        url = f"https://api.telegram.org/bot{settings.telegram_bot_token}/sendMessage"
        resp = httpx.post(url, json={
            "chat_id":                  settings.telegram_chat_id,
            "text":                     text,
            "disable_web_page_preview": True,
            # NO parse_mode — mandate format is literal plain text with emojis
        }, timeout=10.0)
        if not resp.is_success:
            log.warning("[strategist] Telegram send failed status=%s body=%s",
                        resp.status_code, resp.text[:200])
    except Exception as exc:
        log.warning("[strategist] Telegram plain send error: %s", exc)


# ─────────────────────────────────────────────────────────────────────────────
# MT5 demo enqueue — only when execution_status == DEMO_TRADE_PLACED
# ─────────────────────────────────────────────────────────────────────────────

def _maybe_enqueue_demo_order(db: Session, verdict: dict) -> int | None:
    """
    If the verdict authorises a demo execution AND the operator has opted in,
    insert a PendingExecution row at lot=0.01 for the MT5 bridge to claim.
    Strict fingerprint dedupe so the same plan doesn't queue twice.

    Returns the PendingExecution id (or None if nothing was enqueued).
    """
    global _last_enqueue_fingerprint, _last_enqueue_at

    if verdict.get("execution_status") != "DEMO_TRADE_PLACED":
        return None
    mt5_obj = verdict.get("mt5_execution_object") or {}
    if not mt5_obj:
        return None

    # Hard mandate guards — fail loudly if the verdict shape is wrong
    if mt5_obj.get("lot") != 0.01:
        log.error("[strategist] refused to enqueue: lot != 0.01 (%s)", mt5_obj.get("lot"))
        return None
    if mt5_obj.get("live_execution_allowed", True):
        log.error("[strategist] refused to enqueue: live_execution_allowed must be false")
        return None
    if not settings.allow_demo_trading:
        log.info("[strategist] enqueue skipped: ALLOW_DEMO_TRADING=false")
        return None
    if not settings.mt5_bridge_enabled:
        log.info("[strategist] enqueue skipped: MT5_BRIDGE_ENABLED=false")
        return None

    fp = (
        f"{mt5_obj['action']}|{mt5_obj['entry']}|{mt5_obj['stop_loss']}"
        f"|{mt5_obj['take_profit_1']}|{mt5_obj['take_profit_2']}"
    )
    if fp == _last_enqueue_fingerprint and (time.time() - _last_enqueue_at) < _ENQUEUE_COOLDOWN_S:
        log.debug("[strategist] enqueue dedupe — same plan within cooldown")
        return None

    try:
        row = PendingExecution(
            pair="xauusd",
            signal=mt5_obj["action"],                  # BUY or SELL
            entry=float(mt5_obj["entry"]),
            stop_loss=float(mt5_obj["stop_loss"]),
            take_profit=float(mt5_obj["take_profit_1"]),
            risk_pips=float(abs(mt5_obj["entry"] - mt5_obj["stop_loss"])),
            quality_score=int(verdict.get("setup_score") or 0),
            rr=float(mt5_obj["risk_reward"]),
            max_lot=0.01,                              # MANDATE: fixed 0.01
            reason=(
                f"strategist mandate · {mt5_obj['conditions_passed']}/5 conditions"
                f" · est WR {verdict.get('estimated_win_rate_range')}"
            ),
            confirmations_json=json.dumps({
                "conditions":            verdict.get("conditions"),
                "conditions_passed":     verdict.get("conditions_passed"),
                "execution_status":      verdict.get("execution_status"),
                "session":               verdict.get("session_classification"),
                "market_state":          verdict.get("market_state"),
                "liquidity_behaviour":   verdict.get("liquidity_behaviour"),
                "tf_alignment":          verdict.get("tf_alignment_label"),
                "mt5_execution_object":  mt5_obj,
            }),
            expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
            status="PENDING",
        )
        db.add(row)
        db.commit()
        db.refresh(row)
        _last_enqueue_fingerprint = fp
        _last_enqueue_at           = time.time()
        log.info(
            "[strategist] ENQUEUED demo order #%d %s xauusd lot=0.01 entry=%s SL=%s TP1=%s TP2=%s",
            row.id, mt5_obj["action"], mt5_obj["entry"], mt5_obj["stop_loss"],
            mt5_obj["take_profit_1"], mt5_obj["take_profit_2"],
        )
        return row.id
    except Exception as exc:
        log.warning("[strategist] enqueue failed (non-fatal): %s", exc)
        try:
            db.rollback()
        except Exception:
            pass
        return None
