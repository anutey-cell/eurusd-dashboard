"""Regression tests for operator-visible market-data health."""
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from routers import health as health_router


def _market(status: str) -> dict:
    return {
        "status": status,
        "data_quality_score": 100 if status == "fresh" else 40,
        "stale_timeframes": [] if status == "fresh" else ["M5"],
        "fresh_timeframes": ["M5", "M15", "H1", "H4", "D1"] if status == "fresh" else ["H1"],
        "active_provider": "tradingview",
        "last_bar_time": "2026-09-16T01:55:00+00:00",
        "provider_by_timeframe": {},
        "last_ingest_error": {},
        "tradingview_enabled": True,
        "weekend": False,
    }


def test_health_is_ok_when_market_data_is_fresh(monkeypatch):
    monkeypatch.setattr(health_router, "_db_status", lambda: "connected")
    monkeypatch.setattr(health_router, "_market_data_status", lambda: _market("fresh"))

    result = health_router.health_check()

    assert result.status == "ok"
    assert result.market_data["status"] == "fresh"
    assert result.market_data["active_provider"] == "tradingview"


def test_health_is_degraded_when_market_data_is_stale(monkeypatch):
    monkeypatch.setattr(health_router, "_db_status", lambda: "connected")
    monkeypatch.setattr(health_router, "_market_data_status", lambda: _market("stale"))

    result = health_router.health_check()

    assert result.status == "degraded"
    assert result.market_data["stale_timeframes"] == ["M5"]


def test_health_is_error_when_database_is_down(monkeypatch):
    monkeypatch.setattr(health_router, "_db_status", lambda: "error")

    result = health_router.health_check()

    assert result.status == "error"
    assert result.market_data["status"] == "unknown"
