import logging
from datetime import datetime, timezone

from fastapi import APIRouter
from pydantic import BaseModel

from config import settings

router = APIRouter(tags=["health"])
logger = logging.getLogger(__name__)


class HealthDetail(BaseModel):
    status:                  str
    version:                 str
    data_mode:               str
    database:                str
    fx_provider:             str
    calendar_provider:       str
    broker_execution_enabled: bool
    timestamp:               datetime


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
    logger.info("Health check db=%s mode=%s", db, settings.data_mode)
    return HealthDetail(
        status="ok",
        version=settings.version,
        data_mode=settings.data_mode,
        database=db,
        fx_provider=settings.active_fx_provider,
        calendar_provider=settings.active_calendar_provider,
        broker_execution_enabled=settings.broker_execution_enabled,
        timestamp=datetime.now(timezone.utc),
    )
