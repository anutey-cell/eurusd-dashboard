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


# ── pending_executions (MT5 bridge queue) ─────────────────────────────────────

class PendingExecution(Base):
    """
    Queue of orders the VPS auto-executor has produced and that the Windows
    laptop bridge daemon is expected to pull, execute on MT5, and report back.

    Lifecycle:
      PENDING  -> laptop daemon claims it (sets claimed_at)
      EXECUTING -> daemon called mt5.order_send; awaiting response
      ACCEPTED -> order_send returned a ticket
      REJECTED -> daemon rejected (gate failure, spread, etc.)
      FAILED   -> order_send raised / TCP error
      EXPIRED  -> not consumed within 5 minutes; auto-cleaned by scheduler
    """
    __tablename__ = "pending_executions"

    id              = Column(Integer, primary_key=True, index=True, autoincrement=True)
    created_at      = Column(DateTime(timezone=True), default=_now, nullable=False, index=True)
    expires_at      = Column(DateTime(timezone=True), nullable=True, index=True)
    pair            = Column(String(16),  nullable=False, default="xauusd")
    signal          = Column(String(8),   nullable=False)            # BUY | SELL
    entry           = Column(Float, nullable=False)
    stop_loss       = Column(Float, nullable=False)
    take_profit     = Column(Float, nullable=False)                  # = TP1 (close target)
    take_profit_2   = Column(Float, nullable=True)                   # TP2 (stretch / BE trigger per mandate)
    risk_pips       = Column(Float, nullable=True)
    quality_score   = Column(Integer, nullable=True)
    rr              = Column(Float, nullable=True)
    max_lot         = Column(Float, nullable=False, default=0.05)
    reason          = Column(Text, nullable=True)                    # Setup description
    confirmations_json = Column(Text, nullable=True)                 # 3-layer confirmation snapshot

    # Workflow state
    status          = Column(String(16), nullable=False, default="PENDING", index=True)
    claimed_at      = Column(DateTime(timezone=True), nullable=True)
    claimed_by      = Column(String(64), nullable=True)              # Bridge daemon ID
    resolved_at     = Column(DateTime(timezone=True), nullable=True)
    ticket          = Column(Integer, nullable=True)                 # MT5 ticket on success
    lot_executed    = Column(Float, nullable=True)
    execution_error = Column(Text, nullable=True)


# ── strategist_verdicts (mandate signal log) ──────────────────────────────────

