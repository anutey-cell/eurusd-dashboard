"""
Centralized pair configuration for EUR/USD and XAU/USD.

Pairs are keyed by short code ("eurusd", "xauusd").
All backend components use get_pair_config(code) to retrieve settings.

Operating modes (per-pair, overridable via environment variable):
  MONITOR_ONLY       — engine runs, no signals confirmed, Telegram disabled
  PAPER_OBSERVATION  — signals generated, can be logged, no execution
  DEMO_ELIGIBLE      — signals eligible for demo broker execution
  LIVE_READ_ONLY     — engine runs in live context, human-only confirmations
  DISABLED           — engine disabled entirely for this pair

Readiness gate:
  A pair must score >= 85/100 on the readiness checklist before
  its mode can be promoted above MONITOR_ONLY.
"""
from __future__ import annotations

import os

# ── Operating mode definitions ────────────────────────────────────────────────

VALID_MODES = {"MONITOR_ONLY", "PAPER_OBSERVATION", "DEMO_ELIGIBLE", "LIVE_READ_ONLY", "DISABLED"}

# Per-pair mode — read once at startup from environment; default MONITOR_ONLY
_PAIR_MODES: dict[str, str] = {
    "eurusd": os.getenv("EURUSD_MODE", "MONITOR_ONLY").upper(),
    "xauusd": os.getenv("XAUUSD_MODE", "MONITOR_ONLY").upper(),
}

# Validate mode values at import time — fallback to safe default
for _code, _mode in _PAIR_MODES.items():
    if _mode not in VALID_MODES:
        import logging as _log
        _log.getLogger(__name__).warning(
            "Unknown operating mode '%s' for %s — defaulting to MONITOR_ONLY", _mode, _code
        )
        _PAIR_MODES[_code] = "MONITOR_ONLY"


# ── Per-pair configuration ────────────────────────────────────────────────────

SUPPORTED_PAIRS: dict[str, dict] = {
    "eurusd": {
        # ── Identity ─────────────────────────────────────────────────────────
        "display":          "EUR/USD",
        "symbol":           "EUR/USD",

        # ── Signal model ─────────────────────────────────────────────────────
        "pip_size":         0.0001,
        "target_pips":      40,
        "sl_buffer_pips":   3,
        "fvg_min_pips":     3,
        "min_rr":           2.5,
        "min_score":        80,
        "price_decimals":   5,
        "strong_wick_pips": 3,      # minimum wick size to count as liquidity sweep

        # ── Spread / volatility filter ────────────────────────────────────────
        "max_spread":                2,      # pips — reject signal if spread > this
        "volatility_filter_enabled": True,
        "atr_min":                   8,      # pips — too quiet, skip
        "atr_max":                   80,     # pips — too volatile, skip

        # ── News sensitivity ──────────────────────────────────────────────────
        "news_currencies":      ["EUR", "USD"],
        "critical_news_events": [
            "NFP", "CPI", "FOMC", "ECB", "GDP",
            "Unemployment Rate", "Retail Sales",
        ],
        "news_block_minutes_before": 30,
        "news_block_minutes_after":  60,

        # ── Target / unit labels ──────────────────────────────────────────────
        "target_type":  "pip",
        "target_units": "pips",

        # ── Execution gates ───────────────────────────────────────────────────
        "allow_telegram_alerts":    os.getenv("EURUSD_TELEGRAM_ALERTS", "true").lower() in ("1", "true", "yes"),
        "allow_manual_confirmation": False,   # only True when mode >= PAPER_OBSERVATION
        "allow_demo_execution":      False,   # only True when mode == DEMO_ELIGIBLE

        # ── Provider identifiers ──────────────────────────────────────────────
        "tv_symbol":        "FX:EURUSD",
        "td_symbol":        "EUR/USD",
        "av_from":          "EUR", "av_to": "USD",
        "oanda_instrument": "EUR_USD",
        "polygon_ticker":   "C:EURUSD",
        "fmp_symbol":       "EURUSD",
        "cot_name":         "EURO FX",
    },
    "xauusd": {
        # ── Identity ─────────────────────────────────────────────────────────
        "display":          "XAU/USD",
        "symbol":           "XAU/USD",

        # ── Signal model ─────────────────────────────────────────────────────
        "pip_size":         1.0,            # 1 point = $0.01 on standard lot
        "target_pips":      50,             # points
        "sl_buffer_pips":   5,              # points
        "fvg_min_pips":     5,              # points
        "min_rr":           2.5,
        "min_score":        80,
        "price_decimals":   2,
        "strong_wick_pips": 8,              # XAU sweeps are larger — 8 points minimum

        # ── Spread / volatility filter ────────────────────────────────────────
        "max_spread":                5,      # points — gold has wider spreads
        "volatility_filter_enabled": True,
        "atr_min":                   15,     # points — gold needs minimum movement
        "atr_max":                   300,    # points — above this = news spike, skip

        # ── News sensitivity ──────────────────────────────────────────────────
        "news_currencies":      ["USD"],
        "critical_news_events": [
            "NFP", "CPI", "FOMC", "GDP",
            "Unemployment Rate", "Retail Sales",
            "PPI", "PCE",          # gold reacts strongly to inflation prints
            "Fed Chair Speaks", "FOMC Minutes",
        ],
        "news_block_minutes_before": 45,    # wider window for gold (more volatile)
        "news_block_minutes_after":  90,

        # ── Target / unit labels ──────────────────────────────────────────────
        "target_type":  "point",
        "target_units": "points",

        # ── Execution gates ───────────────────────────────────────────────────
        "allow_telegram_alerts":    os.getenv("XAUUSD_TELEGRAM_ALERTS", "true").lower() in ("1", "true", "yes"),
        "allow_manual_confirmation": False,
        "allow_demo_execution":      False,

        # ── Provider identifiers ──────────────────────────────────────────────
        "tv_symbol":        "OANDA:XAU_USD",
        "tv_symbol_alt":    "TVC:GOLD",
        "td_symbol":        "XAU/USD",
        "av_from":          "XAU", "av_to": "USD",
        "oanda_instrument": "XAU_USD",
        "polygon_ticker":   "C:XAUUSD",
        "fmp_symbol":       "XAUUSD",
        "cot_name":         "GOLD",
    },
}

