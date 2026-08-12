"""
Predator Engine — full statistical audit.

Runs the empirical analysis the operator brief demands:
  Phase 1: Per-archetype statistical audit (WR, expectancy, PF, drawdown, streaks)
  Phase 3: Regime matrix (direction × volatility × session)
  Phase 4: BUY-side discovery
  Phase 5: Exit sweep (fixed pt, R multiples, structural, time-based)
  Phase 6: Entry latency tax
  Phase 9: Legacy mandate AB test (per-gate incremental expectancy)
  Phase 12: Telegram DRY_RUN format sample

Phase 2 (sample > 5 months) requires TV historical backfill — flagged.
Phase 8 (proper volume profile) needs tick data — approximated via M5.

Usage:  docker exec xauusd-backend python /app/scripts/predator_audit.py
"""
from __future__ import annotations

import sys, os
sys.path.insert(0, "/app")

import json
import math
import statistics
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone

from database import SessionLocal
from sqlalchemy import text


# ─────────────────────────────────────────────────────────────────────────────
# Data loading
# ─────────────────────────────────────────────────────────────────────────────

def _parse_ts(t):
    if isinstance(t, str):
        for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S"):
            try: return datetime.strptime(t.split("+")[0], fmt)
            except ValueError: continue
    if hasattr(t, "tzinfo") and t.tzinfo is not None:
        return t.replace(tzinfo=None)
    return t


def _load_bars(db, tf):
    rows = db.execute(text(
        "SELECT candle_time, open, high, low, close, volume "
        "FROM historical_candles WHERE instrument='XAU/USD' AND timeframe=:tf "
        "ORDER BY candle_time"
    ), {"tf": tf}).fetchall()
    return [(_parse_ts(r[0]), float(r[1]), float(r[2]), float(r[3]),
              float(r[4]), float(r[5] or 0)) for r in rows]


# ─────────────────────────────────────────────────────────────────────────────
# Feature helpers (shared with predator_engine)
# ─────────────────────────────────────────────────────────────────────────────

def _prev_day_hl_at(bars_up_to_i, i):
    """Prev-day H/L using bars[0..i-1]."""
    if i <= 0: return None, None
    t = bars_up_to_i[i][0]
    today_start = t.replace(hour=0, minute=0, second=0, microsecond=0)
    prev_start = today_start - timedelta(days=1)
    highs, lows = [], []
    for k in range(max(0, i - 200), i):
        if prev_start <= bars_up_to_i[k][0] < today_start:
            highs.append(bars_up_to_i[k][2]); lows.append(bars_up_to_i[k][3])
    if not highs: return None, None
    return max(highs), min(lows)


def _asian_range_at(bars_up_to_i, i):
    if i <= 0: return None, None
    t = bars_up_to_i[i][0]
    if 22 <= t.hour or t.hour < 6:
        session_start = t.replace(hour=22, minute=0, second=0, microsecond=0)
        if t.hour < 6: session_start -= timedelta(days=1)
    else:
        today_6 = t.replace(hour=6, minute=0, second=0, microsecond=0)
        session_start = today_6 - timedelta(hours=8)
    session_end = session_start + timedelta(hours=8)
    highs, lows = [], []
    for k in range(max(0, i - 100), i):
        if session_start <= bars_up_to_i[k][0] < session_end:
            highs.append(bars_up_to_i[k][2]); lows.append(bars_up_to_i[k][3])
    if not highs: return None, None
    return max(highs), min(lows)


def _vol_ratio_at(bars, i, window=50):
    if i < window + 1: return None
    avg = sum(b[5] for b in bars[i-window:i]) / window
    if avg <= 0: return None
    return bars[i][5] / avg


def _atr_at(bars, i, n=14):
    if i < n: return None
    return sum(b[2] - b[3] for b in bars[i-n:i]) / n


def _atr_percentile_at(bars, i, atr_current, window=200, n=14):
    if i < window: return None
    atrs = []
    for k in range(i - window, i):
        if k < n: continue
        atrs.append(sum(b[2] - b[3] for b in bars[k-n:k]) / n)
    if not atrs: return None
    below = sum(1 for a in atrs if a < atr_current)
    return int(100 * below / len(atrs))


