"""
MT5 demo integration service.

MetaTrader5 Python package is Windows-only. On Linux/Docker this module
degrades gracefully — all public functions raise MT5UnavailableError.

Security rules:
  - mt5_password is NEVER logged, printed, or returned in any response.
  - mt5_login appears in logs ONLY as "***{last4}" format.
  - Credentials are read from config (which reads .env). Never hardcoded.

Demo-only constraints:
  - MT5_MODE must be "demo" (account.trade_mode == 0) to allow any order.
  - MT5_EXECUTION_ENABLED and ALLOW_DEMO_TRADING must BOTH be true.
  - No live account execution is possible by design.
"""
from __future__ import annotations

import json
import logging
import math
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)

# ── Optional MT5 import (Windows-only package) ────────────────────────────────
try:
    import MetaTrader5 as mt5        # type: ignore[import]
    _MT5_AVAILABLE = True
except ImportError:
    mt5 = None                       # type: ignore[assignment]
    _MT5_AVAILABLE = False

# ── Symbol candidates per internal pair code ──────────────────────────────────
# XAU/USD is the only supported instrument — EUR/USD candidates removed
SYMBOL_CANDIDATES: dict[str, list[str]] = {
    "xauusd": ["XAUUSD", "XAUUSDm", "GOLD", "XAUUSD.raw", "XAUUSD.", "XAUUSD_"],
}

# ── Module-level symbol cache (resolved once per session) ─────────────────────
_resolved_symbols: dict[str, str] = {}


# ── Exceptions ────────────────────────────────────────────────────────────────

class MT5UnavailableError(RuntimeError):
    """Raised when the MetaTrader5 package is not installed (non-Windows platform)."""

class MT5NotConnectedError(RuntimeError):
    """Raised when MT5 is installed but not initialised/connected."""

class MT5SafetyError(ValueError):
    """Raised when a demo order fails a safety gate."""


# ── Internal helpers ──────────────────────────────────────────────────────────

def _require_mt5() -> None:
    if not _MT5_AVAILABLE:
        raise MT5UnavailableError(
            "MetaTrader5 package not available. "
            "It is Windows-only. Run: pip install MetaTrader5 on Windows with MT5 client installed."
        )


def _require_connected() -> None:
    _require_mt5()
    if mt5.terminal_info() is None:
        raise MT5NotConnectedError(
            "MT5 terminal not connected. Call initialize_mt5() first, "
            "or set MT5_ENABLED=true in .env."
        )


def _mask_login(login) -> str:
    """Return login number with all-but-last-4 digits masked. Accepts int or str."""
    s = str(login).strip()
    if not s or s == "0":
        return "***"
    return "***" + s[-4:] if len(s) > 4 else "***" + s


def _login_as_int() -> int:
    """Return mt5_login as int (config stores it as str to accept placeholder text)."""
    from config import settings
    try:
        return int(settings.mt5_login)
    except (ValueError, TypeError):
        return 0


def _log_trade(
    *,
    mode: str,
    pair: str,
    broker_symbol: str | None,
    signal: str,
    order_type: str,
    volume: float,
    entry: float | None,
    stop_loss: float | None,
    take_profit: float | None,
    risk_percent: float | None,
    risk_amount: float | None,
    spread: float | None,
    ticket: int | None,
    status: str,
    rejection_reason: str | None,
    reason: str | None,
    raw_response: dict | None,
) -> None:
    """Persist a trade attempt (accepted, rejected, or failed) to the database."""
    from database import db_session
    from db_models import MT5TradeLog

    raw_json = json.dumps(raw_response) if raw_response else None
    try:
        with db_session() as db:
            record = MT5TradeLog(
                mode=mode,
                pair=pair,
                broker_symbol=broker_symbol,
                signal=signal,
                order_type=order_type,
                volume=volume,
                entry=entry,
                stop_loss=stop_loss,
                take_profit=take_profit,
                risk_percent=risk_percent,
                risk_amount=risk_amount,
                spread=spread,
                ticket=ticket,
                status=status,
                rejection_reason=rejection_reason,
                reason=reason,
                raw_response_json=raw_json,
            )
            db.add(record)
    except Exception as exc:
        logger.error("Failed to persist MT5 trade log: %s", exc)


# ── Public API ────────────────────────────────────────────────────────────────

