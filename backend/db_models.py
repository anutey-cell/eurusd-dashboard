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
    lot_size        = Column(Float, nullable=False, default=0.01)    # actual lot to trade (mandate/predator size)
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


# ── vp_trap_zones ─────────────────────────────────────────────────────────────

class VpTrapZone(Base):
    """
    A candidate "trapped trader" zone derived from the previous day's volume
    profile. One row per (date, level_type, level_side). The zone progresses
    through states as price interacts with the level. Persistent so it survives
    container restart.
    """
    __tablename__ = "vp_trap_zones"

    id                = Column(Integer, primary_key=True, index=True, autoincrement=True)
    created_at        = Column(DateTime(timezone=True), default=_now, nullable=False, index=True)
    updated_at        = Column(DateTime(timezone=True), default=_now, onupdate=_now, nullable=False)

    # Identity
    instrument        = Column(String(16), nullable=False, default="XAU/USD", index=True)
    zone_id           = Column(String(32), nullable=False, unique=True, index=True)   # SHA hash
    profile_date      = Column(String(10), nullable=False, index=True)                # YYYY-MM-DD (the prev day)
    level_type        = Column(String(16), nullable=False)   # PDH | PDL | VAH | VAL | POC
    level_side        = Column(String(4),  nullable=False)   # BUY (trapped-seller zone, below price)
                                                              # or SELL (trapped-buyer zone, above price)
    reference_price   = Column(Float,      nullable=False)   # the exact prev-day level

    # State machine
    state             = Column(String(24), nullable=False, default="LEVEL_DETECTED", index=True)
      # LEVEL_DETECTED | BREAKOUT_SEEN | TRAP_ARMED | WAITING_RETEST | RETEST_ACTIVE
      # | TRIGGERED | INVALIDATED | EXPIRED
    state_reason      = Column(String(255), nullable=True)   # human-readable why in current state

    # Trap evidence (populated as state advances)
    breakout_time     = Column(DateTime(timezone=True), nullable=True)
    breakout_extreme  = Column(Float, nullable=True)          # furthest point beyond level
    reclaim_time      = Column(DateTime(timezone=True), nullable=True)
    reclaim_price     = Column(Float, nullable=True)          # price when it closed back through
    displacement_pts  = Column(Float, nullable=True)          # magnitude of counter-move
    retest_count      = Column(Integer, nullable=False, default=0)
    last_touched_at   = Column(DateTime(timezone=True), nullable=True)

    # Volume data quality
    volume_source     = Column(String(16), nullable=False, default="tick_proxy")
      # comex_gc | broker_real | tick_proxy
    volume_at_level   = Column(Float, nullable=True)          # observed vol at the breakout wick

    # Expiry
    expires_at        = Column(DateTime(timezone=True), nullable=False, index=True)

    # Diagnostics
    profile_json      = Column(Text, nullable=True)          # snapshot of the day's full profile


# ── vp_trap_signals ───────────────────────────────────────────────────────────

class VpTrapSignal(Base):
    """
    A tradable BUY/SELL signal produced by the vp_trap strategy when a zone
    reaches TRIGGERED state and the composite score passes the live threshold.
    Linked back to its parent VpTrapZone for full audit trail.
    """
    __tablename__ = "vp_trap_signals"

    id                = Column(Integer, primary_key=True, index=True, autoincrement=True)
    created_at        = Column(DateTime(timezone=True), default=_now, nullable=False, index=True)
    instrument        = Column(String(16), nullable=False, default="XAU/USD", index=True)

    # Link to parent zone
    zone_id           = Column(String(32), nullable=False, index=True)          # matches VpTrapZone.zone_id
    zone_row_id       = Column(Integer, nullable=True)                          # FK-style (soft link)

    # Signal core
    signal            = Column(String(4), nullable=False)    # BUY | SELL
    entry             = Column(Float, nullable=False)
    stop_loss         = Column(Float, nullable=False)
    tp1               = Column(Float, nullable=True)
    tp2               = Column(Float, nullable=True)
    tp3               = Column(Float, nullable=True)
    rr                = Column(Float, nullable=True)
    risk_points       = Column(Float, nullable=True)

    # Score breakdown
    score_total       = Column(Integer, nullable=False)      # 0-100
    score_breakdown_json = Column(Text, nullable=True)       # per-factor points

    # Setup context
    trap_side         = Column(String(16), nullable=False)   # trapped_buyers | trapped_sellers
    setup_type        = Column(String(32), nullable=False)   # PDH_fail | PDL_fail | VAH_fail | VAL_fail
    session           = Column(String(32), nullable=True)
    market_regime     = Column(String(32), nullable=True)    # trending | balanced | expansion | transition
    htf_context       = Column(String(64), nullable=True)
    is_countertrend   = Column(Boolean, nullable=False, default=False)
    volume_source     = Column(String(16), nullable=False, default="tick_proxy")

    # Confluence with other engines
    mandate_agrees        = Column(Boolean, nullable=False, default=False)
    momentum_agrees       = Column(Boolean, nullable=False, default=False)
    liquidity_map_agrees  = Column(Boolean, nullable=False, default=False)

    # Dedupe / lifecycle
    fingerprint       = Column(String(32), nullable=False, unique=True, index=True)
    state             = Column(String(16), nullable=False, default="ALERTED", index=True)
      # ALERTED | ENQUEUED | ACCEPTED | TP1_HIT | TP2_HIT | STOPPED | EXPIRED | INVALIDATED
    expires_at        = Column(DateTime(timezone=True), nullable=True)

    # Outcome (forward-resolved)
    resolved_at       = Column(DateTime(timezone=True), nullable=True)
    outcome           = Column(String(16), nullable=True)    # WIN | LOSS | BE | TIMEOUT
    r_realized        = Column(Float, nullable=True)

    # Rationale (human-readable)
    reason_qualifies    = Column(Text, nullable=True)
    reason_invalidates  = Column(Text, nullable=True)
    conditions_met_json = Column(Text, nullable=True)
    conditions_missing_json = Column(Text, nullable=True)


