"""Phase 1B — macro/rates backfill.

Sources:
  Treasury Direct CSV  → daily nominal + real yield curves (1M–30Y).
                         Free, no key, EOD, official.
  tvDatafeed          → DXY, USDJPY, USDCNH, VIX, WTI, MOVE  (D1)
                         via credentials in existing backend env.
  DERIVED             → 10Y breakeven = 10Y_nominal − 10Y_real (Treasury pair)

Explicitly NOT available:
  FRED direct CSV      → HTTP timeouts from droplet.
  Fed funds futures    → CME data not free-tier automatable.
  SOFR curve           → CME data not free-tier automatable.
"""
import csv
import io
import os
import time
import urllib.request
import urllib.parse
from datetime import datetime, timezone
from database import SessionLocal
from sqlalchemy import text


def insert_macro(db, series_id, series_name, obs_date, value, unit,
                 provider, source_url, frequency, latency_hint, raw=None):
    db.execute(text("""
        INSERT OR REPLACE INTO macro_series_raw (
            series_id, series_name, obs_date, value, unit,
            provider, source_url, frequency, latency_hint, raw_payload
        ) VALUES (
            :series_id, :series_name, :obs_date, :value, :unit,
            :provider, :source_url, :frequency, :latency_hint, :raw
        )
    """), {"series_id": series_id, "series_name": series_name,
           "obs_date": obs_date, "value": value, "unit": unit,
           "provider": provider, "source_url": source_url,
           "frequency": frequency, "latency_hint": latency_hint, "raw": raw})


# ────────────────────────────────────────────────────────────────────
# Treasury Direct — nominal + real yield curves
# ────────────────────────────────────────────────────────────────────

