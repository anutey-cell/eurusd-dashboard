from datetime import datetime
from typing import Literal
from pydantic import BaseModel


Impact = Literal["high", "medium", "low"]


class CalendarEvent(BaseModel):
    id: int
    time: datetime
    currency: str
    event: str
    impact: Impact
    forecast: str | None
    previous: str | None
    actual: str | None
    pending: bool
    beat: bool | None = None

    model_config = {"json_encoders": {datetime: lambda v: v.isoformat()}}


class NewsItem(BaseModel):
    id: int
    time: str
    headline: str
    source: str
    sentiment: Literal["bullish", "bearish", "neutral"]


class CalendarResponse(BaseModel):
    date: str
    events: list[CalendarEvent]
    news: list[NewsItem]