class StrategistVerdict(Base):
    """
    Append-only log of every institutional-strategist verdict produced.
    Required by the institutional demo-execution mandate ("for every generated
    signal, log: ..."). One row per fresh /strategist/decision compute
    (~once every 60s during the active session — small, easy to manage).

    Used downstream to build:
      • learning-curve dashboards (conditions × outcome)
      • execution_status distribution over time
      • improvement-note trend
      • MFE/MAE post-trade (when the bridge resolves the order back to us)
    """
    __tablename__ = "strategist_verdicts"

    id                       = Column(Integer, primary_key=True, index=True, autoincrement=True)
    created_at               = Column(DateTime(timezone=True), default=_now, nullable=False, index=True)

    # Mandate primary fields ─────────────────────────────────────────────
    symbol                   = Column(String(16), nullable=False, default="XAUUSD")
    decision                 = Column(String(16), nullable=False, index=True)  # BUY | SELL | STAND ASIDE
    conditions_passed        = Column(Integer, nullable=False, default=0)
    estimated_win_rate_range = Column(String(16), nullable=True)
    execution_status         = Column(String(32), nullable=False, index=True)
    execution_status_reason  = Column(Text,       nullable=True)

    # Setup classification (mandate enums) ───────────────────────────────
    setup_score              = Column(Integer, nullable=True)             # legacy 0-100
    quality_band             = Column(String(16), nullable=True)
    market_state             = Column(String(64), nullable=True)
    session_classification   = Column(String(64), nullable=True)
    tf_alignment_label       = Column(String(32), nullable=True)
    liquidity_behaviour      = Column(String(64), nullable=True)
    market_sentiment         = Column(String(16), nullable=True)

    # Trade plan ─────────────────────────────────────────────────────────
    entry                    = Column(Float,    nullable=True)
    entry_tolerance          = Column(Float,    nullable=True)
    stop_loss                = Column(Float,    nullable=True)
    tp1                      = Column(Float,    nullable=True)
    tp2                      = Column(Float,    nullable=True)
    tp3                      = Column(Float,    nullable=True)
    risk_reward              = Column(Float,    nullable=True)
    lot_size                 = Column(Float,    nullable=False, default=0.01)

    # Market-data snapshot ───────────────────────────────────────────────
    rsi_h1                   = Column(Float,    nullable=True)
    atr_h1                   = Column(Float,    nullable=True)
    spread_pts               = Column(Float,    nullable=True)
    long_pct                 = Column(Float,    nullable=True)  # MyFXBook sentiment
    short_pct                = Column(Float,    nullable=True)
    dxy_bias                 = Column(String(16), nullable=True)
    yields_bias              = Column(String(16), nullable=True)
    gold_macro_bias          = Column(String(32), nullable=True)
    news_risk                = Column(String(16), nullable=True)

    # Notes + traceability ───────────────────────────────────────────────
    improvement_note         = Column(Text, nullable=True)
    final_verdict_text       = Column(Text, nullable=True)
    full_verdict_json        = Column(Text, nullable=True)   # complete JSON snapshot

    # Linkage when a trade ends up firing ────────────────────────────────
    pending_execution_id     = Column(Integer, nullable=True, index=True)
    mt5_ticket               = Column(Integer, nullable=True, index=True)

    # Post-trade fields (filled by bridge result + monitor) ──────────────
    # These start NULL and are updated after the trade closes; that's why
    # this row stays in `strategist_verdicts` as the durable source of truth.
    result                   = Column(String(16), nullable=True)   # WIN | LOSS | BE | PENDING
    pips_outcome             = Column(Float,    nullable=True)
    mfe_pts                  = Column(Float,    nullable=True)     # Max Favorable Excursion
    mae_pts                  = Column(Float,    nullable=True)     # Max Adverse Excursion
    rules_followed           = Column(Integer, nullable=True)      # 1 / 0 (bool stored as int for sqlite + pg)
    post_trade_note          = Column(Text, nullable=True)


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


# ── historical_candles ────────────────────────────────────────────────────────

class HistoricalCandle(Base):
    """
    Imported historical XAU/USD candles for backtesting.
    Separate from candle_cache (which is for live-feed caching).
    """
    __tablename__ = "historical_candles"

    id           = Column(Integer, primary_key=True, index=True, autoincrement=True)
    instrument   = Column(String(16),  nullable=False, default="XAU/USD", index=True)
    timeframe    = Column(String(8),   nullable=False, index=True)
    candle_time  = Column(DateTime(timezone=True), nullable=False, index=True)
    open         = Column(Float, nullable=False)
    high         = Column(Float, nullable=False)
    low          = Column(Float, nullable=False)
    close        = Column(Float, nullable=False)
    volume       = Column(Integer, nullable=False, default=0)
    source       = Column(String(32), nullable=False, default="csv")   # csv | provider | sync
    created_at   = Column(DateTime(timezone=True), default=_now, nullable=False)

    __table_args__ = (
        UniqueConstraint("instrument", "timeframe", "candle_time",
                         name="uq_historical_candle"),
    )


# ── backtest_runs ─────────────────────────────────────────────────────────────

