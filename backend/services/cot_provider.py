"""
COT (Commitments of Traders) data provider.

Routing:
  DATA_MODE=demo  → static mock COT data, no network required
  DATA_MODE=live  → fetches CFTC weekly legacy report (free, no API key)

CFTC COT report:
  Source  : https://www.cftc.gov/MarketReports/CommitmentsofTraders/index.htm
  Format  : Comma-delimited legacy format (fut_fin_txt_YYYY.zip)
  Weekly  : https://www.cftc.gov/dea/newcot/FinFutWk.txt  (currencies)
            https://www.cftc.gov/dea/newcot/c_year.txt     (commodities incl. gold)

Bias convention:
  Net non-commercial position = NonComm_Long - NonComm_Short
  Positive  → speculative funds are net LONG the base currency → Bullish
  Negative  → speculative funds are net SHORT the base currency → Bearish
  |net| < threshold → Neutral

For USD/JPY:
  The report shows JPY positioning vs USD.
  Net JPY long → JPY bullish → USD/JPY bearish.  We invert the bias.

pip dependency for live mode:
  pip install httpx
"""
from __future__ import annotations

import io
import csv
import logging
from datetime import datetime, timezone, date

logger = logging.getLogger(__name__)

# ── Market name → COT report lookup ──────────────────────────────────────────

_CFTC_NAMES: dict[str, str] = {
    "EUR/USD": "EURO FX - CHICAGO MERCANTILE EXCHANGE",
    "GBP/USD": "BRITISH POUND STERLING - CHICAGO MERCANTILE EXCHANGE",
    "USD/JPY": "JAPANESE YEN - CHICAGO MERCANTILE EXCHANGE",
    "XAU/USD": "GOLD - COMMODITY EXCHANGE INC.",
}

# Pairs whose COT bias should be inverted (base currency is quoted vs USD)
_INVERT_PAIRS: set[str] = {"USD/JPY"}

# Net position threshold for neutral zone (contracts)
_NEUTRAL_THRESHOLD = 10_000

# Report URLs
_FIN_FUT_URL  = "https://www.cftc.gov/dea/newcot/FinFutWk.txt"
_COMM_FUT_URL = "https://www.cftc.gov/dea/newcot/c_year.txt"


# ── Demo data ─────────────────────────────────────────────────────────────────

_MOCK_COT: dict[str, dict] = {
    "EUR/USD": {
        "pair":        "EUR/USD",
        "asOf":        "2026-05-06",
        "netPosition": 42_350,
        "longContracts": 198_200,
        "shortContracts": 155_850,
        "bias":        "Bullish",
        "change":      +3_120,
        "source":      "demo",
    },
    "GBP/USD": {
        "pair":        "GBP/USD",
        "asOf":        "2026-05-06",
        "netPosition": -8_900,
        "longContracts": 54_100,
        "shortContracts": 63_000,
        "bias":        "Neutral",
        "change":      -1_450,
        "source":      "demo",
    },
    "USD/JPY": {
        "pair":        "USD/JPY",
        "asOf":        "2026-05-06",
        "netPosition": -62_000,   # JPY net short → USD/JPY bullish
        "longContracts": 24_500,
        "shortContracts": 86_500,
        "bias":        "Bullish",  # inverted
        "change":      -4_800,
        "source":      "demo",
    },
    "XAU/USD": {
        "pair":        "XAU/USD",
        "asOf":        "2026-05-06",
        "netPosition": 185_000,
        "longContracts": 298_000,
        "shortContracts": 113_000,
        "bias":        "Bullish",
        "change":      +9_200,
        "source":      "demo",
    },
}


# ── CFTC CSV fetch + parse ────────────────────────────────────────────────────

def _fetch_text(url: str) -> str:
    try:
        import httpx
    except ImportError:
        raise RuntimeError("httpx is not installed. Run `pip install httpx` then restart.")
    resp = httpx.get(url, timeout=20.0, follow_redirects=True)
    resp.raise_for_status()
    return resp.text


