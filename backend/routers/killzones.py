"""
Killzone Edge API
=================

Per-killzone analytics: ranks ICT killzones by edge score blending
paper-observation outcomes and raw M15 price-action statistics.

Endpoints
---------
GET /api/v1/killzones/edge      Full per-KZ report (table + ranking)
GET /api/v1/killzones/current   What KZ is active now + recommended posture
GET /api/v1/killzones/heatmap   24-cell hour-of-day edge heatmap

Read-only. Safe for live trading mode.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy.orm import Session

from database import get_db
from models.common import APIResponse
from rate_limit import limiter
from services.killzone_analyzer import (
    analyze_killzones, get_current_recommendation, get_hour_heatmap,
)

router = APIRouter(prefix="/killzones", tags=["killzones"])
log = logging.getLogger(__name__)


@router.get(
    "/edge",
    response_model=APIResponse[dict],
    summary="Per-killzone edge ranking (observations + price action blended)",
)
@limiter.limit("30/minute")
def killzones_edge(
    request: Request,
    lookback_days: int = Query(default=60, ge=7, le=180,
                                description="Days of paper-observations and M15 candles to analyze"),
    engine_id:     str | None = Query(default=None,
                                description="Restrict observations to one engine (swing | trend_pullback)"),
    db: Session = Depends(get_db),
) -> APIResponse[dict]:
    """
    Returns the full per-killzone breakdown with rankings.

    Each killzone row carries:
      - `observations`: win rate, expectancy R, BUY/SELL split, best setup_type
      - `price_action`: avg range, body, momentum, breakout/reversal counts
      - `edge_score`:   0-100 composite (50% expectancy, 20% wr, 15% momentum, 15% expansion)
      - `posture`:      PRESS | TRADE | OBSERVE | AVOID
    """
    report = analyze_killzones(db, lookback_days=lookback_days, engine_id=engine_id)
    return APIResponse(data=report, source="killzone_analyzer")


@router.get(
    "/current",
    response_model=APIResponse[dict],
    summary="Current active killzone + recommended posture",
)
@limiter.limit("60/minute")
def killzones_current(
    request: Request,
    lookback_days: int = Query(default=60, ge=7, le=180),
    db: Session = Depends(get_db),
) -> APIResponse[dict]:
    """
    Lightweight 'what should I do right now' endpoint — designed to be
    polled by the dashboard banner every minute.
    """
    rec = get_current_recommendation(db, lookback_days=lookback_days)
    return APIResponse(data=rec, source="killzone_analyzer")


@router.get(
    "/heatmap",
    response_model=APIResponse[dict],
    summary="24-hour edge heatmap (one cell per UTC hour)",
)
@limiter.limit("30/minute")
def killzones_heatmap(
    request: Request,
    lookback_days: int = Query(default=60, ge=7, le=180),
    db: Session = Depends(get_db),
) -> APIResponse[dict]:
    """
    Returns a 24-element array (hour 0-23 UTC) with the killzone covering
    that hour and its current edge score. Renders as a colored strip
    on the dashboard.
    """
    cells = get_hour_heatmap(db, lookback_days=lookback_days)
    return APIResponse(data={"cells": cells}, source="killzone_analyzer")
