"""
Strategist backtester — validate the mandate confluence on real history.

Walks historical TradingView H1 bars chronologically. At each bar:
  1. Builds D1/H4/H1 windows up to that point
  2. Runs the candle-deterministic parts of the mandate confluence:
     - HTF EMA biases (D1, H4)
     - HTF-derived direction proposal
     - Sweep+reclaim detection (against prev-day H/L)
     - TF alignment classification (Strong/Extended/Conflicted/Neutral)
     - Session classification (mandate enum)
     - Market state classification
     - Execution-model letter (Model A confirmed iff sweep+reclaim)
     - 5-condition evaluation
     - ATR-anchored trade plan (entry / SL / TP1 / TP2)
  3. If conditions_passed >= 4 AND direction in (BUY, SELL):
     - Walks forward through next 24 H1 bars
     - Determines first hit: TP1+BE+stopped / TP2 win / full SL loss / timeout
  4. Aggregates outcomes

What this backtest CANNOT test (no historical data for these):
  • C4 macro alignment — DXY/yields/news weren't recorded per H1
  • ICT framework score — needs M15 series across full backtest window
  • Bridge/spread/cap gates — execution-side, not signal-side
  • Live MyFXBook sentiment

What this backtest DOES test:
  • C1 (TF alignment) — fully
  • C2 (Liquidity sweep) — fully
  • C3 (Structure/momentum) — partially (no ICT score, so passes if scanner state mocked = SIGNAL_READY)
  • C5 (RR + invalidation) — fully

Run inside the backend container:
  ssh doxau "docker exec -e PYTHONPATH=/app xauusd-backend python /app/tools/backtest_strategist.py"
"""
from __future__ import annotations

import sys
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from statistics import mean
from typing import Optional

from sqlalchemy import asc

# Strategist helpers — we reuse the exact same logic the live engine uses
from services.strategist import (
    _atr,
    _classify_execution_model,
    _classify_market_state_mandate,
    _classify_session_mandate,
    _classify_tf_alignment_mandate,
    _derive_direction_from_htf,
    _detect_liquidity_sweep,
    _ema,
    _evaluate_5_conditions,
    _generate_trade_plan,
    _htf_bias_label,
    _rsi,
    _NEVER_TRADE_SESSIONS,
)
from services.killzone_policy import evaluate as eval_kz_policy


# UTC hour → killzone_key (mirrors KILLZONES table in killzone_analyzer.py)
# Used to compute kz_policy per bar so the backtest exercises the new C2.
def _kz_key_for_hour(hour_utc: float) -> str:
    h = int(hour_utc)
    if   h >= 22:  return "asian_early"
    elif h < 6:    return "asian"
    elif h < 7:    return "london_pre"
    elif h < 10:   return "london_kz"
    elif h < 13:   return "overlap"
    elif h < 16:   return "ny_kz"
    else:          return "ny_pm"   # 16-22


# ── A minimal Bar that mimics what get_candles returns ──────────────────────
@dataclass
class Bar:
    time: datetime
    open: float
    high: float
    low: float
    close: float
    volume: int = 0


def load_bars(db, timeframe: str, start: Optional[datetime] = None,
              end: Optional[datetime] = None) -> list[Bar]:
    """Load HistoricalCandle rows as Bar objects, sorted ascending."""
    from db_models import HistoricalCandle
    q = db.query(HistoricalCandle).filter(HistoricalCandle.timeframe == timeframe)
    if start: q = q.filter(HistoricalCandle.candle_time >= start)
    if end:   q = q.filter(HistoricalCandle.candle_time <= end)
    q = q.order_by(asc(HistoricalCandle.candle_time))
    return [
        Bar(time=(r.candle_time if r.candle_time.tzinfo else r.candle_time.replace(tzinfo=timezone.utc)),
            open=r.open, high=r.high, low=r.low, close=r.close, volume=r.volume or 0)
        for r in q.all()
    ]


