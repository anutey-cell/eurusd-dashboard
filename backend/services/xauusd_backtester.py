"""
Strict XAU/USD backtester — diagnostic and validation tool.

Designed to answer one question: does the signal engine produce positive
expectancy on historical XAU/USD data under realistic execution costs?

Hard rules (all enforced — none can be silently bypassed)
---------------------------------------------------------
1. Look-ahead bias is impossible: at bar i, the engine sees candles[:i+1] only.
   Future bars (i+1 onward) are used solely to resolve TP/SL on already-open
   trades — they cannot influence the entry decision.

2. Only SIGNAL_READY-equivalent setups count as trades:
     signal == BUY|SELL  AND
     score  >= min_score  AND
     RR     >= min_rr  (after spread + slippage)  AND
     stop_loss is defined  AND
     take_profit is defined  AND
     news    == CLEAR  AND
     entry candle is not stale

3. Conservative collision rule: if a single candle touches both TP and SL,
   count it as a LOSS. The intrabar order is unknown — pessimism is mandatory.

4. One trade at a time by default. While a trade is open, no new entries
   are taken until it resolves (TP, SL, or expiry). Override with
   BACKTEST_ALLOW_OVERLAP=true (NOT recommended for diagnostics).

5. No fake profitability. Every skipped signal is logged with a reason.
   Every metric is computed from the actual trade list — no curve-fitting.

This module DOES NOT place trades, send Telegram alerts, or write to MT5.
It is read-only against historical data.
"""
from __future__ import annotations

import logging
import os
import random
import statistics
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from sqlalchemy.orm import Session

from pair_config import get_pair_config
from services.signal_engine import (
    analyze_signal,
    detect_session,
    Candle as EngineCandle,
)
from services.historical_data_provider import (
    get_historical_candles, load_historical_macro_events, seed_historical_data,
)

log = logging.getLogger(__name__)

# ═══════════════════════════════════════════════════════════════════════════════
# Configuration
# ═══════════════════════════════════════════════════════════════════════════════

MIN_WARMUP_BARS    = 60      # need >= 50 for EMA-50 + buffer
DEFAULT_HOLDING    = 96      # 96 × 15min = 24 hours

ALLOW_OVERLAP = os.getenv("BACKTEST_ALLOW_OVERLAP", "false").lower() in ("1", "true", "yes")


# ═══════════════════════════════════════════════════════════════════════════════
# Data classes
# ═══════════════════════════════════════════════════════════════════════════════

@dataclass
class Trade:
    id:             int
    entry_time:     str
    exit_time:      str
    signal:         str           # BUY | SELL
    entry:          float         # signal entry price
    adjusted_entry: float         # after spread + slippage
    stop_loss:      float
    take_profit:    float
    risk_points:    float
    target_points:  float
    rr:             float
    result:         str           # WIN | LOSS | BREAKEVEN | EXPIRED
    points:         float         # +/- points captured
    r_multiple:     float         # +reward/risk on win, -1 on loss
    equity_before:  float
    equity_after:   float
    risk_amount:    float
    pnl_amount:     float
    session:        str
    score:          int
    reason:         str
    bars_held:      int
    market_state:   str = ""
    setup_type:     str = "unknown"   # structured classification from engine
    blockers:       list[str] = field(default_factory=list)
    costs:          dict = field(default_factory=dict)


@dataclass
class SkippedTrade:
    time:    str
    signal:  str
    score:   int
    reason:  str          # e.g. RR_AFTER_COST_TOO_LOW
    detail:  str = ""
    # Optional fields for hypothetical-outcome simulation
    bar_idx:     int   = -1
    entry:       float = 0.0
    stop_loss:   float = 0.0
    take_profit: float = 0.0
    risk_points: float = 0.0
    target_points: float = 0.0
    hypothetical_result: str = ""    # WIN | LOSS | EXPIRED (filled when analyze_skipped=true)
    hypothetical_r:      float = 0.0


# ═══════════════════════════════════════════════════════════════════════════════
# Main entry point
# ═══════════════════════════════════════════════════════════════════════════════

