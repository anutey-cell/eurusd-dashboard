"""
Candle Ingestion — Recurring Top-Up
====================================

Runs periodically to keep `historical_candles` fresh. The production cloud
continuity path prefers true XAU/USD spot candles and fails closed rather
than silently substituting a stale or materially different instrument.

Provider order for recurring VPS pulls:
  1. Twelve Data XAU/USD (primary cloud spot source)
  2. TradingView OANDA:XAUUSD (independent spot fallback, with retries)

MT5 bars arrive separately via the Windows bridge and naturally win the
freshness race when the laptop is online. Yahoo GC=F remains useful as a
futures/context feed, but it is deliberately NOT persisted as XAU/USD spot
history by this module.

This module is:
  1. Idempotent — inserts are gated on unique (instrument, timeframe,
     candle_time); duplicates skip via IntegrityError.
  2. Bounded — pulls only N most-recent bars per call (default 200),
     enough to fill any gap up to a week without paying to re-fetch
     historical.
  3. Freshness-gated — a provider payload must contain a sufficiently recent
     closed bar before it can enter the signal database.
  4. Silent by design — errors log at WARN, never raise out of the scheduler;
     the freshness sentinel remains responsible for operator alerting.
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
    """Fetch via TradingView. Returns [] on failure."""
    from services.tradingview_provider import get_tv_candles
    r = get_tv_candles(pair, timeframe=interval, limit=lookback)
    return r or []


def _fetch_yahoo(pair: str, interval: str, lookback: int) -> list:
    """Fetch Yahoo GC=F for the independent futures-context table only."""
    from services.yahoo_provider import get_yahoo_candles
    r = get_yahoo_candles(pair, timeframe=interval, limit=lookback)
    return r or []


# TV retry policy — anonymous/authenticated tvDatafeed sessions can drop.
# Keep retries short: Twelve Data has already been attempted before we get here.
_TV_RETRY_BACKOFFS_S: list[float] = [1.0, 3.0]


def _candle_time_utc(candle) -> Optional[datetime]:
    """Extract a candle timestamp from dict/Pydantic shapes and normalise UTC."""
    raw = candle.get("time") if isinstance(candle, dict) else getattr(candle, "time", None)
    if isinstance(raw, str):
        try:
            raw = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            return None
    if not isinstance(raw, datetime):
        return None
    return raw if raw.tzinfo else raw.replace(tzinfo=timezone.utc)


def _payload_fresh(candles: list, interval: str,
                   now: Optional[datetime] = None) -> tuple[bool, str]:
    """Return whether a provider payload is recent enough for the signal DB.

    The thresholds intentionally reuse the production freshness sentinel's
    per-timeframe tolerances. During the market's weekend closure we accept
    the latest completed bars because no newer bar should exist.
    """
    if not candles:
        return False, "empty payload"

    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)

    from services.data_freshness import STALENESS_MIN_BY_TF, _is_weekend_closed

    times = [ts for ts in (_candle_time_utc(c) for c in candles) if ts is not None]
    if not times:
        return False, "payload has no parseable candle timestamps"

    latest = max(times)
    if _is_weekend_closed(now):
        return True, f"market closed; latest={latest.isoformat()}"

    threshold_min = STALENESS_MIN_BY_TF.get(interval.upper(), 60)
    age_min = max(0.0, (now - latest).total_seconds() / 60.0)
    if age_min <= threshold_min:
        return True, f"latest age={age_min:.1f}m <= {threshold_min}m"

    return False, (
        f"stale latest={latest.isoformat()} age={age_min:.1f}m "
        f"> threshold={threshold_min}m"
    )


def _fetch_with_fallback(pair: str, interval: str,
                          lookback: int) -> tuple[list, str]:
    """Fetch a fresh spot payload using a health-aware fallback chain.

    MT5 bars are pushed independently by the Windows bridge. The VPS cloud
    path uses Twelve Data then TradingView OANDA:XAUUSD. Provider health
    circuits prevent a known-broken credential/quota from being retried on
    every timeframe/cycle. Yahoo GC=F is never a spot fallback.
    """
    from services.tradingview_provider import invalidate_cache as _tv_invalidate
    from services.provider_health import should_attempt, note_success, note_failure

    failures: list[str] = []

    td_attempt, td_reason = should_attempt("twelvedata", interval)
    if td_attempt:
        try:
            candles = _fetch_twelvedata(pair, interval, lookback)
            ok, detail = _payload_fresh(candles, interval)
            if ok:
                note_success("twelvedata", interval)
                return candles, "twelvedata"
            failures.append(f"TwelveData rejected: {detail}")
            note_failure("twelvedata", interval, detail, category="stale_payload")
            log.warning("[candle_ingestion] %s %s TwelveData rejected: %s",
                        pair, interval, detail)
        except Exception as exc:
            category = note_failure("twelvedata", interval, exc)
            failures.append(f"TwelveData {type(exc).__name__}: {exc}")
            log.warning(
                "[candle_ingestion] %s %s TwelveData failed (%s): %s: %s",
                pair, interval, category, type(exc).__name__, exc,
            )
    else:
        failures.append(f"TwelveData skipped: {td_reason}")
        log.info("[candle_ingestion] %s %s TwelveData skipped: %s",
                 pair, interval, td_reason)

    tv_attempt, tv_reason = should_attempt("tradingview", interval)
    if tv_attempt:
        for attempt, backoff in enumerate([0.0] + _TV_RETRY_BACKOFFS_S, start=1):
            if backoff > 0:
                time.sleep(backoff)
                _tv_invalidate(pair)
            try:
                candles = _fetch_tradingview(pair, interval, lookback)
                ok, detail = _payload_fresh(candles, interval)
                if ok:
                    note_success("tradingview", interval)
                    if attempt > 1 or failures:
                        log.info(
                            "[candle_ingestion] %s %s using TradingView fallback "
                            "after primary/unhealthy source", pair, interval,
                        )
                    return candles, "tradingview"
                failures.append(f"TradingView attempt {attempt} rejected: {detail}")
                note_failure("tradingview", interval, detail, category="stale_payload")
                log.warning(
                    "[candle_ingestion] %s %s TradingView attempt %d rejected: %s",
                    pair, interval, attempt, detail,
                )
            except Exception as exc:
                category = note_failure("tradingview", interval, exc)
                failures.append(
                    f"TradingView attempt {attempt} {type(exc).__name__}: {exc}"
                )
                log.warning(
                    "[candle_ingestion] %s %s TradingView attempt %d failed (%s): %s: %s",
                    pair, interval, attempt, category, type(exc).__name__, exc,
                )
    else:
        failures.append(f"TradingView skipped: {tv_reason}")
        log.warning("[candle_ingestion] %s %s TradingView skipped: %s",
                    pair, interval, tv_reason)

    summary = " | ".join(failures[-8:])
    raise RuntimeError(
        f"No fresh XAU/USD spot provider for {pair} {interval}. {summary}"
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
    """Persist Yahoo GC=F into its independent futures table, never XAU/USD."""
    from db_models import GcFuturesBar
    inserted, skipped, errors = 0, 0, 0
    for c in candles:
        try:
            ts = _field(c, "time")
            if isinstance(ts, str):
                ts = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            if ts is None:
                errors += 1
                continue
            if not getattr(ts, "tzinfo", None):
                ts = ts.replace(tzinfo=timezone.utc)
            row = GcFuturesBar(
                contract="GC=F",
                timeframe=tf,
                candle_time=ts,
                open=float(_field(c, "open", 0.0)),
                high=float(_field(c, "high", 0.0)),
                low=float(_field(c, "low", 0.0)),
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
                skipped += 1
        except Exception:
            db.rollback()
            errors += 1
    return {
        "inserted": inserted,
        "skipped_duplicate": skipped,
        "errors": errors,
        "fetched": len(candles),
    }


def ingest_gc_futures(db: Session, timeframes: tuple = ("M5", "M15", "H1"),
                      lookback: int = 100) -> dict:
    """Refresh the independent GC=F context series expected by the scheduler.

    This compatibility function is deliberately separate from `top_up_recent`: GC
    futures may support basis/context research, but can never satisfy XAU/USD spot
    freshness or be persisted into `historical_candles`.
    """
    report = {
        "pair": "GC=F",
        "timeframes": {},
        "totals": {"inserted": 0, "skipped": 0, "errors": 0},
    }
    for tf in timeframes:
        try:
            candles = _fetch_yahoo("xauusd", tf, lookback)
            if not candles:
                report["timeframes"][tf] = {"note": "yahoo empty"}
                continue
            r = _persist_gc_bars(db, tf, candles)
            report["timeframes"][tf] = r
            report["totals"]["inserted"] += r["inserted"]
            report["totals"]["skipped"] += r["skipped_duplicate"]
            report["totals"]["errors"] += r["errors"]
        except Exception as exc:
            log.warning("[gc_ingest] %s failed: %s", tf, exc)
            report["timeframes"][tf] = {"error": str(exc)}
            report["totals"]["errors"] += 1
    return report


__all__ = ["top_up_recent", "ingest_gc_futures"]
