"""
Request logging middleware.

Logs every inbound HTTP request with:
  method, path, status code, duration (ms), client IP, request ID

The request ID is a short random token added as X-Request-ID response header
and stored in the log record so application loggers can correlate their
entries to a specific request.
"""
from __future__ import annotations

import logging
import time
import uuid

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

logger = logging.getLogger("middleware.request")


class RequestLoggingMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next) -> Response:
        request_id = str(uuid.uuid4())[:8]
        start = time.perf_counter()

        response = await call_next(request)

        duration_ms = round((time.perf_counter() - start) * 1000, 1)
        status = response.status_code
        method = request.method
        path   = request.url.path

        level = logging.WARNING if status >= 400 else logging.INFO
        logger.log(
            level,
            "%s %s → %s  %.1fms  rid=%s",
            method, path, status, duration_ms, request_id,
            extra={"request_id": request_id},
        )

        response.headers["X-Request-ID"] = request_id
        return response


class AuthMiddleware(BaseHTTPMiddleware):
    """
    Optional API-key middleware.
    Only active when AUTH_ENABLED=true in .env.
    Protected paths: /signal/confirm, /signal/*/result, /execution/*, /backtest/*
    Skips /health, /pairs, /signal/analyze, /signal/current, /signal/history,
          /signal/db/history, /candles, /calendar, /analytics, /cot
    """
    _PROTECTED_PREFIXES = (
        "/api/v1/signal/confirm",
        "/api/v1/signal/db/history",  # write endpoints only
        "/api/v1/execution",
    )
    _OPEN_PREFIXES = (
        "/api/v1/health",
        "/api/v1/pairs",
        "/api/v1/signal/analyze",
        "/api/v1/signal/current",
        "/api/v1/signal/history",
        "/api/v1/candles",
        "/api/v1/calendar",
        "/api/v1/analytics",
        "/api/v1/cot",
        "/api/v1/backtest",
    )

    async def dispatch(self, request: Request, call_next):
        from config import settings
        if not settings.auth_enabled:
            return await call_next(request)

        path = request.url.path
        # Allow open paths
        if any(path.startswith(p) for p in self._OPEN_PREFIXES):
            return await call_next(request)

        # Check protected paths
        if any(path.startswith(p) for p in self._PROTECTED_PREFIXES):
            api_key = request.headers.get("X-API-Key", "")
            if not api_key or api_key != settings.api_key:
                from fastapi.responses import JSONResponse
                return JSONResponse(
                    status_code=401,
                    content={"error": True, "message": "Missing or invalid API key"},
                )

        return await call_next(request)
