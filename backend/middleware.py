"""Request logging and API/control-plane authentication middleware."""
from __future__ import annotations

import hmac
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

        # MT5 routes can return 503 inside Docker (Windows-only package).
        _is_expected_503 = status == 503 and "/mt5/" in path
        level = logging.INFO if (status < 400 or _is_expected_503) else logging.WARNING
        logger.log(
            level,
            "%s %s → %s  %.1fms  rid=%s",
            method, path, status, duration_ms, request_id,
            extra={"request_id": request_id},
        )

        response.headers["X-Request-ID"] = request_id
        return response


def _strong_operator_key(settings) -> str | None:
    """Return configured operator API key, rejecting placeholder/default values."""
    key = str(getattr(settings, "api_key", "") or "").strip()
    if not key or key.lower() in {"change_me", "changeme", "default", "test"}:
        return None
    return key


def _secret_matches(candidate: str, expected: str | None) -> bool:
    if not candidate or not expected:
        return False
    try:
        return hmac.compare_digest(candidate, expected)
    except Exception:
        return False


class AuthMiddleware(BaseHTTPMiddleware):
    """Authentication boundary for public data vs operator control-plane routes.

    Historical behaviour made API-key protection optional and only covered a
    few legacy endpoints. During takeover, routes that can trigger side effects
    or expose account/position state are ALWAYS protected, even when the broad
    `AUTH_ENABLED` switch is false. If no non-placeholder operator key exists,
    these control routes fail closed with HTTP 503 rather than becoming public.

    Bridge machine-to-machine mutation routes retain their own shared-secret
    dependency. `/bridge/status` accepts either that bridge secret or the strong
    operator API key so the private status can still be inspected securely.
    """

    # These paths can enqueue/execute/send or force a fresh side-effectful
    # strategist compute. They require a strong operator X-API-Key regardless
    # of the legacy AUTH_ENABLED setting.
    _ALWAYS_OPERATOR_PREFIXES = (
        "/api/v1/strategist/decision",
        "/api/v1/strategist/refresh",
        "/api/v1/strategist/briefing/send-now",
        "/api/v1/strategist/newsletter/",
        "/api/v1/strategist/learnings/digest-now",
        "/api/v1/mt5/demo-order",
        "/api/v1/execution",
    )

    # Legacy API-key coverage when AUTH_ENABLED=true.
    _PROTECTED_PREFIXES = (
        "/api/v1/signal/confirm",
        "/api/v1/signal/db/history",
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

    @staticmethod
    def _json_error(status: int, message: str):
        from fastapi.responses import JSONResponse
        return JSONResponse(status_code=status, content={"error": True, "message": message})

    async def dispatch(self, request: Request, call_next):
        from config import settings

        path = request.url.path
        operator_key = _strong_operator_key(settings)
        supplied_api_key = request.headers.get("X-API-Key", "")

        # Account/position status is never public. Accept machine bridge secret
        # OR a strong operator key. Compare secrets in constant time.
        if path.startswith("/api/v1/bridge/status"):
            supplied_bridge = request.headers.get("X-Bridge-Secret", "")
            current = str(getattr(settings, "mt5_bridge_shared_secret", "") or "").strip()
            previous = str(getattr(settings, "mt5_bridge_shared_secret_prev", "") or "").strip()
            bridge_ok = (
                _secret_matches(supplied_bridge, current)
                or _secret_matches(supplied_bridge, previous)
            )
            operator_ok = bool(operator_key and _secret_matches(supplied_api_key, operator_key))
            if not (bridge_ok or operator_ok):
                return self._json_error(401, "Bridge status requires bridge secret or operator API key")
            return await call_next(request)

        # Side-effectful/control routes are fail-closed independent of the old
        # global AUTH_ENABLED switch.
        if any(path.startswith(p) for p in self._ALWAYS_OPERATOR_PREFIXES):
            if operator_key is None:
                return self._json_error(
                    503,
                    "Operator API key is not securely configured; control endpoint disabled",
                )
            if not _secret_matches(supplied_api_key, operator_key):
                return self._json_error(401, "Missing or invalid operator API key")
            return await call_next(request)

        # Preserve ordinary historical auth policy for non-control endpoints.
        if not settings.auth_enabled:
            return await call_next(request)

        if any(path.startswith(p) for p in self._OPEN_PREFIXES):
            return await call_next(request)

        if any(path.startswith(p) for p in self._PROTECTED_PREFIXES):
            if operator_key is None:
                return self._json_error(503, "API key protection enabled but no strong API key is configured")
            if not _secret_matches(supplied_api_key, operator_key):
                return self._json_error(401, "Missing or invalid API key")

        return await call_next(request)
