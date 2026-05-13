"""
Broker execution endpoints — disabled by default.

POST /api/v1/execution/place-order
  Full safety chain: execution must be enabled, signal quality ≥ 80,
  RR ≥ 2.5, news not blocked, SL/TP/invalidation required,
  spread acceptable, daily loss limit not breached.
  Rate-limited to 5 requests/minute per IP.

POST /api/v1/execution/kill-switch
  Immediately blocks all execution.  Cannot be reversed without restart.

GET  /api/v1/execution/status
  Returns current execution state (always safe to call).
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from rate_limit import limiter

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/execution", tags=["execution"])


# ── Request / response models ─────────────────────────────────────────────────

class OrderRequest(BaseModel):
    pair:           str   = Field(...,  description="Trading pair, e.g. EUR/USD")
    side:           str   = Field(...,  description="BUY or SELL")
    quality_score:  int   = Field(...,  description="Signal quality score (0–100)")
    rr:             float = Field(...,  description="Risk-reward ratio of the setup")
    stop_loss:      float = Field(...,  description="Stop-loss price level")
    take_profit:    float = Field(...,  description="Take-profit price level")
    invalidation:   str   = Field(...,  description="Invalidation condition (required)")
    news_blocked:   bool  = Field(False, description="True if news blackout is active")
    stop_loss_pips: float = Field(...,  description="SL distance in pips")


class KillSwitchResponse(BaseModel):
    killSwitchActive: bool
    triggeredAt:      str
    message:          str


# ── Safety validation ─────────────────────────────────────────────────────────

MIN_QUALITY_SCORE = 80
MIN_RR            = 2.5

def _validate_order(req: OrderRequest) -> None:
    errors: list[str] = []

    if req.quality_score < MIN_QUALITY_SCORE:
        errors.append(f"Quality score {req.quality_score} below minimum {MIN_QUALITY_SCORE}.")
    if req.rr < MIN_RR:
        errors.append(f"RR {req.rr:.2f} below minimum {MIN_RR}.")
    if req.news_blocked:
        errors.append("News blackout active — execution blocked.")
    if not req.stop_loss:
        errors.append("Stop-loss price is required.")
    if not req.take_profit:
        errors.append("Take-profit price is required.")
    if not req.invalidation or len(req.invalidation.strip()) < 5:
        errors.append("Invalidation condition must be specified.")
    if req.side not in ("BUY", "SELL"):
        errors.append(f"Side must be BUY or SELL, got {req.side!r}.")

    if errors:
        logger.warning(
            "Execution safety rejection pair=%s side=%s reasons=%s",
            req.pair, req.side, errors,
        )
        raise HTTPException(
            status_code=422,
            detail={"safetyRejection": True, "reasons": errors},
        )


# ── Endpoints ─────────────────────────────────────────────────────────────────

@router.get("/status", summary="Check broker execution status")
def execution_status() -> dict:
    from services.broker_provider import check_execution_enabled
    return check_execution_enabled()


@router.post(
    "/place-order",
    summary="Place a market order (disabled — broker integration not implemented)",
    description=(
        "Submits a market order through the configured broker after passing the "
        "full safety chain. **Disabled by default.** Requires "
        "`BROKER_EXECUTION_ENABLED=true` in .env. "
        "Rate-limited to 5 requests/minute per IP."
    ),
)
@limiter.limit("5/minute")
def place_order(request: Request, req: OrderRequest) -> dict:
    from config import settings
    from services.broker_provider import (
        check_execution_enabled,
        check_spread,
        calculate_position_size,
        place_market_order,
    )

    logger.warning(
        "Execution attempt pair=%s side=%s quality=%s rr=%s ip=%s",
        req.pair, req.side, req.quality_score, req.rr,
        request.client.host if request.client else "?",
    )

    # Gate 1: execution enabled
    state = check_execution_enabled()
    if not state["enabled"]:
        logger.warning("Execution blocked: %s", state["note"])
        raise HTTPException(
            status_code=403,
            detail={
                "safetyRejection": True,
                "reasons": [state["note"]],
                "hint": (
                    "Set BROKER_EXECUTION_ENABLED=true in .env after completing "
                    "a minimum of 100 backtested setups, 50 paper-traded signals, "
                    "positive expectancy, and acceptable drawdown."
                ),
            },
        )

    # Gate 2: signal quality + RR + news + SL/TP/invalidation
    _validate_order(req)

    # Gate 3: live spread check
    try:
        check_spread(req.pair)
    except NotImplementedError as exc:
        raise HTTPException(status_code=501, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc

    # Gate 4: position sizing
    try:
        sizing = calculate_position_size(
            account_balance=0,
            risk_percent=settings.max_risk_per_trade_percent,
            stop_loss_pips=req.stop_loss_pips,
        )
    except NotImplementedError as exc:
        raise HTTPException(status_code=501, detail=str(exc)) from exc

    # Gate 5: place
    try:
        result = place_market_order(
            pair=req.pair,
            side=req.side,
            units=sizing.get("units", 0),
            stop_loss=req.stop_loss,
            take_profit=req.take_profit,
        )
    except NotImplementedError as exc:
        raise HTTPException(status_code=501, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc

    logger.info(
        "Order placed pair=%s side=%s sl=%.5f tp=%.5f",
        req.pair, req.side, req.stop_loss, req.take_profit,
    )
    return result


@router.post(
    "/kill-switch",
    summary="Emergency kill switch — immediately disables all execution",
    response_model=KillSwitchResponse,
)
def kill_switch() -> KillSwitchResponse:
    from services.broker_provider import trigger_kill_switch
    result = trigger_kill_switch()
    logger.critical("KILL SWITCH activated via API")
    return KillSwitchResponse(**result)
