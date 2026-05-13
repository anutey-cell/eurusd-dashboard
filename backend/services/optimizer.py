"""
Walk-forward parameter optimizer for the ICT signal engine.

Scans a grid of (signal_window, min_rr) combinations over the in-sample portion
of the candle history, selects the best-performing parameters by composite score,
then validates them on the untouched out-of-sample portion.

This guards against pure in-sample overfitting: if the OOS performance degrades
significantly compared to IS, the result includes an overfit warning.

Config:
  IN_SAMPLE_PCT     – fraction of candles used for parameter search (default 0.7)
  MIN_IS_TRADES     – minimum trades needed in IS to consider a param combo valid

Composite score (IS):
  score = expectancy × win_rate (rewards consistent positive expectancy at scale)
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

# ── Grid ──────────────────────────────────────────────────────────────────────

SIGNAL_WINDOW_GRID = [100, 150, 200]   # bars fed to engine per check
MIN_RR_GRID        = [2.0, 2.5, 3.0]  # minimum risk-reward gate

IN_SAMPLE_PCT  = 0.70   # 70% IS / 30% OOS
MIN_IS_TRADES  = 10     # skip combos with too few IS trades (unreliable estimate)

OVERFIT_WR_GAP  = 15.0  # win-rate drop IS→OOS that triggers overfit warning (%)
OVERFIT_EXP_GAP = 5.0   # expectancy drop IS→OOS that triggers warning (pips)


# ── Public API ────────────────────────────────────────────────────────────────

def optimize(
    candles_raw: list[Any],
    pair: str = "EUR/USD",
    macro_events: list[dict] | None = None,
) -> dict:
    """
    Run walk-forward optimization over `candles_raw`.

    Returns:
      bestParams        – optimal signal_window + min_rr found on IS
      inSampleResult    – backtest summary for best params on IS slice
      outOfSampleResult – backtest summary for best params on OOS slice
      gridResults       – all param combos + IS scores (sorted best first)
      overfitWarning    – True if IS→OOS degradation exceeds threshold
    """
    from services.backtester import run_backtest

    macro_events = macro_events or []
    total = len(candles_raw)

    if total < 150:
        return {
            "error":    "Need at least 150 candles for optimization",
            "received": total,
        }

    split = int(total * IN_SAMPLE_PCT)
    is_candles  = candles_raw[:split]
    oos_candles = candles_raw[split:]

    grid_results: list[dict] = []

    for sw in SIGNAL_WINDOW_GRID:
        for mr in MIN_RR_GRID:
            try:
                r = run_backtest(
                    is_candles, macro_events,
                    pair=pair,
                    signal_window=sw,
                    min_rr_override=mr,
                )
            except Exception as exc:
                logger.warning("Optimizer: grid (%d, %.1f) failed: %s", sw, mr, exc)
                continue

            n = r.get("totalTrades", 0)
            if n < MIN_IS_TRADES:
                continue

            exp = r.get("expectancy", 0) or 0
            wr  = r.get("winRate", 0) or 0
            composite = round(exp * wr / 100, 3) if exp > 0 else -999

            grid_results.append({
                "signalWindow": sw,
                "minRR":        mr,
                "trades":       n,
                "winRate":      wr,
                "expectancy":   exp,
                "profitFactor": r.get("profitFactor"),
                "composite":    composite,
            })

    if not grid_results:
        return {
            "error": "No parameter combination produced enough trades in the in-sample period.",
            "tip":   "Try a larger lookback or a more liquid timeframe (M15, H1).",
        }

    grid_results.sort(key=lambda x: -x["composite"])
    best = grid_results[0]

    # Run IS and OOS with best params
    is_result  = run_backtest(is_candles,  macro_events, pair=pair,
                              signal_window=best["signalWindow"],
                              min_rr_override=best["minRR"])
    oos_result = run_backtest(oos_candles, macro_events, pair=pair,
                              signal_window=best["signalWindow"],
                              min_rr_override=best["minRR"])

    # Overfit check
    wr_drop  = is_result.get("winRate", 0) - oos_result.get("winRate", 0)
    exp_drop = (is_result.get("expectancy", 0) or 0) - (oos_result.get("expectancy", 0) or 0)
    overfit  = wr_drop > OVERFIT_WR_GAP or exp_drop > OVERFIT_EXP_GAP

    oos_trades = oos_result.get("totalTrades", 0)

    return {
        "pair":              pair,
        "totalCandles":      total,
        "inSampleCandles":   split,
        "outOfSampleCandles": total - split,
        "bestParams": {
            "signalWindow": best["signalWindow"],
            "minRR":        best["minRR"],
        },
        "inSampleResult":    _slim(is_result),
        "outOfSampleResult": _slim(oos_result),
        "gridResults":       grid_results,
        "overfitWarning":    overfit,
        "overfitNote": (
            f"Win rate dropped {wr_drop:.1f}% IS→OOS. "
            f"Expectancy dropped {exp_drop:.1f} pips. "
            "Consider using default parameters or collecting more data."
        ) if overfit else None,
        "confidenceNote": (
            "Insufficient OOS trades for reliable validation. "
            "Increase lookback before trusting these parameters."
        ) if oos_trades < 20 else None,
    }


def _slim(result: dict) -> dict:
    """Return a summary-only view of a backtest result (no full trade list)."""
    return {k: v for k, v in result.items() if k != "trades"}