# ── canonical signals (Telegram Notification P1) ─────────────────────────────

class Signal(Base):
    """
    Canonical persistent signal — single source of truth for the
    Telegram notification layer + future dashboard v2 consumers.

    One row per unique fingerprint. `state` mutates via the registry;
    every transition also writes a SignalStateTransition row for audit.
    """
    __tablename__ = "signals_canonical"

    id                = Column(Integer, primary_key=True, index=True, autoincrement=True)
    created_at        = Column(DateTime(timezone=True), default=_now, nullable=False, index=True)
    updated_at        = Column(DateTime(timezone=True), default=_now, onupdate=_now, nullable=False)

    # Identity
    signal_id         = Column(String(48), nullable=False, unique=True, index=True)
      # e.g. MDT-XAU-20260723-001
    fingerprint       = Column(String(32), nullable=False, unique=True, index=True)
    strategy_id       = Column(String(24), nullable=False, index=True)
      # mandate | vp_trap | momentum | kz_magnet | aggregated
    strategy_name     = Column(String(64), nullable=False)
    instrument        = Column(String(16), nullable=False, default="XAUUSD", index=True)

    # Direction + confidence
    direction         = Column(String(4),  nullable=False)    # BUY | SELL | NONE
    confidence        = Column(Integer,    nullable=False, default=0)

    # Entry / stop / invalidation
    entry_zone_low    = Column(Float, nullable=False)
    entry_zone_high   = Column(Float, nullable=False)
    stop_loss         = Column(Float, nullable=False)
    current_stop      = Column(Float, nullable=False)   # mutated by risk engine
    invalidation      = Column(String(255), nullable=False, default="")
    no_chase_price    = Column(Float, nullable=True)

    # Targets
    tp1               = Column(Float, nullable=True)
    tp2               = Column(Float, nullable=True)
    tp3               = Column(Float, nullable=True)
    tp1_label         = Column(String(64), nullable=True)
    tp2_label         = Column(String(64), nullable=True)
    tp3_label         = Column(String(64), nullable=True)
    rr_tp1            = Column(Float, nullable=True)
    rr_tp2            = Column(Float, nullable=True)
    rr_tp3            = Column(Float, nullable=True)

    # Setup context
    session           = Column(String(64), nullable=False, default="")
    market_regime     = Column(String(64), nullable=True)
    htf_bias          = Column(String(64), nullable=True)
    trap_side         = Column(String(32), nullable=True)
    reference_zone_low  = Column(Float, nullable=True)
    reference_zone_high = Column(Float, nullable=True)

    # Rationale (JSON-encoded lists)
    conditions_met_json     = Column(Text, nullable=True)
    conditions_missing_json = Column(Text, nullable=True)
    rationale               = Column(Text, nullable=True)
    data_source             = Column(String(32), nullable=False, default="tick_proxy")
    confluence_json         = Column(Text, nullable=True)
      # JSON array of {"strategy_name":..., "confidence":...}

    # State machine
    state             = Column(String(24), nullable=False, default="DETECTED", index=True)
    previous_state    = Column(String(24), nullable=True)

    # Timestamps
    valid_until       = Column(DateTime(timezone=True), nullable=True, index=True)
    triggered_at      = Column(DateTime(timezone=True), nullable=True)
    closed_at         = Column(DateTime(timezone=True), nullable=True)

    # Execution
    is_broker_confirmed = Column(Boolean, nullable=False, default=False)
    r_realized        = Column(Float, nullable=True)
    partial_taken     = Column(Boolean, nullable=False, default=False)

    # Aggregation — when this signal was absorbed into a high-confluence agg
    absorbed_into     = Column(String(48), nullable=True, index=True)


class SignalStateTransition(Base):
    """Append-only audit log — every state change of every signal."""
    __tablename__ = "signal_state_transitions"

    id                = Column(Integer, primary_key=True, index=True, autoincrement=True)
    at                = Column(DateTime(timezone=True), default=_now, nullable=False, index=True)
    signal_id         = Column(String(48), nullable=False, index=True)   # references Signal.signal_id
    from_state        = Column(String(24), nullable=False)
    to_state          = Column(String(24), nullable=False)
    reason            = Column(String(255), nullable=True)   # human-readable trigger
    price_at_transition = Column(Float, nullable=True)
    payload_json      = Column(Text, nullable=True)          # optional structured extras


