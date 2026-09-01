from typing import Literal
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    app_name: str           = "XAU/USD Signal Dashboard"
    version: str            = "1.0.0"
    api_prefix: str         = "/api/v1"
    supported_instrument:   str = "xauusd"   # single-instrument mode
    xauusd_mode:            str = "MONITOR_ONLY"  # operating mode for XAU/USD

    # ── Data mode ─────────────────────────────────────────────────────────────
    # "demo"  → deterministic seeded mock data, no API key required
    # "live"  → routes to the configured provider (requires valid API key)
    data_mode: Literal["demo", "live"] = "demo"

    # ── FX candle provider ────────────────────────────────────────────────────
    # Supported: twelvedata | alpha_vantage | oanda | polygon | fmp
    fx_data_provider: str = "twelvedata"
    fx_api_key: str       = ""

    # OANDA-specific: required when fx_data_provider = "oanda"
    fx_oanda_account_id: str  = ""
    fx_oanda_environment: Literal["practice", "live"] = "practice"

    # ── Economic calendar provider ────────────────────────────────────────────
    # Supported: fmp | trading_economics | eodhd | broker
    calendar_provider: str = "fmp"
    calendar_api_key: str  = ""

    # Trading Economics specific (uses client:secret auth)
    calendar_te_client: str = ""
    calendar_te_secret: str = ""

    # ── Database ──────────────────────────────────────────────────────────────
    database_url: str = "sqlite:///./xauusd_signals.db"

    # ── Macro data feeds (Tier 3 — fundamental confluence layers) ────────────
    fred_api_key:        str = ""    # https://fred.stlouisfed.org/docs/api/api_key.html
    myfxbook_enabled:    bool = False
    myfxbook_email:      str = ""
    myfxbook_password:   str = ""

    # ── Backtesting ───────────────────────────────────────────────────────────
    # Applied at entry to simulate real execution costs.
    backtest_spread_pips: float   = 1.0
    backtest_slippage_pips: float = 0.5

    # ── Broker execution (disabled by default) ────────────────────────────────
    # Enable ONLY after 100+ backtested setups, 50+ paper-traded signals,
    # positive expectancy, and acceptable drawdown have been verified.
    broker_execution_enabled: bool  = False
    broker_provider:          str   = "oanda"
    broker_api_key:           str   = ""
    broker_account_id:        str   = ""
    max_risk_per_trade_percent: float = 0.25
    daily_loss_limit_percent:   float = 1.0

    # ── MT5 demo integration ──────────────────────────────────────────────────
    mt5_enabled:              bool  = False
    mt5_mode:                 str   = "demo"      # "demo" | "live" (live blocks execution)
    mt5_server:               str   = ""
    mt5_login:                str   = "0"   # stored as str; provider converts to int
    mt5_password:             str   = ""          # NEVER logged or returned in responses
    mt5_execution_enabled:    bool  = False       # both this AND allow_demo_trading must be true
    allow_demo_trading:       bool  = False
    max_open_trades:          int   = 1
    max_spread_xauusd_points: float = 5.0   # XAU/USD spread gate in points

    # ── Autonomous live execution (learning-mode trading) ─────────────────────
    # Master switch. When True AND data_mode=="live" AND live_trading_authorized,
    # the background executor will fire orders that pass the 3-layer confirmation
    # gate (scanner SIGNAL_READY + predictor STRONG/MODERATE + killzone TRADE/PRESS).
    auto_execution_enabled:           bool  = False
    # Hard cap. Even if risk-percent math says larger, the executor never
    # submits more than this lot size. Set explicitly per user policy.
    auto_execution_max_lot:           float = 0.05
    # Daily trade ceiling — counted from accepted MT5TradeLog rows since 00:00 UTC.
    auto_execution_max_trades_per_day:int   = 3
    # MUST be set true to allow execution on a live MT5 account.
    # Gate 9 (demo-only) remains hard-blocked unless this is explicitly true.
    live_trading_authorized:          bool  = False
    # Auto-executor poll interval (seconds). Default 60s = once per minute,
    # synced with scanner cadence.
    auto_execution_interval_sec:      int   = 60

    # ── MT5 Bridge (Linux VPS ↔ Windows laptop) ──────────────────────────────
    # When True, the auto-executor pushes orders into the pending-executions
    # queue (instead of calling MT5 directly). A daemon on the Windows laptop
    # polls /api/v1/bridge/pending-orders and executes them via MetaTrader5,
    # then POSTs the result back to /api/v1/bridge/result/{id}.
    #
    # Use this in production deployments where the dashboard runs on Linux
    # (Oracle Cloud VPS) but MT5 execution must happen on Windows.
    mt5_bridge_enabled:           bool = False
    # Shared secret protecting the bridge endpoints. Generate with:
    #   python -c "import secrets; print(secrets.token_urlsafe(48))"
    mt5_bridge_shared_secret:     str  = ""
    # Previous shared secret — accepted during rotation windows so the daemon
    # can migrate without downtime. Clear once the daemon is on the new value.
    mt5_bridge_shared_secret_prev: str  = ""

    # ── CORS ──────────────────────────────────────────────────────────────────
    # Accepts a comma-separated string from .env or a JSON array
    cors_origins: list[str] = [
        "http://localhost:5173",
        "http://localhost:3000",
        "http://127.0.0.1:5173",
    ]

    # ── Candle caching ────────────────────────────────────────────────────────
    candle_cache_enabled:    bool = True
    candle_cache_ttl_seconds: int = 60

    # ── Alert system ─────────────────────────────────────────────────────────
    alerts_enabled:       bool = False
    alert_webhook_url:    str  = ""
    alert_email_to:       str  = ""

    # ── Telegram bot notifications ────────────────────────────────────────────
    # Get token from @BotFather. Chat ID = your personal chat or channel.
    # SECURITY: NEVER commit token to git — keep in .env only.
    # Primary env var: TELEGRAM_ALERTS_ENABLED (spec-compliant name)
    telegram_alerts_enabled:         bool = False
    telegram_bot_token:              str  = ""   # NEVER logged or returned in API responses
    telegram_chat_id:                str  = ""
    # Secondary (backup) recipient — separate bot + chat. _send_plain broadcasts
    # to both if configured. Independent try/except per recipient.
    telegram_bot_token_2:            str  = ""   # NEVER logged or returned in API responses
    telegram_chat_id_2:              str  = ""
    telegram_parse_mode:             str  = "HTML"
    telegram_alert_cooldown_minutes: int  = 15   # suppress duplicate alerts within window
    telegram_signal_alerts:          bool = True   # alert when BUY/SELL signal fires
    telegram_confirm_alerts:         bool = True   # alert when signal confirmed to DB
    telegram_trade_alerts:           bool = True   # alert when demo trade placed / rejected
    telegram_standby_alerts:         bool = False  # informational alerts when verdict is STAND ASIDE
    telegram_hourly_briefing:        bool = False  # hourly structured market briefing on NN:00 UTC
    telegram_preformation_alerts:    bool = True   # heads-up 1 hour before each major killzone opens

    # ── Predator notification architecture (2026-08-28 refactor) ─────────────
    # legacy   → current direct _send_plain paths (pre-refactor behaviour, kept for rollback only)
    # shadow   → gateway computes decision + logs but never sends; legacy path still runs
    # gateway  → gateway is the sole sender; legacy direct-send paths become no-ops
    # Production target: `gateway`. Default until shadow validates: `shadow`.
    predator_notification_architecture: str  = "shadow"
    # ACTIONABLE_ONLY → only qualifying FIREs + their lifecycle (default, matches user mandate)
    # PRE_ALERT       → reserved; currently not implemented (design defers PRE_ALERT)
    predator_notification_mode:         str   = "ACTIONABLE_ONLY"
    # Setup identity — price zone bucket in points. Prevents 4430.07 vs 4429.99
    # from creating separate hypotheses. Configurable so we can tighten later.
    predator_setup_price_bucket:        float = 5.0
    # Actionability gate — minimum RR to Telegram. Signals below this stay silent.
    predator_notification_min_rr:       float = 1.2
    # Freshness threshold for the last M5 candle (minutes). Older = data_stale → silent.
    predator_notification_max_bar_age_min: int = 15

    # ── VP Trap strategy (Previous-Day Volume Profile + Trapped Traders) ─────
    # Independent secondary strategy that identifies failed breakouts of prev-day
    # PDH/PDL/VAH/VAL and generates high-conviction reversal signals only when
    # a full trap → displacement → retest → rejection sequence completes.
    # Default is signal-only, independent mode. Never affects existing engines.
    vp_trap_enabled:                 bool  = True    # master toggle (Phase 4 ships enabled)
    vp_trap_mode:                    str   = "independent"  # independent | confirmation | confluence
    vp_trap_live_threshold:          int   = 70      # P132: aggressive — was 80
    vp_trap_watch_threshold:         int   = 55      # P132: aggressive — was 60
    vp_trap_countertrend_bonus:      int   = 10      # extra threshold for countertrend setups
    vp_trap_value_area_pct:          float = 0.70    # standard TPO 70%
    vp_trap_min_rr:                  float = 1.8     # minimum RR to fire
    vp_trap_zone_expiry_hours:       int   = 48      # armed zone expiry
    vp_trap_max_retests:             int   = 3       # max retests before zone stales
    vp_trap_alert_cooldown_s:        int   = 600     # P132: was 1800 (30m) → 10m
    vp_trap_auto_execute:            bool  = False   # OFF initially — validate live first
    vp_trap_penalize_tick_volume:    int   = 15      # score penalty when only tick-volume proxy
    vp_trap_telegram_alerts:         bool  = True    # send Telegram alerts (when enabled)

    # ── KZ Magnet strategy (Killzone POC magnet + Flux directional bias) ────
    # Uses backtest-validated cross-KZ POC touch rates (60-85%) as tradeable
    # magnets. Signal-only initially — validate live before enabling execution.
    kz_magnet_enabled:               bool  = True    # ships enabled (signal-only, no exec)
    kz_magnet_telegram_alerts:       bool  = True    # Telegram alerts when enabled
    kz_magnet_min_distance_atr:      float = 0.6     # min distance from POC to trigger
    kz_magnet_max_va_width_atr:      float = 2.0     # skip prior KZ if VA too wide
    kz_magnet_alert_cooldown_s:      int   = 900     # P132: was 1800 (30m) → 15m

    # ── Mandate demo-execution opt-in ─────────────────────────────────────────
    # When True, the strategist will enqueue a 0.01-lot MT5 PendingExecution row
    # every time execution_status == DEMO_TRADE_PLACED. Operator opts in
    # explicitly; default OFF so the system runs as SIGNAL_ONLY out-of-the-box.
    demo_auto_enqueue:               bool = False

    # ── Execution authority selector ──────────────────────────────────────────
    # True  = institutional demo-mandate strategist is the sole decision engine
    #         (5-condition scoring, fixed 0.01 lot, live execution hard-disabled)
    # False = legacy 5-gate auto_executor runs (development / back-compat only)
    use_mandate_strategist:          bool = True

    # ── Monday observation mode ───────────────────────────────────────────────
    # Per operator risk plan: Monday is observation-only. Signal alerts still
    # fire (so the operator can study Monday's setups + assess weekly direction)
    # but no MT5 order is enqueued. Execution resumes Tuesday 00:00 UTC.
    # Rationale: Mondays carry over Friday positioning + open with gap risk;
    # Tuesday onward gives cleaner institutional flow.
    monday_observation_mode:         bool = True

    # ── Position-cap risk gate (dynamic pyramid) ──────────────────────────────
    # Base cap: 5 concurrent positions of 0.01 lot each.
    # When floating P&L crosses the threshold AND HTF trend continues AND
    # recent volume confirms institutional participation, the cap relaxes
    # to the extended ceiling (pyramid scaling on confirmed trend).
    # If any of those three conditions falls back, cap snaps to base —
    # existing positions stay, but no new adds are allowed.
    max_concurrent_positions:        int   = 5      # base cap (kept name for back-compat)
    max_positions_extended:          int   = 10     # cap when pyramid override is active
    extended_cap_profit_usd:         float = 300.0  # floating P&L threshold to unlock extended cap
    extended_cap_volume_ratio:       float = 1.2    # recent 3-bar vol vs prior-20 median ratio
    # Legacy fallback — honoured if TELEGRAM_ENABLED is set in older .env files
    telegram_enabled:                bool = False

    # ── TradingView market data ───────────────────────────────────────────────
    tradingview_enabled:  bool = False
    tradingview_username: str  = ""    # NEVER logged or returned in responses
    tradingview_password: str  = ""    # NEVER logged or returned in responses

    # ── MyFXBook community sentiment ──────────────────────────────────────────
    myfxbook_enabled:  bool = False
    myfxbook_email:    str  = ""
    myfxbook_password: str  = ""       # NEVER logged or returned in responses

    # ── Institutional scanner ─────────────────────────────────────────────────
    auto_scan_enabled:          bool = True
    scan_cache_ttl_seconds:     int  = 45    # cache freshness window in seconds
    scan_interval_seconds:      int  = 60    # frontend auto-refresh interval hint
    telegram_watchlist_alerts:  bool = False  # send WATCHLIST state alerts

    # ── Fixed-dollar TP structure (P130) ──────────────────────────────────────
    # Trade goal: extract $20 min → $40 max XAU/USD price change per trade
    # (i.e. 20-40 points per setup, at 0.01 lot). Trade plans are generated
    # with FIXED-POINT TPs instead of ATR multiples, and SL is capped so RR
    # stays ≥ mandate floor. Any structural stop that would require wider
    # SL than max_sl_points is rejected as "sl_too_wide_for_target" —
    # this keeps every fired setup inside the operator's capture envelope.
    target_tp1_points:              float = 20.0    # first exit / breakeven trigger
    target_tp2_points:              float = 40.0    # full close cap
    max_sl_points:                  float = 20.0    # skip trades needing wider stops
    min_sl_points:                  float = 8.0     # noise floor
    fixed_tp_enabled:               bool  = True    # master switch (off = revert to ATR R-multiples)

    # ── Execution quality + headwind gates (P128) ─────────────────────────────
    # Sit on top of the 5-condition mandate. A verdict at 4/5+ still has to
    # clear these gates before demo execution fires. Each can be disabled
    # independently. Post-analysis of 30-day live data (7-25 → 8-26) showed
    # the mandate was over-firing 10× the designed rate because the
    # coarse conditions_passed count is the only quality gate — the
    # fine-grain setup_score and negative-EV setups slipped through.
    min_setup_score_for_execution:  int   = 85      # Q1: floor on 0-100 score
    require_positive_ev:            bool  = True    # Q2: est_WR × RR must be > 1
    session_penalty_enabled:        bool  = True    # H1: NY / overlap penalty
    post_loss_cooldown_min:         int   = 60      # H2: minutes after last stop
    direction_flip_cooldown_min:    int   = 120     # H3: minutes after flip alert
    execution_tight_spread_pts:     float = 3.0     # H4: below → clean
    execution_hard_spread_pts:      float = 5.0     # H4: above → hard block
    news_proximity_lookahead_min:   int   = 30      # H5: no exec within window

    # ── 30-day VP Trap measurement protocol (P135) ────────────────────────────
    # Records every VP Trap live alert as a measurement row + a background
    # loop advances the outcome (TRIGGER / TP / SL / EXPIRED) against live
    # price. Query stats via /api/v1/diagnostics/vp-trap-measurement?days=30.
    # Verdict: ON TARGET only when signals/day 1-3, WR >= 40%, avg R >= +0.15R,
    # over >= 20 closed trades. Below that we don't have a niche.
    vp_trap_measurement_enabled: bool = True

    # ── External confluence layer (P134) ──────────────────────────────────────
    # Read-only confirmation layer: FastBull retail positioning + CME GC
    # futures context. Never generates trades on its own; only nudges the
    # setup_score (+5/+10 confirm, -5/-15 conflict) or downgrades execution
    # to EXTERNAL_CONFLUENCE_CONFLICT. Fails open as unavailable/neutral.
    external_confluence_enabled:    bool  = True
    fastbull_confluence_enabled:    bool  = True
    cme_confluence_enabled:         bool  = True
    external_confluence_cache_seconds: int = 600   # 10 min
    external_confluence_http_timeout_s: int = 3    # never delay the scanner
    external_confluence_max_downgrade: int = 15    # cap on negative score adj
    external_confluence_max_upgrade:  int  = 10    # cap on positive score adj

    # ── Telegram bot (command-handler poller) ─────────────────────────────────
    # When True AND telegram_bot_token is set, a background thread long-polls
    # getUpdates and dispatches slash-commands (/status, /signals, /mute, ...).
    # Only chats explicitly marked is_admin in TelegramChatPreference may run
    # privileged commands (/mute, /unmute, /mode).
    telegram_bot_enabled: bool = True

    # ── Canonical notification layer (unified Telegram pipeline) ─────────────
    # Runs alongside the legacy Telegram path. When shadow_mode is True the
    # canonical layer only writes audit rows (TelegramNotification with
    # delivery_result='suppressed' + reason='shadow_mode_dry_run'), so we can
    # compare against the legacy alert_log without double-firing. Once the two
    # agree on all shipped signals, flip shadow_mode → False and disable the
    # matching legacy path for the same strategy.
    notification_canonical_enabled: bool = True    # master switch for the new layer

    # Per-message-type confidence thresholds (env override for the router's
    # _DEFAULT_THRESHOLDS). Set MONITORING high to mute watchlist heads-ups;
    # keep INVALIDATED >= ACTIONABLE so only real-trade candidates trigger
    # invalidation alerts. 101 = always suppress.
    notification_threshold_monitoring:      int = 65
    notification_threshold_actionable:      int = 80
    notification_threshold_high_confluence: int = 90
    notification_threshold_invalidated:     int = 65
    notification_threshold_expired:         int = 80
    notification_threshold_entry_triggered: int = 80
    notification_threshold_tp1_hit:         int = 80
    notification_threshold_tp2_hit:         int = 80
    notification_threshold_final_target:    int = 80
    notification_threshold_breakeven:       int = 80
    notification_threshold_trailing:        int = 80
    notification_threshold_stop_hit:        int = 0
    notification_threshold_post_trade_review: int = 0
    notification_threshold_end_of_session:  int = 0
    notification_shadow_mode:       bool = True    # canonical is dry-run parallel to legacy
    notification_mode:              str  = "standard"  # minimal | standard | detailed

    # ── Signal grading (A+/A/B/C) — signal-only quality gate ─────────────────
    # These control which alerts reach Telegram. NO EXECUTION IMPACT — the
    # execution enqueue path is separately gated by allow_demo_trading (which
    # is False in signals-only mode).
    signal_min_score_a:      int   = 80      # score ≥ 80 → grade A eligible
    signal_min_score_aplus:  int   = 90      # score ≥ 90 → grade A+ eligible
    signal_min_rr_a:         float = 2.5     # RR ≥ 2.5   → grade A eligible
    signal_min_rr_aplus:     float = 3.0     # RR ≥ 3.0   → grade A+ eligible
    signal_watchlist_score:  int   = 70      # score 70-79 → grade B (watchlist)
    signal_watchlist_alerts_enabled: bool = False   # False = never send B alerts

    # ── Mandate lot sizing (RE-REVISED 2026-08-11 — 0.01 fixed on DEMO only) ──
    # Comprehensive brief on 2026-08-11 (later same day) reverted the earlier
    # grade-based 0.05-0.15 scheme back to fixed 0.01 for demo testing.
    # Brief also added hard account/symbol guards enforced in strategist_runner.
    mandate_lot_a_plus:            float = 0.01
    mandate_lot_a:                 float = 0.01
    mandate_lot_b:                 float = 0.01
    mandate_lot_max_aggregate:     float = 0.01

    # Demo-only account guard — hard-refuse enqueue if these don't match.
    # ONLY these credentials are allowed to receive test orders.
    mandate_demo_login:            int   = 435888680
    mandate_demo_server_contains:  str   = "Trial"     # server must contain this substring
    mandate_demo_symbol:           str   = "XAUUSD"

    # ── Legacy mandate gates — DISABLED per Phase 9 empirical audit 2026-08-12
    # Each of these gates removed net POSITIVE expectancy from predator signals:
    #   killzone_gate       blocked +11,243 pts of profit (368 wins, 581 losses)
    #   extended_ema20_gate blocked +12,903 pts of profit (508 wins, 808 losses)
    #   atr_high_gate       blocked  +9,418 pts of profit (410 wins, 544 losses)
    # Kept as feature flags so we can re-enable individually if a regime shift
    # makes them useful again. Set True to restore the old suppressive behavior.
    mandate_killzone_gate_enabled:        bool = False
    mandate_extended_ema20_gate_enabled:  bool = False
    mandate_atr_high_gate_enabled:        bool = False

    # ── Predator engine — outcome-first empirical signals (2026-08-12) ────────
    # Runs in parallel with the mandate strategist. Emits distinct alerts for
    # walk-forward-validated edges: ASIAN_BREAKDOWN, PDL_BREAK, VOL_CONTINUATION.
    # Records every signal to shadow_trades for outcome tracking. Telegram is
    # off by default so operator can review before it fires to their phone.
    predator_enabled:              bool = True
    predator_telegram_enabled:     bool = False
    predator_loop_interval_s:      int  = 60
    predator_dedupe_cooldown_s:    int  = 3600      # 1 hour between same fingerprint

    # DEMO execution wiring (spec §7 — Predator sizing architecture).
    # Master gate. When False, every FIRE plans a batch + writes shadow rows
    # (predator_signal_batches + predator_positions status=PLANNED) but sends
    # NO MT5 orders. Flip to True to activate STANDARD-tier execution.
    predator_execution_enabled:    bool  = False

    # EXPANSION mode gate. Only takes effect when execution is enabled AND the
    # per-archetype VOLUME_EXPANSION_CONFIRMED threshold is met on the fire bar.
    # Kept as a separate flag so STANDARD-only rollouts are possible.
    predator_expansion_enabled:    bool  = False

    # Non-negotiable sizing constants — mirrored from predator_position_sizer.
    # Kept here as env-visible values but the sizer treats its own module
    # constants as authoritative; do NOT flex these to "increase" exposure.
    predator_lot_size:             float = 0.03
    predator_standard_max_positions: int = 5
    predator_expansion_max_positions: int = 10
    predator_standard_max_exposure_lots: float = 0.15
    predator_expansion_max_exposure_lots: float = 0.30
    predator_absolute_exposure_ceiling: float = 0.30

    # Staged deployment gap between tickets (seconds). Small — gives the bridge
    # daemon breathing room between fills without letting price drift too much.
    predator_stage_delay_s:        float = 0.5

    # ── Shadow trade simulator (data source for grader calibration) ──────────
    # Records EVERY BUY/SELL verdict (all grades A+/A/B/C) with a complete
    # trade plan into shadow_trades, then a background loop walks each row
    # against live price and marks TRIGGERED / TP1_HIT / TP2_HIT / STOPPED /
    # INVALIDATED / EXPIRED. Pure data capture — NEVER executes, NEVER alerts.
    # Query via /api/v1/diagnostics/shadow-trade-stats?days=30.
    shadow_trade_recording_enabled:   bool = True
    shadow_trade_advance_interval_s:  int  = 60
    shadow_trade_expiry_hours:        int  = 12

    # ── Phase 2: Canonical market data service ────────────────────────────────
    # Single source of truth for every strategy: bid/ask/spread/candles/session/
    # levels/data_quality. Ships behind a flag so we can shadow-verify via
    # /api/v1/diagnostics/canonical-market-data before wiring strategies to it.
    xauusd_canonical_data_enabled: bool = False
    xauusd_canonical_data_cache_ttl_s: int = 15

    # ── Phase 3-14: Directional intelligence build (all off until validated) ──
    # Each flag corresponds to one phase of the market-intelligence overhaul.
    # Stays False until the phase's replay validation passes.
    xauusd_market_regime_enabled: bool = False
    xauusd_weighted_htf_alignment_enabled: bool = False
    xauusd_directional_intelligence_enabled: bool = False
    xauusd_breakout_acceptance_enabled: bool = False
    xauusd_opportunity_state_machine_enabled: bool = False
    xauusd_separated_verdicts_enabled: bool = False
    xauusd_key_level_ranking_enabled: bool = False
    xauusd_macro_interpretation_enabled: bool = False
    xauusd_market_intelligence_telegram_enabled: bool = False
    xauusd_market_intel_shadow_mode: bool = True     # canonical shadow while true
    xauusd_opportunity_coverage_enabled: bool = False
    xauusd_replay_validation_enabled: bool = False

    # ── Auth (optional API key protection) ───────────────────────────────────
    auth_enabled: bool = False
    api_key:      str  = "change_me"

    # ── Rate limit backend ────────────────────────────────────────────────────
    # "memory" = in-process (default, single node)
    # "redis"  = Redis backend (multi-node VPS)
    rate_limit_backend: str = "memory"
    redis_url:          str = "redis://localhost:6379"

    # ── Derived helpers (not settable) ────────────────────────────────────────
    @property
    def data_source(self) -> str:
        """Human-readable label for health endpoint."""
        if self.data_mode == "demo":
            return "demo"
        return self.fx_data_provider

    @property
    def active_fx_provider(self) -> str:
        return "demo" if self.data_mode == "demo" else self.fx_data_provider

    @property
    def active_calendar_provider(self) -> str:
        return "demo" if self.data_mode == "demo" else self.calendar_provider

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"
        extra = "ignore"  # silently ignore unrecognised env vars


settings = Settings()
