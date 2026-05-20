"""
XAU/USD macro calendar data layer.

In demo mode (DATA_MODE=demo), returns the static mock events below.
In live mode (DATA_MODE=live), delegates to calendar_provider which routes to
the configured CALENDAR_PROVIDER (fmp | trading_economics | eodhd | broker).
"""
from datetime import datetime, timezone

from models.calendar import CalendarEvent, CalendarResponse, NewsItem

_EVENTS: list[CalendarEvent] = [
    CalendarEvent(
        id=1,
        time=datetime(2026, 5, 13, 10, 0, 0, tzinfo=timezone.utc),
        currency="USD",
        event="Core CPI m/m",
        impact="high",
        forecast="0.3%",
        previous="0.4%",
        actual=None,
        pending=True,
    ),
    CalendarEvent(
        id=2,
        time=datetime(2026, 5, 13, 12, 30, 0, tzinfo=timezone.utc),
        currency="USD",
        event="FOMC Member Speech",
        impact="medium",
        forecast=None,
        previous=None,
        actual=None,
        pending=True,
    ),
    CalendarEvent(
        id=3,
        time=datetime(2026, 5, 13, 7, 0, 0, tzinfo=timezone.utc),
        currency="EUR",
        event="German ZEW Economic Sentiment",
        impact="medium",
        forecast="12.3",
        previous="11.9",
        actual="13.1",
        pending=False,
        beat=True,
    ),
    CalendarEvent(
        id=4,
        time=datetime(2026, 5, 13, 6, 0, 0, tzinfo=timezone.utc),
        currency="EUR",
        event="French Industrial Output m/m",
        impact="low",
        forecast="0.2%",
        previous="-0.1%",
        actual="0.4%",
        pending=False,
        beat=True,
    ),
    CalendarEvent(
        id=5,
        time=datetime(2026, 5, 13, 14, 0, 0, tzinfo=timezone.utc),
        currency="USD",
        event="Michigan Consumer Sentiment",
        impact="medium",
        forecast="76.5",
        previous="75.1",
        actual=None,
        pending=True,
    ),
]

_NEWS: list[NewsItem] = [
    NewsItem(
        id=1,
        time="08:15",
        headline="ECB Lagarde signals data-dependent pause; EUR rallies on reduced cut expectations",
        source="Reuters",
        sentiment="bullish",
    ),
    NewsItem(
        id=2,
        time="07:44",
        headline="US Treasury yields dip ahead of CPI print; DXY softens toward 104.20",
        source="Bloomberg",
        sentiment="bullish",
    ),
    NewsItem(
        id=3,
        time="07:22",
        headline="German ZEW beats estimates at 13.1 vs 12.3 forecast, boosting EUR sentiment",
        source="ForexFactory",
        sentiment="bullish",
    ),
    NewsItem(
        id=4,
        time="06:50",
        headline="Fed officials push back on near-term cut expectations amid sticky services inflation",
        source="WSJ",
        sentiment="bearish",
    ),
    NewsItem(
        id=5,
        time="06:12",
        headline="XAU/USD consolidates near $3285 — gold bulls target 3300 breakout on USD weakness",
        source="FXStreet",
        sentiment="bullish",
    ),
]


def get_calendar(
    date: str | None = None,
    *,
    high_impact_only: bool = False,
    force_refresh:    bool = False,
) -> CalendarResponse:
    """
    Fetch macro calendar events.

    Parameters
    ----------
    date              ISO date YYYY-MM-DD. Defaults to today.
    high_impact_only  When True, returns only impact="high" events (CPI, NFP, FOMC, etc.)
    force_refresh     When True, bypasses any provider-side cache for fresh data.
    """
    from config import settings

    if settings.data_mode == "live":
        from services.calendar_provider import get_macro_calendar
        # The provider may accept force_refresh kw; pass it best-effort.
        try:
            live_events_raw = get_macro_calendar(date, force_refresh=force_refresh)
        except TypeError:
            live_events_raw = get_macro_calendar(date)
        live_events = [CalendarEvent(**e) for e in live_events_raw]
        if high_impact_only:
            live_events = [e for e in live_events if (e.impact or "").lower() == "high"]
        target = date or datetime.now(timezone.utc).strftime("%Y-%m-%d")
        return CalendarResponse(date=target, events=live_events, news=[])

    # Demo mode: serve mock — but still honour the high_impact_only filter
    events = _EVENTS
    if high_impact_only:
        events = [e for e in events if (e.impact or "").lower() == "high"]
    target = date or "2026-05-13"
    return CalendarResponse(date=target, events=events, news=_NEWS)
