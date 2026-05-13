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