class TelegramNotification(Base):
    """
    Audit trail for every Telegram message the notification layer emits
    (or SUPPRESSES). Idempotency: (signal_id, message_fingerprint) is unique
    per attempt — repeat sends detect and no-op.
    """
    __tablename__ = "telegram_notifications"

    id                = Column(Integer, primary_key=True, index=True, autoincrement=True)
    created_at        = Column(DateTime(timezone=True), default=_now, nullable=False, index=True)

    # Signal + transition context
    signal_id         = Column(String(48), nullable=False, index=True)
    strategy_id       = Column(String(24), nullable=False, index=True)
    message_type      = Column(String(32), nullable=False, index=True)
      # monitoring | actionable | entry_triggered | tp1_hit | ... | high_confluence
    from_state        = Column(String(24), nullable=True)
    to_state          = Column(String(24), nullable=False)

    # Idempotency
    message_fingerprint = Column(String(32), nullable=False, unique=True, index=True)

    # Delivery
    delivered         = Column(Boolean, nullable=False, default=False)
    delivery_result   = Column(String(24), nullable=False, default="pending")
      # pending | delivered | failed | suppressed | dry_run
    retry_count       = Column(Integer, nullable=False, default=0)
    error_message     = Column(String(255), nullable=True)
    delivered_at      = Column(DateTime(timezone=True), nullable=True)

    # Recipient (masked in logs; full stored here for audit)
    chat_id_hash      = Column(String(32), nullable=False, default="")
      # SHA(chat_id)[:16] — never store raw chat ID in logs

    # Rendered content (for post-hoc inspection / regression testing)
    message_text      = Column(Text, nullable=True)
    message_bytes     = Column(Integer, nullable=True)

    # Suppression rationale — populated when delivery_result == "suppressed"
    suppression_reason = Column(String(255), nullable=True)


# ── Telegram bot state (P9) ────────────────────────────────────────────────

class TelegramBotState(Base):
    """
    Single-row cache of the getUpdates offset so the poller doesn't
    re-process the same command on restart.
    """
    __tablename__ = "telegram_bot_state"

    id             = Column(Integer, primary_key=True, autoincrement=True)
    updated_at     = Column(DateTime(timezone=True), default=_now, onupdate=_now, nullable=False)
    last_update_id = Column(Integer, nullable=False, default=0)


class TelegramChatPreference(Base):
    """
    Per-chat preferences persisted across restarts. `chat_id` is the
    Telegram chat_id (stored as string). Only chats registered here
    receive notifications from the router (once P5 cutover flips
    shadow_mode off).

    Admin chats can run privileged commands (/mute, /unmute, /mode).
    """
    __tablename__ = "telegram_chat_preferences"

    id              = Column(Integer, primary_key=True, autoincrement=True)
    created_at      = Column(DateTime(timezone=True), default=_now, nullable=False)
    updated_at      = Column(DateTime(timezone=True), default=_now, onupdate=_now, nullable=False)

    chat_id         = Column(String(32), nullable=False, unique=True, index=True)
    chat_type       = Column(String(16), nullable=False, default="private")  # private|group|channel
    is_admin        = Column(Boolean, nullable=False, default=False)
    is_muted        = Column(Boolean, nullable=False, default=False)
    verbosity_mode  = Column(String(16), nullable=False, default="standard")  # minimal|standard|detailed

    # JSON list of strategy_ids muted just for this chat
    strategy_mutes_json = Column(Text, nullable=True)

    # Display-only
    label           = Column(String(64), nullable=True)   # e.g. "Owner", "Ops group"


class VpTrapMeasurementEvent(Base):
    """
    P135 — one row per VP Trap signal fired during the 30-day measurement
    protocol. This is the append-only ground truth for computing the four
    protocol metrics: setups/day, win rate, avg R, drawdown.

    Lifecycle: PENDING → TRIGGERED → (TP1_HIT | TP2_HIT | STOPPED)
                         └→ INVALIDATED (if opposing move before trigger)
                         └→ EXPIRED (if valid_until passes without trigger)
    """
    __tablename__ = "vp_trap_measurement_events"

    id             = Column(Integer, primary_key=True, autoincrement=True)
    created_at     = Column(DateTime(timezone=True), default=_now, nullable=False, index=True)
    updated_at     = Column(DateTime(timezone=True), default=_now, onupdate=_now, nullable=False)

    # Identity
    zone_id        = Column(String(48), nullable=False, index=True)
    signal_id      = Column(String(64), nullable=True)  # optional cross-ref to canonical/legacy

    # Setup context at fire time
    direction      = Column(String(4),  nullable=False)  # BUY | SELL
    score          = Column(Integer,    nullable=False)
    session        = Column(String(32), nullable=False, default="unknown")
    trap_side      = Column(String(16), nullable=True)   # bull_trap | bear_trap

    # Trade plan
    fired_at       = Column(DateTime(timezone=True), default=_now, nullable=False, index=True)
    entry_price    = Column(Float, nullable=False)
    stop_loss      = Column(Float, nullable=False)
    tp1_price      = Column(Float, nullable=True)
    tp2_price      = Column(Float, nullable=True)
    tp1_rr         = Column(Float, nullable=True)
    tp2_rr         = Column(Float, nullable=True)
    invalidation_price = Column(Float, nullable=True)
    valid_until    = Column(DateTime(timezone=True), nullable=True)

    # Live outcome
    status         = Column(String(24), nullable=False, default="PENDING", index=True)
      # PENDING | TRIGGERED | TP1_HIT | TP2_HIT | STOPPED | INVALIDATED | EXPIRED
    triggered_at   = Column(DateTime(timezone=True), nullable=True)
    triggered_price = Column(Float, nullable=True)
    closed_at      = Column(DateTime(timezone=True), nullable=True)
    closed_price   = Column(Float, nullable=True)

    # Outcome metrics (populated once closed)
    r_realized     = Column(Float, nullable=True)   # 0.5 half-taken at TP1 + 2R runner at TP2 = 2.25R blended
    mfe_pts        = Column(Float, nullable=True)   # max favorable excursion (points from entry)
    mae_pts        = Column(Float, nullable=True)   # max adverse excursion
    duration_min   = Column(Float, nullable=True)   # entry → close, minutes

    notes_json     = Column(Text, nullable=True)


