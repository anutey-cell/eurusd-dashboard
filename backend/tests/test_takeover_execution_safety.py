"""Regression tests for ChatGPT takeover execution-safety hardening."""
from __future__ import annotations

from dataclasses import dataclass
import threading

import services
from services.execution_safety_bootstrap import (
    disabled_legacy_executor_wrapper,
    fail_closed_check_wrapper,
    fail_closed_reserve_wrapper,
    strategist_enqueue_cleanup_wrapper,
)


def test_service_package_installs_takeover_safety_policy():
    state = services.TAKEOVER_SAFETY_STATE
    assert state["governor"] is True
    assert state["strategist_cleanup"] is True
    assert state["legacy_executor"] is True

    from services import portfolio_governor as gov
    from services import strategist_runner as strategist
    from services import auto_executor as legacy

    assert getattr(gov.reserve_capacity, "_takeover_fail_closed", False) is True
    assert getattr(gov.check_new_order, "_takeover_fail_closed", False) is True
    assert getattr(strategist._maybe_enqueue_demo_order, "_takeover_reservation_cleanup", False) is True
    assert getattr(legacy.evaluate_and_execute, "_takeover_disabled", False) is True


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


class FakeGovernor:
    def __init__(self):
        self._GOVERNOR_LOCK = threading.RLock()
        self._RESERVATIONS = {}
        self.released = []

    def release_reservation(self, rid, reason):
        self.released.append((rid, reason))
        self._RESERVATIONS.pop(rid, None)


def test_strategist_cleanup_releases_new_reserved_capacity_on_refusal():
    gov = FakeGovernor()

    def refusing_enqueue(db, verdict):
        gov._RESERVATIONS["new-rid"] = ["STRATEGIST", "BUY", 0.01, "RESERVED", 9999999999, None]
        return None

    wrapped = strategist_enqueue_cleanup_wrapper(refusing_enqueue, gov)
    result = wrapped(None, {"decision": "BUY"})
    assert result is None
    assert gov.released == [("new-rid", "post_reservation_refusal")]
    assert "new-rid" not in gov._RESERVATIONS


def test_strategist_cleanup_never_releases_sent_capacity():
    gov = FakeGovernor()

    def successful_enqueue(db, verdict):
        gov._RESERVATIONS["sent-rid"] = ["STRATEGIST", "BUY", 0.01, "SENT", 9999999999, 123]
        return 77

    wrapped = strategist_enqueue_cleanup_wrapper(successful_enqueue, gov)
    result = wrapped(None, {"decision": "BUY"})
    assert result == 77
    assert gov.released == []
    assert "sent-rid" in gov._RESERVATIONS


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
