#!/usr/bin/env python3
"""Read-only post-deploy verification for takeover execution hardening.

Safe to run inside the backend container. Exits non-zero if the takeover
safety bootstrap is not installed or if obviously dangerous generic/live
execution switches are enabled unexpectedly.

This script does not query or mutate trading tables and never places orders.
"""
from __future__ import annotations

import json
import sys

import services
from config import settings


def main() -> int:
    patch_state = dict(getattr(services, "TAKEOVER_SAFETY_STATE", {}) or {})
    expected_patches = {
        "governor": True,
        "strategist_cleanup": True,
        "legacy_executor": True,
    }

    switches = {
        "broker_execution_enabled": bool(getattr(settings, "broker_execution_enabled", False)),
        "live_trading_authorized": bool(getattr(settings, "live_trading_authorized", False)),
        "use_mandate_strategist": bool(getattr(settings, "use_mandate_strategist", True)),
        "predator_execution_enabled": bool(getattr(settings, "predator_execution_enabled", False)),
        "mt5_bridge_enabled": bool(getattr(settings, "mt5_bridge_enabled", False)),
        "allow_demo_trading": bool(getattr(settings, "allow_demo_trading", False)),
    }

    failures: list[str] = []
    for key, expected in expected_patches.items():
        if patch_state.get(key) is not expected:
            failures.append(f"takeover patch {key} expected {expected}, got {patch_state.get(key)!r}")

    # Takeover policy: generic/live account execution must stay off. Demo bridge
    # may legitimately be enabled, so it is reported but not treated as failure.
    if switches["broker_execution_enabled"]:
        failures.append("BROKER_EXECUTION_ENABLED is true")
    if switches["live_trading_authorized"]:
        failures.append("LIVE_TRADING_AUTHORIZED is true")
    if not switches["use_mandate_strategist"]:
        failures.append("USE_MANDATE_STRATEGIST is false")

    report = {
        "ok": not failures,
        "takeover_safety_state": patch_state,
        "execution_switches": switches,
        "failures": failures,
    }
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if not failures else 2


if __name__ == "__main__":
    raise SystemExit(main())
