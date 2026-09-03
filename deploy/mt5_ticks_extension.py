"""
Track A — MT5 tick capture extension for mt5_bridge_daemon.py.

This module is imported by mt5_bridge_daemon.py and adds:
  - push_ticks() — retrieves all ticks since a persistent cursor and POSTs them
  - persistent cursor file (survives daemon / laptop / MT5 restarts)
  - gap detection + reporting when a range cannot be recovered

Research-only. No production strategy consumes this data. Fields are stored
verbatim; no derived metrics are computed here.

DESIGN NOTES
------------
1. Complete stream capture — uses `mt5.copy_ticks_from(symbol, from_ms,
   count=100_000, flags=mt5.COPY_TICKS_ALL)`. This returns EVERY broker tick
   in the requested range up to the count limit, not a snapshot. Repeatedly
   called until fewer than count ticks come back — that means we caught up.

2. Cursor persistence — after every successful POST we write the max
   `time_msc` we pushed to a small JSON file on disk. On restart the daemon
   reads it and resumes from `cursor + 1 ms`.

3. Gap reporting — if `copy_ticks_from` returns None (MT5 disconnect) OR
   returns 0 records for a window that ends more than 30 s in the past
   (broker gap), we POST an explicit gap notice to the droplet so analysis
   code knows to exclude that window.

4. Raw only — bid / ask / last / volume_real / flags copied field-for-field
   from the numpy struct. No aggregator classification, no delta, no CVD,
   nothing derived inside ingestion.

5. Quote vs trade — the `flags` field is preserved as-is. Distinguishing a
   quote update from an actual trade is a QUERY-TIME concern:
       trades:   (flags & 0x08) != 0     # TICK_FLAG_LAST
       quotes:   (flags & 0x08) == 0

6. Independence — this module is optional. If the tick capture code errors
   at import or runtime, the rest of the daemon (candle push, trade
   execution) continues untouched.
"""
from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime, timezone
from typing import Optional

log = logging.getLogger("mt5_bridge.ticks")

# ── configuration (env-overridable) ──────────────────────────────────────────
TICK_SYMBOL       = os.getenv("MT5_TICK_SYMBOL", os.getenv("MT5_SYMBOL", "XAUUSD"))
TICK_PUSH_SEC     = float(os.getenv("MT5_TICK_PUSH_SEC", "3.0"))
TICK_BATCH_MAX    = int(os.getenv("MT5_TICK_BATCH_MAX", "5000"))
TICK_PULL_MAX     = int(os.getenv("MT5_TICK_PULL_MAX", "100000"))
TICK_CURSOR_FILE  = os.getenv("MT5_TICK_CURSOR_FILE",
                                os.path.join(os.path.dirname(__file__),
                                             ".mt5_tick_cursor.json"))
TICK_GAP_STALE_S  = int(os.getenv("MT5_TICK_GAP_STALE_S", "60"))
TICK_ENABLED      = os.getenv("MT5_TICK_ENABLED", "true").lower() in ("1", "true", "yes")

# ── cursor persistence ───────────────────────────────────────────────────────

def _load_cursor(symbol: str) -> Optional[int]:
    try:
        with open(TICK_CURSOR_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return int(data.get(symbol, None)) if data.get(symbol) else None
    except (FileNotFoundError, json.JSONDecodeError, ValueError, TypeError):
        return None
    except Exception as exc:
        log.warning("tick cursor load failed: %s", exc)
        return None


def _save_cursor(symbol: str, time_msc: int) -> None:
    try:
        data = {}
        if os.path.exists(TICK_CURSOR_FILE):
            try:
                with open(TICK_CURSOR_FILE, "r", encoding="utf-8") as f:
                    data = json.load(f)
            except Exception:
                data = {}
        data[symbol] = int(time_msc)
        data["_updated_at"] = datetime.now(timezone.utc).isoformat()
        tmp = TICK_CURSOR_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, TICK_CURSOR_FILE)
    except Exception as exc:
        log.warning("tick cursor save failed: %s", exc)


# ── main tick worker ─────────────────────────────────────────────────────────

