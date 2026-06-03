"""
Institutional Demo-Mandate Strategist API
=========================================

GET /api/v1/strategist/decision   — Current unified verdict (60s cached).
GET /api/v1/strategist/refresh    — Force fresh; bypasses cache.
GET /api/v1/strategist/log        — Recent verdicts (mandate signal log).

All side-effects (Telegram alert, MT5 demo enqueue, verdict persistence) live
in services.strategist_runner.run_once() so the background scheduler can drive
the identical pipeline on its own cadence — there is one authoritative
strategist runner, two entry points.
"""
from __future__ import annotations

import logging
import time

from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from database import get_db
from models.common import APIResponse
from rate_limit import limiter
from services.strategist_runner import run_once

router = APIRouter(prefix="/strategist", tags=["strategist"])
log = logging.getLogger(__name__)

# Module-level cache (single-process VPS — fine at this scale)
_cache: dict = {"verdict": None, "cached_at": 0.0}
_CACHE_TTL = 60.0


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
    to bypass. Side-effects (Telegram + MT5 enqueue + audit log) only fire
    on a fresh compute, never on cache hits — so polling from the dashboard
    is safe.
    """
    age = time.time() - _cache.get("cached_at", 0)
    if _cache.get("verdict") and age < _CACHE_TTL:
        return APIResponse(data=_cache["verdict"], source="strategist:cached")

    verdict = run_once(db)
    _cache["verdict"]   = verdict
    _cache["cached_at"] = time.time()
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
    "/briefing/preview",
    response_model=APIResponse[dict],
    summary="Preview the daily market briefing without sending it",
)
@limiter.limit("10/minute")
def briefing_preview(
    request: Request,
    db: Session = Depends(get_db),
) -> APIResponse[dict]:
    """Build the current daily briefing and return it as plain text. No send."""
    from services.hourly_briefing import build_briefing
    msg = build_briefing(db)
    return APIResponse(
        data={"message": msg or "(failed to build briefing)"},
        source="briefing:preview",
    )


@router.post(
    "/briefing/send-now",
    response_model=APIResponse[dict],
    summary="Force-send the daily briefing to Telegram immediately (bypasses dedupe)",
)
@limiter.limit("3/minute")
def briefing_send_now(
    request: Request,
    db: Session = Depends(get_db),
) -> APIResponse[dict]:
    """
    Build + send the briefing right now, ignoring the once-per-day dedupe.
    Useful for testing format / verifying Telegram wiring without waiting.
    """
    from services.hourly_briefing import build_briefing, _send_plain
    msg = build_briefing(db)
    if not msg:
        return APIResponse(data={"sent": False, "reason": "build failed"}, source="briefing")
    sent = _send_plain(msg)
    return APIResponse(data={"sent": sent, "preview": msg[:200] + "..."}, source="briefing")


@router.get(
    "/learnings",
    response_model=APIResponse[dict],
    summary="Aggregate closed strategist trades into actionable lessons",
)
@limiter.limit("12/minute")
def strategist_learnings(
    request: Request,
    window_days: int = 7,
    db: Session = Depends(get_db),
) -> APIResponse[dict]:
    """
    Pulls strategist_verdicts with non-null result (WIN / LOSS / BREAKEVEN)
    in the last `window_days`, aggregates by conditions_passed / killzone ×
    direction / direction_source / sweep_side / macro alignment, and surfaces:
      • overall WR, expectancy R, sample size
      • per-bucket WR with comparison to mandate's predicted ranges
      • top 3 winners + top 3 losers (by R-multiple)
      • calibration notes — where reality diverges from theory
    """
    from services.learnings import build_learnings
    window_days = max(1, min(window_days, 90))
    data = build_learnings(db, window_days=window_days)
    return APIResponse(data=data, source="strategist_learnings")


@router.get(
    "/newsletter/saturday-recap/preview",
    response_model=APIResponse[dict],
    summary="Preview the Saturday weekly-recap newsletter without sending",
)
@limiter.limit("10/minute")
def newsletter_saturday_preview(
    request: Request,
    db: Session = Depends(get_db),
) -> APIResponse[dict]:
    from services.weekend_newsletters import build_saturday_recap
    return APIResponse(data={"message": build_saturday_recap(db)}, source="newsletter:preview")


@router.get(
    "/newsletter/sunday-forecast/preview",
    response_model=APIResponse[dict],
    summary="Preview the Sunday week-ahead forecast newsletter without sending",
)
@limiter.limit("10/minute")
def newsletter_sunday_preview(
    request: Request,
    db: Session = Depends(get_db),
) -> APIResponse[dict]:
    from services.weekend_newsletters import build_sunday_forecast
    return APIResponse(data={"message": build_sunday_forecast(db)}, source="newsletter:preview")


@router.post(
    "/newsletter/{kind}/send-now",
    response_model=APIResponse[dict],
    summary="Force-send a weekend newsletter (kind=saturday-recap|sunday-forecast)",
)
@limiter.limit("3/minute")
def newsletter_send_now(
    request: Request,
    kind: str,
    db: Session = Depends(get_db),
) -> APIResponse[dict]:
    from services.weekend_newsletters import build_saturday_recap, build_sunday_forecast
    from services.strategist_runner import _send_plain
    if kind == "saturday-recap":
        msg = build_saturday_recap(db)
    elif kind == "sunday-forecast":
        msg = build_sunday_forecast(db)
    else:
        return APIResponse(data={"sent": False, "error": "kind must be saturday-recap | sunday-forecast"})
    sent = False
    try:
        _send_plain(msg)
        sent = True
    except Exception as exc:
        log.warning("[newsletter] send failed: %s", exc)
    return APIResponse(data={"sent": sent, "kind": kind, "preview": msg[:500] + "..."},
                       source="newsletter:send")


@router.post(
    "/learnings/digest-now",
    response_model=APIResponse[dict],
    summary="Force-send the weekly Telegram digest (bypasses schedule)",
)
@limiter.limit("3/minute")
def strategist_digest_now(
    request: Request,
    window_days: int = 7,
    db: Session = Depends(get_db),
) -> APIResponse[dict]:
    """Build the weekly digest and POST it to Telegram immediately."""
    from services.learnings import build_learnings, format_weekly_digest
    from services.strategist_runner import _send_plain
    data = build_learnings(db, window_days=window_days)
    msg = format_weekly_digest(data)
    sent = False
    try:
        _send_plain(msg)
        sent = True
    except Exception as exc:
        log.warning("[strategist] digest send failed: %s", exc)
    return APIResponse(
        data={"sent": sent, "sample_size": data["sample_size"], "preview": msg[:600] + "..."},
        source="strategist_learnings",
    )


@router.get(
    "/log",
    response_model=APIResponse[dict],
    summary="Recent strategist verdicts (mandate signal log)",
)
@limiter.limit("30/minute")
def strategist_log(
    request: Request,
    limit: int = 100,
    decision: str | None = None,
    execution_status: str | None = None,
    db: Session = Depends(get_db),
) -> APIResponse[dict]:
    """Most recent N verdicts. Filter by decision and/or execution_status."""
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
        data={"count": len(rows), "verdicts": [_serialise(r) for r in rows]},
        source="strategist_log",
    )
