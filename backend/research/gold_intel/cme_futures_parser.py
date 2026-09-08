"""CME Metals Futures Bulletin — COMEX GOLD (GC + MGC) parser.

Parses the pdftotext -layout output of the CME Metals Futures section into
structured rows for GC / MGC futures. Only rows required for gold
research are extracted.

Column mapping (verified against the Sep 04 2026 bulletin, GC FUT):
  contract_month | session_open | globex_high/low | settlement + change
  | globex_volume | open_outcry_volume | open_interest[+/- marker]
  | oi_change
"""
from __future__ import annotations
import re, hashlib
from dataclasses import dataclass, asdict
from typing import Optional

_RX_PRODUCT = re.compile(
    r"^\s*(GC|MGC|1OZ|QO)\s+FUT\b\s*(.*)"
)
_RX_MONTH = re.compile(r"^\s*([A-Z]{3}\d{2})\b")
_RX_TOTAL = re.compile(r"^\s*TOTAL\b")

_GOLD_PRODUCTS = {"GC", "MGC", "1OZ", "QO"}

@dataclass
class FuturesRow:
    bulletin_date: str
    bulletin_status: str
    bulletin_number: str
    source_file: str
    source_page_hint: int
    product_code: str
    product_name: str
    contract_month_code: str        # e.g. DEC26
    contract_month_iso: Optional[str] = None
    contract_symbol: Optional[str] = None
    session_open: Optional[float] = None
    globex_high: Optional[float] = None
    globex_low: Optional[float] = None
    settlement: Optional[float] = None
    settlement_change: Optional[float] = None
    settlement_change_flag: Optional[str] = None
    globex_volume: Optional[int] = None
    open_outcry_volume: Optional[int] = None
    open_interest: Optional[int] = None
    oi_direction_marker: Optional[str] = None
    oi_change: Optional[int] = None
    oi_change_flag: Optional[str] = None
    raw_row: str = ""
    raw_row_hash: str = ""
    parse_status: str = "PARSED"

_MONTH_CODE = {"JAN":1,"FEB":2,"MAR":3,"APR":4,"MAY":5,"JUN":6,
                "JUL":7,"AUG":8,"SEP":9,"OCT":10,"NOV":11,"DEC":12}
_CME_MONTH_LETTER = {1:"F",2:"G",3:"H",4:"J",5:"K",6:"M",
                      7:"N",8:"Q",9:"U",10:"V",11:"X",12:"Z"}

def _month_iso(code: str) -> Optional[str]:
    m = re.match(r"^([A-Z]{3})(\d{2})$", code)
    if not m: return None
    mm = _MONTH_CODE.get(m.group(1))
    if not mm: return None
    yy = 2000 + int(m.group(2))
    return f"{yy:04d}-{mm:02d}"

def _contract_symbol(product: str, code: str) -> Optional[str]:
    m = re.match(r"^([A-Z]{3})(\d{2})$", code)
    if not m: return None
    mm = _MONTH_CODE.get(m.group(1))
    if not mm: return None
    letter = _CME_MONTH_LETTER[mm]
    return f"{product}{letter}{m.group(2)}"

def _to_float(s):
    if s is None: return None
    s = s.strip().replace(",", "").replace(" ", "")
    if s in ("", "----", "-", "UNCH", "NEW"): return None
    # Handle "4487.30A", "4468.40B" (bid/ask markers) - strip trailing letter
    s = re.sub(r"[ABP]$", "", s)
    try: return float(s)
    except Exception: return None

def _to_int(s):
    if s is None: return None
    s = s.strip().replace(",", "").replace(" ", "")
    if s in ("", "----", "-", "UNCH", "NEW"): return None
    try: return int(float(s))
    except Exception: return None

def _split(l: str) -> list[str]:
    """Split by 2+ spaces then reassemble 'N.NN -/+ N.NN' fragments (settlement + change)
    which the naive split breaks apart when CME renders them with a 2-space gap."""
    parts = [p.strip() for p in re.split(r"\s{2,}", l.strip()) if p.strip() != ""]
    out = []
    i = 0
    while i < len(parts):
        p = parts[i]
        # Case: "4476.60 -" or "4476.60 +" — merge with next numeric fragment
        m = re.match(r"^([\d,]+\.\d+)\s+([+\-])$", p)
        if m and i + 1 < len(parts) and re.match(r"^[\d,]+\.\d+$", parts[i+1]):
            merged = f"{m.group(1)} {m.group(2)} {parts[i+1]}"
            out.append(merged)
            i += 2
            continue
        out.append(p)
        i += 1
    return out