class ShadowTrade(Base):
    """
    Shadow trade simulator ledger — one row per BUY/SELL verdict with a
    complete trade plan (entry + SL + TP1 + TP2). Records ALL grades
    (A+/A/B/C) so we can validate whether suppressed grades would have
    underperformed.

    Lifecycle:
        PENDING → TRIGGERED → (TP1_HIT | TP2_HIT | STOPPED | EXPIRED)
                              └→ INVALIDATED (if opposing move before trigger)

    Never affects any live path. Never sends alerts. Pure data capture
    for grade-rubric calibration and shadow-mode validation (Section 7 of
    the strategy review).
    """
    __tablename__ = "shadow_trades"

    id                    = Column(Integer, primary_key=True, autoincrement=True)
    created_at            = Column(DateTime(timezone=True), default=_now, nullable=False, index=True)
    updated_at            = Column(DateTime(timezone=True), default=_now, onupdate=_now, nullable=False)

    # Idempotency + attribution
    fingerprint           = Column(String(48), nullable=False, index=True, unique=True)
    verdict_id            = Column(Integer, nullable=True)          # optional FK to strategist_verdicts
    instrument            = Column(String(16), default="XAU/USD", nullable=False, index=True)
    # Model version — populated for predator-sourced rows; NULL for mandate rows
    predator_version      = Column(String(32), nullable=True)

    # Grading context
    grade                 = Column(String(16), nullable=False, index=True)   # A+ / A / B / C / STAND_ASIDE / INSUFFICIENT_DATA
    grade_reason          = Column(String(255), nullable=True)
    composite_score       = Column(Float, nullable=True)             # signal grading composite score if computed
    archetype             = Column(String(48), nullable=True, index=True)   # e.g. "liquidity_sweep_reversal"
    regime_at_entry       = Column(String(48), nullable=True, index=True)
    session_at_entry      = Column(String(24), nullable=True, index=True)

    # Setup context
    direction             = Column(String(4),  nullable=False)       # BUY | SELL
    setup_score           = Column(Integer,    nullable=True)
    conditions_passed     = Column(Integer,    nullable=True)

    # Trade plan
    fired_at              = Column(DateTime(timezone=True), default=_now, nullable=False, index=True)
    entry_price           = Column(Float, nullable=False)
    stop_loss             = Column(Float, nullable=False)
    tp1_price             = Column(Float, nullable=False)
    tp2_price             = Column(Float, nullable=True)
    tp3_price             = Column(Float, nullable=True)
    invalidation_price    = Column(Float, nullable=True)
    tp1_rr                = Column(Float, nullable=True)
    tp2_rr                = Column(Float, nullable=True)

    # Session-conditional slippage assumptions (applied to expectancy)
    est_spread_pts        = Column(Float, nullable=True)
    est_slippage_pts      = Column(Float, nullable=True)

    # Outcome tracking
    status                = Column(String(24), nullable=False, default="PENDING", index=True)
    triggered_at          = Column(DateTime(timezone=True), nullable=True)
    triggered_price       = Column(Float, nullable=True)
    closed_at             = Column(DateTime(timezone=True), nullable=True)
    closed_price          = Column(Float, nullable=True)

    # Outcome metrics (populated once closed)
    r_realized            = Column(Float, nullable=True)     # nominal R
    r_spread_adjusted     = Column(Float, nullable=True)     # spread-adjusted R (what live P&L would show)
    mfe_pts               = Column(Float, nullable=True)     # max favorable excursion
    mae_pts               = Column(Float, nullable=True)     # max adverse excursion
    duration_min          = Column(Float, nullable=True)

    # Free-form metadata
    notes_json            = Column(Text, nullable=True)


class QualifyingExpansion(Base):
    """
    Phase 13 — one row per detected significant directional move. The
    "ground truth" against which alerts are scored for coverage.
    """
    __tablename__ = "qualifying_expansions"

    id                      = Column(Integer, primary_key=True, autoincrement=True)
    instrument              = Column(String(16), default="XAU/USD", nullable=False, index=True)
    direction               = Column(String(4),  nullable=False)   # BULL | BEAR
    started_at              = Column(DateTime(timezone=True), nullable=False, index=True)
    ended_at                = Column(DateTime(timezone=True), nullable=False)
    trigger_level           = Column(Float, nullable=True)
    confirmation_at         = Column(DateTime(timezone=True), nullable=True)
    total_distance          = Column(Float, nullable=False)
    atr_multiple            = Column(Float, nullable=False)
    max_retracement         = Column(Float, nullable=True)
    detected                = Column(Boolean, nullable=False, default=False, index=True)
    first_intel_alert_at    = Column(DateTime(timezone=True), nullable=True)
    first_entry_alert_at    = Column(DateTime(timezone=True), nullable=True)
    detection_delay_min     = Column(Float, nullable=True)
    pct_of_move_captured    = Column(Float, nullable=True)
    blocking_filter         = Column(String(64), nullable=True)
    was_extended_at_detect  = Column(Boolean, nullable=False, default=False)
    fingerprint             = Column(String(48), nullable=False, index=True, unique=True)
    created_at              = Column(DateTime(timezone=True), default=_now, nullable=False)


class ExpansionAlertMatch(Base):
    """Links a QualifyingExpansion to a specific alert row for audit."""
    __tablename__ = "expansion_alert_matches"

    id             = Column(Integer, primary_key=True, autoincrement=True)
    expansion_id   = Column(Integer, nullable=False, index=True)
    alert_source   = Column(String(32), nullable=False)     # intel | strategist
    alert_type     = Column(String(64), nullable=True)
    alert_sent_at  = Column(DateTime(timezone=True), nullable=False)
    delay_seconds  = Column(Integer, nullable=False)
    created_at     = Column(DateTime(timezone=True), default=_now, nullable=False)


