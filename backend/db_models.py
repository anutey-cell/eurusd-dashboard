"""
SQLAlchemy ORM table definitions.
Intentionally separate from Pydantic models in models/ to avoid naming collisions.
"""
from datetime import datetime, timezone

from sqlalchemy import (
    Boolean, Column, DateTime, Float, Integer, String, Text, UniqueConstraint,
)
from sqlalchemy.sql import func

from database import Base


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ── signals ───────────────────────────────────────────────────────────────────

class SignalRecord(Base):
    __tablename__ = "signals"

    id              = Column(Integer,  primary_key=True, index=True, autoincrement=True)
    created_at      = Column(DateTime(timezone=True), default=_now, nullable=False)
    pair            = Column(String(16),  default="XAU/USD", nullable=False)
    timeframe       = Column(String(8),   default="H4",      nullable=False)

    # Engine output
    signal          = Column(String(8),   nullable=False)   # BUY | SELL | WAIT
    quality_score   = Column(Integer,     nullable=False)
    entry           = Column(Float,       nullable=True)
    stop_loss       = Column(Float,       nullable=True)
    take_profit     = Column(Float,       nullable=True)
    risk_pips       = Column(Integer,     nullable=True)
    target_pips     = Column(Integer,     default=40, nullable=False)
    rr              = Column(Float,       nullable=True)
    invalidation    = Column(Float,       nullable=True)

    # Model component descriptions (from engine model field)
    liquidity_status  = Column(Text,   nullable=False, default="")
    fvg_status        = Column(Text,   nullable=False, default="")
    structure_status  = Column(Text,   nullable=False, default="")
    macro_status      = Column(Text,   nullable=False, default="")   # HTF bias text
    news_status       = Column(String(8), nullable=False, default="CLEAR")  # CLEAR | BLOCKED
    session           = Column(String(64), nullable=False, default="")
    reason            = Column(Text,   nullable=False, default="")

    # Confirmation
    confirmed       = Column(Boolean, default=False, nullable=False)

    # Result (filled in manually after trade closes)
    result          = Column(String(16), nullable=True)   # WIN | LOSS | BREAKEVEN
    exit_price      = Column(Float,  nullable=True)
    pips            = Column(Integer, nullable=True)
    pnl             = Column(Float,  nullable=True)
    notes           = Column(Text,   nullable=True)

    # Snapshot of which ICT components were active at signal time (JSON)
    # e.g. {"htf": true, "liq": true, "ms": false, "fvg": true, "news": true, "session": true}
    component_snapshot = Column(Text, nullable=True)


# ── macro_events ──────────────────────────────────────────────────────────────

class MacroEventRecord(Base):
    __tablename__ = "macro_events"

    id          = Column(Integer, primary_key=True, index=True, autoincrement=True)
    event_time  = Column(DateTime(timezone=True), nullable=False, index=True)
    currency    = Column(String(8),  nullable=False)
    event_name  = Column(String(256), nullable=False)
    impact      = Column(String(16), nullable=False)   # high | medium | low
    actual      = Column(String(64), nullable=True)
    forecast    = Column(String(64), nullable=True)
    previous    = Column(String(64), nullable=True)
    source      = Column(String(64), default="manual", nullable=False)

    __table_args__ = (
        UniqueConstraint("event_time", "currency", "event_name", name="uq_macro_event"),
    )


# ── candle_cache ──────────────────────────────────────────────────────────────

class CandleCacheRecord(Base):
    __tablename__ = "candle_cache"

    id          = Column(Integer, primary_key=True, index=True, autoincrement=True)
    pair        = Column(String(16),  default="XAU/USD", nullable=False)
    timeframe   = Column(String(8),   nullable=False)
    candle_time = Column(DateTime(timezone=True), nullable=False, index=True)
    open        = Column(Float, nullable=False)
    high        = Column(Float, nullable=False)
    low         = Column(Float, nullable=False)
    close       = Column(Float, nullable=False)
    volume      = Column(Integer, nullable=False)
    source      = Column(String(64), default="generated", nullable=False)

    __table_args__ = (
        UniqueConstraint("pair", "timeframe", "candle_time", name="uq_candle"),
    )


# ── mt5_trade_logs ────────────────────────────────────────────────────────────

