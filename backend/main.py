"""
XAU/USD Signal Dashboard — FastAPI Backend
Single-instrument: XAU/USD (spot gold) only.
ICT/SMC signal engine · SQLite storage · Manual confirmation workflow.
"""
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware

from config import settings
from database import engine
import db_models  # registers ORM classes with Base.metadata
from logging_config import setup_logging
from middleware import RequestLoggingMiddleware, AuthMiddleware
from rate_limit import limiter
from routers import health, candles, calendar, signal, analytics, backtest, execution, mt5 as mt5_router, telegram as telegram_router, engine as engine_router, readiness as readiness_router, risk as risk_router, scan as scan_router, observations as observations_router, prediction as prediction_router, killzones as killzones_router, institutional as institutional_router, bridge as bridge_router, diagnostics as diagnostics_router, strategist as strategist_router, summary as summary_router, vp_trap as vp_trap_router, kz_magnet as kz_magnet_router
from routers.overview import router as overview_router
from routers import predator_convergence as predator_convergence_router

# ── Startup / shutdown ────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    setup_logging(level="INFO")
    db_models.Base.metadata.create_all(bind=engine)
    import logging
    log = logging.getLogger(__name__)
    # ── Inline migrations (idempotent, DB-agnostic) ──────────────────────────
    # Single quotes for string defaults so the same SQL runs on both SQLite
    # (dev) and Postgres (Supabase prod). Each statement is in its own try
    # block so a "column already exists" failure doesn't abort later ones.
    try:
        from sqlalchemy import text
        with engine.begin() as _conn:
            # paper_observations.engine_id (Phase 3 — dual-engine tagging)
            try:
                _conn.execute(text(
                    "ALTER TABLE paper_observations "
                    "ADD COLUMN engine_id VARCHAR(32) NOT NULL DEFAULT 'swing'"
                ))
                log.info("Migration: added paper_observations.engine_id column")
            except Exception:
                pass    # already present
            try:
                _conn.execute(text(
                    'CREATE INDEX IF NOT EXISTS ix_paper_observations_engine_id '
                    'ON paper_observations(engine_id)'
                ))
            except Exception:
                pass
            # pending_executions table for the MT5 bridge (VPS ↔ laptop daemon).
            # create_all above handles new installs; this ensures upgrades pick it up too.
            try:
                _conn.execute(text(
                    'CREATE INDEX IF NOT EXISTS ix_pending_executions_status_created '
                    'ON pending_executions(status, created_at)'
                ))
            except Exception:
                pass
            # pending_executions.take_profit_2 — mandate TP2 (stretch / BE trigger)
            try:
                _conn.execute(text(
                    'ALTER TABLE pending_executions ADD COLUMN take_profit_2 FLOAT'
                ))
                log.info("Migration: added pending_executions.take_profit_2 column")
            except Exception:
                pass    # already present
    except Exception as _mig_exc:
        log.warning("Inline migrations skipped: %s", _mig_exc)
    log.info(
        "Server started data_mode=%s fx_provider=%s",
        settings.data_mode, settings.active_fx_provider,
    )
    # Archive scans older than 30 days (best-effort, never blocks startup)
    try:
        from database import SessionLocal
        from services.institutional_scanner import archive_old_scans
        with SessionLocal() as _db:
            deleted = archive_old_scans(_db, keep_days=30)
            if deleted:
                log.info("Startup archival: removed %d old scan records", deleted)
    except Exception as _arc_exc:
        log.debug("Startup archival skipped (non-fatal): %s", _arc_exc)

    # ── Portfolio governor startup reconciliation (2026-08-21 hardening) ─────
    # Governor stays NOT READY until MT5 heartbeat + persisted SENT reservations
    # are reconstructed. All new-order requests refuse until then.
    try:
        from services.portfolio_governor import startup_reconcile
        _r = startup_reconcile()
        log.info("Governor startup reconcile: %s", _r)
    except Exception as _gov_exc:
        log.warning("Governor startup reconcile failed: %s", _gov_exc)

    # ── Phase Final: spawn background loops for autonomous live operation ────
    try:
        from services.background_scheduler import start_background_loops, stop_background_loops
        await start_background_loops()
        log.info("Background scheduler started — autonomous 2-week live testing mode")

        # ── P9: Telegram bot command handler (long-poll) ────────────────
        try:
            from services.telegram_bot_poller import (
                start_background_poller, stop_background_poller,
            )
            if start_background_poller():
                log.info("Telegram bot poller started")
        except Exception as _bot_exc:
            log.warning("Telegram bot poller failed to start: %s", _bot_exc)

        try:
            yield
        finally:
            try:
                from services.telegram_bot_poller import stop_background_poller
                stop_background_poller()
            except Exception:
                pass
            await stop_background_loops()
    except Exception as _sched_exc:
        log.warning("Background scheduler failed to start: %s", _sched_exc)
        yield


# ── App ───────────────────────────────────────────────────────────────────────

app = FastAPI(
    title=settings.app_name,
    version=settings.version,
    description=(
        "REST API for the XAU/USD Signal Dashboard. "
        "ICT/SMC signal engine · SQLite storage · XAU/USD (spot gold) only. "
        "Manual confirmation workflow — broker execution disabled. "
        "Set DATA_MODE=live in .env to switch from demo to real candle data."
    ),
    docs_url="/docs",
    redoc_url="/redoc",
    lifespan=lifespan,
)

# ── Rate limiter ──────────────────────────────────────────────────────────────
app.state.limiter = limiter
app.add_middleware(SlowAPIMiddleware)
app.add_middleware(AuthMiddleware)