def initialize_mt5() -> bool:
    """
    Initialise the MT5 connection using credentials from .env.
    Returns True on success, False on failure.
    Password is NEVER logged.
    """
    _require_mt5()
    from config import settings

    if not settings.mt5_enabled:
        logger.info("MT5 disabled (MT5_ENABLED=false)")
        return False

    # password intentionally omitted from logs
    logger.info(
        "MT5 init: account=%s server=%s",
        _mask_login(settings.mt5_login), settings.mt5_server,
    )

    ok = mt5.initialize(
        server=settings.mt5_server,
        login=_login_as_int(),             # convert str→int; password intentionally omitted from logs
        password=settings.mt5_password,   # NEVER logged
        timeout=10_000,
    )

    if not ok:
        err = mt5.last_error()
        logger.error("MT5 init failed: code=%s msg=%s", err[0], err[1])
        return False

    info = mt5.account_info()
    trade_mode = getattr(info, "trade_mode", -1) if info else -1
    mode_label = "demo" if trade_mode == 0 else "live"
    logger.info(
        "MT5 connected: account=%s server=%s mode=%s balance=%.2f",
        _mask_login(settings.mt5_login),
        getattr(info, "server", settings.mt5_server),
        mode_label,
        getattr(info, "balance", 0.0),
    )
    return True


def shutdown_mt5() -> None:
    """Shut down the MT5 connection."""
    _require_mt5()
    mt5.shutdown()
    logger.info("MT5 connection closed")


def get_account_info() -> dict:
    """
    Return account info with login masked. Password is never included.
    """
    _require_connected()
    from config import settings

    info = mt5.account_info()
    if info is None:
        raise MT5NotConnectedError("MT5 account_info() returned None")

    trade_mode = getattr(info, "trade_mode", -1)
    mode_label = "demo" if trade_mode == 0 else ("live" if trade_mode == 2 else "unknown")

    return {
        "connected":        True,
        "mode":             mode_label,
        "login_masked":     _mask_login(settings.mt5_login),
        "server":           getattr(info, "server", settings.mt5_server),
        "name":             getattr(info, "name", ""),
        "currency":         getattr(info, "currency", "USD"),
        "leverage":         getattr(info, "leverage", 0),
        "balance":          round(getattr(info, "balance", 0.0), 2),
        "equity":           round(getattr(info, "equity", 0.0), 2),
        "margin":           round(getattr(info, "margin", 0.0), 2),
        "margin_free":      round(getattr(info, "margin_free", 0.0), 2),
        "margin_level":     round(getattr(info, "margin_level", 0.0), 2),
        "profit":           round(getattr(info, "profit", 0.0), 2),
        "execution_enabled": settings.mt5_execution_enabled,
        "demo_trading_allowed": settings.allow_demo_trading,
    }


def resolve_mt5_symbol(pair: str) -> str | None:
    """
    Discover the correct broker symbol name for the given pair code.
    Tries candidates from SYMBOL_CANDIDATES and caches the result.
    Returns None if no candidate is available on this broker.
    """
    _require_connected()

    code = pair.lower().replace("/", "").replace("_", "")
    if code in _resolved_symbols:
        return _resolved_symbols[code]

    candidates = SYMBOL_CANDIDATES.get(code, [])
    for sym in candidates:
        info = mt5.symbol_info(sym)
        if info is not None:
            # Enable the symbol in Market Watch if not already
            if not info.visible:
                mt5.symbol_select(sym, True)
            _resolved_symbols[code] = sym
            logger.info("MT5 symbol resolved: %s → %s", code, sym)
            return sym

    logger.warning("MT5 symbol not found for %s (tried: %s)", code, candidates)
    return None


def get_symbol_info(pair: str) -> dict:
    """Return broker symbol details for the pair."""
    _require_connected()

    broker_symbol = resolve_mt5_symbol(pair)
    if not broker_symbol:
        return {"pair": pair, "broker_symbol": None, "error": f"No MT5 symbol found for {pair}"}

    info = mt5.symbol_info(broker_symbol)
    if info is None:
        return {"pair": pair, "broker_symbol": broker_symbol, "error": "symbol_info() returned None"}

    return {
        "pair":           pair,
        "broker_symbol":  broker_symbol,
        "description":    info.description,
        "digits":         info.digits,
        "contract_size":  info.trade_contract_size,
        "tick_size":      info.trade_tick_size,
        "tick_value":     info.trade_tick_value,
        "volume_min":     info.volume_min,
        "volume_max":     info.volume_max,
        "volume_step":    info.volume_step,
        "spread":         info.spread,
        "currency_base":  info.currency_base,
        "currency_profit": info.currency_profit,
    }