def push_ticks(mt5, session, api, log_parent, account: str = "unknown") -> None:
    """
    Retrieve broker ticks since the persistent cursor and POST them.

    Called every TICK_PUSH_SEC by the daemon's main loop. Fully wrapped in
    try/except so any failure is logged and does not disturb the parent
    daemon's other duties (candle push, trade execution, heartbeats).

    Parameters
    ----------
    mt5      : the MetaTrader5 module (already initialized by the daemon)
    session : requests.Session with auth headers already set up
    api      : callable that returns the fully-qualified droplet URL for a path
    log_parent: parent daemon's logger (used for cross-referenceable log lines)
    account  : MT5 login id string, for provenance
    """
    if not TICK_ENABLED:
        return

    try:
        cursor = _load_cursor(TICK_SYMBOL)

        # First run: seed with "now - 5 minutes" so we don't pull the entire
        # broker history and DoS ourselves.
        if cursor is None:
            seed_dt = datetime.now(timezone.utc)
            cursor = int(seed_dt.timestamp() * 1000) - 5 * 60 * 1000
            log.info("tick cursor seeded to %d ms (5 min ago)", cursor)

        from_dt = datetime.fromtimestamp(cursor / 1000.0, tz=timezone.utc)
        # copy_ticks_from wants a datetime in UTC — MT5 will treat it as UTC
        # because the terminal is set to UTC. If the terminal is set to a
        # different timezone the daemon should surface that as a separate
        # issue — tick_time_msc from MT5 is always UTC epoch ms regardless.
        raw = mt5.copy_ticks_from(TICK_SYMBOL, from_dt, TICK_PULL_MAX,
                                   mt5.COPY_TICKS_ALL)

        # None ≠ [] ≠ zero-length ndarray — treat all three as distinct
        if raw is None:
            _report_gap(session, api, TICK_SYMBOL, cursor,
                        int(datetime.now(timezone.utc).timestamp() * 1000),
                        reason="mt5_returned_none",
                        detail="copy_ticks_from returned None; "
                                "possible broker disconnect")
            return

        n = len(raw)
        if n == 0:
            # No ticks in [cursor, now]. If cursor is far in the past we may
            # have a genuine broker outage — mark a gap once it stays stale.
            now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
            if now_ms - cursor > TICK_GAP_STALE_S * 1000:
                _report_gap(session, api, TICK_SYMBOL, cursor, now_ms,
                            reason="mt5_returned_empty",
                            detail=f"no ticks in {(now_ms-cursor)/1000:.0f}s window")
                # Advance cursor so we don't re-report the same gap
                _save_cursor(TICK_SYMBOL, now_ms)
            return

        # Build batch payload — verbatim broker fields
        max_msc = int(raw[-1]["time_msc"])
        ticks_payload = []
        for r in raw:
            t_msc = int(r["time_msc"])
            if t_msc <= cursor:
                continue    # already have this one
            flags = int(r["flags"]) if "flags" in r.dtype.names else 0
            rec = {
                "time_msc":    t_msc,
                "bid":         float(r["bid"]),
                "ask":         float(r["ask"]),
                "flags":       flags,
            }
            # only include last / volume_real when broker actually populated them
            if "last" in r.dtype.names:
                last_val = float(r["last"])
                if last_val != 0.0:
                    rec["last"] = last_val
            if "volume_real" in r.dtype.names:
                vol_real = float(r["volume_real"])
                if vol_real != 0.0:
                    rec["volume_real"] = vol_real
            ticks_payload.append(rec)

        if not ticks_payload:
            return

        # POST in chunks of TICK_BATCH_MAX
        for i in range(0, len(ticks_payload), TICK_BATCH_MAX):
            chunk = ticks_payload[i:i + TICK_BATCH_MAX]
            body = {
                "symbol":  TICK_SYMBOL,
                "broker":  os.getenv("MT5_BROKER", "exness"),
                "account": account,
                "count":   len(chunk),
                "ticks":   chunk,
            }
            try:
                r = session.post(api("/ticks/receive"), json=body, timeout=15)
                if not r.ok:
                    log.warning("tick push HTTP %s %s", r.status_code,
                                 (r.text or "")[:120])
                    return  # keep cursor unchanged → will retry next tick
            except Exception as exc:
                log.warning("tick push failed: %s", exc)
                return

        _save_cursor(TICK_SYMBOL, max_msc)
        log.debug("tick push: %d ticks up to msc=%d", len(ticks_payload), max_msc)

    except Exception as exc:
        # Never let this crash the daemon
        log.warning("push_ticks unexpected error: %s", exc)


def _report_gap(session, api, symbol: str, start_msc: int, end_msc: int,
                reason: str, detail: Optional[str] = None) -> None:
    try:
        body = {
            "symbol":    symbol,
            "start_msc": int(start_msc),
            "end_msc":   int(end_msc),
            "reason":    reason,
        }
        if detail:
            body["detail"] = detail
        r = session.post(api("/ticks/gap"), json=body, timeout=10)
        if r.ok:
            log.warning("tick gap reported: %s %d→%d (%s)",
                         symbol, start_msc, end_msc, reason)
        else:
            log.warning("tick gap report HTTP %s", r.status_code)
    except Exception as exc:
        log.warning("tick gap report failed: %s", exc)
