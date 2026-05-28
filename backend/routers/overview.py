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


def fetch_json(path: str, *, headers: dict[str, str] | None = None, timeout: int = 10) -> dict[str, Any]:
    """
    Fetch an internal VPS/backend API endpoint.
    This overview endpoint must not depend on the laptop-local MT5 bridge.
    """
    url = f"{LOCAL_API_BASE}{path}"

    req = urllib.request.Request(
        url,
        headers=headers or {},
        method="GET",
    )

    try:
        with urllib.request.urlopen(req, timeout=timeout) as res:
            raw = res.read().decode("utf-8")
            data = json.loads(raw) if raw else None

            return {
                "ok": True,
                "source": path,
                "http_status": res.status,
                "data": data,
                "error": None,
            }

    except urllib.error.HTTPError as exc:
        body = ""
        try:
            body = exc.read().decode("utf-8")
        except Exception:
            body = ""

        return {
            "ok": False,
            "source": path,
            "http_status": exc.code,
            "data": None,
            "error": body or str(exc),
        }

    except Exception as exc:
        return {
            "ok": False,
            "source": path,
            "http_status": None,
            "data": None,
            "error": f"{type(exc).__name__}: {exc}",
        }


def first_working(name: str, paths: list[str], *, headers: dict[str, str] | None = None) -> dict[str, Any]:
    """
    Try multiple possible route paths and return the first successful one.
    Keeps deployment resilient while route names are being normalized.
    """
    attempts: list[dict[str, Any]] = []

    for path in paths:
        result = fetch_json(path, headers=headers)
        attempts.append(result)

        if result["ok"]:
            return {
                "status": "ok",
                "name": name,
                "source": result["source"],
                "data": result["data"],
                "attempts": attempts,
            }

    return {
        "status": "unavailable",
        "name": name,
        "source": None,
        "data": None,
        "attempts": attempts,
        "error": f"No working endpoint found for {name}",
    }


def extract_health(health_block: dict[str, Any]) -> dict[str, Any]:
    data = health_block.get("data") or {}

    if isinstance(data, dict) and "data" in data and isinstance(data["data"], dict):
        data = data["data"]

    return {
        "status": data.get("status"),
        "instrument": data.get("instrument"),
        "data_mode": data.get("data_mode"),
        "database": data.get("database"),
        "fx_provider": data.get("fx_provider"),
        "calendar_provider": data.get("calendar_provider"),
        "broker_execution_enabled": data.get("broker_execution_enabled"),
        "timestamp": data.get("timestamp"),
    }


@router.get("/daily", summary="Daily XAU/USD institutional overview")
def daily_overview() -> dict[str, Any]:
    """
    Public daily overview for ChatGPT / analyst review.

    Design principle:
    - VPS data is the source for public overview.
    - Laptop MT5 bridge remains local execution/backup price feed.
    - This endpoint should be safely fetchable from ChatGPT.
    """

    bridge_secret = os.getenv("MT5_BRIDGE_SHARED_SECRET", "").strip()
    bridge_headers = {"X-Bridge-Secret": bridge_secret} if bridge_secret else {}

    health = first_working(
        "health",
        [
            "/health",
        ],
    )

    # Keep paths flexible until exact router paths are normalized.
    candles_m15 = first_working(
        "candles_m15",
        [
            "/candles?pair=XAUUSD&timeframe=M15&limit=120",
            "/candles?pair=XAU/USD&timeframe=M15&limit=120",
            "/candles?symbol=XAUUSD&timeframe=M15&limit=120",
            "/candles/XAUUSD?timeframe=M15&limit=120",
            "/candles/XAUUSD/M15?limit=120",
        ],
    )

    candles_h1 = first_working(
        "candles_h1",
        [
            "/candles?pair=XAUUSD&timeframe=H1&limit=120",
            "/candles?pair=XAU/USD&timeframe=H1&limit=120",
            "/candles?symbol=XAUUSD&timeframe=H1&limit=120",
            "/candles/XAUUSD?timeframe=H1&limit=120",
            "/candles/XAUUSD/H1?limit=120",
        ],
    )

    candles_h4 = first_working(
        "candles_h4",
        [
            "/candles?pair=XAUUSD&timeframe=H4&limit=120",
            "/candles?pair=XAU/USD&timeframe=H4&limit=120",
            "/candles?symbol=XAUUSD&timeframe=H4&limit=120",
            "/candles/XAUUSD?timeframe=H4&limit=120",
            "/candles/XAUUSD/H4?limit=120",
        ],
    )

    candles_d1 = first_working(
        "candles_d1",
        [
            "/candles?pair=XAUUSD&timeframe=D1&limit=80",
            "/candles?pair=XAU/USD&timeframe=D1&limit=80",
            "/candles?symbol=XAUUSD&timeframe=D1&limit=80",
            "/candles/XAUUSD?timeframe=D1&limit=80",
            "/candles/XAUUSD/D1?limit=80",
        ],
    )

    latest_scan = first_working(
        "latest_scan",
        [
            "/scan/status",
            "/scan/xauusd",
            "/scan",
            "/scan/market-view/xauusd",
            "/market-view/xauusd",
        ],
    )

    latest_signal = first_working(
        "latest_signal",
        [
            "/signal/current",
            "/signal",
            "/signal/history?limit=1",
            "/signal/db/history?limit=1",
        ],
    )

    calendar_risk = first_working(
        "calendar_risk",
        [
            "/calendar",
            "/calendar?impact=high",
            "/calendar/today",
        ],
    )

    killzone = first_working(
        "killzone",
        [
            "/killzones/current",
            "/killzones/edge",
            "/killzones",
        ],
    )

    bridge_status = first_working(
        "bridge_status",
        [
            "/bridge/status",
            "/bridge/health",
        ],
        headers=bridge_headers,
    )

    health_summary = extract_health(health)

    execution_readiness = {
        "mode": "demo",
        "lot_size": 0.01,
        "broker_execution_enabled": health_summary.get("broker_execution_enabled"),
        "live_execution_allowed": False,
        "laptop_bridge_required_for_mt5_execution": True,
        "note": (
            "Dashboard may scan and alert from VPS. MT5 execution requires laptop bridge "
            "or Windows VPS bridge to be running."
        ),
    }

    return {
        "status": "ok",
        "instrument": "XAU/USD",
        "timestamp": datetime.now(timezone.utc).isoformat(),

        "health": health_summary,

        "market_data": {
            "source": health_summary.get("fx_provider") or "twelvedata",
            "candles": {
                "m15": candles_m15,
                "h1": candles_h1,
                "h4": candles_h4,
                "d1": candles_d1,
            },
        },

        "latest_scan": latest_scan,
        "latest_signal": latest_signal,
        "calendar_risk": calendar_risk,
        "killzone": killzone,
        "bridge_status": bridge_status,
        "execution_readiness": execution_readiness,

        "analysis_contract": {
            "purpose": "Provide ChatGPT with one public VPS endpoint for daily XAU/USD overview.",
            "chatgpt_can_use_this": True,
            "depends_on_laptop_localhost": False,
            "laptop_bridge_role": "MT5 execution and backup Exness price feed only.",
            "expected_analysis": [
                "daily bias",
                "market structure",
                "liquidity zones",
                "latest scan/signal quality",
                "calendar/news risk",
                "kill-zone context",
                "execution readiness",
                "long scenario",
                "short scenario",
                "stand-aside condition",
            ],
        },
    }