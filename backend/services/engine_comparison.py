"""
Multi-engine historical comparison.

Runs the same backtest configuration across N engine variants on the SAME
historical candles and returns side-by-side summary stats so the operator
can see which engine actually has edge — and which engines are too strict
to ever fire.

Currently compares: swing | intraday | momentum_breakout
(any other engine_variant accepted by xauusd_backtester can be added.)

Each variant runs in a separate call. Total time = sum of individual runs
(no parallelism — each backtest is already CPU-bound and the per-bar loop
isn't easily parallelizable).
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Iterable

from sqlalchemy.orm import Session

log = logging.getLogger(__name__)

DEFAULT_VARIANTS = ("swing", "intraday", "momentum_breakout", "momentum_fade")


def run_engine_comparison(
    db: Session,
    *,
    timeframe:       str = "M15",
    lookback:        int = 5000,
    variants:        Iterable[str] | None = None,
    min_score:       int   = 65,           # loose so all engines get a shot
    min_rr:          float = 1.5,          # loose so we measure raw edge
    risk_percent:    float = 0.25,
    spread_points:   float = 1.5,
    slippage_points: float = 0.5,
) -> dict:
    """
    Run each variant once on the same candle window, return a comparison table.

    Returns:
      {
        "timeframe", "lookback", "config",
        "variants": [
          { name, timing_sec, dataSource, isSynthetic,
            summary: {validTrades, winRate, expectancyR, profitFactor, ...},
            sample_trades: [...first 3 trades for inspection],
            error: str | None }
        ],
        "ranking": [name in best→worst order by expectancyR],
        "winner":  name | None,
        "verdict": short text about which engine to trust,
        "generated_at",
      }
    """
    from services.xauusd_backtester import run_xauusd_backtest

    variants = list(variants or DEFAULT_VARIANTS)
    log.info(
        "[engine_compare] starting %d variants over %d %s bars",
        len(variants), lookback, timeframe,
    )

    rows: list[dict] = []
    total_start = time.time()

    for variant in variants:
        t0 = time.time()
        row = {
            "name":         variant,
            "timing_sec":   0.0,
            "dataSource":   None,
            "isSynthetic":  None,
            "summary":      None,
            "sample_trades": [],
            "error":        None,
        }
        try:
            result = run_xauusd_backtest(
                db=db,
                timeframe=timeframe,
                lookback=lookback,
                risk_percent=risk_percent,
                spread_points=spread_points,
                slippage_points=slippage_points,
                min_score=min_score,
                min_rr=min_rr,
                engine_variant=variant,
                # Skip heavy post-analysis — we just need summary stats
                walk_forward_segments=0,
                monte_carlo_runs=0,
                classify_regimes=False,
                analyze_skipped=False,
            )
            s = result.get("summary", {}) or {}
            row["dataSource"]  = result.get("dataSource")
            row["isSynthetic"] = result.get("isSynthetic")
            row["summary"] = {
                "candidatesScanned":  s.get("totalSignalsScanned", 0),
                "validTrades":        s.get("validTrades", 0),
                "wins":               s.get("wins", 0),
                "losses":             s.get("losses", 0),
                "winRate":            s.get("winRate", 0.0),
                "expectancyR":        s.get("expectancyR", 0.0),
                "expectancyPoints":   s.get("expectancyPoints", 0.0),
                "profitFactor":       s.get("profitFactor"),
                "averageRR":          s.get("averageRR", 0.0),
                "maxDrawdownPercent": s.get("maxDrawdownPercent", 0.0),
                "buyWinRate":         s.get("buyWinRate", 0.0),
                "sellWinRate":        s.get("sellWinRate", 0.0),
                "netReturnPct":       s.get("netReturnPercent", 0.0),
            }
            # Grab first 3 trades for inspection (what kind of setups did this engine catch?)
            trades = result.get("trades") or []
            row["sample_trades"] = [
                {
                    "time":   t.get("entryTime"),
                    "signal": t.get("signal"),
                    "entry":  t.get("entry"),
                    "exit":   t.get("exitPrice") or t.get("adjustedExit"),
                    "rr":     t.get("rr"),
                    "result": t.get("result"),
                    "points": t.get("points"),
                    "score":  t.get("score"),
                }
                for t in trades[:3]
            ]
        except Exception as exc:
            log.exception("[engine_compare] %s failed: %s", variant, exc)
            row["error"] = str(exc)
        row["timing_sec"] = round(time.time() - t0, 2)
        rows.append(row)

    # Rank by expectancyR (when at least 5 trades) — otherwise by trade count
    def _sort_key(r):
        if r.get("error"):
            return (0, -999, 0)
        s = r.get("summary") or {}
        n = s.get("validTrades", 0)
        if n < 5:
            return (0, -990, n)
        return (1, s.get("expectancyR", 0.0), n)

    ranked = sorted(rows, key=_sort_key, reverse=True)
    winner = ranked[0]["name"] if ranked and ranked[0].get("summary") and ranked[0]["summary"]["validTrades"] >= 5 else None

    # Verdict text
    if not winner:
        any_with_trades = any(
            (r.get("summary") or {}).get("validTrades", 0) > 0
            for r in rows if not r.get("error")
        )
        if any_with_trades:
            verdict = (
                "Some engines produced trades but none with sample size >= 5 "
                "for a reliable measurement. Run with more lookback bars."
            )
        else:
            verdict = (
                "NO engine produced trades on this window. Either thresholds "
                "are too tight (lower min_score/min_rr) or the engines genuinely "
                "find no setups in this data."
            )
    else:
        s = next(r["summary"] for r in rows if r["name"] == winner)
        verdict = (
            f"{winner} wins: {s['validTrades']} trades, "
            f"WR {s['winRate']:.1f}%, ExpR {s['expectancyR']:+.2f}, "
            f"PF {s['profitFactor'] if s['profitFactor'] is not None else 'inf'}. "
            f"Consider running it live alongside swing."
        )

    return {
        "timeframe":     timeframe,
        "lookback":      lookback,
        "config": {
            "minScore":       min_score,
            "minRR":          min_rr,
            "riskPercent":    risk_percent,
            "spreadPoints":   spread_points,
            "slippagePoints": slippage_points,
        },
        "variants":     rows,
        "ranking":      [r["name"] for r in ranked],
        "winner":       winner,
        "verdict":      verdict,
        "total_sec":    round(time.time() - total_start, 2),
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }
