"""
MT5 Bridge Daemon  — runs on your WINDOWS LAPTOP
================================================

Polls the VPS at https://<your-domain>/api/v1/bridge/pending-orders every 30 s,
executes any PENDING orders on MetaTrader5, and POSTs the result back.

Mandate-aware behaviour (when order originates from the institutional strategist):
  • Lot size FIXED at 0.01 regardless of risk-% calc
  • Initial order placed at TP = TP2 (the stretch target)
  • Per-position monitor thread tracks MFE / MAE in points every 30 s
  • When TP1 milestone hit, SL is modified to entry (breakeven)
  • When position closes, the daemon POSTs CLOSED with result + MFE + MAE
    so the backend's strategist_verdicts learning curve sees the outcome

Legacy orders (no mandate metadata) still use risk-%-derived sizing.

Usage
-----
  python deploy/mt5_bridge_daemon.py

Requires
--------
  pip install requests MetaTrader5

Setup
-----
1. On your Windows laptop, copy `deploy/.env.bridge.example` to `.env.bridge`
   in the same folder as this script. Fill in:
     - DASHBOARD_URL  (e.g. https://xauusd-anwar.duckdns.org)
     - BRIDGE_SECRET  (must match MT5_BRIDGE_SHARED_SECRET on the VPS)
     - MT5_LOGIN / MT5_PASSWORD / MT5_SERVER  (your Exness DEMO account)
2. Make sure MetaTrader5 terminal is installed and logged into the same account
3. Run: python deploy/mt5_bridge_daemon.py
4. (Optional) Install as a Windows service via NSSM so it auto-starts at boot

Safety
------
- DEMO ACCOUNT ONLY — live execution is hard-disabled by the VPS strategist
- The daemon ONLY executes orders signed with the shared secret
- 5-minute claim TTL — stale orders auto-expire on the VPS side
- Heartbeat every minute so the dashboard knows the daemon is alive
"""
from __future__ import annotations

import logging
import os
import platform
import signal
import socket
import sys
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Optional

# ── Configure logging early ──────────────────────────────────────────────────
LOG_FORMAT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"
logging.basicConfig(level=logging.INFO, format=LOG_FORMAT)
log = logging.getLogger("mt5_bridge")