class MarketIntelligenceAlert(Base):
    """
    Phase 11 — audit trail for every market-intelligence alert the engine
    considered firing. `delivery_result` distinguishes sent / shadow /
    suppressed / failed so we can reconcile behavior in shadow mode.
    """
    __tablename__ = "market_intelligence_alerts"

    id                      = Column(Integer, primary_key=True, autoincrement=True)
    ts                      = Column(DateTime(timezone=True), default=_now, nullable=False, index=True)
    instrument              = Column(String(16), default="XAU/USD", nullable=False, index=True)
    alert_type              = Column(String(64), nullable=False, index=True)
    trigger_price           = Column(Float, nullable=True)
    directional_assessment  = Column(String(32), nullable=True)
    opportunity_status      = Column(String(48), nullable=True)
    entry_status            = Column(String(32), nullable=True)
    directional_confidence  = Column(Float, nullable=True)
    entry_confidence        = Column(Float, nullable=True)
    body_text               = Column(Text, nullable=True)
    delivery_result         = Column(String(16), nullable=False, default="pending", index=True)
    delivery_reason         = Column(String(255), nullable=True)
    fingerprint             = Column(String(48), nullable=False, index=True)


class OpportunityStateTransition(Base):
    """
    Phase 7 — persisted history of the opportunity state machine.

    Every state change appends one row. Restart-safe: on process boot the
    engine reads the most recent row to hydrate `current_state`. Append-only —
    never mutated. Query by `ts` for a full timeline.
    """
    __tablename__ = "opportunity_state_transitions"

    id                  = Column(Integer, primary_key=True, autoincrement=True)
    ts                  = Column(DateTime(timezone=True), default=_now, nullable=False, index=True)
    instrument          = Column(String(16), default="XAU/USD", nullable=False, index=True)

    prev_state          = Column(String(32), nullable=True)
    new_state           = Column(String(32), nullable=False, index=True)
    price               = Column(Float,      nullable=True)

    # Full context captured at the moment of transition
    evidence_json       = Column(Text, nullable=True)
    contradictions_json = Column(Text, nullable=True)
    key_levels_json     = Column(Text, nullable=True)
    invalidation_price  = Column(Float, nullable=True)
    confidence          = Column(Float, nullable=True)   # 0-100
    trigger_condition   = Column(String(96), nullable=True)

    created_at          = Column(DateTime(timezone=True), default=_now, nullable=False)


class TelegramCommandLog(Base):
    """Append-only log of received bot commands — for audit + debug."""
    __tablename__ = "telegram_command_log"

    id           = Column(Integer, primary_key=True, autoincrement=True)
    at           = Column(DateTime(timezone=True), default=_now, nullable=False, index=True)
    update_id    = Column(Integer, nullable=False, index=True)
    chat_id_hash = Column(String(32), nullable=False, default="")
    command      = Column(String(64), nullable=False)
    args         = Column(String(255), nullable=True)
    accepted     = Column(Boolean, nullable=False, default=True)
    reject_reason = Column(String(255), nullable=True)
    response_bytes = Column(Integer, nullable=True)


# ── Predator DEMO sizing (P182-P185) ─────────────────────────────────────────

