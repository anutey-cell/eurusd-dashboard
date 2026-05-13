"""
Backtesting and optimization endpoints.

GET  /api/v1/backtest/run?pair=EUR/USD&timeframe=H4&lookback=500
GET  /api/v1/backtest/optimize?pair=EUR/USD&timeframe=H4&lookback=1000

Rate limits:
  /run      — 5 requests/minute per IP  (CPU-intensive walk-forward scan)
  /optimize — 3 requests/minute per IP  (grid × IS + OOS)

The legacy /backtest/eurusd route is kept for backwards compatibility.
"""
import logging

from fastapi import APIRouter, HTTPException, Query, Request

from data.candles import get_candles, INTERVAL_MINUTES
from exceptions import ProviderError
from pair_config import SUPPORTED_PAIRS, DEFAULT_PAIR, DEFAULT_PAIR_CODE, validate_pair
from rate_limit import limiter

router = APIRouter(prefix="/backtest", tags=["backtest"])
logger = logging.getLogger(__name__)

VALID_TIMEFRAMES = list(INTERVAL_MINUTES.keys())


def _fetch_candles(pair: str, timeframe: str, lookback: int):
    """Fetch candles for any supported pair (pair is already a normalised code)."""
    return get_candles(interval=timeframe, limit=lookback, pair=pair)


# ── /backtest/run ─────────────────────────────────────────────────────────────

@router.get(
    "/run",
    summary="Backtest the ICT signal engine on historical candles",
    description=(
        "Walk-forward backtest with no look-ahead bias. "
        "Rate-limited to 5 requests/minute per IP."
    ),
)
@limiter.limit("5/minute")
def backtest_run(
    request: Request,
    pair: str = Query(
        default=DEFAULT_PAIR,
        description=f"Trading pair. Supported: {', '.join(SUPPORTED_PAIRS)}",
    ),
    timeframe: str = Query(
        default="H4",
        description=f"Candle timeframe. Valid: {', '.join(VALID_TIMEFRAMES)}",
    ),
    lookback: int = Query(
        default=500,
        ge=100,
        le=5000,
        description="Number of historical candles (100–5000)",
    ),
) -> dict:
    tf = timeframe.upper()
    if tf not in INTERVAL_MINUTES:
        raise HTTPException(
            status_code=422,
            detail=f"Unknown timeframe '{timeframe}'. Valid: {VALID_TIMEFRAMES}",
        )
    try:
        pair = validate_pair(pair)   # normalise to code ("eurusd" | "xauusd")
    except ValueError:
        raise HTTPException(
            status_code=422,
            detail=f"Unsupported pair '{pair}'. Valid codes: {list(SUPPORTED_PAIRS)}",
        )

    logger.info(
        "Backtest requested pair=%s timeframe=%s lookback=%d ip=%s",
        pair, tf, lookback, request.client.host if request.client else "?",
    )

    try:
        candle_resp = _fetch_candles(pair, tf, lookback)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except ProviderError as exc:
        logger.warning("Provider error in backtest pair=%s: %s", pair, exc)
        raise HTTPException(status_code=exc.http_code, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Candle data error: {exc}") from exc

    from services.backtester import run_backtest
    result = run_backtest(candle_resp.candles, pair=pair)
    logger.info(
        "Backtest complete pair=%s trades=%s winRate=%s expectancy=%s",
        pair, result.get("totalTrades"), result.get("winRate"), result.get("expectancy"),
    )
    return result


# ── /backtest/optimize ────────────────────────────────────────────────────────

@router.get(
    "/optimize",
    summary="Walk-forward parameter optimization",
    description=(
        "Grid search over signal_window × min_rr on IS slice (70%), "
        "validates best params on OOS (30%). "
        "Rate-limited to 3 requests/minute per IP."
    ),
)
@limiter.limit("3/minute")
def backtest_optimize(
    request: Request,
    pair: str = Query(
        default=DEFAULT_PAIR,
        description=f"Trading pair. Supported: {', '.join(SUPPORTED_PAIRS)}",
    ),
    timeframe: str = Query(
        default="H4",
        description=f"Candle timeframe. Valid: {', '.join(VALID_TIMEFRAMES)}",
    ),
    lookback: int = Query(
        default=1000,
        ge=200,
        le=5000,
        description="Number of historical candles for IS+OOS split (200–5000)",
    ),
) -> dict:
    tf = timeframe.upper()
    if tf not in INTERVAL_MINUTES:
        raise HTTPException(
            status_code=422,
            detail=f"Unknown timeframe '{timeframe}'. Valid: {VALID_TIMEFRAMES}",
        )
    try:
        pair = validate_pair(pair)   # normalise to code
    except ValueError:
        raise HTTPException(
            status_code=422,
            detail=f"Unsupported pair '{pair}'. Valid codes: {list(SUPPORTED_PAIRS)}",
        )

    logger.info(
        "Optimizer requested pair=%s timeframe=%s lookback=%d ip=%s",
        pair, tf, lookback, request.client.host if request.client else "?",
    )

    try:
        candle_resp = _fetch_candles(pair, tf, lookback)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except ProviderError as exc:
        logger.warning("Provider error in optimize pair=%s: %s", pair, exc)
        raise HTTPException(status_code=exc.http_code, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Candle data error: {exc}") from exc

    from services.optimizer import optimize
    result = optimize(candle_resp.candles, pair=pair)

    if "error" in result:
        raise HTTPException(status_code=422, detail=result)

    logger.info(
        "Optimizer complete pair=%s bestParams=%s overfitWarning=%s",
        pair, result.get("bestParams"), result.get("overfitWarning"),
    )
    return result


# ── /backtest/eurusd — backwards-compatible alias ─────────────────────────────

@router.get(
    "/eurusd",
    summary="Backtest EUR/USD (legacy alias — use /backtest/run instead)",
    include_in_schema=False,
)
@limiter.limit("5/minute")
def backtest_eurusd(
    request: Request,
    timeframe: str = Query(default="H4"),
    lookback:  int = Query(default=500, ge=100, le=5000),
) -> dict:
    return backtest_run(request=request, pair="EUR/USD", timeframe=timeframe, lookback=lookback)
