"""
Track A — MT5 tick capture extension for mt5_bridge_daemon.py.

Retrieves broker ticks via mt5.copy_ticks_from(symbol, from_time, count,
COPY_TICKS_ALL) and POSTs them to the droplet for research-only storage.

CURSOR SAFETY (rev-2, 2026-09-03)
─────────────────────────────────
There are exactly TWO facts a cursor may reflect:
  (1) "server has confirmed persistence up to msc=X"     ← authoritative
  (2) "daemon has inspected up to msc=X"                 ← non-authoritative

We persist only (1) to disk. The authoritative cursor advances ONLY after
the server's POST response confirms the batch was committed. On timeout,
5xx, connection failure or invalid response, the cursor is not touched
and the same window will be re-fetched next cycle. Server-side dedup
(unique (symbol, content_hash)) makes replay safe.

An empty return from copy_ticks_from is NOT proof the interval had no
ticks. Before advancing across an empty interval we classify the state:

  VERIFIED_NO_MARKET_TICKS   → advance safely (market open, terminal ok,
                               symbol ok, non-zero ticks recently)
  MARKET_CLOSED              → advance in bounded steps (max 1 h at a time)
  MT5_TEMPORARILY_UNAVAILABLE→ do NOT advance; retry
  TERMINAL_DISCONNECTED      → do NOT advance; retry
  SYMBOL_UNAVAILABLE         → do NOT advance; retry
  API_ERROR                  → do NOT advance; retry
  UNKNOWN_EMPTY_RESPONSE     → do NOT advance; retry; log

Only VERIFIED_NO_MARKET_TICKS and MARKET_CLOSED advance the cursor without
a POST. Everything else preserves it for later recovery.

Research-only. No production strategy consumes this data.
"""
from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime, timezone
from typing import Optional

log = logging.getLogger("mt5_bridge.ticks")

# ── configuration ────────────────────────────────────────────────────────────
TICK_SYMBOL       = os.getenv("MT5_TICK_SYMBOL", os.getenv("MT5_SYMBOL", "XAUUSD"))
TICK_PUSH_SEC     = float(os.getenv("MT5_TICK_PUSH_SEC", "3.0"))
TICK_BATCH_MAX    = int(os.getenv("MT5_TICK_BATCH_MAX", "5000"))
TICK_PULL_MAX     = int(os.getenv("MT5_TICK_PULL_MAX", "100000"))
TICK_CURSOR_FILE  = os.getenv("MT5_TICK_CURSOR_FILE",
                                os.path.join(os.path.dirname(__file__),
                                             ".mt5_tick_cursor.json"))
# Max time we advance the cursor across a market-closed interval in a
# single step. Keeps us honest about when we "confirmed" a window.
MAX_EMPTY_ADVANCE_MS = int(os.getenv("MT5_TICK_MAX_EMPTY_ADVANCE_MS", str(60 * 60 * 1000)))
# Grace after last confirmed tick before we start classifying empty windows
EMPTY_GRACE_MS       = int(os.getenv("MT5_TICK_EMPTY_GRACE_MS", "30000"))
TICK_ENABLED         = os.getenv("MT5_TICK_ENABLED", "true").lower() in ("1", "true", "yes")


# ── health state (in-memory; also surfaced by the daemon later) ──────────────
_HEALTH = {
    "last_state":            None,   # last classification of empty return
    "last_state_at":         None,   # when
    "consec_empty":          0,      # consecutive empty windows
    "last_confirmed_msc":    None,   # last msc server confirmed persistence for
    "last_confirmed_at":     None,
    "last_ticks_pushed":     0,
    "posts_ok":              0,
    "posts_failed":          0,
    "gaps_reported":         0,
}


def health_snapshot() -> dict:
    return dict(_HEALTH)


# ── cursor persistence ───────────────────────────────────────────────────────

def _load_cursor(symbol: str) -> Optional[int]:
    """Reads the confirmed-persisted cursor for a symbol; None if unset."""
    try:
        with open(TICK_CURSOR_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        v = data.get(symbol)
        return int(v) if v is not None else None
    except (FileNotFoundError, json.JSONDecodeError, ValueError, TypeError):
        return None
    except Exception as exc:
        log.warning("tick cursor load failed: %s", exc)
        return None


def _save_cursor(symbol: str, time_msc: int, reason: str) -> None:
    """
    Persists the CONFIRMED cursor (server acknowledged). Reason is stored
    so post-hoc audit can tell why the cursor moved (server_ack, market_closed,
    verified_no_ticks). Never call from unconfirmed code paths.
    """
    try:
        data = {}
        if os.path.exists(TICK_CURSOR_FILE):
            try:
                with open(TICK_CURSOR_FILE, "r", encoding="utf-8") as f:
                    data = json.load(f)
            except Exception:
                data = {}
        data[symbol] = int(time_msc)
        data[f"{symbol}_last_advance_reason"] = reason
        data["_updated_at"] = datetime.now(timezone.utc).isoformat()
        tmp = TICK_CURSOR_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, TICK_CURSOR_FILE)
    except Exception as exc:
        log.warning("tick cursor save failed: %s", exc)