def run_xauusd_backtest(
    db:                   Session,
    start_date:           datetime | None = None,
    end_date:             datetime | None = None,
    timeframe:            str             = "M15",
    lookback:             int             = 5000,
    initial_balance:      float           = 10_000.0,
    risk_percent:         float           = 0.25,
    spread_points:        float           = 1.5,
    slippage_points:      float           = 0.5,
    commission_per_trade: float           = 0.0,
    include_news_filter:  bool            = True,
    session_filter:       Optional[str]   = None,
    min_score:            int             = 80,
    min_rr:               float           = 2.5,
    max_trades:           Optional[int]   = None,
    max_holding_candles:  int             = DEFAULT_HOLDING,
    allow_synthetic_fallback: bool        = True,
    allow_overlap:        Optional[bool]  = None,
    auto_seed:            bool            = True,
    enable_premium_gates: bool            = True,
    walk_forward_segments: int            = 4,
    monte_carlo_runs:      int            = 500,
    classify_regimes:      bool           = True,
    analyze_skipped:       bool           = False,
    risk_sensitivity:      bool           = True,
    engine_variant:        str            = "swing",        # "swing" | "intraday"
    anti_cluster_hours:    float          = 0.0,            # 0 = disabled
    target_pips_override:  Optional[int]  = None,           # override pair config target
) -> dict:
    """
    Run the strict XAU/USD backtest.

    Returns a dict containing settings, summary metrics, equity curve,
    trade list, skipped list, breakdowns, and reliability rating.
    """
    # ── Validate inputs ─────────────────────────────────────────────────────
    if risk_percent <= 0 or risk_percent > 5:
        raise ValueError("risk_percent must be in (0, 5]")
    if initial_balance <= 0:
        raise ValueError("initial_balance must be positive")
    if min_score < 50 or min_score > 100:
        raise ValueError("min_score must be in [50, 100]")
    if min_rr < 1.0 or min_rr > 10:
        raise ValueError("min_rr must be in [1.0, 10]")
    if spread_points < 0 or slippage_points < 0:
        raise ValueError("costs cannot be negative")

    # ── Load pair config ────────────────────────────────────────────────────
    cfg              = get_pair_config("xauusd")
    pip_size         = cfg["pip_size"]            # 1.0 for XAU/USD
    target_points    = target_pips_override if target_pips_override else cfg["target_pips"]
    sl_buffer_points = cfg["sl_buffer_pips"]      # 5 for XAU/USD
    fvg_min_points   = cfg["fvg_min_pips"]        # 5 for XAU/USD
    price_decimals   = cfg["price_decimals"]      # 2 for XAU/USD

    # ── Resolve overlap setting ──────────────────────────────────────────────
    overlap_enabled = ALLOW_OVERLAP if allow_overlap is None else bool(allow_overlap)

    # ── Load historical candles ─────────────────────────────────────────────
    candles, source = get_historical_candles(
        db=db,
        timeframe=timeframe,
        start_date=start_date,
        end_date=end_date,
        lookback=lookback,
        allow_synthetic_fallback=False,    # try DB first; we control fallback
    )

    # Auto-seed a year of historical data on first backtest if DB is empty
    if (not candles or len(candles) < MIN_WARMUP_BARS) and auto_seed:
        log.info("[backtest] Auto-seeding 365 days of historical data for %s", timeframe)
        seed_result = seed_historical_data(
            db=db, days=365, timeframe=timeframe, instrument="XAU/USD", seed=42,
        )
        log.info(
            "[backtest] Seed complete: %d candles, %d macro events",
            seed_result.get("candlesInserted", 0),
            seed_result.get("eventsInserted", 0),
        )
        # Reload
        candles, source = get_historical_candles(
            db=db, timeframe=timeframe,
            start_date=start_date, end_date=end_date, lookback=lookback,
            allow_synthetic_fallback=False,
        )

    # Synthetic fallback only if explicitly allowed and DB still empty
    if (not candles or len(candles) < MIN_WARMUP_BARS) and allow_synthetic_fallback:
        candles, source = get_historical_candles(
            db=db, timeframe=timeframe,
            start_date=start_date, end_date=end_date, lookback=lookback,
            allow_synthetic_fallback=True,
        )

    if not candles or len(candles) < MIN_WARMUP_BARS:
        raise RuntimeError(
            f"Historical XAU/USD candle data unavailable "
            f"(got {len(candles)} bars, need >= {MIN_WARMUP_BARS}). "
            f"Import candles via POST /api/v1/backtest/import-csv or call "
            f"POST /api/v1/backtest/seed-historical."
        )

    # ── Load historical macro events for the candle range ────────────────────
    macro_events: list[dict] = []
    if include_news_filter and candles:
        first_t = candles[0].time
        last_t  = candles[-1].time
        if not first_t.tzinfo:
            first_t = first_t.replace(tzinfo=timezone.utc)
        if not last_t.tzinfo:
            last_t = last_t.replace(tzinfo=timezone.utc)
        macro_events = load_historical_macro_events(db=db, start=first_t, end=last_t)
        log.info("[backtest] Loaded %d historical macro events for the period",
                 len(macro_events))

    log.info(
        "[backtest] Starting XAU/USD backtest bars=%d source=%s tf=%s "
        "balance=%.2f risk=%.2f%% spread=%.1f slippage=%.1f min_score=%d min_rr=%.2f",
        len(candles), source, timeframe, initial_balance, risk_percent,
        spread_points, slippage_points, min_score, min_rr,
    )

    # Convert to engine candles (engine functions duck-type but be explicit)
    bars = [_to_engine_candle(c) for c in candles]

    # ── Pre-flight data quality audit (Phase 2b) ─────────────────────────────
    data_quality_report = _audit_data_quality(bars, timeframe, source)

    # If the user requested a date range wider than available data, surface that
    if start_date and bars:
        first_t = bars[0].time
        if first_t.tzinfo is None: first_t = first_t.replace(tzinfo=timezone.utc)
        sd_aware = start_date if start_date.tzinfo else start_date.replace(tzinfo=timezone.utc)
        if first_t > sd_aware + timedelta(days=7):
            data_quality_report.setdefault("warnings", []).insert(
                0,
                f"Requested start {sd_aware.date()} but data only goes back to "
                f"{first_t.date()} — date range clipped to available history. "
                f"Use POST /backtest/fetch-tradingview to import more.",
            )
            data_quality_report["requestedStart"] = sd_aware.isoformat()
            data_quality_report["actualStart"]    = first_t.isoformat()

    if data_quality_report["status"] == "FAIL":
        log.warning("[backtest] Data quality FAIL — proceeding with reliability caveat")

    # ── Walk-forward loop ───────────────────────────────────────────────────
    trades:  list[Trade]        = []
    skipped: list[SkippedTrade] = []
    # Anti-clustering tracker: last entry time per (signal direction)
    last_entry_per_direction: dict[str, datetime] = {}
    equity_curve: list[dict]    = [{
        "step":      0,
        "time":      bars[MIN_WARMUP_BARS - 1].time.isoformat(),
        "equity":    round(initial_balance, 2),
        "drawdownPct": 0.0,
        "trade":     None,
    }]
    equity     = initial_balance
    peak_eq    = initial_balance
    next_entry = MIN_WARMUP_BARS
    signals_scanned = 0

    for i in range(MIN_WARMUP_BARS, len(bars)):
        # ── Look-ahead boundary ──────────────────────────────────────────────
        # The engine receives bars[:i+1] only. It MUST NOT see bars[i+1:].
        # That guarantee is what makes this backtest valid.
        window = bars[: i + 1]
        candle_ts = bars[i].time

        if i < next_entry and not overlap_enabled:
            # An earlier trade is still open — skip scanning while it resolves
            continue

        # ── Run signal engine on the historical window ──────────────────────
        # NOTE: macro_events are pre-loaded from the historical macro_events table
        # and filtered by check_news_risk() at each candle timestamp.
        try:
            if engine_variant == "intraday":
                # Intraday ICT engine (M15-tuned + Asian range break-retest)
                from services.intraday_engine import analyze_intraday, INTRADAY_DEFAULTS
                result = analyze_intraday(
                    pair="xauusd",
                    candles=window,
                    macro_events=macro_events,
                    at=candle_ts,
                    pip_size=pip_size,
                    target_pips=target_points,
                    sl_buffer_pips=INTRADAY_DEFAULTS["sl_buffer_pips"],
                    min_rr=min_rr if min_rr != 2.5 else INTRADAY_DEFAULTS["min_rr"],
                    fvg_min_pips=INTRADAY_DEFAULTS["fvg_min_pips"],
                    strong_wick_pips=INTRADAY_DEFAULTS["strong_wick_pips"],
                    atr_min=INTRADAY_DEFAULTS["atr_min"],
                    atr_max=INTRADAY_DEFAULTS["atr_max"],
                    min_score=min_score if min_score != 80 else INTRADAY_DEFAULTS["min_score"],
                    enable_killzone=True,
                    enable_asian_range=True,
                    enable_news_filter=include_news_filter,
                )
            elif engine_variant in ("trend_pullback", "bb_reversion",
                                     "opening_range", "asian_fade"):
                # Non-ICT intraday strategies (Option C)
                from services.intraday_strategies import analyze_non_ict_strategy
                result = analyze_non_ict_strategy(
                    variant=engine_variant,
                    candles=window,
                    at=candle_ts,
                    macro_events=macro_events,
                    pip_size=pip_size,
                    target_pips=target_points,
                    min_rr=min_rr,
                    enable_news_filter=include_news_filter,
                )
            else:
                result = analyze_signal(
                    pair="xauusd",
                    candles=window,
                    macro_events=macro_events,
                    at=candle_ts,
                    pip_size=pip_size,
                    target_pips=target_points,
                    sl_buffer_pips=sl_buffer_points,
                    min_rr=min_rr,
                    fvg_min_pips=fvg_min_points,
                    enable_premium_gates=enable_premium_gates,
                    historical_mode=True,         # skip live DXY fetch
                )
        except Exception as e:
            log.debug("[backtest] Engine error at bar %d: %s", i, e)
            continue

        signals_scanned += 1

        # ── Gate 1: signal must be BUY/SELL ──────────────────────────────────
        if result.signal not in ("BUY", "SELL"):
            continue   # WAIT / NEUTRAL — not counted, not skipped

        # ── Gate 2: score >= min_score ───────────────────────────────────────
        if result.quality_score < min_score:
            skipped.append(SkippedTrade(
                time=candle_ts.isoformat(), signal=result.signal,
                score=result.quality_score, reason="SCORE_TOO_LOW",
                detail=f"{result.quality_score} < {min_score}",
            ))
            continue

        # ── Gate 3: news must be CLEAR (when filter enabled) ─────────────────
        if include_news_filter and result.news_status != "CLEAR":
            skipped.append(SkippedTrade(
                time=candle_ts.isoformat(), signal=result.signal,
                score=result.quality_score, reason="NEWS_BLOCKED",
                detail=result.news_status,
            ))
            continue

        # ── Gate 4: SL/TP must exist ─────────────────────────────────────────
        if result.stop_loss is None or result.take_profit is None:
            skipped.append(SkippedTrade(
                time=candle_ts.isoformat(), signal=result.signal,
                score=result.quality_score, reason="MISSING_SL_TP",
            ))
            continue

        # ── Gate 5: session filter (optional) ────────────────────────────────
        sess = detect_session(at=candle_ts)
        if session_filter and sess.session != session_filter:
            skipped.append(SkippedTrade(
                time=candle_ts.isoformat(), signal=result.signal,
                score=result.quality_score, reason="SESSION_FILTERED",
                detail=f"want={session_filter} got={sess.session}",
            ))
            continue

        # ── Apply spread + slippage to derive adjusted entry ─────────────────
        signal_entry = float(result.entry) if result.entry is not None else float(bars[i].close)
        if result.signal == "BUY":
            adj_entry = round(signal_entry + spread_points + slippage_points, price_decimals)
            risk_pts  = adj_entry - float(result.stop_loss)
            reward_pts = float(result.take_profit) - adj_entry
        else:  # SELL
            adj_entry = round(signal_entry - spread_points - slippage_points, price_decimals)
            risk_pts  = float(result.stop_loss)  - adj_entry
            reward_pts = adj_entry - float(result.take_profit)

        # ── Gate 6: risk must be positive ────────────────────────────────────
        if risk_pts <= 0 or reward_pts <= 0:
            skipped.append(SkippedTrade(
                time=candle_ts.isoformat(), signal=result.signal,
                score=result.quality_score, reason="INVALID_GEOMETRY",
                detail=f"risk={risk_pts:.2f} reward={reward_pts:.2f}",
            ))
            continue

        # ── Gate 7: RR after costs ───────────────────────────────────────────
        rr_after = round(reward_pts / risk_pts, 3)
        if rr_after < min_rr:
            skipped.append(SkippedTrade(
                time=candle_ts.isoformat(), signal=result.signal,
                score=result.quality_score, reason="RR_AFTER_COST_TOO_LOW",
                detail=f"rr={rr_after:.2f} < {min_rr}",
                bar_idx=i,
                entry=adj_entry,
                stop_loss=float(result.stop_loss),
                take_profit=float(result.take_profit),
                risk_points=risk_pts,
                target_points=reward_pts,
            ))
            continue

        # ── Anti-clustering filter ──────────────────────────────────────────
        # Prevents the engine from firing the same-direction setup multiple
        # times in close succession (e.g. 5 BUY losses in 16h on 2026-01-06).
        if anti_cluster_hours > 0:
            last_t = last_entry_per_direction.get(result.signal)
            if last_t is not None:
                hours_since = (candle_ts - last_t).total_seconds() / 3600
                if hours_since < anti_cluster_hours:
                    skipped.append(SkippedTrade(
                        time=candle_ts.isoformat(), signal=result.signal,
                        score=result.quality_score, reason="ANTI_CLUSTER",
                        detail=f"{hours_since:.1f}h since last {result.signal} < {anti_cluster_hours}h",
                    ))
                    continue

        # ── Trade is valid — simulate forward to find TP or SL ───────────────
        risk_amount = equity * (risk_percent / 100.0)
        exit_time, result_label, points_captured, r_mult, bars_held = _resolve_trade(
            bars=bars,
            entry_idx=i,
            signal=result.signal,
            adj_entry=adj_entry,
            stop_loss=float(result.stop_loss),
            take_profit=float(result.take_profit),
            max_holding=max_holding_candles,
            risk_points=risk_pts,
            reward_points=reward_pts,
        )

        # ── PnL & equity update ──────────────────────────────────────────────
        if result_label == "WIN":
            pnl_amount = risk_amount * r_mult
        elif result_label == "LOSS":
            pnl_amount = -risk_amount
        elif result_label == "BREAKEVEN":
            pnl_amount = 0.0
        else:  # EXPIRED — pro-rated by exit-vs-entry distance
            pnl_amount = risk_amount * r_mult

        # Subtract commission (one-way) — apply per trade
        pnl_amount -= commission_per_trade

        equity_before = equity
        equity_after  = equity + pnl_amount
        equity        = equity_after
        peak_eq       = max(peak_eq, equity_after)
        drawdown_pct  = round((peak_eq - equity_after) / peak_eq * 100, 2) if peak_eq else 0.0

        # Classify market regime at the trade bar (uses window up to i)
        regime = _classify_regime(window, pip_size) if classify_regimes else "UNKNOWN"
        # News-day classification (fine-grained: news_window_30 / pre_news / post_news_*)
        news_class = _classify_news_window_fine(candle_ts, macro_events)

        trade = Trade(
            id=len(trades) + 1,
            entry_time=candle_ts.isoformat(),
            exit_time=exit_time,
            signal=result.signal,
            entry=round(signal_entry, price_decimals),
            adjusted_entry=adj_entry,
            stop_loss=round(float(result.stop_loss), price_decimals),
            take_profit=round(float(result.take_profit), price_decimals),
            risk_points=round(risk_pts, 2),
            target_points=round(reward_pts, 2),
            rr=rr_after,
            result=result_label,
            points=round(points_captured, 2),
            r_multiple=round(r_mult, 3),
            equity_before=round(equity_before, 2),
            equity_after=round(equity_after, 2),
            risk_amount=round(risk_amount, 2),
            pnl_amount=round(pnl_amount, 2),
            session=sess.session,
            score=result.quality_score,
            reason=_short_reason(result.model, result.reason),
            bars_held=bars_held,
            market_state=regime,
            setup_type=getattr(result, "setup_type", None) or result.model.get("setupType", "unknown"),
            blockers=[news_class] if news_class != "normal_day" else [],
            costs={"spreadPoints": spread_points, "slippagePoints": slippage_points,
                   "commission": commission_per_trade},
        )
        trades.append(trade)
        # Update anti-clustering tracker on every accepted entry
        last_entry_per_direction[result.signal] = candle_ts

        equity_curve.append({
            "step":        len(trades),
            "time":        exit_time,
            "equity":      round(equity_after, 2),
            "drawdownPct": drawdown_pct,
            "trade":       trade.id,
            "result":      result_label,
        })

        # Block further entries until this trade closes (or always, if !overlap_enabled)
        if not overlap_enabled:
            next_entry = i + bars_held + 1

        if max_trades and len(trades) >= max_trades:
            log.info("[backtest] max_trades=%d reached — stopping early", max_trades)
            break

    # ── Compute summary ─────────────────────────────────────────────────────
    summary = _compute_summary(
        trades, equity_curve, initial_balance, equity,
        signals_scanned, len(skipped),
    )

    # ── Compute breakdowns ──────────────────────────────────────────────────
    breakdowns = _compute_breakdowns(trades)

    # ── Phase 2a additions ──────────────────────────────────────────────────
    walk_forward = _walk_forward_analysis(trades, segments=walk_forward_segments)
    monte_carlo  = _monte_carlo_simulation(
        trades, runs=monte_carlo_runs, initial_balance=initial_balance,
        risk_percent=risk_percent,
    ) if monte_carlo_runs > 0 else None
    regime_perf = _regime_breakdown(trades) if classify_regimes else []
    news_perf   = _news_day_breakdown(trades)
    setup_perf  = _setup_type_breakdown(trades)
    risk_sens   = _risk_sensitivity_analysis(
        trades, initial_balance=initial_balance,
    ) if risk_sensitivity else []
    # Phase 2b — simulate hypothetical outcomes for skipped trades
    if analyze_skipped:
        _simulate_skipped_outcomes(skipped, bars, max_holding_candles)
    skipped_diag = _skipped_diagnostics(skipped)

    overfitting = _assess_overfitting(
        summary=summary, walk_forward=walk_forward, breakdowns=breakdowns,
        regime_perf=regime_perf, monte_carlo=monte_carlo, trades=trades,
    )

    # ── Quality score (replaces reliability) ────────────────────────────────
    rating = _compute_quality_score(
        summary=summary, trades=trades, walk_forward=walk_forward,
        breakdowns=breakdowns, regime_perf=regime_perf, overfitting=overfitting,
        data_source=source, data_quality=data_quality_report,
    )

    settings_obj = {
        "instrument":          "XAU/USD",
        "timeframe":           timeframe,
        "startDate":           start_date.isoformat() if start_date else None,
        "endDate":             end_date.isoformat()   if end_date   else None,
        "lookback":            lookback,
        "initialBalance":      initial_balance,
        "riskPercent":         risk_percent,
        "spreadPoints":        spread_points,
        "slippagePoints":      slippage_points,
        "commissionPerTrade":  commission_per_trade,
        "minScore":            min_score,
        "minRR":               min_rr,
        "includeNewsFilter":   include_news_filter,
        "sessionFilter":       session_filter,
        "maxHoldingCandles":   max_holding_candles,
        "allowOverlap":        overlap_enabled,
        "dataSource":          source,
        "barsAnalyzed":        len(candles),
        "macroEventsLoaded":   len(macro_events),
        "premiumGatesEnabled": enable_premium_gates,
        "engineVariant":       engine_variant,
        "antiClusterHours":    anti_cluster_hours,
    }

    return {
        "instrument": "XAU/USD",
        "period": {
            "start": bars[MIN_WARMUP_BARS - 1].time.isoformat() if bars else None,
            "end":   bars[-1].time.isoformat() if bars else None,
        },
        "settings":     settings_obj,
        "dataQuality":  data_quality_report,   # Phase 2b
        "summary":      summary,
        "qualityScore": rating,
        "reliability":  rating,    # alias for backwards compat
        "recommendation": rating.get("recommendation"),
        "breakdowns":   breakdowns,
        # Phase 2a
        "walkForward":      walk_forward,
        "monteCarlo":       monte_carlo,
        "regimePerformance": regime_perf,
        "newsPerformance":  news_perf,
        "setupPerformance": setup_perf,
        "riskSensitivity":  risk_sens,
        "overfittingRisk":  overfitting,
        "skippedDiagnostics": skipped_diag,
        # Existing
        "equityCurve":  equity_curve,
        "trades":       [_trade_to_dict(t) for t in trades],
        "skipped":      [_skip_to_dict(s)  for s in skipped],
        "warnings":     _standard_warnings(source),
    }


