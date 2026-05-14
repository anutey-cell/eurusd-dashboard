"""
Centralized instrument configuration — XAU/USD only.

This dashboard supports a single instrument: XAU/USD (spot gold).
All backend components use get_pair_config("xauusd") to retrieve settings.

Operating modes:
  MONITOR_ONLY       — engine runs, signals generated, no confirmation allowed
  PAPER_OBSERVATION  — signals generated, can be logged, no execution
  DEMO_ELIGIBLE      — signals eligible for demo broker execution (readiness ≥ 85)
  LIVE_READ_ONLY     — engine runs in live context, human-only confirmations
  DISABLED           — engine disabled entirely

Readiness gate:
  Score ≥ 85/100 required before mode can be promoted above MONITOR_ONLY.
"""
from __future__ import annotations

import os

# ── Operating mode ────────────────────────────────────────────────────────────

VALID_MODES = {"MONITOR_ONLY", "PAPER_OBSERVATION", "DEMO_ELIGIBLE", "LIVE_READ_ONLY", "DISABLED"}

_XAUUSD_MODE = os.getenv("XAUUSD_MODE", "MONITOR_ONLY").upper()
if _XAUUSD_MODE not in VALID_MODES:
    import logging as _log
    _log.getLogger(__name__).warning(
        "Unknown operating mode '%s' for xauusd — defaulting to MONITOR_ONLY", _XAUUSD_MODE
    )
    _XAUUSD_MODE = "MONITOR_ONLY"

# ── Instrument configuration ──────────────────────────────────────────────────

DEFAULT_PAIR_CODE = "xauusd"
DEFAULT_PAIR      = "XAU/USD"

SUPPORTED_PAIRS: dict[str, dict] = {
    "xauusd": {
        # ── Identity ─────────────────────────────────────────────────────────
        "display":          "XAU/USD",
        "symbol":           "XAU/USD",

        # ── Signal model ─────────────────────────────────────────────────────
        "pip_size":         1.0,            # 1 point = $1 move per lot
        "target_pips":      50,             # 50 points target
        "target_label":     "points",       # display label — always "points" for gold
        "sl_buffer_pips":   5,              # 5 points SL buffer
        "fvg_min_pips":     5,              # minimum 5-point FVG
        "min_rr":           2.5,
        "min_score":        80,
        "price_decimals":   2,
        "strong_wick_pips": 8,              # minimum sweep wick in points for full 20-pt score

        # ── Spread / volatility filter ────────────────────────────────────────
        "max_spread":                5.0,   # points — reject signal if spread > 5
        "volatility_filter_enabled": True,
        "atr_min":                   15,    # points — too quiet, skip
        "atr_max":                   300,   # points — too volatile, skip

        # ── News sensitivity ──────────────────────────────────────────────────
        "news_currencies":      ["USD"],
        "critical_news_events": [
            "NFP",
            "US CPI", "CPI", "Core CPI",
            "PPI", "Core PPI",
            "PCE", "Core PCE",
            "FOMC", "FOMC Minutes",
            "Fed Chair Speaks", "Fed Chair Speech",
            "US GDP", "GDP",
            "ISM Manufacturing PMI", "ISM Services PMI",
            "Retail Sales",
            "Unemployment Claims", "Unemployment Rate",
            "US 10-Year Bond Yield",
        ],
        "news_block_minutes_before": 60,    # 60 min before critical events
        "news_block_minutes_after":  30,    # 30 min after critical events
        "minor_news_block_before":   30,    # 30 min before other high-impact events
        "minor_news_block_after":    15,    # 15 min after other high-impact events

        # ── Preferred sessions ────────────────────────────────────────────────
        "preferred_sessions": ["London", "New York", "Overlap"],

        # ── Execution gates ───────────────────────────────────────────────────
        "allow_telegram_alerts":     os.getenv("XAUUSD_TELEGRAM_ALERTS", "true").lower() in ("1", "true", "yes"),
        "allow_manual_confirmation": False,  # derived from mode below
        "allow_demo_execution":      False,  # derived from mode below

        # ── TradingView chart ─────────────────────────────────────────────────
        "tv_symbol":        "OANDA:XAUUSD",
        "tv_symbol_alt":    "TVC:GOLD",
        "tv_symbol_alt2":   "FOREXCOM:XAUUSD",

        # ── Provider identifiers ──────────────────────────────────────────────
        "td_symbol":        "XAU/USD",
        "av_from":          "XAU", "av_to": "USD",
        "oanda_instrument": "XAU_USD",
        "polygon_ticker":   "C:XAUUSD",
        "fmp_symbol":       "XAUUSD",
        "cot_name":         "GOLD",

        # ── MT5 symbol candidates ─────────────────────────────────────────────
        "mt5_candidates":   ["XAUUSD", "XAUUSDm", "GOLD", "XAUUSD.raw", "XAUUSD."],
    },
}