def get_tick(pair: str) -> dict:
    """Return the latest bid/ask/spread for the pair."""
    _require_connected()

    broker_symbol = resolve_mt5_symbol(pair)
    if not broker_symbol:
        raise ValueError(f"Cannot resolve MT5 symbol for {pair}")

    tick = mt5.symbol_info_tick(broker_symbol)
    if tick is None:
        raise RuntimeError(f"MT5 tick data unavailable for {broker_symbol}")

    from pair_config import get_pair_config
    cfg = get_pair_config(pair)
    pip_size = cfg["pip_size"]

    raw_spread = tick.ask - tick.bid
    spread_pips = round(raw_spread / pip_size, 2) if pip_size > 0 else 0.0

    return {
        "pair":          pair,
        "broker_symbol": broker_symbol,
        "bid":           tick.bid,
        "ask":           tick.ask,
        "spread_raw":    round(raw_spread, cfg["price_decimals"]),
        "spread_pips":   spread_pips,
        "timestamp":     datetime.fromtimestamp(tick.time, tz=timezone.utc).isoformat(),
    }


def get_spread(pair: str) -> float:
    """Return current spread in pips/points."""
    return get_tick(pair)["spread_pips"]


def get_rates(pair: str, timeframe: str = "H4", lookback: int = 200) -> list[dict]:
    """Return OHLCV bars from MT5 for the given pair."""
    _require_connected()

    # Map internal timeframe codes to MT5 constants
    _tf_map = {
        "M1":  mt5.TIMEFRAME_M1,  "M5":  mt5.TIMEFRAME_M5,
        "M15": mt5.TIMEFRAME_M15, "M30": mt5.TIMEFRAME_M30,
        "H1":  mt5.TIMEFRAME_H1,  "H4":  mt5.TIMEFRAME_H4,
        "D1":  mt5.TIMEFRAME_D1,  "W1":  mt5.TIMEFRAME_W1,
    }
    tf_const = _tf_map.get(timeframe.upper())
    if tf_const is None:
        raise ValueError(f"Unknown timeframe {timeframe!r}")

    broker_symbol = resolve_mt5_symbol(pair)
    if not broker_symbol:
        raise ValueError(f"Cannot resolve MT5 symbol for {pair}")

    rates = mt5.copy_rates_from_pos(broker_symbol, tf_const, 0, lookback)
    if rates is None or len(rates) == 0:
        return []

    return [
        {
            "time":   datetime.fromtimestamp(r["time"], tz=timezone.utc).isoformat(),
            "open":   r["open"],  "high":   r["high"],
            "low":    r["low"],   "close":  r["close"],
            "volume": int(r["tick_volume"]),
        }
        for r in rates
    ]


def calculate_position_size(
    pair: str,
    account_balance: float,
    risk_percent: float,
    stop_loss_pips: float,
) -> dict:
    """
    Calculate safe lot size using tick value from MT5 symbol info.
    Always rounds DOWN to the nearest volume_step.
    Raises ValueError if calculation is not safe.
    """
    _require_connected()

    if stop_loss_pips <= 0:
        raise ValueError("Stop loss pips must be > 0 to calculate position size.")
    if account_balance <= 0:
        raise ValueError("Account balance must be > 0.")
    if risk_percent <= 0 or risk_percent > 0.5:
        raise ValueError(f"Risk percent {risk_percent}% is outside safe range (0, 0.5]%.")

    broker_symbol = resolve_mt5_symbol(pair)
    if not broker_symbol:
        raise ValueError(f"Cannot resolve MT5 symbol for {pair}")

    sym = mt5.symbol_info(broker_symbol)
    if sym is None:
        raise ValueError(f"Symbol info not available for {broker_symbol}")

    if sym.trade_tick_size <= 0 or sym.trade_tick_value <= 0:
        raise ValueError("Position size could not be calculated safely — tick size/value is zero.")

    from pair_config import get_pair_config
    cfg = get_pair_config(pair)
    pip_size = cfg["pip_size"]

    # pip_value_per_lot = value (in account currency) of 1 pip move on 1 lot
    pip_value_per_lot = (pip_size / sym.trade_tick_size) * sym.trade_tick_value
    if pip_value_per_lot <= 0:
        raise ValueError("Position size could not be calculated safely — pip value is zero.")

    risk_amount = account_balance * (risk_percent / 100.0)
    raw_lots    = risk_amount / (stop_loss_pips * pip_value_per_lot)

    # Floor to volume_step, clamp to [volume_min, volume_max]
    step = sym.volume_step or 0.01
    lots = math.floor(raw_lots / step) * step
    lots = max(sym.volume_min, min(sym.volume_max, lots))
    lots = round(lots, 2)

    return {
        "pair":              pair,
        "broker_symbol":     broker_symbol,
        "account_balance":   round(account_balance, 2),
        "risk_percent":      risk_percent,
        "risk_amount":       round(risk_amount, 2),
        "stop_loss_pips":    stop_loss_pips,
        "pip_value_per_lot": round(pip_value_per_lot, 4),
        "raw_lot_size":      round(raw_lots, 4),
        "lot_size":          lots,
        "volume_min":        sym.volume_min,
        "volume_max":        sym.volume_max,
        "volume_step":       step,
    }


