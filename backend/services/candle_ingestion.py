"""
Candle Ingestion — Recurring Top-Up
====================================

Runs periodically to keep `historical_candles` fresh. Uses TwelveData
(same provider the live scanner already uses successfully) as the
primary source; falls back to TradingView if configured.

Why: the TradingView-only backfill (services/realdata_backfill.py)
depends on tvDatafeed sign-in which has been failing silently since
2026-05-26. Result: historical_candles went 2 months stale while the
live scanner (also TwelveData) kept working — creating a split-brain
where verdicts run on live ticks but any lookback feature reads May
data.

This module is:
  1. Idempotent — inserts are gated on unique (instrument, timeframe,
     candle_time); duplicates skip via IntegrityError.
  2. Bounded — pulls only N most-recent bars per call (default 200),
     enough to fill any gap up to a week without paying to re-fetch
     historical.
  3. Silent by design — errors log at WARN, never raise; the daily
     backfill_historical_candles() job still handles bulk history.
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

log = logging.getLogger(__name__)


# Records the most recent ingestion error string so the freshness sentinel
# can include a concrete root-cause hint in its Telegram alert (e.g.
# "invalid or expired API key" vs "rate limit exceeded" vs "TV timeout").
# Written on every failed fetch. Read only.
_last_ingest_error: dict = {"at": None, "pair": None, "tf": None, "message": ""}


def get_last_ingest_error() -> dict:
    """Latest fetch failure across timeframes. Empty dict fields if none."""
    return dict(_last_ingest_error)


# Timeframes to keep topped up + max fetch per pull.
# M5 added post-Pre-Phase-0 so directional intelligence has fresh execution
# refinement data. TwelveData free tier: 8 req/min → 5 TFs/cycle fits easily.
_TF_PLAN = [
    ("M5",  200),
    ("M15", 200),
    ("H1",  200),
    ("H4",  100),
    ("D1",   50),
]


def _twelvedata_symbol(pair: str) -> str:
    return {
        "xauusd":  "XAU/USD",
        "eurusd":  "EUR/USD",
        "gbpusd":  "GBP/USD",
    }.get(pair.lower(), pair.upper())


def _stored_instrument(pair: str) -> str:
    """Match the string historical_candles.instrument uses in the DB."""
    return _twelvedata_symbol(pair)


def _fetch_twelvedata(pair: str, interval: str, lookback: int) -> list:
    from services.candle_provider import get_twelvedata_candles
    sym = _twelvedata_symbol(pair)
    return get_twelvedata_candles(interval=interval, lookback=lookback,
                                    symbol=sym).candles


def _fetch_tradingview(pair: str, interval: str, lookback: int) -> list:
    """Fetch via TradingView (free, unlimited). Returns [] on failure."""
    from services.tradingview_provider import get_tv_candles
    r = get_tv_candles(pair, timeframe=interval, limit=lookback)
    return r or []


def _fetch_yahoo(pair: str, interval: str, lookback: int) -> list:
    """Fetch via Yahoo GC=F gold futures (free, unlimited). Returns [] on failure."""
    from services.yahoo_provider import get_yahoo_candles
    r = get_yahoo_candles(pair, timeframe=interval, limit=lookback)
    return r or []


# TV retry policy — the anonymous tvDatafeed session drops occasionally.
# Expanded backoffs after Sep-8 outage: 18h of MT5 downtime, zero TV fills.
# 5 attempts, cumulative wait ~ 32s per top-up cycle.
_TV_RETRY_BACKOFFS_S: list[float] = [1.0, 3.0, 8.0, 20.0]


def _yahoo_gc_basis_adjusted(pair: str, interval: str, lookback: int) -> Optional[list]:
    """Signal-survival fallback: Yahoo GC=F bars, basis-adjusted to spot-equivalent.

    P189 says do NOT retag GC as XAU. That rule stands for RAW data. This
    function computes a basis-adjusted spot proxy explicitly stamped
    source='yahoo_gc_proxy' so downstream can distinguish it from true spot.

    Basis = median(recent_gc_close - recent_xau_close) over the last 24h of
    historical_candles. If insufficient history exists, uses a $50 default
    (consistent with the 2026-09-04 Section 62 observation).

    Purpose: keep the signal path fresh when BOTH MT5 push and TradingView
    are unavailable. Prevents 18h signal blackouts like Sep 8-9 2026.
    """
    from services.yahoo_provider import get_yahoo_candles
    from datetime import datetime, timedelta
    try:
        candles = get_yahoo_candles(pair, timeframe=interval, lookback=lookback)
    except Exception as exc:
        log.debug("[candle_ingestion] Yahoo GC fetch exc: %s", exc)
        return None
    if not candles:
        return None

    # Compute basis from recent overlap of GC=F vs XAU/USD H1 closes.
    basis = 50.0    # default per Section 62 Sep-04-2026 observation
    try:
        from database import SessionLocal
        from sqlalchemy import text
        cutoff = (datetime.utcnow() - timedelta(hours=48)).strftime("%Y-%m-%d %H:%M:%S")
        with SessionLocal() as db:
            r = db.execute(text("""
                SELECT AVG(g.close - x.close) AS median_basis, COUNT(*) AS n
                FROM historical_candles g
                JOIN historical_candles x
                  ON g.candle_time = x.candle_time AND g.timeframe = x.timeframe
                WHERE g.instrument IN ('GC=F','gc_futures') AND x.instrument='XAU/USD'
                  AND g.timeframe='H1' AND g.candle_time >= :cut
                  AND x.source != 'yahoo_gc_proxy'
            """), {"cut": cutoff}).fetchone()
            if r and r[1] and r[1] >= 3:
                basis = float(r[0])
    except Exception as exc:
        log.debug("[candle_ingestion] basis calc exc: %s — using default $%.1f",
                    exc, basis)

    # Subtract basis so the stamped source's spot-equivalent price is a
    # rough approximation of XAU.
    adjusted = []
    for c in candles:
        if isinstance(c, dict):
            adj = dict(c)
            for k in ("open", "high", "low", "close"):
                if k in adj and adj[k] is not None:
                    adj[k] = float(adj[k]) - basis
            adjusted.append(adj)
        else:
            # Pydantic-model shape — take a plain-dict copy
            adjusted.append({
                "time": getattr(c, "time", None),
                "open": (getattr(c, "open", 0) or 0) - basis,
                "high": (getattr(c, "high", 0) or 0) - basis,
                "low":  (getattr(c, "low",  0) or 0) - basis,
                "close":(getattr(c, "close",0) or 0) - basis,
                "volume": getattr(c, "volume", 0) or 0,
            })
    log.info("[candle_ingestion] yahoo_gc_proxy: %d bars adjusted by basis=$%.2f",
              len(adjusted), basis)
    return adjusted


def _fetch_with_fallback(pair: str, interval: str,
                          lookback: int) -> tuple[list, str]:
    """
    Ingest fallback chain (updated 2026-09-09 after MT5 outage exposed
    single-point-of-failure risk):

      1. TradingView OANDA:XAUUSD   (spot, 5 attempts with backoffs)
      2. Yahoo GC=F basis-adjusted   (spot proxy, stamped 'yahoo_gc_proxy')
         Basis auto-computed from recent GC-vs-XAU overlap; default $50.

    Note: MT5 bars arrive via a separate PUSH from the laptop daemon at
    routers/bridge.py POST /candles/receive — they don't need to be pulled
    here. When the daemon is running, MT5 bars land with source='mt5' and
    win the freshness race naturally. When MT5 is down (laptop offline,
    ISP outage, endpoint filter), the two cloud sources above keep the
    signal path fresh so strategist doesn't blackout.

    Raises RuntimeError only when ALL providers are exhausted so the
    freshness sentinel gets a clear error text.
    """
    from services.tradingview_provider import invalidate_cache as _tv_invalidate

    last_exc: Optional[Exception] = None

    # 1. TradingView with expanded retries
    for attempt, backoff in enumerate([0.0] + _TV_RETRY_BACKOFFS_S):
        if backoff > 0:
            time.sleep(backoff)
            _tv_invalidate(pair)
        try:
            candles = _fetch_tradingview(pair, interval, lookback)
            if candles:
                return candles, "tradingview"
            last_exc = RuntimeError(
                f"TradingView empty for {pair} {interval} (attempt {attempt+1})"
            )
        except Exception as exc:
            last_exc = exc
            log.debug("[candle_ingestion] TV attempt %d for %s %s: %s",
                        attempt + 1, pair, interval, exc)

    # 2. Yahoo GC=F basis-adjusted spot proxy — signal survival only
    if pair.lower() == "xauusd":
        try:
            adjusted = _yahoo_gc_basis_adjusted(pair, interval, lookback)
            if adjusted:
                log.warning(
                    "[candle_ingestion] TV exhausted — falling back to "
                    "yahoo_gc_proxy for %s %s (%d bars). "
                    "Signal path preserved; strategist may see slightly "
                    "different spot price until TV/MT5 recovers.",
                    pair, interval, len(adjusted)
                )
                return adjusted, "yahoo_gc_proxy"
        except Exception as exc:
            log.warning("[candle_ingestion] yahoo_gc_proxy fallback failed: %s", exc)

    raise RuntimeError(
        f"All providers exhausted for {pair} {interval}: TV {last_exc}"
    )


def _field(c, name, default=None):
    """
    Read a candle field safely across Pydantic models AND plain dicts.

    Historical bug: the old form `getattr(c, name, c.get(name))` evaluated
    the DEFAULT arg first, which raises AttributeError on Pydantic v2 models
    (they have no `.get()` method) — so every candle was silently dropped
    at the bare `except Exception` in `_persist`. This helper avoids that.
    """
    if isinstance(c, dict):
        v = c.get(name)
        return default if v is None else v
    v = getattr(c, name, None)
    return default if v is None else v


def _persist(db: Session, pair: str, tf: str, candles: list,
              source: str = "twelvedata") -> dict:
    """Insert candles idempotently. Returns per-timeframe counts (with errors!)."""
    from db_models import HistoricalCandle
    inserted, skipped, errors = 0, 0, 0
    instrument = _stored_instrument(pair)
    for c in candles:
        try:
            ts = _field(c, "time")
            if isinstance(ts, str):
                ts = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            if ts is None:
                errors += 1
                if errors <= 3:
                    log.warning("[candle_ingestion] %s %s: candle has no time field: %r",
                                pair, tf, c)
                continue
            if not getattr(ts, "tzinfo", None):
                ts = ts.replace(tzinfo=timezone.utc)
            row = HistoricalCandle(
                instrument=instrument,
                timeframe=tf,
                candle_time=ts,
                open=float(_field(c, "open", 0.0)),
                high=float(_field(c, "high", 0.0)),
                low=float(_field(c, "low",  0.0)),
                close=float(_field(c, "close", 0.0)),
                volume=int(float(_field(c, "volume", 0)) or 0),
                source=source,
            )
            db.add(row)
            db.commit()
            inserted += 1
        except IntegrityError:
            db.rollback()
            skipped += 1
        except Exception as exc:
            db.rollback()
            errors += 1
            if errors <= 3:
                log.warning("[candle_ingestion] %s %s: insert failed: %s: %s",
                            pair, tf, type(exc).__name__, exc)
    return {"inserted": inserted, "skipped_duplicate": skipped, "errors": errors}


def top_up_recent(db: Session, pair: str = "xauusd",
                    lookback_override: Optional[int] = None,
                    only_timeframes: Optional[tuple] = None) -> dict:
    """
    Public entry point. Loops every TF (or only `only_timeframes` if given),
    top-ups, returns report.

    `only_timeframes` lets the scheduler run a fast loop for M5/M15 and a
    separate slow loop for HTFs so we stay within the TwelveData budget.
    """
    report = {
        "pair":      pair,
        "started":   datetime.now(timezone.utc).isoformat(),
        "totals":    {"inserted": 0, "skipped": 0, "errors": 0},
        "timeframes": {},
    }
    plan = _TF_PLAN if not only_timeframes else [
        (tf, n) for tf, n in _TF_PLAN if tf in only_timeframes
    ]
    for tf, n_default in plan:
        n = lookback_override or n_default
        t0 = time.time()
        try:
            candles, source = _fetch_with_fallback(pair, tf, n)
            if not candles:
                report["timeframes"][tf] = {"error": "empty response"}
                report["totals"]["errors"] += 1
                continue
            r = _persist(db, pair, tf, candles, source=source)
            r["fetched"] = len(candles)
            r["source"] = source
            r["elapsed_s"] = round(time.time() - t0, 2)
            report["timeframes"][tf] = r
            report["totals"]["inserted"] += r["inserted"]
            report["totals"]["skipped"]  += r["skipped_duplicate"]
            report["totals"]["errors"]   += r.get("errors", 0)
            if r["inserted"] or r.get("errors", 0):
                log.info("[candle_ingestion] %s %s [%s]: fetched=%d inserted=%d dup=%d errors=%d (%.2fs)",
                          pair, tf, source, r["fetched"], r["inserted"],
                          r["skipped_duplicate"], r.get("errors", 0), r["elapsed_s"])
        except Exception as exc:
            log.warning("[candle_ingestion] %s %s failed: %s", pair, tf, exc)
            report["timeframes"][tf] = {"error": str(exc)}
            report["totals"]["errors"] += 1
            _last_ingest_error.update({
                "at":      datetime.now(timezone.utc).isoformat(),
                "pair":    pair,
                "tf":      tf,
                "message": str(exc)[:400],
            })

    report["finished"] = datetime.now(timezone.utc).isoformat()
    return report


def _persist_gc_bars(db: Session, tf: str, candles: list) -> dict:
    """Persist Yahoo GC=F candles into gc_futures_bars (independent from XAU/USD)."""
    from db_models import GcFuturesBar
    inserted, skipped, errors = 0, 0, 0
    for c in candles:
        try:
            ts = _field(c, "time")
            if isinstance(ts, str):
                ts = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            if ts is None:
                errors += 1; continue
            if not getattr(ts, "tzinfo", None):
                ts = ts.replace(tzinfo=timezone.utc)
            row = GcFuturesBar(
                contract="GC=F",
                timeframe=tf,
                candle_time=ts,
                open=float(_field(c, "open", 0.0)),
                high=float(_field(c, "high", 0.0)),
                low=float(_field(c, "low",  0.0)),
                close=float(_field(c, "close", 0.0)),
                volume=float(_field(c, "volume", 0) or 0),
                source="yahoo",
            )
            db.add(row)
            try:
                db.commit()
                inserted += 1
            except Exception:
                db.rollback()
                skipped += 1     # unique-constraint hit — dedupe
        except Exception:
            db.rollback()
            errors += 1
    return {"inserted": inserted, "skipped_duplicate": skipped, "errors": errors,
             "fetched": len(candles)}


def ingest_gc_futures(db: Session, timeframes: tuple = ("M5", "M15", "H1"),
                        lookback: int = 100) -> dict:
    """
    Fetch Yahoo GC=F for each timeframe and persist to gc_futures_bars.
    Independent from XAU/USD ingestion. Never contaminates historical_candles.
    """
    report = {"pair": "GC=F", "timeframes": {},
              "totals": {"inserted": 0, "skipped": 0, "errors": 0}}
    for tf in timeframes:
        try:
            candles = _fetch_yahoo("xauusd", tf, lookback)
            if not candles:
                report["timeframes"][tf] = {"note": "yahoo empty"}
                continue
            r = _persist_gc_bars(db, tf, candles)
            report["timeframes"][tf] = r
            report["totals"]["inserted"] += r["inserted"]
            report["totals"]["skipped"]  += r["skipped_duplicate"]
            report["totals"]["errors"]   += r["errors"]
        except Exception as exc:
            log.warning("[gc_ingest] %s failed: %s", tf, exc)
            report["timeframes"][tf] = {"error": str(exc)}
            report["totals"]["errors"] += 1
    return report


__all__ = ["top_up_recent", "ingest_gc_futures"]
