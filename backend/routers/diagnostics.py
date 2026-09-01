"""
Engine Diagnostics API
======================

Answers the question: "Why isn't the engine producing signals right now?"

When the user sees gold move $30 and nothing fires, this endpoint exposes:
  - The current gate-by-gate state for all 3 engines (swing, trend_pullback, momentum)
  - The exact blocking reason for each
  - Recent near-misses (engines that returned WAIT but were close to firing)
  - Data freshness across timeframes
  - Live price + 24h range so the user can sanity-check what they're seeing

Read-only. Safe under any load.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy.orm import Session

from database import get_db
from models.common import APIResponse
from rate_limit import limiter

log = logging.getLogger(__name__)
router = APIRouter(prefix="/diagnostics", tags=["diagnostics"])


@router.get(
    "/ict-framework",
    response_model=APIResponse[dict],
    summary="Advanced ICT alignment: PO3 + Daily Open + Premium/Discount + Judas (5th confirmation gate)",
)
@limiter.limit("30/minute")
def ict_framework(
    request: Request,
    direction: str | None = Query(default=None, description="Optional: BUY or SELL — scores cell against this direction"),
) -> APIResponse[dict]:
    """
    The 5th confirmation gate — combines four ICT day-trading concepts into
    a single 0-100 alignment score. Used by the auto-executor to refuse
    trades that fail the ICT confluence test even when scanner + predictor
    + killzone + policy all pass.

    Returns per-component breakdowns so you can see WHY the trade was
    allowed or refused.
    """
    from datetime import datetime, timezone
    from data.candles import get_candles
    from services.ict_advanced import compute_ict_alignment
    m15 = get_candles(interval="M15", limit=500, pair="xauusd")
    h4  = get_candles(interval="H4",  limit=100, pair="xauusd")
    sig = direction.upper() if direction else None
    ict = compute_ict_alignment(
        candles_m15=m15.candles,
        candles_h4=h4.candles,
        at=datetime.now(timezone.utc),
        signal_direction=sig,
    )
    return APIResponse(data={
        "scored_against_direction": sig,
        "score":                    ict.score,
        "posture":                  ict.posture,
        "summary":                  ict.summary,
        "blocking":                 ict.blocking,
        "components": {
            "po3": {
                "phase":     ict.po3.phase,
                "score":     ict.po3.score,
                "range_pts": ict.po3.range_pts,
                "body_pct":  ict.po3.body_pct,
                "reason":    ict.po3.reason,
            },
            "daily_open": {
                "daily_open":  ict.daily_open.daily_open,
                "current":     ict.daily_open.current,
                "bias":        ict.daily_open.bias,
                "aligned":     ict.daily_open.aligned,
                "score":       ict.daily_open.score,
                "distance":    ict.daily_open.distance_pts,
                "reason":      ict.daily_open.reason,
            },
            "premium_discount": {
                "range_high":    ict.premium_discount.range_high,
                "range_low":     ict.premium_discount.range_low,
                "equilibrium":   ict.premium_discount.equilibrium,
                "current":       ict.premium_discount.current,
                "position":      ict.premium_discount.position,
                "position_pct":  ict.premium_discount.position_pct,
                "aligned":       ict.premium_discount.aligned,
                "score":         ict.premium_discount.score,
                "reason":        ict.premium_discount.reason,
            },
            "judas": {
                "in_window":       ict.judas.in_window,
                "detected":        ict.judas.judas_detected,
                "direction":       ict.judas.judas_direction,
                "swept_level":     ict.judas.swept_level,
                "reversed":        ict.judas.reversed,
                "score":           ict.judas.score,
                "reason":          ict.judas.reason,
            },
        },
    }, source="ict_framework")


@router.get(
    "/killzone-policy",
    response_model=APIResponse[dict],
    summary="Killzone × direction policy table — the 4th gate of the auto-executor",
)
@limiter.limit("30/minute")
def killzone_policy(request: Request) -> APIResponse[dict]:
    """
    Returns the learned policy table: for each (killzone, direction) cell,
    whether the engine is ALLOWED / EXPLORE / BLOCKED to fire based on
    historical edge in the 245-trade observation dataset.

    Use this to audit what the auto-executor will refuse before it gets
    to MT5. Updates require editing services/killzone_policy.py.
    """
    from services.killzone_policy import get_full_policy
    return APIResponse(data=get_full_policy(), source="killzone_policy")


@router.get(
    "/trader-development",
    response_model=APIResponse[dict],
    summary="Curriculum of 20 trading psychology principles — engine self-evaluation per principle",
)
@limiter.limit("30/minute")
def trader_development(
    request: Request,
    db: Session = Depends(get_db),
) -> APIResponse[dict]:
    """
    Returns the full catalogue of trader-psychology principles
    (sourced from canonical books: Mark Douglas, Van Tharp, Steenbarger,
    Schwager, Kahneman, Taleb, Elder, Murphy, Covel, Peterson) and
    self-evaluates the engine against each one.

    For every principle:
      - status: FULL / PARTIAL / MISSING
      - book, author, text, engine_implication
      - category: mindset / risk / discipline / edge / structure / cognitive /
                  execution / learning

    Frontend renders these as a curriculum — what the engine has internalised,
    what is partial, what is missing. As the engine evolves (more trades,
    more data, more features), principles progress from MISSING → PARTIAL → FULL.
    """
    from config import settings
    from data.trading_psychology import evaluate_principles, PRINCIPLES
    from db_models import PaperObservation, HistoricalCandle
    from sqlalchemy import func

    # Build state context from current system
    n_resolved = (
        db.query(PaperObservation)
          .filter(PaperObservation.result.in_(["WIN", "LOSS"]))
          .count()
    )
    n_obs = db.query(PaperObservation).count()
    # Expectancy
    avg_r = 0.0
    if n_resolved > 0:
        resolved = (
            db.query(PaperObservation)
              .filter(PaperObservation.result.in_(["WIN", "LOSS"]))
              .all()
        )
        avg_r = sum((r.r_multiple or 0) for r in resolved) / n_resolved
    # Max DD estimate
    max_dd = 0.0
    if n_resolved >= 5:
        sorted_obs = (
            db.query(PaperObservation)
              .filter(PaperObservation.result.in_(["WIN", "LOSS"]))
              .order_by(PaperObservation.observed_at.asc())
              .all()
        )
        equity, peak = 10000.0, 10000.0
        for r in sorted_obs:
            equity += equity * 0.0025 * (r.r_multiple or 0)
            peak = max(peak, equity)
            dd = (peak - equity) / peak * 100
            max_dd = max(max_dd, dd)
    # Synthetic data presence
    synth_n = (
        db.query(func.count(HistoricalCandle.id))
          .filter(HistoricalCandle.source.in_(["synthetic", "synthetic_seed", "seed", "generated"]))
          .scalar() or 0
    )

    ctx = {
        "resolved_observations":   n_resolved,
        "observation_count":       n_obs,
        "expectancy_r":            avg_r,
        "max_dd_pct":              max_dd,
        "synthetic_data_count":    synth_n,
        "data_source":             "real" if synth_n == 0 else "mixed",
        # Engine features the catalogue checks for
        "has_rr":                  True,
        "has_scores":              True,
        "r_multiples_logged":      True,
        "fixed_fractional":        True,
        "max_lot_cap":             settings.auto_execution_max_lot,
        "sl_required":             True,
        "avg_down_anywhere":       False,
        "auto_execution":          settings.auto_execution_enabled,
        "max_trades_per_day":      settings.auto_execution_max_trades_per_day,
        "htf_trend_filter_enabled":True,
        "min_rr_gate":             2.5,
        "sample_size_in_metrics":  True,
        "daily_loss_limit":        settings.daily_loss_limit_percent,
        "news_blackout":           True,
        "max_open_trades":         settings.max_open_trades,
        "multi_layer_confirmation":True,
        "correlation_engine":      True,
        "killzone_analyzer":       True,
        "sl_in_order_send":        True,
        "plan_completeness":       True,
        "discretionary_overrides": False,
    }

    principles = evaluate_principles(ctx)

    # Aggregate
    full    = sum(1 for p in principles if p["status"] == "FULL")
    partial = sum(1 for p in principles if p["status"] == "PARTIAL")
    missing = sum(1 for p in principles if p["status"] == "MISSING")
    total   = len(principles)
    pct_internalized = round((full + 0.5 * partial) / total * 100, 1)

    # Next 3 principles to focus on (highest-priority MISSING/PARTIAL)
    priorities = (
        [p for p in principles if p["status"] == "MISSING"][:3]
        + [p for p in principles if p["status"] == "PARTIAL"][:3]
    )[:3]

    # Group by category for the UI
    by_category: dict[str, list] = {}
    for p in principles:
        by_category.setdefault(p["category"], []).append(p)

    return APIResponse(
        data={
            "total_principles": total,
            "full":             full,
            "partial":          partial,
            "missing":          missing,
            "percent_internalized": pct_internalized,
            "principles":       principles,
            "by_category":      by_category,
            "next_to_internalize": priorities,
            "books_referenced": sorted({p["book"] for p in PRINCIPLES}),
            "context_snapshot": ctx,
            "generated_at":     datetime.now(timezone.utc).isoformat(),
        },
        source="trader_development",
    )


@router.get(
    "/intermarket-correlations",
    response_model=APIResponse[dict],
    summary="Live correlations: gold vs DXY/yields/oil/VIX/silver/SPX with regime detection",
)
@limiter.limit("12/minute")
def intermarket_correlations(
    request: Request,
    timeframe: str = Query(default="H1"),
    n_bars:    int = Query(default=200, ge=50, le=500),
) -> APIResponse[dict]:
    from services.correlation_engine import compute_intermarket_correlations
    return APIResponse(
        data=compute_intermarket_correlations(timeframe=timeframe, n_bars=n_bars),
        source="correlation_engine",
    )


@router.get(
    "/trader-mindset",
    response_model=APIResponse[dict],
    summary="10-dimension scorecard: does the engine have a profitable trader's mindset?",
)
@limiter.limit("30/minute")
def trader_mindset(
    request: Request,
    db: Session = Depends(get_db),
) -> APIResponse[dict]:
    from services.trader_mindset_score import score_trader_mindset
    return APIResponse(data=score_trader_mindset(db), source="trader_mindset")


@router.get(
    "/momentum-trace",
    response_model=APIResponse[dict],
    summary="Trace momentum_breakout call on N most recent historical bars — see why each fails",
)
@limiter.limit("6/minute")
def momentum_trace(
    request: Request,
    timeframe: str = Query(default="M15"),
    lookback:  int = Query(default=500, ge=100, le=2000),
    sample:    int = Query(default=20, ge=1, le=100,
                           description="How many recent bars to trace through the engine"),
    db: Session = Depends(get_db),
) -> APIResponse[dict]:
    """
    Invokes analyze_momentum_breakout on each of the last N candles in the DB
    and returns the per-call signal + reason. Lets us see WHY the engine returns
    WAIT for every bar (which gate is rejecting).
    """
    from services.historical_data_provider import get_historical_candles
    from services.intraday_strategies import analyze_momentum_breakout

    candles, source = get_historical_candles(
        db=db, timeframe=timeframe, lookback=lookback,
        allow_synthetic_fallback=False,
    )
    if len(candles) < sample + 30:
        return APIResponse(data={
            "error": f"Not enough candles ({len(candles)}). Need {sample + 30}.",
            "source": source,
        })

    traces = []
    n = len(candles)
    for i in range(n - sample, n):
        window = candles[:i + 1]
        bar = window[-1]
        try:
            result = analyze_momentum_breakout(
                candles=window,
                at=bar.time if bar.time.tzinfo else bar.time.replace(tzinfo=timezone.utc),
                macro_events=[],
                pip_size=1.0,
                enable_killzone=True,
                enable_news_filter=False,
            )
            traces.append({
                "barTime": bar.time.isoformat(),
                "barOpen": bar.open,
                "barHigh": bar.high,
                "barLow":  bar.low,
                "barClose":bar.close,
                "barRange":round(bar.high - bar.low, 2),
                "barBody": round(abs(bar.close - bar.open), 2),
                "barVol":  bar.volume,
                "signal":  result.signal,
                "score":   result.quality_score,
                "reason":  (result.reason or "")[:200],
            })
        except Exception as exc:
            traces.append({
                "barTime": bar.time.isoformat(),
                "error":   str(exc),
            })

    # Summarise — by-reason counter
    from collections import Counter
    reason_counter = Counter(
        t.get("reason", "")[:60] for t in traces if t.get("signal") == "WAIT"
    )
    by_signal = Counter(t.get("signal") for t in traces if "signal" in t)

    return APIResponse(data={
        "timeframe":    timeframe,
        "candleCount":  len(candles),
        "samplesTraced":len(traces),
        "dataSource":   source,
        "signalCounts": dict(by_signal),
        "topWaitReasons": dict(reason_counter.most_common(10)),
        "traces":       traces,
    })


@router.get(
    "/momentum-audit",
    response_model=APIResponse[dict],
    summary="Diagnose why momentum_breakout fires 0 times — per-gate filtering breakdown",
)
@limiter.limit("6/minute")
def momentum_audit(
    request: Request,
    timeframe: str = Query(default="M15"),
    lookback:  int = Query(default=5000, ge=200, le=20000),
    db: Session = Depends(get_db),
) -> APIResponse[dict]:
    from services.historical_data_provider import get_historical_candles
    from services.momentum_gate_audit import audit_momentum_gates

    candles, source = get_historical_candles(
        db=db, timeframe=timeframe, lookback=lookback,
        allow_synthetic_fallback=False,
    )
    if not candles:
        return APIResponse(data={
            "error": f"No real {timeframe} candles in DB.",
            "source": source,
        })

    audit = audit_momentum_gates(candles)
    return APIResponse(data={
        "timeframe":   timeframe,
        "candleCount": len(candles),
        "dataSource":  source,
        "audit":       audit,
    })


@router.get(
    "/engine-state",
    response_model=APIResponse[dict],
    summary="Why no signal right now? Per-engine gate state + data freshness",
)
@limiter.limit("30/minute")
def engine_state(
    request: Request,
    db: Session = Depends(get_db),
) -> APIResponse[dict]:
    from data.candles import get_candles
    from services.intraday_strategies import (
        analyze_momentum_breakout, analyze_trend_pullback,
    )
    from services.institutional_scanner import scan_xauusd_market

    LIVE_SOURCES = {"tradingview", "mt5", "tradingview-cached", "mt5-cached"}
    now = datetime.now(timezone.utc)

    # ── Data freshness sweep across timeframes ──────────────────────────
    freshness: list[dict] = []
    candles_by_tf: dict[str, list] = {}
    for tf, period_min in (("M5", 5), ("M15", 15), ("H1", 60), ("H4", 240)):
        try:
            resp = get_candles(interval=tf, limit=200, pair="xauusd")
            cs = resp.candles
            last_t = cs[-1].time if cs else None
            if last_t and not last_t.tzinfo:
                last_t = last_t.replace(tzinfo=timezone.utc)
            age_min = (now - last_t).total_seconds() / 60 if last_t else None
            stale_threshold = period_min * (2.5 if tf == "H4" else 2)
            freshness.append({
                "timeframe":      tf,
                "source":         getattr(resp, "source", "unknown"),
                "count":          len(cs),
                "lastBarTime":    last_t.isoformat() if last_t else None,
                "ageMinutes":     round(age_min, 1) if age_min else None,
                "staleThreshold": stale_threshold,
                "isLive":         getattr(resp, "source", "") in LIVE_SOURCES,
                "isFresh":        (age_min is not None and age_min <= stale_threshold),
            })
            candles_by_tf[tf] = cs
        except Exception as exc:
            freshness.append({"timeframe": tf, "error": str(exc)})

    # ── Live price + 24h range ──────────────────────────────────────────
    price_snapshot = {}
    m15 = candles_by_tf.get("M15", [])
    if len(m15) >= 96:
        last96 = m15[-96:]
        price_snapshot = {
            "currentPrice":   round(last96[-1].close, 2),
            "open24hAgo":     round(last96[0].open, 2),
            "high24h":        round(max(c.high for c in last96), 2),
            "low24h":         round(min(c.low  for c in last96), 2),
            "range24h":       round(max(c.high for c in last96) - min(c.low for c in last96), 2),
            "netChange":      round(last96[-1].close - last96[0].close, 2),
            "netChangePct":   round((last96[-1].close - last96[0].close) / last96[0].close * 100, 2),
        }
        # Highlight the biggest M15 expansion in last 24h
        biggest = max(last96, key=lambda c: abs(c.close - c.open))
        price_snapshot["biggestM15Move"] = {
            "time":      biggest.time.isoformat() if hasattr(biggest, "time") else None,
            "direction": "BUY" if biggest.close > biggest.open else "SELL",
            "body":      round(abs(biggest.close - biggest.open), 2),
            "range":     round(biggest.high - biggest.low, 2),
            "volume":    int(biggest.volume),
        }

    # ── Engine 1: Scanner (swing ICT) ───────────────────────────────────
    scanner_diag = {"engine": "swing_ict", "status": "unknown"}
    try:
        scan = scan_xauusd_market(force_refresh=False, db=db)
        scanner_diag = {
            "engine":         "swing_ict",
            "status":         scan.get("marketState"),
            "signal":         scan.get("signal"),
            "qualityScore":   (scan.get("recommendedAction") or {}).get("tradePlan", {}).get("qualityScore", 0),
            "summary":        (scan.get("summary") or "")[:200],
            "blockers":       scan.get("blockers", [])[:5],
            "passing":        scan.get("marketState") == "SIGNAL_READY",
        }
    except Exception as exc:
        scanner_diag["error"] = str(exc)

    # ── Engine 2: Trend Pullback ────────────────────────────────────────
    tp_diag = {"engine": "trend_pullback", "status": "no_data"}
    h1 = candles_by_tf.get("H1", [])
    if len(h1) >= 60:
        try:
            tp_res = analyze_trend_pullback(
                candles=h1, at=now, macro_events=[],
                pip_size=1.0, target_pips=50, max_sl_pips=15, min_rr=2.0,
                enable_killzone=True, enable_news_filter=False,
            )
            tp_diag = {
                "engine":  "trend_pullback",
                "status":  "READY" if tp_res.signal in ("BUY", "SELL") else "WAIT",
                "signal":  tp_res.signal,
                "score":   tp_res.quality_score,
                "reason":  (tp_res.reason or "")[:200],
                "passing": tp_res.signal in ("BUY", "SELL"),
            }
        except Exception as exc:
            tp_diag["error"] = str(exc)

    # ── Engine 3: Momentum Breakout (NEW) ───────────────────────────────
    mb_diag = {"engine": "momentum_breakout", "status": "no_data"}
    if len(m15) >= 22:
        try:
            mb_res = analyze_momentum_breakout(
                candles=m15, at=now, macro_events=[],
                pip_size=1.0, target_rr=2.5,
                enable_killzone=True, enable_news_filter=False,
            )
            mb_diag = {
                "engine":  "momentum_breakout",
                "status":  "READY" if mb_res.signal in ("BUY", "SELL") else "WAIT",
                "signal":  mb_res.signal,
                "score":   mb_res.quality_score,
                "reason":  (mb_res.reason or "")[:200],
                "passing": mb_res.signal in ("BUY", "SELL"),
            }
        except Exception as exc:
            mb_diag["error"] = str(exc)

    # ── Overall verdict ─────────────────────────────────────────────────
    any_engine_ready = any(d.get("passing") for d in (scanner_diag, tp_diag, mb_diag))
    data_ok = all(f.get("isFresh", False) and f.get("isLive", False)
                  for f in freshness if "error" not in f)

    # ── Historical data-store check (powers paper observations + backtests) ──
    learning_data = {"hasReal": False, "realCount": 0, "syntheticCount": 0, "details": {}}
    try:
        from db_models import HistoricalCandle
        from sqlalchemy import func
        REAL_SOURCES = ("csv", "tradingview", "mt5", "provider", "sync", "broker")
        SYNTH_SOURCES = ("synthetic", "synthetic_seed", "seed", "generated")
        for tf in ("M15", "H1", "H4"):
            real_n = (
                db.query(func.count(HistoricalCandle.id))
                  .filter(HistoricalCandle.timeframe == tf,
                          HistoricalCandle.source.in_(REAL_SOURCES))
                  .scalar() or 0
            )
            synth_n = (
                db.query(func.count(HistoricalCandle.id))
                  .filter(HistoricalCandle.timeframe == tf,
                          HistoricalCandle.source.in_(SYNTH_SOURCES))
                  .scalar() or 0
            )
            learning_data["details"][tf] = {"real": real_n, "synthetic": synth_n}
            learning_data["realCount"]      += real_n
            learning_data["syntheticCount"] += synth_n
        learning_data["hasReal"] = learning_data["realCount"] > 0
        learning_data["needsBackfill"] = (
            learning_data["syntheticCount"] > 0 or learning_data["realCount"] == 0
        )
    except Exception as exc:
        learning_data["error"] = str(exc)

    return APIResponse(
        data={
            "generatedAt":    now.isoformat(),
            "priceSnapshot":  price_snapshot,
            "dataFreshness":  freshness,
            "dataAllFresh":   data_ok,
            "learningData":   learning_data,
            "engines": {
                "swing":             scanner_diag,
                "trend_pullback":    tp_diag,
                "momentum_breakout": mb_diag,
            },
            "anyEngineReady": any_engine_ready,
            "verdict": (
                "AT_LEAST_ONE_ENGINE_READY" if any_engine_ready
                else "ALL_ENGINES_WAIT — see each engine's reason field"
            ),
        },
        source="diagnostics",
    )


@router.get(
    "/skip-stats",
    response_model=APIResponse[dict],
    summary="Rolling in-process 'why did I skip' counter (P131)",
)
@limiter.limit("30/minute")
def skip_stats(request: Request,
                hours: int = Query(default=24, ge=1, le=168)) -> APIResponse[dict]:
    """
    Aggregates the last N hours of strategist verdicts into a breakdown of
    which condition failed most often, which gate blocked most often, and
    the setup_score distribution. Resets on process restart.
    """
    from services.skip_stats import summary
    return APIResponse(data=summary(hours=hours), source="diagnostics")


@router.get(
    "/freshness",
    response_model=APIResponse[dict],
    summary="Historical-candle freshness across timeframes (P131)",
)
@limiter.limit("30/minute")
def freshness(request: Request, db: Session = Depends(get_db)) -> APIResponse[dict]:
    from services.data_freshness import check_freshness
    return APIResponse(data=check_freshness(db), source="diagnostics")


@router.get(
    "/rollout-status",
    response_model=APIResponse[dict],
    summary="Rollout gates (Phase 15) — per-flag readiness + kill-switch hint",
)
@limiter.limit("20/minute")
def rollout_status(
    request: Request,
    db: Session = Depends(get_db),
) -> APIResponse[dict]:
    """
    For every Phase 2-14 feature flag, report:
      currently_enabled, gate_flag, gate_currently,
      requires (promotion criteria), risk, ready (True/False/None),
      ready_reason.

    None = cannot judge (missing signal). Never writes config; only
    recommends. Includes emergency-disable instructions.
    """
    from services.rollout_gates import evaluate_rollout
    report = evaluate_rollout(db)
    return APIResponse(data=report.to_dict(), source="diagnostics")


@router.get(
    "/replay-validation",
    response_model=APIResponse[dict],
    summary="Replay validation (Phase 14) — old engine vs new engine over N days",
)
@limiter.limit("10/minute")
def replay_validation(
    request: Request,
    days: int = Query(default=30, ge=7, le=60),
    scan_first: bool = Query(default=True,
                                description="Populate qualifying_expansions first"),
    db: Session = Depends(get_db),
) -> APIResponse[dict]:
    """
    Compare old engine (strategist_verdicts cp>=3 BUY/SELL) against new engine
    (market_intelligence_alerts) over the last N days. Uses
    qualifying_expansions as ground truth.

    Returns per-engine metrics + delta + verdict (BETTER / MIXED / WORSE /
    NEUTRAL / INSUFFICIENT_SAMPLE), plus per-day scenario tagging.
    """
    from services.replay_engine import run_replay
    from services.opportunity_coverage import detect_and_score
    if scan_first:
        detect_and_score(db, lookback_hours=days * 24)
    report = run_replay(db, days=days)
    return APIResponse(data=report.to_dict(), source="diagnostics")


@router.get(
    "/opportunity-coverage",
    response_model=APIResponse[dict],
    summary="Opportunity coverage report (Phase 13) — how many qualifying moves did we catch?",
)
@limiter.limit("20/minute")
def opportunity_coverage(
    request: Request,
    days: int = Query(default=30, ge=1, le=90),
    scan_hours: int = Query(default=168, description="Hours to scan for new expansions (0=skip)"),
    db: Session = Depends(get_db),
) -> APIResponse[dict]:
    """
    Returns the coverage report: bull/bear/overall coverage %, median
    detection delay, missed count, late detections, false directional alerts,
    plus a verdict (ON TARGET / BELOW TARGET / UNDER-DETECTING / …).

    Pass `scan_hours=0` to skip re-scanning and just return the report from
    already-detected rows.
    """
    from services.opportunity_coverage import detect_and_score, compute_coverage_report
    scan_result = None
    if scan_hours > 0:
        scan_result = detect_and_score(db, lookback_hours=scan_hours)
    report = compute_coverage_report(db, days=days)
    return APIResponse(
        data={"report": report.to_dict(), "scan": scan_result},
        source="diagnostics",
    )


@router.get(
    "/missed-expansions",
    response_model=APIResponse[dict],
    summary="Missed expansions list (Phase 13) — the misses we need to close",
)
@limiter.limit("20/minute")
def missed_expansions_endpoint(
    request: Request,
    days: int = Query(default=30, ge=1, le=90),
    db: Session = Depends(get_db),
) -> APIResponse[dict]:
    """Returns the list of qualifying expansions with no matched alert."""
    from services.opportunity_coverage import missed_expansions
    misses = missed_expansions(db, days=days)
    return APIResponse(data={"count": len(misses), "misses": misses},
                        source="diagnostics")


@router.get(
    "/market-intelligence-alerts",
    response_model=APIResponse[dict],
    summary="Market intelligence alert engine (Phase 11) — 18 alert types, shadow-first",
)
@limiter.limit("30/minute")
def market_intelligence_alerts(
    request: Request,
    fire: bool = Query(default=False, description="Actually attempt to fire alerts (respects flags)"),
    force_send: bool = Query(default=False, description="Bypass flags (dry-run only if fire=true)"),
    history: int = Query(default=20, ge=0, le=100),
    db: Session = Depends(get_db),
) -> APIResponse[dict]:
    """
    Runs the full Phase 2-10 pipeline, detects intel-worthy transitions,
    and (if `fire=true`) processes each candidate through cooldown + daily
    cap + delivery.

    Delivery result values: sent | shadow | suppressed | failed.
    Default endpoint call = detect-only (no persist, no send).
    """
    from services.canonical_market_data import get_canonical
    from services.htf_weighted_alignment import compute_htf_alignment
    from services.market_regime import classify_regime
    from services.directional_evidence import compute_directional_evidence
    from services.breakout_acceptance import scan_key_levels
    from services.opportunity_state import evaluate_and_transition
    from services.separated_verdicts import compute_separated_verdict
    from services.key_level_ranking import rank_key_levels
    from services.macro_interpretation import compute_macro_context
    from services.market_intelligence_alerts import (
        detect_alert_candidates, fire_intel_alerts, recent_intel_alerts,
    )
    from config import settings

    cmd = get_canonical(cache_ttl_s=settings.xauusd_canonical_data_cache_ttl_s)
    snap = cmd.snapshot(db)

    try:
        from services.calendar_provider import get_upcoming_events
        events = get_upcoming_events(hours=8) or []
    except Exception:
        events = []

    htf = compute_htf_alignment(snap)
    regime = classify_regime(snap, upcoming_events=events)
    evidence = compute_directional_evidence(
        snap, htf_alignment=htf, regime=regime, upcoming_events=events,
    )
    breakouts = scan_key_levels(snap, htf_alignment=htf)
    state_tr = evaluate_and_transition(
        db, snapshot=snap, regime=regime, htf_alignment=htf,
        evidence=evidence, breakouts=breakouts, persist=False,
    )
    verdict = compute_separated_verdict(
        snapshot=snap, htf_alignment=htf, regime=regime, evidence=evidence,
        breakouts=breakouts, state_transition=state_tr,
    )
    ranking = rank_key_levels(snap, breakouts=breakouts)

    # Macro (best-effort)
    macro = None
    try:
        correlation_snapshot = None
        yields_context = None
        dxy_bars = None
        try:
            from services.correlation_engine import compute_intermarket_correlations
            correlation_snapshot = compute_intermarket_correlations(timeframe="H1", n_bars=100)
        except Exception: pass
        try:
            from services.fred_provider import get_yields_context
            yields_context = get_yields_context()
        except Exception: pass
        try:
            from services.tradingview_provider import get_tv_candles
            dxy_bars = get_tv_candles("dxy", timeframe="H1", limit=40)
        except Exception: pass
        macro = compute_macro_context(
            snapshot=snap, tech_direction=htf.direction,
            upcoming_events=events, dxy_bars=dxy_bars,
            correlation_snapshot=correlation_snapshot, yields_context=yields_context,
        )
    except Exception:
        pass

    # Detect (always)
    cands = detect_alert_candidates(
        prev_state=None,   # detection-only view uses "no prev" so all state hits register
        new_state=state_tr.new_state,
        trigger_condition=state_tr.trigger_condition,
        trigger_price=state_tr.price,
        breakouts=breakouts, macro=macro, snapshot=snap,
    )

    outcomes = []
    if fire:
        outcomes = fire_intel_alerts(
            db, prev_state=state_tr.prev_state, new_state=state_tr.new_state,
            trigger_condition=state_tr.trigger_condition,
            trigger_price=state_tr.price,
            snapshot=snap, verdict=verdict, evidence=evidence, ranking=ranking,
            macro=macro, state_transition=state_tr, breakouts=breakouts,
            force_send=force_send,
        )

    return APIResponse(
        data={
            "state": state_tr.new_state,
            "candidates": [c.to_dict() for c in cands],
            "outcomes": [o.to_dict() for o in outcomes],
            "history": recent_intel_alerts(db, limit=history),
            "flags": {
                "market_intelligence_telegram_enabled": settings.xauusd_market_intelligence_telegram_enabled,
                "shadow_mode": settings.xauusd_market_intel_shadow_mode,
            },
        },
        source="diagnostics",
    )


@router.get(
    "/macro-context",
    response_model=APIResponse[dict],
    summary="Enhanced macro interpretation (Phase 10) — DXY, yields, correlation, event risk",
)
@limiter.limit("30/minute")
def macro_context(
    request: Request,
    force: bool = Query(default=False),
    db: Session = Depends(get_db),
) -> APIResponse[dict]:
    """
    Returns DXY direction + yield direction + gold-DXY correlation state +
    macro alignment with current technicals + move driver classification +
    time-to-next-high-impact event.

    Consumes correlation_engine + fred_provider + calendar. Any input missing
    just marks that field UNKNOWN — never blocks the assessment.
    """
    from services.canonical_market_data import get_canonical
    from services.htf_weighted_alignment import compute_htf_alignment
    from services.macro_interpretation import compute_macro_context
    from config import settings

    cmd = get_canonical(cache_ttl_s=settings.xauusd_canonical_data_cache_ttl_s)
    snap = cmd.snapshot(db, force_refresh=force)
    htf = compute_htf_alignment(snap)
    tech_direction = htf.direction

    # Optional data sources — best-effort, never raise
    correlation_snapshot = None
    yields_context = None
    dxy_bars = None
    try:
        from services.correlation_engine import compute_intermarket_correlations
        correlation_snapshot = compute_intermarket_correlations(timeframe="H1", n_bars=100)
    except Exception as exc:
        log.debug("[macro-context] correlation snapshot skipped: %s", exc)

    try:
        from services.fred_provider import get_yields_context
        yields_context = get_yields_context()
    except Exception as exc:
        log.debug("[macro-context] yields context skipped: %s", exc)

    try:
        from services.tradingview_provider import get_tv_candles
        dxy_bars = get_tv_candles("dxy", timeframe="H1", limit=40)
    except Exception as exc:
        log.debug("[macro-context] dxy bars skipped: %s", exc)

    try:
        from services.calendar_provider import get_upcoming_events
        events = get_upcoming_events(hours=8) or []
    except Exception:
        events = []

    assessment = compute_macro_context(
        snapshot=snap, tech_direction=tech_direction,
        upcoming_events=events, dxy_bars=dxy_bars,
        correlation_snapshot=correlation_snapshot,
        yields_context=yields_context,
    )
    return APIResponse(data=assessment.to_dict(), source="diagnostics")


@router.get(
    "/key-level-ranking",
    response_model=APIResponse[dict],
    summary="Ranked key levels into Tier 1/2/3 (Phase 9)",
)
@limiter.limit("30/minute")
def key_level_ranking(
    request: Request,
    force: bool = Query(default=False),
    db: Session = Depends(get_db),
) -> APIResponse[dict]:
    """
    Returns levels grouped into three tiers:
      Tier 1 — immediate decision levels (≤ 2 ATR, score ≥ 40, up to 4)
      Tier 2 — important supporting levels (≤ 4 ATR, score ≥ 25, up to 6)
      Tier 3 — secondary intraday references (up to 8)

    Consumes PDH/PDL/PWH/PWL/Asian/session levels + H4/H1 swing pivots
    (+ optionally liquidity_map zones + breakout retest/acceptance context).
    Ranks by TF weight, tag boost, reactions, sweep/acceptance/flipped role,
    distance from price, and confluence.
    """
    from services.canonical_market_data import get_canonical
    from services.htf_weighted_alignment import compute_htf_alignment
    from services.breakout_acceptance import scan_key_levels
    from services.key_level_ranking import rank_key_levels
    from config import settings

    cmd = get_canonical(cache_ttl_s=settings.xauusd_canonical_data_cache_ttl_s)
    snap = cmd.snapshot(db, force_refresh=force)
    htf = compute_htf_alignment(snap)
    breakouts = scan_key_levels(snap, htf_alignment=htf)

    # Optional: existing liquidity_map — try to include
    lm = None
    try:
        from services.liquidity_map import build_liquidity_map
        d1 = snap.timeframes.get("D1")
        m15 = snap.timeframes.get("M15")
        h1 = snap.timeframes.get("H1")
        if d1 and m15 and h1 and d1.candles and m15.candles and h1.candles:
            lm = build_liquidity_map(
                candles_d1=d1.candles, candles_m15=m15.candles,
                candles_h1=h1.candles,
                current_price=m15.candles[-1].close,
                atr_h1=None,
            )
    except Exception as exc:
        log.debug("[key_level_ranking] liquidity_map integration skipped: %s", exc)

    ranking = rank_key_levels(snap, liquidity_map=lm, breakouts=breakouts)
    return APIResponse(data=ranking.to_dict(), source="diagnostics")


@router.get(
    "/separated-verdicts",
    response_model=APIResponse[dict],
    summary="Direction / Opportunity / Entry three-part verdict (Phase 8)",
)
@limiter.limit("30/minute")
def separated_verdicts(
    request: Request,
    force: bool = Query(default=False),
    db: Session = Depends(get_db),
) -> APIResponse[dict]:
    """
    Publishes the three separate conclusions the brief demands — the fix
    for "STAND ASIDE hides direction". Does NOT alter the mandate
    strategist's entry rules (Phase 12 preserves them). Read-only view.

    Fields:
      directional_assessment ∈ Strong bullish … Strong bearish (7 levels)
      opportunity_status     ∈ Conditions developing … Thesis invalidated
      entry_status           ∈ No compliant entry … Entry confirmed
      + reasons for each + ready_to_alert flag + confidence
    """
    from services.canonical_market_data import get_canonical
    from services.htf_weighted_alignment import compute_htf_alignment
    from services.market_regime import classify_regime
    from services.directional_evidence import compute_directional_evidence
    from services.breakout_acceptance import scan_key_levels
    from services.opportunity_state import evaluate_and_transition
    from services.separated_verdicts import compute_separated_verdict
    from config import settings

    cmd = get_canonical(cache_ttl_s=settings.xauusd_canonical_data_cache_ttl_s)
    snap = cmd.snapshot(db, force_refresh=force)

    try:
        from services.calendar_provider import get_upcoming_events
        events = get_upcoming_events(hours=2) or []
    except Exception:
        events = []

    htf = compute_htf_alignment(snap)
    regime = classify_regime(snap, upcoming_events=events)
    evidence = compute_directional_evidence(
        snap, htf_alignment=htf, regime=regime, upcoming_events=events,
    )
    breakouts = scan_key_levels(snap, htf_alignment=htf)
    # Non-persisting evaluation — we just need the current transition object
    state_tr = evaluate_and_transition(
        db, snapshot=snap, regime=regime, htf_alignment=htf,
        evidence=evidence, breakouts=breakouts, persist=False,
    )
    verdict = compute_separated_verdict(
        snapshot=snap, htf_alignment=htf, regime=regime, evidence=evidence,
        breakouts=breakouts, state_transition=state_tr,
    )
    return APIResponse(
        data={
            "verdict": verdict.to_dict(),
            "supporting": {
                "state_transition_new_state": state_tr.new_state,
                "htf_direction": htf.direction,
                "htf_strength":  htf.strength,
                "htf_score":     htf.score,
                "regime":        regime.regime,
                "dq_score":      evidence.data_quality_score,
                "bull_evidence": evidence.bull_evidence_score,
                "bear_evidence": evidence.bear_evidence_score,
                "contradiction": evidence.contradiction_score,
                "extension_risk": evidence.extension_risk_score,
                "event_risk":    evidence.event_risk_score,
            },
        },
        source="diagnostics",
    )


@router.get(
    "/opportunity-state",
    response_model=APIResponse[dict],
    summary="Opportunity state machine (Phase 7) — persisted bull/bear state graph",
)
@limiter.limit("30/minute")
def opportunity_state(
    request: Request,
    force: bool = Query(default=False),
    persist: bool = Query(default=False, description="Write transition to DB if state changed"),
    history: int = Query(default=10, ge=0, le=100),
    db: Session = Depends(get_db),
) -> APIResponse[dict]:
    """
    Runs the full Phase 2-6 pipeline (canonical → HTF → regime → evidence →
    breakout scan) and asks the state machine what the current opportunity
    state is. Optionally persists the transition if `persist=true`.

    Returns the current transition + recent history from the DB.
    """
    from services.canonical_market_data import get_canonical
    from services.htf_weighted_alignment import compute_htf_alignment
    from services.market_regime import classify_regime
    from services.directional_evidence import compute_directional_evidence
    from services.breakout_acceptance import scan_key_levels
    from services.opportunity_state import (
        evaluate_and_transition, get_recent_transitions,
    )
    from config import settings

    cmd = get_canonical(cache_ttl_s=settings.xauusd_canonical_data_cache_ttl_s)
    snap = cmd.snapshot(db, force_refresh=force)

    try:
        from services.calendar_provider import get_upcoming_events
        events = get_upcoming_events(hours=2) or []
    except Exception:
        events = []

    htf = compute_htf_alignment(snap)
    regime = classify_regime(snap, upcoming_events=events)
    evidence = compute_directional_evidence(
        snap, htf_alignment=htf, regime=regime, upcoming_events=events,
    )
    breakouts = scan_key_levels(snap, htf_alignment=htf)

    tr = evaluate_and_transition(
        db, snapshot=snap, regime=regime, htf_alignment=htf,
        evidence=evidence, breakouts=breakouts, persist=persist,
    )

    return APIResponse(
        data={
            "current_transition": tr.to_dict(),
            "history": get_recent_transitions(db, limit=history),
        },
        source="diagnostics",
    )


@router.get(
    "/breakout-acceptance",
    response_model=APIResponse[dict],
    summary="Breakout acceptance classification (Phase 6) — 9-way per key level",
)
@limiter.limit("30/minute")
def breakout_acceptance(
    request: Request,
    level: float = Query(default=None, description="Optional specific level"),
    direction: str = Query(default=None, description="UP or DOWN (with `level`)"),
    force: bool = Query(default=False),
    db: Session = Depends(get_db),
) -> APIResponse[dict]:
    """
    Without params: scans PDH/PDL/PWH/PWL/Asian-high/Asian-low and returns
    a list of active breakout assessments.
    With `level` and `direction`: classifies a specific level only.
    """
    from services.canonical_market_data import get_canonical
    from services.htf_weighted_alignment import compute_htf_alignment
    from services.breakout_acceptance import classify_breakout, scan_key_levels
    from config import settings

    cmd = get_canonical(cache_ttl_s=settings.xauusd_canonical_data_cache_ttl_s)
    snap = cmd.snapshot(db, force_refresh=force)
    htf = compute_htf_alignment(snap)

    if level is not None and direction:
        result = classify_breakout(snap, level=float(level),
                                    direction=direction.upper(),
                                    level_name="user", htf_alignment=htf)
        return APIResponse(data=result.to_dict(), source="diagnostics")

    assessments = scan_key_levels(snap, htf_alignment=htf)
    return APIResponse(
        data={"count": len(assessments),
              "assessments": [a.to_dict() for a in assessments]},
        source="diagnostics",
    )


@router.get(
    "/directional-evidence",
    response_model=APIResponse[dict],
    summary="Directional evidence + contradiction scores (Phase 5) — 8-way multi-score",
)
@limiter.limit("30/minute")
def directional_evidence(
    request: Request,
    force: bool = Query(default=False),
    db: Session = Depends(get_db),
) -> APIResponse[dict]:
    """
    Returns the 8-score evidence assessment:
      bull_evidence_score, bear_evidence_score, contradiction_score,
      data_quality_score, event_risk_score, extension_risk_score,
      directional_confidence, entry_quality_confidence
    Plus itemised bull_items[], bear_items[], contradictions[].

    Contradictions REDUCE confidence proportionately, they do not veto.
    """
    from services.canonical_market_data import get_canonical
    from services.htf_weighted_alignment import compute_htf_alignment
    from services.market_regime import classify_regime
    from services.directional_evidence import compute_directional_evidence
    from config import settings

    cmd = get_canonical(cache_ttl_s=settings.xauusd_canonical_data_cache_ttl_s)
    snap = cmd.snapshot(db, force_refresh=force)

    try:
        from services.calendar_provider import get_upcoming_events
        events = get_upcoming_events(hours=2) or []
    except Exception:
        events = []

    htf = compute_htf_alignment(snap)
    regime = classify_regime(snap, upcoming_events=events)
    ev = compute_directional_evidence(
        snap, htf_alignment=htf, regime=regime,
        upcoming_events=events, macro_context=None,
    )
    return APIResponse(data=ev.to_dict(), source="diagnostics")


@router.get(
    "/htf-alignment",
    response_model=APIResponse[dict],
    summary="Weighted HTF alignment score (Phase 4) — D1/H4/H1/M15/M5",
)
@limiter.limit("30/minute")
def htf_alignment(
    request: Request,
    force: bool = Query(default=False),
    db: Session = Depends(get_db),
) -> APIResponse[dict]:
    """
    Weighted per-timeframe direction score. Replaces STRONG-only unanimity
    (the current C1 logic) with:

      D1  20% weight — broader context
      H4  30% weight — structural bias
      H1  30% weight — active directional control
      M15 15% weight — transition/displacement
      M5   5% weight — execution refinement

    Score ∈ [-100, +100]. `direction` ∈ BULL/BEAR/NEUTRAL based on ±15 band.
    `strength` ∈ STRONG (≥60) / MEDIUM (≥30) / WEAK (≥15) / NONE.
    Also returns per-TF breakdown, unanimity flag, and TF grouping.

    Fails open — missing/short TFs contribute 0 and warnings[] is filled.
    """
    from services.canonical_market_data import get_canonical
    from services.htf_weighted_alignment import compute_htf_alignment
    from config import settings
    cmd = get_canonical(cache_ttl_s=settings.xauusd_canonical_data_cache_ttl_s)
    snap = cmd.snapshot(db, force_refresh=force)
    result = compute_htf_alignment(snap)
    return APIResponse(data=result.to_dict(), source="diagnostics")


@router.get(
    "/market-regime",
    response_model=APIResponse[dict],
    summary="Market regime classification (Phase 3) — 14-way, direction-first",
)
@limiter.limit("30/minute")
def market_regime(
    request: Request,
    force: bool = Query(default=False),
    db: Session = Depends(get_db),
) -> APIResponse[dict]:
    """
    Returns the current market regime assessment. Runs BEFORE any entry
    strategy and does NOT depend on a compliant trade setup. Classification
    is one of:

      STRONG_BULLISH_EXPANSION | BULLISH_CONTINUATION | BULLISH_PULLBACK |
      BULLISH_TRANSITION | BULLISH_ACCUMULATION |
      BALANCED_RANGE |
      BEARISH_ACCUMULATION | BEARISH_TRANSITION | BEARISH_PULLBACK |
      BEARISH_CONTINUATION | STRONG_BEARISH_EXPANSION |
      EXHAUSTION_OVEREXTENSION | HIGH_IMPACT_EVENT_RISK | INSUFFICIENT_DATA

    Also returns: directional_bias, controller, control_trend, transitioning,
    accepting_above/below, move_maturity, liquidity pools, invalidation_price,
    confidence (0-100), evidence[], warnings[].

    Fails open — snapshot missing bars → returns INSUFFICIENT_DATA, never raises.
    """
    from services.canonical_market_data import get_canonical
    from services.market_regime import classify_regime
    from config import settings
    cmd = get_canonical(cache_ttl_s=settings.xauusd_canonical_data_cache_ttl_s)
    snap = cmd.snapshot(db, force_refresh=force)
    # Pull upcoming events from calendar (best-effort — pass empty list if unavailable)
    try:
        from services.calendar_provider import get_upcoming_events
        events = get_upcoming_events(hours=1) or []
    except Exception:
        events = []
    assessment = classify_regime(snap, upcoming_events=events)
    return APIResponse(data=assessment.to_dict(), source="diagnostics")


@router.get(
    "/canonical-market-data",
    response_model=APIResponse[dict],
    summary="Canonical market-data snapshot (Phase 2) — single source of truth per tick",
)
@limiter.limit("30/minute")
def canonical_market_data(
    request: Request,
    instrument: str = Query(default="XAU/USD"),
    timeframes: str = Query(default="M5,M15,H1,H4,D1"),
    force: bool = Query(default=False),
    db: Session = Depends(get_db),
) -> APIResponse[dict]:
    """
    Returns the current canonical snapshot. This is the single-source-of-truth
    payload every strategy should read from (Phase 2 of the directional
    intelligence overhaul). Snapshot includes:

      - bid/ask/spread/tick timestamp + source
      - per-timeframe candles + freshness status + age
      - session/killzone identity + intraday hi/lo so far
      - PDH/PDL/PDC/PWH/PWL/PWO/daily-open/asian-high/asian-low
      - data_quality_score (0-100)
      - build_latency_ms + warnings[]

    Cache TTL: settings.xauusd_canonical_data_cache_ttl_s (default 15 s).
    Pass ?force=true to bypass the cache.
    """
    from services.canonical_market_data import get_canonical
    from config import settings
    cmd = get_canonical(cache_ttl_s=settings.xauusd_canonical_data_cache_ttl_s)
    tfs = tuple(t.strip().upper() for t in timeframes.split(",") if t.strip())
    snap = cmd.snapshot(db, instrument=instrument, timeframes=tfs, force_refresh=force)
    return APIResponse(data=snap.to_dict(), source="diagnostics")


@router.get(
    "/external-confluence",
    response_model=APIResponse[dict],
    summary="FastBull + CME confluence snapshot (P134)",
)
@limiter.limit("30/minute")
def external_confluence(request: Request,
                          engine_direction: str = Query(default="STAND_ASIDE"),
                          spot_price: float = Query(default=None),
                          force: bool = Query(default=False),
                          db: Session = Depends(get_db)) -> APIResponse[dict]:
    """
    Returns the current external-confluence verdict.
    Pass ?engine_direction=BUY|SELL to see how the confluence
    would score for that direction. ?force=true bypasses the cache.
    """
    from services.external_confluence import get_external_confluence
    return APIResponse(
        data=get_external_confluence(
            db=db,
            engine_direction=engine_direction,
            spot_price=spot_price,
            engine_levels=[],
            force_refresh=force,
        ),
        source="diagnostics",
    )


@router.get(
    "/vp-trap-measurement",
    response_model=APIResponse[dict],
    summary="30-day VP Trap protocol stats — the 4 numbers that decide the niche (P135)",
)
@limiter.limit("30/minute")
def vp_trap_measurement(request: Request,
                          days: int = Query(default=30, ge=1, le=90),
                          db: Session = Depends(get_db)) -> APIResponse[dict]:
    """
    Returns signals/day, win rate, avg R, drawdown + per-session breakdown +
    verdict against the protocol targets (ON TARGET | BELOW TARGET |
    INSUFFICIENT SAMPLE | NO DATA).
    """
    from services.vp_trap_measurement import compute_stats
    return APIResponse(data=compute_stats(db, days=days), source="diagnostics")


@router.get(
    "/four-hour-manipulation",
    response_model=APIResponse[dict],
    summary="Prev-4H sweep + M15 reclaim (trap filter) — current snapshot (P136)",
)
@limiter.limit("30/minute")
def four_hour_manipulation(request: Request,
                             db: Session = Depends(get_db)) -> APIResponse[dict]:
    from services.four_hour_manipulation import detect_4h_manipulation
    from data.candles import get_candles
    try:
        h4  = get_candles(interval="H4",  limit=5,   pair="xauusd").candles or []
        m15 = get_candles(interval="M15", limit=20,  pair="xauusd").candles or []
    except Exception as exc:
        return APIResponse(data={"error": f"candle fetch failed: {exc}"},
                            source="diagnostics")
    return APIResponse(
        data=detect_4h_manipulation(
            h4_candles=h4,
            candles_m15=m15,
            candles_m5=None,
            atr_h1=0.0,
        ),
        source="diagnostics",
    )


@router.get(
    "/shadow-trade-stats",
    response_model=APIResponse[dict],
    summary="Shadow trade simulator — outcome stats bucketed by grade × archetype × regime × session",
)
@limiter.limit("30/minute")
def shadow_trade_stats(
    request: Request,
    days: int = Query(default=30, ge=1, le=180),
    db: Session = Depends(get_db),
) -> APIResponse[dict]:
    """
    Reports resolved shadow trades over the last `days` days:
      - overall count + mean R (nominal + spread-adjusted)
      - per-bucket (grade, archetype, regime, session) stats
      - hit rate + sample size + meets-min-sample flag (>=20 for calibration)

    Data source for grader recalibration — was A+ actually better than A?
    Did suppressed B/C setups outperform? Feed into config lookup once buckets
    reach 20+ samples.
    """
    from services.shadow_trade_simulator import compute_bucket_stats
    return APIResponse(
        data=compute_bucket_stats(db, days=days),
        source="diagnostics",
    )


@router.get(
    "/shadow-trade-recent",
    response_model=APIResponse[dict],
    summary="Recent shadow trades (all grades, PENDING/TRIGGERED/CLOSED)",
)
@limiter.limit("30/minute")
def shadow_trade_recent(
    request: Request,
    limit: int = Query(default=50, ge=1, le=500),
    status: str | None = Query(default=None,
        description="Filter: PENDING | TRIGGERED | TP1_HIT | TP2_HIT | STOPPED | INVALIDATED | EXPIRED"),
    grade: str | None = Query(default=None,
        description="Filter: A+ | A | B | C | UNGRADED"),
    db: Session = Depends(get_db),
) -> APIResponse[dict]:
    """Recent shadow-trade rows for quick inspection of what the simulator is capturing."""
    from sqlalchemy import text
    where = ["instrument = :inst"]
    params: dict = {"inst": "XAU/USD", "lim": limit}
    if status:
        where.append("status = :st")
        params["st"] = status
    if grade:
        where.append("grade = :gr")
        params["gr"] = grade
    sql = (
        "SELECT id, created_at, fingerprint, grade, direction, "
        "session_at_entry, regime_at_entry, archetype, "
        "entry_price, stop_loss, tp1_price, tp2_price, "
        "tp1_rr, tp2_rr, composite_score, setup_score, conditions_passed, "
        "status, triggered_at, closed_at, closed_price, "
        "r_realized, r_spread_adjusted, mfe_pts, mae_pts, duration_min, "
        "est_spread_pts, est_slippage_pts "
        "FROM shadow_trades WHERE " + " AND ".join(where) +
        " ORDER BY created_at DESC LIMIT :lim"
    )
    try:
        rows = db.execute(text(sql), params).fetchall()
    except Exception as exc:
        return APIResponse(data={"error": str(exc), "rows": []}, source="diagnostics")

    cols = ["id", "created_at", "fingerprint", "grade", "direction",
            "session_at_entry", "regime_at_entry", "archetype",
            "entry_price", "stop_loss", "tp1_price", "tp2_price",
            "tp1_rr", "tp2_rr", "composite_score", "setup_score",
            "conditions_passed", "status", "triggered_at", "closed_at",
            "closed_price", "r_realized", "r_spread_adjusted", "mfe_pts",
            "mae_pts", "duration_min", "est_spread_pts", "est_slippage_pts"]
    out = [dict(zip(cols, r)) for r in rows]
    return APIResponse(
        data={"count": len(out), "rows": out},
        source="diagnostics",
    )


@router.get(
    "/predator-candidates",
    response_model=APIResponse[dict],
    summary="Live Predator engine — current signal candidates from empirical edges",
)
@limiter.limit("30/minute")
def predator_candidates(
    request: Request,
    db: Session = Depends(get_db),
) -> APIResponse[dict]:
    """
    Runs the Predator engine on the freshest data and returns any signals
    it would emit right now. Read-only inspection — does not record.
    Used to preview alerts before enabling predator_telegram_enabled.
    """
    from services.predator_engine import evaluate, format_telegram_alert
    from config import settings
    sigs = evaluate(db)
    return APIResponse(
        data={
            "count":               len(sigs),
            "signals":             [s.to_dict() for s in sigs],
            "previews":            [format_telegram_alert(s) for s in sigs],
            "telegram_enabled":    getattr(settings, "predator_telegram_enabled", False),
            "predator_enabled":    getattr(settings, "predator_enabled", True),
        },
        source="diagnostics",
    )


@router.get(
    "/provider-health",
    response_model=APIResponse[dict],
    summary="Per-timeframe candle provider health + freshness + signal-blocked state",
)
@limiter.limit("30/minute")
def provider_health(
    request: Request,
    db: Session = Depends(get_db),
) -> APIResponse[dict]:
    """
    Comprehensive provider-health report per brief section 7:
      - active candle provider per timeframe (source of latest bar)
      - latest candle timestamp per timeframe
      - candle age per timeframe (minutes)
      - status vs freshness threshold (fresh | degraded | stale | missing)
      - last provider error text
      - flags: TV in use, TD failure, synthetic used, MT5 tick available
      - current bid/ask/spread
      - data-quality score (0-100)
      - signals-allowed vs blocked-due-to-data
    """
    from datetime import datetime as _dt, timezone as _tz, timedelta as _td
    from sqlalchemy import text
    from services.data_freshness import STALENESS_MIN_BY_TF, data_quality_score
    from services.candle_ingestion import get_last_ingest_error
    from config import settings

    now = _dt.now(_tz.utc)
    now_naive = now.replace(tzinfo=None)

    # Per-timeframe latest bar + source
    tfs = ["M5", "M15", "H1", "H4", "D1"]
    per_tf: dict[str, dict] = {}
    tv_in_use = False
    synthetic_used = False
    mt5_bars_present = False
    yahoo_used = False
    td_used = False
    latest_by_source: dict[str, str] = {}

    for tf in tfs:
        row = db.execute(text(
            "SELECT candle_time, source FROM historical_candles "
            "WHERE instrument=:i AND timeframe=:tf "
            "ORDER BY candle_time DESC LIMIT 1"
        ), {"i": "XAU/USD", "tf": tf}).fetchone()
        threshold = STALENESS_MIN_BY_TF.get(tf, 60)
        if not row or not row[0]:
            per_tf[tf] = {
                "provider":       None,
                "latest":         None,
                "age_min":        None,
                "threshold_min":  threshold,
                "status":         "missing",
            }
            continue
        latest = row[0]
        source = row[1]

        # SQLite may return candle_time as str; normalise to naive UTC datetime
        if isinstance(latest, str):
            for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S"):
                try:
                    latest = _dt.strptime(latest.split("+")[0], fmt); break
                except ValueError:
                    continue
        if hasattr(latest, "tzinfo") and latest.tzinfo is not None:
            latest = latest.astimezone(_tz.utc).replace(tzinfo=None)

        try:
            age_min = (now_naive - latest).total_seconds() / 60
        except Exception:
            age_min = None

        if age_min is None:
            status = "missing"
        elif age_min <= threshold:
            status = "fresh"
        elif age_min <= 3 * threshold:
            status = "degraded"
        else:
            status = "stale"

        per_tf[tf] = {
            "provider":       source,
            "latest":         latest.isoformat() if hasattr(latest, "isoformat") else str(latest),
            "age_min":        round(age_min, 1) if age_min is not None else None,
            "threshold_min":  threshold,
            "status":         status,
        }
        # Track which providers are actively serving fresh/degraded bars
        latest_by_source[source or "unknown"] = per_tf[tf]["latest"]
        if source == "tradingview" and status in ("fresh", "degraded"):
            tv_in_use = True
        if source == "mt5" and status in ("fresh", "degraded"):
            mt5_bars_present = True
        if source == "yahoo" and status in ("fresh", "degraded"):
            yahoo_used = True
        if source == "twelvedata" and status in ("fresh", "degraded"):
            td_used = True
        if source == "synthetic" and status in ("fresh", "degraded"):
            synthetic_used = True

    quality = data_quality_score(per_tf)

    # MT5 tick availability (from bridge heartbeat) + current bid/ask/spread
    tick_available = False
    tick_data: Optional[dict] = None
    try:
        from routers.bridge import _MT5_TERMINAL_STATE
        if _MT5_TERMINAL_STATE:
            latest_hb = max(_MT5_TERMINAL_STATE.values(),
                              key=lambda s: s.get("last_seen") or _dt.min.replace(tzinfo=_tz.utc))
            last_seen = latest_hb.get("last_seen")
            if last_seen and (now - last_seen).total_seconds() < 120:
                tick_available = True
                tick_data = {
                    "account_login":     latest_hb.get("account_login"),
                    "account_server":    latest_hb.get("account_server"),
                    "account_demo":      latest_hb.get("account_demo"),
                    "connected":         latest_hb.get("connected"),
                    "trade_allowed":     latest_hb.get("trade_allowed"),
                    "last_seen":         last_seen.isoformat(),
                    "seconds_ago":       round((now - last_seen).total_seconds(), 1),
                }
    except Exception:
        pass

    # Try to fetch current bid/ask/spread from mt5_provider (in-container mt5
    # module won't work on Linux — this returns nothing on droplet, but the
    # heartbeat gives us the bridge-side snapshot)
    bid_ask_spread: Optional[dict] = None
    try:
        from services.mt5_provider import get_tick
        bid_ask_spread = get_tick("xauusd")
    except Exception:
        pass  # expected on droplet (no local MT5 install)

    last_err = get_last_ingest_error() or {}

    # Decide whether signals are allowed based on primary-TF freshness
    stale_primary = [tf for tf in ("M5", "M15") if per_tf[tf]["status"] in ("stale", "missing")]
    signals_allowed = not stale_primary and quality >= 60 and not synthetic_used

    return APIResponse(
        data={
            "now":                  now.isoformat(),
            "per_timeframe":        per_tf,
            "data_quality_score":   quality,
            "signals_allowed":      signals_allowed,
            "blocked_reason":       (
                f"stale primary TFs: {stale_primary}"          if stale_primary
                else "synthetic candles present"                if synthetic_used
                else f"quality score {quality}<60"              if quality < 60
                else None
            ),
            "flags": {
                "tradingview_in_use":    tv_in_use,
                "twelvedata_in_use":     td_used,
                "yahoo_in_use":          yahoo_used,
                "mt5_bars_present":      mt5_bars_present,
                "synthetic_used":        synthetic_used,
                "mt5_tick_available":    tick_available,
                "tradingview_enabled_setting": getattr(settings, "tradingview_enabled", None),
            },
            "mt5_tick":             tick_data,
            "current_bid_ask":      bid_ask_spread,
            "last_ingest_error":    last_err,
            "provider_priority": [
                "mt5 (via daemon push)",
                "tradingview (OANDA:XAUUSD)",
                "yahoo (GC=F futures)",
            ],
        },
        source="diagnostics",
    )


# ─────────────────────────────────────────────────────────────────────────────
# Predator notification gateway diagnostics (P233)
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/predator-notifications")
def diag_predator_notifications(
    hours: int = Query(24, ge=1, le=168),
    db=Depends(get_db),
):
    """
    Gateway-driven notification-quality metrics.

    Fields:
      - internal_candidates_detected: total signals evaluated in window
      - setup_ids_created: unique setups touched
      - average_observations_per_setup: how many M5 evaluations per hypothesis
      - telegram_messages_sent: actual Telegram sends by gateway
      - telegram_messages_would_send_shadow: shadow-mode "would send" counter
      - telegram_messages_suppressed: total (either mode)
      - telegram_suppression_rate: suppressed / all decisions
      - candidate_to_actionable_ratio: how noisy the pre-refactor world was
      - actionable_signals_suppressed: legit gate failures (rr/stale/overext)
      - suppression_reason_breakdown: {reason: count}
      - decision_breakdown: {SENT/SUPPRESSED/WOULD_SEND/WOULD_SUPPRESS: count}
      - msg_type_breakdown: {ACTIONABLE/INVALIDATION/...: count}
    """
    from services.predator_notification_gateway import notification_metrics
    metrics = notification_metrics(db, hours=hours)
    from config import settings as _cfg
    metrics["current_architecture_mode"] = getattr(
        _cfg, "predator_notification_architecture", "unknown"
    )
    metrics["current_notification_mode"] = getattr(
        _cfg, "predator_notification_mode", "unknown"
    )
    metrics["setup_price_bucket_pts"] = getattr(
        _cfg, "predator_setup_price_bucket", None
    )
    metrics["min_rr_gate"] = getattr(
        _cfg, "predator_notification_min_rr", None
    )
    metrics["max_bar_age_min"] = getattr(
        _cfg, "predator_notification_max_bar_age_min", None
    )
    return APIResponse(data=metrics, source="predator_notification_gateway")
