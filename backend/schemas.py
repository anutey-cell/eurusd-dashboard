"""
Pydantic schemas for database I/O.

Naming convention:
  *Create  — request body when writing to DB
  *Read    — response body when reading from DB
  *Update* — request body for partial updates

All schemas use camelCase JSON aliases (via alias_generator=to_camel) so the
frontend can POST the /analyze response directly to /confirm without adaptation.
"""
from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel

_cc = ConfigDict(alias_generator=to_camel, populate_by_name=True, from_attributes=True)

TradeResult   = Literal["WIN", "LOSS", "BREAKEVEN"]
SignalDir     = Literal["BUY", "SELL", "WAIT"]
NewsStatusVal = Literal["CLEAR", "BLOCKED"]
ImpactVal     = Literal["high", "medium", "low"]


# ── Signal ────────────────────────────────────────────────────────────────────

class SignalCreate(BaseModel):
    """
    Body for POST /signal/confirm.
    Mirrors SignalAnalysisOutput so the frontend can post the /analyze response
    directly after flattening the nested model field.

    Required flat fields from model{}:
      liquidity_status  → model.liquidity
      fvg_status        → model.fvg
      structure_status  → model.structure
      macro_status      → model.higherTimeframeBias
      session           → model.session
    """
    model_config = _cc

    pair:             str          = "XAU/USD"
    timeframe:        str          = "H4"
    signal:           SignalDir
    quality_score:    int          = Field(ge=0, le=100)
    entry:            float | None = None
    stop_loss:        float | None = None
    take_profit:      float | None = None
    risk_pips:        int   | None = None
    target_pips:      int          = 50
    rr:               float | None = None
    invalidation:     float | None = None
    liquidity_status: str          = ""
    fvg_status:       str          = ""
    structure_status: str          = ""
    macro_status:     str          = ""
    news_status:      NewsStatusVal = "CLEAR"
    session:          str          = ""
    reason:           str          = ""


class SignalRead(BaseModel):
    """Full signal record returned from DB queries."""
    model_config = _cc

    id:               int
    created_at:       datetime
    pair:             str
    timeframe:        str
    signal:           str
    quality_score:    int
    entry:            float | None
    stop_loss:        float | None
    take_profit:      float | None
    risk_pips:        int   | None
    target_pips:      int
    rr:               float | None
    invalidation:     float | None
    liquidity_status: str
    fvg_status:       str
    structure_status: str
    macro_status:     str
    news_status:      str
    session:          str
    reason:           str
    confirmed:        bool
    result:           str   | None
    exit_price:       float | None
    pips:             int   | None
    pnl:              float | None
    notes:            str   | None


class SignalUpdateResult(BaseModel):
    """Body for PUT /signal/{id}/result."""
    model_config = _cc

    result:     TradeResult
    exit_price: float
    pips:       int
    pnl:        float | None = None
    notes:      str   | None = None


# ── Macro event ───────────────────────────────────────────────────────────────

class MacroEventCreate(BaseModel):
    model_config = _cc

    event_time: datetime
    currency:   str
    event_name: str
    impact:     ImpactVal
    actual:     str | None = None
    forecast:   str | None = None
    previous:   str | None = None
    source:     str        = "manual"


class MacroEventRead(BaseModel):
    model_config = _cc

    id:         int
    event_time: datetime
    currency:   str
    event_name: str
    impact:     str
    actual:     str | None
    forecast:   str | None
    previous:   str | None
    source:     str


# ── Candle cache ──────────────────────────────────────────────────────────────

class CandleRead(BaseModel):
    model_config = _cc

    id:          int
    pair:        str
    timeframe:   str
    candle_time: datetime
    open:        float
    high:        float
    low:         float
    close:       float
    volume:      int
    source:      str


# ── Analytics (computed from DB records) ─────────────────────────────────────

class AnalyticsRead(BaseModel):
    model_config = _cc

    total_confirmed:  int
    total_trades:     int     # confirmed signals that have a result
    pending:          int
    wins:             int
    losses:           int
    breakevens:       int
    win_rate:         float   # wins / total_trades * 100, or 0
    total_pips:       int
    avg_rr:           float | None
    avg_quality_score: float | None


# ── Paginated history response ────────────────────────────────────────────────

class SignalHistoryDB(BaseModel):
    model_config = _cc

    total:     int
    page:      int
    page_size: int
    signals:   list[SignalRead]
