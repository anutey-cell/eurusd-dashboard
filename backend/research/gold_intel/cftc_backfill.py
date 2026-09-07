"""CFTC Disaggregated COT — full backfill from official annual archives.

Sources:
  fut_disagg_txt_hist_2006_2016.zip                 # Futures Only, 2006-2016 bulk
  fut_disagg_txt_YYYY.zip   for YYYY in 2017..2026  # Futures Only, per year
  com_disagg_txt_hist_2006_2016.zip                 # F+O Combined bulk (if exists)
  com_disagg_txt_YYYY.zip   for YYYY in 2017..2026  # F+O Combined per year
  f_disagg.txt              current week Futures Only
  c_disagg.txt (if present) current week F+O Combined

Gold contract: cftc_commodity_code = '088691'
             or market_and_exchange_names LIKE 'GOLD - COMMODITY EXCHANGE INC.%'
"""
import csv
import io
import json
import time
import urllib.request
import zipfile
from datetime import datetime, timezone
from database import SessionLocal
from sqlalchemy import text

GOLD_CODE = "088691"
BASE = "https://www.cftc.gov/files/dea/history/"

# Column names for the DISAGGREGATED FUTURES-ONLY format.
# This layout is documented in the CFTC "Commitments of Traders — Explanatory Notes"
# and has been stable across annual archives.
DISAGG_FUT_COLS = [
    "market_and_exchange_names",       # 0
    "as_of_date_yymmdd",               # 1
    "report_date_yyyy_mm_dd",          # 2
    "cftc_contract_market_code",       # 3
    "cftc_market_code",                # 4
    "cftc_region_code",                # 5
    "cftc_commodity_code",             # 6
    "open_interest_all",               # 7
    # Producer / Merchant / Processor / User
    "prod_merc_positions_long_all",    # 8
    "prod_merc_positions_short_all",   # 9
    # Swap Dealers
    "swap_positions_long_all",         # 10
    "swap_positions_short_all",        # 11
    "swap_positions_spread_all",       # 12
    # Managed Money
    "m_money_positions_long_all",      # 13
    "m_money_positions_short_all",     # 14
    "m_money_positions_spread_all",    # 15
    # Other Reportables
    "other_rept_positions_long_all",   # 16
    "other_rept_positions_short_all",  # 17
    "other_rept_positions_spread_all", # 18
    # Total Reportables
    "tot_rept_positions_long_all",     # 19
    "tot_rept_positions_short_all",    # 20
    # Non-reportable
    "nonrept_positions_long_all",      # 21
    "nonrept_positions_short_all",     # 22
    # Weekly changes
    "change_in_open_interest_all",     # 23
    "change_in_prod_merc_long",        # 24
    "change_in_prod_merc_short",       # 25
    "change_in_swap_long",             # 26
    "change_in_swap_short",            # 27
    "change_in_swap_spread",           # 28
    "change_in_m_money_long",          # 29
    "change_in_m_money_short",         # 30
    "change_in_m_money_spread",        # 31
    "change_in_other_rept_long",       # 32
    "change_in_other_rept_short",      # 33
    "change_in_other_rept_spread",     # 34
    "change_in_tot_rept_long",         # 35
    "change_in_tot_rept_short",        # 36
    "change_in_nonrept_long",          # 37
    "change_in_nonrept_short",         # 38
    # % of OI ...
    "pct_of_open_interest_all",        # 39
    "pct_of_oi_prod_merc_long",        # 40
    "pct_of_oi_prod_merc_short",       # 41
    "pct_of_oi_swap_long",             # 42
    "pct_of_oi_swap_short",            # 43
    "pct_of_oi_swap_spread",           # 44
    "pct_of_oi_m_money_long",          # 45
    "pct_of_oi_m_money_short",         # 46
    "pct_of_oi_m_money_spread",        # 47
    "pct_of_oi_other_rept_long",       # 48
    "pct_of_oi_other_rept_short",      # 49
    "pct_of_oi_other_rept_spread",     # 50
    "pct_of_oi_tot_rept_long",         # 51
    "pct_of_oi_tot_rept_short",        # 52
    "pct_of_oi_nonrept_long",          # 53
    "pct_of_oi_nonrept_short",         # 54
    # Trader counts follow ...
]