# ── CORS ──────────────────────────────────────────────────────────────────────
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "DELETE"],
    allow_headers=["*"],
    expose_headers=["X-Request-ID", "X-RateLimit-Limit", "X-RateLimit-Remaining"],
)

# ── Request logging ───────────────────────────────────────────────────────────
app.add_middleware(RequestLoggingMiddleware)


# ── Error handlers ────────────────────────────────────────────────────────────

def _error_body(message: str, detail: str | None = None) -> dict:
    obj: dict = {"error": True, "message": message}
    if detail:
        obj["detail"] = detail
    return obj


@app.exception_handler(RateLimitExceeded)
async def rate_limit_handler(request: Request, exc: RateLimitExceeded) -> JSONResponse:
    return JSONResponse(
        status_code=429,
        content=_error_body(
            "Rate limit exceeded — slow down and retry.",
            str(exc.detail) if hasattr(exc, "detail") else None,
        ),
        headers={"Retry-After": "60"},
    )


@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException) -> JSONResponse:
    detail = exc.detail
    if isinstance(detail, dict):
        # Preserve structured payloads (e.g. safety-rejection reasons)
        return JSONResponse(status_code=exc.status_code, content=detail)
    return JSONResponse(
        status_code=exc.status_code,
        content=_error_body(str(detail)),
    )


@app.exception_handler(RequestValidationError)
async def validation_error_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    errors = [f"{' → '.join(str(l) for l in e['loc'])}: {e['msg']}" for e in exc.errors()]
    return JSONResponse(
        status_code=422,
        content=_error_body(
            "Request validation failed.",
            "; ".join(errors),
        ),
    )


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    import logging
    logging.getLogger(__name__).exception("Unhandled error on %s %s", request.method, request.url.path)
    return JSONResponse(
        status_code=500,
        content=_error_body("Internal server error.", str(exc)),
    )


# ── Routers ───────────────────────────────────────────────────────────────────
prefix = settings.api_prefix

app.include_router(health.router,     prefix=prefix)  # includes /health and /pairs
app.include_router(candles.router,    prefix=prefix)
app.include_router(calendar.router,   prefix=prefix)
app.include_router(signal.router,     prefix=prefix)
app.include_router(analytics.router,  prefix=prefix)
app.include_router(backtest.router,   prefix=prefix)
app.include_router(execution.router,  prefix=prefix)
app.include_router(mt5_router.router,      prefix=prefix)
app.include_router(telegram_router.router, prefix=prefix)
app.include_router(engine_router.router,   prefix=prefix)
app.include_router(readiness_router.router, prefix=prefix)
app.include_router(risk_router.router,      prefix=prefix)
app.include_router(scan_router.router,      prefix=prefix)
app.include_router(observations_router.router, prefix=prefix)
app.include_router(prediction_router.router,   prefix=prefix)
app.include_router(killzones_router.router,    prefix=prefix)
app.include_router(institutional_router.router, prefix=prefix)
app.include_router(bridge_router.router,         prefix=prefix)
app.include_router(diagnostics_router.router,    prefix=prefix)
app.include_router(predator_convergence_router.router, prefix=prefix)
app.include_router(strategist_router.router,     prefix=prefix)
app.include_router(vp_trap_router.router,        prefix=prefix)
app.include_router(kz_magnet_router.router,      prefix=prefix)
app.include_router(overview_router, prefix="/api/v1")
# Consolidated single-call dashboard summary for AI cross-referencing.
# Mounted twice: /api/v1/summary (consistent) AND /api/summary (shorthand).
app.include_router(summary_router.router,        prefix=prefix)
app.include_router(summary_router.router,        prefix="/api")


# ── Root ──────────────────────────────────────────────────────────────────────
@app.get("/", include_in_schema=False)
async def root() -> JSONResponse:
    return JSONResponse({
        "message": "XAU/USD Signal Dashboard API",
        "docs":    "/docs",
        "version": settings.version,
    })
from datetime import datetime, timezone
import os

@app.get("/api/v1/overview/daily")
def daily_overview():
    return {
        "status": "ok",
        "instrument": "XAU/USD",
        "data_mode": os.getenv("DATA_MODE", "unknown"),
        "database": "connected",
        "fx_provider": os.getenv("FX_PROVIDER", os.getenv("MARKET_DATA_PROVIDER", "unknown")),
        "calendar_provider": os.getenv("CALENDAR_PROVIDER", "unknown"),
        "broker_execution_enabled": os.getenv("BROKER_EXECUTION_ENABLED", "false"),
        "price": None,
        "bias": "pending_engine_data",
        "market_state": "pending_engine_data",
        "timeframes": {
            "m15": {},
            "h1": {},
            "h4": {},
            "daily": {}
        },
        "levels": {
            "support": [],
            "resistance": [],
            "supply": [],
            "demand": [],
            "liquidity": []
        },
        "indicators": {
            "rsi_14": None,
            "atr_14": None,
            "ema_20": None,
            "ema_50": None,
            "vwap": None
        },
        "macro": {
            "dxy_bias": "unknown",
            "yields_bias": "unknown",
            "calendar_provider": os.getenv("CALENDAR_PROVIDER", "unknown"),
            "news_risk": "unknown"
        },
        "latest_signal": {},
        "bridge_status": {},
        "analysis_instruction": "Daily overview endpoint active. Enrich with live scan, candles, calendar and signal data.",
        "timestamp": datetime.now(timezone.utc).isoformat()
    }