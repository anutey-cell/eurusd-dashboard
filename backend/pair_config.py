"""
Centralized pair configuration for EUR/USD and XAU/USD.

Pairs are keyed by short code ("eurusd", "xauusd").
All backend components use get_pair_config(code) to retrieve settings.
"""
from __future__ import annotations

SUPPORTED_PAIRS: dict[str, dict] = {
    "eurusd": {
        "display":          "EUR/USD",
        "symbol":           "EUR/USD",
        "pip_size":         0.0001,
        "target_pips":      40,
        "sl_buffer_pips":   3,
        "fvg_min_pips":     3,
        "min_rr":           2.5,
        "min_score":        80,
        "price_decimals":   5,
        "news_currencies":  ["EUR", "USD"],
        # TradingView chart symbol
        "tv_symbol":        "FX:EURUSD",
        # Provider identifiers
        "td_symbol":        "EUR/USD",
        "av_from":          "EUR", "av_to": "USD",
        "oanda_instrument": "EUR_USD",
        "polygon_ticker":   "C:EURUSD",
        "fmp_symbol":       "EURUSD",
        "cot_name":         "EURO FX",
    },
    "xauusd": {
        "display":          "XAU/USD",
        "symbol":           "XAU/USD",
        "pip_size":         1.0,
        "target_pips":      50,
        "sl_buffer_pips":   5,
        "fvg_min_pips":     5,
        "min_rr":           2.5,
        "min_score":        80,
        "price_decimals":   2,
        "news_currencies":  ["USD"],
        # TradingView chart symbol (OANDA:XAU_USD preferred; falls back to TVC:GOLD)
        "tv_symbol":        "OANDA:XAU_USD",
        "tv_symbol_alt":    "TVC:GOLD",
        # Provider identifiers
        "td_symbol":        "XAU/USD",
        "av_from":          "XAU", "av_to": "USD",
        "oanda_instrument": "XAU_USD",
        "polygon_ticker":   "C:XAUUSD",
        "fmp_symbol":       "XAUUSD",
        "cot_name":         "GOLD",
    },
}

# Legacy display-name → code map for backward compat
_DISPLAY_TO_CODE: dict[str, str] = {
    "EUR/USD": "eurusd",
    "XAU/USD": "xauusd",
    "GBP/USD": "eurusd",   # fallback to eurusd for unsupported pairs
    "USD/JPY": "eurusd",
}

DEFAULT_PAIR_CODE = "eurusd"
DEFAULT_PAIR      = "EUR/USD"   # legacy compat alias


def get_pair_config(pair: str) -> dict:
    """
    Return config for a pair by short code or display name.
    Raises ValueError for unknown pairs.
    """
    code = pair.lower().replace("/", "").replace("_", "")
    if code in SUPPORTED_PAIRS:
        return {**SUPPORTED_PAIRS[code], "code": code}
    # Try display name lookup
    code2 = _DISPLAY_TO_CODE.get(pair.upper())
    if code2:
        return {**SUPPORTED_PAIRS[code2], "code": code2}
    raise ValueError(
        f"Unsupported pair '{pair}'. Supported codes: {list(SUPPORTED_PAIRS)}"
    )


def validate_pair(pair: str) -> str:
    """Return normalised pair code or raise ValueError."""
    cfg = get_pair_config(pair)
    return cfg["code"]


def get_supported_pairs() -> list[dict]:
    """Return list of supported pair summaries for the /pairs endpoint."""
    return [
        {
            "code":          code,
            "display":       cfg["display"],
            "symbol":        cfg["symbol"],
            "pip_size":      cfg["pip_size"],
            "target_pips":   cfg["target_pips"],
            "min_rr":        cfg["min_rr"],
            "price_decimals": cfg["price_decimals"],
            "news_currencies": cfg["news_currencies"],
            "tv_symbol":     cfg["tv_symbol"],
        }
        for code, cfg in SUPPORTED_PAIRS.items()
    ]


# Legacy compat — older code uses get_pair(display_name)
def get_pair(pair: str) -> dict:
    """Backward-compatible alias for get_pair_config."""
    return get_pair_config(pair)
