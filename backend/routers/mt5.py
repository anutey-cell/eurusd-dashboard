"""
MT5 demo integration endpoints.

GET  /api/v1/mt5/status          — account status (masked login, flags, balance)
GET  /api/v1/mt5/symbols         — resolved broker symbols for both pairs
GET  /api/v1/mt5/tick/{pair}     — live bid/ask/spread
GET  /api/v1/mt5/positions       — open demo positions
GET  /api/v1/mt5/history         — recent trade history from MT5
GET  /api/v1/mt5/logs            — trade attempt log from our DB (paginated, newest first)
POST /api/v1/mt5/demo-order      — place demo market order (13-gate validation)

Security: mt5_password is NEVER logged or returned. Login is masked (***last4).
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from database import get_db
from rate_limit import limiter
from services.mt5_provider import (
    MT5NotConnectedError,
    MT5SafetyError,
    MT5UnavailableError,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/mt5", tags=["mt5"])


# ── Request / response models ─────────────────────────────────────────────────

class DemoOrderRequest(BaseModel):
    pair:         str   = Field(..., description="xauusd (only supported instrument)")
    signal:       str   = Field(..., description="BUY or SELL")
    entry:        float = Field(..., description="Expected entry price")
    stopLoss:     float = Field(..., description="Stop loss price")
    takeProfit:   float = Field(..., description="Take profit price")
    riskPips:     float = Field(..., description="Stop distance in pips/points")
    targetPips:   int   = Field(..., description="Target in pips/points")
    rr:           float = Field(..., description="Risk-reward ratio")
    qualityScore: int   = Field(..., description="Signal quality score 0-100")
    newsStatus:   str   = Field(..., description="CLEAR or BLOCKED")
    reason:       str   = Field("",  description="ICT setup description")


# ── Helpers ───────────────────────────────────────────────────────────────────

def _unavailable_response() -> dict:
    """Graceful response when MT5 package is not installed."""
    from config import settings
    return {
        "connected":            False,
        "mode":                 "unavailable",
        "error":                (
            "MetaTrader5 package not available. "
            "It is Windows-only. Run: pip install MetaTrader5 on a Windows host "
            "with the MT5 terminal installed."
        ),
        "execution_enabled":    settings.mt5_execution_enabled,
        "demo_trading_allowed": settings.allow_demo_trading,
    }


def _not_connected_response(detail: str) -> dict:
    """Graceful response when MT5 is installed but not connected."""
    from config import settings
    return {
        "connected":            False,
        "mode":                 "disconnected",
        "error":                detail,
        "execution_enabled":    settings.mt5_execution_enabled,
        "demo_trading_allowed": settings.allow_demo_trading,
    }


# ── Endpoints ─────────────────────────────────────────────────────────────────

@router.get("/status", summary="MT5 account status (masked login, flags, balance)")
def mt5_status() -> dict:
    """
    Returns MT5 connection status and account info.
    Login is masked (***last4). Password is never included.
    Returns a graceful JSON response when MT5 is not available rather than raising.
    """
    from services.mt5_provider import get_account_info

    try:
        return get_account_info()
    except MT5UnavailableError:
        return _unavailable_response()
    except MT5NotConnectedError as exc:
        return _not_connected_response(str(exc))
    except Exception as exc:
        logger.exception("Unexpected error in mt5_status")
        return _not_connected_response(f"Unexpected error: {exc}")


@router.get("/symbols", summary="Resolved broker symbol for XAU/USD")
def mt5_symbols() -> dict:
    """
    Attempts to resolve the broker symbol name for XAU/USD (XAUUSD, XAUUSDm, GOLD, etc.).
    Returns a graceful response when MT5 is not available.
    """
    from services.mt5_provider import get_symbol_info

    try:
        return {
            "xauusd": get_symbol_info("xauusd"),
        }
    except MT5UnavailableError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except MT5NotConnectedError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception("Unexpected error in mt5_symbols")
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.get("/tick/{pair}", summary="Live bid/ask/spread for a pair")
def mt5_tick(pair: str) -> dict:
    """
    Returns the latest bid, ask, and spread (in points) for XAU/USD.
    Only xauusd is supported. Other pair codes return 422.
    """
    from services.mt5_provider import get_tick

    try:
        return get_tick(pair)
    except MT5UnavailableError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except MT5NotConnectedError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception("Unexpected error in mt5_tick pair=%s", pair)
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.get("/positions", summary="Open demo positions")
def mt5_positions() -> list[dict]:
    """
    Returns all currently open positions on the connected MT5 demo account.
    """
    from services.mt5_provider import get_open_positions

    try:
        return get_open_positions()
    except MT5UnavailableError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except MT5NotConnectedError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception("Unexpected error in mt5_positions")
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.get("/history", summary="Recent closed trade history from MT5 (last 30 days)")
def mt5_history() -> list[dict]:
    """
    Returns up to 50 recent closed deals from the MT5 account history (last 30 days).
    """
    from services.mt5_provider import get_trade_history

    try:
        return get_trade_history(last_n=50)
    except MT5UnavailableError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except MT5NotConnectedError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception("Unexpected error in mt5_history")
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.get("/logs", summary="Trade attempt log from our DB (newest first, max 100)")
def mt5_logs(
    db: Session = Depends(get_db),
    skip: int = 0,
    limit: int = 100,
) -> list[dict]:
    """
    Returns trade attempt records (accepted, rejected, failed) stored in our
    local database, newest first. Max 100 rows per call. Use skip for pagination.
    """
    from db_models import MT5TradeLog

    limit = min(limit, 100)
    records = (
        db.query(MT5TradeLog)
        .order_by(MT5TradeLog.created_at.desc())
        .offset(skip)
        .limit(limit)
        .all()
    )

    return [
        {
            "id":               r.id,
            "created_at":       r.created_at.isoformat() if r.created_at else None,
            "mode":             r.mode,
            "pair":             r.pair,
            "broker_symbol":    r.broker_symbol,
            "signal":           r.signal,
            "volume":           r.volume,
            "entry":            r.entry,
            "stop_loss":        r.stop_loss,
            "take_profit":      r.take_profit,
            "risk_percent":     r.risk_percent,
            "risk_amount":      r.risk_amount,
            "spread":           r.spread,
            "ticket":           r.ticket,
            "status":           r.status,
            "rejection_reason": r.rejection_reason,
        }
        for r in records
    ]


@router.post(
    "/demo-order",
    summary="Place a demo market order (13-gate safety validation)",
    description=(
        "Validates and places a market order on the connected MT5 demo account. "
        "Passes through 13 safety gates: execution flags, signal quality, RR, "
        "SL/TP presence, news status, pair validation, MT5 connectivity, demo-only "
        "account check, max open trades, daily loss limit, spread check, and "
        "duplicate position check. "
        "Every attempt (accepted, rejected, or failed) is logged to the database. "
        "Rate-limited to 5 requests/minute per IP. "
        "Requires MT5_EXECUTION_ENABLED=true and ALLOW_DEMO_TRADING=true in .env."
    ),
)
@limiter.limit("5/minute")
def place_demo_order(request: Request, body: DemoOrderRequest) -> dict:
    """
    Place a demo market order with full 13-gate safety validation.

    Returns the accepted order details on success.
    Returns HTTP 422 with safetyRejection=true if any gate fails.
    Returns HTTP 503 if MT5 is unavailable or not connected.
    """
    from services.mt5_provider import place_demo_market_order

    logger.warning(
        "Demo order attempt pair=%s signal=%s quality=%s rr=%s ip=%s",
        body.pair, body.signal, body.qualityScore, body.rr,
        request.client.host if request.client else "?",
    )

    # Convert Pydantic model to plain dict for the provider
    signal_dict = {
        "pair":         body.pair,
        "signal":       body.signal,
        "entry":        body.entry,
        "stopLoss":     body.stopLoss,
        "takeProfit":   body.takeProfit,
        "riskPips":     body.riskPips,
        "targetPips":   body.targetPips,
        "rr":           body.rr,
        "qualityScore": body.qualityScore,
        "newsStatus":   body.newsStatus,
        "reason":       body.reason,
    }

    try:
        result = place_demo_market_order(signal_dict)
        logger.info(
            "Demo order accepted pair=%s signal=%s ticket=%s lot=%.2f",
            body.pair, body.signal, result.get("ticket"), result.get("lot_size"),
        )
        return result
    except MT5SafetyError as exc:
        reasons = [r.strip() for r in str(exc).split("|") if r.strip()]
        raise HTTPException(
            status_code=422,
            detail={"safetyRejection": True, "reasons": reasons},
        ) from exc
    except MT5UnavailableError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except MT5NotConnectedError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception(
            "Unexpected error placing demo order pair=%s signal=%s",
            body.pair, body.signal,
        )
        raise HTTPException(status_code=500, detail=str(exc)) from exc