# ── empty-response classification ────────────────────────────────────────────

def _classify_empty(mt5, symbol: str, cursor_ms: int) -> str:
    """
    Return one of:
      VERIFIED_NO_MARKET_TICKS
      MARKET_CLOSED
      MT5_TEMPORARILY_UNAVAILABLE
      TERMINAL_DISCONNECTED
      SYMBOL_UNAVAILABLE
      API_ERROR
      UNKNOWN_EMPTY_RESPONSE

    Only VERIFIED_NO_MARKET_TICKS + MARKET_CLOSED are safe cursor-advance states.
    """
    try:
        ti = mt5.terminal_info()
        if ti is None:
            return "TERMINAL_DISCONNECTED"
        if getattr(ti, "connected", False) is False:
            return "TERMINAL_DISCONNECTED"

        si = mt5.symbol_info(symbol)
        if si is None:
            return "SYMBOL_UNAVAILABLE"
        # visible=False can mean broker disabled the symbol
        if getattr(si, "visible", True) is False:
            return "SYMBOL_UNAVAILABLE"

        # Live probe — does symbol_info_tick return something plausible?
        live = mt5.symbol_info_tick(symbol)
        if live is None:
            return "MT5_TEMPORARILY_UNAVAILABLE"

        # If the LIVE tick is older than the cursor + a small grace,
        # the market is effectively quiet — but we cannot conclude
        # "no ticks in interval" without more evidence. Only assert
        # "verified no market ticks" when the live tick's msc is
        # strictly greater than cursor+grace AND we still got zero
        # rows from copy_ticks_from — that means the broker sees a
        # newer tick than the cursor but historical returned empty,
        # which implies the historical window was genuinely quiet.
        live_msc = int(getattr(live, "time_msc", 0) or 0)
        if live_msc > cursor_ms + EMPTY_GRACE_MS:
            return "VERIFIED_NO_MARKET_TICKS"

        # Weekend / holiday heuristic: use UTC weekday.
        # CME Globex Gold electronic session: 22:00 UTC Sun → 21:00 UTC Fri
        # with a daily 60 min break at 21:00-22:00 UTC.
        now = datetime.now(timezone.utc)
        wd = now.weekday()  # Mon=0, Sun=6
        hr = now.hour
        # weekend closed
        if wd == 5:  # Saturday
            return "MARKET_CLOSED"
        if wd == 6 and hr < 22:  # Sunday before session open
            return "MARKET_CLOSED"
        if wd == 4 and hr >= 21:  # Friday after session close
            return "MARKET_CLOSED"
        # Daily maintenance window
        if 21 <= hr < 22:
            return "MARKET_CLOSED"

        return "UNKNOWN_EMPTY_RESPONSE"

    except Exception as exc:
        log.warning("_classify_empty error: %s", exc)
        return "API_ERROR"


# ── main tick worker ─────────────────────────────────────────────────────────