# ── Display-name → code map ───────────────────────────────────────────────────
# Only XAU/USD is supported. All other display names raise ValueError.
_DISPLAY_TO_CODE: dict[str, str] = {
    "XAU/USD": "xauusd",
    "XAUUSD":  "xauusd",
    "GOLD":    "xauusd",
}


# ── Public accessors ──────────────────────────────────────────────────────────

def get_pair_config(pair: str) -> dict:
    """
    Return XAU/USD config. Accepts code ("xauusd"), display name ("XAU/USD"),
    or aliases. Raises ValueError for anything else.
    """
    code = pair.lower().replace("/", "").replace("_", "")
    if code == "xauusd":
        cfg = {**SUPPORTED_PAIRS["xauusd"], "code": "xauusd"}
        _inject_mode(cfg)
        return cfg

    # Try display name lookup
    code2 = _DISPLAY_TO_CODE.get(pair) or _DISPLAY_TO_CODE.get(pair.upper())
    if code2:
        cfg = {**SUPPORTED_PAIRS[code2], "code": code2}
        _inject_mode(cfg)
        return cfg

    raise ValueError(
        f"Unsupported instrument '{pair}'. "
        "This dashboard supports XAU/USD only."
    )


def _inject_mode(cfg: dict) -> None:
    """Inject current operating mode and derived execution flags."""
    cfg["mode"] = _XAUUSD_MODE
    cfg["allow_manual_confirmation"] = _XAUUSD_MODE in (
        "PAPER_OBSERVATION", "DEMO_ELIGIBLE", "LIVE_READ_ONLY"
    )
    cfg["allow_demo_execution"] = _XAUUSD_MODE == "DEMO_ELIGIBLE"


def validate_pair(pair: str) -> str:
    """
    Return 'xauusd' if pair maps to XAU/USD.
    Raises ValueError for anything else — no silent fallback.
    """
    cfg = get_pair_config(pair)
    return cfg["code"]


def get_pair_mode() -> str:
    """Return the current operating mode for XAU/USD."""
    return _XAUUSD_MODE


def set_pair_mode(mode: str) -> None:
    """
    Update operating mode at runtime (not persisted to disk).
    Raises ValueError if mode is unknown.
    """
    global _XAUUSD_MODE
    if mode.upper() not in VALID_MODES:
        raise ValueError(f"Invalid mode '{mode}'. Valid modes: {sorted(VALID_MODES)}")
    _XAUUSD_MODE = mode.upper()


def get_supported_pairs() -> list[dict]:
    """Return the single-instrument list for the /instrument and /pairs endpoints."""
    mode = _XAUUSD_MODE
    cfg  = SUPPORTED_PAIRS["xauusd"]
    return [{
        "code":                      "xauusd",
        "display":                   cfg["display"],
        "symbol":                    cfg["symbol"],
        "pip_size":                  cfg["pip_size"],
        "target_pips":               cfg["target_pips"],
        "target_label":              cfg["target_label"],
        "min_rr":                    cfg["min_rr"],
        "min_score":                 cfg["min_score"],
        "price_decimals":            cfg["price_decimals"],
        "news_currencies":           cfg["news_currencies"],
        "tv_symbol":                 cfg["tv_symbol"],
        "max_spread":                cfg["max_spread"],
        "atr_min":                   cfg["atr_min"],
        "atr_max":                   cfg["atr_max"],
        "preferred_sessions":        cfg["preferred_sessions"],
        "mode":                      mode,
        "allow_telegram_alerts":     cfg["allow_telegram_alerts"],
        "allow_manual_confirmation": mode in ("PAPER_OBSERVATION", "DEMO_ELIGIBLE", "LIVE_READ_ONLY"),
        "allow_demo_execution":      mode == "DEMO_ELIGIBLE",
    }]


# Legacy compat — older code uses get_pair(display_name)
def get_pair(pair: str) -> dict:
    """Backward-compatible alias for get_pair_config."""
    return get_pair_config(pair)
