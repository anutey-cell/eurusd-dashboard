from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Callable

from fastapi import APIRouter

router = APIRouter(prefix="/overview", tags=["overview"])


def safe_call(name: str, fn: Callable[[], Any]) -> dict[str, Any]:
    """
    Prevent one failing engine component from breaking the whole overview endpoint.
    """
    try:
        return {
            "status": "ok",
            "data": fn(),
            "error": None,
        }
    except Exception as exc:
        return {
            "status": "error",
            "data": None,
            "error": f"{name}: {type(exc).__name__}: {exc}",
        }


def try_import(path: str, attr: str):
    """
    Soft import helper so this endpoint remains resilient even if a service name changes.
    """
    try:
        module = __import__(path, fromlist=[attr])
        return getattr(module, attr)
    except Exception:
        return None


@router.get("/daily", summary="Daily XAU/USD institutional overview")
def daily_overview() -> dict[str, Any]:
    """
    Aggregated daily overview for ChatGPT / analyst review.

    This endpoint intentionally pulls from the existing engine layers where possible:
    candles, scan, signal, kill zones, calendar risk, bridge status, and MT5 status.
    If a component fails, the endpoint still returns the rest of the overview.
    """

    # --- Soft-load existing service functions/classes if available ---
    # These names may need adjustment depending on your actual service function names.
    # The safe fallback keeps the endpoint alive even before deeper integration.

    # Candle/live data
    get_live_snapshot = (
        try_import("services.live_feed", "get_live_snapshot")
        or try_import("services.candle_provider", "get_live_snapshot")
        or try_import("services.candle_provider", "get_candle_snapshot")
    )

    # Institutional scanner
    run_scan = (
        try_import("services.institutional_scanner", "run_scan")
        or try_import("services.institutional_scanner", "run_fresh_scan")
        or try_import("services.institutional_scanner", "scan_xauusd")
    )

    # Signal engine
    get_latest_signal = (
        try_import("services.signal_engine", "get_latest_signal")
        or try_import("services.dual_engine_runner", "get_latest_signal")
        or try_import("services.alert_service", "get_latest_signal")
    )

    # Kill zones
    get_killzone_status = (
        try_import("services.killzone_analyzer", "get_killzone_status")
        or try_import("services.killzone_policy", "get_killzone_status")
        or try_import("services.killzone_analyzer", "analyze_current_killzone")
    )

    # Calendar/news risk
    get_calendar_risk = (
        try_import("services.calendar_provider", "get_calendar_risk")
        or try_import("services.calendar_provider", "get_today_calendar")
        or try_import("services.myfxbook_provider", "get_calendar_risk")
    )

    # Bridge status
    get_bridge_status = (
        try_import("services.broker_provider", "get_bridge_status")
        or try_import("services.auto_executor", "get_bridge_status")
    )

    # MT5 status
    get_mt5_status = (
        try_import("services.mt5_provider", "get_status")
        or try_import("services.mt5_provider", "get_mt5_status")
    )

    overview = {
        "status": "ok",
        "instrument": "XAU/USD",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "candles": safe_call(
            "candles",
            lambda: get_live_snapshot() if get_live_snapshot else {
                "status": "not_wired",
                "message": "No live candle snapshot function detected yet.",
            },
        ),
        "latest_scan": safe_call(
            "latest_scan",
            lambda: run_scan() if run_scan else {
                "status": "not_wired",
                "message": "No scanner function detected yet.",
            },
        ),
        "latest_signal": safe_call(
            "latest_signal",
            lambda: get_latest_signal() if get_latest_signal else {
                "status": "not_wired",
                "message": "No latest signal function detected yet.",
            },
        ),
        "killzones": safe_call(
            "killzones",
            lambda: get_killzone_status() if get_killzone_status else {
                "status": "not_wired",
                "message": "No kill-zone status function detected yet.",
            },
        ),
        "calendar_risk": safe_call(
            "calendar_risk",
            lambda: get_calendar_risk() if get_calendar_risk else {
                "status": "not_wired",
                "message": "No calendar risk function detected yet.",
            },
        ),
        "bridge_status": safe_call(
            "bridge_status",
            lambda: get_bridge_status() if get_bridge_status else {
                "status": "not_wired",
                "message": "No bridge status function detected yet.",
            },
        ),
        "mt5_status": safe_call(
            "mt5_status",
            lambda: get_mt5_status() if get_mt5_status else {
                "status": "not_wired",
                "message": "No MT5 status function detected yet.",
            },
        ),
        "daily_analysis_hint": (
            "Use candles, scan, signal, killzones, calendar_risk, bridge_status, "
            "and mt5_status to produce daily XAU/USD institutional overview. "
            "If any component is not_wired, inspect the matching router/service and connect the correct function."
        ),
    }

    return overview