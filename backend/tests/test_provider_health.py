from __future__ import annotations

from services.provider_health import (
    note_failure,
    note_success,
    provider_health_snapshot,
    reset_provider_health,
    should_attempt,
)


def setup_function():
    reset_provider_health()


def teardown_function():
    reset_provider_health()


def test_auth_failure_opens_provider_wide_circuit():
    note_failure(
        "twelvedata",
        "M5",
        "Twelve Data: Invalid or expired API key",
        now_ts=1000.0,
    )

    ok_m5, reason_m5 = should_attempt("twelvedata", "M5", now_ts=1001.0)
    ok_h1, reason_h1 = should_attempt("twelvedata", "H1", now_ts=1001.0)

    assert ok_m5 is False
    assert ok_h1 is False
    assert "category=auth" in reason_m5
    assert "category=auth" in reason_h1

    snap = provider_health_snapshot(now_ts=1001.0)
    assert snap["providers"]["twelvedata:*"]["circuit_open"] is True
    assert snap["providers"]["twelvedata:*"]["last_failure_category"] == "auth"


def test_transient_failure_is_timeframe_scoped():
    note_failure("tradingview", "M5", "temporary websocket timeout", now_ts=2000.0)

    ok_m5, _ = should_attempt("tradingview", "M5", now_ts=2001.0)
    ok_h1, _ = should_attempt("tradingview", "H1", now_ts=2001.0)

    assert ok_m5 is False
    assert ok_h1 is True


def test_success_closes_timeframe_circuit():
    note_failure("tradingview", "M15", "temporary timeout", now_ts=3000.0)
    note_success("tradingview", "M15")

    ok, reason = should_attempt("tradingview", "M15", now_ts=3001.0)
    assert ok is True
    assert reason == "available"