class PredatorSignalBatch(Base):
    """
    One row per Predator FIRE event. Captures the planning-time context needed
    to compare STANDARD vs EXPANSION performance later.

    Individual tickets live in PredatorPosition (FK by batch_id).
    """
    __tablename__ = "predator_signal_batches"

    id                = Column(Integer, primary_key=True, autoincrement=True)
    created_at        = Column(DateTime(timezone=True), default=_now, nullable=False, index=True)

    # Opportunity lifecycle (P26 — canonical opportunity accounting)
    # A single "opportunity" represents ONE structural market thesis;
    # multiple FIREs on the same key level within the same session belong
    # to the same opportunity. Populated at FIRE time by
    # predator_execution_manager.create_batch() using the canonical algorithm
    # (session-reset ON, structural-reset on price drift >75pt after resolution).
    opportunity_id            = Column(String(96), nullable=True, index=True)
    opportunity_state         = Column(String(16), nullable=True)   # CREATED|ARMED|FIRE|ACTIVE|RESOLVED|INVALIDATED|RESET
    is_primary_fire           = Column(Boolean, nullable=True)
    fire_sequence_within_opportunity = Column(Integer, nullable=True)
    opportunity_created_at    = Column(DateTime(timezone=True), nullable=True)
    opportunity_resolved_at   = Column(DateTime(timezone=True), nullable=True)
    opportunity_reset_reason  = Column(String(64), nullable=True)

    # Decision journal — frozen at signal time (never reconstructed)
    trend_context        = Column(String(32), nullable=True)  # WITH_TREND|HTF_DISAGREEMENT|TRANSITION
    htf_disagreement     = Column(Boolean, nullable=True)
    transition_state     = Column(String(32), nullable=True)  # STABLE|EARLY|MATURE
    velocity_state       = Column(String(24), nullable=True)  # SLOW|NORMAL|ACCELERATING|EXHAUSTED
    compression_state    = Column(String(24), nullable=True)
    time_at_level_min    = Column(Float, nullable=True)
    gc_context           = Column(String(24), nullable=True)  # ALIGNED|OPPOSED|NEUTRAL|NO_DATA
    spread_at_fire       = Column(Float, nullable=True)

    # Signal identity
    signal_id         = Column(String(64), nullable=False, unique=True, index=True)
    archetype         = Column(String(32), nullable=False)   # ASIAN_BREAKDOWN | PDL_BREAK | VOL_CONTINUATION
    direction         = Column(String(8),  nullable=False)   # SELL (predator is SELL-only)
    # Baseline model identifier — every batch must be traceable to the exact
    # engine version that produced it, so future decision-layer variants can
    # be A/B compared without contaminating the baseline sample.
    predator_version  = Column(String(32), nullable=False, default="PREDATOR_v1.0_M5")

    # Trade plan (validated archetype targets)
    entry_price       = Column(Float, nullable=False)
    stop_loss         = Column(Float, nullable=False)
    tp1               = Column(Float, nullable=False)
    tp2               = Column(Float, nullable=False)
    key_level         = Column(Float, nullable=True)         # asian_low | pdl

    # Sizing decision
    exposure_mode     = Column(String(16), nullable=False, default="STANDARD")  # STANDARD | EXPANSION
    planned_positions = Column(Integer, nullable=False)      # 5 or 10
    lot_per_position  = Column(Float, nullable=False, default=0.03)
    max_exposure_lots = Column(Float, nullable=False)        # 0.15 or 0.30

    # Volume expansion evidence at fire bar
    vol_pct_at_fire   = Column(Float, nullable=True)         # 0-100 (rolling 100-M5)
    atr20_at_fire     = Column(Float, nullable=True)
    disp_atr_ratio    = Column(Float, nullable=True)
    expansion_reason  = Column(String(255), nullable=True)   # "vol_pct=93 >= 90 AND disp/atr=1.4 >= 1.0"

    # Regime snapshot (for post-hoc bucketing)
    regime_direction  = Column(String(32), nullable=True)
    regime_volatility = Column(String(16), nullable=True)

    # Execution state
    execution_status  = Column(String(32), nullable=False, default="PLANNED", index=True)
    # PLANNED | ENQUEUING | ENQUEUED | PARTIAL | COMPLETE | ABORTED
    positions_opened  = Column(Integer, nullable=False, default=0)
    total_exposure    = Column(Float, nullable=False, default=0.0)
    abort_reason      = Column(String(255), nullable=True)


class PredatorPosition(Base):
    """
    One row per individual 0.03-lot ticket within a Predator batch.
    Maps to PendingExecution.id after enqueue succeeds; ticket populated
    once the bridge daemon reports back from MT5.
    """
    __tablename__ = "predator_positions"

    id                    = Column(Integer, primary_key=True, autoincrement=True)
    batch_id              = Column(Integer, nullable=False, index=True)   # → predator_signal_batches.id
    seq_no                = Column(Integer, nullable=False)               # 1..N within the batch
    created_at            = Column(DateTime(timezone=True), default=_now, nullable=False)
    predator_version      = Column(String(32), nullable=False, default="PREDATOR_v1.0_M5")

    # Ticket plan
    lot_size              = Column(Float, nullable=False, default=0.03)
    tp_target             = Column(String(16), nullable=False)            # TP1 | TP2 | RUNNER
    entry_price_planned   = Column(Float, nullable=False)
    stop_loss             = Column(Float, nullable=False)
    take_profit_planned   = Column(Float, nullable=False)

    # Live conditions AT time this ticket was enqueued
    price_at_enqueue      = Column(Float, nullable=True)
    spread_at_enqueue     = Column(Float, nullable=True)
    pct_consumed_at_enqueue = Column(Float, nullable=True)                # extension check

    # Wiring
    pending_execution_id  = Column(Integer, nullable=True, index=True)    # → pending_executions.id
    mt5_ticket            = Column(Integer, nullable=True, index=True)    # populated by bridge

    # Outcome (populated by bridge post-close reporter)
    opened_at             = Column(DateTime(timezone=True), nullable=True)
    closed_at             = Column(DateTime(timezone=True), nullable=True)
    exit_price            = Column(Float, nullable=True)
    outcome               = Column(String(16), nullable=True)             # TP1 | TP2 | SL | MANUAL | TIMEOUT
    realized_pts          = Column(Float, nullable=True)
    mfe_pts               = Column(Float, nullable=True)
    mae_pts               = Column(Float, nullable=True)

    # Status (of THIS ticket, distinct from batch-level status)
    status                = Column(String(16), nullable=False, default="PLANNED", index=True)
    # PLANNED | ENQUEUED | REJECTED | OPEN | CLOSED
    reject_reason         = Column(String(255), nullable=True)


# ── GC futures bars — independent from XAU/USD (P189) ────────────────────────
# Yahoo GC=F used to be retagged as XAU/USD candles for freshness fallback.
# That mixed spot and futures into the same series, contaminating any
# future GC-vs-XAU basis analysis. This table keeps GC bars distinct.
# Deprecates the Yahoo-as-XAUUSD-fallback path.

class GcFuturesBar(Base):
    __tablename__ = "gc_futures_bars"
    __table_args__ = (
        UniqueConstraint("contract", "timeframe", "candle_time",
                          name="ux_gc_contract_tf_time"),
    )

    id          = Column(Integer, primary_key=True, autoincrement=True)
    created_at  = Column(DateTime(timezone=True), default=_now, nullable=False, index=True)
    contract    = Column(String(16), nullable=False, index=True, default="GC=F")
    # "GC=F" = Yahoo's continuous front-month; when a real futures feed is added
    # this will become the specific listed contract (e.g. "GCZ26").
    timeframe   = Column(String(8), nullable=False, index=True)   # M5 | M15 | H1 | H4 | D1
    candle_time = Column(DateTime(timezone=True), nullable=False, index=True)
    open        = Column(Float, nullable=False)
    high        = Column(Float, nullable=False)
    low         = Column(Float, nullable=False)
    close       = Column(Float, nullable=False)
    volume      = Column(Float, nullable=True)
    source      = Column(String(16), nullable=False, default="yahoo")


