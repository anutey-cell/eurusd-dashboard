"""
Institutional Strategist API
============================

GET /api/v1/strategist/decision   — Current unified verdict (60s cached).
GET /api/v1/strategist/refresh    — Force fresh; bypasses cache.

Returns the strict institutional-strategist JSON schema. Output never
fabricates: if data is missing, decision is STAND ASIDE with explicit reason.

Telegram alert hook: the endpoint MAY fire an informational Telegram alert
when the decision flips to LONG/SHORT AND all execution gates pass.
Deduplication is via fingerprint (decision + entry + 5min cooldown).
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from database import get_db
from models.common import APIResponse
from rate_limit import limiter
from services.strategist import make_decision

router = APIRouter(prefix="/strategist", tags=["strategist"])
log = logging.getLogger(__name__)

# Module-level cache (single-process VPS — fine at this scale)
_cache: dict = {"verdict": None, "cached_at": 0.0}
_CACHE_TTL = 60.0   # seconds

# Telegram dedupe — same actionable verdict within COOLDOWN_S only alerts once
_last_alert_fingerprint: str = ""
_last_alert_at: float = 0.0
_ALERT_COOLDOWN_S = 600.0   # 10 min


@router.get(
    "/decision",
    response_model=APIResponse[dict],
    summary="Current institutional verdict (LONG / SHORT / STAND ASIDE) + full reasoning",
)
@limiter.limit("60/minute")
def strategist_decision(
    request: Request,
    db: Session = Depends(get_db),
) -> APIResponse[dict]:
    """
    Returns the strict-format JSON the institutional strategist mandate
    specifies. Cached for 60s. Use /strategist/refresh for an immediate
    fresh computation.
    """
    global _last_alert_fingerprint, _last_alert_at

    age = time.time() - _cache.get("cached_at", 0)
    if _cache.get("verdict") and age < _CACHE_TTL:
        return APIResponse(data=_cache["verdict"], source="strategist:cached")

    verdict = make_decision(db)
    _cache["verdict"] = verdict
    _cache["cached_at"] = time.time()

    # Telegram alert — only when verdict is LONG/SHORT AND allow_alert AND
    # this is a new actionable verdict (not just a repeat of the cached one).
    try:
        if verdict.get("decision") in ("LONG", "SHORT") and \
           verdict.get("execution_permission", {}).get("allow_alert"):
            tp = verdict.get("trade_plan", {})
            fp = f"{verdict['decision']}|{tp.get('entry')}|{tp.get('stop_loss')}|{verdict.get('setup_score')}"
            if fp != _last_alert_fingerprint or (time.time() - _last_alert_at) > _ALERT_COOLDOWN_S:
                _fire_strategist_alert(verdict)
                _last_alert_fingerprint = fp
                _last_alert_at = time.time()
    except Exception as exc:
        log.debug("[strategist] alert hook failed (non-fatal): %s", exc)

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


def _fire_strategist_alert(verdict: dict) -> None:
    """Fire a structured Telegram alert summarising the verdict."""
    try:
        from services.telegram_service import send_text_alert
    except Exception:
        return

    tp = verdict.get("trade_plan", {}) or {}
    dec = verdict.get("decision", "STAND ASIDE")
    score = verdict.get("setup_score", 0)
    band = verdict.get("quality_band", "?")
    macro = (verdict.get("macro_context") or {}).get("gold_macro_bias", "Neutral")
    model = (verdict.get("liquidity_model") or {}).get("type", "None")
    kz_block = verdict.get("institutional_logic", "")[:160]

    icon = "📈" if dec == "LONG" else "📉" if dec == "SHORT" else "⏸"

    msg = (
        f"{icon} <b>STRATEGIST · {dec}</b>\n"
        f"Score: {score}/100 · {band} · Model {model}\n"
        f"Entry: {tp.get('entry')} · SL: {tp.get('stop_loss')}\n"
        f"TP1 {tp.get('tp1')} · TP2 {tp.get('tp2')} · TP3 {tp.get('tp3')}\n"
        f"RR: {tp.get('risk_reward')}\n"
        f"Macro: {macro}\n"
        f"{kz_block}"
    )
    try:
        send_text_alert(text=msg)
    except Exception as exc:
        log.debug("[strategist] send_text_alert failed: %s", exc)