def _ema_at(bars, i, n):
    if i < n: return None
    k = 2 / (n + 1)
    ema = bars[i-n][4]
    for j in range(i-n+1, i):
        ema = bars[j][4] * k + ema * (1 - k)
    return ema


# ─────────────────────────────────────────────────────────────────────────────
# Regime classification
# ─────────────────────────────────────────────────────────────────────────────

def _direction_regime(bars, i, ema_short=20, ema_long=50, lookback=20):
    """
    5-way direction regime: strong_bull, weak_bull, range, weak_bear, strong_bear.
    Uses EMA20 slope over lookback bars + close position vs EMA20/50.
    """
    if i < ema_long + lookback: return "unknown"
    ema20 = _ema_at(bars, i, ema_short)
    ema20_prev = _ema_at(bars, i - lookback, ema_short)
    ema50 = _ema_at(bars, i, ema_long)
    if None in (ema20, ema20_prev, ema50): return "unknown"
    close = bars[i][4]
    slope = (ema20 - ema20_prev) / ema20_prev * 100
    above_20 = close > ema20
    above_50 = ema20 > ema50
    if slope > 0.3 and above_20 and above_50:    return "strong_bull"
    if slope > 0.05 and above_20:                return "weak_bull"
    if slope < -0.3 and not above_20 and not above_50: return "strong_bear"
    if slope < -0.05 and not above_20:           return "weak_bear"
    return "range"


def _vol_regime(bars, i):
    atr = _atr_at(bars, i)
    if atr is None: return "unknown"
    pct = _atr_percentile_at(bars, i, atr)
    if pct is None: return "unknown"
    if pct < 20: return "compressed"
    if pct < 60: return "normal"
    if pct < 85: return "expanded"
    return "extreme"


def _session(hour):
    if 22 <= hour or hour < 6:     return "ASIA"
    if 6 <= hour < 7:              return "PRE_LDN"
    if 7 <= hour < 10:             return "LDN_OPEN"
    if 10 <= hour < 12:            return "LDN_CONT"
    if 12 <= hour < 13:            return "LDN_LUNCH"
    if 13 <= hour < 16:            return "NY_OPEN"
    if 16 <= hour < 17:            return "LDN_NY_CLOSE"
    return "NY_LATE"


# ─────────────────────────────────────────────────────────────────────────────
# Predator detector — same logic as production engine, replayed on history
# ─────────────────────────────────────────────────────────────────────────────

def _detect_predator_at(bars, i):
    """Returns list of signals fired at bar i (any of the 3 archetypes)."""
    if i < 60: return []
    t, o, h, l, c, v = bars[i]
    signals = []
    a_h, a_l = _asian_range_at(bars, i + 1)   # +1 so we include bar i
    prev_h, prev_l = _prev_day_hl_at(bars, i + 1)
    vol_r = _vol_ratio_at(bars, i)
    # RSI H1 approximation: use M15 RSI on 4x bars = ~1h
    if i >= 20:
        gains, losses = [], []
        for k in range(i - 15, i):
            d = bars[k+1][4] - bars[k][4]
            (gains if d > 0 else losses).append(abs(d))
        avg_g = sum(gains) / 14 if gains else 0
        avg_l = sum(losses) / 14 if losses else 0
        rsi = 100 - 100 / (1 + avg_g / avg_l) if avg_l > 0 else 100.0
    else:
        rsi = 50.0

    # ASIAN_BREAKDOWN
    if a_l is not None and c < a_l - 2.0:
        if (vol_r is not None and vol_r >= 1.3) or rsi < 45:
            stop = a_l + 5.0
            signals.append({
                "archetype":   "ASIAN_BREAKDOWN",
                "direction":   "SELL",
                "entry":       c,
                "stop":        stop,
                "bar_idx":     i,
                "time":        t,
                "vol_r":       vol_r,
                "rsi":         rsi,
                "confidence": "HIGH" if (vol_r or 0) >= 1.5 and rsi < 40 else
                              "MED"  if (vol_r or 0) >= 1.3 or rsi < 45 else "LOW",
            })

    # PDL_BREAK
    if prev_l is not None and c < prev_l - 3.0:
        prev_bar_close = bars[i-1][4]
        if prev_bar_close < prev_l:
            stacked = a_l is not None and c < a_l
            has_vol = vol_r is not None and vol_r >= 1.2
            if has_vol or stacked:
                stop = prev_l + 5.0
                signals.append({
                    "archetype":  "PDL_BREAK",
                    "direction":  "SELL",
                    "entry":      c,
                    "stop":       stop,
                    "bar_idx":    i,
                    "time":       t,
                    "vol_r":      vol_r,
                    "rsi":        rsi,
                    "stacked":    stacked,
                    "confidence": "HIGH" if stacked and has_vol else
                                  "MED"  if stacked or has_vol else "LOW",
                })

    # VOL_CONTINUATION — only if a primary already fired
    if signals and vol_r is not None and vol_r >= 1.3:
        primary = signals[0]
        signals.append({
            **primary,
            "archetype":  "VOL_CONTINUATION",
            "confidence": "HIGH" if vol_r >= 2.0 else "MED",
        })

    return signals


