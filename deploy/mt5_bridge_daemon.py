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
from pathlib import Path
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT / ".env.bridge", override=True)
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

# Cache the resolved symbol after first discovery (saves a symbols_get sweep
# on every order). Resolved once per daemon process; broker symbol names
# don't change at runtime.
_RESOLVED_SYMBOL: Optional[str] = None


def _resolve_broker_symbol() -> Optional[str]:
    """
    Find which XAUUSD symbol name this broker exposes. Brokers vary widely:
      Standard accounts:  XAUUSD
      Micro accounts:     XAUUSDm
      Cent accounts:      XAUUSDc
      Exness Trial:       XAUUSDz / XAUUSDt
      Raw-spread:         XAUUSD.r / XAUUSD.s
      CFD aliases:        GOLD, XAU/USD, GOLD#
      ECN suffixes:       XAUUSD.ecn / XAUUSD_pro / XAUUSD-cd

    Strategy:
      1. Try common literal names first (fast path)
      2. Fall back to scanning ALL symbols and matching anything that
         contains "XAU" + a USD anchor
      3. Log every candidate so the operator can see what's available
    """
    global _RESOLVED_SYMBOL
    if _RESOLVED_SYMBOL is not None:
        return _RESOLVED_SYMBOL

    # ── Pass 1: explicit common names (fast) ────────────────────────────
    # mt5.symbol_select(name, True) is the reliable existence test:
    #   returns True  → symbol exists, now added to Market Watch
    #   returns False → symbol doesn't exist on this broker
    # mt5.symbol_info() can return None for valid symbols that aren't yet
    # in Market Watch on some MT5 builds, so we lead with symbol_select.
    explicit = [
        "XAUUSD", "XAUUSDm", "XAUUSDc", "XAUUSDz", "XAUUSDt",
        "XAUUSD.r", "XAUUSD.s", "XAUUSD.ecn", "XAUUSD_pro", "XAUUSD-cd",
        "GOLD", "XAU/USD", "XAU.USD", "GOLD#",
    ]
    for name in explicit:
        if mt5.symbol_select(name, True):
            info = mt5.symbol_info(name)
            if info is not None:
                log.info("Symbol resolved via explicit list: %s", name)
                _RESOLVED_SYMBOL = name
                return name

    # ── Pass 2: scan all broker symbols, match anything XAU + USD ───────
    log.warning("No explicit XAUUSD match — scanning broker's full symbol list")
    try:
        all_symbols = mt5.symbols_get() or []
    except Exception as exc:
        log.error("symbols_get failed: %s", exc)
        return None

    candidates = []
    for s in all_symbols:
        n = s.name.upper().replace("/", "").replace(".", "").replace("_", "").replace("-", "")
        if "XAU" in n and "USD" in n:
            candidates.append(s.name)

    if candidates:
        log.info("XAU/USD candidates found on broker: %s", candidates[:10])
        # Prefer the shortest name (usually the canonical one for the account type)
        candidates.sort(key=lambda x: (len(x), x))
        for chosen in candidates:
            if mt5.symbol_select(chosen, True):
                info = mt5.symbol_info(chosen)
                if info is not None:
                    log.info("Symbol resolved via scan: %s", chosen)
                    _RESOLVED_SYMBOL = chosen
                    return chosen
            else:
                log.warning("Candidate %s exists in symbols_get but symbol_select rejected", chosen)

    # ── Pass 3: nothing matched — dump first 20 symbols for triage ──────
    sample = [s.name for s in all_symbols[:20]]
    log.error(
        "No XAU/USD symbol found on broker. First 20 of %d symbols: %s. "
        "Check Market Watch in MT5 — the gold symbol may need to be added.",
        len(all_symbols), sample,
    )
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

    # ── Pick a filling mode the broker actually accepts ────────────────
    # sym.filling_mode is a bitmask of supported modes:
    #   bit 0 (=1) → FOK supported
    #   bit 1 (=2) → IOC supported
    # Brokers vary (Exness Trial servers commonly accept only FOK).
    # Without auto-detection we hit retcode=10030 "Unsupported filling mode".
    supported_bits = sym.filling_mode or 0
    if supported_bits & 2:          # IOC preferred (allows partial fills)
        type_filling = mt5.ORDER_FILLING_IOC
    elif supported_bits & 1:        # FOK fallback
        type_filling = mt5.ORDER_FILLING_FOK
    else:                            # Return-after-deal — always allowed
        type_filling = mt5.ORDER_FILLING_RETURN

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
        "type_filling": type_filling,
    }

    log.info(
        "Placing %s on %s lots=%.2f sl=%.2f tp=%.2f (mandate=%s filling=%s tp1=%s tp2=%s)",
        sig, broker_sym, lots, sl, initial_tp, mandate, type_filling, tp1, tp2,
    )
    result = mt5.order_send(request)

    # If the broker still rejects the chosen mode (rare — bitmask wrong),
    # retry once with the alternate. Belt-and-suspenders against unknown
    # broker quirks.
    if result is not None and result.retcode == 10030:
        alt_filling = (mt5.ORDER_FILLING_FOK if type_filling == mt5.ORDER_FILLING_IOC
                       else mt5.ORDER_FILLING_IOC)
        log.warning("retcode 10030 with filling=%s — retrying with %s",
                    type_filling, alt_filling)
        request["type_filling"] = alt_filling
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
    from datetime import datetime as _dt, timedelta as _td
    # Try to read the most recent deal to figure out the realized outcome.
    # MT5's history_deals_get(position=...) is inconsistent across builds — some
    # return empty without a date range. Use date_from/date_to and filter manually.
    result_str   = "BREAKEVEN"
    pips_outcome = 0.0
    close_profit = 0.0
    note         = ""
    closer       = None
    try:
        # Pull 30 days of deal history (covers all our orphaned trades) and
        # filter by position_id ourselves. Far more reliable than the
        # position= kwarg which silently returns [] on some MT5 builds.
        date_from = _dt.now() - _td(days=30)
        date_to   = _dt.now() + _td(hours=1)   # +1h cushion for clock skew
        history = mt5.history_deals_get(date_from, date_to) or []
        log.info("[monitor %d] history sweep: %d deals in last 30d", ticket, len(history))

        position_deals = [d for d in history if d.position_id == ticket]
        log.info("[monitor %d] %d deals match position_id", ticket, len(position_deals))

        # Closing deal = DEAL_ENTRY_OUT for this position
        closer = next((d for d in reversed(position_deals)
                       if d.entry == mt5.DEAL_ENTRY_OUT), None)

        if closer is not None:
            close_price  = closer.price
            close_profit = closer.profit or 0.0
            move = (close_price - entry) if direction == "BUY" else (entry - close_price)
            pips_outcome = round(move, 2)
            if   close_profit > 0.05:  result_str = "WIN"
            elif close_profit < -0.05: result_str = "LOSS"
            else:                       result_str = "BREAKEVEN"
            note = f"closed @ {close_price:.2f}  move={pips_outcome:+.2f}pts  profit=${close_profit:+.2f}"
            log.info("[monitor %d] resolved: %s %s", ticket, result_str, note)
        else:
            note = f"position_id={ticket} not in deal history — may still be open or out of window"
            log.warning("[monitor %d] %s", ticket, note)
    except Exception as exc:
        log.warning("[monitor %d] history lookup failed: %s", ticket, exc)
        note = f"history lookup error: {exc}"

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