def validate_demo_order(signal: dict) -> tuple[bool, list[str]]:
    """
    Run all safety gates for a demo order request.
    Returns (is_valid, list_of_rejection_reasons).
    Does NOT raise — caller decides what to do with the result.
    """
    from config import settings
    from pair_config import get_pair_config, validate_pair

    reasons: list[str] = []

    # Gate 1 — execution flags (both must be true)
    if not settings.mt5_execution_enabled:
        reasons.append("MT5_EXECUTION_ENABLED=false. Enable in .env to allow demo orders.")
    if not settings.allow_demo_trading:
        reasons.append("ALLOW_DEMO_TRADING=false. Enable in .env to allow demo orders.")

    # Gate 2 — signal direction
    sig = signal.get("signal", "WAIT")
    if sig not in ("BUY", "SELL"):
        reasons.append(f"Signal is '{sig}'. Only BUY or SELL signals may be traded.")

    # Gate 3 — quality score
    score = int(signal.get("qualityScore", 0))
    if score < 80:
        reasons.append(f"Quality score {score}/100 is below the 80-point minimum.")

    # Gate 4 — risk-reward
    rr = float(signal.get("rr") or 0)
    if rr < 2.5:
        reasons.append(f"RR {rr:.2f} is below the minimum 2.5.")

    # Gate 5 — stop loss & take profit
    if not signal.get("stopLoss"):
        reasons.append("Stop loss is required. No trade without SL.")
    if not signal.get("takeProfit"):
        reasons.append("Take profit is required. No trade without TP.")

    # Gate 6 — news status
    if signal.get("newsStatus", "BLOCKED") != "CLEAR":
        reasons.append("News blackout is active — trading blocked.")

    # Gate 7 — pair supported
    pair = signal.get("pair", "")
    pair_cfg = None
    try:
        code = validate_pair(pair)
        pair_cfg = get_pair_config(code)
    except ValueError:
        reasons.append(f"Unsupported pair '{pair}'.")

    # Short-circuit if execution flags not set — MT5 not needed for early rejection
    if not settings.mt5_execution_enabled or not settings.allow_demo_trading:
        return False, reasons

    # Gate 8 — MT5 available & connected
    if not _MT5_AVAILABLE:
        reasons.append("MetaTrader5 package not available on this platform (Windows-only).")
        return False, reasons

    try:
        _require_connected()
    except (MT5UnavailableError, MT5NotConnectedError) as exc:
        reasons.append(str(exc))
        return False, reasons

    account = mt5.account_info()
    if account is None:
        reasons.append("Cannot read MT5 account info.")
        return False, reasons

    # Gate 9 — account mode check
    # By default we hard-block live accounts (demo-only). When the operator
    # explicitly opts in via LIVE_TRADING_AUTHORIZED=true, live accounts pass
    # this gate — but every other gate (sizing, spread, daily-loss, duplicates,
    # max-open-trades) still applies and the autonomous executor adds:
    #   - 3-layer confirmation requirement
    #   - per-day trade ceiling (default 3)
    #   - 0.05 lot hard cap
    trade_mode = getattr(account, "trade_mode", -1)
    if trade_mode != 0 and not settings.live_trading_authorized:
        reasons.append(
            "Live account detected and LIVE_TRADING_AUTHORIZED=false. "
            "Set LIVE_TRADING_AUTHORIZED=true in .env to allow live execution."
        )

    # Gate 10 — max open trades
    open_pos = mt5.positions_get() or []
    if len(open_pos) >= settings.max_open_trades:
        reasons.append(
            f"Maximum open trades ({settings.max_open_trades}) already reached."
        )

    # Gate 11 — daily loss limit
    balance = getattr(account, "balance", 0.0)
    equity  = getattr(account, "equity", 0.0)
    if balance > 0:
        daily_loss_pct = (balance - equity) / balance * 100
        if daily_loss_pct >= settings.daily_loss_limit_percent:
            reasons.append(
                f"Daily loss limit ({settings.daily_loss_limit_percent}%) reached "
                f"({daily_loss_pct:.2f}% loss). Trading blocked for today."
            )

    # Gate 12 — spread check
    if pair_cfg:
        try:
            broker_sym = resolve_mt5_symbol(pair)
            if broker_sym:
                tick = mt5.symbol_info_tick(broker_sym)
                if tick:
                    pip_size = pair_cfg["pip_size"]
                    spread_pips = (tick.ask - tick.bid) / pip_size
                    code2 = pair_cfg.get("code", pair)
                    if code2 == "xauusd" and spread_pips > settings.max_spread_xauusd_points:
                        reasons.append(
                            f"XAU/USD spread {spread_pips:.1f} points exceeds "
                            f"max {settings.max_spread_xauusd_points} points."
                        )
        except Exception as exc:
            logger.warning("Spread check failed: %s", exc)

    # Gate 13 — no duplicate position on same pair + direction
    if pair_cfg:
        try:
            broker_sym = resolve_mt5_symbol(pair)
            if broker_sym and open_pos:
                for pos in open_pos:
                    if pos.symbol == broker_sym:
                        pos_dir = "BUY" if pos.type == 0 else "SELL"
                        if pos_dir == sig:
                            reasons.append(
                                f"Duplicate: {pair_cfg['display']} {sig} position already open."
                            )
                            break
        except Exception as exc:
            logger.warning("Duplicate check failed: %s", exc)

    return len(reasons) == 0, reasons


