"""
Shared rate-limiter instance.

Import `limiter` from this module in routers that need per-endpoint limits.
The limiter is attached to app.state in main.py and uses in-memory storage
(suitable for single-process local/VPS deployment).

Usage in a router:
    from rate_limit import limiter
    from fastapi import Request

    @router.get("/expensive")
    @limiter.limit("10/minute")
    def my_endpoint(request: Request, ...):
        ...

The `request: Request` parameter is required by slowapi for key extraction.
FastAPI injects it automatically — callers do not pass it explicitly.
"""
from slowapi import Limiter
from slowapi.util import get_remote_address

limiter = Limiter(
    key_func=get_remote_address,
    default_limits=[],         # no blanket limit — apply per endpoint only
    headers_enabled=False,     # True requires response: Response in every handler;
                               # disabled to keep signatures clean for local use
)