def maybe_periodic_reconcile() -> None:
    """
    Run reconcile_orphaned_trades every 15 min (in addition to startup).
    Picks up any orphans that accumulated since the last sweep — useful when
    monitor threads die mid-run for any reason (network blips etc.).
    """
    global _last_periodic_reconcile_at
    import time as _time
    now = _time.time()
    if now - _last_periodic_reconcile_at < 15 * 60:
        return
    _last_periodic_reconcile_at = now
    try:
        reconcile_orphaned_trades()
    except Exception as exc:
        log.warning("periodic reconcile error: %s", exc)


_last_periodic_reconcile_at: float = 0.0


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
    Heartbeat with MT5 terminal state + open-position snapshot. Backend
    uses trade_allowed/dlls_allowed for AutoTrading visibility, and
    open-position count for the risk-cap gate.
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
            headers["X-MT5-Equity"]         = f"{acc.equity:.2f}"
        # Open positions — count + ticket list (used by the position-cap gate)
        positions = mt5.positions_get() or []
        headers["X-MT5-Open-Positions"] = str(len(positions))
        if positions:
            tickets = [str(p.ticket) for p in positions[:20]]  # cap header size
            headers["X-MT5-Open-Tickets"] = ",".join(tickets)
            # Aggregate floating P&L across all open positions
            floating_pnl = sum((p.profit or 0.0) for p in positions)
            headers["X-MT5-Floating-PnL"] = f"{floating_pnl:.2f}"
    except Exception as exc:
        log.debug("heartbeat terminal_info failed: %s", exc)
    try:
        session.get(api("/health"), headers=headers, timeout=10)
    except Exception:
        pass


