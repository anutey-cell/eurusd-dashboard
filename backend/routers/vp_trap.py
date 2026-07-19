"""
VP Trap Strategy Router
=======================

Phase 1 endpoints:
  GET /api/v1/vp-trap/profile          — current prev-day profile (fresh compute)
  GET /api/v1/vp-trap/profile/{date}   — profile for a specific date (backtest / audit)
  GET /api/v1/vp-trap/status           — quick health + config flags

Later phases will add:
  GET /api/v1/vp-trap/zones            — live trap zones + their states
  GET /api/v1/vp-trap/signals          — recent BUY/SELL signals from the strategy
  POST /api/v1/vp-trap/reset           — clear zones + rearm from prev-day (ops)
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from config import settings
from database import get_db
from models.common import APIResponse

router = APIRouter(prefix="/vp-trap", tags=["vp_trap"])
log = logging.getLogger(__name__)


@router.get("/status")
def get_status() -> APIResponse:
    """Config + capabilities snapshot. Cheap; safe to poll."""
    data = {
        "enabled":              getattr(settings, "vp_trap_enabled", False),
        "mode":                 getattr(settings, "vp_trap_mode", "independent"),
        "live_threshold":       getattr(settings, "vp_trap_live_threshold", 80),
        "watch_threshold":      getattr(settings, "vp_trap_watch_threshold", 60),
        "value_area_pct":       getattr(settings, "vp_trap_value_area_pct", 0.70),
        "min_rr":               getattr(settings, "vp_trap_min_rr", 1.8),
        "zone_expiry_hours":    getattr(settings, "vp_trap_zone_expiry_hours", 48),
        "auto_execute":         getattr(settings, "vp_trap_auto_execute", False),
        "telegram_alerts":      getattr(settings, "vp_trap_telegram_alerts", True),
        "tick_volume_penalty":  getattr(settings, "vp_trap_penalize_tick_volume", 15),
        # Volume-source capability (Phase 1 = tick_proxy only)
        "volume_sources_available": ["tick_proxy"],
        "phase": "1 — profile computation only. No signals yet.",
    }
    return APIResponse(data=data, source="vp_trap:status")


@router.get("/profile")
def get_current_profile(
    value_area_pct: float = Query(0.70, ge=0.5, le=0.9,
                                  description="Value area percentage (default 70%)"),
) -> APIResponse:
    """Compute + return the profile for the most recent COMPLETED trading day.

    Uses the same live candle providers the rest of the engine uses. Returns
    the full profile dict — PDH, PDL, PDO, PDC, POC, VAH, VAL, HVN/LVN,
    VWAP, day type, plus meta (volume_source, bin_size, computed_at).
    """
    from services.vp_trap_strategy import compute_current_prev_day_profile

    try:
        profile = compute_current_prev_day_profile(value_area_pct=value_area_pct)
    except Exception as exc:
        log.exception("[vp-trap] profile compute failed: %s", exc)
        raise HTTPException(status_code=500, detail=f"profile compute failed: {exc}")

    if profile is None:
        raise HTTPException(status_code=503,
                            detail="Insufficient candle data to compute previous-day profile")
    return APIResponse(data=profile.to_dict(), source="vp_trap:profile:fresh")


@router.get("/profile/{date}")
def get_profile_for_date(
    date: str,
    value_area_pct: float = Query(0.70, ge=0.5, le=0.9),
) -> APIResponse:
    """Compute a profile for a specific date (YYYY-MM-DD) using historical candles.

    Used by the backtest walker (later phases) and for audit/debugging: given
    a date, compute what the profile of THAT day would have looked like
    at end-of-day UTC. Requires HistoricalCandle rows covering the date.
    """
    from services.vp_trap_strategy import compute_prev_day_profile
    try:
        target = datetime.strptime(date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except ValueError:
        raise HTTPException(status_code=400,
                            detail=f"date must be YYYY-MM-DD, got {date!r}")

    # reference_time = the START of the day AFTER target (so target IS "previous")
    from datetime import timedelta
    reference = target + timedelta(days=1)

    # Pull historical candles covering target date - 2 days to +1 day
    try:
        from database import SessionLocal
        from db_models import HistoricalCandle
        from sqlalchemy import asc

        db = SessionLocal()
        try:
            def _load(tf: str) -> list:
                rows = (db.query(HistoricalCandle)
                          .filter(HistoricalCandle.timeframe == tf)
                          .filter(HistoricalCandle.candle_time >= target - timedelta(days=2))
                          .filter(HistoricalCandle.candle_time <= reference + timedelta(hours=1))
                          .order_by(asc(HistoricalCandle.candle_time))
                          .all())
                from types import SimpleNamespace
                return [SimpleNamespace(
                    time=r.candle_time,
                    open=r.open, high=r.high, low=r.low, close=r.close,
                    volume=r.volume or 0,
                ) for r in rows]

            h1  = _load("H1")
            m15 = _load("M15") or None
        finally:
            db.close()
    except Exception as exc:
        log.exception("[vp-trap] historical candle load failed: %s", exc)
        raise HTTPException(status_code=500, detail=f"historical load failed: {exc}")

    if not h1:
        raise HTTPException(status_code=404,
                            detail=f"No historical H1 candles found for {date}")

    profile = compute_prev_day_profile(
        candles_h1=h1, candles_m15=m15,
        reference_time=reference,
        value_area_pct=value_area_pct,
    )
    if profile is None:
        raise HTTPException(status_code=422,
                            detail=f"Profile computation returned None (degenerate day?)")
    return APIResponse(data=profile.to_dict(), source=f"vp_trap:profile:historical:{date}")


# ── Phase 2: zone endpoints ────────────────────────────────────────────────

@router.get("/zones")
def get_active_zones(
    include_terminal: bool = Query(False,
        description="Include EXPIRED/INVALIDATED zones as well as active ones"),
    db: Session = Depends(get_db),
) -> APIResponse:
    """Return the currently persisted trap zones with their states.

    Reads from the vp_trap_zones table. Populated by scan_and_persist_zones()
    which runs from the strategist runner background loop (Phase 2) OR the
    /scan endpoint below (manual trigger).
    """
    from services.vp_trap_state import load_active_zones
    try:
        zones = load_active_zones(db, exclude_terminal=(not include_terminal))
    except Exception as exc:
        log.exception("[vp-trap] zone load failed: %s", exc)
        raise HTTPException(status_code=500, detail=f"zone load failed: {exc}")
    return APIResponse(
        data={
            "count":            len(zones),
            "include_terminal": include_terminal,
            "zones":            zones,
        },
        source="vp_trap:zones",
    )


@router.get("/diagnostics")
def get_diagnostics(db: Session = Depends(get_db)) -> APIResponse:
    """Phase 3 diagnostics — for every zone in the current profile, run the
    state machine AND the scorer (regardless of TRIGGERED state).

    Lets the operator see partial scores + which factors are pulling weight
    even before a zone reaches TRIGGERED. Purely diagnostic; does NOT persist
    scores to DB (VpTrapSignal rows are only written in Phase 4 when alerts
    actually fire).
    """
    from services.vp_trap_strategy import (
        compute_current_prev_day_profile, _bars_since, _build_market_context,
    )
    from services.vp_trap_state import zones_from_profile, scan_zone
    from services.vp_trap_scoring import score_zone
    from data.candles import get_candles
    from datetime import timedelta

    profile = compute_current_prev_day_profile()
    if profile is None:
        raise HTTPException(status_code=503, detail="No prev-day profile available")

    try:
        m15_resp = get_candles(interval="M15", limit=500, pair="xauusd")
        candles = m15_resp.candles if m15_resp and m15_resp.candles else []
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"candle fetch failed: {exc}")

    bars_window = _bars_since(candles, profile.computed_at)
    if not bars_window:
        try:
            day_end = datetime.strptime(profile.profile_date, "%Y-%m-%d").replace(
                tzinfo=timezone.utc) + timedelta(days=1)
            bars_window = _bars_since(candles, day_end)
        except Exception:
            pass

    ctx = _build_market_context(profile)

    live_threshold      = getattr(settings, "vp_trap_live_threshold", 80)
    countertrend_bonus  = getattr(settings, "vp_trap_countertrend_bonus", 10)
    tick_volume_penalty = getattr(settings, "vp_trap_penalize_tick_volume", 15)
    min_rr              = getattr(settings, "vp_trap_min_rr", 1.8)

    zones = zones_from_profile(profile, expiry_hours=48)
    diagnostics = []
    for z in zones:
        scan_zone(z, bars_window,
                  min_displacement_pts=5.0, retest_tolerance_pts=3.0, max_retests=3)
        breakdown, plan = score_zone(
            z, profile, ctx,
            countertrend_bonus=countertrend_bonus,
            tick_volume_penalty=tick_volume_penalty,
            min_rr=min_rr,
            live_threshold=live_threshold,
        )
        diagnostics.append({
            "zone":         z.to_dict(),
            "score":        breakdown.to_dict(),
            "trade_plan":   plan,
        })

    return APIResponse(
        data={
            "profile_date":  profile.profile_date,
            "current_price": ctx.current_price,
            "atr_h1":        round(ctx.atr_h1, 2),
            "d1_bias":       ctx.d1_bias,
            "h4_bias":       ctx.h4_bias,
            "volume_source": ctx.volume_source,
            "news_clear":    ctx.news_clear,
            "live_threshold": live_threshold,
            "zone_scores":   diagnostics,
        },
        source="vp_trap:diagnostics",
    )


@router.get("/signals")
def get_signals(
    limit: int = Query(20, ge=1, le=200,
                       description="Max signals to return, newest first"),
    include_terminal: bool = Query(True,
        description="Include signals with terminal state (STOPPED/EXPIRED/etc)"),
    db: Session = Depends(get_db),
) -> APIResponse:
    """Fired VP Trap signals (Phase 4 output). Newest first.

    Every alert dispatched via Telegram writes a VpTrapSignal row here.
    Dashboard consumes for the signals list; ops uses for audit.
    """
    from db_models import VpTrapSignal as SM
    import json as _json

    q = db.query(SM).order_by(SM.created_at.desc()).limit(limit)
    rows = q.all()
    out = []
    for r in rows:
        if not include_terminal and r.state in ("STOPPED", "EXPIRED", "INVALIDATED"):
            continue
        try:
            breakdown = _json.loads(r.score_breakdown_json) if r.score_breakdown_json else None
        except Exception:
            breakdown = None
        out.append({
            "id":             r.id,
            "created_at":     r.created_at.isoformat() if r.created_at else None,
            "zone_id":        r.zone_id,
            "signal":         r.signal,
            "entry":          r.entry,
            "stop_loss":      r.stop_loss,
            "tp1":            r.tp1, "tp2": r.tp2, "tp3": r.tp3,
            "rr":             r.rr,
            "risk_points":    r.risk_points,
            "score_total":    r.score_total,
            "score_breakdown": breakdown,
            "trap_side":      r.trap_side,
            "setup_type":     r.setup_type,
            "session":        r.session,
            "is_countertrend": r.is_countertrend,
            "volume_source":  r.volume_source,
            "mandate_agrees": r.mandate_agrees,
            "momentum_agrees": r.momentum_agrees,
            "state":          r.state,
            "outcome":        r.outcome,
            "r_realized":     r.r_realized,
            "reason_qualifies": r.reason_qualifies,
        })
    return APIResponse(
        data={"count": len(out), "signals": out},
        source="vp_trap:signals",
    )


@router.post("/scan")
def force_scan(db: Session = Depends(get_db)) -> APIResponse:
    """Force a full scan cycle: rebuild profile + candidate zones + advance
    state + persist. Ops endpoint for testing.

    In steady-state, the strategist runner's background loop calls
    scan_and_persist_zones() automatically. This route lets you re-run on
    demand without waiting for the next tick.
    """
    from services.vp_trap_strategy import scan_and_persist_zones
    try:
        zones = scan_and_persist_zones(db)
    except Exception as exc:
        log.exception("[vp-trap] force scan failed: %s", exc)
        raise HTTPException(status_code=500, detail=f"scan failed: {exc}")
    return APIResponse(
        data={"scanned": len(zones), "zones": zones},
        source="vp_trap:scan:forced",
    )