class BacktestRun(Base):
    """
    Persisted strict-backtest results for the XAU/USD signal engine.
    settings_json / summary_json / trades_json are JSON-serialised blobs.
    """
    __tablename__ = "backtest_runs"

    id                    = Column(Integer, primary_key=True, index=True, autoincrement=True)
    created_at            = Column(DateTime(timezone=True), default=_now, nullable=False, index=True)
    instrument            = Column(String(16), nullable=False, default="XAU/USD")
    timeframe             = Column(String(8),  nullable=False, default="M15")

    # Date window
    start_date            = Column(DateTime(timezone=True), nullable=True)
    end_date              = Column(DateTime(timezone=True), nullable=True)

    # Run settings (also mirrored in settings_json for full audit)
    initial_balance       = Column(Float, nullable=False, default=10000.0)
    risk_percent          = Column(Float, nullable=False, default=0.25)
    spread_points         = Column(Float, nullable=False, default=1.5)
    slippage_points       = Column(Float, nullable=False, default=0.5)
    min_score             = Column(Integer, nullable=False, default=80)
    min_rr                = Column(Float,   nullable=False, default=2.5)

    # Summary metrics
    total_signals_scanned = Column(Integer, nullable=False, default=0)
    valid_trades          = Column(Integer, nullable=False, default=0)
    win_rate              = Column(Float,   nullable=False, default=0.0)
    expectancy_points     = Column(Float,   nullable=False, default=0.0)
    expectancy_r          = Column(Float,   nullable=False, default=0.0)
    profit_factor         = Column(Float,   nullable=True)
    max_drawdown_percent  = Column(Float,   nullable=False, default=0.0)
    final_balance         = Column(Float,   nullable=False, default=0.0)
    net_return_percent    = Column(Float,   nullable=False, default=0.0)
    reliability_rating    = Column(Integer, nullable=False, default=0)
    reliability_band      = Column(String(32), nullable=True)

    # Full payloads
    settings_json         = Column(Text, nullable=True)
    summary_json          = Column(Text, nullable=True)
    trades_json           = Column(Text, nullable=True)
    skipped_json          = Column(Text, nullable=True)
    breakdowns_json       = Column(Text, nullable=True)
    equity_curve_json     = Column(Text, nullable=True)


# ── paper_observations ────────────────────────────────────────────────────────

class PaperObservation(Base):
    """
    Auto-logged record of every SIGNAL_READY scanner state.
    Used for PAPER_OBSERVATION_ONLY workflow — the dashboard logs each
    qualifying signal without manual confirmation, then resolves the
    outcome forward against future candles. Acts as the second validation
    dataset alongside the backtest.
    """
    __tablename__ = "paper_observations"

    id              = Column(Integer, primary_key=True, index=True, autoincrement=True)
    observed_at     = Column(DateTime(timezone=True), default=_now, nullable=False, index=True)
    instrument      = Column(String(16), nullable=False, default="XAU/USD")
    timeframe       = Column(String(8),  nullable=False, default="H4")
    engine_id       = Column(String(32), nullable=False, default="swing", index=True)   # swing | trend_pullback | ...

    # Trade plan at the moment of observation
    signal          = Column(String(8),  nullable=False)   # BUY | SELL
    entry           = Column(Float, nullable=False)
    stop_loss       = Column(Float, nullable=False)
    take_profit     = Column(Float, nullable=False)
    risk_points     = Column(Float, nullable=True)
    target_points   = Column(Float, nullable=True)
    rr              = Column(Float, nullable=True)
    score           = Column(Integer, nullable=True)
    session         = Column(String(32), nullable=True)
    setup_type      = Column(String(64), nullable=True)
    market_state    = Column(String(32), nullable=True)
    confidence      = Column(Integer, nullable=True)
    grade           = Column(String(4),  nullable=True)

    # Dedupe fingerprint — prevents duplicate logs for the same setup
    # within a cooldown window. sha256(signal+entry+sl+tp+score)[:16]
    fingerprint     = Column(String(32), nullable=False, default="", index=True)

    # Forward resolution (filled by tracker.resolve_pending())
    resolved_at     = Column(DateTime(timezone=True), nullable=True)
    result          = Column(String(16), nullable=True)    # WIN | LOSS | EXPIRED | PENDING
    exit_price      = Column(Float, nullable=True)
    points_captured = Column(Float, nullable=True)
    r_multiple      = Column(Float, nullable=True)
    bars_held       = Column(Integer, nullable=True)

    # Engine snapshot for audit/debugging
    engine_model_json = Column(Text, nullable=True)


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
