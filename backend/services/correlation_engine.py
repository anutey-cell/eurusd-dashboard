"""
Intermarket Correlation Engine for XAU/USD
===========================================

Gold's price doesn't move in isolation. The historically robust relationships:

  DXY  ↔ XAU/USD  → typically NEGATIVE   (-0.7 to -0.4 long-run)
  US10Y ↔ XAU/USD  → typically NEGATIVE   (real yields = opportunity cost)
  WTI  ↔ XAU/USD  → typically POSITIVE   (inflation hedge corollary)
  VIX  ↔ XAU/USD  → typically POSITIVE   (safe-haven flows during stress)
  XAG  ↔ XAU/USD  → typically POSITIVE   (precious-metals complex)
  SPX  ↔ XAU/USD  → typically NEGATIVE   (risk-on, risk-off rotations)

When the live correlation BREAKS DOWN, the market is signalling a regime shift.
Example: gold rising WHILE DXY is rising = "fear-driven gold bid" — both are
safe havens and demand can lift both at once.

This module:
  1. Pulls H1 candles for XAUUSD and each intermarket via TradingView
  2. Computes Pearson correlation of log-returns over 20/60/100-bar windows
  3. Compares each live correlation to the historical norm
  4. Flags deviations as "regime alerts" the strategist must factor in
  5. Surfaces a single composite "intermarket alignment score"
"""
from __future__ import annotations

import logging
import math
from datetime import datetime, timezone
from typing import Optional

log = logging.getLogger(__name__)

# Expected long-run correlations (rough rolling-1-year normals).
# Used to detect "regime shifts" — when live correlation deviates by > 0.3
# from the expected sign, we flag it as a watch item.
EXPECTED_CORR: dict[str, float] = {
    "dxy":   -0.55,    # USD up → gold down
    "us10y": -0.40,    # yields up → gold down
    "wti":   +0.25,    # oil up → gold up (inflation hedge)
    "vix":   +0.20,    # fear up → gold up (safe haven)
    "xagusd":+0.75,    # silver and gold move together
    "spx":   -0.15,    # risk on/off — weak relationship
}

PAIR_LABELS: dict[str, str] = {
    "dxy":   "DXY (US Dollar Index)",
    "us10y": "US 10Y Yield",
    "wti":   "WTI Crude Oil",
    "vix":   "VIX (volatility)",
    "xagusd":"Silver",
    "spx":   "S&P 500",
}


def _log_returns(closes: list[float]) -> list[float]:
    """Return log returns: ln(c[i] / c[i-1])."""
    out = []
    for i in range(1, len(closes)):
        if closes[i - 1] > 0 and closes[i] > 0:
            out.append(math.log(closes[i] / closes[i - 1]))
    return out


def _pearson(a: list[float], b: list[float]) -> float:
    """Standard Pearson correlation. Returns 0.0 on degenerate input."""
    n = min(len(a), len(b))
    if n < 5:
        return 0.0
    a, b = a[-n:], b[-n:]
    ma = sum(a) / n
    mb = sum(b) / n
    num = sum((a[i] - ma) * (b[i] - mb) for i in range(n))
    sa  = math.sqrt(sum((a[i] - ma) ** 2 for i in range(n)))
    sb  = math.sqrt(sum((b[i] - mb) ** 2 for i in range(n)))
    if sa == 0 or sb == 0:
        return 0.0
    return round(num / (sa * sb), 3)


def _align_closes_by_time(a_candles: list, b_candles: list) -> tuple[list[float], list[float]]:
    """
    Inner-join two candle series by timestamp. Returns aligned close arrays
    so correlations are computed only across timestamps both series cover.
    """
    def _key(c):
        t = c.time if hasattr(c, "time") else c.get("time")
        if isinstance(t, str):
            try:
                t = datetime.fromisoformat(t.replace("Z", "+00:00"))
            except Exception:
                return None
        if hasattr(t, "isoformat"):
            return t.replace(microsecond=0).isoformat()
        return str(t)
    def _close(c):
        return float(c.close if hasattr(c, "close") else c.get("close", 0))
    map_b = {_key(c): _close(c) for c in b_candles}
    aa, bb = [], []
    for c in a_candles:
        k = _key(c)
        if k in map_b:
            aa.append(_close(c))
            bb.append(map_b[k])
    return aa, bb


