#!/usr/bin/env python3
"""Hardened launcher for the existing MT5 bridge daemon.

This launcher keeps existing bridge data/reconciliation behaviour but adds:
1. independent local demo-account/terminal verification immediately before
   every execution attempt; and
2. atomic server-side order claiming via `/bridge/claim-v2/{id}` so racing
   daemons cannot both execute the same PendingExecution row.

Operational cutover: point the Windows Scheduled Task/NSSM service at this file
instead of `mt5_bridge_daemon.py` after validating in demo mode.
"""
from __future__ import annotations

import logging

import mt5_bridge_daemon as legacy
from bridge_safety import verify_demo_account, verify_terminal

log = logging.getLogger("mt5_bridge_hardened")
_ORIGINAL_EXECUTE_ORDER = legacy.execute_order


def claim_atomic(order_id: int) -> bool:
    """Claim through the compare-and-set v2 endpoint; any ambiguity blocks."""
    try:
        r = legacy.session.post(legacy.api(f"/claim-v2/{order_id}"), timeout=10)
        if r.status_code == 200:
            return True
        if r.status_code == 409:
            log.warning("atomic claim %d refused (already claimed/expired): %s",
                        order_id, (r.text or "")[:160])
            return False
        log.error("atomic claim %d HTTP %d: %s", order_id, r.status_code,
                  (r.text or "")[:160])
        return False
    except Exception as exc:
        log.error("atomic claim %d failed CLOSED: %s", order_id, exc)
        return False


def execute_order_fail_closed(order: dict) -> dict:
    """Re-verify the active MT5 terminal/account immediately before execution."""
    try:
        account = legacy.mt5.account_info()
        terminal = legacy.mt5.terminal_info()
    except Exception as exc:
        log.error("Pre-order MT5 state read failed — REFUSED: %s", exc)
        return {"status": "REJECTED", "error": f"local safety guard: MT5 state read failed ({type(exc).__name__})"}

    account_guard = verify_demo_account(
        account,
        expected_login=legacy.MT5_LOGIN,
        expected_server=legacy.MT5_SERVER,
        require_demo_trade_mode=True,
    )
    if not account_guard.ok:
        log.critical("LOCAL DEMO GUARD BLOCK — %s", account_guard.reason)
        return {"status": "REJECTED", "error": f"local demo guard: {account_guard.reason}"}

    terminal_guard = verify_terminal(terminal)
    if not terminal_guard.ok:
        log.critical("LOCAL TERMINAL GUARD BLOCK — %s", terminal_guard.reason)
        return {"status": "REJECTED", "error": f"local terminal guard: {terminal_guard.reason}"}

    # Optional final symbol sanity check before handing off to the existing
    # executor. Broker suffixes are allowed; the resolved name must still be
    # an XAU/USD instrument.
    try:
        symbol = legacy._resolve_broker_symbol()
    except Exception as exc:
        return {"status": "REJECTED", "error": f"local symbol guard: resolver failed ({type(exc).__name__})"}
    canonical = (symbol or "").upper().replace("/", "").replace(".", "").replace("_", "").replace("-", "")
    if "XAU" not in canonical or "USD" not in canonical:
        log.critical("LOCAL SYMBOL GUARD BLOCK — resolved=%r", symbol)
        return {"status": "REJECTED", "error": f"local symbol guard: resolved symbol {symbol!r} is not XAU/USD"}

    return _ORIGINAL_EXECUTE_ORDER(order)


# Monkey-patch only executable bridge boundaries. Candle/tick research,
# heartbeat, reconciliation and monitoring behaviour remain unchanged.
legacy.claim = claim_atomic
legacy.execute_order = execute_order_fail_closed


if __name__ == "__main__":
    log.warning("Starting HARDENED MT5 bridge — atomic claim + local demo guard ACTIVE")
    legacy.main()