def simulate_outcome(
    direction: str, entry: float, sl: float, tp1: float, tp2: float,
    forward_bars: list[Bar],
) -> tuple[str, int, float]:
    """
    Walk forward through H1 bars to find first SL/TP hit.

    Models the engine's actual management:
      - Initial order: TP = tp2, SL = sl
      - When price reaches tp1 → SL moves to entry (breakeven)
      - If subsequently SL_effective (entry) is hit → BE_AFTER_TP1 (call it 0R)
      - If tp2 hit → win at +R-multiple
      - If SL hit before tp1 → full loss (-1R)
      - If neither in 24 bars → timeout (0R)

    Intra-bar ambiguity (both high and low extend past tp1+sl in same bar):
      assume worst-case — SL first for losers, TP for winners.
      Backtest noise, but consistent across all scenarios.
    """
    sl_eff   = sl
    tp1_hit  = False
    initial_risk = abs(entry - sl)

    for i, b in enumerate(forward_bars):
        # Check TP1 (raises SL to entry)
        if not tp1_hit:
            if direction == "BUY"  and b.high >= tp1: tp1_hit = True; sl_eff = entry
            if direction == "SELL" and b.low  <= tp1: tp1_hit = True; sl_eff = entry

        if direction == "BUY":
            if b.low <= sl_eff:
                if tp1_hit: return ("BE_AFTER_TP1", i, 0.0)
                else:       return ("LOSS",          i, -1.0)
            if b.high >= tp2:
                return ("WIN_TP2", i, (tp2 - entry) / initial_risk)
        else:   # SELL
            if b.high >= sl_eff:
                if tp1_hit: return ("BE_AFTER_TP1", i, 0.0)
                else:       return ("LOSS",          i, -1.0)
            if b.low <= tp2:
                return ("WIN_TP2", i, (entry - tp2) / initial_risk)

    return ("TIMEOUT", len(forward_bars), 0.0)


# ── Main backtest loop ──────────────────────────────────────────────────────

