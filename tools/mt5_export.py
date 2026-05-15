"""
MT5 Historical Candle Exporter — Windows side.

Pulls historical XAU/USD candles from your locally-installed MetaTrader 5
terminal and saves them as a CSV that can be uploaded to the dashboard's
backtest import endpoint.

WHY: The Docker backend runs on Linux. The MetaTrader5 Python library
is Windows-only. This script bridges the gap — runs on your Windows
machine where MT5 is installed, produces the CSV the backend can consume.

USAGE
-----
1. Install MT5 terminal + log in to your broker (demo or live)
2. Install Python 3.10+ and the MetaTrader5 package:

       pip install MetaTrader5

3. Run this script:

       python tools/mt5_export.py
       python tools/mt5_export.py --timeframe H4 --years 5
       python tools/mt5_export.py --timeframe M15 --years 2 --symbol XAUUSDm

4. The CSV will appear next to this script: xauusd_<TF>_<from>_<to>.csv
5. Upload it via the dashboard's "Historical Candle Import (CSV)" panel
   OR via curl:

       curl -X POST "http://localhost:8000/api/v1/backtest/import-csv?timeframe=H4" \\
            -F "file=@xauusd_H4_2020-01-01_2026-05-15.csv"

This script does NOT place trades or interact with the dashboard directly.
Read-only against your MT5 history.
"""
from __future__ import annotations

import argparse
import csv
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

# ── Imports ───────────────────────────────────────────────────────────────────
try:
    import MetaTrader5 as mt5
except ImportError:
    print("ERROR: MetaTrader5 package not installed.")
    print("Install with: pip install MetaTrader5")
    print("Note: MT5 Python is Windows-only.")
    sys.exit(2)


# ── Symbol candidates ─────────────────────────────────────────────────────────
# Different brokers name gold differently. Try them in order.
XAUUSD_SYMBOL_CANDIDATES = [
    "XAUUSD", "XAUUSDz", "XAUUSDm", "XAUUSD.r", "XAUUSD.raw",
    "XAUUSD_", "XAUUSDc", "XAUUSDpro",
    "GOLD", "GOLDz", "GOLDm", "GOLD.r", "XAUUSD-PRO",
]


def auto_discover_xauusd_symbol() -> str | None:
    """
    Scan all available symbols for any starting with 'XAUUSD' or 'GOLD'
    and pick the most likely one. Handles broker-specific suffixes like
    z (Exness), m (Forex.com), .r (raw spreads), etc.
    """
    try:
        all_syms = mt5.symbols_get() or []
    except Exception:
        return None
    candidates = []
    for s in all_syms:
        u = s.name.upper()
        if u == "XAUUSD" or u == "GOLD":
            return s.name   # exact match wins immediately
        if u.startswith("XAUUSD") or u.startswith("GOLD"):
            candidates.append(s.name)
    return candidates[0] if candidates else None

TIMEFRAME_MAP = {
    "M1":  mt5.TIMEFRAME_M1,
    "M5":  mt5.TIMEFRAME_M5,
    "M15": mt5.TIMEFRAME_M15,
    "M30": mt5.TIMEFRAME_M30,
    "H1":  mt5.TIMEFRAME_H1,
    "H4":  mt5.TIMEFRAME_H4,
    "D1":  mt5.TIMEFRAME_D1,
    "W1":  mt5.TIMEFRAME_W1,
}


def resolve_xauusd_symbol(preferred: str | None = None) -> str | None:
    """Try the preferred symbol first, then the candidate list, then auto-discover."""
    candidates = [preferred] if preferred else []
    candidates += XAUUSD_SYMBOL_CANDIDATES
    for sym in candidates:
        if sym is None:
            continue
        info = mt5.symbol_info(sym)
        if info is not None:
            return sym
    # Fallback: scan all symbols
    return auto_discover_xauusd_symbol()


def fetch_history(symbol: str, tf_const, start: datetime, end: datetime) -> list:
    """Fetch bars between start and end (inclusive). Uses copy_rates_range."""
    bars = mt5.copy_rates_range(symbol, tf_const, start, end)
    if bars is None or len(bars) == 0:
        return []
    return list(bars)