# ── Predator rejections (observation-only) ────────────────────────────────
# Every candidate that Predator considered but rejected is frozen here at
# decision time with the hypothetical trade plan + full context. Later a
# reconciler walks the M5 outcome path to determine what the trade WOULD
# have paid. Enables economic evaluation of every gate/filter.

class PredatorRejection(Base):
    __tablename__ = "predator_rejections"

    id                    = Column(Integer, primary_key=True, autoincrement=True)
    created_at            = Column(DateTime(timezone=True), default=_now, nullable=False, index=True)
    opportunity_id        = Column(String(96), nullable=True, index=True)
    predator_version      = Column(String(32), nullable=False, default="PREDATOR_v1.0_M5")

    # Signal identity
    archetype             = Column(String(32), nullable=False)
    direction             = Column(String(8),  nullable=False)
    rejection_reason      = Column(String(64), nullable=False, index=True)
    rejection_detail      = Column(String(255), nullable=True)

    # Hypothetical trade plan (what would have been enqueued)
    hypothetical_entry    = Column(Float, nullable=True)
    hypothetical_sl       = Column(Float, nullable=True)
    hypothetical_tp1      = Column(Float, nullable=True)
    hypothetical_tp2      = Column(Float, nullable=True)
    key_level             = Column(Float, nullable=True)

    # Frozen context (never reconstructed)
    session               = Column(String(16), nullable=True)
    regime_direction      = Column(String(32), nullable=True)
    regime_volatility     = Column(String(16), nullable=True)
    extension_pct         = Column(Float, nullable=True)
    velocity_state        = Column(String(24), nullable=True)
    trend_context         = Column(String(32), nullable=True)
    transition_state      = Column(String(32), nullable=True)
    compression_state     = Column(String(24), nullable=True)
    gc_context            = Column(String(24), nullable=True)
    spread_at_decision    = Column(Float, nullable=True)

    # Outcome walk (populated by a reconciler AFTER the fact — not at decision time)
    outcome               = Column(String(16), nullable=True)  # TP1|TP2|SL|TIMEOUT
    outcome_pnl_pts       = Column(Float, nullable=True)
    outcome_walked_at     = Column(DateTime(timezone=True), nullable=True)


# ═════════════════════════════════════════════════════════════════════════════
# Forward convergence infrastructure (2026-08-19 launch)
# One row per unique canonical forward opportunity from launch onward.
# Distinguishes STRATEGY REJECTION (Champion says no trade) vs PORTFOLIO
# REJECTION (Champion says trade but exposure/duplicate blocks it).
# ═════════════════════════════════════════════════════════════════════════════

class PredatorForwardOpportunity(Base):
    __tablename__ = "predator_forward_opportunities"

    id                          = Column(Integer, primary_key=True, autoincrement=True)
    created_at                  = Column(DateTime(timezone=True), default=_now, nullable=False, index=True)
    resolved_at                 = Column(DateTime(timezone=True), nullable=True)
    opportunity_id              = Column(String(96), nullable=False, index=True)
    signal_id                   = Column(String(64), nullable=True, index=True)
    model_version               = Column(String(32), nullable=False, default="PREDATOR_v1.0_M5")
    archetype                   = Column(String(32), nullable=False)
    direction                   = Column(String(8),  nullable=False)

    # STRATEGY level: what did Champion decide?
    model_decision              = Column(String(16), nullable=False)   # FIRE | ARMED | REJECT
    strategy_rejection_reason   = Column(String(64), nullable=True)

    # PORTFOLIO level: could it actually execute?
    portfolio_decision          = Column(String(24), nullable=True)     # EXECUTED | SKIPPED_EXPOSURE | DUPLICATE | ERROR | PENDING
    portfolio_skip_reason       = Column(String(64), nullable=True)

    # Frozen expected trade
    expected_entry              = Column(Float, nullable=True)
    sl                          = Column(Float, nullable=True)
    tp1                         = Column(Float, nullable=True)
    tp2                         = Column(Float, nullable=True)
    expected_tickets            = Column(Integer, nullable=True)
    expected_lots               = Column(Float, nullable=True)

    # Portfolio state at decision time
    actual_available_capacity   = Column(Float, nullable=True)
    actual_open_exposure        = Column(Float, nullable=True)

    # Resolution
    resolution_status           = Column(String(16), nullable=True)     # PENDING | RESOLVED | ORPHAN


