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

from fastapi import APIRouter, HTTPException, Query

from config import settings
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
