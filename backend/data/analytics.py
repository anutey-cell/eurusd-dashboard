"""
Mock analytics data generator.
Replace with real aggregations from a trade database in Phase 3.
"""
import random
from datetime import date, timedelta

from models.analytics import (
    AnalyticsResponse, KPIs, EquityPoint, MonthlyPnL, SessionStat,
)

_KPIS = KPIs(
    win_rate=70.0,
    total_trades=201,
    total_pips=1847,
    total_pnl=3240.0,
    avg_rr=1.94,
    profit_factor=2.31,
    max_drawdown=-8.4,
    sharpe_ratio=1.72,
    avg_win_pips=41,
    avg_loss_pips=23,
    expectancy=18.4,
    best_month="Jan +$1,050",
    worst_month="Dec -$180",
    current_streak="+5 wins",
)

_MONTHLY_PNL = [
    MonthlyPnL(month="Nov", pnl=620),
    MonthlyPnL(month="Dec", pnl=-180),
    MonthlyPnL(month="Jan", pnl=1050),
    MonthlyPnL(month="Feb", pnl=430),
    MonthlyPnL(month="Mar", pnl=870),
    MonthlyPnL(month="Apr", pnl=-220),
    MonthlyPnL(month="May", pnl=1320),
    MonthlyPnL(month="Jun", pnl=580),
    MonthlyPnL(month="Jul", pnl=240),
    MonthlyPnL(month="Aug", pnl=-95),
    MonthlyPnL(month="Sep", pnl=760),
    MonthlyPnL(month="Oct", pnl=990),
]

_SESSION_STATS = [
    SessionStat(session="Tokyo",   win_rate=48, avg_pips=18, trades=21),
    SessionStat(session="London",  win_rate=72, avg_pips=38, trades=87),
    SessionStat(session="NY Open", win_rate=68, avg_pips=32, trades=64),
    SessionStat(session="NY PM",   win_rate=54, avg_pips=14, trades=29),
]


def _build_equity_curve(days: int = 90) -> list[EquityPoint]:
    rng = random.Random(99)
    points: list[EquityPoint] = []
    equity = 10000.0
    start = date(2026, 2, 13)
    for i in range(days):
        d = start + timedelta(days=i)
        if d.weekday() >= 5:
            continue
        equity += rng.gauss(18, 120)
        equity = max(9000.0, equity)
        points.append(EquityPoint(date=d.isoformat(), equity=round(equity, 2)))
    return points


_EQUITY_CURVE = _build_equity_curve()


def get_analytics() -> AnalyticsResponse:
    return AnalyticsResponse(
        kpis=_KPIS,
        equity_curve=_EQUITY_CURVE,
        monthly_pnl=_MONTHLY_PNL,
        session_performance=_SESSION_STATS,
    )