def main(min_bars_history: int = 50, forward_horizon: int = 24):
    """
    min_bars_history: warm-up — skip first N H1 bars so EMAs have history
    forward_horizon : H1 bars to look forward when simulating outcome (24 = 1 day)
    """
    from database import SessionLocal

    print("=" * 78)
    print(" STRATEGIST BACKTEST — real TradingView H1 bars")
    print("=" * 78)

    with SessionLocal() as db:
        h1_bars = load_bars(db, "H1")
        h4_bars = load_bars(db, "H4")
        d1_bars = load_bars(db, "D1")

    print(f"  Loaded H1: {len(h1_bars):>5} bars  ({h1_bars[0].time} → {h1_bars[-1].time})")
    print(f"  Loaded H4: {len(h4_bars):>5} bars")
    print(f"  Loaded D1: {len(d1_bars):>5} bars")
    print(f"  Warm-up: skipping first {min_bars_history} H1 bars (EMA history)")
    print(f"  Forward horizon for outcome: {forward_horizon} H1 bars")
    print()

    if len(h1_bars) <= min_bars_history + forward_horizon:
        print("Not enough H1 bars to run a meaningful backtest.")
        return

    triggers = 0
    by_cp:     dict[int, list[float]] = defaultdict(list)
    by_session: dict[str, list[float]] = defaultdict(list)
    by_kz_dir: dict[str, list[float]] = defaultdict(list)
    outcomes:  Counter = Counter()
    sample_trades: list[dict] = []      # keep first 5 of each outcome for spot-check

    # Iterate H1 bars from min_bars_history to len-forward_horizon
    for i in range(min_bars_history, len(h1_bars) - forward_horizon):
        bar = h1_bars[i]
        bar_t = bar.time

        # Build candle windows up to (and including) this bar
        h1_window = h1_bars[: i + 1]
        h4_window = [b for b in h4_bars if b.time <= bar_t]
        d1_window = [b for b in d1_bars if b.time <= bar_t]

        if len(d1_window) < 21 or len(h4_window) < 51:
            continue   # not enough HTF history

        # ── Compute everything the strategist would compute ─────────────
        h1_closes = [b.close for b in h1_window]
        ema20_h1  = _ema(h1_closes, 20)
        ema50_h1  = _ema(h1_closes, 50)
        rsi_h1_val = _rsi(h1_closes)   # real RSI so C1 STRONG-vs-Extended distinction works

        d1_bias = _htf_bias_label([b.close for b in d1_window], lookback=20)
        h4_bias = _htf_bias_label([b.close for b in h4_window], lookback=50)

        # HTF-derived direction (since no scanner/predictor in backtest)
        direction, _rationale = _derive_direction_from_htf(
            d1_bias=d1_bias, h4_bias=h4_bias,
            h1_ema20=ema20_h1, h1_ema50=ema50_h1,
        )
        if direction not in ("BUY", "SELL"):
            continue

        # Sweep detection (use H1 as M15 proxy — coarser but works)
        sweep = _detect_liquidity_sweep(candles_m15=h1_window, candles_d1=d1_window,
                                        lookback_m15_bars=8)

        # TF alignment label — real RSI now feeds Extended detection
        tf_label = _classify_tf_alignment_mandate(
            d1_bias=d1_bias, h4_bias=h4_bias,
            h1_ema20=ema20_h1, h1_ema50=ema50_h1,
            rsi_h1=rsi_h1_val,
        )

        # Session — pure function of UTC hour
        hour_utc = bar_t.hour + bar_t.minute / 60.0
        session = _classify_session_mandate(hour_utc=hour_utc, news_clear=True)

        # Skip bad sessions (mirrors C4 blacklist in live code)
        if session in _NEVER_TRADE_SESSIONS:
            continue

        # Skip Mondays (mirrors operator risk rule)
        if bar_t.weekday() == 0:
            continue

        # Execution model — Model A confirms when our sweep is detected+reclaimed
        # Pass scan=None-equivalent (empty); ict_score=70 as proxy (assume not misaligned)
        model_letter, model_confirmed, _, _ = _classify_execution_model(
            scan={}, ict=70, news_clear=True, sweep=sweep, ict_score=70,
        )

        # ATR + market state
        h1_highs  = [b.high for b in h1_window]
        h1_lows   = [b.low  for b in h1_window]
        atr_h1    = _atr(h1_highs, h1_lows, h1_closes, n=14)
        atr_baseline = atr_h1   # simple — no rolling baseline for backtest

        market_state = _classify_market_state_mandate(
            ema20_h1=ema20_h1, ema50_h1=ema50_h1, ema100_h1=_ema(h1_closes, 100),
            rsi_h1=50, atr_h1=atr_h1, atr_h1_baseline=atr_baseline,
            news_clear=True,
            scan_market_state="UNKNOWN", swept_recent=sweep.get("swept", False),
            kz_posture="TRADE",
        )

        # Generate trade plan using the live function — ATR-anchored
        # h1_ema20 wires in the pullback-zone gate (chase-entry rejection)
        current_price = bar.close
        plan = _generate_trade_plan(
            direction=direction, current_price=current_price, atr_h1=atr_h1,
            candles_m15=h1_window,    # use H1 as proxy
            h1_ema20=ema20_h1,
        )
        if plan["entry"] is None:
            continue

        # kz_policy verdict — used as C2 (empirical edge filter)
        kz_key = _kz_key_for_hour(hour_utc)
        kz_pol = eval_kz_policy(
            killzone_key=kz_key, direction=direction,
            engine_id="trend_pullback",   # match live engine — bypass=swing only
        )

        # 5-condition evaluation
        conditions = _evaluate_5_conditions(
            proposed_signal=direction,
            tf_alignment_label=tf_label,
            model_letter=model_letter,
            model_confirmed=model_confirmed,
            scan_market_state="SIGNAL_READY",  # mock — no scanner in backtest
            ict_score=70,                       # mock — no ICT module run per bar
            macro_alignment="Aligned",          # mock — no historical macro
            news_clear=True,
            kz_posture="TRADE",
            session_label=session,
            rr=plan["rr"],
            entry=plan["entry"], stop_loss=plan["stop_loss"],
            tp1=plan["tp1"], tp2=plan["tp2"],
            kz_policy=kz_pol,                   # exercises empirical C2
            candles_m15=h1_window,              # ← NEW: micro-momentum in C3 (H1 proxy)
        )
        cp = sum(1 for c in conditions if c["passed"])

        if cp < 4:
            continue

        triggers += 1
        # Walk forward
        forward = h1_bars[i + 1 : i + 1 + forward_horizon]
        outcome, bars_held, r_realized = simulate_outcome(
            direction=direction, entry=plan["entry"], sl=plan["stop_loss"],
            tp1=plan["tp1"], tp2=plan["tp2"], forward_bars=forward,
        )

        outcomes[outcome] += 1
        by_cp[cp].append(r_realized)
        by_session[session].append(r_realized)
        by_kz_dir[f"{session[:18]}_{direction}"].append(r_realized)

        # Keep first 3 per outcome type for spot-check
        if sum(1 for t in sample_trades if t["outcome"] == outcome) < 3:
            sample_trades.append({
                "when":     bar_t.isoformat(),
                "dir":      direction,
                "cp":       cp,
                "entry":    plan["entry"],
                "sl":       plan["stop_loss"],
                "tp1":      plan["tp1"],
                "tp2":      plan["tp2"],
                "outcome":  outcome,
                "bars":     bars_held,
                "R":        r_realized,
                "session":  session,
            })

    # ── REPORT ──────────────────────────────────────────────────────────
    print(f"  Triggers (4+/5 verdicts with valid plan): {triggers}")
    print()

    if triggers == 0:
        print("No triggers — backtest produced nothing to analyze. Likely too few D1/H4 bars,")
        print("or HTF derivation never settles on a direction with the available history.")
        return

    print("-- Outcome distribution --")
    total = sum(outcomes.values())
    for o, n in outcomes.most_common():
        pct = 100.0 * n / total
        print(f"  {o:14}  {n:>4}  ({pct:5.1f}%)")

    all_rs = [r for rs in by_cp.values() for r in rs]
    wins  = sum(1 for r in all_rs if r > 0)
    losses = sum(1 for r in all_rs if r < 0)
    bes   = sum(1 for r in all_rs if r == 0)
    wr = 100.0 * wins / len(all_rs) if all_rs else 0
    exp_r = mean(all_rs) if all_rs else 0
    print(f"\n  Overall: {len(all_rs)}t  {wins}W/{losses}L/{bes}BE  WR={wr:.1f}%  exp={exp_r:+.3f}R")

    # Empirical bands from prior 893-trade run (replaced mandate's optimistic 70-85%)
    EMPIRICAL = {5: "~19% WR · +0.09R", 4: "~19% WR · +0.05R"}
    print("\n-- By conditions_passed --")
    for cp in sorted(by_cp.keys(), reverse=True):
        rs = by_cp[cp]
        w = sum(1 for r in rs if r > 0)
        wr = 100.0 * w / len(rs)
        exp_r = mean(rs)
        band = f"prior: {EMPIRICAL[cp]}" if cp in EMPIRICAL else ""
        print(f"  {cp}/5  n={len(rs):>4}  WR={wr:5.1f}%  exp={exp_r:+.3f}R   {band}")

    print("\n-- By session --")
    for s, rs in sorted(by_session.items(), key=lambda x: -len(x[1])):
        w = sum(1 for r in rs if r > 0)
        wr = 100.0 * w / len(rs)
        exp_r = mean(rs)
        print(f"  {s:38}  n={len(rs):>3}  WR={wr:5.1f}%  exp={exp_r:+.3f}R")

    print("\n-- By session × direction --")
    for k, rs in sorted(by_kz_dir.items(), key=lambda x: -len(x[1])):
        if len(rs) < 3: continue
        w = sum(1 for r in rs if r > 0)
        wr = 100.0 * w / len(rs)
        exp_r = mean(rs)
        print(f"  {k:38}  n={len(rs):>3}  WR={wr:5.1f}%  exp={exp_r:+.3f}R")

    print("\n-- Sample trades (spot-check) --")
    for t in sample_trades:
        print(f"  {t['when'][:16]}  {t['dir']:4}  {t['cp']}/5  "
              f"E={t['entry']:.2f} SL={t['sl']:.2f} TP1={t['tp1']:.2f} TP2={t['tp2']:.2f}  "
              f"-> {t['outcome']:14} bars={t['bars']:>2} R={t['R']:+.2f}")


if __name__ == "__main__":
    main()