def place_demo_market_order(signal: dict) -> dict:
    """
    Validate and place a demo market order.
    Logs EVERY attempt (accepted, rejected, failed) to the database.
    Raises HTTP-safe exceptions on failure.
    Raises MT5SafetyError if validation fails.
    """
    from config import settings

    pair        = signal.get("pair", "")
    sig_dir     = signal.get("signal", "WAIT")
    stop_loss   = signal.get("stopLoss")
    take_profit = signal.get("takeProfit")
    entry_hint  = signal.get("entry")
    risk_pips   = float(signal.get("riskPips") or 0)
    reason_txt  = signal.get("reason", "")

    broker_symbol = None
    spread        = None
    ticket        = None
    actual_entry  = None
    lot_size      = 0.0
    risk_amount   = 0.0

    # Try to resolve symbol and get spread (even if validation fails, for logging)
    try:
        if _MT5_AVAILABLE and mt5.terminal_info():
            broker_symbol = resolve_mt5_symbol(pair)
            if broker_symbol:
                tick = mt5.symbol_info_tick(broker_symbol)
                if tick:
                    from pair_config import get_pair_config
                    cfg = get_pair_config(pair)
                    spread = round((tick.ask - tick.bid) / cfg["pip_size"], 2)
    except Exception:
        pass

    # Run all safety gates
    valid, reasons = validate_demo_order(signal)

    if not valid:
        rejection_reason = " | ".join(reasons)
        logger.warning(
            "Demo order rejected pair=%s signal=%s reasons=%s",
            pair, sig_dir, reasons,
        )
        _log_trade(
            mode="demo", pair=pair, broker_symbol=broker_symbol,
            signal=sig_dir, order_type="MARKET", volume=0.0,
            entry=entry_hint, stop_loss=stop_loss, take_profit=take_profit,
            risk_percent=settings.max_risk_per_trade_percent, risk_amount=0.0,
            spread=spread, ticket=None, status="rejected",
            rejection_reason=rejection_reason, reason=reason_txt, raw_response=None,
        )
        # Fire Telegram rejection alert — fire-and-forget
        try:
            from services.telegram_service import send_demo_trade_rejected
            send_demo_trade_rejected(pair=pair, signal=sig_dir, reasons=reasons)
        except Exception as _tg_exc:
            logger.debug("Telegram rejection alert (non-fatal): %s", _tg_exc)
        raise MT5SafetyError(rejection_reason)

    # Calculate position size (backend only — never trust frontend lot)
    account     = mt5.account_info()
    balance     = getattr(account, "balance", 0.0)
    risk_pct    = settings.max_risk_per_trade_percent

    try:
        sizing      = calculate_position_size(pair, balance, risk_pct, risk_pips)
        lot_size    = sizing["lot_size"]
        risk_amount = sizing["risk_amount"]
        # Optional hard lot cap from caller (autonomous executor sets this to
        # settings.auto_execution_max_lot). Never larger than the calculated size.
        cap = signal.get("max_lot")
        if cap is not None and float(cap) > 0:
            cap = float(cap)
            if lot_size > cap:
                logger.info(
                    "Lot cap applied: calc=%.2f cap=%.2f → using cap", lot_size, cap,
                )
                lot_size = cap
                # Re-scale risk_amount proportionally for accurate logging
                if sizing["raw_lot_size"] > 0:
                    risk_amount = round(
                        risk_amount * (cap / sizing["raw_lot_size"]), 2
                    )
    except ValueError as exc:
        _log_trade(
            mode="demo", pair=pair, broker_symbol=broker_symbol,
            signal=sig_dir, order_type="MARKET", volume=0.0,
            entry=entry_hint, stop_loss=stop_loss, take_profit=take_profit,
            risk_percent=risk_pct, risk_amount=0.0, spread=spread,
            ticket=None, status="rejected",
            rejection_reason=str(exc), reason=reason_txt, raw_response=None,
        )
        raise MT5SafetyError(str(exc)) from exc

    if lot_size <= 0:
        msg = "Calculated lot size is zero — order rejected for safety."
        _log_trade(
            mode="demo", pair=pair, broker_symbol=broker_symbol,
            signal=sig_dir, order_type="MARKET", volume=0.0,
            entry=entry_hint, stop_loss=stop_loss, take_profit=take_profit,
            risk_percent=risk_pct, risk_amount=0.0, spread=spread,
            ticket=None, status="rejected", rejection_reason=msg,
            reason=reason_txt, raw_response=None,
        )
        raise MT5SafetyError(msg)

    # Build MT5 order
    tick = mt5.symbol_info_tick(broker_symbol)
    if sig_dir == "BUY":
        order_type_const = mt5.ORDER_TYPE_BUY
        price = tick.ask
    else:
        order_type_const = mt5.ORDER_TYPE_SELL
        price = tick.bid

    mt5_request = {
        "action":       mt5.TRADE_ACTION_DEAL,
        "symbol":       broker_symbol,
        "volume":       lot_size,
        "type":         order_type_const,
        "price":        price,
        "sl":           float(stop_loss),
        "tp":           float(take_profit),
        "comment":      "FX Signal Pro | demo",
        "type_time":    mt5.ORDER_TIME_GTC,
        "type_filling": mt5.ORDER_FILLING_IOC,
    }

    result = mt5.order_send(mt5_request)
    raw_resp = {
        "retcode": result.retcode,
        "comment": result.comment,
        "deal":    result.deal,
        "order":   result.order,
        "volume":  result.volume,
        "price":   result.price,
    }

    if result.retcode == mt5.TRADE_RETCODE_DONE:
        ticket       = result.order
        actual_entry = result.price
        status       = "accepted"
        logger.info(
            "Demo order placed pair=%s signal=%s ticket=%s lot=%.2f entry=%.5f",
            pair, sig_dir, ticket, lot_size, actual_entry,
        )
        # Fire Telegram alert — fire-and-forget, never raises
        try:
            from services.telegram_service import send_demo_trade_placed
            send_demo_trade_placed(
                pair=pair, signal=sig_dir, ticket=ticket,
                broker_symbol=broker_symbol or pair,
                lot_size=lot_size, entry=actual_entry,
                stop_loss=stop_loss, take_profit=take_profit,
                risk_percent=risk_pct, risk_amount=risk_amount, spread=spread,
            )
        except Exception as _tg_exc:
            logger.debug("Telegram trade alert (non-fatal): %s", _tg_exc)
    else:
        status           = "failed"
        rejection_reason = f"MT5 error {result.retcode}: {result.comment}"
        logger.error(
            "Demo order failed pair=%s signal=%s retcode=%s comment=%s",
            pair, sig_dir, result.retcode, result.comment,
        )
        _log_trade(
            mode="demo", pair=pair, broker_symbol=broker_symbol,
            signal=sig_dir, order_type="MARKET", volume=lot_size,
            entry=price, stop_loss=stop_loss, take_profit=take_profit,
            risk_percent=risk_pct, risk_amount=risk_amount, spread=spread,
            ticket=None, status=status, rejection_reason=rejection_reason,
            reason=reason_txt, raw_response=raw_resp,
        )
        raise MT5SafetyError(rejection_reason)

    _log_trade(
        mode="demo", pair=pair, broker_symbol=broker_symbol,
        signal=sig_dir, order_type="MARKET", volume=lot_size,
        entry=actual_entry, stop_loss=stop_loss, take_profit=take_profit,
        risk_percent=risk_pct, risk_amount=risk_amount, spread=spread,
        ticket=ticket, status=status, rejection_reason=None,
        reason=reason_txt, raw_response=raw_resp,
    )

    return {
        "status":        "accepted",
        "ticket":        ticket,
        "pair":          pair,
        "broker_symbol": broker_symbol,
        "signal":        sig_dir,
        "lot_size":      lot_size,
        "entry":         actual_entry,
        "stop_loss":     stop_loss,
        "take_profit":   take_profit,
        "risk_percent":  risk_pct,
        "risk_amount":   risk_amount,
        "spread":        spread,
    }