def compute_intermarket_correlations(
    *, timeframe: str = "H1", n_bars: int = 200,
) -> dict:
    """
    Pull XAUUSD + intermarkets and return live correlations at 20/60/100-bar windows.

    Returns:
      {
        timeframe, n_bars, generated_at, xauusd_count,
        pairs: [
          { code, label, expected, corr_20, corr_60, corr_100,
            current_corr, alignment, deviation, status }
        ],
        intermarket_alignment_score: 0-100   (how well the current regime
                                              matches historical norms)
      }
    """
    from services.tradingview_provider import get_tv_candles

    # Pull XAUUSD baseline
    xau_bars = get_tv_candles("xauusd", timeframe=timeframe, limit=n_bars)
    if not xau_bars:
        return {"error": "Could not fetch XAUUSD baseline", "pairs": []}

    pair_rows: list[dict] = []
    aligned_signs = 0
    total_with_data = 0

    for code, label in PAIR_LABELS.items():
        bars = get_tv_candles(code, timeframe=timeframe, limit=n_bars)
        if not bars:
            pair_rows.append({
                "code": code, "label": label,
                "expected":      EXPECTED_CORR.get(code),
                "corr_20":       None, "corr_60": None, "corr_100": None,
                "current_corr":  None,
                "status":        "no_data",
            })
            continue

        # Align by timestamp (intermarkets trade slightly different hours)
        xau_closes, other_closes = _align_closes_by_time(xau_bars, bars)
        if len(xau_closes) < 25:
            pair_rows.append({
                "code": code, "label": label,
                "expected":      EXPECTED_CORR.get(code),
                "corr_20":       None, "corr_60": None, "corr_100": None,
                "current_corr":  None,
                "status":        "insufficient_overlap",
                "aligned_bars":  len(xau_closes),
            })
            continue

        xau_r   = _log_returns(xau_closes)
        other_r = _log_returns(other_closes)

        c20  = _pearson(xau_r[-20:],  other_r[-20:])
        c60  = _pearson(xau_r[-60:],  other_r[-60:])
        c100 = _pearson(xau_r[-100:], other_r[-100:])

        expected = EXPECTED_CORR.get(code, 0)
        current  = c60  # use 60-bar as the "current" snapshot
        # Alignment: do signs match expected? (negative*negative = aligned)
        aligned  = (expected * current) >= 0 if (expected != 0 and current != 0) else False
        deviation = round(current - expected, 3)
        status = (
            "aligned" if aligned and abs(deviation) < 0.30 else
            "weakened" if aligned else
            "regime_shift"
        )

        total_with_data += 1
        if aligned: aligned_signs += 1

        pair_rows.append({
            "code":           code,
            "label":          label,
            "expected":       expected,
            "corr_20":        c20,
            "corr_60":        c60,
            "corr_100":       c100,
            "current_corr":   current,
            "deviation":      deviation,
            "aligned":        aligned,
            "status":         status,
            "aligned_bars":   len(xau_closes),
        })

    alignment_score = (
        round(aligned_signs / total_with_data * 100, 1) if total_with_data > 0 else 0.0
    )

    return {
        "timeframe":   timeframe,
        "n_bars":      n_bars,
        "xauusd_bars": len(xau_bars),
        "generated_at":datetime.now(timezone.utc).isoformat(),
        "pairs":       pair_rows,
        "intermarket_alignment_score": alignment_score,
        "interpretation": _interpret(pair_rows, alignment_score),
    }


def _interpret(pair_rows: list[dict], alignment_score: float) -> str:
    """Human-readable summary of the intermarket regime."""
    shifts = [p for p in pair_rows if p.get("status") == "regime_shift"]
    if alignment_score >= 80:
        return (
            f"NORMAL REGIME — {alignment_score:.0f}% of intermarket correlations "
            f"align with historical norms. Trade decisions can rely on standard "
            f"DXY/yields/oil logic."
        )
    elif alignment_score >= 60:
        return (
            f"PARTIAL REGIME SHIFT — {alignment_score:.0f}% aligned. Some "
            f"correlations have weakened or broken. Watch: "
            f"{', '.join(p['code'] for p in shifts) or 'none flagged'}"
        )
    else:
        return (
            f"MAJOR REGIME SHIFT — only {alignment_score:.0f}% of correlations "
            f"hold. Markets are not respecting normal relationships. Reduce "
            f"trade size and tighten risk management. Broken: "
            f"{', '.join(p['code'] for p in shifts) or 'none flagged'}"
        )