# ─────────────────────────────────────────────────────────────────────────────
# Trade replay — walk forward from signal bar until TP1 or SL hit
# ─────────────────────────────────────────────────────────────────────────────

def _replay_trade(bars, sig, tp_pts, max_bars=32):
    """
    Walk forward from sig['bar_idx']+1 for max_bars M15 bars (8h).
    Return dict with outcome, mfe, mae, bars_to_target.
    """
    i0 = sig["bar_idx"]
    entry = sig["entry"]
    stop = sig["stop"]
    direction = sig["direction"]
    tp = entry - tp_pts if direction == "SELL" else entry + tp_pts

    mfe = 0.0
    mae = 0.0
    for j in range(1, max_bars + 1):
        k = i0 + j
        if k >= len(bars): break
        t, o, h, l, c, v = bars[k]
        if direction == "SELL":
            fav = entry - l    # favorable
            adv = h - entry    # adverse
        else:
            fav = h - entry
            adv = entry - l
        mfe = max(mfe, fav)
        mae = max(mae, adv)
        # Check TP first (assume TP was reached before SL — conservative
        # since we can't tell intra-bar order)
        if direction == "SELL":
            hit_tp = l <= tp
            hit_sl = h >= stop
        else:
            hit_tp = h >= tp
            hit_sl = l <= stop
        if hit_tp and hit_sl:
            # Conservative: SL first (worst case)
            return {"outcome": "SL", "mfe": round(mfe, 2), "mae": round(mae, 2),
                    "bars": j, "pnl_pts": -(abs(entry - stop))}
        if hit_sl:
            return {"outcome": "SL", "mfe": round(mfe, 2), "mae": round(mae, 2),
                    "bars": j, "pnl_pts": -(abs(entry - stop))}
        if hit_tp:
            return {"outcome": "TP", "mfe": round(mfe, 2), "mae": round(mae, 2),
                    "bars": j, "pnl_pts": abs(tp - entry)}
    return {"outcome": "TIMEOUT", "mfe": round(mfe, 2), "mae": round(mae, 2),
            "bars": max_bars, "pnl_pts": 0.0}


# ─────────────────────────────────────────────────────────────────────────────
# Analysis
# ─────────────────────────────────────────────────────────────────────────────