# ═══════════════════════════════════════════════════════════════════════════════
# Internal helpers
# ═══════════════════════════════════════════════════════════════════════════════

def _to_engine_candle(c) -> EngineCandle:
    """Convert Pydantic / dataclass candles to the engine's dataclass type."""
    if isinstance(c, EngineCandle):
        return c
    return EngineCandle(
        time=getattr(c, "time", datetime.now(timezone.utc)),
        open=float(c.open),
        high=float(c.high),
        low=float(c.low),
        close=float(c.close),
        volume=int(getattr(c, "volume", 0) or 0),
    )


def _resolve_trade(
    bars: list[EngineCandle],
    entry_idx: int,
    signal: str,
    adj_entry: float,
    stop_loss: float,
    take_profit: float,
    max_holding: int,
    risk_points: float,
    reward_points: float,
) -> tuple[str, str, float, float, int]:
    """
    Walk bars forward from entry_idx+1 to find TP or SL.

    Conservative collision: if a single candle touches BOTH TP and SL,
    count it as a LOSS — intrabar order is unknown.

    Returns (exit_time, result_label, points, r_multiple, bars_held).
    """
    end_idx = min(entry_idx + max_holding, len(bars) - 1)

    for j in range(entry_idx + 1, end_idx + 1):
        bar = bars[j]
        if signal == "BUY":
            tp_hit = bar.high >= take_profit
            sl_hit = bar.low  <= stop_loss
        else:  # SELL
            tp_hit = bar.low  <= take_profit
            sl_hit = bar.high >= stop_loss

        if tp_hit and sl_hit:
            # Conservative: SL hit first
            return (
                bar.time.isoformat(),
                "LOSS",
                -risk_points,
                -1.0,
                j - entry_idx,
            )
        if tp_hit:
            return (
                bar.time.isoformat(),
                "WIN",
                reward_points,
                reward_points / risk_points,
                j - entry_idx,
            )
        if sl_hit:
            return (
                bar.time.isoformat(),
                "LOSS",
                -risk_points,
                -1.0,
                j - entry_idx,
            )

    # Expired — close at end-of-window with whatever price implies
    last = bars[end_idx]
    if signal == "BUY":
        exit_price = last.close
        points_captured = exit_price - adj_entry
    else:
        exit_price = last.close
        points_captured = adj_entry - exit_price

    r_mult = points_captured / risk_points if risk_points > 0 else 0.0
    return (
        last.time.isoformat(),
        "EXPIRED",
        points_captured,
        r_mult,
        end_idx - entry_idx,
    )


def _short_reason(model: dict, fallback: str) -> str:
    parts = []
    for key in ("liquidity", "structure", "fvg"):
        txt = model.get(key, "")
        if txt and "No" not in txt and "Waiting" not in txt:
            clean = txt.split("(")[0].split("--")[0].strip().rstrip(";,.")
            if clean:
                parts.append(clean)
    return "; ".join(parts) if parts else (fallback or "ICT setup")


# ═══════════════════════════════════════════════════════════════════════════════
# Summary metrics
# ═══════════════════════════════════════════════════════════════════════════════

def _compute_summary(
    trades:   list[Trade],
    equity_curve: list[dict],
    initial_balance: float,
    final_balance:   float,
    signals_scanned: int,
    skipped_count:   int,
) -> dict:
    n = len(trades)
    wins        = [t for t in trades if t.result == "WIN"]
    losses      = [t for t in trades if t.result == "LOSS"]
    breakevens  = [t for t in trades if t.result == "BREAKEVEN"]
    expired     = [t for t in trades if t.result == "EXPIRED"]

    nw, nl, nb, ne = len(wins), len(losses), len(breakevens), len(expired)

    win_rate  = round(nw / n * 100, 2) if n else 0.0
    loss_rate = round(nl / n * 100, 2) if n else 0.0

    total_win_pts  = sum(t.points for t in wins)
    total_loss_pts = abs(sum(t.points for t in losses))
    avg_win_pts    = round(total_win_pts  / nw, 2) if nw else 0.0
    avg_loss_pts   = round(total_loss_pts / nl, 2) if nl else 0.0
    avg_rr         = round(sum(t.rr      for t in trades) / n, 3) if n else 0.0

    # Expectancy
    wr = win_rate / 100
    lr = loss_rate / 100
    expectancy_points = round(wr * avg_win_pts - lr * avg_loss_pts, 3) if n else 0.0
    expectancy_r      = round(sum(t.r_multiple for t in trades) / n, 3) if n else 0.0

    # Profit factor
    profit_factor = round(total_win_pts / total_loss_pts, 3) if total_loss_pts > 0 else None

    # Drawdown
    max_dd_pct    = max((p["drawdownPct"] for p in equity_curve), default=0.0)
    peak_eq       = max((p["equity"] for p in equity_curve), default=initial_balance)
    max_dd_amount = round(peak_eq - min((p["equity"] for p in equity_curve), default=initial_balance), 2)

    # Consecutive wins / losses
    max_cons_win  = max_consecutive(trades, "WIN")
    max_cons_loss = max_consecutive(trades, "LOSS")

    # Average holding time (bars)
    avg_held = round(sum(t.bars_held for t in trades) / n, 1) if n else 0.0

    # Best / worst trades
    best  = max(trades, key=lambda t: t.points, default=None)
    worst = min(trades, key=lambda t: t.points, default=None)

    net_return_pct = round((final_balance - initial_balance) / initial_balance * 100, 3) if initial_balance else 0.0

    # Buy / Sell stats
    buys   = [t for t in trades if t.signal == "BUY"]
    sells  = [t for t in trades if t.signal == "SELL"]
    buy_wr  = round(sum(1 for t in buys  if t.result == "WIN") / len(buys)  * 100, 2) if buys  else 0.0
    sell_wr = round(sum(1 for t in sells if t.result == "WIN") / len(sells) * 100, 2) if sells else 0.0

    return {
        "totalSignalsScanned":  signals_scanned,
        "validTrades":          n,
        "wins":                 nw,
        "losses":               nl,
        "breakeven":            nb,
        "expired":              ne,
        "skipped":              skipped_count,
        "winRate":              win_rate,
        "lossRate":             loss_rate,
        "averageRR":            avg_rr,
        "averageWinPoints":     avg_win_pts,
        "averageLossPoints":    avg_loss_pts,
        "expectancyPoints":     expectancy_points,
        "expectancyR":          expectancy_r,
        "profitFactor":         profit_factor,
        "maxDrawdownPercent":   max_dd_pct,
        "maxDrawdownAmount":    max_dd_amount,
        "maxConsecutiveWins":   max_cons_win,
        "maxConsecutiveLosses": max_cons_loss,
        "averageBarsHeld":      avg_held,
        "buyWinRate":           buy_wr,
        "sellWinRate":          sell_wr,
        "buyTrades":            len(buys),
        "sellTrades":           len(sells),
        "bestTrade": None if not best else {
            "id": best.id, "points": best.points, "rMultiple": best.r_multiple,
            "signal": best.signal, "entryTime": best.entry_time,
        },
        "worstTrade": None if not worst else {
            "id": worst.id, "points": worst.points, "rMultiple": worst.r_multiple,
            "signal": worst.signal, "entryTime": worst.entry_time,
        },
        "initialBalance":       round(initial_balance, 2),
        "finalBalance":         round(final_balance,   2),
        "netReturnPercent":     net_return_pct,
        "totalPoints":          round(sum(t.points for t in trades), 2),
    }


