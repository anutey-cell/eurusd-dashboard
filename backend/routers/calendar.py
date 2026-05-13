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
        "Returns today's EUR and USD calendar events with impact ratings "
        "and a recent news feed. Supply ?date=YYYY-MM-DD to query another day."
    ),
)
def calendar(
    date: str | None = Query(default=None, description="Date filter YYYY-MM-DD (default: today)"),
) -> APIResponse[CalendarResponse]:
    try:
        data = get_calendar(date=date)
    except ProviderError as exc:
        raise HTTPException(status_code=exc.http_code, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Calendar data error: {exc}") from exc
    return APIResponse(data=data)
