"""Streaming, read-only export of Predator research candles.

Writes M5, M15 and H1 XAU/USD historical_candles to one gzip CSV without
materialising the dataset in memory. Intended for governance research off-host.

Usage inside backend container:
  PYTHONPATH=/app python /tmp/export_predator_history.py /tmp/predator_history.csv.gz
"""
from __future__ import annotations

import csv
import gzip
import sys

from sqlalchemy import text

from database import engine


def main():
    if len(sys.argv) != 2:
        raise SystemExit("usage: export_predator_history.py OUTPUT.csv.gz")
    output = sys.argv[1]
    sql = text(
        "SELECT timeframe, candle_time, open, high, low, close, volume "
        "FROM historical_candles "
        "WHERE instrument='XAU/USD' AND timeframe IN ('M5','M15','H1') "
        "ORDER BY timeframe, candle_time"
    )
    counts = {"M5": 0, "M15": 0, "H1": 0}
    with engine.connect().execution_options(stream_results=True) as conn:
        result = conn.execute(sql).yield_per(1000)
        with gzip.open(output, "wt", newline="", encoding="utf-8", compresslevel=6) as gz:
            writer = csv.writer(gz)
            writer.writerow(["timeframe", "candle_time", "open", "high", "low", "close", "volume"])
            for r in result:
                tf = str(r[0])
                counts[tf] = counts.get(tf, 0) + 1
                writer.writerow([tf, r[1], r[2], r[3], r[4], r[5], r[6]])
    print({"output": output, "counts": counts})


if __name__ == "__main__":
    main()