def to_int(s):
    if s is None: return None
    s = str(s).strip()
    if not s or s in ("."): return None
    try: return int(float(s))
    except Exception: return None

def parse_row(row_arr, report_type):
    """Given a list of CSV fields, extract into our schema."""
    if len(row_arr) < 25:  # sanity
        return None
    d = {}
    for i, k in enumerate(DISAGG_FUT_COLS):
        if i >= len(row_arr): break
        d[k] = row_arr[i]

    # Only keep gold: match by contract market code OR canonical market name.
    # In the TXT archives, field[6] is the SHORT commodity code (e.g. "088" for
    # gold), and field[3] is the full CFTC_Contract_Market_Code ("088691").
    contract_code = (d.get("cftc_contract_market_code") or "").strip()
    market_name = (d.get("market_and_exchange_names") or "").strip().upper()
    if contract_code != GOLD_CODE and not market_name.startswith("GOLD - COMMODITY EXCHANGE INC"):
        return None

    rep_date_raw = (d.get("report_date_yyyy_mm_dd") or "").strip()
    if not rep_date_raw or len(rep_date_raw) < 10:
        return None

    values = {
        "report_type": report_type,
        "cftc_commodity_code": GOLD_CODE,
        "market_and_exchange_names": (d.get("market_and_exchange_names") or "").strip(),
        "report_date_yyyy_mm_dd": rep_date_raw[:10],
        "publication_date_utc": None,
        "open_interest_all":                 to_int(d.get("open_interest_all")),
        "prod_merc_positions_long_all":      to_int(d.get("prod_merc_positions_long_all")),
        "prod_merc_positions_short_all":     to_int(d.get("prod_merc_positions_short_all")),
        "swap_positions_long_all":           to_int(d.get("swap_positions_long_all")),
        "swap_positions_short_all":          to_int(d.get("swap_positions_short_all")),
        "swap_positions_spread_all":         to_int(d.get("swap_positions_spread_all")),
        "m_money_positions_long_all":        to_int(d.get("m_money_positions_long_all")),
        "m_money_positions_short_all":       to_int(d.get("m_money_positions_short_all")),
        "m_money_positions_spread_all":      to_int(d.get("m_money_positions_spread_all")),
        "other_rept_positions_long_all":     to_int(d.get("other_rept_positions_long_all")),
        "other_rept_positions_short_all":    to_int(d.get("other_rept_positions_short_all")),
        "other_rept_positions_spread_all":   to_int(d.get("other_rept_positions_spread_all")),
        "tot_rept_positions_long_all":       to_int(d.get("tot_rept_positions_long_all")),
        "tot_rept_positions_short_all":      to_int(d.get("tot_rept_positions_short_all")),
        "nonrept_positions_long_all":        to_int(d.get("nonrept_positions_long_all")),
        "nonrept_positions_short_all":       to_int(d.get("nonrept_positions_short_all")),
        "change_in_open_interest_all":       to_int(d.get("change_in_open_interest_all")),
        "change_in_prod_merc_long":          to_int(d.get("change_in_prod_merc_long")),
        "change_in_prod_merc_short":         to_int(d.get("change_in_prod_merc_short")),
        "change_in_swap_long":               to_int(d.get("change_in_swap_long")),
        "change_in_swap_short":              to_int(d.get("change_in_swap_short")),
        "change_in_m_money_long":            to_int(d.get("change_in_m_money_long")),
        "change_in_m_money_short":           to_int(d.get("change_in_m_money_short")),
        "change_in_other_rept_long":         to_int(d.get("change_in_other_rept_long")),
        "change_in_other_rept_short":        to_int(d.get("change_in_other_rept_short")),
        "raw_json": json.dumps(d),
    }
    return values

