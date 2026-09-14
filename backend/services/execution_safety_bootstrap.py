"""Takeover execution-safety bootstrap.

This module applies defence-in-depth patches at package import time without
changing strategy thresholds or enabling execution. The goal is simple:
capital-protection and execution-authorisation failures must fail CLOSED.

The patches are intentionally narrow and reversible:
- portfolio governor exceptions become explicit rejections;
- strategist reservations are cleaned up if enqueue refuses/fails after reserve;
- the legacy autonomous executor is disabled during the takeover period;
- direct/local MT5 order placement is disabled so executable authority stays on
  the audited PendingExecution -> hardened bridge path.

The authoritative Mandate Strategist and PREDATOR signal/research logic are not
changed here. Only unsafe failure behaviour is constrained.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Callable

log = logging.getLogger(__name__)


def _conservative_snapshot(reason: str) -> dict[str, Any]:
    """Return a snapshot shape that downstream logging can safely consume."""
    return {
        "predator_lots": None,
        "strategist_lots": None,
        "reserved_lots": None,
        "mt5_actual_lots": None,
        "mt5_authoritative": False,
        "mt5_reason": reason,
        "db_gross": None,
        "committed_gross": None,
        "max_gross": 0.15,
        "remaining_gross": 0.0,
        "mt5_mismatch": True,
        "state_unknown": True,
        "active_reservations": None,
        "within_limit": False,
        "safety_error": reason,
    }


def fail_closed_reserve_wrapper(original: Callable) -> Callable:
    """Wrap reserve_capacity() so any unexpected exception rejects the order."""
    def wrapped(*args, **kwargs):
        try:
            return original(*args, **kwargs)
        except Exception as exc:  # execution safety boundary
            reason = f"GOVERNOR_INTERNAL_ERROR:{type(exc).__name__}"
            log.exception("[takeover-safety] reserve_capacity failed CLOSED: %s", exc)
            return False, reason, _conservative_snapshot(str(exc)), None
    wrapped.__name__ = getattr(original, "__name__", "reserve_capacity")
    wrapped.__doc__ = getattr(original, "__doc__", None)
    setattr(wrapped, "_takeover_fail_closed", True)
    return wrapped


def fail_closed_check_wrapper(original: Callable) -> Callable:
    """Wrap check_new_order() so any unexpected exception rejects the order."""
    def wrapped(*args, **kwargs):
        try:
            return original(*args, **kwargs)
        except Exception as exc:  # execution safety boundary
            reason = f"GOVERNOR_INTERNAL_ERROR:{type(exc).__name__}"
            log.exception("[takeover-safety] check_new_order failed CLOSED: %s", exc)
            return False, reason, _conservative_snapshot(str(exc))
    wrapped.__name__ = getattr(original, "__name__", "check_new_order")
    wrapped.__doc__ = getattr(original, "__doc__", None)
    setattr(wrapped, "_takeover_fail_closed", True)
    return wrapped


def strategist_enqueue_cleanup_wrapper(original: Callable, gov: Any) -> Callable:
    """Release new Strategist RESERVED capacity whenever enqueue does not succeed.

    `strategist_runner._maybe_enqueue_demo_order()` reserves global capacity
    before several later safety/config checks. If one of those checks refuses
    the order, this wrapper ensures a newly-created RESERVED entry is released
    immediately rather than waiting for the governor TTL/prune cycle.

    SENT reservations are never touched here.
    """
    def _reserved_ids() -> set[str]:
        try:
            with gov._GOVERNOR_LOCK:
                return {
                    rid for rid, row in gov._RESERVATIONS.items()
                    if len(row) >= 4 and row[0] == "STRATEGIST" and row[3] == "RESERVED"
                }
        except Exception:
            return set()

    def wrapped(db, verdict):
        before = _reserved_ids()
        try:
            result = original(db, verdict)
        except Exception as exc:
            log.exception("[takeover-safety] strategist enqueue raised; failing closed: %s", exc)
            result = None
        if result is None:
            leaked = _reserved_ids() - before
            for rid in leaked:
                try:
                    gov.release_reservation(rid, "post_reservation_refusal")
                    log.warning("[takeover-safety] released stranded Strategist reservation %s", rid[:8])
                except Exception as exc:
                    log.exception("[takeover-safety] reservation cleanup failed rid=%s: %s", rid[:8], exc)
        return result

    wrapped.__name__ = getattr(original, "__name__", "_maybe_enqueue_demo_order")
    wrapped.__doc__ = getattr(original, "__doc__", None)
    setattr(wrapped, "_takeover_reservation_cleanup", True)
    return wrapped


def disabled_legacy_executor_wrapper(original: Callable, attempt_cls: type) -> Callable:
    """Disable the legacy live executor while the Mandate Strategist is canonical.

    The legacy executor contains historical fail-open enrichment paths. Rather
    than silently trusting them, takeover policy blocks the executable entry
    point completely. Its dry-run/preview and analytics code remain available.
    """
    def wrapped(db):
        att = attempt_cls(ts=datetime.now(timezone.utc).isoformat())
        att.attempted = False
        att.fired = False
        att.blocking_layer = "takeover_safety"
        att.blocking_reason = (
            "LEGACY_AUTO_EXECUTOR_DISABLED — authoritative execution is the "
            "Mandate Strategist/PREDATOR architecture; legacy path remains "
            "available for dry-run research only."
        )
        return att
    wrapped.__name__ = getattr(original, "__name__", "evaluate_and_execute")
    wrapped.__doc__ = getattr(original, "__doc__", None)
    setattr(wrapped, "_takeover_disabled", True)
    return wrapped


def disabled_local_mt5_wrapper(original: Callable, safety_error_cls: type[Exception]) -> Callable:
    """Block the historical direct/local MT5 `order_send` path.

    During takeover all executable authority is intentionally funnelled through
    `PendingExecution -> hardened Windows bridge`, where the active demo account
    is re-verified immediately before execution. The direct `/mt5/demo-order`
    path and legacy local caller therefore remain disabled.
    """
    def wrapped(*args, **kwargs):
        raise safety_error_cls(
            "DIRECT_LOCAL_MT5_EXECUTION_DISABLED — takeover policy requires "
            "PendingExecution -> hardened MT5 bridge execution."
        )
    wrapped.__name__ = getattr(original, "__name__", "place_demo_market_order")
    wrapped.__doc__ = getattr(original, "__doc__", None)
    setattr(wrapped, "_takeover_disabled", True)
    return wrapped


def install_safety_patches() -> dict[str, bool]:
    """Install idempotent runtime safety patches and return applied-state flags."""
    state = {
        "governor": False,
        "strategist_cleanup": False,
        "legacy_executor": False,
        "direct_local_mt5": False,
    }
    gov = None

    try:
        from services import portfolio_governor as gov

        if not getattr(gov.reserve_capacity, "_takeover_fail_closed", False):
            gov.reserve_capacity = fail_closed_reserve_wrapper(gov.reserve_capacity)
        if not getattr(gov.check_new_order, "_takeover_fail_closed", False):
            gov.check_new_order = fail_closed_check_wrapper(gov.check_new_order)
        state["governor"] = True
    except Exception as exc:
        # Package startup must remain observable, but failure to install this
        # patch is serious and should be loud in logs.
        log.exception("[takeover-safety] governor patch install failed: %s", exc)

    if gov is not None:
        try:
            from services import strategist_runner as strategist

            if not getattr(strategist._maybe_enqueue_demo_order, "_takeover_reservation_cleanup", False):
                strategist._maybe_enqueue_demo_order = strategist_enqueue_cleanup_wrapper(
                    strategist._maybe_enqueue_demo_order,
                    gov,
                )
            state["strategist_cleanup"] = True
        except Exception as exc:
            log.exception("[takeover-safety] strategist reservation cleanup patch failed: %s", exc)

    try:
        from services import auto_executor as legacy

        if not getattr(legacy.evaluate_and_execute, "_takeover_disabled", False):
            legacy.evaluate_and_execute = disabled_legacy_executor_wrapper(
                legacy.evaluate_and_execute,
                legacy.ExecutionAttempt,
            )
        state["legacy_executor"] = True
    except Exception as exc:
        log.exception("[takeover-safety] legacy executor patch install failed: %s", exc)

    try:
        from services import mt5_provider as mt5_provider

        if not getattr(mt5_provider.place_demo_market_order, "_takeover_disabled", False):
            mt5_provider.place_demo_market_order = disabled_local_mt5_wrapper(
                mt5_provider.place_demo_market_order,
                mt5_provider.MT5SafetyError,
            )
        state["direct_local_mt5"] = True
    except Exception as exc:
        log.exception("[takeover-safety] direct/local MT5 patch install failed: %s", exc)

    return state
