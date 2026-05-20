from fastapi import APIRouter, Query, HTTPException

from models.calendar import CalendarResponse
from models.common import APIResponse
from data.calendar import get_calendar
from exceptions import ProviderError

router = APIRouter(prefix="/calendar", tags=["calendar"])


@router.get(
    "",
    response_model=APIResponse[CalendarResponse],
    summary="Macro economic calendar + news",
    description=(
        "Returns today's EUR/USD calendar events with impact ratings. "
        "Supply ?date=YYYY-MM-DD to query another day. "
        "Pass ?high_impact=true to filter to high-impact events only "
        "(CPI, NFP, FOMC, interest-rate decisions). "
        "Pass ?refresh=true to bypass any provider-side cache for fresh data."
    ),
)
def calendar(
    date:         str | None = Query(default=None, description="Date filter YYYY-MM-DD (default: today)"),
    high_impact:  bool       = Query(default=False, description="When true, return only impact='high' events"),
    refresh:      bool       = Query(default=False, description="When true, force provider re-fetch (no cache)"),
) -> APIResponse[CalendarResponse]:
    try:
        data = get_calendar(date=date, high_impact_only=high_impact, force_refresh=refresh)
    except ProviderError as exc:
        raise HTTPException(status_code=exc.http_code, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Calendar data error: {exc}") from exc
    # Surface provider info via the source field so frontend can show the chip
    from config import settings
    src = settings.calendar_provider if settings.data_mode == "live" else "demo"
    return APIResponse(data=data, source=src)
