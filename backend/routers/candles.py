from fastapi import APIRouter, Query, HTTPException

from models.candle import CandleResponse, IntervalLiteral
from models.common import APIResponse
from data.candles import get_candles, INTERVAL_MINUTES
from exceptions import ProviderError

router = APIRouter(prefix="/candles", tags=["candles"])

_UNSUPPORTED_MSG = (
    "Unsupported instrument. This dashboard supports XAU/USD only. "
    "Use pair=xauusd or GET /api/v1/candles/fx/xauusd."
)


def _validate_xauusd(pair: str) -> str:
    """Validate pair is xauusd; raises HTTP 400 for anything else."""
    from pair_config import validate_pair
    try:
        return validate_pair(pair)
    except ValueError:
        raise HTTPException(status_code=400, detail=_UNSUPPORTED_MSG)


@router.get(
    "",
    response_model=APIResponse[CandleResponse],
    summary="OHLCV candles for XAU/USD",
    description="Returns H4 candles for XAU/USD. Only pair=xauusd is accepted.",
)
def candles(
    interval: str = Query(default="H4", description="Candle interval (M5–W1)"),
    limit: int    = Query(default=200, ge=1, le=500, description="Number of candles"),
    pair: str     = Query(default="xauusd", description="Instrument code — only xauusd supported"),
) -> APIResponse[CandleResponse]:
    pair = _validate_xauusd(pair)
    try:
        data = get_candles(interval=interval, limit=limit, pair=pair)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except ProviderError as exc:
        raise HTTPException(status_code=exc.http_code, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Candle data error: {exc}") from exc
    # Propagate the actual provider (tradingview | mt5 | synthetic | demo)
    return APIResponse(data=data, source=getattr(data, "source", "synthetic"))


@router.get(
    "/fx/{pair_code}",
    response_model=APIResponse[CandleResponse],
    summary="OHLCV candles by path param — only xauusd accepted",
    description=(
        "Convenience route: GET /candles/fx/xauusd. "
        "Any other pair code returns HTTP 400."
    ),
)
def candles_by_pair(
    pair_code: str,
    interval: str = Query(default="H4"),
    limit: int    = Query(default=200, ge=1, le=500),
) -> APIResponse[CandleResponse]:
    return candles(interval=interval, limit=limit, pair=pair_code)
