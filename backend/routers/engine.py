# -*- coding: utf-8 -*-
"""
Engine Status & Learning Router
================================
Exposes the adaptive learning system and engine maturity metrics.

Routes:
  GET  /engine/status          → Full engine health: maturity, weights, per-component accuracy
  GET  /engine/weights         → Current learned scoring weights vs baseline
  POST /engine/learn           → Force an immediate learning cycle from DB outcomes
  GET  /engine/bridge          → MT5 bridge connectivity status
"""
import logging

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from database import get_db
from models.common import APIResponse
from rate_limit import limiter
from fastapi import Request

router = APIRouter(prefix="/engine", tags=["engine"])
logger = logging.getLogger(__name__)


@router.get(
    "/status",
    summary="Engine maturity, adaptive weights, and MT5 bridge status",
)
def engine_status(
    pair: str = Query(default="all", description="Filter by pair: eurusd | xauusd | all"),
    db: Session = Depends(get_db),
):
    """
    Returns the full real-time learning system state:

    - **maturity**: 0-100 score growing as outcomes are logged (matures at 100 outcomes)
    - **weights**: current learned scoring weights vs baseline for each ICT component
    - **components**: per-component win rate, sample count, and calibration status
    - **pair_breakdown**: separate win rates for EUR/USD and XAU/USD
    - **bridge**: MT5 Windows bridge connectivity
    """
    from services.adaptive_engine import full_engine_status
    status = full_engine_status(db, pair=pair)
    return {"status": "ok", "data": status}


@router.get(
    "/weights",
    summary="Current learned component scoring weights",
)
def engine_weights(
    pair: str = Query(default="all"),
    db: Session = Depends(get_db),
):
    """Returns only the scoring weights: baseline vs current learned values."""
    from services.adaptive_engine import get_current_weights, BASE_WEIGHTS
    w = get_current_weights(db, pair=pair)
    return {
        "status": "ok",
        "data": {
            "pair":     w.pair,
            "baseline": BASE_WEIGHTS,
            "learned":  w.as_score_dict(),
            "delta": {
                k: w.as_score_dict()[k] - BASE_WEIGHTS[k]
                for k in BASE_WEIGHTS
            },
            "calibrated":    w.calibrated,
            "maturity_score": w.maturity_score,
            "n_outcomes":    w.n_outcomes,
            "overall_win_rate": w.overall_win_rate,
            "updated_at":    w.updated_at,
        },
    }


@router.post(
    "/learn",
    summary="Trigger immediate adaptive learning cycle",
)
def trigger_learning(
    pair: str = Query(default="all", description="Pair to learn for: eurusd | xauusd | all"),
    db: Session = Depends(get_db),
):
    """
    Forces the engine to re-analyse all logged outcomes and update scoring weights.
    Normally triggered automatically whenever a trade result is logged.
    Call this manually after importing historical outcome data.
    """
    from services.adaptive_engine import run_learning_cycle
    result = run_learning_cycle(db, pair=pair)
    logger.info(
        "Manual learning triggered pair=%s maturity=%d outcomes=%d win_rate=%.2f",
        pair, result.maturity_score, result.n_outcomes, result.overall_win_rate,
    )
    return {
        "status": "ok",
        "data": {
            "pair":             result.pair,
            "maturity_score":   result.maturity_score,
            "n_outcomes":       result.n_outcomes,
            "overall_win_rate": result.overall_win_rate,
            "calibrated":       result.calibrated,
            "learned_weights":  result.as_score_dict(),
            "updated_at":       result.updated_at,
        },
    }


@router.get(
    "/bridge",
    summary="MT5 Windows bridge connectivity",
)
def bridge_status():
    """
    Returns whether the MT5 Windows bridge is online.
    The bridge runs natively on the Windows host (mt5_bridge/start_bridge.bat).
    When online, the signal engine uses live Exness prices instead of synthetic data.
    """
    from services.live_feed import bridge_status as _bridge_status, BRIDGE_BASE
    status = _bridge_status()
    return {
        "status": "ok",
        "data": {
            **status,
            "instructions": (
                "Start mt5_bridge/start_bridge.bat on the Windows host "
                f"to enable live price feed at {BRIDGE_BASE}"
            ),
        },
    }


@router.get(
    "/providers",
    summary="Status of all market data providers",
)
def provider_status():
    """
    Returns connection status for all configured data providers:
    MT5 bridge, TradingView, MyFXBook sentiment.
    """
    from services.live_feed import bridge_status as _bridge_st
    from services.tradingview_provider import tv_status
    from services.myfxbook_provider import myfxbook_status

    return {
        "status": "ok",
        "data": {
            "mt5_bridge":  _bridge_st(),
            "tradingview": tv_status(),
            "myfxbook":    myfxbook_status(),
            "candle_chain": [
                "MT5 Bridge  (live broker prices — highest priority)",
                "TradingView (real OHLCV when bridge offline)",
                "Synthetic   (seeded demo — always available)",
            ],
        },
    }