def parse_futures(text: str, meta: dict, source_file: str) -> list[FuturesRow]:
    """meta must contain bulletin_date/bulletin_status/bulletin_number
    from the detector."""
    lines = text.splitlines()
    rows: list[FuturesRow] = []
    cur_product = None
    cur_product_name = None
    cur_product_line = 0

    # Regex to detect any 'XXX FUT' product code (to reset state on non-gold products)
    _RX_ANY_PRODUCT = re.compile(r"^\s*([A-Z0-9]{1,5})\s+FUT\b")

    for i, l in enumerate(lines):
        # ── ANY product-header line resets state; only gold sets cur_product ──
        m = _RX_PRODUCT.match(l)
        if m:
            code = m.group(1)
            desc = m.group(2).strip()
            if code in _GOLD_PRODUCTS:
                cur_product = code
                cur_product_name = re.split(r"\s{2,}", desc)[0] if desc else code
                cur_product_line = i
            else:
                cur_product = None
                cur_product_name = None
            continue
        # Non-gold "XXX FUT" header — reset state so we don't attribute HDG / HG etc rows to gold
        m_any = _RX_ANY_PRODUCT.match(l)
        if m_any and m_any.group(1) not in _GOLD_PRODUCTS:
            cur_product = None
            cur_product_name = None
            continue

        if cur_product is None:
            continue

        # ── skip TOTAL / disclaimer / page-header lines ──
        if _RX_TOTAL.match(l): continue
        if "THE INFORMATION" in l or "Copyright CME" in l: continue
        if l.startswith("PG") or l.startswith("Side ") or "METAL FUTURES" in l:
            continue
        if not l.strip(): continue

        # ── month row candidate ──
        mm = _RX_MONTH.match(l)
        if not mm: continue
        month = mm.group(1)
        parts = _split(l)
        if not parts or parts[0] != month: continue

        # Map: month | open | high/low | settlement + change | globex_vol
        #    | outcry_vol | open_interest [dir] | oi_change
        def pick(k): return parts[k] if k < len(parts) else None
        row_open = _to_float(pick(1))
        # High/Low cell
        hl = pick(2) or ""
        gh = gl = None
        if "/" in hl:
            h, low = hl.split("/", 1)
            gh = _to_float(h)
            gl = _to_float(low)
        elif hl not in ("", "----"):
            gh = _to_float(hl)
        # Settlement + change cell — may be "4441.90 - 63.00" as one token
        sc = pick(3) or ""
        sett = None; d_sett = None; d_flag = None
        if sc:
            sc_norm = sc.strip()
            m3 = re.match(r"^([\d,]+\.\d+)\s*([+\-]?\s*[\d\.]+|UNCH|NEW)?$", sc_norm)
            if m3:
                sett = _to_float(m3.group(1))
                cc = (m3.group(2) or "").strip()
                if cc == "UNCH": d_sett = 0.0; d_flag = "UNCH"
                elif cc == "NEW": d_sett = None; d_flag = "NEW"
                elif cc: d_sett = _to_float(cc.replace(" ", ""))
        gvol   = _to_int(pick(4))
        ovol   = _to_int(pick(5))
        oi_cell = pick(6)
        oi_val = oi_marker = None
        if oi_cell:
            mm2 = re.match(r"^(\d+)\s*([+\-])?$", oi_cell.strip())
            if mm2:
                oi_val = int(mm2.group(1))
                oi_marker = mm2.group(2)
            else:
                oi_val = _to_int(oi_cell)
        oi_chg_cell = pick(7)
        oi_chg = None; oi_chg_flag = None
        if oi_chg_cell:
            v = oi_chg_cell.strip()
            if v == "UNCH": oi_chg = 0; oi_chg_flag = "UNCH"
            elif v == "NEW": oi_chg = None; oi_chg_flag = "NEW"
            else: oi_chg = _to_int(v)

        row = FuturesRow(
            bulletin_date=meta.get("bulletin_date") or "",
            bulletin_status=meta.get("bulletin_status") or "",
            bulletin_number=meta.get("bulletin_number") or "",
            source_file=source_file,
            source_page_hint=cur_product_line,
            product_code=cur_product,
            product_name=cur_product_name or "",
            contract_month_code=month,
            contract_month_iso=_month_iso(month),
            contract_symbol=_contract_symbol(cur_product, month),
            session_open=row_open,
            globex_high=gh,
            globex_low=gl,
            settlement=sett,
            settlement_change=d_sett,
            settlement_change_flag=d_flag,
            globex_volume=gvol,
            open_outcry_volume=ovol,
            open_interest=oi_val,
            oi_direction_marker=oi_marker,
            oi_change=oi_chg,
            oi_change_flag=oi_chg_flag,
            raw_row=l,
            raw_row_hash=hashlib.md5(l.encode("utf-8","replace")).hexdigest()[:16],
        )
        rows.append(row)
    return rows


if __name__ == "__main__":
    import sys, json
    from cme_bulletin_detector import detect
    with open(sys.argv[1], "rb") as f:
        text = f.read().decode("cp1252","replace")
    meta = detect(text)
    print("META:", json.dumps({k:v for k,v in meta.items() if k!="detector_signals"}))
    rows = parse_futures(text, meta, sys.argv[1].split("/")[-1])
    print(f"n_rows={len(rows)}")
    for r in rows[:10]:
        print(f"  {r.product_code:<4} {r.contract_month_code:<6}  sett={r.settlement}  dSett={r.settlement_change}({r.settlement_change_flag})  gvol={r.globex_volume}  OI={r.open_interest} ({r.oi_direction_marker})  dOI={r.oi_change}({r.oi_change_flag})  sym={r.contract_symbol}")