def push_ticks(mt5, session, api, log_parent, account: str = "unknown") -> None:
    """
    One iteration. Called every TICK_PUSH_SEC by the daemon's main loop.

    Cursor advances ONLY when:
      (a) server acknowledges persistence of a non-empty batch, OR
      (b) empty return is classified VERIFIED_NO_MARKET_TICKS or
          MARKET_CLOSED (bounded step).

    In every other empty case the cursor stays put and we retry.
    """
    if not TICK_ENABLED:
        return

    try:
        cursor = _load_cursor(TICK_SYMBOL)
        first_run = cursor is None
        if first_run:
            # Seed with "now - 5 minutes" so we don't pull an unbounded history
            cursor = int(datetime.now(timezone.utc).timestamp() * 1000) - 5 * 60 * 1000
            log.info("tick cursor seeded to %d ms (5 min ago)", cursor)

        # copy_ticks_from wants a datetime; MT5 interprets it as UTC.
        # We ask for cursor+1 ms so we don't refetch the exact confirmed msc.
        from_dt = datetime.fromtimestamp((cursor + 1) / 1000.0, tz=timezone.utc)
        raw = mt5.copy_ticks_from(TICK_SYMBOL, from_dt, TICK_PULL_MAX,
                                   mt5.COPY_TICKS_ALL)

        # Distinguish None (API error) vs zero-length (empty window)
        if raw is None:
            _HEALTH["last_state"] = "MT5_TEMPORARILY_UNAVAILABLE"
            _HEALTH["last_state_at"] = datetime.now(timezone.utc).isoformat()
            _HEALTH["consec_empty"] += 1
            _report_gap(session, api, TICK_SYMBOL, cursor,
                        int(datetime.now(timezone.utc).timestamp() * 1000),
                        reason="mt5_returned_none",
                        detail="copy_ticks_from returned None")
            _HEALTH["gaps_reported"] += 1
            return

        n = len(raw)
        if n == 0:
            state = _classify_empty(mt5, TICK_SYMBOL, cursor)
            _HEALTH["last_state"] = state
            _HEALTH["last_state_at"] = datetime.now(timezone.utc).isoformat()
            _HEALTH["consec_empty"] += 1

            now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)

            if state == "VERIFIED_NO_MARKET_TICKS":
                # Advance ONLY to the live tick's msc (not past it),
                # bounded — this is the safe form of "the interval
                # was genuinely empty."
                live = mt5.symbol_info_tick(TICK_SYMBOL)
                live_msc = int(getattr(live, "time_msc", cursor) or cursor)
                new_cursor = min(live_msc - 1, cursor + MAX_EMPTY_ADVANCE_MS)
                if new_cursor > cursor:
                    _save_cursor(TICK_SYMBOL, new_cursor,
                                    "verified_no_market_ticks")
                return

            if state == "MARKET_CLOSED":
                # Bounded step; we never leap the cursor across an
                # arbitrary interval, so a subsequent market re-open
                # never leaves an under-inspected window.
                new_cursor = min(now_ms - EMPTY_GRACE_MS,
                                    cursor + MAX_EMPTY_ADVANCE_MS)
                if new_cursor > cursor:
                    _save_cursor(TICK_SYMBOL, new_cursor, "market_closed")
                return

            # Any other classification: DO NOT advance. Retry next cycle.
            # Record a gap if we're stale > EMPTY_GRACE_MS so analysis
            # code can respect it.
            if now_ms - cursor > EMPTY_GRACE_MS:
                _report_gap(session, api, TICK_SYMBOL, cursor, now_ms,
                            reason=state.lower(),
                            detail=(f"empty response classified as {state}; "
                                    f"cursor unchanged for retry"))
                _HEALTH["gaps_reported"] += 1
            return

        # Non-empty result → build the payload with only ticks strictly newer
        # than the confirmed cursor, then POST and advance only on server ACK.
        max_msc_in_batch = int(raw[-1]["time_msc"])
        ticks_payload = []
        for r in raw:
            t_msc = int(r["time_msc"])
            if t_msc <= cursor:
                continue
            flags = int(r["flags"]) if "flags" in r.dtype.names else 0
            rec = {
                "time_msc": t_msc,
                "bid":      float(r["bid"]),
                "ask":      float(r["ask"]),
                "flags":    flags,
            }
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
            # Ticks came back but all were <= cursor — nothing new
            _HEALTH["last_state"] = "NO_NEW_TICKS_ABOVE_CURSOR"
            return

        # POST in bounded chunks. Cursor advances chunk-by-chunk on ACK.
        chunks_ok = 0
        chunks_failed = 0
        confirmed_msc_this_cycle = cursor
        for i in range(0, len(ticks_payload), TICK_BATCH_MAX):
            chunk = ticks_payload[i:i + TICK_BATCH_MAX]
            chunk_max_msc = max(t["time_msc"] for t in chunk)
            body = {
                "symbol":  TICK_SYMBOL,
                "broker":  os.getenv("MT5_BROKER", "exness"),
                "account": account,
                "count":   len(chunk),
                "ticks":   chunk,
            }
            try:
                r = session.post(api("/ticks/receive"), json=body, timeout=15)
            except Exception as exc:
                chunks_failed += 1
                _HEALTH["posts_failed"] += 1
                log.warning("tick push transport error: %s", exc)
                # STOP advancing on transport error — later chunks
                # would be out-of-order commits. Break and retry
                # everything above the current confirmed msc next cycle.
                break
            if not r.ok:
                chunks_failed += 1
                _HEALTH["posts_failed"] += 1
                log.warning("tick push HTTP %s %s", r.status_code,
                             (r.text or "")[:120])
                break
            # Confirm server-side commit before advancing
            try:
                body_json = r.json()
                data = body_json.get("data", {}) if isinstance(body_json, dict) else {}
                latest_ack = data.get("latest_msc")
                # If server gives us a latest_msc we trust that. Otherwise
                # trust the chunk_max_msc since HTTP 2xx means commit.
                if isinstance(latest_ack, int) and latest_ack >= chunk_max_msc:
                    confirmed_msc_this_cycle = latest_ack
                else:
                    confirmed_msc_this_cycle = chunk_max_msc
            except Exception:
                # No parseable body — HTTP 2xx alone is our commit signal
                confirmed_msc_this_cycle = chunk_max_msc
            chunks_ok += 1
            _HEALTH["posts_ok"] += 1

        if confirmed_msc_this_cycle > cursor:
            _save_cursor(TICK_SYMBOL, confirmed_msc_this_cycle, "server_ack")
            _HEALTH["last_confirmed_msc"] = confirmed_msc_this_cycle
            _HEALTH["last_confirmed_at"]  = datetime.now(timezone.utc).isoformat()
            _HEALTH["last_ticks_pushed"]  = len(ticks_payload)
            _HEALTH["consec_empty"]       = 0
            log.info("tick push: %d ticks, %d/%d chunks OK, cursor→%d",
                       len(ticks_payload), chunks_ok, chunks_ok + chunks_failed,
                       confirmed_msc_this_cycle)
        else:
            log.warning("tick push: no chunks confirmed — cursor unchanged at %d",
                         cursor)

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