def close_demo_position(ticket: int) -> dict:
    """Close an open demo position by ticket number."""
    _require_connected()

    positions = mt5.positions_get(ticket=ticket)
    if not positions:
        raise ValueError(f"No open position with ticket {ticket}")

    pos = positions[0]
    if pos.type == mt5.ORDER_TYPE_BUY:
        close_type  = mt5.ORDER_TYPE_SELL
        close_price = mt5.symbol_info_tick(pos.symbol).bid
    else:
        close_type  = mt5.ORDER_TYPE_BUY
        close_price = mt5.symbol_info_tick(pos.symbol).ask

    req = {
        "action":       mt5.TRADE_ACTION_DEAL,
        "symbol":       pos.symbol,
        "volume":       pos.volume,
        "type":         close_type,
        "position":     pos.ticket,
        "price":        close_price,
        "comment":      "FX Signal Pro | demo close",
        "type_time":    mt5.ORDER_TIME_GTC,
        "type_filling": mt5.ORDER_FILLING_IOC,
    }

    result = mt5.order_send(req)
    if result.retcode != mt5.TRADE_RETCODE_DONE:
        raise RuntimeError(f"Close failed: {result.retcode} — {result.comment}")

    logger.info("Demo position closed: ticket=%s", ticket)
    return {
        "closed":  True,
        "ticket":  ticket,
        "retcode": result.retcode,
        "comment": result.comment,
    }