# ── Load .env.bridge ─────────────────────────────────────────────────────────
def _load_env_file(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    if not path.exists():
        return out
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        k, v = line.split("=", 1)
        v = v.strip().strip('"').strip("'")
        out[k.strip()] = v
    return out


SCRIPT_DIR = Path(__file__).resolve().parent
ENV = _load_env_file(SCRIPT_DIR / ".env.bridge")

def env(key: str, default: Optional[str] = None) -> Optional[str]:
    return os.environ.get(key) or ENV.get(key) or default

DASHBOARD_URL  = (env("DASHBOARD_URL") or "").rstrip("/")
BRIDGE_SECRET  = env("BRIDGE_SECRET", "")
DAEMON_ID      = env("DAEMON_ID") or f"{socket.gethostname()}-{uuid.uuid4().hex[:6]}"
POLL_SEC       = int(env("POLL_SEC", "30"))
HEARTBEAT_SEC  = int(env("HEARTBEAT_SEC", "60"))

MT5_LOGIN     = env("MT5_LOGIN", "")
MT5_PASSWORD  = env("MT5_PASSWORD", "")
MT5_SERVER    = env("MT5_SERVER", "")

if not DASHBOARD_URL or not BRIDGE_SECRET:
    log.error("DASHBOARD_URL and BRIDGE_SECRET are required. "
              "Create deploy/.env.bridge from .env.bridge.example.")
    sys.exit(2)

# ── HTTP client ──────────────────────────────────────────────────────────────
try:
    import requests
except ImportError:
    log.error("requests not installed. Run: pip install requests")
    sys.exit(2)

session = requests.Session()
session.headers.update({
    "X-Bridge-Secret":     BRIDGE_SECRET,
    "X-Bridge-Daemon-Id":  DAEMON_ID,
    "Content-Type":        "application/json",
    "User-Agent":          f"xauusd-mt5-bridge/{platform.python_version()}",
})

def api(path: str) -> str:
    return f"{DASHBOARD_URL}/api/v1/bridge{path}"


# ── MT5 connection ───────────────────────────────────────────────────────────
try:
    import MetaTrader5 as mt5
except ImportError:
    log.error("MetaTrader5 not installed. Run: pip install MetaTrader5")
    sys.exit(2)


def mt5_init() -> bool:
    """Connect to the running MT5 terminal. Returns True if connected."""
    kwargs = {}
    if MT5_LOGIN:    kwargs["login"]    = int(MT5_LOGIN)
    if MT5_PASSWORD: kwargs["password"] = MT5_PASSWORD
    if MT5_SERVER:   kwargs["server"]   = MT5_SERVER

    if not mt5.initialize(**kwargs):
        err = mt5.last_error()
        log.error("mt5.initialize failed: %s", err)
        return False
    acc = mt5.account_info()
    log.info("MT5 connected: login=%s server=%s balance=%.2f currency=%s",
             acc.login, acc.server, acc.balance, acc.currency)
    return True


def mt5_shutdown() -> None:
    try:
        mt5.shutdown()
    except Exception:
        pass


# ── Order execution (mirrors VPS-side validation) ────────────────────────────

def _resolve_broker_symbol() -> Optional[str]:
    """Find which XAUUSD symbol name this broker exposes."""
    for c in ["XAUUSD", "XAUUSDm", "GOLD", "XAU/USD"]:
        info = mt5.symbol_info(c)
        if info:
            if not info.visible:
                mt5.symbol_select(c, True)
            return c
    return None


def _is_mandate_order(order: dict) -> bool:
    """An order is mandate-driven if the strategist tagged its confirmations."""
    conf = order.get("confirmations") or {}
    if conf.get("source") == "mandate_strategist":
        return True
    mt5_obj = conf.get("mt5_execution_object") or {}
    return bool(mt5_obj)


def execute_order(order: dict) -> dict:
    """
    Place a market order on MT5. Mandate orders use lot=0.01 + TP=TP2 (the
    stretch target) with TP1 as a breakeven milestone managed by the monitor
    thread. Legacy orders use risk-%-derived sizing with TP=TP1.
    """
    sig          = order["signal"]
    entry_hint   = float(order["entry"])
    sl           = float(order["stopLoss"])
    tp1          = float(order["takeProfit"])                      # always present
    tp2          = order.get("takeProfit2")                        # mandate only
    risk_pips    = float(order.get("riskPips") or 0)
    max_lot      = float(order.get("maxLot") or 0.05)
    mandate      = _is_mandate_order(order)

    broker_sym = _resolve_broker_symbol()
    if not broker_sym:
        return {"status": "FAILED", "error": "Could not resolve XAU/USD symbol on broker"}

    sym  = mt5.symbol_info(broker_sym)
    tick = mt5.symbol_info_tick(broker_sym)
    if not sym or not tick:
        return {"status": "FAILED", "error": f"symbol_info / tick unavailable for {broker_sym}"}

    # ── Lot sizing ──────────────────────────────────────────────────────
    # MANDATE: fixed 0.01 lot, period. Mandate orders are demo-only, fixed-size
    # learning trades — no risk-% recalc.
    if mandate:
        lots = 0.01
    else:
        # Legacy 0.25% risk path (back-compat with old auto_executor enqueues)
        if sym.trade_tick_size <= 0 or sym.trade_tick_value <= 0:
            return {"status": "FAILED", "error": "tick_size/value invalid for XAU/USD"}
        pip_size = 0.1
        pip_value_per_lot = (pip_size / sym.trade_tick_size) * sym.trade_tick_value
        account = mt5.account_info()
        if not account:
            return {"status": "FAILED", "error": "Cannot read MT5 account_info"}
        risk_amount = account.balance * 0.0025
        raw_lots = (
            risk_amount / (risk_pips * pip_value_per_lot)
            if (risk_pips > 0 and pip_value_per_lot > 0) else max_lot
        )
        import math
        step = sym.volume_step or 0.01
        lots = math.floor(raw_lots / step) * step
        lots = max(sym.volume_min, min(sym.volume_max, lots, max_lot))
        lots = round(lots, 2)
        if lots <= 0:
            return {"status": "REJECTED", "error": "Calculated lot size is zero"}

    # ── Decide initial TP for the order ─────────────────────────────────
    # Mandate: place at TP=TP2 (final target). When price reaches TP1 the
    # monitor moves SL to entry. If TP2 is missing, fall back to TP1.
    if mandate and tp2 is not None:
        initial_tp = float(tp2)
    else:
        initial_tp = tp1

    # ── Build order request ─────────────────────────────────────────────
    if sig == "BUY":
        order_type, price = mt5.ORDER_TYPE_BUY, tick.ask
    elif sig == "SELL":
        order_type, price = mt5.ORDER_TYPE_SELL, tick.bid
    else:
        return {"status": "REJECTED", "error": f"Invalid signal {sig}"}

    request = {
        "action":       mt5.TRADE_ACTION_DEAL,
        "symbol":       broker_sym,
        "volume":       lots,
        "type":         order_type,
        "price":        price,
        "sl":           sl,
        "tp":           initial_tp,
        "comment":      f"XAUUSD-{'mandate' if mandate else 'legacy'}-bridge",
        "type_time":    mt5.ORDER_TIME_GTC,
        "type_filling": mt5.ORDER_FILLING_IOC,
    }

    log.info(
        "Placing %s on %s lots=%.2f sl=%.2f tp=%.2f (mandate=%s tp1=%s tp2=%s)",
        sig, broker_sym, lots, sl, initial_tp, mandate, tp1, tp2,
    )
    result = mt5.order_send(request)

    if result is None:
        return {"status": "FAILED", "error": f"order_send returned None; last_error={mt5.last_error()}"}
    if result.retcode != mt5.TRADE_RETCODE_DONE:
        return {
            "status":       "REJECTED",
            "error":        f"retcode={result.retcode} comment={result.comment}",
            "lot_executed": 0.0,
        }

    ticket = int(result.order)
    fill_price = float(result.price or price)
    log.info("ACCEPTED ticket=%d fill=%.2f volume=%.2f", ticket, fill_price, result.volume)

    # ── Spawn position monitor (MFE/MAE + TP1 breakeven) ───────────────
    # Only when the order is mandate-tagged AND we have a TP1 to chase.
    if mandate and tp1 is not None:
        t = threading.Thread(
            target=monitor_position,
            kwargs=dict(
                order_id=int(order["id"]),
                ticket=ticket,
                broker_sym=broker_sym,
                direction=sig,
                entry=fill_price,
                sl=sl,
                tp1=tp1,
                tp2=float(tp2) if tp2 is not None else tp1,
            ),
            name=f"monitor-{ticket}",
            daemon=True,
        )
        t.start()

    return {
        "status":       "ACCEPTED",
        "ticket":       ticket,
        "lot_executed": float(result.volume),
        "error":        None,
    }


# ── Position monitor (mandate: MFE/MAE + TP1 breakeven) ──────────────────────

_POSITION_POLL_SEC      = 30
_POSITION_MAX_LIFETIME  = 24 * 60 * 60     # safety stop after 24h

def monitor_position(
    *, order_id: int, ticket: int, broker_sym: str, direction: str,
    entry: float, sl: float, tp1: float, tp2: float,
) -> None:
    """
    Per-trade monitor thread. Runs until the position closes.
    Tracks peak MFE / MAE in points, moves SL→breakeven when TP1 hit,
    and POSTs CLOSED + outcome to the VPS when MT5 closes the position.

    Points are calculated in USD price units (1.0 point = $1 for XAUUSD).
    """
    log.info("[monitor %d] started entry=%.2f sl=%.2f tp1=%.2f tp2=%.2f", ticket, entry, sl, tp1, tp2)
    is_buy        = direction == "BUY"
    mfe_pts       = 0.0
    mae_pts       = 0.0
    breakeven_set = False
    started       = time.time()

    while _running and (time.time() - started) < _POSITION_MAX_LIFETIME:
        try:
            time.sleep(_POSITION_POLL_SEC)
            positions = mt5.positions_get(ticket=ticket)

            # Position closed? (positions_get returns empty when terminated)
            if not positions:
                _report_closed(order_id, ticket, broker_sym, direction,
                               entry=entry, mfe_pts=mfe_pts, mae_pts=mae_pts)
                log.info("[monitor %d] CLOSED · mfe=%.1f mae=%.1f", ticket, mfe_pts, mae_pts)
                return

            pos = positions[0]
            cur = pos.price_current

            # Update MFE / MAE (mandate: every fresh tick we see)
            favourable = (cur - entry) if is_buy else (entry - cur)
            adverse    = -favourable
            if favourable > mfe_pts: mfe_pts = favourable
            if adverse    > mae_pts: mae_pts = adverse

            # Move SL to entry when TP1 milestone reached
            if not breakeven_set:
                tp1_hit = (cur >= tp1) if is_buy else (cur <= tp1)
                if tp1_hit:
                    _modify_sl(ticket, broker_sym, new_sl=entry, current_tp=pos.tp)
                    breakeven_set = True
                    log.info("[monitor %d] TP1 hit → SL moved to entry (%.2f)", ticket, entry)
        except Exception as exc:
            log.warning("[monitor %d] tick error: %s", ticket, exc)

    # If we hit the lifetime cap, report what we have
    log.warning("[monitor %d] lifetime exceeded — closing monitor", ticket)
    _report_closed(order_id, ticket, broker_sym, direction,
                   entry=entry, mfe_pts=mfe_pts, mae_pts=mae_pts, lifetime_exceeded=True)


def _modify_sl(ticket: int, broker_sym: str, new_sl: float, current_tp: float) -> None:
    """Move SL on an open position. Keeps TP unchanged."""
    req = {
        "action":   mt5.TRADE_ACTION_SLTP,
        "symbol":   broker_sym,
        "position": ticket,
        "sl":       new_sl,
        "tp":       current_tp,
    }
    r = mt5.order_send(req)
    if r is None or r.retcode != mt5.TRADE_RETCODE_DONE:
        log.warning("[monitor %d] SL modify failed: %s", ticket, mt5.last_error() if r is None else r.retcode)


def _report_closed(
    order_id: int, ticket: int, broker_sym: str, direction: str,
    *, entry: float, mfe_pts: float, mae_pts: float,
    lifetime_exceeded: bool = False,
) -> None:
    """POST the terminal CLOSED status with mandate post-trade fields."""
    # Try to read the most recent deal to figure out the realized outcome
    result_str  = "BREAKEVEN"
    pips_outcome = 0.0
    note         = ""
    try:
        history = mt5.history_deals_get(position=ticket) or []
        # The closing deal is the one with type opposite to the entry
        closer = next((d for d in reversed(history) if d.position_id == ticket
                       and d.entry == mt5.DEAL_ENTRY_OUT), None)
        if closer:
            close_price = closer.price
            move = (close_price - entry) if direction == "BUY" else (entry - close_price)
            pips_outcome = round(move, 2)
            if move > 0.5:    result_str = "WIN"
            elif move < -0.5: result_str = "LOSS"
            else:             result_str = "BREAKEVEN"
            note = f"closed at {close_price:.2f}, move={pips_outcome:+.2f} pts"
    except Exception as exc:
        log.debug("[monitor %d] history lookup failed: %s", ticket, exc)

    if lifetime_exceeded:
        note = (note + " · lifetime cap exceeded").strip(" ·")

    payload = {
        "status":          "CLOSED",
        "ticket":          ticket,
        "result":          result_str,
        "pips_outcome":    pips_outcome,
        "mfe_pts":         round(mfe_pts, 2),
        "mae_pts":         round(mae_pts, 2),
        "rules_followed":  True,
        "post_trade_note": note or None,
    }
    try:
        r = session.post(api(f"/result/{order_id}"), json=payload, timeout=15)
        if r.status_code >= 400:
            log.warning("[monitor %d] CLOSED report HTTP %d: %s",
                        ticket, r.status_code, r.text[:200])
        else:
            log.info("[monitor %d] CLOSED report posted (result=%s mfe=%.1f mae=%.1f)",
                     ticket, result_str, mfe_pts, mae_pts)
    except Exception as exc:
        log.warning("[monitor %d] CLOSED report failed: %s", ticket, exc)


# ── Main loop ────────────────────────────────────────────────────────────────

_running = True
def _handle_sigterm(*_):
    global _running
    log.info("Received SIGTERM/SIGINT — shutting down…")
    _running = False
signal.signal(signal.SIGINT,  _handle_sigterm)
signal.signal(signal.SIGTERM, _handle_sigterm)


def fetch_pending() -> list[dict]:
    try:
        r = session.get(api("/pending-orders"), timeout=15)
        r.raise_for_status()
        body = r.json()
        return (body.get("data") or {}).get("orders") or []
    except Exception as exc:
        log.warning("fetch_pending error: %s", exc)
        return []


def claim(order_id: int) -> bool:
    try:
        r = session.post(api(f"/claim/{order_id}"), timeout=10)
        return r.status_code == 200
    except Exception as exc:
        log.warning("claim %d failed: %s", order_id, exc)
        return False


def report(order_id: int, result: dict) -> None:
    try:
        r = session.post(api(f"/result/{order_id}"), json=result, timeout=15)
        if r.status_code >= 400:
            log.warning("report %d HTTP %d: %s", order_id, r.status_code, r.text[:200])
    except Exception as exc:
        log.warning("report %d failed: %s", order_id, exc)


def heartbeat() -> None:
    """
    Heartbeat with MT5 terminal state. The backend uses trade_allowed and
    dlls_allowed to surface AutoTrading status on /bridge/status without
    waiting for an order to fail with retcode=10027.
    """
    headers = {}
    try:
        info = mt5.terminal_info()
        acc  = mt5.account_info()
        if info is not None:
            headers["X-MT5-Trade-Allowed"]  = str(bool(info.trade_allowed)).lower()
            headers["X-MT5-DLLs-Allowed"]   = str(bool(info.dlls_allowed)).lower()
            headers["X-MT5-Connected"]      = str(bool(info.connected)).lower()
            headers["X-MT5-Company"]        = (info.company or "")[:40]
        if acc is not None:
            headers["X-MT5-Account-Login"]  = str(acc.login)
            headers["X-MT5-Account-Server"] = (acc.server or "")[:40]
            headers["X-MT5-Account-Demo"]   = str(acc.trade_mode == 0).lower()  # 0 = DEMO
            headers["X-MT5-Balance"]        = f"{acc.balance:.2f}"
    except Exception as exc:
        log.debug("heartbeat terminal_info failed: %s", exc)
    try:
        session.get(api("/health"), headers=headers, timeout=10)
    except Exception:
        pass


def main():
    log.info("MT5 Bridge Daemon starting")
    log.info("Daemon ID: %s", DAEMON_ID)
    log.info("Dashboard: %s", DASHBOARD_URL)
    log.info("Poll interval: %ds  ·  Heartbeat: %ds", POLL_SEC, HEARTBEAT_SEC)

    if not mt5_init():
        log.error("Fatal: MT5 connect failed. Exiting.")
        sys.exit(1)

    last_heartbeat = 0
    try:
        while _running:
            now = time.time()

            # Periodic heartbeat
            if now - last_heartbeat >= HEARTBEAT_SEC:
                heartbeat()
                last_heartbeat = now

            # Pull pending orders
            orders = fetch_pending()
            if orders:
                log.info("Found %d pending order(s)", len(orders))
            for o in orders:
                oid = o["id"]
                # Try to claim atomically
                if not claim(oid):
                    log.info("Order %d already claimed by another daemon — skipping", oid)
                    continue
                # Execute on MT5
                try:
                    result = execute_order(o)
                except Exception as exc:
                    result = {"status": "FAILED", "error": f"daemon exception: {exc}"}
                log.info("Order %d result: %s", oid, result)
                # Report back to VPS
                report(oid, result)

            # Sleep until next poll (interrupt-friendly)
            sleep_until = now + POLL_SEC
            while _running and time.time() < sleep_until:
                time.sleep(1)
    finally:
        mt5_shutdown()
        log.info("Bridge daemon stopped.")


if __name__ == "__main__":
    main()
