from fastapi import APIRouter, Query, HTTPException

from models.candle import CandleResponse, IntervalLiteral
from models.common import APIResponse
from data.candles import get_candles, INTERVAL_MINUTES
from exceptions import ProviderError

router = APIRouter(prefix="/candles", tags=["candles"])


@router.get(
    "",
    response_model=APIResponse[CandleResponse],
    summary="OHLCV candles for EUR/USD or XAU/USD",
)
def candles(
    interval: str = Query(default="H4", description="Candle interval (M5–W1)"),
    limit: int = Query(default=200, ge=1, le=500, description="Number of candles"),
    pair: str = Query(default="eurusd", description="Pair code: eurusd | xauusd"),
) -> APIResponse[CandleResponse]:
    from pair_config import validate_pair
    try:
        pair = validate_pair(pair)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    try:
        from data.candles import get_candles
        data = get_candles(interval=interval, limit=limit, pair=pair)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except ProviderError as exc:
        raise HTTPException(status_code=exc.http_code, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Candle data error: {exc}") from exc
    return APIResponse(data=data)


@router.get(
    "/fx/{pair_code}",
    response_model=APIResponse[CandleResponse],
    summary="OHLCV candles by pair code path param",
)
def candles_by_pair(
    pair_code: str,
    interval: str = Query(default="H4"),
    limit: int = Query(default=200, ge=1, le=500),
) -> APIResponse[CandleResponse]:
    return candles(interval=interval, limit=limit, pair=pair_code)
