from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter

router = APIRouter(prefix="/overview", tags=["overview"])

LOCAL_API_BASE = os.getenv("OVERVIEW_INTERNAL_API_BASE", "http://127.0.0.1:8000/api/v1")


def fetch_json(path: str, *, headers: dict[str, str] | None = None, timeout: int = 8) -> dict[str, Any]:
    """
    Fetch an internal API endpoint from the same backend container.
    This avoids directly importing route functions that may require Request, Depends, DB sessions, or headers.
    """
    url = f"{LOCAL_API_BASE}{path}"

    request = urllib.request.Request(
        url,
        headers=headers or {},
        method="GET",
    )

    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8")
            return {
                "status": "ok",
                "url": url,
                "http_status": response.status,
                "data": json.loads(raw) if raw else None,
                "error": None,
            }

    except urllib.error.HTTPError as exc:
        body = ""
        try:
            body = exc.read().decode("utf-8")
        except Exception:
            body = ""

        return {
            "status": "error",
            "url": url,
            "http_status": exc.code,
            "data": None,
            "error": body or str(exc),
        }

    except Exception as exc:
        return {
            "status": "error",
            "url": url,
            "http_status": None,
            "data": None,
            "error": f"{type(exc).__name__}: {exc}",
        }


def first_ok(name: str, paths: list[str], *, headers: dict[str, str] | None = None) -> dict[str, Any]:
    """
    Try several likely endpoint paths and return the first successful response.
    This is useful while route paths are being normalized.
    """
    attempts = []

    for path in paths:
        result = fetch_json(path, headers=headers)
        attempts.append(result)

        if result.get("status") == "ok":
            return {
                "status": "ok",
                "source": path,
                "data": result.get("data"),
                "attempts": attempts,
            }

    return {
        "status": "error",
        "source": None,
        "data": None,
        "attempts": attempts,
        "error": f"No working endpoint found for {name}",
    }


@router.get("/daily", summary="Daily XAU/USD institutional overview")
def daily_overview() -> dict[str, Any]:
    """
    Aggregated daily overview for XAU/USD.

    This endpoint pulls from existing backend routes:
    - health
    - candles
    - scan
    - signal
    - kill zones
    - calendar
    - bridge
    - MT5
    """

    bridge_secret = os.getenv("MT5_BRIDGE_SHARED_SECRET", "")
    bridge_headers = {"X-Bridge-Secret": bridge_secret} if bridge_secret else {}

    payload = {
        "status": "ok",
        "instrument": "XAU/USD",
        "timestamp": datetime.now(timezone.utc).isoformat(),

        "health": first_ok(
            "health",
            [
                "/health",
            ],
        ),

        "candles": {
            "m15": first_ok(
                "candles_m15",
                [
                    "/candles?pair=XAUUSD&timeframe=M15&limit=120",
                    "/candles?symbol=XAUUSD&timeframe=M15&limit=120",
                    "/candles/XAUUSD?timeframe=M15&limit=120",
                    "/candles/XAU/USD?timeframe=M15&limit=120",
                ],
            ),
            "h1": first_ok(
                "candles_h1",
                [
                    "/candles?pair=XAUUSD&timeframe=H1&limit=120",
                    "/candles?symbol=XAUUSD&timeframe=H1&limit=120",
                    "/candles/XAUUSD?timeframe=H1&limit=120",
                    "/candles/XAU/USD?timeframe=H1&limit=120",
                ],
            ),
            "h4": first_ok(
                "candles_h4",
                [
                    "/candles?pair=XAUUSD&timeframe=H4&limit=120",
                    "/candles?symbol=XAUUSD&timeframe=H4&limit=120",
                    "/candles/XAUUSD?timeframe=H4&limit=120",
                    "/candles/XAU/USD?timeframe=H4&limit=120",
                ],
            ),
            "daily": first_ok(
                "candles_daily",
                [
                    "/candles?pair=XAUUSD&timeframe=D1&limit=80",
                    "/candles?symbol=XAUUSD&timeframe=D1&limit=80",
                    "/candles/XAUUSD?timeframe=D1&limit=80",
                    "/candles/XAU/USD?timeframe=D1&limit=80",
                ],
            ),
        },

        "latest_scan": first_ok(
            "latest_scan",
            [
                "/scan/status",
                "/scan/xauusd",
                "/scan",
                "/market-view/xauusd",
                "/scan/market-view/xauusd",
            ],
        ),

        "latest_signal": first_ok(
            "latest_signal",
            [
                "/signal/current",
                "/signal",
                "/signal/history?limit=1",
                "/signal/db/history?limit=1",
            ],
        ),

        "killzones": first_ok(
            "killzones",
            [
                "/killzones/current",
                "/killzones/edge",
                "/killzones",
            ],
        ),

        "calendar_risk": first_ok(
            "calendar",
            [
                "/calendar",
                "/calendar?impact=high",
                "/calendar/today",
            ],
        ),

        "bridge_status": first_ok(
            "bridge_status",
            [
                "/bridge/status",
                "/bridge/health",
            ],
            headers=bridge_headers,
        ),

        "mt5_status": first_ok(
            "mt5_status",
            [
                "/mt5/status",
                "/mt5/positions",
            ],
        ),

        "mt5_tick": first_ok(
            "mt5_tick",
            [
                "/mt5/tick/XAUUSD",
                "/mt5/tick/XAU/USD",
            ],
        ),

        "analysis_instruction": (
            "Use this overview to produce a daily institutional XAU/USD briefing. "
            "Prioritize market structure, liquidity, session context, calendar risk, "
            "latest signal quality, bridge heartbeat, and MT5 demo execution readiness."
        ),
    }

    return payload