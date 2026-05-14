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
from routers import health, candles, calendar, signal, analytics, backtest, execution, mt5 as mt5_router, telegram as telegram_router, engine as engine_router, readiness as readiness_router, risk as risk_router, scan as scan_router


# ── Startup / shutdown ────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    setup_logging(level="INFO")
    db_models.Base.metadata.create_all(bind=engine)
    import logging
    logging.getLogger(__name__).info(
        "Server started data_mode=%s fx_provider=%s",
        settings.data_mode, settings.active_fx_provider,
    )
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


# ── Root ──────────────────────────────────────────────────────────────────────
@app.get("/", include_in_schema=False)
async def root() -> JSONResponse:
    return JSONResponse({
        "message": "XAU/USD Signal Dashboard API",
        "docs":    "/docs",
        "version": settings.version,
    })