def max_consecutive(trades: list[Trade], result_label: str) -> int:
    streak = max_streak = 0
    for t in trades:
        if t.result == result_label:
            streak += 1
            max_streak = max(max_streak, streak)
        else:
            streak = 0
    return max_streak


# ═══════════════════════════════════════════════════════════════════════════════
# Breakdowns
# ═══════════════════════════════════════════════════════════════════════════════

def _compute_breakdowns(trades: list[Trade]) -> dict:
    if not trades:
        return {
            "session": [], "direction": [], "scoreBand": [], "result": [],
        }

    # By session
    sess_map: dict[str, dict] = {}
    for t in trades:
        s = t.session or "Off-session"
        d = sess_map.setdefault(s, {"trades": 0, "wins": 0, "points": 0.0, "rTotal": 0.0})
        d["trades"] += 1
        d["points"] += t.points
        d["rTotal"] += t.r_multiple
        if t.result == "WIN":
            d["wins"] += 1
    session_b = [
        {
            "session":      s,
            "trades":       d["trades"],
            "wins":         d["wins"],
            "winRate":      round(d["wins"] / d["trades"] * 100, 2),
            "totalPoints":  round(d["points"], 2),
            "expectancyR":  round(d["rTotal"] / d["trades"], 3),
        }
        for s, d in sess_map.items()
    ]
    session_b.sort(key=lambda x: -x["winRate"])

    # By direction
    dir_b = []
    for direction in ("BUY", "SELL"):
        sub = [t for t in trades if t.signal == direction]
        if not sub:
            continue
        wins = sum(1 for t in sub if t.result == "WIN")
        dir_b.append({
            "direction":   direction,
            "trades":      len(sub),
            "wins":        wins,
            "winRate":     round(wins / len(sub) * 100, 2),
            "totalPoints": round(sum(t.points for t in sub), 2),
            "expectancyR": round(sum(t.r_multiple for t in sub) / len(sub), 3),
        })

    # By score band
    bands = [(80, 84), (85, 89), (90, 94), (95, 100)]
    score_b = []
    for lo, hi in bands:
        sub = [t for t in trades if lo <= t.score <= hi]
        if not sub:
            continue
        wins = sum(1 for t in sub if t.result == "WIN")
        score_b.append({
            "band":        f"{lo}-{hi}",
            "trades":      len(sub),
            "wins":        wins,
            "winRate":     round(wins / len(sub) * 100, 2),
            "expectancyR": round(sum(t.r_multiple for t in sub) / len(sub), 3),
        })

    # By result label
    result_b = []
    for label in ("WIN", "LOSS", "BREAKEVEN", "EXPIRED"):
        n = sum(1 for t in trades if t.result == label)
        result_b.append({"result": label, "count": n,
                         "pct": round(n / len(trades) * 100, 2)})

    return {
        "session":   session_b,
        "direction": dir_b,
        "scoreBand": score_b,
        "result":    result_b,
    }


# ═══════════════════════════════════════════════════════════════════════════════
# Phase 2a — Walk-forward segmentation
# ═══════════════════════════════════════════════════════════════════════════════

def _walk_forward_analysis(trades: list[Trade], segments: int = 4) -> dict:
    """
    Split trades into N equal-size segments by index. For each segment compute
    win rate, expectancy R, profit factor, max DD%. Classify consistency.
    Detects overfitting: if only one segment is profitable, all others weak.
    """
    n = len(trades)
    if n < segments or n < 8:
        return {
            "segments":       [],
            "interpretation": "insufficient_sample",
            "note": f"Need at least {max(segments, 8)} trades for {segments}-segment walk-forward "
                    f"(have {n})",
        }

    size = n // segments
    rows: list[dict] = []
    expectancies: list[float] = []

    for i in range(segments):
        start = i * size
        end   = (i + 1) * size if i < segments - 1 else n
        sub   = trades[start:end]
        if not sub:
            continue

        wins   = [t for t in sub if t.result == "WIN"]
        losses = [t for t in sub if t.result == "LOSS"]
        n_sub  = len(sub)
        wr     = round(len(wins) / n_sub * 100, 2)
        exp_R  = round(sum(t.r_multiple for t in sub) / n_sub, 3)
        exp_pts = round(sum(t.points for t in sub) / n_sub, 3)

        win_pts  = sum(t.points for t in wins)
        loss_pts = abs(sum(t.points for t in losses))
        pf = round(win_pts / loss_pts, 3) if loss_pts > 0 else None

        # Max DD within segment (compute from segment-only equity curve)
        eq = 0.0
        peak = 0.0
        max_dd = 0.0
        for t in sub:
            eq += t.r_multiple
            peak = max(peak, eq)
            dd = (peak - eq)
            max_dd = max(max_dd, dd)
        max_dd_pct = round(max_dd * 100 / max(peak, 1), 2)  # in R units → display as %

        rows.append({
            "segment":      i + 1,
            "period":       f"segment {i + 1}/{segments}",
            "validTrades":  n_sub,
            "winRate":      wr,
            "expectancyR":  exp_R,
            "expectancyPoints": exp_pts,
            "profitFactor": pf,
            "maxDrawdownR": round(max_dd, 2),
            "startTime":    sub[0].entry_time,
            "endTime":      sub[-1].entry_time,
        })
        expectancies.append(exp_R)

    # Interpretation
    if not expectancies:
        interp = "insufficient_sample"
    else:
        positive_count = sum(1 for e in expectancies if e > 0)
        # Trends
        if positive_count == 0:
            interp = "consistently_negative"
        elif positive_count == len(expectancies):
            interp = "consistent"
        elif positive_count == 1:
            interp = "single_segment_profitable_overfitting_risk"
        elif expectancies[-1] > expectancies[0] and positive_count >= len(expectancies) // 2:
            interp = "improving"
        elif expectancies[0] > expectancies[-1] and positive_count >= len(expectancies) // 2:
            interp = "deteriorating"
        else:
            interp = "inconsistent"

    return {
        "segments":       rows,
        "interpretation": interp,
        "segmentCount":   segments,
    }


# ═══════════════════════════════════════════════════════════════════════════════
# Phase 2a — Monte Carlo simulation
# ═══════════════════════════════════════════════════════════════════════════════

