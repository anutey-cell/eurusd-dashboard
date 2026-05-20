"""
Backtest Probability Sweep
==========================

Instead of running the heavy `run_xauusd_backtest()` function N times for N
threshold combinations (which would take N × ~30s), we run the engine ONCE
with the loosest possible gates (min_score=50, min_rr=1.0), then POST-FILTER
the resulting trade list for each (min_score, min_rr) combination on the grid
and recompute summary metrics.

Why this is correct
-------------------
`min_score` and `min_rr` are pure entry gates inside the per-bar loop:
  - min_score check:  `if result.quality_score < min_score: skip`   (line 364)
  - min_rr check:     `if rr_after < min_rr: skip`                   (line 421)

Both REJECT setups before they become trades, but don't change anything else
about the engine. So running the engine with min_score=50/min_rr=1.0 yields
a SUPERSET of trades for every tighter combination — and we just drop the
ones that fail the tighter gate to get the answer for that combination.

Speedup
-------
- N=12 threshold combinations    →  ~12x faster than running 12 backtests
- Single 5000-bar M15 backtest   ≈  25-40 seconds
- 12-combo sweep on top of that  ≈  +1-2 seconds for filtering & summary

Returns
-------
A list of `SweepRow` dicts ranked by expectancyR descending, plus a
`best_combo` shortcut for the dashboard banner.
"""
from __future__ import annotations

import logging
from dataclasses import asdict
from datetime import datetime, timezone
from typing import Iterable, Optional

from sqlalchemy.orm import Session

log = logging.getLogger(__name__)

# Default sweep grids. The user can override via request body.
DEFAULT_MIN_SCORES = [65, 70, 75, 80, 85, 90]
DEFAULT_MIN_RRS    = [1.5, 2.0, 2.5, 3.0]


def _filtered_summary(trade_dicts, score_floor: int, rr_floor: float, risk_percent: float) -> dict:
    """
    Compute a slim summary for the subset of trades that pass the (score, rr) gate.

    Trade dicts come straight from `_trade_to_dict` and carry: score, rr, result,
    points, rMultiple, signal, session.
    """
    subset = [
        t for t in trade_dicts
        if int(t.get("score", 0))  >= score_floor
        and float(t.get("rr", 0))  >= rr_floor
    ]
    n = len(subset)
    if n == 0:
        return {
            "minScore":            score_floor,
            "minRR":               rr_floor,
            "validTrades":         0,
            "wins":                0,
            "losses":              0,
            "winRate":             0.0,
            "expectancyR":         0.0,
            "expectancyPoints":    0.0,
            "profitFactor":        None,
            "averageRR":           0.0,
            "buyCount":            0,
            "sellCount":           0,
            "buyWinRate":          0.0,
            "sellWinRate":         0.0,
            "totalReturnPoints":   0.0,
            "totalReturnPercent":  0.0,
            "verdict":             "no-trades",
        }

    wins   = [t for t in subset if t.get("result") == "WIN"]
    losses = [t for t in subset if t.get("result") == "LOSS"]
    nw, nl = len(wins), len(losses)
    win_rate  = round(nw / n * 100, 2)
    loss_rate = round(nl / n * 100, 2)

    total_win_pts  = sum(float(t.get("points", 0)) for t in wins)
    total_loss_pts = abs(sum(float(t.get("points", 0)) for t in losses))
    avg_win_pts    = total_win_pts  / nw if nw else 0.0
    avg_loss_pts   = total_loss_pts / nl if nl else 0.0

    wr_frac = win_rate / 100
    lr_frac = loss_rate / 100
    expectancy_points = round(wr_frac * avg_win_pts - lr_frac * avg_loss_pts, 3)
    expectancy_r      = round(sum(float(t.get("rMultiple", 0)) for t in subset) / n, 3)
    profit_factor     = round(total_win_pts / total_loss_pts, 3) if total_loss_pts > 0 else None
    avg_rr            = round(sum(float(t.get("rr", 0))         for t in subset) / n, 3)

    buys  = [t for t in subset if t.get("signal") == "BUY"]
    sells = [t for t in subset if t.get("signal") == "SELL"]
    buy_wr  = round(sum(1 for t in buys  if t.get("result") == "WIN") / len(buys)  * 100, 2) if buys  else 0.0
    sell_wr = round(sum(1 for t in sells if t.get("result") == "WIN") / len(sells) * 100, 2) if sells else 0.0

    total_return_points  = round(sum(float(t.get("points", 0)) for t in subset), 2)
    # Each trade risks risk_percent% of capital, so 1R = +risk_percent%, -1R = -risk_percent%
    total_return_percent = round(sum(float(t.get("rMultiple", 0)) for t in subset) * risk_percent, 3)

    # Verdict bucket
    if n < 10:
        verdict = "low-sample"
    elif expectancy_r >= 0.4 and win_rate >= 50:
        verdict = "high-edge"
    elif expectancy_r >= 0.15:
        verdict = "positive-edge"
    elif expectancy_r >= 0:
        verdict = "marginal"
    else:
        verdict = "negative"

    return {
        "minScore":           score_floor,
        "minRR":              rr_floor,
        "validTrades":        n,
        "wins":               nw,
        "losses":             nl,
        "winRate":            win_rate,
        "expectancyR":        expectancy_r,
        "expectancyPoints":   expectancy_points,
        "profitFactor":       profit_factor,
        "averageRR":          avg_rr,
        "buyCount":           len(buys),
        "sellCount":          len(sells),
        "buyWinRate":         buy_wr,
        "sellWinRate":        sell_wr,
        "totalReturnPoints":  total_return_points,
        "totalReturnPercent": total_return_percent,
        "verdict":            verdict,
    }