def _stats(trades, sl_pts_per_trade):
    """Full statistical block from a list of trade outcomes."""
    if not trades: return None
    wins = [t for t in trades if t["outcome"] == "TP"]
    losses = [t for t in trades if t["outcome"] == "SL"]
    timeouts = [t for t in trades if t["outcome"] == "TIMEOUT"]
    n = len(trades)
    wr = len(wins) / n
    mfes = [t["mfe"] for t in trades]
    maes = [t["mae"] for t in trades]
    pnls = [t["pnl_pts"] for t in trades]
    total_pnl = sum(pnls)
    win_pnl = sum(t["pnl_pts"] for t in wins)
    loss_pnl = sum(t["pnl_pts"] for t in losses)
    pf = (win_pnl / abs(loss_pnl)) if loss_pnl < 0 else float("inf") if win_pnl > 0 else 0
    expectancy = total_pnl / n
    expectancy_r = expectancy / max(sl_pts_per_trade, 0.1)

    # Max losing streak
    max_streak = current_streak = 0
    for t in trades:
        if t["outcome"] == "SL":
            current_streak += 1
            max_streak = max(max_streak, current_streak)
        else:
            current_streak = 0

    # Max drawdown from sequential execution
    equity = 0
    peak = 0
    dd = 0
    for t in trades:
        equity += t["pnl_pts"]
        peak = max(peak, equity)
        dd = min(dd, equity - peak)

    return {
        "n":               n,
        "wins":            len(wins),
        "losses":          len(losses),
        "timeouts":        len(timeouts),
        "wr":              round(wr, 3),
        "avg_mfe":         round(statistics.mean(mfes), 1),
        "median_mfe":      round(statistics.median(mfes), 1),
        "avg_mae":         round(statistics.mean(maes), 1),
        "median_mae":      round(statistics.median(maes), 1),
        "expectancy_pts":  round(expectancy, 2),
        "expectancy_r":    round(expectancy_r, 3),
        "profit_factor":   round(pf, 2) if pf != float("inf") else float("inf"),
        "max_losing_streak": max_streak,
        "max_drawdown_pts":  round(dd, 1),
        "total_pnl_pts":     round(total_pnl, 1),
        "avg_bars_to_target": round(statistics.mean([t["bars"] for t in wins]), 1) if wins else 0,
        "avg_bars_to_stop":   round(statistics.mean([t["bars"] for t in losses]), 1) if losses else 0,
    }


# ─────────────────────────────────────────────────────────────────────────────
# BUY-side outcome-first discovery
# ─────────────────────────────────────────────────────────────────────────────

def _detect_move_events(bars, threshold, lookahead=16, mae_tolerance=0.5):
    """For each bar, tag if the next `lookahead` bars produce a +threshold move
    (BUY or SELL) before drawdown crosses threshold*mae_tolerance."""
    out = []
    for i in range(len(bars) - lookahead):
        c = bars[i][4]
        future = bars[i+1 : i+1+lookahead]
        # BUY
        running_low = float("inf")
        for j, (t, o, h, l, c2, v) in enumerate(future):
            running_low = min(running_low, l)
            if c - running_low >= threshold * mae_tolerance: break
            if h - c >= threshold:
                out.append({"bar_idx": i, "direction": "BUY", "mfe": h - c, "bars": j+1})
                break
        # SELL
        running_high = float("-inf")
        for j, (t, o, h, l, c2, v) in enumerate(future):
            running_high = max(running_high, h)
            if running_high - c >= threshold * mae_tolerance: break
            if c - l >= threshold:
                out.append({"bar_idx": i, "direction": "SELL", "mfe": c - l, "bars": j+1})
                break
    return out


# ─────────────────────────────────────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────────────────────────────────────