class PredatorDemoConvergence(Base):
    """One row per closed executed batch, pairing FORWARD_MODEL_RESULT with
    ACTUAL_DEMO_RESULT. Populated by a resolver after batch closure."""
    __tablename__ = "predator_demo_convergence"

    id                          = Column(Integer, primary_key=True, autoincrement=True)
    created_at                  = Column(DateTime(timezone=True), default=_now, nullable=False, index=True)
    opportunity_id              = Column(String(96), nullable=False, index=True)
    batch_id                    = Column(Integer, nullable=True, index=True)

    # Model expectation
    model_entry                 = Column(Float, nullable=True)
    model_exit                  = Column(Float, nullable=True)
    model_lots                  = Column(Float, nullable=True)
    model_pnl_usd               = Column(Float, nullable=True)
    model_pnl_pts               = Column(Float, nullable=True)
    model_r                     = Column(Float, nullable=True)
    model_holding_min           = Column(Float, nullable=True)

    # Actual demo
    actual_entry                = Column(Float, nullable=True)
    actual_exit                 = Column(Float, nullable=True)
    actual_lots                 = Column(Float, nullable=True)
    actual_pnl_usd              = Column(Float, nullable=True)
    actual_pnl_pts              = Column(Float, nullable=True)
    actual_r                    = Column(Float, nullable=True)
    actual_holding_min          = Column(Float, nullable=True)

    # Deltas
    entry_slippage_pts          = Column(Float, nullable=True)
    exit_diff_pts               = Column(Float, nullable=True)
    cost_diff_usd               = Column(Float, nullable=True)
    ticket_count_diff           = Column(Integer, nullable=True)
    execution_latency_ms        = Column(Integer, nullable=True)

    # Classification: MATCH | ECONOMIC_DRIFT | OUTCOME_DIVERGENCE | EXECUTION_MISS | ORPHAN_EXECUTION
    convergence_class           = Column(String(24), nullable=True, index=True)


# ═════════════════════════════════════════════════════════════════════════════
# Strategist ledgers (2026-08-21) — BUY outcomes + SELL shadow
# BUY: actual demo outcomes from strategist-originated MT5 tickets
# SELL: hypothetical outcomes for verdicts blocked by shadow-only mandate
# ═════════════════════════════════════════════════════════════════════════════

class StrategistBuyOutcome(Base):
    __tablename__ = "strategist_buy_outcomes"

    id                        = Column(Integer, primary_key=True, autoincrement=True)
    created_at                = Column(DateTime(timezone=True), default=_now, nullable=False, index=True)
    resolved_at               = Column(DateTime(timezone=True), nullable=True)
    opportunity_id            = Column(String(96), nullable=False, unique=True, index=True)
    verdict_id                = Column(Integer, nullable=True)
    pending_execution_id      = Column(Integer, nullable=True, index=True)
    follow_on_execution_ids   = Column(String(255), nullable=True)  # comma-sep additional pending_execution_ids
    reservation_id            = Column(String(32), nullable=True)
    mt5_ticket                = Column(Integer, nullable=True, index=True)

    # Frozen decision context
    conditions_passed         = Column(Integer, nullable=True)
    setup_score               = Column(Integer, nullable=True)
    quality_band              = Column(String(24), nullable=True)
    market_state              = Column(String(64), nullable=True)

    # Plan
    entry                     = Column(Float, nullable=True)
    sl                        = Column(Float, nullable=True)
    tp1                       = Column(Float, nullable=True)
    tp2                       = Column(Float, nullable=True)
    rr                        = Column(Float, nullable=True)
    lot_size                  = Column(Float, nullable=True)

    # Actual demo outcome
    actual_entry              = Column(Float, nullable=True)
    actual_exit               = Column(Float, nullable=True)
    actual_pnl_usd            = Column(Float, nullable=True)
    actual_pnl_pts            = Column(Float, nullable=True)
    actual_r                  = Column(Float, nullable=True)
    mfe_pts                   = Column(Float, nullable=True)
    mae_pts                   = Column(Float, nullable=True)
    holding_min               = Column(Float, nullable=True)
    slippage_pts              = Column(Float, nullable=True)
    commission_usd            = Column(Float, nullable=True)
    swap_usd                  = Column(Float, nullable=True)

    outcome                   = Column(String(16), nullable=True)   # TP1|TP2|SL|TIMEOUT|MANUAL_CLOSE
    resolution_status         = Column(String(16), nullable=True, index=True)  # OPEN|RESOLVED|ORPHAN
    cohort                    = Column(String(48), nullable=True, index=True)
    #  cohorts: FULLY_INSTRUMENTED_FORWARD | PRE_INSTRUMENTATION_ACTUAL_DEMO | HISTORICAL_RESEARCH
    canonicalization_version  = Column(String(24), nullable=True, default="PROVISIONAL_V0")


class StrategistSellShadow(Base):
    __tablename__ = "strategist_sell_shadow"

    id                        = Column(Integer, primary_key=True, autoincrement=True)
    created_at                = Column(DateTime(timezone=True), default=_now, nullable=False, index=True)
    resolved_at               = Column(DateTime(timezone=True), nullable=True)
    opportunity_id            = Column(String(96), nullable=False, unique=True, index=True)
    verdict_id                = Column(Integer, nullable=True)

    # Frozen decision context
    conditions_passed         = Column(Integer, nullable=True)
    setup_score               = Column(Integer, nullable=True)
    quality_band              = Column(String(24), nullable=True)
    market_state              = Column(String(64), nullable=True)

    # Hypothetical plan (what would have been enqueued if execution allowed)
    hypothetical_entry        = Column(Float, nullable=True)
    sl                        = Column(Float, nullable=True)
    tp1                       = Column(Float, nullable=True)
    tp2                       = Column(Float, nullable=True)
    rr                        = Column(Float, nullable=True)

    # Resolved outcome (from forward M5 walk)
    outcome                   = Column(String(16), nullable=True)   # TP1|TP2|SL|TIMEOUT|STILL_OPEN
    exit_price                = Column(Float, nullable=True)
    hypothetical_pnl_pts      = Column(Float, nullable=True)
    mfe_pts                   = Column(Float, nullable=True)
    mae_pts                   = Column(Float, nullable=True)
    holding_min               = Column(Float, nullable=True)
    resolution_status         = Column(String(16), nullable=True, index=True)  # PENDING|RESOLVED