class MT5TradeLog(Base):
    __tablename__ = "mt5_trade_logs"

    id               = Column(Integer,  primary_key=True, index=True, autoincrement=True)
    created_at       = Column(DateTime(timezone=True), default=_now, nullable=False)
    mode             = Column(String(8),   nullable=False, default="demo")   # demo | live
    pair             = Column(String(16),  nullable=False)
    broker_symbol    = Column(String(32),  nullable=True)
    signal           = Column(String(8),   nullable=False)   # BUY | SELL | WAIT
    order_type       = Column(String(16),  nullable=False, default="MARKET")
    volume           = Column(Float,       nullable=False, default=0.0)
    entry            = Column(Float,       nullable=True)
    stop_loss        = Column(Float,       nullable=True)
    take_profit      = Column(Float,       nullable=True)
    risk_percent     = Column(Float,       nullable=True)
    risk_amount      = Column(Float,       nullable=True)
    spread           = Column(Float,       nullable=True)
    ticket           = Column(Integer,     nullable=True)     # MT5 order ticket
    status           = Column(String(16),  nullable=False)    # accepted | rejected | failed
    rejection_reason = Column(Text,        nullable=True)
    reason           = Column(Text,        nullable=True)     # ICT setup description
    raw_response_json = Column(Text,       nullable=True)     # JSON string of MT5 response


# ── telegram_alert_logs ───────────────────────────────────────────────────────

class TelegramAlertLog(Base):
    """
    Audit log for every Telegram alert attempt — sent, duplicate_skipped, not_qualified, failed.
    Fields mirror the spec's telegram_alert_logs schema exactly.
    """
    __tablename__ = "telegram_alert_logs"

    id               = Column(Integer,  primary_key=True, index=True, autoincrement=True)
    created_at       = Column(DateTime(timezone=True), default=_now, nullable=False)
    pair             = Column(String(16),  nullable=False)
    signal           = Column(String(8),   nullable=False)   # BUY | SELL | WAIT
    quality_score    = Column(Integer,     nullable=True)
    entry            = Column(Float,       nullable=True)
    stop_loss        = Column(Float,       nullable=True)
    take_profit      = Column(Float,       nullable=True)
    rr               = Column(Float,       nullable=True)
    fingerprint      = Column(String(32),  nullable=False, default="", index=True)
    status           = Column(String(32),  nullable=False)   # sent|duplicate_skipped|not_qualified|failed
    error_message    = Column(Text,        nullable=True)
    raw_response_json = Column(Text,       nullable=True)


# ── institutional_scans ───────────────────────────────────────────────────────

class InstitutionalScan(Base):
    """
    Stores institutional scanner results.
    Written on every forced (manual) scan and on significant auto-scan changes.
    """
    __tablename__ = "institutional_scans"

    id                 = Column(Integer,  primary_key=True, index=True, autoincrement=True)
    created_at         = Column(DateTime(timezone=True), default=_now, nullable=False, index=True)
    instrument         = Column(String(16),  nullable=False, default="XAU/USD")
    scan_mode          = Column(String(8),   nullable=False, default="auto")   # auto | manual
    market_state       = Column(String(32),  nullable=False)
    signal             = Column(String(8),   nullable=False, default="WAIT")
    institutional_bias = Column(String(32),  nullable=True)
    confidence         = Column(Integer,     nullable=True)
    readiness          = Column(String(32),  nullable=True)
    summary            = Column(Text,        nullable=True)
    action             = Column(String(64),  nullable=True)

    # JSON-serialised sub-objects
    key_drivers_json   = Column(Text, nullable=True)
    blockers_json      = Column(Text, nullable=True)
    opportunity_json   = Column(Text, nullable=True)
    model_json         = Column(Text, nullable=True)
    liquidity_json     = Column(Text, nullable=True)
    fvg_json           = Column(Text, nullable=True)
    news_json          = Column(Text, nullable=True)
    risk_json          = Column(Text, nullable=True)
    raw_payload_json   = Column(Text, nullable=True)


# ── engine_weights ────────────────────────────────────────────────────────────

class EngineWeightsRecord(Base):
    """
    Stores adaptive scoring weights learned from historical outcomes.
    One row per pair (+ one 'all' aggregate row).
    """
    __tablename__ = "engine_weights"

    id               = Column(Integer,  primary_key=True, index=True, autoincrement=True)
    updated_at       = Column(DateTime(timezone=True), default=_now, onupdate=_now, nullable=False)
    pair             = Column(String(16),  nullable=False, unique=True, index=True)  # 'all' | 'eurusd' | 'xauusd'

    # Learned weights (integers, sum = 100)
    htf_weight       = Column(Integer, nullable=False, default=15)
    liq_weight       = Column(Integer, nullable=False, default=20)
    ms_weight        = Column(Integer, nullable=False, default=20)
    fvg_weight       = Column(Integer, nullable=False, default=20)
    news_weight      = Column(Integer, nullable=False, default=15)
    session_weight   = Column(Integer, nullable=False, default=10)

    # Metadata
    n_samples        = Column(Integer, nullable=False, default=0)    # completed outcomes analysed
    overall_win_rate = Column(Float,   nullable=False, default=0.0)
    maturity_score   = Column(Integer, nullable=False, default=0)    # 0–100
    calibrated       = Column(Boolean, nullable=False, default=False) # True if any weight adjusted
