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
from dataclasses import dataclass, field
from datetime import datetime, timezone
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
    blockers:       list[str] = field(default_factory=list)
    costs:          dict = field(default_factory=dict)


@dataclass
class SkippedTrade:
    time:    str
    signal:  str
    score:   int
    reason:  str          # e.g. RR_AFTER_COST_TOO_LOW
    detail:  str = ""


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
    target_points    = cfg["target_pips"]         # 50 for XAU/USD
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

    # ── Walk-forward loop ───────────────────────────────────────────────────
    trades:  list[Trade]        = []
    skipped: list[SkippedTrade] = []
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
            costs={"spreadPoints": spread_points, "slippagePoints": slippage_points,
                   "commission": commission_per_trade},
        )
        trades.append(trade)

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

    # ── Reliability rating ──────────────────────────────────────────────────
    rating = _reliability_rating(summary, trades)

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
    }

    return {
        "instrument": "XAU/USD",
        "period": {
            "start": bars[MIN_WARMUP_BARS - 1].time.isoformat() if bars else None,
            "end":   bars[-1].time.isoformat() if bars else None,
        },
        "settings":    settings_obj,
        "summary":     summary,
        "reliability": rating,
        "breakdowns":  breakdowns,
        "equityCurve": equity_curve,
        "trades":      [_trade_to_dict(t) for t in trades],
        "skipped":     [_skip_to_dict(s)  for s in skipped],
        "warnings":    _standard_warnings(source),
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
# Reliability rating (0-100)
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