def _monte_carlo_simulation(
    trades:          list[Trade],
    runs:            int   = 500,
    initial_balance: float = 10000.0,
    risk_percent:    float = 0.25,
) -> dict:
    """
    Shuffle trade R-multiples N times. For each shuffle, walk through trades
    computing the equity curve and record max drawdown + final equity.

    Returns probability bands for drawdown and profitability.
    """
    n = len(trades)
    if n < 5:
        return {
            "runs":  0,
            "note":  f"Need >= 5 trades for Monte Carlo (have {n})",
            "available": False,
        }

    r_multiples = [t.r_multiple for t in trades]
    risk_amount_pct = risk_percent / 100.0

    final_equities: list[float] = []
    max_drawdowns:  list[float] = []
    losing_streaks: list[int]   = []

    rng = random.Random(42)   # deterministic seed for reproducibility

    for _ in range(runs):
        shuffled = list(r_multiples)
        rng.shuffle(shuffled)

        equity = initial_balance
        peak   = initial_balance
        max_dd_pct = 0.0
        current_streak = 0
        max_streak = 0

        for r in shuffled:
            risk_amt = equity * risk_amount_pct
            equity += risk_amt * r
            peak = max(peak, equity)
            if peak > 0:
                dd_pct = (peak - equity) / peak * 100
                max_dd_pct = max(max_dd_pct, dd_pct)
            if r < 0:
                current_streak += 1
                max_streak = max(max_streak, current_streak)
            else:
                current_streak = 0

        final_equities.append(equity)
        max_drawdowns.append(max_dd_pct)
        losing_streaks.append(max_streak)

    final_equities.sort()
    max_drawdowns.sort()
    losing_streaks.sort()

    profitable = sum(1 for f in final_equities if f > initial_balance)
    dd_above_10 = sum(1 for d in max_drawdowns if d > 10)
    dd_above_20 = sum(1 for d in max_drawdowns if d > 20)

    def _pct(idx: float) -> float:
        i = int(idx)
        return max_drawdowns[min(i, len(max_drawdowns) - 1)]

    return {
        "runs":                              runs,
        "available":                         True,
        "tradeSampleSize":                   n,
        "probabilityProfitable":             round(profitable / runs * 100, 2),
        "medianFinalBalance":                round(final_equities[runs // 2], 2),
        "medianMaxDrawdownPercent":          round(max_drawdowns[runs // 2], 2),
        "p95MaxDrawdownPercent":             round(_pct(runs * 0.95), 2),
        "worstMaxDrawdownPercent":           round(max_drawdowns[-1], 2),
        "probabilityDrawdownAbove10Percent": round(dd_above_10 / runs * 100, 2),
        "probabilityDrawdownAbove20Percent": round(dd_above_20 / runs * 100, 2),
        "medianLongestLosingStreak":         losing_streaks[runs // 2],
        "worstLongestLosingStreak":          losing_streaks[-1],
        "interpretation": _mc_interpretation(
            profitable / runs * 100,
            max_drawdowns[runs // 2],
            dd_above_20 / runs * 100,
        ),
    }


def _mc_interpretation(prob_profitable: float, median_dd: float, prob_dd20: float) -> str:
    if prob_profitable >= 70 and median_dd < 10 and prob_dd20 < 5:
        return "Robust — high probability of profit, drawdown well-contained"
    if prob_profitable >= 60 and median_dd < 15:
        return "Acceptable — likely profitable with manageable drawdown"
    if prob_profitable >= 50:
        return "Marginal — coin-flip outcome, drawdown risk material"
    return "Fragile — strategy probably unprofitable under random sequencing"


# ═══════════════════════════════════════════════════════════════════════════════
# Phase 2a — Market regime classification
# ═══════════════════════════════════════════════════════════════════════════════

def _classify_regime(candles: list, pip_size: float) -> str:
    """
    Classify market regime at the trade bar using ATR + range characteristics.

    Regimes:
      TRENDING        — directional bias + ATR moderate
      RANGE_BOUND     — price oscillating in tight band
      COMPRESSION     — ATR shrinking sharply
      EXPANSION       — ATR rising sharply
      HIGH_VOLATILITY — ATR > 2x typical
      LOW_VOLATILITY  — ATR < 0.5x typical
      REVERSAL        — recent strong move + opposing wick
      UNKNOWN         — insufficient data
    """
    if len(candles) < 25:
        return "UNKNOWN"

    bars = candles[-25:]
    ranges = [(c.high - c.low) / pip_size for c in bars]
    atr_14 = sum(ranges[-14:]) / 14
    atr_5  = sum(ranges[-5:]) / 5
    atr_prior = sum(ranges[:-5]) / max(len(ranges) - 5, 1)

    if atr_prior == 0:
        return "UNKNOWN"

    # Vol regime
    if atr_14 > 2 * atr_prior:
        return "HIGH_VOLATILITY"
    if atr_14 < 0.5 * atr_prior:
        return "LOW_VOLATILITY"

    # Expansion / compression
    if atr_prior > 0:
        ratio = atr_5 / atr_prior
        if ratio > 1.4:
            return "EXPANSION"
        if ratio < 0.6:
            return "COMPRESSION"

    # Trending vs range
    highs = [c.high for c in bars]
    lows  = [c.low  for c in bars]
    price_range = max(highs) - min(lows)
    if price_range == 0:
        return "RANGE_BOUND"

    # If close has moved >= 60% of range from period start → trending
    travel = abs(bars[-1].close - bars[0].close)
    if travel >= price_range * 0.55:
        return "TRENDING"

    # Reversal: large wick on most recent bar opposing direction
    last = bars[-1]
    body = abs(last.close - last.open)
    upper_wick = last.high - max(last.open, last.close)
    lower_wick = min(last.open, last.close) - last.low
    if body > 0 and max(upper_wick, lower_wick) > body * 1.5:
        return "REVERSAL"

    return "RANGE_BOUND"


def _regime_breakdown(trades: list[Trade]) -> list[dict]:
    if not trades:
        return []
    regimes: dict[str, list[Trade]] = {}
    for t in trades:
        regimes.setdefault(t.market_state or "UNKNOWN", []).append(t)

    rows: list[dict] = []
    for regime, sub in regimes.items():
        wins = [t for t in sub if t.result == "WIN"]
        wr   = round(len(wins) / len(sub) * 100, 2)
        exp_R = round(sum(t.r_multiple for t in sub) / len(sub), 3)
        win_pts  = sum(t.points for t in wins)
        loss_pts = abs(sum(t.points for t in sub if t.result == "LOSS"))
        pf = round(win_pts / loss_pts, 3) if loss_pts > 0 else None
        rows.append({
            "regime":       regime,
            "trades":       len(sub),
            "wins":         len(wins),
            "winRate":      wr,
            "expectancyR":  exp_R,
            "averagePoints": round(sum(t.points for t in sub) / len(sub), 2),
            "profitFactor": pf,
            "recommendation": (
                "Trade — strong edge"        if exp_R >= 0.3 and len(sub) >= 10 else
                "Trade — acceptable edge"    if exp_R >= 0.1 and len(sub) >= 10 else
                "Watchlist — small sample"   if len(sub) < 10 else
                "Filter out — negative edge"
            ),
        })
    rows.sort(key=lambda r: -r["expectancyR"])
    return rows


# ═══════════════════════════════════════════════════════════════════════════════
# Phase 2a — News-day classification
# ═══════════════════════════════════════════════════════════════════════════════

def _classify_news_day(at: datetime, macro_events: list[dict]) -> str:
    """
    Classify the bar as: normal_day | pre_news | post_news | news_window | high_impact_day.
    """
    if not macro_events:
        return "normal_day"

    if not at.tzinfo:
        at = at.replace(tzinfo=timezone.utc)

    same_day_events = []
    for e in macro_events:
        if str(e.get("impact", "")).lower() != "high":
            continue
        try:
            ev_time = datetime.fromisoformat(str(e.get("time", "")).replace("Z", "+00:00"))
        except Exception:
            continue
        # SQLite may strip tzinfo — coerce both sides to UTC
        if ev_time.tzinfo is None:
            ev_time = ev_time.replace(tzinfo=timezone.utc)
        if ev_time.date() == at.date():
            same_day_events.append(ev_time)

    if not same_day_events:
        return "normal_day"

    # Find the closest event
    nearest = min(same_day_events, key=lambda t: abs((t - at).total_seconds()))
    diff_min = (nearest - at).total_seconds() / 60

    if -30 <= diff_min <= 60:
        return "news_window"
    if 0 < diff_min <= 180:
        return "pre_news"
    if -180 <= diff_min < 0:
        return "post_news"
    return "high_impact_day"


def _news_day_breakdown(trades: list[Trade]) -> list[dict]:
    if not trades:
        return []
    groups: dict[str, list[Trade]] = {}
    for t in trades:
        key = t.blockers[0] if t.blockers else "normal_day"
        groups.setdefault(key, []).append(t)
    rows = []
    for label, sub in groups.items():
        wins = [t for t in sub if t.result == "WIN"]
        wr = round(len(wins) / len(sub) * 100, 2)
        exp_R = round(sum(t.r_multiple for t in sub) / len(sub), 3)
        rows.append({
            "category":    label,
            "trades":      len(sub),
            "winRate":     wr,
            "expectancyR": exp_R,
        })
    rows.sort(key=lambda r: -r["expectancyR"])
    return rows


# ═══════════════════════════════════════════════════════════════════════════════
# Phase 2a — Setup type classification (lightweight)
# ═══════════════════════════════════════════════════════════════════════════════

def _setup_type_breakdown(trades: list[Trade]) -> list[dict]:
    """
    Group trades by the engine's structured setup_type field.
    Used to identify which setup categories actually have edge.
    """
    if not trades:
        return []

    # Human-readable display labels for each canonical setup type
    display = {
        "liquidity_sweep_choch_fvg":  "Liquidity sweep + CHoCH + FVG",
        "bos_continuation_fvg":       "BOS continuation + FVG",
        "choch_ob_reversal":          "CHoCH + OB retest reversal",
        "bos_continuation":           "BOS continuation (no FVG retest)",
        "choch_reversal":             "CHoCH reversal (no FVG retest)",
        "fvg_retest_only":            "FVG retest only",
        "session_sweep_reversal":     "Session sweep reversal",
        "premium_discount_reversal":  "Premium/discount FVG+OB",
        "post_news_displacement":     "Post-news displacement",
        "ote_pullback":               "OTE pullback (no clear pattern)",
        "weak_setup":                 "Weak setup (marginal)",
        "no_signal":                  "No primary signal",
        "unknown":                    "Unclassified",
    }

    groups: dict[str, list[Trade]] = {}
    for t in trades:
        key = t.setup_type or "unknown"
        label = display.get(key, key)
        groups.setdefault(label, []).append(t)

    rows = []
    for label, sub in groups.items():
        wins = [t for t in sub if t.result == "WIN"]
        wr = round(len(wins) / len(sub) * 100, 2)
        exp_R = round(sum(t.r_multiple for t in sub) / len(sub), 3)
        avg_held = round(sum(t.bars_held for t in sub) / len(sub), 1)
        rows.append({
            "setupType":         label,
            "trades":            len(sub),
            "winRate":           wr,
            "expectancyR":       exp_R,
            "averageBarsHeld":   avg_held,
            "failureRate":       round((len(sub) - len(wins)) / len(sub) * 100, 2),
        })
    rows.sort(key=lambda r: -r["expectancyR"])
    return rows


# ═══════════════════════════════════════════════════════════════════════════════
# Phase 2a — Risk sensitivity (analytical)
# ═══════════════════════════════════════════════════════════════════════════════

def _risk_sensitivity_analysis(
    trades: list[Trade],
    initial_balance: float,
    risk_levels: list[float] = (0.25, 0.5, 1.0),
) -> list[dict]:
    """
    Project final equity + max DD for each risk level by re-walking R-multiples.
    Analytical — no re-running of the engine.
    """
    if not trades:
        return []

    r_multiples = [t.r_multiple for t in trades]
    rows: list[dict] = []
    for risk_pct in risk_levels:
        equity = initial_balance
        peak   = initial_balance
        max_dd_pct = 0.0
        for r in r_multiples:
            risk_amt = equity * (risk_pct / 100.0)
            equity += risk_amt * r
            peak = max(peak, equity)
            if peak > 0:
                max_dd_pct = max(max_dd_pct, (peak - equity) / peak * 100)
        rows.append({
            "riskPercent":          risk_pct,
            "finalBalance":         round(equity, 2),
            "netReturnPercent":     round((equity - initial_balance) / initial_balance * 100, 2),
            "maxDrawdownPercent":   round(max_dd_pct, 2),
            "recommendation": (
                "Acceptable" if max_dd_pct < 10 else
                "Caution"    if max_dd_pct < 20 else
                "Reject — drawdown too large"
            ),
        })
    return rows


# ═══════════════════════════════════════════════════════════════════════════════
# Phase 2a — Skipped diagnostics
# Phase 2b — Hypothetical skipped-trade outcomes
# ═══════════════════════════════════════════════════════════════════════════════

def _skipped_diagnostics(skipped: list[SkippedTrade]) -> dict:
    if not skipped:
        return {"totalSkipped": 0, "byReason": [], "hypotheticalOutcomes": None}
    counter: dict[str, int] = {}
    for s in skipped:
        counter[s.reason] = counter.get(s.reason, 0) + 1
    rows = sorted(
        [{"reason": k, "count": v, "percent": round(v / len(skipped) * 100, 2)}
         for k, v in counter.items()],
        key=lambda r: -r["count"],
    )

    # Hypothetical-outcome aggregates (only for skipped trades with simulation data)
    hypothetical_rows: list[dict] = []
    by_reason_hyp: dict[str, list[SkippedTrade]] = {}
    for s in skipped:
        if s.hypothetical_result in ("WIN", "LOSS", "EXPIRED"):
            by_reason_hyp.setdefault(s.reason, []).append(s)

    for reason, lst in by_reason_hyp.items():
        wins = sum(1 for s in lst if s.hypothetical_result == "WIN")
        losses = sum(1 for s in lst if s.hypothetical_result == "LOSS")
        expired = sum(1 for s in lst if s.hypothetical_result == "EXPIRED")
        total = len(lst)
        exp_r = sum(s.hypothetical_r for s in lst) / total if total else 0
        win_rate = wins / total * 100 if total else 0
        hypothetical_rows.append({
            "reason":             reason,
            "simulated":          total,
            "hypotheticalWins":   wins,
            "hypotheticalLosses": losses,
            "hypotheticalExpired": expired,
            "hypotheticalWinRate":     round(win_rate, 2),
            "hypotheticalExpectancyR": round(exp_r, 3),
            "interpretation": (
                "Skipping saved you losses"  if exp_r < -0.1 else
                "Skipping cost you wins"     if exp_r > 0.3  else
                "Skipping had neutral impact"
            ),
        })
    hypothetical_rows.sort(key=lambda r: -r["simulated"])

    return {
        "totalSkipped": len(skipped),
        "byReason":     rows,
        "hypotheticalOutcomes": hypothetical_rows if hypothetical_rows else None,
        "note": (
            "Skipped trades are NOT counted in main performance. "
            "Hypothetical outcomes show what would have happened if "
            "the skip-reason had been relaxed — diagnostic insight only."
        ),
    }


def _simulate_skipped_outcomes(
    skipped: list[SkippedTrade],
    bars: list,
    max_holding: int,
) -> None:
    """
    For each skipped trade with a defined entry/SL/TP, walk forward through
    bars and determine whether it would have WIN/LOSS/EXPIRED. Mutates the
    SkippedTrade in place. Only simulates skips that have entry geometry.
    """
    for s in skipped:
        if s.bar_idx < 0 or s.risk_points <= 0 or s.target_points <= 0:
            continue
        exit_time, result_label, points_captured, r_mult, bars_held = _resolve_trade(
            bars=bars,
            entry_idx=s.bar_idx,
            signal=s.signal,
            adj_entry=s.entry,
            stop_loss=s.stop_loss,
            take_profit=s.take_profit,
            max_holding=max_holding,
            risk_points=s.risk_points,
            reward_points=s.target_points,
        )
        s.hypothetical_result = result_label
        s.hypothetical_r      = round(r_mult, 3)


# ═══════════════════════════════════════════════════════════════════════════════
# Phase 2b — Historical data quality audit
# ═══════════════════════════════════════════════════════════════════════════════

def _audit_data_quality(candles: list, timeframe: str, data_source: str) -> dict:
    """
    Pre-backtest data quality audit. Classifies each gap:
      - Weekend gap        Fri close -> Mon/Sun open (expected, not counted)
      - Holiday gap        Christmas/New Year/Thanksgiving/etc (expected)
      - Suspicious gap     Mid-week, non-holiday — counted as missing
      - Duplicate          Two candles at same timestamp
      - Invalid OHLC       Sanity violation (counted as critical)
      - Flat               high == low (low-volume bar)
      - Abnormal range     > 5 stdev (likely real news spike, not error)

    Real gold market data ALWAYS has ~6-12 H4 bars missing per holiday
    closure. Over 3+ years of H4 data, 200-500 such bars is normal.
    Only "suspicious" gaps reduce the reliability score.
    """
    if not candles:
        return {
            "status": "FAIL",
            "totalCandles": 0,
            "warnings": ["No candles available"],
        }

    n = len(candles)
    warnings: list[str] = []

    interval_min_map = {"M5": 5, "M15": 15, "M30": 30, "H1": 60, "H4": 240, "D1": 1440}
    interval_min = interval_min_map.get(timeframe, 15)
    interval_sec = interval_min * 60

    first_t = candles[0].time
    last_t  = candles[-1].time
    if first_t.tzinfo is None: first_t = first_t.replace(tzinfo=timezone.utc)
    if last_t.tzinfo  is None: last_t  = last_t.replace(tzinfo=timezone.utc)

    # Gap classification
    weekend_bars  = 0    # bars skipped over weekends (expected)
    holiday_bars  = 0    # bars skipped over holiday closures (expected)
    missing_bars  = 0    # actual data integrity issues (mid-week, no holiday)
    duplicates    = 0
    prev = None
    for c in candles:
        t = c.time if c.time.tzinfo else c.time.replace(tzinfo=timezone.utc)
        if prev is not None:
            diff_sec = (t - prev).total_seconds()
            if diff_sec == 0:
                duplicates += 1
            elif diff_sec > interval_sec * 3:
                gap_bars = int(diff_sec / interval_sec) - 1
                if _is_weekend_gap(prev, t):
                    weekend_bars += gap_bars
                elif _is_holiday_gap(prev, t):
                    holiday_bars += gap_bars
                else:
                    missing_bars += gap_bars
        prev = t

    # OHLC validity + flat detection
    invalid = 0
    flat = 0
    ranges = []
    for c in candles:
        if c.high < max(c.open, c.close, c.low):
            invalid += 1
        elif c.low > min(c.open, c.close, c.high):
            invalid += 1
        elif c.high <= 0 or c.low <= 0:
            invalid += 1
        elif c.high == c.low:
            flat += 1
        ranges.append(c.high - c.low)

    # Abnormal range — note: large H4 ranges during news are NORMAL for gold
    # (NFP/CPI can produce >5σ moves). Reported for transparency but
    # NOT treated as a data integrity issue.
    abnormal = 0
    if len(ranges) >= 20:
        mean_r = sum(ranges) / len(ranges)
        var = sum((r - mean_r) ** 2 for r in ranges) / len(ranges)
        std = var ** 0.5
        threshold = mean_r + 5 * std
        abnormal = sum(1 for r in ranges if r > threshold)

    # Status determination — based ONLY on REAL data issues, not holidays
    missing_pct = missing_bars / max(n, 1) * 100
    if invalid > 0 or missing_pct > 5:
        status = "FAIL"
    elif missing_pct > 2 or flat > n * 0.05 or duplicates > 0:
        status = "WARN"
    else:
        status = "PASS"

    # Honest warnings
    if missing_bars > 0:
        warnings.append(
            f"Mid-week gaps (likely data feed issues): {missing_bars} bars "
            f"({missing_pct:.2f}% of dataset)"
        )
    if holiday_bars > 0:
        warnings.append(
            f"Holiday closures: ~{holiday_bars} bars skipped "
            f"(expected — Christmas/New Year/Thanksgiving/Good Friday etc.)"
        )
    if weekend_bars > 0 and n < 100:
        # Only mention weekends for very small datasets
        warnings.append(f"Weekend gaps: ~{weekend_bars} bars skipped (expected)")
    if duplicates > 0:
        warnings.append(f"Duplicate timestamps detected: {duplicates}")
    if invalid > 0:
        warnings.append(f"INVALID OHLC candles: {invalid} (critical)")
    if flat > n * 0.05:
        warnings.append(f"High flat-candle rate: {flat} ({flat / n * 100:.1f}%)")
    if abnormal > 0:
        warnings.append(
            f"Abnormal-range candles: {abnormal} "
            f"(likely real news-driven moves, not errors)"
        )
    if data_source == "synthetic":
        warnings.append(
            "Synthetic data — results are illustrative only. "
            "Import real candles for statistical validity."
        )

    return {
        "status":          status,
        "totalCandles":    n,
        # Backward-compat: "missingCandles" now reports ONLY suspicious gaps
        "missingCandles":  missing_bars,
        "weekendGaps":     weekend_bars,
        "holidayGaps":     holiday_bars,
        "duplicateCandles": duplicates,
        "invalidCandles":  invalid,
        "flatCandles":     flat,
        "abnormalCandles": abnormal,
        "coverageStart":   first_t.isoformat(),
        "coverageEnd":     last_t.isoformat(),
        "timeframe":       timeframe,
        "dataSource":      data_source,
        "warnings":        warnings,
        "interpretation": (
            "Data quality acceptable. Holiday closures are normal market behaviour, not data issues."
                if status == "PASS" else
            "Data quality reduced — some mid-week gaps detected. Backtest still runs but reliability slightly impacted."
                if status == "WARN" else
            "Critical data quality issues — significant mid-week gaps or invalid OHLC. Backtest reliability significantly reduced."
        ),
    }


def _is_weekend_gap(prev: datetime, curr: datetime) -> bool:
    """True if the gap spans a weekend (Sat/Sun)."""
    delta = (curr - prev).total_seconds()
    if delta < 24 * 3600:
        return False
    return prev.weekday() == 4 or curr.weekday() == 0


def _is_holiday_gap(prev: datetime, curr: datetime) -> bool:
    """
    True if the gap likely spans a recognised gold market holiday closure.

    Detects:
      - Christmas / New Year (Dec 23 -> Jan 3)
      - US Thanksgiving (4th Thursday of November + Friday)
      - US Independence Day (July 4)
      - Memorial Day (last Monday of May)
      - Labor Day (1st Monday of September)
      - Good Friday (varies — uses date range Mar 20 - Apr 25 with weekday check)
      - Easter Monday
    """
    delta = (curr - prev).total_seconds()
    # Holiday gaps are typically 24h - 5d; longer is suspicious
    if delta < 12 * 3600 or delta > 6 * 86400:
        return False

    # Check if any day in the gap window is a recognised holiday
    cur_check = prev.replace(hour=0, minute=0, second=0, microsecond=0)
    end_check = curr.replace(hour=0, minute=0, second=0, microsecond=0)
    days_to_check = []
    while cur_check <= end_check and len(days_to_check) < 10:
        days_to_check.append(cur_check)
        cur_check = cur_check + timedelta(days=1)

    for d in days_to_check:
        m, day, wd = d.month, d.day, d.weekday()
        # Christmas / New Year window (Dec 23 - Jan 3)
        if (m == 12 and day >= 23) or (m == 1 and day <= 3):
            return True
        # US Independence Day
        if m == 7 and day in (3, 4, 5):
            return True
        # Thanksgiving (4th Thursday of November) + Black Friday
        if m == 11 and wd == 3 and 22 <= day <= 28:
            return True
        if m == 11 and wd == 4 and 23 <= day <= 29:  # Black Friday
            return True
        # Memorial Day (last Monday of May)
        if m == 5 and wd == 0 and day >= 25:
            return True
        # Labor Day (1st Monday of September)
        if m == 9 and wd == 0 and day <= 7:
            return True
        # Good Friday window (approximate — late March / April)
        if m in (3, 4) and wd == 4 and 20 <= day <= 25:
            return True
        # Easter Monday (approximate)
        if m in (3, 4) and wd == 0 and 22 <= day <= 27:
            return True
        # New Year's Eve / Day proper
        if m == 1 and day == 1:
            return True
        if m == 12 and day == 31:
            return True
    return False


# ═══════════════════════════════════════════════════════════════════════════════
# Phase 2b — Refined news-day windows
# ═══════════════════════════════════════════════════════════════════════════════

def _classify_news_window_fine(at: datetime, macro_events: list[dict]) -> str:
    """
    Finer-grained news classification than _classify_news_day.

    Returns:
      news_window_30        within ±30 min of high-impact event
      post_news_30_120      30-120 min after release
      post_news_120_240     2-4 hours after release
      pre_news_120          0-120 min before release
      high_impact_day       same day, outside windows
      normal_day            no high-impact USD event same day
    """
    if not macro_events:
        return "normal_day"

    if not at.tzinfo:
        at = at.replace(tzinfo=timezone.utc)

    same_day_events: list[datetime] = []
    for e in macro_events:
        if str(e.get("impact", "")).lower() != "high":
            continue
        try:
            ev_time = datetime.fromisoformat(str(e.get("time", "")).replace("Z", "+00:00"))
            if ev_time.tzinfo is None:
                ev_time = ev_time.replace(tzinfo=timezone.utc)
            if ev_time.date() == at.date():
                same_day_events.append(ev_time)
        except Exception:
            continue

    if not same_day_events:
        return "normal_day"

    nearest = min(same_day_events, key=lambda t: abs((t - at).total_seconds()))
    diff_min = (at - nearest).total_seconds() / 60

    if -30 <= diff_min <= 30:
        return "news_window_30"
    if 30 < diff_min <= 120:
        return "post_news_30_120"
    if 120 < diff_min <= 240:
        return "post_news_120_240"
    if -120 <= diff_min < 0:
        return "pre_news_120"
    return "high_impact_day"


# ═══════════════════════════════════════════════════════════════════════════════
# Phase 2a — Overfitting risk assessment
# ═══════════════════════════════════════════════════════════════════════════════

def _assess_overfitting(
    summary: dict, walk_forward: dict, breakdowns: dict,
    regime_perf: list[dict], monte_carlo: dict | None, trades: list[Trade],
) -> dict:
    """
    Emit an overfitting risk level (LOW/MEDIUM/HIGH/CRITICAL) with warnings.
    """
    warnings: list[str] = []
    score = 0   # higher = more risk

    # 1. Sample size
    n = summary.get("validTrades", 0)
    if n < 30:
        warnings.append(f"Tiny sample ({n} trades) — statistically unreliable")
        score += 3
    elif n < 100:
        warnings.append(f"Small sample ({n} trades) — limited confidence")
        score += 1

    # 2. Walk-forward consistency
    interp = walk_forward.get("interpretation", "")
    if interp == "single_segment_profitable_overfitting_risk":
        warnings.append("Only 1 walk-forward segment profitable — likely curve-fit")
        score += 3
    elif interp == "consistently_negative":
        warnings.append("All walk-forward segments are negative")
        score += 2
    elif interp == "deteriorating":
        warnings.append("Walk-forward performance is deteriorating")
        score += 2
    elif interp == "inconsistent":
        warnings.append("Walk-forward performance is inconsistent across segments")
        score += 1

    # 3. Single-session dominance
    sess_b = breakdowns.get("session", [])
    if sess_b:
        total_pts = sum(s.get("totalPoints", 0) for s in sess_b)
        if total_pts != 0:
            for s in sess_b:
                if s.get("totalPoints", 0) > 0 and abs(s["totalPoints"]) >= abs(total_pts) * 0.8:
                    warnings.append(
                        f"Single session '{s['session']}' produces >80% of total profit"
                    )
                    score += 2
                    break

    # 4. Score predictiveness
    score_b = breakdowns.get("scoreBand", [])
    if len(score_b) >= 2:
        # Higher score bands should monotonically increase expectancyR
        exps = [b["expectancyR"] for b in score_b]
        if not all(exps[i] <= exps[i + 1] for i in range(len(exps) - 1)):
            warnings.append("Score bands not monotonic — signal score may not be predictive")
            score += 1

    # 5. Regime dependence
    if regime_perf:
        positives = [r for r in regime_perf if r["expectancyR"] > 0]
        if len(regime_perf) >= 3 and len(positives) == 1:
            warnings.append(
                f"Only regime '{positives[0]['regime']}' is profitable — regime-dependent edge"
            )
            score += 1

    # 6. Drawdown excessive
    if summary.get("maxDrawdownPercent", 0) > 20:
        warnings.append(f"Max drawdown {summary['maxDrawdownPercent']}% exceeds 20%")
        score += 2

    # 7. Monte Carlo robustness
    if monte_carlo and monte_carlo.get("available"):
        if monte_carlo.get("probabilityProfitable", 100) < 50:
            warnings.append(
                f"Monte Carlo: only {monte_carlo['probabilityProfitable']}% of "
                "random sequences are profitable"
            )
            score += 2
        if monte_carlo.get("probabilityDrawdownAbove20Percent", 0) > 25:
            warnings.append(
                f"Monte Carlo: {monte_carlo['probabilityDrawdownAbove20Percent']}% "
                "of sequences hit >20% drawdown"
            )
            score += 1

    # 8. Excessive RR_AFTER_COST_TOO_LOW skips
    rr_skip_pct = 0.0
    # (computed downstream from skipped diagnostics — skip for now)

    # Risk level
    if score >= 7:
        level = "CRITICAL"
    elif score >= 4:
        level = "HIGH"
    elif score >= 2:
        level = "MEDIUM"
    else:
        level = "LOW"

    return {
        "level":       level,
        "score":       score,
        "warnings":    warnings,
        "interpretation": {
            "LOW":      "No major red flags detected.",
            "MEDIUM":   "Some concerns — review warnings before proceeding to paper.",
            "HIGH":     "Strong signs of overfitting — strategy may not generalise.",
            "CRITICAL": "Backtest evidence is unreliable — do NOT proceed.",
        }[level],
    }


# ═══════════════════════════════════════════════════════════════════════════════
# Phase 2a — Quality score + recommendation engine (replaces reliability)
# ═══════════════════════════════════════════════════════════════════════════════

def _compute_quality_score(
    summary:      dict,
    trades:       list[Trade],
    walk_forward: dict,
    breakdowns:   dict,
    regime_perf:  list[dict],
    overfitting:  dict,
    data_source:  str,
    data_quality: dict | None = None,
) -> dict:
    """
    Backtest Quality Score (0-100). Replaces older reliability rating.

    Components (max points):
      Data quality:               15
      Sample size:                15
      Expectancy:                 15
      Profit factor:              15
      Drawdown control:           15
      Walk-forward consistency:   10
      Session robustness:          5
      Regime robustness:           5
      Score predictiveness:        5

    Hard caps applied AFTER summing.
    Adds 5-tier recommendation enum.
    """
    n = summary.get("validTrades", 0)
    expectancy = summary.get("expectancyPoints", 0)
    eR         = summary.get("expectancyR", 0)
    pf         = summary.get("profitFactor") or 0
    max_dd     = summary.get("maxDrawdownPercent", 0)

    score = 0

    # Data quality (15) — combines source + audit status
    dq_status = (data_quality or {}).get("status", "PASS")
    if data_source == "database" and dq_status == "PASS":
        score += 15
    elif data_source == "database" and dq_status == "WARN":
        score += 10
    elif data_source == "database":
        score += 5    # FAIL — keep some signal but penalise
    elif data_source == "synthetic":
        score += 5    # honest about synthetic limitation
    else:
        score += 8

    # Sample size (15)
    if n >= 200:    score += 15
    elif n >= 100:  score += 12
    elif n >= 50:   score += 8
    elif n >= 30:   score += 5
    elif n >= 10:   score += 2

    # Expectancy R (15)
    if eR >= 0.5:    score += 15
    elif eR >= 0.3:  score += 12
    elif eR >= 0.15: score += 8
    elif eR >= 0.05: score += 4

    # Profit factor (15)
    if pf >= 2.0:    score += 15
    elif pf >= 1.5:  score += 12
    elif pf >= 1.2:  score += 8
    elif pf >= 1.0:  score += 4

    # Drawdown (15) — lower is better
    if max_dd < 5:     score += 15
    elif max_dd < 10:  score += 12
    elif max_dd < 15:  score += 8
    elif max_dd < 25:  score += 4

    # Walk-forward consistency (10)
    interp = walk_forward.get("interpretation", "")
    if interp == "consistent":           score += 10
    elif interp == "improving":          score += 7
    elif interp == "inconsistent":       score += 4
    elif interp == "deteriorating":      score += 2
    # consistently_negative, single_segment, insufficient_sample → 0

    # Session robustness (5) — at least 2 sessions with WR >= 45% and >= 5 trades
    sess_robust = 0
    for s in breakdowns.get("session", []):
        if s.get("winRate", 0) >= 45 and s.get("trades", 0) >= 5:
            sess_robust += 1
    if sess_robust >= 3:   score += 5
    elif sess_robust >= 2: score += 3
    elif sess_robust >= 1: score += 1

    # Regime robustness (5) — at least 2 profitable regimes
    pos_regimes = sum(1 for r in regime_perf if r.get("expectancyR", 0) > 0)
    if pos_regimes >= 3:   score += 5
    elif pos_regimes >= 2: score += 3
    elif pos_regimes >= 1: score += 1

    # Score predictiveness (5) — monotonic improvement across bands
    score_b = breakdowns.get("scoreBand", [])
    if len(score_b) >= 2:
        exps = [b["expectancyR"] for b in score_b]
        monotonic = all(exps[i] <= exps[i + 1] for i in range(len(exps) - 1))
        if monotonic:    score += 5
        elif exps[-1] > exps[0]:  score += 2

    # Hard caps
    of_level = overfitting.get("level", "LOW")
    if n < 30:                        score = min(score, 50)
    elif n < 100:                     score = min(score, 75)
    if expectancy <= 0:               score = min(score, 50)
    if pf < 1.2:                      score = min(score, 60)
    if max_dd > 15:                   score = min(score, 65)
    if of_level == "HIGH":            score = min(score, 70)
    if of_level == "CRITICAL":        score = min(score, 50)

    # Band classification
    if score >= 85:    band = "Strong — paper-test eligible"
    elif score >= 70:  band = "Promising — requires paper observation"
    elif score >= 50:  band = "Needs more testing"
    else:              band = "Weak / unreliable"

    # 5-tier recommendation
    if expectancy <= 0 or pf < 1.1 or max_dd > 25 or n < 30:
        recommendation = "NOT_READY"
        verdict = (
            "Backtest evidence is insufficient. Do not proceed to paper trading. "
            "Iterate the strategy or gather more historical data."
        )
    elif n < 100 or of_level in ("HIGH", "CRITICAL"):
        recommendation = "COLLECT_MORE_DATA"
        verdict = (
            f"Sample size and/or overfitting risk ({of_level}) prevent recommendation. "
            "Import more historical XAU/USD data and re-run."
        )
    elif n < 100 and eR > 0 and pf >= 1.2:
        recommendation = "PAPER_OBSERVATION_ONLY"
        verdict = (
            "Results are promising but sample is below 100 trades. "
            "Permit paper observation only — no execution decisions yet."
        )
    elif n >= 100 and eR > 0 and pf >= 1.2 and max_dd <= 15 and of_level in ("LOW", "MEDIUM"):
        if n >= 150 and pf >= 1.3 and max_dd <= 10 and of_level == "LOW":
            recommendation = "READY_FOR_DEMO_TEST_AFTER_REVIEW"
            verdict = (
                "Strong evidence across all dimensions. Proceed to structured "
                "demo testing AFTER manual strategy review. Live trading remains disabled."
            )
        else:
            recommendation = "READY_FOR_STRUCTURED_PAPER_TEST"
            verdict = (
                "Backtest passes validation thresholds. Proceed to structured "
                "paper observation — track 30+ live signals before considering demo."
            )
    else:
        recommendation = "NOT_READY"
        verdict = "One or more validation thresholds not met. Iterate and re-run."

    return {
        "score":          score,
        "band":           band,
        "verdict":        verdict,
        "recommendation": recommendation,
        "overfittingLevel": of_level,
        "components": {
            "dataQuality":          15 if data_source == "database" else 5 if data_source == "synthetic" else 8,
            "sampleSize":           min(15, score),    # informational
            "expectancyR":          eR,
            "profitFactor":         pf,
            "maxDrawdownPercent":   max_dd,
            "walkForward":          interp,
            "positiveRegimes":      pos_regimes,
            "robustSessions":       sess_robust,
        },
    }


# ═══════════════════════════════════════════════════════════════════════════════
# Reliability rating (0-100) — kept as legacy alias
# ═══════════════════════════════════════════════════════════════════════════════

def _reliability_rating(summary: dict, trades: list[Trade]) -> dict:
    """
    Composite reliability rating with hard caps.

    Components (max points):
      Sample size:        20
      Profit factor:      20
      Expectancy:         20
      Drawdown:           15
      Session consistency: 10
      Direction balance:   5
      News robustness:     5  (placeholder — needs historical news data)
      Rule compliance:     5

    Hard caps applied AFTER component scoring:
      validTrades < 30          → cap 50
      validTrades < 100         → cap 75
      expectancy <= 0           → cap 50
      profit factor < 1.2       → cap 60
      max drawdown > 15%        → cap 65
      rule violations exist     → cap 60
    """
    n          = summary["validTrades"]
    expectancy = summary["expectancyPoints"]
    pf         = summary.get("profitFactor") or 0
    max_dd     = summary["maxDrawdownPercent"]

    score = 0
    violations: list[str] = []

    # Sample size (20)
    if n >= 200:    score += 20
    elif n >= 100:  score += 16
    elif n >= 50:   score += 10
    elif n >= 30:   score += 6
    elif n >= 10:   score += 2

    # Profit factor (20)
    if pf >= 2.0:    score += 20
    elif pf >= 1.5:  score += 15
    elif pf >= 1.2:  score += 10
    elif pf >= 1.0:  score += 5

    # Expectancy (20) — measured in R
    eR = summary["expectancyR"]
    if eR >= 0.5:    score += 20
    elif eR >= 0.3:  score += 15
    elif eR >= 0.15: score += 10
    elif eR >= 0.05: score += 5

    # Drawdown (15) — lower is better
    if max_dd < 5:     score += 15
    elif max_dd < 10:  score += 12
    elif max_dd < 15:  score += 8
    elif max_dd < 25:  score += 4

    # Session consistency (10): >= 2 winning sessions with WR >= 40%
    sess_wins = 0
    sess_map: dict[str, list[Trade]] = {}
    for t in trades:
        sess_map.setdefault(t.session, []).append(t)
    for s, lst in sess_map.items():
        wr = sum(1 for t in lst if t.result == "WIN") / max(len(lst), 1)
        if wr >= 0.4 and len(lst) >= 5:
            sess_wins += 1
    if sess_wins >= 3:   score += 10
    elif sess_wins >= 2: score += 7
    elif sess_wins >= 1: score += 3

    # Direction balance (5): BUY/SELL trade counts within 30-70% split
    buy_count  = sum(1 for t in trades if t.signal == "BUY")
    sell_count = sum(1 for t in trades if t.signal == "SELL")
    if n > 0:
        buy_pct = buy_count / n
        if 0.3 <= buy_pct <= 0.7:
            score += 5
        elif 0.2 <= buy_pct <= 0.8:
            score += 3
        else:
            violations.append("UNBALANCED_DIRECTION_DISTRIBUTION")

    # News robustness (5) — placeholder since historical news isn't yet wired
    score += 3   # neutral midpoint; full credit requires historical news data

    # Rule compliance (5) — verify no trade slipped through gate checks
    for t in trades:
        if t.score < 80:
            violations.append("GATE_VIOLATION_SCORE")
            break
        if t.rr < 2.5:
            violations.append("GATE_VIOLATION_RR")
            break
    else:
        score += 5

    # Apply hard caps
    if n < 30:        score = min(score, 50)
    elif n < 100:     score = min(score, 75)
    if expectancy <= 0: score = min(score, 50)
    if pf < 1.2:      score = min(score, 60)
    if max_dd > 15:   score = min(score, 65)
    if violations:    score = min(score, 60)

    # Band classification
    if score >= 85:    band = "Strong (paper-test eligible)"
    elif score >= 70:  band = "Promising (more data required)"
    elif score >= 50:  band = "Needs more data"
    else:              band = "Weak / insufficient evidence"

    return {
        "score":      score,
        "band":       band,
        "violations": violations,
        "verdict": (
            "Backtest evidence is sufficient to proceed to structured paper observation."
            if score >= 85 else
            "Backtest shows promise but requires more historical data or improved consistency."
            if score >= 70 else
            "Backtest evidence is too weak to proceed to paper trading. Iterate or gather more data."
        ),
    }


# ═══════════════════════════════════════════════════════════════════════════════
# Serialisation helpers
# ═══════════════════════════════════════════════════════════════════════════════

def _trade_to_dict(t: Trade) -> dict:
    return {
        "id":            t.id,
        "entryTime":     t.entry_time,
        "exitTime":      t.exit_time,
        "signal":        t.signal,
        "entry":         t.entry,
        "adjustedEntry": t.adjusted_entry,
        "stopLoss":      t.stop_loss,
        "takeProfit":    t.take_profit,
        "riskPoints":    t.risk_points,
        "targetPoints":  t.target_points,
        "rr":            t.rr,
        "result":        t.result,
        "points":        t.points,
        "rMultiple":     t.r_multiple,
        "equityBefore":  t.equity_before,
        "equityAfter":   t.equity_after,
        "riskAmount":    t.risk_amount,
        "pnlAmount":     t.pnl_amount,
        "session":       t.session,
        "score":         t.score,
        "reason":        t.reason,
        "barsHeld":      t.bars_held,
        "blockers":      t.blockers,
        "costs":         t.costs,
    }


def _skip_to_dict(s: SkippedTrade) -> dict:
    return {
        "time":   s.time,
        "signal": s.signal,
        "score":  s.score,
        "reason": s.reason,
        "detail": s.detail,
    }


def _standard_warnings(data_source: str) -> list[str]:
    base = [
        "Backtest does not guarantee future performance.",
        "Results depend on data quality.",
        "Spread and slippage are estimates.",
        "Candle-based testing may not reflect intrabar execution.",
        "Historical news data may be incomplete.",
        "Do not move to live trading based on backtest alone.",
        "Backtest is a filter, not proof.",
    ]
    if data_source == "synthetic":
        base.insert(0, "DATA SOURCE IS SYNTHETIC — results are illustrative only. "
                      "Import real historical XAU/USD candles via "
                      "POST /api/v1/backtest/import-csv for valid statistics.")
    return base
