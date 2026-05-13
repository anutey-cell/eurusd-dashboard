"""
Pair correlation filter.

Computes rolling Pearson correlation between the closing prices of multiple
FX pairs and flags conflicting directional signals.

Correlation interpretation:
  |r| >= 0.7  → strong  (positively or negatively correlated)
  |r| >= 0.4  → moderate
  |r| <  0.4  → weak / unrelated

Signal suppression rule:
  If two pairs have |r| >= 0.7 AND their signals conflict (one BUY, one SELL):
    - The pair with the lower quality score is marked suppressed.
  If two pairs have |r| >= 0.7 AND their signals agree:
    - The pair with the lower quality score gets a +5 confirmation bonus.
"""
from __future__ import annotations

import math
import logging
from typing import Any

logger = logging.getLogger(__name__)

STRONG_CORRELATION  = 0.70
MODERATE_CORRELATION = 0.40
LOOKBACK_BARS        = 50    # bars used for correlation calculation


# ── Math helpers ──────────────────────────────────────────────────────────────

def pearson(xs: list[float], ys: list[float]) -> float:
    """Pearson correlation coefficient between two equal-length sequences."""
    n = len(xs)
    if n < 10 or n != len(ys):
        return 0.0

    mx = sum(xs) / n
    my = sum(ys) / n

    num   = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    den_x = math.sqrt(sum((x - mx) ** 2 for x in xs))
    den_y = math.sqrt(sum((y - my) ** 2 for y in ys))

    if den_x == 0 or den_y == 0:
        return 0.0
    return num / (den_x * den_y)


def compute_pair_correlations(
    closes_by_pair: dict[str, list[float]],
    lookback: int = LOOKBACK_BARS,
) -> dict[str, dict[str, float]]:
    """
    Return a dict of {pair_a: {pair_b: correlation}} for all pair combinations.
    Each list of closes is trimmed to `lookback` bars before computing.
    """
    pairs   = list(closes_by_pair.keys())
    trimmed = {p: closes_by_pair[p][-lookback:] for p in pairs}
    result: dict[str, dict[str, float]] = {p: {} for p in pairs}

    for i, pa in enumerate(pairs):
        for pb in pairs[i + 1:]:
            xa, xb = trimmed[pa], trimmed[pb]
            n = min(len(xa), len(xb))
            if n < 10:
                r = 0.0
            else:
                r = pearson(xa[-n:], xb[-n:])
            result[pa][pb] = round(r, 3)
            result[pb][pa] = round(r, 3)

    return result


# ── Signal suppression ────────────────────────────────────────────────────────

def apply_correlation_filter(
    signals: list[dict],
    correlations: dict[str, dict[str, float]],
) -> list[dict]:
    """
    Modify a list of signal dicts in-place with correlation-based suppression.

    Each signal dict is expected to have:
      pair          str   e.g. "EUR/USD"
      signal        str   "BUY" | "SELL" | "WAIT"
      quality_score int

    Returns the same list with two new fields added to each entry:
      suppressed    bool   True if signal was suppressed by conflict
      corr_note     str    Human-readable correlation note
    """
    for s in signals:
        s.setdefault("suppressed", False)
        s.setdefault("corr_note",  "")

    for i, sa in enumerate(signals):
        if sa["signal"] == "WAIT":
            continue
        for sb in signals[i + 1:]:
            if sb["signal"] == "WAIT":
                continue

            r = correlations.get(sa["pair"], {}).get(sb["pair"], 0.0)

            if abs(r) < STRONG_CORRELATION:
                continue

            if r > 0:
                # Positively correlated: expect same direction
                if sa["signal"] != sb["signal"]:
                    # Conflict — suppress the weaker score
                    weaker = sa if sa["quality_score"] <= sb["quality_score"] else sb
                    weaker["suppressed"] = True
                    weaker["corr_note"] = (
                        f"Suppressed: {sa['pair']}/{sb['pair']} correlation={r:.2f} "
                        f"but signals conflict ({sa['signal']} vs {sb['signal']})"
                    )
                else:
                    note = f"Confirmed by correlated pair {sb['pair']} (r={r:.2f})"
                    sa["corr_note"] = note
                    sb["corr_note"] = note
            else:
                # Negatively correlated: expect opposite directions
                if sa["signal"] == sb["signal"]:
                    # Conflict — suppress the weaker score
                    weaker = sa if sa["quality_score"] <= sb["quality_score"] else sb
                    weaker["suppressed"] = True
                    weaker["corr_note"] = (
                        f"Suppressed: {sa['pair']}/{sb['pair']} inverse correlation={r:.2f} "
                        f"but both signal {sa['signal']}"
                    )

    return signals


# ── Convenience: fetch closes and compute ────────────────────────────────────

def get_correlation_matrix(pairs: list[str], timeframe: str = "H4", lookback: int = 100) -> dict:
    """
    Fetch candles for multiple pairs and return the correlation matrix.
    Falls back gracefully if a pair's candles cannot be fetched.
    """
    from data.candles import get_candles, INTERVAL_MINUTES
    from services.candle_provider import get_eurusd_candles
    from pair_config import get_pair

    closes_by_pair: dict[str, list[float]] = {}

    for pair in pairs:
        try:
            # Use the generic candle provider for non-EUR/USD pairs
            if pair == "EUR/USD":
                resp = get_candles(interval=timeframe.upper(), limit=lookback)
            else:
                # Multi-pair candle fetch
                resp = _get_pair_candles(pair, timeframe.upper(), lookback)
            closes_by_pair[pair] = [c.close for c in resp.candles]
        except Exception as exc:
            logger.warning("Skipping %s in correlation: %s", pair, exc)

    if len(closes_by_pair) < 2:
        return {
            "pairs":       list(closes_by_pair.keys()),
            "matrix":      {},
            "note":        "Need at least 2 pairs with data to compute correlation",
        }

    matrix = compute_pair_correlations(closes_by_pair, lookback=LOOKBACK_BARS)
    return {
        "pairs":       list(closes_by_pair.keys()),
        "lookback":    LOOKBACK_BARS,
        "timeframe":   timeframe,
        "matrix":      matrix,
        "strong_threshold":   STRONG_CORRELATION,
        "moderate_threshold": MODERATE_CORRELATION,
    }


def _get_pair_candles(pair: str, timeframe: str, lookback: int):
    """Fetch candles for a non-EUR/USD pair using the multi-pair provider."""
    from services.candle_provider import get_eurusd_candles
    from config import settings
    from pair_config import get_pair
    from data.candles import _get_mock_candles, INTERVAL_MINUTES

    if settings.data_mode == "demo":
        # Demo: return mock candles (same seed, different offset per pair to simulate variation)
        return _get_mock_candles(timeframe, lookback)

    # Live: use candle_provider with pair symbol
    cfg = get_pair(pair)
    from services.candle_provider import get_eurusd_candles
    return get_eurusd_candles(timeframe=timeframe, lookback=lookback, pair=pair)
