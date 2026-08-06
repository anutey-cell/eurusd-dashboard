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


def _persist(db: Session, pair: str, tf: str, candles: list) -> dict:
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
                source="twelvedata",
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
            candles = _fetch_twelvedata(pair, tf, n)
            if not candles:
                report["timeframes"][tf] = {"error": "empty response"}
                report["totals"]["errors"] += 1
                continue
            r = _persist(db, pair, tf, candles)
            r["fetched"] = len(candles)
            r["elapsed_s"] = round(time.time() - t0, 2)
            report["timeframes"][tf] = r
            report["totals"]["inserted"] += r["inserted"]
            report["totals"]["skipped"]  += r["skipped_duplicate"]
            report["totals"]["errors"]   += r.get("errors", 0)
            if r["inserted"] or r.get("errors", 0):
                log.info("[candle_ingestion] %s %s: fetched=%d inserted=%d dup=%d errors=%d (%.2fs)",
                          pair, tf, r["fetched"], r["inserted"],
                          r["skipped_duplicate"], r.get("errors", 0), r["elapsed_s"])
        except Exception as exc:
            log.warning("[candle_ingestion] %s %s failed: %s", pair, tf, exc)
            report["timeframes"][tf] = {"error": str(exc)}
            report["totals"]["errors"] += 1

    report["finished"] = datetime.now(timezone.utc).isoformat()
    return report


__all__ = ["top_up_recent"]