def to_csv(bars: list, out_path: Path) -> int:
    """Write OHLCV bars to CSV in the dashboard's expected format."""
    with out_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["timestamp", "open", "high", "low", "close", "volume"])
        n = 0
        for b in bars:
            ts = datetime.fromtimestamp(int(b["time"]), tz=timezone.utc)
            writer.writerow([
                ts.isoformat(),
                f"{b['open']:.5f}",
                f"{b['high']:.5f}",
                f"{b['low']:.5f}",
                f"{b['close']:.5f}",
                int(b["tick_volume"]),
            ])
            n += 1
    return n


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Export XAU/USD historical candles from MT5 for backtest import."
    )
    parser.add_argument("--timeframe", "-t", default="H4",
                        choices=list(TIMEFRAME_MAP.keys()),
                        help="Candle timeframe (default: H4)")
    parser.add_argument("--years", "-y", type=float, default=5.0,
                        help="How many years of history to fetch (default: 5)")
    parser.add_argument("--symbol", "-s", default=None,
                        help="MT5 symbol override (e.g. XAUUSDm). "
                             "Auto-resolved if not specified.")
    parser.add_argument("--output", "-o", default=None,
                        help="Output CSV path (default: auto-named in current dir)")
    parser.add_argument("--login",    type=int, default=None, help="MT5 login (optional)")
    parser.add_argument("--password",          default=None, help="MT5 password (optional)")
    parser.add_argument("--server",            default=None, help="MT5 server name (optional)")
    args = parser.parse_args()

    print(f"=== MT5 XAU/USD Historical Exporter ===")
    print(f"Timeframe: {args.timeframe}")
    print(f"Years:     {args.years}")
    print()

    # Initialise MT5 — uses currently-logged-in terminal session by default
    init_args = {}
    if args.login and args.password and args.server:
        init_args = {"login": args.login, "password": args.password, "server": args.server}

    if not mt5.initialize(**init_args):
        err = mt5.last_error()
        print(f"ERROR: mt5.initialize() failed: {err}")
        print("Make sure MetaTrader 5 terminal is running and logged in.")
        return 1

    try:
        # Account info sanity check
        acc = mt5.account_info()
        if acc:
            print(f"Connected: account={acc.login}  server={acc.server}  balance={acc.balance:.2f} {acc.currency}")
        else:
            print("Connected but no account info available")

        # Resolve symbol
        symbol = resolve_xauusd_symbol(args.symbol)
        if not symbol:
            print(f"ERROR: Could not resolve any XAU/USD symbol.")
            print(f"  Tried: {XAUUSD_SYMBOL_CANDIDATES}")
            print(f"  Pass --symbol <your_broker_name> if it's named differently.")
            print(f"  Available symbols (first 50):")
            syms = mt5.symbols_get()
            if syms:
                gold_like = [s.name for s in syms if "GOLD" in s.name.upper() or "XAU" in s.name.upper()]
                for s in gold_like[:50]:
                    print(f"    {s}")
            return 1
        print(f"Symbol resolved: {symbol}")

        # Enable symbol in MarketWatch (required for history fetch)
        if not mt5.symbol_select(symbol, True):
            print(f"WARNING: Could not select {symbol} in MarketWatch — fetch may fail")

        # Compute date range
        end = datetime.now(timezone.utc)
        start = end - timedelta(days=int(args.years * 365.25))
        print(f"Range:     {start.date()} -> {end.date()}")

        tf_const = TIMEFRAME_MAP[args.timeframe]

        # Fetch in chunks if range is large (avoid timeouts on big requests)
        all_bars = []
        chunk_days = 180
        cursor = start
        while cursor < end:
            chunk_end = min(cursor + timedelta(days=chunk_days), end)
            print(f"  fetching {cursor.date()} -> {chunk_end.date()}...", end="", flush=True)
            chunk = fetch_history(symbol, tf_const, cursor, chunk_end)
            if chunk:
                # Deduplicate by timestamp against accumulated set
                seen = {b["time"] for b in all_bars}
                new = [b for b in chunk if b["time"] not in seen]
                all_bars.extend(new)
                print(f" got {len(chunk)} bars ({len(new)} new)")
            else:
                print(" (empty)")
            cursor = chunk_end + timedelta(days=1)

        # Sort + dedupe just in case
        all_bars.sort(key=lambda b: b["time"])
        if len(all_bars) < 100:
            print(f"WARNING: only {len(all_bars)} bars fetched. Check broker history availability.")

        # Write CSV
        if args.output:
            out_path = Path(args.output)
        else:
            out_path = Path(
                f"xauusd_{args.timeframe}_{start.date()}_{end.date()}.csv"
            )
        n = to_csv(all_bars, out_path)
        print()
        print(f"=== DONE ===")
        print(f"Bars exported: {n:,}")
        print(f"Output file:   {out_path.absolute()}")
        print()
        print("UPLOAD via the dashboard's CSV import panel, or:")
        print(f'  curl -X POST "http://localhost:8000/api/v1/backtest/import-csv?timeframe={args.timeframe}" -F "file=@{out_path.name}"')
        return 0
    finally:
        mt5.shutdown()


if __name__ == "__main__":
    sys.exit(main())
