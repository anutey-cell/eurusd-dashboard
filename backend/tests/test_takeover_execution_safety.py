"""Regression tests for ChatGPT takeover execution-safety hardening."""
from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace

from services.execution_safety_bootstrap import (
    disabled_legacy_executor_wrapper,
    fail_closed_check_wrapper,
    fail_closed_reserve_wrapper,
)


def test_reserve_exception_fails_closed():
    def boom(*args, **kwargs):
        raise RuntimeError("db unavailable")

    allowed, reason, snap, rid = fail_closed_reserve_wrapper(boom)(None)
    assert allowed is False
    assert reason.startswith("GOVERNOR_INTERNAL_ERROR")
    assert rid is None
    assert snap["within_limit"] is False
    assert snap["state_unknown"] is True
    assert snap["remaining_gross"] == 0.0


def test_non_reserving_governor_exception_fails_closed():
    def boom(*args, **kwargs):
        raise ValueError("heartbeat parse failed")

    allowed, reason, snap = fail_closed_check_wrapper(boom)(None)
    assert allowed is False
    assert reason.startswith("GOVERNOR_INTERNAL_ERROR")
    assert snap["mt5_authoritative"] is False
    assert snap["within_limit"] is False


@dataclass
class FakeAttempt:
    ts: str = ""
    attempted: bool = True
    fired: bool = True
    blocking_layer: str | None = None
    blocking_reason: str | None = None


def test_legacy_executor_is_blocked_before_original_can_run():
    called = {"value": False}

    def original(db):
        called["value"] = True
        raise AssertionError("legacy executable path must not run")

    wrapped = disabled_legacy_executor_wrapper(original, FakeAttempt)
    result = wrapped(None)
    assert called["value"] is False
    assert result.attempted is False
    assert result.fired is False
    assert result.blocking_layer == "takeover_safety"
    assert "LEGACY_AUTO_EXECUTOR_DISABLED" in result.blocking_reason
