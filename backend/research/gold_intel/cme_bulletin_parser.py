"""CME Metals Options Daily Bulletin — COMEX GOLD parser.

Parses the pdftotext -layout output of the CME Section 64 bulletin
into a structured research table. COMEX GOLD only.

Contract product coverage in this parser:
  OG  CALL/PUT        Standard COMEX Gold Options (Monthly + Serial)
  OG1..OG4 CALL/PUT   Gold Weekly Options (Mon/Tue/Wed/Thu/Fri variants)
  OMG OPT             Micro Gold Options
  MMG MON / WMG WED / FMG OPT   Micro Gold Weeklies
  GMW MON / GWR THU / GWT TUE / GWW WED   Gold Weekly (day-of-week) Options

Column mapping (verified against the Sep 04 2026 Bulletin #171):
  Strike | GlobexOpen | OpenOutcryOpenRange | GlobexHigh/Low
    | OpenOutcryHigh/Low | OpenOutcryCloseRange | SettPrice | PtChge
    | Delta | Exercises | OpenOutcryVolume | GlobexVolume | PntVolume
    | OpenInterest[+/-marker] | OpenInterestChange
"""
from __future__ import annotations

import re
import hashlib
from dataclasses import dataclass, field, asdict
from datetime import datetime, date
from typing import Optional


# ─── Product identity table ────────────────────────────────────────────
# code, opt_type_keyword, full_name_needle, is_gold, is_micro
GOLD_PRODUCTS = {
    ("OG",  "CALL"): ("OG",   "CALL", "COMEX GOLD OPTIONS",           True, False),
    ("OG",  "PUT"):  ("OG",   "PUT",  "COMEX GOLD OPTIONS",           True, False),
    ("OG1", "CALL"): ("OG1",  "CALL", "GOLD OPTIONS",                  True, False),
    ("OG1", "PUT"):  ("OG1",  "PUT",  "GOLD OPTIONS",                  True, False),
    ("OG2", "CALL"): ("OG2",  "CALL", "GOLD OPTIONS",                  True, False),
    ("OG2", "PUT"):  ("OG2",  "PUT",  "GOLD OPTIONS",                  True, False),
    ("OG3", "CALL"): ("OG3",  "CALL", "GOLD OPTIONS",                  True, False),
    ("OG3", "PUT"):  ("OG3",  "PUT",  "GOLD OPTIONS",                  True, False),
    ("OG4", "CALL"): ("OG4",  "CALL", "GOLD OPTIONS",                  True, False),
    ("OG4", "PUT"):  ("OG4",  "PUT",  "GOLD OPTIONS",                  True, False),
    ("OMG", "OPT"):  ("OMG",  "OPT",  "MICRO GOLD OPTIONS",            True, True),
    ("WMG", "WED"):  ("WMG",  "WED",  "MICRO GOLD WEEKLY WED OPTION",  True, True),
    ("MMG", "MON"):  ("MMG",  "MON",  "MICRO GOLD WEEKLY MONDAY OPTION", True, True),
    ("FMG", "OPT"):  ("FMG",  "OPT",  "MICRO GOLD WEEKLY FRI OPTION",  True, True),
    ("GMW", "MON"):  ("GMW",  "MON",  "GOLD WEEKLY MONDAY OPTION",     True, False),
    ("GWR", "THU"):  ("GWR",  "THU",  "GOLD WEEKLY THURSDAY OPTION",   True, False),
    ("GWT", "TUE"):  ("GWT",  "TUE",  "GOLD WEEKLY TUESDAY OPTION",    True, False),
    ("GWW", "WED"):  ("GWW",  "WED",  "GOLD WEEKLY WEDNESDAY OPTION",  True, False),
}

# Regex — a "section header" line marks the start of a new product page
_RX_SECTION = re.compile(
    r"^(OG|OG[1-4]|OMG|WMG|MMG|FMG|GMW|GWR|GWT|GWW|HRO|HX|HXE|HMW|LHO|EHO|COO|ECG\d+|ECGC|ECHG|ECSI|HWR|HWT|HWW|PAO|PAW|PLW|PO|RWS|SMW|SO|SO\d+)"
    r"\s+(CALL|PUT|OPT|MON|TUE|WED|THU|FRI|OOF)\b"
)