def fetch_zip_and_parse(url, report_type):
    """Download the archive zip, extract .txt, parse gold rows only."""
    print(f"  fetching {url}")
    req = urllib.request.Request(url, headers={"User-Agent":"Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=120) as r:
        blob = r.read()
    zf = zipfile.ZipFile(io.BytesIO(blob))
    rows_out = []
    for member in zf.namelist():
        if not member.lower().endswith((".txt", ".csv")):
            continue
        with zf.open(member) as f:
            text_data = f.read().decode("latin-1", errors="replace")
        # The archive .txt files use CSV with quoted fields
        rdr = csv.reader(io.StringIO(text_data))
        for row_arr in rdr:
            if not row_arr:
                continue
            parsed = parse_row(row_arr, report_type)
            if parsed:
                rows_out.append((parsed, url))
    return rows_out

def fetch_txt_and_parse(url, report_type):
    """Fetch a plain .txt weekly file and parse gold rows."""
    print(f"  fetching {url}")
    req = urllib.request.Request(url, headers={"User-Agent":"Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=60) as r:
        text_data = r.read().decode("latin-1", errors="replace")
    rdr = csv.reader(io.StringIO(text_data))
    rows_out = []
    for row_arr in rdr:
        if not row_arr: continue
        parsed = parse_row(row_arr, report_type)
        if parsed:
            rows_out.append((parsed, url))
    return rows_out

def bulk_insert(db, rows_with_url):
    """Insert into cftc_cot_raw via INSERT OR IGNORE."""
    for parsed, url in rows_with_url:
        parsed["source_url"] = url
        db.execute(text("""
            INSERT OR IGNORE INTO cftc_cot_raw (
                report_type, cftc_commodity_code, market_and_exchange_names,
                report_date_yyyy_mm_dd, publication_date_utc,
                open_interest_all,
                prod_merc_positions_long_all, prod_merc_positions_short_all,
                swap_positions_long_all, swap_positions_short_all, swap_positions_spread_all,
                m_money_positions_long_all, m_money_positions_short_all, m_money_positions_spread_all,
                other_rept_positions_long_all, other_rept_positions_short_all, other_rept_positions_spread_all,
                tot_rept_positions_long_all, tot_rept_positions_short_all,
                nonrept_positions_long_all, nonrept_positions_short_all,
                change_in_open_interest_all,
                change_in_prod_merc_long, change_in_prod_merc_short,
                change_in_swap_long, change_in_swap_short,
                change_in_m_money_long, change_in_m_money_short,
                change_in_other_rept_long, change_in_other_rept_short,
                raw_json, source_url
            ) VALUES (
                :report_type, :cftc_commodity_code, :market_and_exchange_names,
                :report_date_yyyy_mm_dd, :publication_date_utc,
                :open_interest_all,
                :prod_merc_positions_long_all, :prod_merc_positions_short_all,
                :swap_positions_long_all, :swap_positions_short_all, :swap_positions_spread_all,
                :m_money_positions_long_all, :m_money_positions_short_all, :m_money_positions_spread_all,
                :other_rept_positions_long_all, :other_rept_positions_short_all, :other_rept_positions_spread_all,
                :tot_rept_positions_long_all, :tot_rept_positions_short_all,
                :nonrept_positions_long_all, :nonrept_positions_short_all,
                :change_in_open_interest_all,
                :change_in_prod_merc_long, :change_in_prod_merc_short,
                :change_in_swap_long, :change_in_swap_short,
                :change_in_m_money_long, :change_in_m_money_short,
                :change_in_other_rept_long, :change_in_other_rept_short,
                :raw_json, :source_url
            )
        """), parsed)
    db.commit()

def main():
    with SessionLocal() as db:
        print("=" * 68)
        print("CFTC DISAGGREGATED COT — GOLD BACKFILL (fut_disagg)")
        print("=" * 68)

        # Purge any accidental prior test rows for a clean re-ingestion
        n_before = db.execute(text("SELECT COUNT(*) FROM cftc_cot_raw")).scalar()
        print(f"cftc_cot_raw rows before: {n_before}")

        # 1. Historical bulk 2006-2016
        url = BASE + "fut_disagg_txt_hist_2006_2016.zip"
        try:
            rows = fetch_zip_and_parse(url, "FUT_ONLY")
            print(f"    parsed {len(rows)} gold rows")
            bulk_insert(db, rows)
        except Exception as e:
            print(f"    ERR {e}")

        # 2. Annual archives 2017-2026
        for yr in range(2017, 2027):
            url = BASE + f"fut_disagg_txt_{yr}.zip"
            try:
                rows = fetch_zip_and_parse(url, "FUT_ONLY")
                print(f"    parsed {len(rows)} gold rows")
                bulk_insert(db, rows)
            except Exception as e:
                print(f"    ERR yr={yr}  {e}")

        # 3. F+O Combined bulk (probe if available)
        combined_urls = [
            (BASE + "com_disagg_txt_hist_2006_2016.zip", "COMBINED"),
        ]
        for yr in range(2017, 2027):
            combined_urls.append((BASE + f"com_disagg_txt_{yr}.zip", "COMBINED"))
        for url, rt in combined_urls:
            try:
                rows = fetch_zip_and_parse(url, rt)
                print(f"    parsed {len(rows)} gold rows  ({rt})")
                bulk_insert(db, rows)
            except Exception as e:
                print(f"    ERR {url}: {e}")

        # 4. Current week (may not yet be in the annual zip)
        for url, rt in [
            ("https://www.cftc.gov/dea/newcot/f_disagg.txt", "FUT_ONLY"),
            ("https://www.cftc.gov/dea/newcot/c_disagg.txt", "COMBINED"),
        ]:
            try:
                rows = fetch_txt_and_parse(url, rt)
                print(f"  current-week {rt}: {len(rows)} gold rows")
                bulk_insert(db, rows)
            except Exception as e:
                print(f"    ERR {url}: {e}")

        # Final report
        print()
        print("=" * 68)
        print("BACKFILL RESULT")
        print("=" * 68)
        for rt in ("FUT_ONLY", "COMBINED"):
            r = db.execute(text(
                "SELECT COUNT(*), MIN(report_date_yyyy_mm_dd), MAX(report_date_yyyy_mm_dd) "
                "FROM cftc_cot_raw WHERE report_type=:rt"
            ), {"rt": rt}).fetchone()
            print(f"  {rt}: rows={r[0]}   min={r[1]}   max={r[2]}")

        # Any duplicate report dates?
        r = db.execute(text(
            "SELECT report_type, report_date_yyyy_mm_dd, COUNT(*) as n "
            "FROM cftc_cot_raw GROUP BY 1,2 HAVING n>1 LIMIT 10"
        )).fetchall()
        print(f"  duplicates: {len(r)}")
        if r:
            for x in r: print(f"    {x}")

        # Latest row detail
        latest = db.execute(text(
            "SELECT report_date_yyyy_mm_dd, report_type, open_interest_all, "
            "m_money_positions_long_all, m_money_positions_short_all, "
            "prod_merc_positions_long_all, prod_merc_positions_short_all, "
            "swap_positions_long_all, swap_positions_short_all "
            "FROM cftc_cot_raw WHERE report_type='FUT_ONLY' "
            "ORDER BY report_date_yyyy_mm_dd DESC LIMIT 3"
        )).fetchall()
        print("  Latest 3 FUT_ONLY rows:")
        for row in latest:
            print(f"    {dict(row._mapping)}")

if __name__ == "__main__":
    main()
