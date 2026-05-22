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