# Expiry line — e.g., "OCT26", "MAY27"
_RX_EXPIRY = re.compile(r"^\s*([A-Z]{3})(\d{2})\s*$")

# Data row — first token must be a whole number (strike) between 10 and 99999
# followed by at least a few dashes / numbers
_RX_DATA_ROW = re.compile(r"^\s{0,6}(\d{2,5})\s{2,}[\-A-Za-z0-9\.\+\/\* ]")

# Total row — "TOTAL" on its own line
_RX_TOTAL = re.compile(r"^\s*TOTAL\b")

_MONTH_CODE = {"JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6,
                "JUL": 7, "AUG": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12}


@dataclass
class OptionRow:
    bulletin_date: str
    bulletin_status: str            # PRELIMINARY | FINAL
    bulletin_number: str
    source_file: str
    source_page_hint: int           # section start line (heuristic)
    product_code: str               # OG | OG1 | ... | OMG | WMG | MMG | FMG | GMW | GWR | GWT | GWW
    product_name: str               # "COMEX GOLD OPTIONS" | ...
    option_type: str                # CALL | PUT | OPT (for combined)
    option_expiry_code: str         # e.g. "OCT26"
    option_expiry_date: Optional[str] = None
    strike: float = 0.0
    globex_open: Optional[str] = None
    open_outcry_open_range: Optional[str] = None
    globex_high_low: Optional[str] = None
    open_outcry_high_low: Optional[str] = None
    open_outcry_close_range: Optional[str] = None
    settlement: Optional[float] = None
    settlement_change: Optional[float] = None
    settlement_change_flag: Optional[str] = None       # UNCH | NEW | none
    delta_cme: Optional[float] = None
    exercises: Optional[int] = None
    open_outcry_volume: Optional[int] = None
    globex_volume: Optional[int] = None
    pnt_volume: Optional[int] = None
    open_interest: Optional[int] = None
    oi_direction_marker: Optional[str] = None          # +, -, blank
    open_interest_change: Optional[int] = None
    oi_change_flag: Optional[str] = None               # UNCH | NEW | signed
    raw_row: str = ""
    raw_row_hash: str = ""
    parse_status: str = "PARSED"


def _to_float(s: Optional[str]) -> Optional[float]:
    if s is None: return None
    s = s.strip()
    if s in ("", "----", "-", "UNCH", "N/A", "NEW"): return None
    # settlement change like "- 63.00"
    s = s.replace(" ", "")
    # allow leading '+'
    try: return float(s)
    except Exception: return None


def _to_int(s: Optional[str]) -> Optional[int]:
    if s is None: return None
    s = s.strip()
    if s in ("", "----", "-", "UNCH", "N/A", "NEW"): return None
    s = s.replace(",", "").replace(" ", "")
    try: return int(float(s))
    except Exception: return None


def _split_row(raw: str) -> list[str]:
    """Split a data row by runs of 2+ spaces, preserving 'X.X' and 'X.XB/X.XA' cells."""
    parts = re.split(r"\s{2,}", raw.strip())
    # Post-process: rejoin "- N" (settlement change) if isolated
    return [p.strip() for p in parts if p.strip() != ""]


def _decode_expiry(code: str) -> Optional[str]:
    m = _RX_EXPIRY.match(code)
    if not m: return None
    mon = _MONTH_CODE.get(m.group(1))
    yy = int(m.group(2))
    if not mon: return None
    yyyy = 2000 + yy
    return f"{yyyy:04d}-{mon:02d}"        # calendar month only; day resolved via LTD table