def fetch_treasury_yield_curve(db, kind: str, series_prefix: str,
                                 series_label_prefix: str, year_from=2019, year_to=2027):
    """Fetch Treasury Direct CSV for either 'nominal' or 'real'.

    Yield curve URL layout (verified reachable):
      home.treasury.gov/resource-center/data-chart-center/interest-rates/
      daily-treasury-rates.csv/YYYY/all?type=daily_treasury_yield_curve...
    """
    tot_ins = 0
    for yr in range(year_from, year_to):
        if kind == "nominal":
            type_param = "daily_treasury_yield_curve"
        else:
            type_param = "daily_treasury_real_yield_curve"
        url = (f"https://home.treasury.gov/resource-center/data-chart-center/"
               f"interest-rates/daily-treasury-rates.csv/{yr}/all?"
               f"type={type_param}&"
               f"field_tdr_date_value={yr}&page&_format=csv")
        try:
            req = urllib.request.Request(url, headers={"User-Agent":"Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=45) as r:
                body = r.read().decode("utf-8", errors="replace")
        except Exception as e:
            print(f"  [ERR] {kind} {yr}: {e}")
            continue

        rdr = csv.DictReader(io.StringIO(body))
        year_ins = 0
        for row in rdr:
            d = row.get("Date")
            if not d: continue
            try:
                obs = datetime.strptime(d, "%m/%d/%Y").strftime("%Y-%m-%d")
            except Exception:
                continue
            for col_name, sid_suffix, label in (
                    # NOMINAL columns
                    ("2 Yr",  "2Y",  "US 2Y Treasury Nominal Yield"),
                    ("10 Yr", "10Y", "US 10Y Treasury Nominal Yield"),
                    ("30 Yr", "30Y", "US 30Y Treasury Nominal Yield"),
                    # REAL columns (in the real CSV — TIPS)
                    ("5 YR",  "REAL5Y",  "US 5Y Real Yield (TIPS)"),
                    ("10 YR", "REAL10Y", "US 10Y Real Yield (TIPS)"),
                    ("20 YR", "REAL20Y", "US 20Y Real Yield (TIPS)"),
                    ("30 YR", "REAL30Y", "US 30Y Real Yield (TIPS)"),
            ):
                if col_name not in row:
                    continue
                v = (row.get(col_name) or "").strip()
                if not v or v.upper() in ("N/A", "NA"):
                    continue
                try:
                    val = float(v)
                except Exception:
                    continue
                sid = f"{series_prefix}_{sid_suffix}"
                label_full = f"{series_label_prefix} — {label}"
                insert_macro(db, sid, label_full, obs, val, "percent",
                             "US Treasury Direct", url, "D",
                             "EOD (business day close)")
                year_ins += 1
        db.commit()
        if year_ins:
            print(f"  {kind} {yr}: rows inserted={year_ins}")
            tot_ins += year_ins
    return tot_ins


# ────────────────────────────────────────────────────────────────────
# tvDatafeed — DXY / VIX / WTI / USDJPY / USDCNH / MOVE
# ────────────────────────────────────────────────────────────────────

def fetch_tv_daily(db, ticker_map, n_bars=800):
    """ticker_map: list of (series_id, series_label, symbol, exchange)."""
    try:
        from tvDatafeed import TvDatafeed, Interval
    except ImportError:
        print("  [SKIP] tvDatafeed not installed")
        return 0

    user = os.getenv("TRADINGVIEW_USERNAME", "")
    pw   = os.getenv("TRADINGVIEW_PASSWORD", "")
    try:
        client = TvDatafeed(user, pw) if user else TvDatafeed()
    except Exception as e:
        print(f"  [ERR] TV client init: {e}")
        return 0

    total = 0
    for series_id, series_label, symbol, exchange in ticker_map:
        try:
            df = client.get_hist(symbol=symbol, exchange=exchange,
                                 interval=Interval.in_daily, n_bars=n_bars)
        except Exception as e:
            print(f"  [ERR] TV {series_id} ({symbol}@{exchange}): {e}")
            continue
        if df is None or df.empty:
            print(f"  [EMPTY] TV {series_id}")
            continue
        for ts, row in df.iterrows():
            if hasattr(ts, "strftime"):
                obs = ts.strftime("%Y-%m-%d")
            else:
                obs = str(ts)[:10]
            try:
                val = float(row["close"])
            except Exception:
                continue
            unit = "index" if series_id.upper() in ("DXY","VIX","MOVE","SPX") else "price"
            insert_macro(db, series_id, series_label, obs, val, unit,
                         "TradingView", f"tv:{symbol}@{exchange}",
                         "D", "EOD/close")
            total += 1
        db.commit()
        # Report latest
        r = db.execute(text(
            "SELECT MIN(obs_date), MAX(obs_date), COUNT(*) FROM macro_series_raw WHERE series_id=:sid"
        ), {"sid": series_id}).fetchone()
        print(f"  TV {series_id:<12} {symbol}@{exchange:<8} n={r[2]}  {r[0]} → {r[1]}")
    return total


def compute_breakeven(db):
    """DERIVED: 10Y breakeven = 10Y_nominal − 10Y_real (per Treasury pair)."""
    r = db.execute(text("""
        SELECT n.obs_date, n.value AS nom, r.value AS real_val
        FROM macro_series_raw n
        JOIN macro_series_raw r ON n.obs_date = r.obs_date
        WHERE n.series_id='UST_10Y' AND r.series_id='UST_REAL10Y'
    """)).fetchall()
    ins = 0
    for x in r:
        be = x[1] - x[2]
        insert_macro(db, "US_BE_10Y_DERIVED",
                     "US 10Y Breakeven Inflation (DERIVED = 10Y nom − 10Y real)",
                     x[0], be, "percent",
                     "DERIVED (US Treasury Direct pair)",
                     "computed:nom-real", "D",
                     "EOD (derived)")
        ins += 1
    db.commit()
    return ins


# ────────────────────────────────────────────────────────────────────
# Main
# ────────────────────────────────────────────────────────────────────

def main():
    with SessionLocal() as db:
        print("=" * 68)
        print("PHASE 1B — MACRO / RATES BACKFILL")
        print("=" * 68)

        # 1) Treasury Direct nominal curve
        print("\n[1/4] US Treasury Direct — NOMINAL yield curve ...")
        n1 = fetch_treasury_yield_curve(db, "nominal", "UST",
                                         "US Treasury Nominal", 2019, 2027)
        print(f"  nominal rows: {n1}")

        # 2) Treasury Direct real yield curve (TIPS)
        print("\n[2/4] US Treasury Direct — REAL (TIPS) yield curve ...")
        n2 = fetch_treasury_yield_curve(db, "real", "UST",
                                         "US Treasury Real (TIPS)", 2019, 2027)
        print(f"  real rows: {n2}")

        # 3) DERIVED breakeven
        print("\n[3/4] DERIVED 10Y breakeven = 10Y nom − 10Y real ...")
        n3 = compute_breakeven(db)
        print(f"  breakeven derived rows: {n3}")

        # 4) tvDatafeed daily
        print("\n[4/4] TradingView daily bars ...")
        ticker_map = [
            # (series_id, label, TV symbol, exchange)
            ("DXY",       "US Dollar Index (spot, TVC)",           "DXY",     "TVC"),
            ("VIX",       "CBOE Volatility Index",                 "VIX",     "CBOE"),
            ("MOVE",      "ICE BofA MOVE Index (Treasury vol)",    "MOVE",    "TVC"),
            ("WTI",       "WTI Crude Oil (spot)",                  "USOIL",   "TVC"),
            ("USDJPY",    "USD/JPY spot",                          "USDJPY",  "OANDA"),
            ("USDCNH",    "USD/CNH spot (offshore yuan)",          "USDCNH",  "OANDA"),
            ("XAUUSD_D1", "XAU/USD spot (D1 close)",               "XAUUSD",  "OANDA"),
        ]
        n4 = fetch_tv_daily(db, ticker_map, n_bars=800)
        print(f"  TV rows inserted: {n4}")

        # ── Summary ──
        print()
        print("=" * 68)
        print("MACRO SERIES INVENTORY")
        print("=" * 68)
        r = db.execute(text("""
            SELECT series_id, series_name, provider, COUNT(*), MIN(obs_date), MAX(obs_date),
                   MAX(value)
            FROM macro_series_raw GROUP BY series_id, series_name, provider
            ORDER BY series_id
        """)).fetchall()
        for x in r:
            sid = x[0]
            # get latest value + obs_date
            latest = db.execute(text(
                "SELECT obs_date, value FROM macro_series_raw WHERE series_id=:s "
                "ORDER BY obs_date DESC LIMIT 1"), {"s": sid}).fetchone()
            print(f"  {sid:<20}  n={x[3]:>5}  {x[4]} → {x[5]}   "
                  f"latest: {latest[0]}  value={latest[1]:.4f}  provider={x[2]}")

        # ── Explicit NOT-AVAILABLE registry ──
        print()
        print("=" * 68)
        print("MACRO GAPS — DOCUMENTED NOT-AVAILABLE")
        print("=" * 68)
        gaps = [
            ("FED_FUNDS_EXPECT",   "Fed Funds Futures policy expectations", "CME (paid)",       "not free-tier automatable"),
            ("SOFR_CURVE",         "SOFR OIS curve",                        "CME/ICAP (paid)",  "not free-tier automatable"),
            ("US_BE_5Y_DIRECT",    "5Y Breakeven direct (FRED T5YIE)",      "FRED (blocked)",   "FRED endpoint times out from droplet"),
            ("US_BE_5Y5YFWD",      "5Y5Y forward inflation (FRED T5YIFR)",  "FRED (blocked)",   "FRED endpoint times out from droplet"),
        ]
        for sid, label, provider, reason in gaps:
            print(f"  NOT AVAILABLE: {sid:<20} {label:<44} provider={provider}  reason={reason}")

if __name__ == "__main__":
    main()
