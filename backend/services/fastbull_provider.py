"""
FastBull retail-positioning fetcher.

Scrapes https://www.fastbull.com/list-position for retail long/short
positioning + notable order/stop clusters. Fails open (returns
available=False) on ANY error — timeout, HTTP non-200, parse failure,
markup change. The confluence layer treats "unavailable" the same as
"neutral" — it never blocks trades.

This module knows one API: `fetch_fastbull_positioning()` → dict.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import Any

import requests

log = logging.getLogger(__name__)


_FASTBULL_URL = "https://www.fastbull.com/list-position"
_UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/122.0 Safari/537.36")


def _empty(reason: str = "no data") -> dict:
    return {
        "available":                    False,
        "long_short_bias":              "unknown",
        "long_pct":                     None,
        "short_pct":                    None,
        "notable_position_zones":       [],
        "notable_pending_order_zones":  [],
        "trapped_buyer_zones":          [],
        "trapped_seller_zones":         [],
        "liquidity_above":              [],
        "liquidity_below":              [],
        "interpretation":               f"unavailable — {reason}",
        "fetched_at":                   datetime.now(timezone.utc).isoformat(),
        "raw_symbol":                   "XAUUSD",
    }


def _classify_bias(long_pct: float | None) -> str:
    if long_pct is None:
        return "unknown"
    if long_pct >= 65: return "long_heavy"     # contrarian: bearish tell
    if long_pct <= 35: return "short_heavy"    # contrarian: bullish tell
    return "balanced"


def _parse_positioning_html(html: str) -> dict:
    """
    Best-effort extractor for the XAU/USD row on the list-position page.
    Structure of that page is not officially documented, so we look for
    fragments that consistently identify a Gold row + long/short percents.
    """
    text = re.sub(r"\s+", " ", html)

    # Find the block that mentions XAU/USD / GOLD / XAUUSD
    match = re.search(
        r"(XAU\s*/?\s*USD|XAUUSD|Gold)[^<]{0,200}?"
        r"(\d{1,3}(?:\.\d+)?)\s*%?[^<]{0,80}?"
        r"(\d{1,3}(?:\.\d+)?)\s*%?",
        text, re.IGNORECASE,
    )
    if not match:
        return _empty("XAU/USD row not found in HTML")

    try:
        pct_a = float(match.group(2))
        pct_b = float(match.group(3))
    except ValueError:
        return _empty("pct parse fail")

    # We have to guess which is long vs short. Heuristic: sum ~= 100.
    if not (85 <= pct_a + pct_b <= 115):
        return _empty(f"pcts don't sum near 100 ({pct_a}+{pct_b})")

    long_pct  = pct_a
    short_pct = pct_b
    bias      = _classify_bias(long_pct)

    interp_map = {
        "long_heavy":  ("Crowd is long-heavy at "
                         f"{long_pct:.0f}% — contrarian bearish tell"),
        "short_heavy": ("Crowd is short-heavy at "
                         f"{short_pct:.0f}% — contrarian bullish tell"),
        "balanced":    (f"Crowd is balanced ({long_pct:.0f}%L/"
                         f"{short_pct:.0f}%S) — no positioning edge"),
        "unknown":     "Positioning could not be classified",
    }

    return {
        "available":                    True,
        "long_short_bias":              bias,
        "long_pct":                     long_pct,
        "short_pct":                    short_pct,
        "notable_position_zones":       [],   # not derivable from this page
        "notable_pending_order_zones":  [],
        "trapped_buyer_zones":          [],
        "trapped_seller_zones":         [],
        "liquidity_above":              [],
        "liquidity_below":              [],
        "interpretation":               interp_map[bias],
        "fetched_at":                   datetime.now(timezone.utc).isoformat(),
        "raw_symbol":                   "XAUUSD",
    }


def fetch_fastbull_positioning(timeout_s: float = 3.0) -> dict:
    """Public entry. Never raises."""
    try:
        r = requests.get(
            _FASTBULL_URL,
            headers={"User-Agent": _UA, "Accept": "text/html"},
            timeout=timeout_s,
        )
        if r.status_code != 200:
            log.debug("[fastbull] HTTP %s", r.status_code)
            return _empty(f"HTTP {r.status_code}")
        if not r.text or len(r.text) < 500:
            return _empty("response too short")
        return _parse_positioning_html(r.text)
    except requests.RequestException as exc:
        log.debug("[fastbull] network: %s", exc)
        return _empty(f"network: {exc}")
    except Exception as exc:
        log.debug("[fastbull] unexpected: %s", exc)
        return _empty(f"parse: {exc}")


__all__ = ["fetch_fastbull_positioning"]
