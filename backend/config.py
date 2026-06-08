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
    telegram_parse_mode:             str  = "HTML"
    telegram_alert_cooldown_minutes: int  = 15   # suppress duplicate alerts within window
    telegram_signal_alerts:          bool = True   # alert when BUY/SELL signal fires
    telegram_confirm_alerts:         bool = True   # alert when signal confirmed to DB
    telegram_trade_alerts:           bool = True   # alert when demo trade placed / rejected
    telegram_standby_alerts:         bool = False  # informational alerts when verdict is STAND ASIDE
    telegram_hourly_briefing:        bool = False  # hourly structured market briefing on NN:00 UTC

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
