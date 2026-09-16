import logging
from datetime import datetime, timezone
from typing import Any

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
    market_data:              dict[str, Any]
    cme_options:              dict[str, Any]
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


def _market_data_status() -> dict[str, Any]:
    """Read-only canonical market-data snapshot; never makes provider HTTP calls."""
    try:
        from database import SessionLocal
        from services.market_data_health import market_data_health
        with SessionLocal() as db:
            return market_data_health(db, instrument="XAU/USD")
    except Exception as exc:
        logger.warning("Market-data health check failed: %s", exc)
        return {
            "status": "unknown",
            "data_quality_score": 0,
            "stale_timeframes": [],
            "fresh_timeframes": [],
            "active_provider": None,
            "last_bar_time": None,
            "provider_by_timeframe": {},
            "last_ingest_error": {"message": str(exc)[:400]},
            "tradingview_enabled": None,
            "weekend": False,
        }


def _cme_options_status() -> dict[str, Any]:
    """CME gold-options intelligence health, independent of spot-feed health."""
    try:
        from database import SessionLocal
        from services.cme_options_context import get_cme_options_context
        from research.gold_intel.cme_live_refresh import get_last_refresh_status
        with SessionLocal() as db:
            ctx = get_cme_options_context(db, top_n=3)
        refresh = get_last_refresh_status()
        return {
            "status": ctx.get("status"),
            "bulletin_date": ctx.get("bulletin_date"),
            "bulletin_status": ctx.get("bulletin_status"),
            "age_days": ctx.get("age_days"),
            "gc_price": ctx.get("gc_price"),
            "xau_price": ctx.get("xau_price"),
            "gc_xau_basis": ctx.get("gc_xau_basis"),
            "basis_status": ctx.get("basis_status"),
            "directional_bias": ctx.get("directional_bias", "UNSIGNED_NEUTRAL"),
            "gamma_status": ctx.get("gamma_status"),
            "nearest_zones": (ctx.get("nearest_zones") or [])[:3],
            "strongest_zones": (ctx.get("strongest_zones") or [])[:3],
            "largest_oi_changes": (ctx.get("largest_oi_changes") or [])[:3],
            "zone_count": ctx.get("zone_count", 0),
            "reason": ctx.get("reason"),
            "refresh": refresh,
        }
    except Exception as exc:
        logger.warning("CME-options health check failed: %s", exc)
        return {
            "status": "UNAVAILABLE",
            "reason": str(exc)[:400],
            "directional_bias": "UNSIGNED_NEUTRAL",
            "gamma_status": "NOT_COMPUTED",
            "refresh": {"status": "UNKNOWN"},
        }


@router.get("/health", response_model=HealthDetail, summary="API health check")
def health_check() -> HealthDetail:
    db = _db_status()
    market = _market_data_status() if db == "connected" else {
        "status": "unknown",
        "data_quality_score": 0,
        "stale_timeframes": [],
        "fresh_timeframes": [],
        "active_provider": None,
        "last_bar_time": None,
        "provider_by_timeframe": {},
        "last_ingest_error": {"message": "database unavailable"},
        "tradingview_enabled": None,
        "weekend": False,
    }
    cme = _cme_options_status() if db == "connected" else {
        "status": "UNAVAILABLE",
        "reason": "database unavailable",
        "directional_bias": "UNSIGNED_NEUTRAL",
        "gamma_status": "NOT_COMPUTED",
        "refresh": {"status": "UNKNOWN"},
    }

    if db != "connected":
        overall = "error"
    elif market.get("status") in ("stale", "degraded", "unknown"):
        overall = "degraded"
    else:
        overall = "ok"

    logger.info(
        "Health check db=%s market=%s quality=%s provider=%s cme=%s mode=%s instrument=XAU/USD",
        db,
        market.get("status"),
        market.get("data_quality_score"),
        market.get("active_provider"),
        cme.get("status"),
        settings.data_mode,
    )
    return HealthDetail(
        status=overall,
        version=settings.version,
        instrument="XAU/USD",
        data_mode=settings.data_mode,
        database=db,
        fx_provider=settings.active_fx_provider,
        calendar_provider=settings.active_calendar_provider,
        broker_execution_enabled=settings.broker_execution_enabled,
        market_data=market,
        cme_options=cme,
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
