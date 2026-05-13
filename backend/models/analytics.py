from typing import Literal
from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel

_cc = ConfigDict(alias_generator=to_camel, populate_by_name=True)

# ── Legacy mock-data models (kept for backward compat with /analytics/mock) ───

class KPIs(BaseModel):
    win_rate: float
    total_trades: int
    total_pips: int
    total_pnl: float
    avg_rr: float
    profit_factor: float
    max_drawdown: float
    sharpe_ratio: float
    avg_win_pips: int
    avg_loss_pips: int
    expectancy: float
    best_month: str
    worst_month: str
    current_streak: str


class EquityPoint(BaseModel):
    date: str
    equity: float


class MonthlyPnL(BaseModel):
    month: str
    pnl: float


class SessionStat(BaseModel):
    session: str
    win_rate: float
    avg_pips: float
    trades: int


class AnalyticsResponse(BaseModel):
    kpis: KPIs
    equity_curve: list[EquityPoint]
    monthly_pnl: list[MonthlyPnL]
    session_performance: list[SessionStat]


# ── Phase 7 full analytics models (camelCase JSON output) ─────────────────────

SampleSize = Literal["low", "developing", "useful", "strong"]


class SampleInfo(BaseModel):
    model_config = _cc
    size: SampleSize
    completed_trades: int
    message: str


class SessionBreakdown(BaseModel):
    model_config = _cc
    session: str
    trades: int
    wins: int
    win_rate: float
    avg_pips: float


class EquityCurvePoint(BaseModel):
    model_config = _cc
    trade: int
    pips: int
    cumulative_pips: int


class FullAnalyticsResponse(BaseModel):
    model_config = _cc

    sample_info:    SampleInfo

    total_signals:    int
    confirmed_trades: int
    completed_trades: int

    win_rate:          float
    loss_rate:         float
    average_win_pips:  float
    average_loss_pips: float
    average_rr:        float | None
    average_pips:      float
    expectancy:        float
    profit_factor:     float | None
    max_drawdown:      int
    consecutive_wins:  int
    consecutive_losses: int

    long_win_rate:         float | None
    short_win_rate:        float | None
    news_day_win_rate:     float | None
    non_news_day_win_rate: float | None

    best_session:  str | None
    worst_session: str | None
    session_breakdown: list[SessionBreakdown]
    equity_curve:      list[EquityCurvePoint]
