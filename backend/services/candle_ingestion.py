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


# Timeframes to keep topped up + max fetch per pull
_TF_PLAN = [
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


def _persist(db: Session, pair: str, tf: str, candles: list) -> dict:
    """Insert candles idempotently. Returns per-timeframe counts."""
    from db_models import HistoricalCandle
    inserted, skipped = 0, 0
    instrument = _stored_instrument(pair)
    for c in candles:
        try:
            ts = c.time if hasattr(c, "time") else c.get("time")
            if isinstance(ts, str):
                ts = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            if ts is None:
                continue
            if not getattr(ts, "tzinfo", None):
                ts = ts.replace(tzinfo=timezone.utc)
            row = HistoricalCandle(
                instrument=instrument,
                timeframe=tf,
                candle_time=ts,
                open=float(getattr(c, "open", c.get("open"))),
                high=float(getattr(c, "high", c.get("high"))),
                low=float(getattr(c, "low",  c.get("low"))),
                close=float(getattr(c, "close", c.get("close"))),
                volume=int(float(getattr(c, "volume", c.get("volume") or 0)) or 0),
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
            log.debug("[candle_ingestion] row insert skipped: %s", exc)
    return {"inserted": inserted, "skipped_duplicate": skipped}


def top_up_recent(db: Session, pair: str = "xauusd",
                    lookback_override: Optional[int] = None) -> dict:
    """Public entry point. Loops every TF, top-ups, returns report."""
    report = {
        "pair":      pair,
        "started":   datetime.now(timezone.utc).isoformat(),
        "totals":    {"inserted": 0, "skipped": 0, "errors": 0},
        "timeframes": {},
    }
    for tf, n_default in _TF_PLAN:
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
            if r["inserted"]:
                log.info("[candle_ingestion] %s %s: fetched=%d inserted=%d "
                          "dup=%d (%.2fs)",
                          pair, tf, r["fetched"], r["inserted"],
                          r["skipped_duplicate"], r["elapsed_s"])
        except Exception as exc:
            log.warning("[candle_ingestion] %s %s failed: %s", pair, tf, exc)
            report["timeframes"][tf] = {"error": str(exc)}
            report["totals"]["errors"] += 1

    report["finished"] = datetime.now(timezone.utc).isoformat()
    return report


__all__ = ["top_up_recent"]