def reconcile_orphaned_trades() -> None:
    """
    On startup, find any prior ACCEPTED orders whose monitor thread died
    with a previous daemon (so result is still PENDING in the backend).
    For each:
      • If position still open on MT5 → respawn monitor_position thread
      • If position closed → look up close deal, POST CLOSED with outcome

    Self-healing: a single restart no longer loses trade outcomes forever.
    """
    try:
        r = session.get(api("/unresolved-fills"), timeout=15)
        if not r.ok:
            log.warning("reconcile: /unresolved-fills HTTP %d", r.status_code)
            return
        orphans = (r.json().get("data") or {}).get("orphans") or []
    except Exception as exc:
        log.warning("reconcile: fetch failed: %s", exc)
        return

    if not orphans:
        log.info("reconcile: no orphaned ACCEPTED orders — clean state")
        return

    log.info("reconcile: %d orphaned ACCEPTED order(s) found", len(orphans))
    for o in orphans:
        oid    = o["id"]
        ticket = o.get("ticket")
        if not ticket:
            log.debug("reconcile: order %d has no MT5 ticket, skipping", oid)
            continue

        direction = o["signal"]
        entry     = float(o["entry"])
        sl        = float(o["stop_loss"])
        tp1       = float(o["take_profit"])
        tp2       = float(o.get("take_profit_2") or o["take_profit"])

        # Is the position still open on MT5?
        try:
            positions = mt5.positions_get(ticket=ticket) or []
        except Exception as exc:
            log.warning("reconcile #%d ticket %s: positions_get failed: %s", oid, ticket, exc)
            continue

        broker_sym = _resolve_broker_symbol() or "XAUUSD"

        if positions:
            # Open → respawn the monitor thread (recovers MFE/MAE tracking
            # from this point forward, though peak excursion before restart is lost)
            log.info("reconcile: ticket %s still OPEN — respawning monitor", ticket)
            t = threading.Thread(
                target=monitor_position,
                kwargs=dict(
                    order_id=oid, ticket=int(ticket), broker_sym=broker_sym,
                    direction=direction, entry=entry, sl=sl, tp1=tp1, tp2=tp2,
                ),
                name=f"monitor-resumed-{ticket}",
                daemon=True,
            )
            t.start()
            continue

        # Closed → reconstruct outcome from deal history and report it
        log.info("reconcile: ticket %s CLOSED — reconstructing outcome", ticket)
        try:
            _report_closed(
                order_id=oid, ticket=int(ticket), broker_sym=broker_sym,
                direction=direction, entry=entry,
                mfe_pts=0.0, mae_pts=0.0,    # lost — couldn't track during outage
                lifetime_exceeded=False,
            )
        except Exception as exc:
            log.warning("reconcile: report_closed for ticket %s failed: %s", ticket, exc)


# ── MT5 candle push (daemon → droplet) ───────────────────────────────────────
#
# Every CANDLE_PUSH_SEC (default 30s) we call mt5.copy_rates_from_pos() for
# each timeframe and POST the batch to the droplet at /candles/receive.
# This makes MT5 the free-tier PRIMARY candle source (not TradingView).
# Read-only — no trade capability. Runs alongside the pending-order poller.

MT5_SYMBOL       = env("MT5_SYMBOL", "XAUUSD")
CANDLE_PUSH_SEC  = int(env("CANDLE_PUSH_SEC", "30"))
CANDLE_LOOKBACK  = int(env("CANDLE_LOOKBACK", "200"))

# MT5 timeframe constants — map string labels to mt5 module attributes
_MT5_TF_MAP: dict[str, int] = {}
def _build_tf_map():
    """Lazily build the MT5 timeframe map after mt5 module imports."""
    global _MT5_TF_MAP
    if _MT5_TF_MAP:
        return
    try:
        _MT5_TF_MAP = {
            "M5":  mt5.TIMEFRAME_M5,
            "M15": mt5.TIMEFRAME_M15,
            "H1":  mt5.TIMEFRAME_H1,
            "H4":  mt5.TIMEFRAME_H4,
            "D1":  mt5.TIMEFRAME_D1,
        }
    except Exception as exc:
        log.warning("Failed to build MT5 timeframe map: %s", exc)


