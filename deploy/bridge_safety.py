"""Pure safety checks for the Windows MT5 bridge.

Kept free of MetaTrader5 imports so it can be unit-tested on Linux/CI.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional


@dataclass(frozen=True)
class BridgeGuardResult:
    ok: bool
    reason: str


def verify_demo_account(
    account: Any,
    *,
    expected_login: str | int | None,
    expected_server: str | None,
    require_demo_trade_mode: bool = True,
) -> BridgeGuardResult:
    """Validate the currently connected MT5 account immediately pre-order.

    `trade_mode == 0` is MT5 demo mode. The login and server checks bind the
    daemon to the same configured account it was launched to use.
    """
    if account is None:
        return BridgeGuardResult(False, "account_info unavailable")

    if expected_login in (None, ""):
        return BridgeGuardResult(False, "MT5_LOGIN not configured; cannot verify sanctioned account")

    try:
        active_login = int(getattr(account, "login"))
        wanted_login = int(expected_login)
    except Exception:
        return BridgeGuardResult(False, "account login unavailable/unparseable")
    if active_login != wanted_login:
        return BridgeGuardResult(False, f"active login {active_login} != configured login {wanted_login}")

    active_server = str(getattr(account, "server", "") or "")
    wanted_server = str(expected_server or "")
    if not wanted_server:
        return BridgeGuardResult(False, "MT5_SERVER not configured; cannot verify sanctioned server")
    if active_server.lower() != wanted_server.lower():
        return BridgeGuardResult(False, f"active server {active_server!r} != configured server {wanted_server!r}")

    if require_demo_trade_mode:
        try:
            trade_mode = int(getattr(account, "trade_mode"))
        except Exception:
            return BridgeGuardResult(False, "account trade_mode unavailable")
        if trade_mode != 0:
            return BridgeGuardResult(False, f"active account trade_mode={trade_mode}; DEMO(0) required")

    return BridgeGuardResult(True, "ok")


def verify_terminal(terminal: Optional[Any]) -> BridgeGuardResult:
    """Require a connected terminal with trading permission before order_send."""
    if terminal is None:
        return BridgeGuardResult(False, "terminal_info unavailable")
    if not bool(getattr(terminal, "connected", False)):
        return BridgeGuardResult(False, "MT5 terminal not connected")
    if not bool(getattr(terminal, "trade_allowed", False)):
        return BridgeGuardResult(False, "MT5 terminal trade_allowed=false")
    return BridgeGuardResult(True, "ok")
