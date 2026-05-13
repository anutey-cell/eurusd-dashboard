from typing import Literal
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    app_name: str    = "EUR/USD Signal API"
    version: str     = "1.0.0"
    api_prefix: str  = "/api/v1"

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
    database_url: str = "sqlite:///./eurusd_signals.db"

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

    # ── CORS ──────────────────────────────────────────────────────────────────
    # Accepts a comma-separated string from .env or a JSON array
    cors_origins: list[str] = [
        "http://localhost:5173",
        "http://localhost:3000",
        "http://127.0.0.1:5173",
    ]

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


settings = Settings()