def parse_bulletin(text: str, source_file: str) -> tuple[dict, list[OptionRow]]:
    """Returns (metadata, list of OptionRow)."""
    lines = text.splitlines()
    meta = {"bulletin_date": None, "bulletin_status": None,
             "bulletin_number": None, "source_file": source_file}

    # ── Metadata pass ─────────────────────────────────────────
    for l in lines[:40]:
        u = l.strip().upper()
        if u == "PRELIMINARY" or u == "FINAL":
            meta["bulletin_status"] = u
        m = re.search(r"BULLETIN #(\d+)", l)
        if m: meta["bulletin_number"] = m.group(1)
        m = re.search(r"([A-Z][a-z]{2}, [A-Z][a-z]{2} \d{2}, \d{4})", l)
        if m:
            try:
                dt = datetime.strptime(m.group(1), "%a, %b %d, %Y")
                meta["bulletin_date"] = dt.strftime("%Y-%m-%d")
            except Exception: pass

    # ── Row extraction with per-section state ─────────────────
    rows: list[OptionRow] = []
    cur_prod_code = None
    cur_prod_type = None
    cur_prod_name = None
    cur_prod_line = 0
    cur_expiry = None

    def is_gold(code, typ):
        return (code, typ) in GOLD_PRODUCTS

    for i, l in enumerate(lines):
        # Skip empty / footer / TOTAL rows
        if not l.strip(): continue
        if l.startswith("PG64") or l.startswith("Side "): continue
        if l.startswith("64  ") or "METALS OPTIONS PRODUCTS" in l: continue
        if "THE INFORMATION CONTAINED" in l: continue
        if "IS ACCEPTED BY" in l: continue
        if "Copyright CME" in l: continue

        # New section?
        m = _RX_SECTION.match(l.strip())
        if m:
            code = m.group(1)
            typ  = m.group(2)
            # Look for gold product signature on same line
            if is_gold(code, typ):
                _, _, needle, _, _ = GOLD_PRODUCTS[(code, typ)]
                if needle.lower() in l.lower():
                    cur_prod_code = code
                    cur_prod_type = typ
                    cur_prod_name = needle
                    cur_prod_line = i
                    cur_expiry = None
                    continue
                # Handle "OG CALL         COMEX GOLD OPTIONS" or
                # "OG3 CALL        GOLD OPTIONS" line where product name is on same line.
                # But some pages the product+name appear on one line and the expiry
                # appears on the next line. If the current line's second half doesn't
                # contain the needle, still register (needle can be inferred).
                cur_prod_code = code
                cur_prod_type = typ
                cur_prod_name = needle
                cur_prod_line = i
                cur_expiry = None
                continue
            else:
                # Non-gold product; clear state
                cur_prod_code = None
                cur_prod_type = None
                cur_prod_name = None
                cur_expiry = None
                continue

        # Expiry line
        m = _RX_EXPIRY.match(l)
        if m and cur_prod_code:
            cur_expiry = m.group(1) + m.group(2)   # e.g. "OCT26"
            continue

        # Skip TOTAL rows
        if _RX_TOTAL.match(l): continue

        # Data row
        if cur_prod_code and cur_expiry:
            # Fast pre-filter: has a strike-looking left token
            dm = _RX_DATA_ROW.match(l)
            if not dm: continue
            strike_str = dm.group(1)
            try: strike = float(strike_str)
            except Exception: continue
            parts = _split_row(l)
            # parts[0] should be strike; parts[1:] the fields.
            if not parts or parts[0] != strike_str: continue

            # Map tokens to columns
            def pick(idx):
                return parts[idx] if idx < len(parts) else None

            row = OptionRow(
                bulletin_date=meta["bulletin_date"] or "",
                bulletin_status=meta["bulletin_status"] or "",
                bulletin_number=meta["bulletin_number"] or "",
                source_file=source_file,
                source_page_hint=cur_prod_line,
                product_code=cur_prod_code,
                product_name=cur_prod_name or "",
                option_type=cur_prod_type,
                option_expiry_code=cur_expiry,
                option_expiry_date=_decode_expiry(cur_expiry),
                strike=strike,
                globex_open=pick(1),
                open_outcry_open_range=pick(2),
                globex_high_low=pick(3),
                open_outcry_high_low=pick(4),
                open_outcry_close_range=pick(5),
                settlement=_to_float(pick(6)),
                settlement_change=None,
                settlement_change_flag=None,
                delta_cme=None,
                exercises=_to_int(pick(9)),
                open_outcry_volume=_to_int(pick(10)),
                globex_volume=_to_int(pick(11)),
                pnt_volume=_to_int(pick(12)),
                open_interest=None,
                oi_direction_marker=None,
                open_interest_change=None,
                oi_change_flag=None,
                raw_row=l,
                raw_row_hash=hashlib.md5(l.encode("utf-8", "replace")).hexdigest()[:16],
            )

            # ── SETTLEMENT CHANGE handling
            sc_str = pick(7)
            if sc_str is not None:
                sc_norm = sc_str.strip().replace(" ", "")
                if sc_norm in ("UNCH",):
                    row.settlement_change = 0.0
                    row.settlement_change_flag = "UNCH"
                elif sc_norm in ("NEW",):
                    row.settlement_change = None
                    row.settlement_change_flag = "NEW"
                elif sc_norm not in ("", "----"):
                    try: row.settlement_change = float(sc_norm)
                    except Exception: row.settlement_change = None

            # ── DELTA — CME uses positive magnitude with leading dot; for OTM = ".0000"
            d_str = pick(8)
            if d_str is not None and d_str.strip() not in ("", "----"):
                try:
                    d_val = float(d_str)
                    # Sign convention: bulletin publishes UNSIGNED magnitudes
                    # (calls: positive, puts: printed as positive magnitude too).
                    # Apply sign here to match Black-76 convention:
                    if row.option_type.upper() == "PUT" and d_val > 0:
                        d_val = -d_val
                    row.delta_cme = d_val
                except Exception:
                    row.delta_cme = None

            # ── OI + OI change — the last 1-3 tokens depending on layout
            # Common shapes:
            #   ["...", "70 -", "UNCH"]        → OI=70, marker=-, change=UNCH
            #   ["...", "50 +", "1"]           → OI=50, marker=+, change=+1
            #   ["...", "13", "UNCH"]          → OI=13, no marker, change=UNCH
            #   ["...", "119"]                 → OI=119 only (rare)
            #   ["...", "1", "1"]              → OI=1, change=+1
            # Last two tokens are typically (OI, change), where the OI cell may
            # embed a marker "N +" or "N -" as a single space-separated token.
            tail_tokens = parts[-2:] if len(parts) >= 2 else parts[-1:]
            if tail_tokens:
                oi_cell = tail_tokens[0]
                # OI cell may contain "70 -" or "50 +" — split on a single internal space
                mm = re.match(r"^(\d+)\s*([+\-])?$", oi_cell.strip())
                if mm:
                    row.open_interest = int(mm.group(1))
                    row.oi_direction_marker = mm.group(2)
                else:
                    row.open_interest = _to_int(oi_cell)
                if len(tail_tokens) >= 2:
                    oc = tail_tokens[1].strip()
                    if oc == "UNCH":
                        row.open_interest_change = 0
                        row.oi_change_flag = "UNCH"
                    elif oc == "NEW":
                        row.open_interest_change = None
                        row.oi_change_flag = "NEW"
                    else:
                        row.open_interest_change = _to_int(oc)

            rows.append(row)

    return meta, rows


if __name__ == "__main__":
    import sys, json
    if len(sys.argv) < 2:
        print("usage: cme_bulletin_parser.py <bulletin_layout.txt>")
        sys.exit(2)
    with open(sys.argv[1], "rb") as f:
        raw = f.read()
    text = raw.decode("cp1252", errors="replace")
    src = sys.argv[1].split("/")[-1]
    meta, rows = parse_bulletin(text, src)
    print(json.dumps({"metadata": meta, "n_rows": len(rows)}, indent=2))
    # Print a sample of rows per product+expiry
    from collections import defaultdict
    by = defaultdict(list)
    for r in rows:
        by[(r.product_code, r.option_type, r.option_expiry_code)].append(r)
    for key, rs in sorted(by.items()):
        print(f"\n-- {key[0]} {key[1]} {key[2]}  n={len(rs)} --")
        for r in rs[:3]:
            d = asdict(r)
            print("   K={strike}  sett={settlement}  dSett={settlement_change}({settlement_change_flag})  delta={delta_cme}  OI={open_interest}  dOI={open_interest_change}({oi_change_flag})  gvol={globex_volume}  exer={exercises}".format(**d))