def push_candles() -> None:
    """
    Fetch and POST OHLCV batches for M5/M15/H1/H4/D1 to the droplet.
    Timestamps are converted to UTC before posting so the backend can trust them.
    Fails silent — one TF failing doesn't stop the others.
    """
    _build_tf_map()
    if not _MT5_TF_MAP:
        return
    from datetime import timezone as _tz
    for tf_str, tf_int in _MT5_TF_MAP.items():
        try:
            rates = mt5.copy_rates_from_pos(MT5_SYMBOL, tf_int, 0, CANDLE_LOOKBACK)
            if rates is None or len(rates) == 0:
                log.debug("push_candles %s %s: no rates", MT5_SYMBOL, tf_str)
                continue
            payload_candles = []
            for r in rates:
                # r is a numpy structured array row: time,open,high,low,close,tick_volume,spread,real_volume
                ts_epoch = int(r["time"])
                ts_iso = datetime.fromtimestamp(ts_epoch, tz=_tz.utc).isoformat()
                payload_candles.append({
                    "time":         ts_iso,
                    "open":         float(r["open"]),
                    "high":         float(r["high"]),
                    "low":          float(r["low"]),
                    "close":        float(r["close"]),
                    "tick_volume":  int(r["tick_volume"]),
                    "spread":       int(r["spread"]) if "spread" in r.dtype.names else None,
                    "real_volume":  int(r["real_volume"]) if "real_volume" in r.dtype.names else None,
                })
            body = {
                "symbol":     MT5_SYMBOL,
                "timeframe":  tf_str,
                "count":      len(payload_candles),
                "candles":    payload_candles,
            }
            r = session.post(api("/candles/receive"), json=body, timeout=15)
            if not r.ok:
                log.warning("push_candles %s: HTTP %s %s",
                              tf_str, r.status_code, (r.text or "")[:120])
        except Exception as exc:
            log.warning("push_candles %s failed: %s", tf_str, exc)


def main():
    log.info("MT5 Bridge Daemon starting")
    log.info("Daemon ID: %s", DAEMON_ID)
    log.info("Dashboard: %s", DASHBOARD_URL)
    log.info("Poll interval: %ds  ·  Heartbeat: %ds  ·  Candle push: %ds",
             POLL_SEC, HEARTBEAT_SEC, CANDLE_PUSH_SEC)

    if not mt5_init():
        log.error("Fatal: MT5 connect failed. Exiting.")
        sys.exit(1)

    # Self-healing: pick up any orphaned ACCEPTED trades from a prior daemon
    # whose monitor thread died on restart. Recovers trade outcomes that
    # would otherwise be lost forever.
    reconcile_orphaned_trades()

    # Track A — research-only MT5 tick capture. Optional; falls back cleanly
    # if the module is missing or errors. NOT read by any trading logic.
    try:
        from mt5_ticks_extension import push_ticks as push_ticks_research
        from mt5_ticks_extension import TICK_PUSH_SEC as _TICK_PUSH_SEC
        _tick_capture_enabled = True
        log.info("[track-A] MT5 tick capture ENABLED (every %.1fs)", _TICK_PUSH_SEC)
    except Exception as _exc:
        push_ticks_research = None
        _TICK_PUSH_SEC = 999999
        _tick_capture_enabled = False
        log.info("[track-A] MT5 tick capture disabled: %s", _exc)

    last_heartbeat = 0
    last_candle_push = 0
    last_tick_push = 0
    try:
        while _running:
            now = time.time()

            # Periodic heartbeat
            if now - last_heartbeat >= HEARTBEAT_SEC:
                heartbeat()
                last_heartbeat = now

            # Periodic candle push — feeds droplet historical_candles from MT5
            if now - last_candle_push >= CANDLE_PUSH_SEC:
                push_candles()
                last_candle_push = now

            # Track A tick push — research-only; wrapped so any failure
            # leaves the rest of the loop untouched.
            if _tick_capture_enabled and now - last_tick_push >= _TICK_PUSH_SEC:
                try:
                    push_ticks_research(mt5, session, api, log,
                                          account=str(MT5_LOGIN))
                except Exception as _texc:
                    log.warning("[track-A] tick push loop error: %s", _texc)
                last_tick_push = now

            # Periodic reconcile sweep (every 15 min) — catches orphans
            # that monitor threads dropped mid-run.
            maybe_periodic_reconcile()

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
