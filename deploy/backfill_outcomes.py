"""
Standalone one-shot backfill — outcomes for orphaned ACCEPTED fills.
====================================================================

The running daemon's reconciler has been failing to back-fill outcomes
for trades that closed while no monitor thread was alive. This script
does the same job manually, idempotently, without restarting the daemon.

For each PendingExecution still tagged ACCEPTED (no CLOSED writeback):
  1. Query MT5 for deals matching that position_id (30-day window)
  2. Find the closing deal (entry == DEAL_ENTRY_OUT)
  3. Classify outcome from realized profit (WIN / LOSS / BREAKEVEN)
  4. POST /api/v1/bridge/result/{id} with status=CLOSED + outcome

Usage (PowerShell):
    cd C:\\Users\\anwar.mohamed\\eurusd-dashboard\\deploy
    python backfill_outcomes.py

The script prints every step. Re-runnable: orphans already back-filled
just disappear from the /unresolved-fills list, so subsequent runs are
no-ops.

Requires (already installed for the daemon):
    pip install requests MetaTrader5
"""
from __future__ import annotations

import logging
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path

LOG_FORMAT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"
logging.basicConfig(level=logging.INFO, format=LOG_FORMAT)
log = logging.getLogger("backfill")


# ── Load .env.bridge (same as the daemon) ────────────────────────────────────

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
        out[k.strip()] = v.strip().strip('"').strip("'")
    return out


SCRIPT_DIR = Path(__file__).resolve().parent
ENV = _load_env_file(SCRIPT_DIR / ".env.bridge")


def env(key: str, default: str | None = None) -> str | None:
    return os.environ.get(key) or ENV.get(key) or default


DASHBOARD_URL = (env("DASHBOARD_URL") or "").rstrip("/")
BRIDGE_SECRET = env("BRIDGE_SECRET", "")
MT5_LOGIN     = env("MT5_LOGIN", "")
MT5_PASSWORD  = env("MT5_PASSWORD", "")
MT5_SERVER    = env("MT5_SERVER", "")

if not DASHBOARD_URL or not BRIDGE_SECRET:
    log.error("DASHBOARD_URL and BRIDGE_SECRET required in deploy/.env.bridge")
    sys.exit(2)


# ── HTTP + MT5 ───────────────────────────────────────────────────────────────

import requests
session = requests.Session()
session.headers.update({
    "X-Bridge-Secret":    BRIDGE_SECRET,
    "X-Bridge-Daemon-Id": "backfill-script",
    "Content-Type":       "application/json",
})

try:
    import MetaTrader5 as mt5
except ImportError:
    log.error("MetaTrader5 not installed. Run: pip install MetaTrader5")
    sys.exit(2)


def mt5_init() -> bool:
    kwargs = {}
    if MT5_LOGIN:    kwargs["login"]    = int(MT5_LOGIN)
    if MT5_PASSWORD: kwargs["password"] = MT5_PASSWORD
    if MT5_SERVER:   kwargs["server"]   = MT5_SERVER
    if not mt5.initialize(**kwargs):
        log.error("mt5.initialize failed: %s", mt5.last_error())
        return False
    acc = mt5.account_info()
    log.info("MT5 connected: login=%s server=%s balance=%.2f",
             acc.login, acc.server, acc.balance)
    return True


# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    log.info("=" * 60)
    log.info(" XAUUSD ORPHAN BACKFILL")
    log.info("=" * 60)

    if not mt5_init():
        sys.exit(1)

    # 1. Get the orphan list from the backend
    log.info("Fetching /unresolved-fills ...")
    r = session.get(f"{DASHBOARD_URL}/api/v1/bridge/unresolved-fills", timeout=15)
    if not r.ok:
        log.error("HTTP %d from /unresolved-fills: %s", r.status_code, r.text[:200])
        mt5.shutdown()
        sys.exit(1)

    orphans = (r.json().get("data") or {}).get("orphans") or []
    log.info("Found %d orphaned ACCEPTED fills", len(orphans))
    if not orphans:
        log.info("Nothing to back-fill. Exiting clean.")
        mt5.shutdown()
        return

    # 2. Pull 30 days of deal history once (cheaper than per-ticket)
    log.info("Pulling 30-day MT5 deal history ...")
    date_from = datetime.now() - timedelta(days=30)
    date_to   = datetime.now() + timedelta(hours=1)
    all_deals = mt5.history_deals_get(date_from, date_to) or []
    log.info("Got %d deals from MT5", len(all_deals))

    # Index by position_id for fast lookup
    by_position: dict[int, list] = {}
    for d in all_deals:
        by_position.setdefault(d.position_id, []).append(d)
    log.info("Indexed %d unique positions", len(by_position))

    # 3. Walk each orphan and POST the outcome
    backfilled = 0
    still_open = 0
    not_found  = 0
    failed     = 0

    for o in orphans:
        oid       = o["id"]
        ticket    = o.get("ticket")
        direction = o["signal"]
        entry     = float(o["entry"])

        if not ticket:
            log.warning("PE#%d has no ticket — skipping", oid)
            continue

        deals = by_position.get(int(ticket), [])
        if not deals:
            log.warning("PE#%d ticket %s: no deals in history", oid, ticket)
            not_found += 1
            continue

        # Look for the closing leg (DEAL_ENTRY_OUT)
        closer = next((d for d in reversed(deals) if d.entry == mt5.DEAL_ENTRY_OUT), None)

        if closer is None:
            # Position likely still open (entry deal only, no exit yet)
            opener = deals[0]
            log.info("PE#%d ticket %s STILL OPEN (entry %s, no exit deal)",
                     oid, ticket, opener.price)
            still_open += 1
            continue

        # Classify outcome from realized profit
        close_price  = closer.price
        close_profit = closer.profit or 0.0
        move = (close_price - entry) if direction == "BUY" else (entry - close_price)
        pips_outcome = round(move, 2)

        if   close_profit > 0.05:   result_str = "WIN"
        elif close_profit < -0.05:  result_str = "LOSS"
        else:                        result_str = "BREAKEVEN"

        note = f"backfilled @ {close_price:.2f}  move={pips_outcome:+.2f}pts  profit=${close_profit:+.2f}"
        log.info("PE#%d ticket %s  %s  %s", oid, ticket, result_str, note)

        # POST the CLOSED report
        payload = {
            "status":          "CLOSED",
            "ticket":          int(ticket),
            "result":          result_str,
            "pips_outcome":    pips_outcome,
            "mfe_pts":         0.0,            # historical — lost
            "mae_pts":         0.0,            # historical — lost
            "rules_followed":  True,
            "post_trade_note": note + " (backfilled)",
        }
        try:
            rr = session.post(
                f"{DASHBOARD_URL}/api/v1/bridge/result/{oid}",
                json=payload, timeout=15,
            )
            if rr.ok:
                backfilled += 1
                log.info("    -> backend ACK")
            else:
                failed += 1
                log.error("    -> HTTP %d: %s", rr.status_code, rr.text[:200])
        except Exception as exc:
            failed += 1
            log.error("    -> POST failed: %s", exc)

    log.info("")
    log.info("=" * 60)
    log.info(" SUMMARY")
    log.info("=" * 60)
    log.info("  Back-filled:    %d", backfilled)
    log.info("  Still open:     %d  (position alive in MT5 — monitor must close it)", still_open)
    log.info("  Not in history: %d  (older than 30d, or wrong account)", not_found)
    log.info("  POST failed:    %d", failed)
    mt5.shutdown()


if __name__ == "__main__":
    main()
