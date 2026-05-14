"""
Historical XAU/USD candle provider for backtesting.

Supports three sources in priority order:
  1. Database (historical_candles table) — preferred
  2. CSV import (one-shot via POST /api/v1/backtest/import-csv)
  3. Synthetic fallback (last-resort, marked clearly in response)

CSV format expected:
  timestamp,open,high,low,close,volume

  2025-01-01T10:00:00Z,2630.50,2637.20,2625.10,2633.80,1000

Validation rules:
  - timestamp must parse to ISO-8601 UTC
  - open/high/low/close must be positive floats
  - high >= max(open, close, low)
  - low  <= min(open, close, high)
  - duplicate (instrument, timeframe, candle_time) rows are skipped
"""
from __future__ import annotations

import csv
import io
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import IO

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from db_models import HistoricalCandle
from models.candle import Candle

log = logging.getLogger(__name__)

# Supported timeframes (must match INTERVAL_MINUTES in data/candles.py)
SUPPORTED_TIMEFRAMES = {"M5", "M15", "M30", "H1", "H4", "D1"}

# Map common CSV labels → canonical timeframe codes
TIMEFRAME_ALIASES = {
    "5min":  "M5",  "5m":  "M5",  "M5":  "M5",
    "15min": "M15", "15m": "M15", "M15": "M15",
    "30min": "M30", "30m": "M30", "M30": "M30",
    "1h":    "H1",  "h1":  "H1",  "H1":  "H1",  "60min": "H1",
    "4h":    "H4",  "h4":  "H4",  "H4":  "H4",  "240min": "H4",
    "1d":    "D1",  "d1":  "D1",  "D1":  "D1",  "daily":  "D1",
}


@dataclass
class ImportResult:
    imported:        int
    skipped:         int
    duplicates:      int
    invalid:         int
    errors:          list[str]
    timeframe:       str
    instrument:      str


def normalise_timeframe(raw: str) -> str:
    """Convert a CSV/HTTP timeframe label to canonical M5/M15/.../D1."""
    key = (raw or "").strip()
    if key in TIMEFRAME_ALIASES:
        return TIMEFRAME_ALIASES[key]
    lower = key.lower()
    if lower in TIMEFRAME_ALIASES:
        return TIMEFRAME_ALIASES[lower]
    upper = key.upper()
    if upper in SUPPORTED_TIMEFRAMES:
        return upper
    raise ValueError(
        f"Unsupported timeframe '{raw}'. Valid: {sorted(SUPPORTED_TIMEFRAMES)}"
    )


# ═══════════════════════════════════════════════════════════════════════════════
# CSV import
# ═══════════════════════════════════════════════════════════════════════════════

def import_csv(
    db: Session,
    csv_text: str,
    timeframe: str = "M15",
    instrument: str = "XAU/USD",
    source: str = "csv",
    max_rows: int = 200_000,
) -> ImportResult:
    """
    Import historical XAU/USD candles from a CSV string.

    Returns ImportResult with counts and error messages.
    All DB writes are committed in a single transaction at the end.
    """
    tf = normalise_timeframe(timeframe)
    reader = csv.DictReader(io.StringIO(csv_text))

    # Validate required columns
    required = {"timestamp", "open", "high", "low", "close"}
    if not reader.fieldnames:
        return ImportResult(0, 0, 0, 0, ["Empty CSV — no header row"], tf, instrument)
    missing = required - {c.lower() for c in reader.fieldnames}
    if missing:
        return ImportResult(
            0, 0, 0, 0,
            [f"Missing required columns: {sorted(missing)}. "
             f"Header must be: timestamp,open,high,low,close[,volume]"],
            tf, instrument,
        )

    # Normalise column casing (CSVs from different sources use varied case)
    def _col(row: dict, name: str) -> str | None:
        # try exact, lowercase, capitalised
        for key in (name, name.lower(), name.upper(), name.capitalize()):
            if key in row:
                return row[key]
        return None

    new_records: list[HistoricalCandle] = []
    seen: set[datetime] = set()           # in-batch dedupe
    errors: list[str] = []
    imported = invalid = duplicates = 0

    for row_idx, row in enumerate(reader, start=2):  # row 1 = header
        if row_idx - 1 > max_rows:
            errors.append(f"Row limit {max_rows} exceeded — truncating import")
            break

        try:
            ts_raw = _col(row, "timestamp")
            o      = float(_col(row, "open")  or 0)
            h      = float(_col(row, "high")  or 0)
            l      = float(_col(row, "low")   or 0)
            c      = float(_col(row, "close") or 0)
            v      = int(float(_col(row, "volume") or 0))

            ts = datetime.fromisoformat(str(ts_raw).replace("Z", "+00:00"))
            if not ts.tzinfo:
                ts = ts.replace(tzinfo=timezone.utc)

            # OHLC sanity
            if any(x <= 0 for x in (o, h, l, c)):
                raise ValueError("OHLC values must be positive")
            if h < max(o, c, l):
                raise ValueError(f"high {h} < max(o,c,l)")
            if l > min(o, c, h):
                raise ValueError(f"low {l} > min(o,c,h)")

            # In-batch dedupe (same TF + timestamp twice in the same CSV)
            if ts in seen:
                duplicates += 1
                continue
            seen.add(ts)

            new_records.append(HistoricalCandle(
                instrument=instrument,
                timeframe=tf,
                candle_time=ts,
                open=o, high=h, low=l, close=c, volume=v,
                source=source,
            ))
            imported += 1
        except Exception as e:
            invalid += 1
            if len(errors) < 20:    # cap error list length
                errors.append(f"Row {row_idx}: {e}")

    # Bulk insert with per-record fallback on integrity errors (DB duplicates)
    if new_records:
        try:
            db.bulk_save_objects(new_records)
            db.commit()
        except IntegrityError:
            # Some rows already exist in DB — fall back to per-row insert
            db.rollback()
            imported = 0
            duplicates = 0
            for r in new_records:
                try:
                    db.add(r)
                    db.commit()
                    imported += 1
                except IntegrityError:
                    db.rollback()
                    duplicates += 1
                except Exception as e:
                    db.rollback()
                    invalid += 1
                    if len(errors) < 20:
                        errors.append(f"Insert error: {e}")

    log.info(
        "[hist] CSV import complete instrument=%s timeframe=%s imported=%d "
        "duplicates=%d invalid=%d",
        instrument, tf, imported, duplicates, invalid,
    )

    return ImportResult(
        imported=imported,
        skipped=duplicates + invalid,
        duplicates=duplicates,
        invalid=invalid,
        errors=errors,
        timeframe=tf,
        instrument=instrument,
    )


