"""
Broker execution provider — disabled skeleton.

All functions in this module are intentionally inert until a licensed broker
integration is implemented and the operator enables execution via config.

Kill-switch state is stored in-process only. A process restart resets it to
the config default (always disabled). Persistent kill-switch state requires
an external store (DB flag, Redis, etc.) — not implemented here.

Safety note:
  Execution can only be enabled responsibly after a minimum of:
    - 100 backtested setups with positive expectancy
    - 50 paper-traded signals reviewed manually
    - Acceptable maximum drawdown in both IS and OOS backtests
  Set BROKER_EXECUTION_ENABLED=true in .env only when the above are met.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

# ── Kill-switch state ─────────────────────────────────────────────────────────
# In-process flag; overridden to False by the kill-switch endpoint.
# Intentionally does not survive restarts (safe default).
_kill_switch_active: bool = False
_kill_switch_triggered_at: datetime | None = None


def _execution_guard() -> None:
    """Raise if execution is disabled or kill-switch is active."""
    from config import settings

    if _kill_switch_active:
        raise RuntimeError(
            f"Kill switch active since {_kill_switch_triggered_at.isoformat()}. "
            "Restart the server and manually re-enable execution to resume."
        )
    if not settings.broker_execution_enabled:
        raise RuntimeError(
            "Broker execution is disabled. Manual confirmation mode only. "
            "Set BROKER_EXECUTION_ENABLED=true in .env to enable."
        )


# ── Public API ────────────────────────────────────────────────────────────────

def check_execution_enabled() -> dict:
    """
    Return the current execution state without raising.
    Always safe to call.
    """
    from config import settings
    return {
        "enabled":          settings.broker_execution_enabled and not _kill_switch_active,
        "configEnabled":    settings.broker_execution_enabled,
        "killSwitchActive": _kill_switch_active,
        "killSwitchAt":     _kill_switch_triggered_at.isoformat() if _kill_switch_triggered_at else None,
        "provider":         settings.broker_provider,
        "note": (
            "Kill switch active — all execution blocked." if _kill_switch_active
            else "Execution disabled via config." if not settings.broker_execution_enabled
            else "Execution enabled."
        ),
    }


def trigger_kill_switch() -> dict:
    """
    Immediately disable all execution.  Cannot be reversed without a restart.
    """
    global _kill_switch_active, _kill_switch_triggered_at
    _kill_switch_active = True
    _kill_switch_triggered_at = datetime.now(timezone.utc)
    logger.critical("KILL SWITCH TRIGGERED at %s", _kill_switch_triggered_at.isoformat())
    return {
        "killSwitchActive": True,
        "triggeredAt":      _kill_switch_triggered_at.isoformat(),
        "message":          "All broker execution blocked. Restart required to reset.",
    }


def get_account_summary() -> dict:
    """
    Return live account balance, margin, and open P&L from the broker.
    Placeholder — requires broker SDK integration.
    """
    _execution_guard()
    # TODO: implement via broker SDK (e.g. oandapyV20.AccountSummary)
    raise NotImplementedError(
        "get_account_summary() is not yet implemented. "
        "Integrate your broker SDK here."
    )


def check_spread(pair: str) -> dict:
    """
    Fetch the live bid/ask spread for `pair` and return pip spread + acceptability.
    Placeholder — requires broker SDK integration.
    """
    _execution_guard()
    # TODO: stream live quote and compute (ask - bid) / pip_size
    raise NotImplementedError(
        f"check_spread({pair!r}) is not yet implemented. "
        "Integrate your broker SDK here."
    )


def calculate_position_size(
    account_balance: float,
    risk_percent: float,
    stop_loss_pips: float,
) -> dict:
    """
    Return lot size for the given risk parameters.

    Uses: units = (account_balance * risk_percent/100) / (stop_loss_pips * pip_value)
    pip_value is broker- and pair-specific — placeholder returns formula only.
    """
    _execution_guard()
    # EUR/USD reference: 1 standard lot = $10/pip at 100k units
    # Actual value depends on account currency and current price — implement via broker API.
    raise NotImplementedError(
        "calculate_position_size() is not yet implemented. "
        "Requires live pip-value lookup from broker."
    )


def place_market_order(
    pair: str,
    side: str,
    units: int,
    stop_loss: float,
    take_profit: float,
) -> dict:
    """
    Submit a market order with attached SL and TP.
    Placeholder — requires broker SDK integration.

    Args:
      pair        e.g. "EUR/USD"
      side        "BUY" | "SELL"
      units       position size in units (not lots)
      stop_loss   absolute price level
      take_profit absolute price level
    """
    _execution_guard()
    # TODO: implement via broker SDK
    # Example (OANDA): oandapyV20.contrib.orders.MarketOrder(...)
    raise NotImplementedError(
        "place_market_order() is not yet implemented. "
        "Integrate your broker SDK here."
    )


def cancel_order(order_id: str) -> dict:
    """
    Cancel a pending order by ID.
    Placeholder — requires broker SDK integration.
    """
    _execution_guard()
    raise NotImplementedError(
        f"cancel_order({order_id!r}) is not yet implemented. "
        "Integrate your broker SDK here."
    )


def close_position(position_id: str) -> dict:
    """
    Close an open position by ID at market.
    Placeholder — requires broker SDK integration.
    """
    _execution_guard()
    raise NotImplementedError(
        f"close_position({position_id!r}) is not yet implemented. "
        "Integrate your broker SDK here."
    )
