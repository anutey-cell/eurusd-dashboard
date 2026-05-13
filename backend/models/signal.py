from datetime import datetime
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel


Direction = Literal["BUY", "SELL", "NEUTRAL"]
TradeResult = Literal["WIN", "LOSS", "BREAKEVEN", "PENDING"]


class SignalFactor(BaseModel):
    name: str
    value: str
    score: int = Field(ge=0, le=100)
    positive: bool


class CurrentSignal(BaseModel):
    direction: Direction
    strength: int = Field(ge=0, le=100)
    confidence: int = Field(ge=0, le=100)
    timestamp: datetime
    session: str
    timeframe: str
    price: float
    change: float
    change_pct: float
    factors: list[SignalFactor]


class TradePlanTarget(BaseModel):
    label: str
    price: float
    rr: str
    pips: int
    partial: str


class TradePlan(BaseModel):
    entry: float
    stop_loss: float
    stop_loss_pips: int
    targets: list[TradePlanTarget]
    risk_percent: float
    position_size: float
    account_size: float
    risk_amount: float
    validity: datetime
    notes: str


class SignalOutput(BaseModel):
    signal: CurrentSignal
    trade_plan: TradePlan


class HistoricalTrade(BaseModel):
    id: int
    date: str
    time: str
    direction: Direction
    entry: float
    exit: float
    pips: int
    rr: float
    result: TradeResult
    pnl: float
    confidence: int


class SignalHistoryResponse(BaseModel):
    total: int
    page: int
    page_size: int
    trades: list[HistoricalTrade]


class ConfirmSignalResponse(BaseModel):
    signal_id: int
    confirmed: bool
    message: str


class UpdateResultRequest(BaseModel):
    result: TradeResult
    exit_price: float
    pips: int
    pnl: float
    notes: str | None = None


class UpdateResultResponse(BaseModel):
    signal_id: int
    updated: bool
    result: TradeResult
    exit_price: float
    pips: int
    pnl: float


# ── Engine analysis models (camelCase JSON output) ────────────────────────────

EngineDirection = Literal["BUY", "SELL", "WAIT"]
NewsStatus      = Literal["CLEAR", "BLOCKED"]

_camel_cfg = ConfigDict(alias_generator=to_camel, populate_by_name=True)


class SignalModelDetail(BaseModel):
    """Human-readable state of each ICT component."""
    model_config = _camel_cfg

    higher_timeframe_bias: str
    liquidity: str
    structure: str
    fvg: str
    session: str


class SignalAnalysisOutput(BaseModel):
    """Flat signal output matching the expected frontend format."""
    model_config = _camel_cfg

    signal:       EngineDirection
    quality_score: int        = Field(ge=0, le=100)
    entry:        float | None = None
    stop_loss:    float | None = None
    take_profit:  float | None = None
    risk_pips:    int   | None = None
    target_pips:  int          = 40
    rr:           float | None = None
    invalidation: float | None = None
    reason:       str
    news_status:  NewsStatus
    model:        SignalModelDetail
