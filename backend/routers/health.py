import logging
from datetime import datetime, timezone

from fastapi import APIRouter
from pydantic import BaseModel

from config import settings

router = APIRouter(tags=["health"])
logger = logging.getLogger(__name__)


class HealthDetail(BaseModel):
    status:                   str
    version:                  str
    instrument:               str   # always "XAU/USD"
    data_mode:                str
    database:                 str
    fx_provider:              str
    calendar_provider:        str
    broker_execution_enabled: bool
    timestamp:                datetime


def _db_status() -> str:
    try:
        from database import engine
        with engine.connect() as conn:
            conn.execute(__import__("sqlalchemy").text("SELECT 1"))
        return "connected"
    except Exception as exc:
        logger.warning("DB health check failed: %s", exc)
        return "error"


@router.get("/health", response_model=HealthDetail, summary="API health check")
def health_check() -> HealthDetail:
    db = _db_status()
    logger.info("Health check db=%s mode=%s instrument=XAU/USD", db, settings.data_mode)
    return HealthDetail(
        status="ok",
        version=settings.version,
        instrument="XAU/USD",
        data_mode=settings.data_mode,
        database=db,
        fx_provider=settings.active_fx_provider,
        calendar_provider=settings.active_calendar_provider,
        broker_execution_enabled=settings.broker_execution_enabled,
        timestamp=datetime.now(timezone.utc),
    )


@router.get(
    "/instrument",
    summary="XAU/USD instrument configuration",
    description="Returns the active instrument configuration. This dashboard supports XAU/USD only.",
)
def instrument_config() -> dict:
    from pair_config import get_pair_config, get_pair_mode
    cfg = get_pair_config("xauusd")
    return {
        "ok": True,
        "data": {
            "code":              cfg["code"],
            "display":           cfg["display"],
            "symbol":            cfg["symbol"],
            "pip_size":          cfg["pip_size"],
            "target_points":     cfg["target_pips"],
            "target_label":      cfg["target_label"],
            "min_rr":            cfg["min_rr"],
            "min_score":         cfg["min_score"],
            "price_decimals":    cfg["price_decimals"],
            "news_currencies":   cfg["news_currencies"],
            "preferred_sessions": cfg["preferred_sessions"],
            "tv_symbol":         cfg["tv_symbol"],
            "max_spread_points": cfg["max_spread"],
            "mode":              get_pair_mode(),
            "supported_only":    True,
            "note":              "This dashboard is configured for XAU/USD (spot gold) only.",
        },
    }


@router.get(
    "/pairs",
    summary="Supported instrument list — XAU/USD only",
    description="Returns the single supported instrument. All other instruments return HTTP 400.",
)
def supported_pairs() -> dict:
    from pair_config import get_supported_pairs
    return {
        "ok":   True,
        "data": get_supported_pairs(),
        "note": "This dashboard supports XAU/USD only. EUR/USD and all other instruments are not active.",
    }