def get_open_positions() -> list[dict]:
    """Return all currently open demo positions."""
    _require_connected()

    positions = mt5.positions_get() or []
    return [
        {
            "ticket":     p.ticket,
            "symbol":     p.symbol,
            "type":       "BUY" if p.type == 0 else "SELL",
            "volume":     p.volume,
            "open_price": p.price_open,
            "sl":         p.sl,
            "tp":         p.tp,
            "profit":     round(p.profit, 2),
            "swap":       round(p.swap, 2),
            "open_time":  datetime.fromtimestamp(p.time, tz=timezone.utc).isoformat(),
            "comment":    p.comment,
        }
        for p in positions
    ]


def get_trade_history(last_n: int = 50) -> list[dict]:
    """Return recent closed deals from MT5 history."""
    _require_connected()
    from datetime import timedelta

    to_date   = datetime.now(timezone.utc)
    from_date = to_date - timedelta(days=30)

    deals = mt5.history_deals_get(from_date, to_date) or []
    # Filter to only entry/exit deals (not commission/swap rows)
    trade_deals = [d for d in deals if d.entry in (1, 0)][-last_n:]

    return [
        {
            "ticket":     d.ticket,
            "order":      d.order,
            "symbol":     d.symbol,
            "type":       "BUY" if d.type == 0 else "SELL",
            "volume":     d.volume,
            "price":      d.price,
            "profit":     round(d.profit, 2),
            "swap":       round(d.swap, 2),
            "commission": round(d.commission, 2),
            "time":       datetime.fromtimestamp(d.time, tz=timezone.utc).isoformat(),
            "comment":    d.comment,
        }
        for d in reversed(trade_deals)
    ]