# ── Display-name → code map ───────────────────────────────────────────────────
# Only map genuinely supported pairs. Do NOT add silent fallbacks for
# unsupported pairs — they will raise ValueError so callers handle them explicitly.
_DISPLAY_TO_CODE: dict[str, str] = {
    "EUR/USD": "eurusd",
    "XAU/USD": "xauusd",
}

DEFAULT_PAIR_CODE = "eurusd"
DEFAULT_PAIR      = "EUR/USD"   # legacy compat alias


# ── Public accessors ──────────────────────────────────────────────────────────

def get_pair_config(pair: str) -> dict:
    """
    Return config for a pair by short code or display name.
    Injects the current operating mode into the returned dict.
    Raises ValueError for unknown pairs.
    """
    code = pair.lower().replace("/", "").replace("_", "")
    if code in SUPPORTED_PAIRS:
        cfg = {**SUPPORTED_PAIRS[code], "code": code}
        _inject_mode(cfg, code)
        return cfg
    # Try display name lookup
    code2 = _DISPLAY_TO_CODE.get(pair)
    if code2 is None:
        # Last-chance: case-insensitive match
        code2 = _DISPLAY_TO_CODE.get(pair.upper())
    if code2:
        cfg = {**SUPPORTED_PAIRS[code2], "code": code2}
        _inject_mode(cfg, code2)
        return cfg
    raise ValueError(
        f"Unsupported pair '{pair}'. Supported codes: {list(SUPPORTED_PAIRS)}"
    )


def _inject_mode(cfg: dict, code: str) -> None:
    """Inject mode-derived fields into a config dict in-place."""
    mode = _PAIR_MODES.get(code, "MONITOR_ONLY")
    cfg["mode"] = mode
    # Derive execution gates from mode
    cfg["allow_manual_confirmation"] = mode in ("PAPER_OBSERVATION", "DEMO_ELIGIBLE", "LIVE_READ_ONLY")
    cfg["allow_demo_execution"]      = mode == "DEMO_ELIGIBLE"


def validate_pair(pair: str) -> str:
    """Return normalised pair code or raise ValueError."""
    cfg = get_pair_config(pair)
    return cfg["code"]


def get_pair_mode(pair_code: str) -> str:
    """Return the current operating mode for a pair code."""
    return _PAIR_MODES.get(pair_code.lower(), "MONITOR_ONLY")


def set_pair_mode(pair_code: str, mode: str) -> None:
    """
    Update operating mode at runtime (takes effect immediately, not persisted to disk).
    Raises ValueError if mode or pair code is unknown.
    """
    if pair_code.lower() not in SUPPORTED_PAIRS:
        raise ValueError(f"Unknown pair code '{pair_code}'")
    if mode.upper() not in VALID_MODES:
        raise ValueError(f"Invalid mode '{mode}'. Valid modes: {sorted(VALID_MODES)}")
    _PAIR_MODES[pair_code.lower()] = mode.upper()


def get_supported_pairs() -> list[dict]:
    """Return list of supported pair summaries for the /pairs endpoint."""
    result = []
    for code, cfg in SUPPORTED_PAIRS.items():
        mode = _PAIR_MODES.get(code, "MONITOR_ONLY")
        result.append({
            "code":                  code,
            "display":               cfg["display"],
            "symbol":                cfg["symbol"],
            "pip_size":              cfg["pip_size"],
            "target_pips":           cfg["target_pips"],
            "target_units":          cfg["target_units"],
            "min_rr":                cfg["min_rr"],
            "min_score":             cfg["min_score"],
            "price_decimals":        cfg["price_decimals"],
            "news_currencies":       cfg["news_currencies"],
            "tv_symbol":             cfg["tv_symbol"],
            "max_spread":            cfg["max_spread"],
            "atr_min":               cfg["atr_min"],
            "atr_max":               cfg["atr_max"],
            "mode":                  mode,
            "allow_telegram_alerts": cfg["allow_telegram_alerts"],
            "allow_manual_confirmation": mode in ("PAPER_OBSERVATION", "DEMO_ELIGIBLE", "LIVE_READ_ONLY"),
            "allow_demo_execution":      mode == "DEMO_ELIGIBLE",
        })
    return result


# Legacy compat — older code uses get_pair(display_name)
def get_pair(pair: str) -> dict:
    """Backward-compatible alias for get_pair_config."""
    return get_pair_config(pair)
