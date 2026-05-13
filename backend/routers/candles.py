from fastapi import APIRouter, Query, HTTPException

from models.candle import CandleResponse, IntervalLiteral
from models.common import APIResponse
from data.candles import get_candles, INTERVAL_MINUTES
from exceptions import ProviderError

router = APIRouter(prefix="/candles", tags=["candles"])


@router.get(
    "",
    response_model=APIResponse[CandleResponse],
    summary="EUR/USD OHLCV candles",
    description=(
        "Returns up to 500 OHLCV candles for EUR/USD. "
        "Valid intervals: M5, M15, M30, H1, H4, D1, W1."
    ),
)
def candles(
    interval: str = Query(default="H4", description="Candle interval (M5–W1)"),
    limit: int = Query(default=200, ge=1, le=500, description="Number of candles"),
) -> APIResponse[CandleResponse]:
    try:
        data = get_candles(interval=interval, limit=limit)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except ProviderError as exc:
        raise HTTPException(status_code=exc.http_code, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Candle data error: {exc}") from exc
    return APIResponse(data=data)