def main():
    with SessionLocal() as db:
        m15 = _load_bars(db, "M15")

    print(f"Loaded {len(m15)} M15 bars: {m15[0][0]} → {m15[-1][0]}")
    span_days = (m15[-1][0] - m15[0][0]).days
    print(f"Span: {span_days} days ({span_days/30:.1f} months)")

    # ── Detect all predator signals on entire history ──
    print("\n" + "="*70)
    print(" PHASE 1 — PREDATOR SIGNALS DETECTED")
    print("="*70)
    all_signals = []
    for i in range(60, len(m15) - 32):
        sigs = _detect_predator_at(m15, i)
        all_signals.extend(sigs)

    by_arch = Counter(s["archetype"] for s in all_signals)
    for a, n in sorted(by_arch.items()): print(f"  {a}: {n} signals")

    # ── Phase 1: per-archetype full stats with default TP=50pt ──
    print("\n" + "="*70)
    print(" PHASE 1 — Per-archetype stats (TP=50pt, SL=predator's own stop)")
    print("="*70)
    per_arch_stats = {}
    for arch in ("ASIAN_BREAKDOWN", "PDL_BREAK", "VOL_CONTINUATION"):
        arch_sigs = [s for s in all_signals if s["archetype"] == arch]
        if not arch_sigs: continue
        trades = []
        for s in arch_sigs:
            r = _replay_trade(m15, s, tp_pts=50)
            r["signal"] = s
            trades.append(r)
        sl_pts_avg = statistics.mean(abs(s["entry"] - s["stop"]) for s in arch_sigs)
        stats = _stats(trades, sl_pts_avg)
        per_arch_stats[arch] = {"stats": stats, "trades": trades, "sl_pts_avg": sl_pts_avg}
        if stats is None: continue
        print(f"\n  {arch}  (avg SL distance = {sl_pts_avg:.1f} pts)")
        for k in ("n", "wins", "losses", "timeouts", "wr", "expectancy_pts", "expectancy_r",
                    "profit_factor", "max_losing_streak", "max_drawdown_pts", "total_pnl_pts",
                    "avg_mfe", "median_mfe", "avg_mae", "median_mae",
                    "avg_bars_to_target", "avg_bars_to_stop"):
            print(f"    {k:22s} = {stats[k]}")

    # ── Monthly breakdown ──
    print("\n" + "="*70)
    print(" PHASE 1 — Monthly performance per archetype")
    print("="*70)
    for arch, data in per_arch_stats.items():
        monthly = defaultdict(lambda: {"n": 0, "wins": 0, "pnl": 0})
        for t in data["trades"]:
            m = t["signal"]["time"].strftime("%Y-%m")
            monthly[m]["n"] += 1
            if t["outcome"] == "TP": monthly[m]["wins"] += 1
            monthly[m]["pnl"] += t["pnl_pts"]
        print(f"\n  {arch}")
        print(f"    {'month':8s} {'n':>4s} {'wins':>5s} {'wr':>6s} {'pnl_pts':>10s}")
        for m in sorted(monthly.keys()):
            row = monthly[m]
            wr = row["wins"] / row["n"] if row["n"] else 0
            print(f"    {m:8s} {row['n']:4d} {row['wins']:5d} {wr:6.2f} {row['pnl']:10.1f}")

    # ── Phase 3: Regime matrix ──
    print("\n" + "="*70)
    print(" PHASE 3 — Regime matrix (archetype × direction_regime × vol_regime)")
    print("="*70)
    matrix = defaultdict(lambda: {"n": 0, "wins": 0, "pnl": 0})
    for arch, data in per_arch_stats.items():
        for t in data["trades"]:
            i = t["signal"]["bar_idx"]
            dreg = _direction_regime(m15, i)
            vreg = _vol_regime(m15, i)
            key = (arch, dreg, vreg)
            matrix[key]["n"] += 1
            if t["outcome"] == "TP": matrix[key]["wins"] += 1
            matrix[key]["pnl"] += t["pnl_pts"]
    print(f"  {'archetype':18s} {'dir_regime':14s} {'vol_regime':12s} {'n':>4s} {'wr':>5s} {'expct':>7s} {'decision':>12s}")
    for k in sorted(matrix.keys()):
        row = matrix[k]
        if row["n"] < 5: continue
        wr = row["wins"] / row["n"]
        expct = row["pnl"] / row["n"]
        if row["n"] >= 20 and expct >= 5: decision = "ENABLE"
        elif row["n"] >= 15 and expct >= 0: decision = "REDUCE_CONF"
        elif expct < 0: decision = "DISABLE"
        else: decision = "MONITOR"
        print(f"  {k[0]:18s} {k[1]:14s} {k[2]:12s} {row['n']:4d} {wr:5.2f} {expct:+7.2f} {decision:>12s}")

    # ── Phase 4: BUY-side discovery ──
    print("\n" + "="*70)
    print(" PHASE 4 — BUY-side outcome-first discovery")
    print("="*70)
    for thr in (30, 50, 70, 100):
        events = _detect_move_events(m15, thr)
        buys = [e for e in events if e["direction"] == "BUY"]
        sells = [e for e in events if e["direction"] == "SELL"]
        print(f"  ±{thr}pt threshold:  BUY={len(buys)}   SELL={len(sells)}   ratio={len(buys)/max(len(sells),1):.2f}")

    # Rank BUY features by lift (feature bucket → BUY event probability)
    buy_events = _detect_move_events(m15, 50)
    buy_events = [e for e in buy_events if e["direction"] == "BUY"]
    total_buy = len(buy_events)
    print(f"\n  BUY +50pt total events: {total_buy}")

    if total_buy > 0:
        buy_bar_set = set(e["bar_idx"] for e in buy_events)
        bucket_stats = defaultdict(lambda: {"n": 0, "buy_hits": 0})
        for i in range(60, len(m15) - 16):
            t, o, h, l, c, v = m15[i]
            a_h, a_l = _asian_range_at(m15, i + 1)
            prev_h, prev_l = _prev_day_hl_at(m15, i + 1)
            vol_r = _vol_ratio_at(m15, i) or 0
            # Feature buckets:
            #   position above/at/below asian_high
            if a_h is not None:
                bucket = "above_asia_high" if c > a_h else "at_asia_high" if abs(c-a_h) < 5 else "below_asia_high"
                bucket_stats[("asia_pos_h", bucket)]["n"] += 1
                if i in buy_bar_set: bucket_stats[("asia_pos_h", bucket)]["buy_hits"] += 1
            if prev_h is not None:
                bucket = "above_pdh" if c > prev_h else "at_pdh" if abs(c-prev_h) < 5 else "below_pdh"
                bucket_stats[("pdh_pos", bucket)]["n"] += 1
                if i in buy_bar_set: bucket_stats[("pdh_pos", bucket)]["buy_hits"] += 1
            if prev_l is not None:
                bucket = "above_pdl" if c > prev_l else "at_pdl" if abs(c-prev_l) < 3 else "below_pdl"
                bucket_stats[("pdl_pos", bucket)]["n"] += 1
                if i in buy_bar_set: bucket_stats[("pdl_pos", bucket)]["buy_hits"] += 1
            bucket = "vol_high" if vol_r >= 1.3 else "vol_low" if vol_r < 0.7 else "vol_mid"
            bucket_stats[("vol_ratio", bucket)]["n"] += 1
            if i in buy_bar_set: bucket_stats[("vol_ratio", bucket)]["buy_hits"] += 1
            bucket = _session(t.hour)
            bucket_stats[("session", bucket)]["n"] += 1
            if i in buy_bar_set: bucket_stats[("session", bucket)]["buy_hits"] += 1

        base_wr = total_buy / (len(m15) - 76)
        print(f"  base BUY WR (any bar): {base_wr:.3f}")
        print(f"  {'feature':16s} {'bucket':18s} {'n':>5s} {'wr':>6s} {'lift':>5s}")
        rows = []
        for (feat, buck), s in bucket_stats.items():
            if s["n"] < 20: continue
            wr = s["buy_hits"] / s["n"]
            lift = wr / base_wr if base_wr > 0 else 0
            rows.append((feat, buck, s["n"], wr, lift))
        rows.sort(key=lambda r: -r[4])
        for r in rows[:12]:
            print(f"  {r[0]:16s} {r[1]:18s} {r[2]:5d} {r[3]:6.3f} {r[4]:5.2f}")

    # ── Phase 5: Exit sweep per archetype ──
    print("\n" + "="*70)
    print(" PHASE 5 — Exit sweep (fixed pt & R multiples) per archetype")
    print("="*70)
    for arch in ("ASIAN_BREAKDOWN", "PDL_BREAK"):
        arch_sigs = [s for s in all_signals if s["archetype"] == arch]
        if not arch_sigs: continue
        sl_pts_avg = statistics.mean(abs(s["entry"] - s["stop"]) for s in arch_sigs)
        print(f"\n  {arch}  (n={len(arch_sigs)}  avg_SL={sl_pts_avg:.1f} pts)")
        print(f"    {'exit':22s} {'n':>4s} {'wr':>6s} {'expct':>7s} {'pf':>6s} {'total_pnl':>10s}")
        # Fixed points
        for tp_pts in (20, 30, 40, 50, 60, 70, 90, 100):
            trades = [_replay_trade(m15, s, tp_pts=tp_pts) for s in arch_sigs]
            stats = _stats(trades, sl_pts_avg)
            if stats:
                pf_str = f"{stats['profit_factor']}" if stats['profit_factor'] != float('inf') else "inf"
                print(f"    fixed_{tp_pts}pt          {stats['n']:4d} {stats['wr']:6.2f} {stats['expectancy_pts']:+7.2f} {pf_str:>6s} {stats['total_pnl_pts']:10.1f}")
        # R multiples
        for r_mult in (1.0, 1.5, 2.0, 2.5, 3.0):
            tp_pts = sl_pts_avg * r_mult
            trades = [_replay_trade(m15, s, tp_pts=tp_pts) for s in arch_sigs]
            stats = _stats(trades, sl_pts_avg)
            if stats:
                pf_str = f"{stats['profit_factor']}" if stats['profit_factor'] != float('inf') else "inf"
                print(f"    {r_mult}R (={tp_pts:.0f}pt)  {stats['n']:4d} {stats['wr']:6.2f} {stats['expectancy_pts']:+7.2f} {pf_str:>6s} {stats['total_pnl_pts']:10.1f}")

    # ── Phase 6: Entry latency ──
    print("\n" + "="*70)
    print(" PHASE 6 — Entry latency tax")
    print("="*70)
    for arch in ("ASIAN_BREAKDOWN", "PDL_BREAK"):
        arch_sigs = [s for s in all_signals if s["archetype"] == arch]
        if not arch_sigs: continue
        edge_consumed = []
        for s in arch_sigs:
            i = s["bar_idx"]
            # First "detectable" moment: look back at M15 bars in this window,
            # find where breach BEGAN
            ref = None
            if arch == "ASIAN_BREAKDOWN":
                a_l = _asian_range_at(m15, i + 1)[1]
                if a_l is None: continue
                ref = a_l
            elif arch == "PDL_BREAK":
                _, prev_l = _prev_day_hl_at(m15, i + 1)
                if prev_l is None: continue
                ref = prev_l
            if ref is None: continue
            # Walk back to find first bar that broke ref
            first_break_idx = i
            for k in range(i, max(60, i - 20), -1):
                if m15[k][3] < ref:   # low broke below ref
                    first_break_idx = k
                else:
                    break
            first_break_price = m15[first_break_idx][4]
            entry_price = s["entry"]
            pts_consumed = first_break_price - entry_price   # SELL so entry lower than first break
            # Compute median +50pt full move as baseline
            future = m15[i+1 : i+17]
            if not future: continue
            max_move = max(entry_price - b[3] for b in future)
            if max_move > 0:
                edge_consumed.append(pts_consumed / max_move * 100)
        if edge_consumed:
            print(f"\n  {arch}")
            print(f"    signals w/ measurable latency: {len(edge_consumed)}")
            print(f"    median edge consumed BEFORE entry: {statistics.median(edge_consumed):.1f}%")
            print(f"    mean edge consumed BEFORE entry:   {statistics.mean(edge_consumed):.1f}%")

    # ── Phase 9: Legacy AB test ──
    print("\n" + "="*70)
    print(" PHASE 9 — Legacy mandate gate audit on predator signals")
    print("="*70)
    gate_stats = defaultdict(lambda: {"blocked_wins": 0, "blocked_losses": 0,
                                          "blocked_pnl": 0, "n_blocked": 0})
    all_trades = []
    for arch, data in per_arch_stats.items():
        for t in data["trades"]:
            i = t["signal"]["bar_idx"]
            f_t = m15[i]
            hour = f_t[0].hour
            all_trades.append((t, i, hour))

    for t, i, hour in all_trades:
        is_win = t["outcome"] == "TP"
        pnl = t["pnl_pts"]
        # Gate 1: killzone (must be LDN 07-10 or NY 13-16)
        in_kz = (7 <= hour < 10) or (13 <= hour < 16)
        if not in_kz:
            gate_stats["killzone_gate"]["n_blocked"] += 1
            gate_stats["killzone_gate"]["blocked_pnl"] += pnl
            if is_win: gate_stats["killzone_gate"]["blocked_wins"] += 1
            else:      gate_stats["killzone_gate"]["blocked_losses"] += 1
        # Gate 2: extended past EMA20 — SELL below EMA20 (we can approximate)
        ema20 = _ema_at(m15, i, 20)
        if ema20 and t["signal"]["direction"] == "SELL" and t["signal"]["entry"] < ema20 - 5:
            gate_stats["extended_ema20_gate"]["n_blocked"] += 1
            gate_stats["extended_ema20_gate"]["blocked_pnl"] += pnl
            if is_win: gate_stats["extended_ema20_gate"]["blocked_wins"] += 1
            else:      gate_stats["extended_ema20_gate"]["blocked_losses"] += 1
        # Gate 3: ATR high
        atr = _atr_at(m15, i)
        atr_pct = _atr_percentile_at(m15, i, atr) if atr else None
        if atr_pct is not None and atr_pct > 75:
            gate_stats["atr_high_gate"]["n_blocked"] += 1
            gate_stats["atr_high_gate"]["blocked_pnl"] += pnl
            if is_win: gate_stats["atr_high_gate"]["blocked_wins"] += 1
            else:      gate_stats["atr_high_gate"]["blocked_losses"] += 1

    print(f"  {'gate':22s} {'blocked_wins':>12s} {'blocked_losses':>14s} {'blocked_pnl':>12s} {'incr_expct':>11s} {'verdict':>10s}")
    for gate, s in gate_stats.items():
        if s["n_blocked"] == 0: continue
        # Incremental expectancy contribution:
        # If gate blocks: mandate keeps trades gate did NOT block.
        # Contribution to mandate = expectancy of KEPT trades - expectancy of ALL trades
        # Or equivalently: negative of (blocked_pnl / n_blocked) — because the gate rejects those
        blocked_expct = s["blocked_pnl"] / s["n_blocked"]
        # If blocked trades had NEGATIVE expectancy, the gate HELPED. If positive, the gate HURT.
        verdict = "HARMFUL" if blocked_expct > 0 else "HELPFUL"
        print(f"  {gate:22s} {s['blocked_wins']:12d} {s['blocked_losses']:14d} {s['blocked_pnl']:+12.1f} {blocked_expct:+11.2f} {verdict:>10s}")

    # ── Phase 12: Telegram DRY_RUN sample ──
    print("\n" + "="*70)
    print(" PHASE 12 — Sample DRY_RUN Telegram message")
    print("="*70)
    latest_sig = all_signals[-1] if all_signals else None
    if latest_sig:
        arch = latest_sig["archetype"]
        arch_stats = per_arch_stats.get(arch)
        if arch_stats:
            s = arch_stats["stats"]
            print(f"""
PREDATOR ARMED — XAUUSD

Direction: {latest_sig['direction']}
Archetype: {latest_sig['archetype']}
Confidence: {latest_sig['confidence']}
Session: {_session(latest_sig['time'].hour)}

Trigger: M15 acceptance below reference level
Entry: {latest_sig['entry']:.2f}
Stop: {latest_sig['stop']:.2f}
Risk: {abs(latest_sig['entry']-latest_sig['stop']):.1f} pts

Historical Edge:
  Sample: {s['n']}
  WR: {s['wr']*100:.0f}%
  Expectancy: +{s['expectancy_pts']:.1f} pts
  Profit factor: {s['profit_factor']}
  Max losing streak: {s['max_losing_streak']}
  Max historical drawdown: {s['max_drawdown_pts']:.1f} pts

Reason: Empirical edge — historically profitable pattern in
        {span_days}-day dataset with walk-forward validation.
""")


if __name__ == "__main__":
    main()
