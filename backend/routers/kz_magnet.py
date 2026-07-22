"""
KZ Magnet strategy endpoints.

  GET /api/v1/kz-magnet/status        — config snapshot
  GET /api/v1/kz-magnet/current       — active magnet setup right now (or null)
  GET /api/v1/kz-magnet/flux          — Flux + VPPP for arbitrary N recent M15 bars
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, HTTPException, Query

from config import settings
from models.common import APIResponse

router = APIRouter(prefix="/kz-magnet", tags=["kz_magnet"])
log = logging.getLogger(__name__)


@router.get("/status")
def get_status() -> APIResponse:
    """Config snapshot + chain of magnet transitions."""
    from services.kz_magnet_strategy import MAGNET_CHAINS
    chains = [
        {
            "prior_kz":     c[0], "target_kz": c[1],
            "prior_hours":  f"{c[2]:02d}-{c[3]:02d} UTC",
            "target_hours": f"{c[4]:02d}-{c[5]:02d} UTC",
            "expected_touch_pct": c[6],
        }
        for c in MAGNET_CHAINS
    ]
    return APIResponse(data={
        "enabled":              getattr(settings, "kz_magnet_enabled", False),
        "telegram_alerts":      getattr(settings, "kz_magnet_telegram_alerts", True),
        "min_distance_atr":     getattr(settings, "kz_magnet_min_distance_atr", 0.6),
        "max_va_width_atr":     getattr(settings, "kz_magnet_max_va_width_atr", 2.0),
        "alert_cooldown_s":     getattr(settings, "kz_magnet_alert_cooldown_s", 1800),
        "magnet_chains":        chains,
    }, source="kz_magnet:status")


@router.get("/current")
def get_current_setup() -> APIResponse:
    """
    Run detector NOW and return current MagnetSetup or null.

    Uses current M15 candles + ATR estimate. Read-only — never sends an
    alert, never persists. Purely for dashboard consumption.
    """
    from services.kz_magnet_strategy import scan_for_magnet
    from data.candles import get_candles
    try:
        # Get a rough ATR
        h1 = get_candles(interval="H1", limit=30, pair="xauusd")
        atr = 20.0
        if h1 and h1.candles and len(h1.candles) > 14:
            highs = [b.high for b in h1.candles]
            lows  = [b.low  for b in h1.candles]
            closes = [b.close for b in h1.candles]
            trs = []
            for i in range(1, len(closes)):
                trs.append(max(highs[i] - lows[i],
                                abs(highs[i] - closes[i-1]),
                                abs(lows[i] - closes[i-1])))
            atr = round(sum(trs[-14:]) / 14, 2)
        setup = scan_for_magnet(atr_h1=atr, news_clear=True)
        return APIResponse(data={
            "atr_h1": atr,
            "setup":  setup.to_dict() if setup else None,
        }, source="kz_magnet:current")
    except Exception as exc:
        log.exception("[kz-magnet] current failed: %s", exc)
        raise HTTPException(status_code=500, detail=f"current failed: {exc}")


@router.get("/flux")
def get_flux_for_recent(
    bars: int = Query(48, ge=8, le=500,
                      description="Number of recent M15 bars to profile (default = last 12h)"),
) -> APIResponse:
    """
    Compute VPPP + Flux for the last N M15 bars. Useful for the operator
    to inspect the current volume-flow state at any timeframe.
    """
    from services.volume_pivot_flux import compute_vppp_flux
    from data.candles import get_candles
    try:
        resp = get_candles(interval="M15", limit=bars, pair="xauusd")
        candles = resp.candles if resp and resp.candles else []
        if not candles:
            raise HTTPException(status_code=503, detail="No M15 candles available")
        result = compute_vppp_flux(candles)
        if not result:
            raise HTTPException(status_code=422, detail="VPPP computation failed")
        return APIResponse(data={
            "bars_used":      len(candles),
            "first_bar_time": (candles[0].time if candles[0].time.tzinfo
                               else candles[0].time.replace(tzinfo=timezone.utc)).isoformat(),
            "last_bar_time":  (candles[-1].time if candles[-1].time.tzinfo
                               else candles[-1].time.replace(tzinfo=timezone.utc)).isoformat(),
            "vppp":           result.to_dict(),
        }, source="kz_magnet:flux")
    except HTTPException:
        raise
    except Exception as exc:
        log.exception("[kz-magnet] flux failed: %s", exc)
        raise HTTPException(status_code=500, detail=f"flux failed: {exc}")
