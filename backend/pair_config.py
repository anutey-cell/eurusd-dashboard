"""
Pair-specific configuration for the signal engine, backtester, and providers.

pip          – smallest price unit (0.0001 for most FX, 0.01 for JPY, 0.1 for XAU)
target_pips  – fixed profit-target in pips
sl_buffer    – structural SL buffer in pips added beyond the swept level
fvg_min_pips – minimum FVG size considered valid (in pair pips)
min_rr       – minimum risk-reward ratio required to fire a signal
"""
from __future__ import annotations

PAIRS: dict[str, dict] = {
    "EUR/USD": {
        "pip": 0.0001,
        "target_pips": 40,
        "sl_buffer_pips": 3,
        "fvg_min_pips": 3,
        "min_rr": 2.5,
        # Provider symbol identifiers
        "td_symbol":         "EUR/USD",
        "av_from":           "EUR",  "av_to": "USD",
        "oanda_instrument":  "EUR_USD",
        "polygon_ticker":    "C:EURUSD",
        "fmp_symbol":        "EURUSD",
        # COT report market name (CFTC disaggregated futures)
        "cot_name":          "EURO FX",
    },
    "GBP/USD": {
        "pip": 0.0001,
        "target_pips": 40,
        "sl_buffer_pips": 3,
        "fvg_min_pips": 3,
        "min_rr": 2.5,
        "td_symbol":         "GBP/USD",
        "av_from":           "GBP",  "av_to": "USD",
        "oanda_instrument":  "GBP_USD",
        "polygon_ticker":    "C:GBPUSD",
        "fmp_symbol":        "GBPUSD",
        "cot_name":          "BRITISH POUND STERLING",
    },
    "USD/JPY": {
        "pip": 0.01,
        "target_pips": 40,
        "sl_buffer_pips": 5,
        "fvg_min_pips": 3,
        "min_rr": 2.5,
        "td_symbol":         "USD/JPY",
        "av_from":           "USD",  "av_to": "JPY",
        "oanda_instrument":  "USD_JPY",
        "polygon_ticker":    "C:USDJPY",
        "fmp_symbol":        "USDJPY",
        "cot_name":          "JAPANESE YEN",
    },
    "XAU/USD": {
        "pip": 0.1,
        "target_pips": 150,
        "sl_buffer_pips": 10,
        "fvg_min_pips": 5,
        "min_rr": 2.5,
        "td_symbol":         "XAU/USD",
        "av_from":           "XAU",  "av_to": "USD",
        "oanda_instrument":  "XAU_USD",
        "polygon_ticker":    "C:XAUUSD",
        "fmp_symbol":        "XAUUSD",
        "cot_name":          "GOLD",
    },
}

DEFAULT_PAIR = "EUR/USD"
SUPPORTED_PAIRS = list(PAIRS.keys())


def get_pair(pair: str) -> dict:
    """Return config for a pair; raises ValueError for unsupported pairs."""
    cfg = PAIRS.get(pair.upper())
    if cfg is None:
        raise ValueError(
            f"Unsupported pair '{pair}'. Supported: {SUPPORTED_PAIRS}"
        )
    return cfg
