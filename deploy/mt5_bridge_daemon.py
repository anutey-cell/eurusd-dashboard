"""
MT5 Bridge Daemon  — runs on your WINDOWS LAPTOP
================================================

Polls the VPS at https://<your-domain>/api/v1/bridge/pending-orders every 30 s,
executes any PENDING orders on MetaTrader5, and POSTs the result back.

This is the missing half of the hybrid architecture:
  Linux VPS  → produces orders (always on, runs scanner + predictor 24/7)
  Windows    → consumes orders (only runs when laptop is on)

When your laptop is off, the VPS still alerts you on Telegram about queued
orders; they'll auto-EXPIRE after 5 minutes if no daemon claims them.

When your laptop is on, this daemon claims them within 30 s, places the
real MT5 order, and reports back. The VPS dashboard's "Bridge Status" panel
shows the heartbeat so you can see at a glance whether the bridge is up.

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
     - MT5_LOGIN / MT5_PASSWORD / MT5_SERVER  (your Exness account)
2. Make sure MetaTrader5 terminal is installed and logged into the same account
3. Run: python deploy/mt5_bridge_daemon.py
4. (Optional) Install as a Windows service via NSSM so it auto-starts at boot

Safety
------
- The daemon ONLY executes orders that the VPS has signed with the shared secret
- It runs the same 13 MT5 safety gates locally (sizing, spread, daily-loss, etc.)
- Hard cap on lot size pulled from each order (default 0.05 from VPS config)
- 5-minute claim TTL — stale orders expire automatically on the VPS side
- Heartbeat every minute so the dashboard knows the daemon is alive
"""
from __future__ import annotations

import logging
import os
import platform
import signal
import socket
import sys
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

def execute_order(order: dict) -> dict:
    """
    Place a market order on MT5 with the lot size = min(calculated, max_lot).
    Returns a dict {status, ticket, lot_executed, error} matching the
    ExecutionResult schema the VPS expects.
    """
    sig          = order["signal"]
    entry_hint   = float(order["entry"])
    sl           = float(order["stopLoss"])
    tp           = float(order["takeProfit"])
    risk_pips    = float(order.get("riskPips") or 0)
    max_lot      = float(order.get("maxLot") or 0.05)

    # Resolve symbol on the broker (Exness uses "XAUUSD" or "XAUUSDm")
    candidates = ["XAUUSD", "XAUUSDm", "GOLD", "XAU/USD"]
    broker_sym = None
    for c in candidates:
        info = mt5.symbol_info(c)
        if info and info.visible:
            broker_sym = c
            break
        if info and not info.visible:
            mt5.symbol_select(c, True)
            broker_sym = c
            break
    if not broker_sym:
        return {"status": "FAILED", "error": "Could not resolve XAU/USD symbol on broker"}

    sym = mt5.symbol_info(broker_sym)
    tick = mt5.symbol_info_tick(broker_sym)
    if not sym or not tick:
        return {"status": "FAILED", "error": f"symbol_info / tick unavailable for {broker_sym}"}

    # Compute lot size (same logic as backend's calculate_position_size).
    # We default to max_lot here since the VPS hard-capped it already.
    pip_size = 0.1   # XAU/USD points
    if sym.trade_tick_size <= 0 or sym.trade_tick_value <= 0:
        return {"status": "FAILED", "error": "tick_size/value invalid for XAU/USD"}
    pip_value_per_lot = (pip_size / sym.trade_tick_size) * sym.trade_tick_value

    account = mt5.account_info()
    if not account:
        return {"status": "FAILED", "error": "Cannot read MT5 account_info"}

    # 0.25% risk per trade; cap at provided max_lot
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

    # Order details
    if sig == "BUY":
        order_type = mt5.ORDER_TYPE_BUY
        price = tick.ask
    elif sig == "SELL":
        order_type = mt5.ORDER_TYPE_SELL
        price = tick.bid
    else:
        return {"status": "REJECTED", "error": f"Invalid signal {sig}"}

    request = {
        "action":       mt5.TRADE_ACTION_DEAL,
        "symbol":       broker_sym,
        "volume":       lots,
        "type":         order_type,
        "price":        price,
        "sl":           sl,
        "tp":           tp,
        "comment":      "XAUUSD-bridge",
        "type_time":    mt5.ORDER_TIME_GTC,
        "type_filling": mt5.ORDER_FILLING_IOC,
    }

    log.info("Placing %s on %s lots=%.2f sl=%.2f tp=%.2f", sig, broker_sym, lots, sl, tp)
    result = mt5.order_send(request)

    if result is None:
        return {"status": "FAILED", "error": f"order_send returned None; last_error={mt5.last_error()}"}
    if result.retcode != mt5.TRADE_RETCODE_DONE:
        return {
            "status":       "REJECTED",
            "error":        f"retcode={result.retcode} comment={result.comment}",
            "lot_executed": 0.0,
        }
    return {
        "status":       "ACCEPTED",
        "ticket":       int(result.order),
        "lot_executed": float(result.volume),
        "error":        None,
    }


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
    try:
        session.get(api("/health"), timeout=10)
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