def run_probability_sweep(
    db: Session,
    *,
    timeframe:    str = "M15",
    lookback:     int = 5000,
    min_scores:   Optional[Iterable[int]]   = None,
    min_rrs:      Optional[Iterable[float]] = None,
    engine_variant: str = "swing",
    risk_percent: float = 0.25,
    spread_points: float = 1.5,
    slippage_points: float = 0.5,
) -> dict:
    """
    Run ONE backtest at the loosest gate, then derive results for every
    (min_score, min_rr) combination on the grid by post-filtering trades.

    Returns
    -------
    dict with:
      - base_run:   the loose backtest's headline stats (so the user sees
                    "we evaluated N candidate signals")
      - rows:       list[SweepRow]  — one per (min_score, min_rr), ranked
                    by expectancyR descending
      - best_combo: shortcut to rows[0]
      - timing:     {base_run_sec, sweep_sec, total_sec}
    """
    import time
    from services.xauusd_backtester import run_xauusd_backtest

    score_grid = sorted(set(min_scores or DEFAULT_MIN_SCORES))
    rr_grid    = sorted(set(float(r) for r in (min_rrs or DEFAULT_MIN_RRS)))

    log.info(
        "[sweep] starting probability sweep: %d scores x %d rrs = %d combos, "
        "timeframe=%s lookback=%d engine=%s",
        len(score_grid), len(rr_grid), len(score_grid)*len(rr_grid),
        timeframe, lookback, engine_variant,
    )

    # ── Step 1: run the engine ONCE at the loosest possible gates ────────
    t0 = time.time()
    base = run_xauusd_backtest(
        db=db,
        timeframe=timeframe,
        lookback=lookback,
        risk_percent=risk_percent,
        spread_points=spread_points,
        slippage_points=slippage_points,
        min_score=min(score_grid),       # loosest score
        min_rr=min(rr_grid),             # loosest rr
        engine_variant=engine_variant,
        # Disable expensive post-analysis we don't need for sweep ranking
        walk_forward_segments=0,
        monte_carlo_runs=0,
        classify_regimes=False,
        analyze_skipped=False,
    )
    t_base = time.time() - t0

    # Trades come back as dicts (already serialized by _trade_to_dict).
    # We can filter on them directly — no need to re-hydrate.
    trade_dicts = base.get("trades", []) or []

    # ── Step 2: post-filter for every (score, rr) combination ────────────
    t1 = time.time()
    rows: list[dict] = []
    for s in score_grid:
        for r in rr_grid:
            rows.append(_filtered_summary(trade_dicts, s, r, risk_percent))

    # Rank: highest expectancyR first, tie-break by validTrades descending
    rows.sort(key=lambda x: (-x["expectancyR"], -x["validTrades"]))
    t_sweep = time.time() - t1

    base_summary = base.get("summary", {})
    return {
        "timeframe":    timeframe,
        "lookback":     lookback,
        "engine":       engine_variant,
        # Surface the underlying data source so the UI can warn when results
        # came from synthetic data instead of real historical candles.
        "dataSource":   base.get("dataSource", "unknown"),
        "isSynthetic":  base.get("isSynthetic", False),
        "dataWarning":  base.get("dataWarning"),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "grid": {
            "minScores": score_grid,
            "minRRs":    rr_grid,
            "total_combinations": len(score_grid) * len(rr_grid),
        },
        "base_run": {
            "candidatesScanned":  base_summary.get("totalSignalsScanned", 0),
            "validTrades":        base_summary.get("validTrades", 0),
            "minScoreUsed":       min(score_grid),
            "minRRUsed":          min(rr_grid),
        },
        "rows":         rows,
        "best_combo":   rows[0] if rows else None,
        "timing": {
            "base_run_sec": round(t_base, 2),
            "sweep_sec":    round(t_sweep, 3),
            "total_sec":    round(t_base + t_sweep, 2),
            "naive_estimate_sec": round(t_base * len(score_grid) * len(rr_grid), 2),
            "speedup": (
                round((t_base * len(score_grid) * len(rr_grid)) / (t_base + t_sweep), 1)
                if (t_base + t_sweep) > 0 else 0
            ),
        },
    }