def _parse_cftc_csv(text: str, market_name: str) -> dict | None:
    """
    Parse the CFTC legacy-format CSV and extract the most recent row
    matching `market_name` (case-insensitive prefix match).
    """
    reader = csv.DictReader(io.StringIO(text))
    best_row: dict | None = None
    best_date: date | None = None

    target = market_name.upper()

    for row in reader:
        name = row.get("Market_and_Exchange_Names", "").upper()
        if not name.startswith(target[:20]):  # prefix match for safety
            continue

        # Date field: "YYMMDD" in legacy format
        date_str = row.get("As_of_Date_In_Form_YYMMDD", "")
        try:
            row_date = datetime.strptime(date_str.strip(), "%y%m%d").date()
        except ValueError:
            continue

        if best_date is None or row_date > best_date:
            best_date = row_date
            best_row  = row

    if best_row is None:
        return None

    try:
        long_nc  = int(best_row.get("NonComm_Positions_Long_All",  0).replace(",", ""))
        short_nc = int(best_row.get("NonComm_Positions_Short_All", 0).replace(",", ""))
        net      = long_nc - short_nc
        # Change from prior week
        long_prev  = int(best_row.get("Change_in_NonComm_Long_All",  0).replace(",", ""))
        short_prev = int(best_row.get("Change_in_NonComm_Short_All", 0).replace(",", ""))
        change = long_prev - short_prev
    except (ValueError, AttributeError):
        return None

    return {
        "longContracts":  long_nc,
        "shortContracts": short_nc,
        "netPosition":    net,
        "change":         change,
        "asOf":           best_date.isoformat(),
    }


# ── Public API ────────────────────────────────────────────────────────────────

def get_cot_data(pair: str) -> dict:
    """
    Return COT positioning data for the given pair.

    demo mode → mock data, no network.
    live mode → downloads CFTC weekly report and parses it.
    """
    from config import settings

    pair = pair.upper()
    mock = _MOCK_COT.get(pair)

    if settings.data_mode == "demo" or pair not in _CFTC_NAMES:
        return mock or {
            "pair":        pair,
            "bias":        "Neutral",
            "netPosition": 0,
            "longContracts": 0,
            "shortContracts": 0,
            "change":      0,
            "asOf":        datetime.now(timezone.utc).date().isoformat(),
            "source":      "demo",
        }

    market_name = _CFTC_NAMES[pair]
    # Gold is in commodity futures; others in financial futures
    url = _COMM_FUT_URL if pair == "XAU/USD" else _FIN_FUT_URL

    try:
        text   = _fetch_text(url)
        parsed = _parse_cftc_csv(text, market_name)
    except Exception as exc:
        logger.warning("COT fetch failed for %s: %s — falling back to mock", pair, exc)
        return {**(mock or {}), "source": "demo_fallback", "error": str(exc)}

    if parsed is None:
        logger.warning("COT parse returned no match for '%s'", market_name)
        return {**(mock or {}), "source": "demo_fallback"}

    net    = parsed["netPosition"]
    invert = pair in _INVERT_PAIRS

    if invert:
        bias = "Bearish" if net > _NEUTRAL_THRESHOLD else ("Bullish" if net < -_NEUTRAL_THRESHOLD else "Neutral")
    else:
        bias = "Bullish" if net > _NEUTRAL_THRESHOLD else ("Bearish" if net < -_NEUTRAL_THRESHOLD else "Neutral")

    return {
        "pair":           pair,
        "asOf":           parsed["asOf"],
        "netPosition":    net,
        "longContracts":  parsed["longContracts"],
        "shortContracts": parsed["shortContracts"],
        "change":         parsed["change"],
        "bias":           bias,
        "source":         "cftc",
    }


def get_cot_for_all_pairs() -> list[dict]:
    """Return COT data for all supported pairs."""
    from pair_config import SUPPORTED_PAIRS
    return [get_cot_data(p) for p in SUPPORTED_PAIRS]
