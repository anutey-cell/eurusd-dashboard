from datetime import datetime
from typing import Literal, Optional
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
    label:   str
    price:   float
    rr:      str
    pips:    Optional[int] = None
    partial: str


class TradePlan(BaseModel):
    entry:          Optional[float] = None
    stop_loss:      Optional[float] = None
    stop_loss_pips: Optional[int]   = None
    targets:        list[TradePlanTarget] = []
    risk_percent:   Optional[float] = None
    position_size:  Optional[float] = None
    account_size:   Optional[float] = None
    risk_amount:    Optional[float] = None
    validity:       Optional[datetime] = None
    notes:          str = ""


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
    sentiment: dict | None = None    # MyFXBook community sentiment (optional)


class SignalAnalysisOutput(BaseModel):
    """Flat signal output matching the expected frontend format."""
    model_config = _camel_cfg

    pair:         str = "xauusd"
    display_pair: str = "XAU/USD"
    signal:       EngineDirection
    quality_score: int        = Field(ge=0, le=100)
    entry:        float | None = None
    stop_loss:    float | None = None
    take_profit:  float | None = None
    risk_pips:    int   | None = None
    target_pips:  int          = 50
    rr:           float | None = None
    invalidation: float | None = None
    reason:       str
    news_status:  NewsStatus
    model:        SignalModelDetail
    # Telegram alert delivery status — set by the signal router after analysis.
    # Values: "sent" | "disabled" | "not_qualified" | "duplicate_skipped" | "failed"
    alert_status: str | None = None

    # Operating mode — reflects the pair's current mode at the time of analysis
    pair_mode: str | None = None   # "MONITOR_ONLY" | "PAPER_OBSERVATION" | "DEMO_ELIGIBLE" | "LIVE_READ_ONLY" | "DISABLED"

    # Learning / data-source transparency fields
    data_source:        str  | None = None   # "live" | "tradingview" | "synthetic" | "stale" | "disabled"
    weights_used:       dict | None = None   # adaptive weights applied {htf, liq, ms, fvg, news, session}
    component_snapshot: str  | None = None   # JSON — which ICT gates were active (for DB learning)