# ═══════════════════════════════════════════════════════════════════════════════
# Historical candle retrieval (DB → synthetic fallback)
# ═══════════════════════════════════════════════════════════════════════════════

def get_historical_candles(
    db: Session,
    timeframe: str = "M15",
    start_date: datetime | None = None,
    end_date:   datetime | None = None,
    lookback:   int             = 5000,
    instrument: str             = "XAU/USD",
    allow_synthetic_fallback: bool = True,
) -> tuple[list[Candle], str]:
    """
    Fetch historical candles in ascending time order.

    Returns (candles, source) where source is:
      - "database"  : rows from historical_candles
      - "synthetic" : generated fallback (only when allow_synthetic_fallback=True)
      - "none"      : no data and fallback disabled

    Lookback caps the result count to keep API responses bounded.
    """
    tf = normalise_timeframe(timeframe)

    q = db.query(HistoricalCandle).filter(
        HistoricalCandle.instrument == instrument,
        HistoricalCandle.timeframe == tf,
    )
    if start_date:
        if not start_date.tzinfo:
            start_date = start_date.replace(tzinfo=timezone.utc)
        q = q.filter(HistoricalCandle.candle_time >= start_date)
    if end_date:
        if not end_date.tzinfo:
            end_date = end_date.replace(tzinfo=timezone.utc)
        q = q.filter(HistoricalCandle.candle_time <= end_date)

    rows = (
        q.order_by(HistoricalCandle.candle_time.asc())
         .limit(max(50, min(lookback, 50_000)))
         .all()
    )

    if rows:
        return (
            [
                Candle(
                    time=r.candle_time,
                    open=r.open, high=r.high, low=r.low, close=r.close,
                    volume=r.volume,
                )
                for r in rows
            ],
            "database",
        )

    if not allow_synthetic_fallback:
        return [], "none"

    # Synthetic fallback — use the deterministic generator
    from data.candles import get_candles
    try:
        resp = get_candles(interval=tf, limit=max(200, min(lookback, 5000)), pair="xauusd")
        return resp.candles, "synthetic"
    except Exception as e:
        log.warning("[hist] Synthetic fallback failed: %s", e)
        return [], "none"


def historical_data_available(
    db: Session,
    timeframe: str = "M15",
    instrument: str = "XAU/USD",
) -> dict:
    """Quick status check for the import UI."""
    tf = normalise_timeframe(timeframe)
    q = db.query(HistoricalCandle).filter(
        HistoricalCandle.instrument == instrument,
        HistoricalCandle.timeframe == tf,
    )
    count = q.count()
    if count == 0:
        return {"available": False, "count": 0, "timeframe": tf, "instrument": instrument}

    first = q.order_by(HistoricalCandle.candle_time.asc()).first()
    last  = q.order_by(HistoricalCandle.candle_time.desc()).first()
    return {
        "available":  True,
        "count":      count,
        "timeframe":  tf,
        "instrument": instrument,
        "earliest":   first.candle_time.isoformat() if first else None,
        "latest":     last.candle_time.isoformat()  if last  else None,
    }
