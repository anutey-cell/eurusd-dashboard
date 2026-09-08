"""CME bulletin type detector.

Given the plain-text (pdftotext -layout) content of a CME daily bulletin
section, identify which section it is. Never hard-code section numbers —
detect from document header, product content, and metadata.

Recognized types:
  OPTIONS_METALS    - contains "METAL OPTIONS PRODUCTS" or "GOLD OPTIONS"
  FUTURES_METALS    - contains "METAL FUTURES PRODUCTS" or "GOLD FUTURES"
  UNKNOWN

Returned metadata:
  bulletin_type
  section_number      (parsed from PG## header; may be None)
  bulletin_date       (YYYY-MM-DD)
  bulletin_number     (e.g. "171")
  bulletin_status     (PRELIMINARY | FINAL)
  detector_confidence HIGH | MEDIUM | LOW
  detector_signals    dict of matched signals for auditability
"""
from __future__ import annotations
import re
from datetime import datetime

_RX_PG = re.compile(r"\bPG(\d{1,3})\b")
_RX_BULLETIN = re.compile(r"BULLETIN\s*#\s*(\d+)")
_RX_DATE = re.compile(r"([A-Z][a-z]{2}, [A-Z][a-z]{2}\s+\d{1,2},\s+\d{4})")
_RX_DATE_LOOSE = re.compile(r"([A-Z][a-z]{2},\s+[A-Z][a-z]{2}\s+\d{1,2},\s+\d{4})")

_OPTIONS_SIGNALS = [
    ("header_METAL_OPTIONS_PRODUCTS", r"\bMETAL\s+OPTION[S]?\s+PRODUCTS\b"),
    ("comex_gold_options",             r"\bCOMEX\s+GOLD\s+OPTIONS\b"),
    ("gold_weekly_options",            r"\bGOLD\s+WEEKLY\b.*\bOPTION"),
    ("micro_gold_options",             r"\bMICRO\s+GOLD\s+OPTIONS\b"),
    ("delta_col",                      r"\bDELTA\b"),
    ("call_put_line",                  r"\b(OG|OG[1-4]|OMG|WMG|MMG|FMG)\s+(CALL|PUT|OPT|MON|TUE|WED|THU|FRI)\b"),
]
_FUTURES_SIGNALS = [
    ("header_METAL_FUTURES_PRODUCTS",  r"\bMETAL\s+FUTURES\s+PRODUCTS\b"),
    ("comex_gold_futures",             r"\bCOMEX\s+GOLD\s+FUTURES\b"),
    ("micro_gold_futures",             r"\bMICRO\s+GOLD\s+FUTURES\b"),
    ("gc_fut_marker",                  r"\bGC\s+FUT\b"),
    ("mgc_fut_marker",                 r"\bMGC\s+FUT\b"),
]

def detect(text: str) -> dict:
    header = text[:6000]      # first ~6k chars are enough
    signals = {}
    # Options signals
    opt_hits = 0
    for name, pat in _OPTIONS_SIGNALS:
        if re.search(pat, text, re.IGNORECASE):
            signals[name] = True
            opt_hits += 1
    # Futures signals
    fut_hits = 0
    for name, pat in _FUTURES_SIGNALS:
        if re.search(pat, text, re.IGNORECASE):
            signals[name] = True
            fut_hits += 1

    # Bias: futures signals if the header has "FUTURES", options signals if "OPTIONS"
    header_is_options = re.search(r"METAL\s+OPTION", header, re.IGNORECASE) is not None
    header_is_futures = re.search(r"METAL\s+FUTURES", header, re.IGNORECASE) is not None

    if header_is_options and header_is_futures:
        bulletin_type = "UNKNOWN"
        conf = "LOW"
    elif header_is_options or (opt_hits >= 3 and opt_hits > fut_hits):
        bulletin_type = "OPTIONS_METALS"
        conf = "HIGH" if header_is_options else "MEDIUM"
    elif header_is_futures or (fut_hits >= 2 and fut_hits > opt_hits):
        bulletin_type = "FUTURES_METALS"
        conf = "HIGH" if header_is_futures else "MEDIUM"
    else:
        bulletin_type = "UNKNOWN"
        conf = "LOW"

    # Section number (informational only — never hard-coded downstream)
    section_number = None
    m = _RX_PG.search(header)
    if m: section_number = m.group(1)

    # Bulletin number
    bnum = None
    m = _RX_BULLETIN.search(header)
    if m: bnum = m.group(1)

    # Status (PRELIMINARY | FINAL)
    status = None
    for keyword in ("PRELIMINARY", "FINAL"):
        if re.search(rf"\b{keyword}\b", header):
            status = keyword
            break

    # Date — try strict then loose
    bdate = None
    for l in header.splitlines()[:40]:
        m = _RX_DATE.search(l) or _RX_DATE_LOOSE.search(l)
        if m:
            raw = re.sub(r"\s+", " ", m.group(1)).strip()
            for fmt in ("%a, %b %d, %Y",):
                try:
                    bdate = datetime.strptime(raw, fmt).strftime("%Y-%m-%d")
                    break
                except Exception:
                    continue
            if bdate: break

    return {
        "bulletin_type":       bulletin_type,
        "section_number":      section_number,
        "bulletin_date":       bdate,
        "bulletin_number":     bnum,
        "bulletin_status":     status,
        "detector_confidence": conf,
        "detector_signals":    signals,
    }

if __name__ == "__main__":
    import sys
    with open(sys.argv[1], "rb") as f:
        t = f.read().decode("cp1252", errors="replace")
    import json
    print(json.dumps(detect(t), indent=2))
