"""
Real-data historical backfill
=============================

One-shot importer that pulls REAL XAU/USD candles from TradingView and writes
them into `historical_candles` with source='tradingview'. Replaces the 25k
synthetic_seed rows the learning paths refuse to use.

Idempotent: the unique constraint on (instrument, timeframe, candle_time)
makes duplicate-inserts safe — re-runs only add new bars.

CLI:
    docker exec xauusd-backend bash -c \
        'cd /app && PYTHONPATH=/app python -m services.realdata_backfill'

API:
    POST /api/v1/strategist/backfill-history  (router exposes this)
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

log = logging.getLogger(__name__)


# How many bars per timeframe to pull. tvDatafeed's free tier caps each request
# at ~5000 bars; these numbers fit comfortably under that.
_BACKFILL_PLAN: list[tuple[str, int]] = [
    # (timeframe, n_bars)
    ("D1",  365),       # 1 year of daily
    ("H4",  2000),      # ~333 days
    ("H1",  3000),      # ~125 days
    ("M15", 5000),      # ~52 days
    ("M5",  4000),      # ~14 days (most granular)
]


def backfill_historical_candles(
    db: Session,
    *,
    pair: str = "xauusd",
    replace_synthetic: bool = True,
) -> dict[str, Any]:
    """
    Pull TradingView historical bars and insert into historical_candles.
    Returns a per-timeframe report dict.

    replace_synthetic — when True, deletes all synthetic_seed rows for the
                        instrument first so the table contains real data only.
    """
    from db_models import HistoricalCandle
    from services.tradingview_provider import get_tv_candles
    from pair_config import get_pair_config

    try:
        pair_cfg = get_pair_config(pair)
        instrument = pair_cfg.get("symbol", "XAU/USD")
    except Exception:
        instrument = "XAU/USD"

    report: dict[str, Any] = {
        "instrument": instrument,
        "pair":       pair,
        "started_at": datetime.now(timezone.utc).isoformat(),
        "timeframes": {},
        "synthetic_purged": 0,
        "total_inserted":   0,
        "total_skipped":    0,
        "errors":           [],
    }

    if replace_synthetic:
        purged = (
            db.query(HistoricalCandle)
              .filter(HistoricalCandle.instrument == instrument,
                      HistoricalCandle.source == "synthetic_seed")
              .delete(synchronize_session=False)
        )
        db.commit()
        report["synthetic_purged"] = purged
        log.info("[backfill] purged %d synthetic_seed rows", purged)

    for tf, n_bars in _BACKFILL_PLAN:
        tf_started = time.time()
        tf_report: dict[str, Any] = {
            "requested_bars": n_bars, "fetched": 0,
            "inserted": 0, "skipped_duplicate": 0,
            "earliest": None, "latest": None,
            "error":    None,
        }

        try:
            candles = get_tv_candles(pair=pair, timeframe=tf, limit=n_bars)
            if not candles:
                tf_report["error"] = "TradingView returned no candles"
                report["timeframes"][tf] = tf_report
                continue

            tf_report["fetched"] = len(candles)
            tf_report["earliest"] = str(candles[0].get("time"))
            tf_report["latest"]   = str(candles[-1].get("time"))

            for c in candles:
                try:
                    ts = c.get("time")
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
                        open=float(c["open"]),
                        high=float(c["high"]),
                        low=float(c["low"]),
                        close=float(c["close"]),
                        volume=int(float(c.get("volume") or 0)),
                        source="tradingview",
                    )
                    db.add(row)
                    db.commit()
                    tf_report["inserted"] += 1
                except IntegrityError:
                    db.rollback()
                    tf_report["skipped_duplicate"] += 1
                except Exception as exc:
                    db.rollback()
                    tf_report["error"] = f"row insert: {exc}"
                    break

            log.info(
                "[backfill] %s %s: fetched=%d inserted=%d dup-skipped=%d (%.1fs)",
                instrument, tf, tf_report["fetched"], tf_report["inserted"],
                tf_report["skipped_duplicate"], time.time() - tf_started,
            )
        except Exception as exc:
            tf_report["error"] = str(exc)
            log.warning("[backfill] %s %s failed: %s", instrument, tf, exc)
            report["errors"].append(f"{tf}: {exc}")

        report["timeframes"][tf] = tf_report
        report["total_inserted"] += tf_report["inserted"]
        report["total_skipped"]  += tf_report["skipped_duplicate"]

    report["finished_at"] = datetime.now(timezone.utc).isoformat()
    return report


# ── CLI entry point ──────────────────────────────────────────────────────────

if __name__ == "__main__":
    import json
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    from database import SessionLocal

    print("Starting real-data historical backfill (TradingView)…")
    with SessionLocal() as db:
        result = backfill_historical_candles(db, pair="xauusd", replace_synthetic=True)
    print()
    print(json.dumps(result, indent=2, default=str))
