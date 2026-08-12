"""
Predator LATENCY & EXECUTABILITY audit.
========================================

Answers the operator brief question: are the discovered edges actually
EXECUTABLE at the timestamps the production engine detects them?

Pattern edge (what happens after condition becomes true) ≠ Executable edge
(what remains after production could realistically confirm & enter).

For every historical predator signal we compute:
  • setup ORIGIN timestamp   — first M5 bar where the level was broken
  • production ENTRY timestamp — M15 close confirmation (current engine)
  • M5-close entry timestamp  — variant B (fastest reasonable alternative)
  • total MFE from origin
  • remaining MFE from each entry point
  • target reachability from executable entries
  • latency in bars, points, and % of move consumed

Then we recompute ALL performance stats from the EXECUTABLE entry price,
run retest / extension-filter variants, and answer the 6 required questions.

Usage: docker exec xauusd-backend python /app/scripts/predator_latency_audit.py
"""
from __future__ import annotations

import sys, os
sys.path.insert(0, "/app")

import statistics
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from typing import Optional

from database import SessionLocal
from sqlalchemy import text


# ─────────────────────────────────────────────────────────────────────────────
# Data
# ─────────────────────────────────────────────────────────────────────────────

def _parse_ts(t):
    if isinstance(t, str):
        for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S"):
            try: return datetime.strptime(t.split("+")[0], fmt)
            except ValueError: continue
    return t


def _load_bars(db, tf):
    rows = db.execute(text(
        "SELECT candle_time, open, high, low, close, volume "
        "FROM historical_candles WHERE instrument='XAU/USD' AND timeframe=:tf "
        "ORDER BY candle_time"
    ), {"tf": tf}).fetchall()
    out = []
    for r in rows:
        t = _parse_ts(r[0])
        if hasattr(t, "tzinfo") and t.tzinfo is not None:
            t = t.replace(tzinfo=None)
        out.append((t, float(r[1]), float(r[2]), float(r[3]),
                     float(r[4]), float(r[5] or 0)))
    return out


# ─────────────────────────────────────────────────────────────────────────────
# Levels (same as predator_engine)
# ─────────────────────────────────────────────────────────────────────────────

def _prev_day_hl_at(bars, i):
    if i <= 0: return None, None
    t = bars[i][0]
    today_start = t.replace(hour=0, minute=0, second=0, microsecond=0)
    prev_start = today_start - timedelta(days=1)
    highs, lows = [], []
    for k in range(max(0, i - 200), i):
        if prev_start <= bars[k][0] < today_start:
            highs.append(bars[k][2]); lows.append(bars[k][3])
    if not highs: return None, None
    return max(highs), min(lows)


def _asian_range_at(bars, i):
    if i <= 0: return None, None
    t = bars[i][0]
    if 22 <= t.hour or t.hour < 6:
        session_start = t.replace(hour=22, minute=0, second=0, microsecond=0)
        if t.hour < 6: session_start -= timedelta(days=1)
    else:
        today_6 = t.replace(hour=6, minute=0, second=0, microsecond=0)
        session_start = today_6 - timedelta(hours=8)
    session_end = session_start + timedelta(hours=8)
    highs, lows = [], []
    for k in range(max(0, i - 100), i):
        if session_start <= bars[k][0] < session_end:
            highs.append(bars[k][2]); lows.append(bars[k][3])
    if not highs: return None, None
    return max(highs), min(lows)


def _vol_ratio_at(bars, i, window=50):
    if i < window + 1: return None
    avg = sum(b[5] for b in bars[i-window:i]) / window
    if avg <= 0: return None
    return bars[i][5] / avg


def _rsi_at(bars, i, n=14):
    if i < n + 1: return None
    gains, losses = [], []
    for k in range(i-n, i):
        d = bars[k+1][4] - bars[k][4] if k+1 < len(bars) else 0
        (gains if d > 0 else losses).append(abs(d))
    if not gains and not losses: return 50.0
    avg_g = sum(gains) / n if gains else 0
    avg_l = sum(losses) / n if losses else 0
    if avg_l == 0: return 100.0
    return 100 - 100 / (1 + avg_g / avg_l)


# ─────────────────────────────────────────────────────────────────────────────
# Predator signal detection (M15 close — production engine)
# ─────────────────────────────────────────────────────────────────────────────

def _detect_predator_m15(bars_m15):
    """Return list of predator signals (production M15-close logic)."""
    out = []
    for i in range(60, len(bars_m15) - 32):
        t, o, h, l, c, v = bars_m15[i]
        a_h, a_l = _asian_range_at(bars_m15, i + 1)
        prev_h, prev_l = _prev_day_hl_at(bars_m15, i + 1)
        vol_r = _vol_ratio_at(bars_m15, i)
        rsi = _rsi_at(bars_m15, i)

        # ASIAN_BREAKDOWN
        if a_l is not None and c < a_l - 2.0:
            if (vol_r is not None and vol_r >= 1.3) or (rsi is not None and rsi < 45):
                out.append({
                    "archetype":   "ASIAN_BREAKDOWN",
                    "direction":   "SELL",
                    "level":       a_l,
                    "entry_prod":  c,             # production entry = M15 close
                    "stop":        a_l + 5.0,
                    "bar_idx":     i,
                    "time_prod":   t,
                    "vol_r":       vol_r,
                    "rsi":         rsi,
                })

        # PDL_BREAK
        if prev_l is not None and c < prev_l - 3.0:
            if bars_m15[i-1][4] < prev_l:
                a_l_ = _asian_range_at(bars_m15, i + 1)[1]
                stacked = a_l_ is not None and c < a_l_
                has_vol = vol_r is not None and vol_r >= 1.2
                if has_vol or stacked:
                    out.append({
                        "archetype":   "PDL_BREAK",
                        "direction":   "SELL",
                        "level":       prev_l,
                        "entry_prod":  c,
                        "stop":        prev_l + 5.0,
                        "bar_idx":     i,
                        "time_prod":   t,
                        "vol_r":       vol_r,
                        "rsi":         rsi,
                    })
    return out


# ─────────────────────────────────────────────────────────────────────────────
# Find setup ORIGIN in M5 (first M5 bar breaking the level)
# ─────────────────────────────────────────────────────────────────────────────

def _find_m5_origin(m5_bars, level: float, prod_time: datetime,
                       lookback_hours: int = 4) -> Optional[dict]:
    """
    Walk M5 bars up to prod_time. Find the first bar where low crossed below
    `level` within `lookback_hours` before prod_time. Return {time, price}
    of that first-break bar, or None.
    """
    window_start = prod_time - timedelta(hours=lookback_hours)
    for i, (t, o, h, l, c, v) in enumerate(m5_bars):
        if t < window_start: continue
        if t >= prod_time: break
        if l < level:
            return {"time": t, "price": c, "low": l, "idx": i}
    return None


def _find_m5_close_entry(m5_bars, level: float, prod_time: datetime,
                            lookback_hours: int = 4) -> Optional[dict]:
    """
    Variant B: first M5 CLOSE below level (not just intra-bar low).
    This is what an M5-based confirmation would trigger on.
    """
    window_start = prod_time - timedelta(hours=lookback_hours)
    for i, (t, o, h, l, c, v) in enumerate(m5_bars):
        if t < window_start: continue
        if t >= prod_time: break
        if c < level:
            return {"time": t, "price": c, "close": c, "idx": i}
    return None


# ─────────────────────────────────────────────────────────────────────────────
# Trade replay from a given entry price (with SL from the level)
# ─────────────────────────────────────────────────────────────────────────────

def _replay_from(m15_bars, start_idx, entry_price, stop_price, direction,
                    tp1_pts, tp2_pts, max_bars=32):
    """Walk M15 forward from start_idx. Return {outcome, mfe, mae, hit_tp1, hit_tp2}."""
    mfe = 0.0
    mae = 0.0
    hit_tp1 = False
    hit_tp2 = False
    tp1 = entry_price - tp1_pts if direction == "SELL" else entry_price + tp1_pts
    tp2 = entry_price - tp2_pts if direction == "SELL" else entry_price + tp2_pts
    for j in range(1, max_bars + 1):
        k = start_idx + j
        if k >= len(m15_bars): break
        t, o, h, l, c, v = m15_bars[k]
        if direction == "SELL":
            fav = entry_price - l
            adv = h - entry_price
            hit_sl = h >= stop_price
            hit_tp1_now = l <= tp1
            hit_tp2_now = l <= tp2
        else:
            fav = h - entry_price
            adv = entry_price - l
            hit_sl = l <= stop_price
            hit_tp1_now = h >= tp1
            hit_tp2_now = h >= tp2
        mfe = max(mfe, fav)
        mae = max(mae, adv)
        if hit_tp1_now: hit_tp1 = True
        if hit_tp2_now: hit_tp2 = True
        if hit_sl:
            return {"outcome": "SL", "mfe": round(mfe, 2), "mae": round(mae, 2),
                     "hit_tp1": hit_tp1, "hit_tp2": hit_tp2,
                     "pnl_pts": -(abs(entry_price - stop_price))}
        # Resolve TP if hit
        if hit_tp1_now:
            # Assume close at TP1 (single-target for stats)
            return {"outcome": "TP", "mfe": round(mfe, 2), "mae": round(mae, 2),
                     "hit_tp1": True, "hit_tp2": hit_tp2,
                     "pnl_pts": tp1_pts}
    return {"outcome": "TIMEOUT", "mfe": round(mfe, 2), "mae": round(mae, 2),
             "hit_tp1": hit_tp1, "hit_tp2": hit_tp2,
             "pnl_pts": 0.0}


# ─────────────────────────────────────────────────────────────────────────────
# Statistics helpers
# ─────────────────────────────────────────────────────────────────────────────

def _percentiles(values, points=(25, 50, 75, 90)):
    if not values: return {p: None for p in points}
    s = sorted(values)
    n = len(s)
    out = {}
    for p in points:
        idx = int(round((p / 100) * (n - 1)))
        out[p] = s[max(0, min(idx, n - 1))]
    return out


def _basic_stats(trades):
    if not trades: return None
    wins = [t for t in trades if t["outcome"] == "TP"]
    losses = [t for t in trades if t["outcome"] == "SL"]
    n = len(trades)
    wr = len(wins) / n
    win_pnl = sum(t["pnl_pts"] for t in wins)
    loss_pnl = sum(t["pnl_pts"] for t in losses)
    pf = (win_pnl / abs(loss_pnl)) if loss_pnl < 0 else 999.99 if win_pnl > 0 else 0
    pnls = [t["pnl_pts"] for t in trades]
    expct = sum(pnls) / n
    mfes = [t["mfe"] for t in trades]
    maes = [t["mae"] for t in trades]
    # Max DD
    equity = 0; peak = 0; dd = 0
    for t in trades:
        equity += t["pnl_pts"]
        peak = max(peak, equity)
        dd = min(dd, equity - peak)
    # Max losing streak
    cur = mx = 0
    for t in trades:
        if t["outcome"] == "SL": cur += 1; mx = max(mx, cur)
        else: cur = 0
    return {
        "n": n, "wins": len(wins), "losses": len(losses),
        "wr": round(wr, 3),
        "expct": round(expct, 2),
        "pf": round(pf, 2),
        "median_mfe": round(statistics.median(mfes), 1),
        "median_mae": round(statistics.median(maes), 1),
        "max_dd": round(dd, 1),
        "max_streak": mx,
        "tp1_hit_rate": round(sum(1 for t in trades if t.get("hit_tp1")) / n, 3),
        "tp2_hit_rate": round(sum(1 for t in trades if t.get("hit_tp2")) / n, 3),
        "total_pnl": round(sum(pnls), 1),
    }


# ─────────────────────────────────────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────────────────────────────────────

def main():
    with SessionLocal() as db:
        m15 = _load_bars(db, "M15")
        m5  = _load_bars(db, "M5")

    print(f"M15 bars: {len(m15)}  ({m15[0][0]} → {m15[-1][0]})")
    print(f"M5  bars: {len(m5)}   ({m5[0][0]} → {m5[-1][0]})")

    # Build M5 time-index for fast lookup
    m5_by_time = {b[0]: i for i, b in enumerate(m5)}

    # ── Detect all M15 predator signals ─────────────────
    signals = _detect_predator_m15(m15)
    print(f"\nTotal M15 predator signals: {len(signals)}")
    print(f"  ASIAN_BREAKDOWN: {sum(1 for s in signals if s['archetype']=='ASIAN_BREAKDOWN')}")
    print(f"  PDL_BREAK:       {sum(1 for s in signals if s['archetype']=='PDL_BREAK')}")

    # ── For each signal: find origin + M5-close entry + measure latency ──
    print("\n" + "="*70)
    print(" PHASE 1 — LATENCY DISTRIBUTIONS PER ARCHETYPE")
    print("="*70)

    per_arch = defaultdict(list)
    for s in signals:
        origin = _find_m5_origin(m5, s["level"], s["time_prod"])
        m5close = _find_m5_close_entry(m5, s["level"], s["time_prod"])
        if origin is None: continue

        # Latency = production_entry_time - origin_time (in minutes and bars)
        latency_min = (s["time_prod"] - origin["time"]).total_seconds() / 60.0
        # Points lost between origin's first-break price and production entry price
        # For SELL, "loss" is how much lower we entered
        pts_lost = origin["price"] - s["entry_prod"]

        # Compute total move from origin (max drop within lookahead window)
        prod_idx = s["bar_idx"]
        future = m15[prod_idx+1 : prod_idx+33]
        total_move_from_origin = origin["price"] - min(b[3] for b in future) if future else 0
        pct_consumed = (pts_lost / max(total_move_from_origin, 0.1)) * 100 if total_move_from_origin > 0 else None

        per_arch[s["archetype"]].append({
            "signal": s,
            "origin": origin,
            "m5close": m5close,
            "latency_min": latency_min,
            "pts_lost": pts_lost,
            "total_move_origin": total_move_from_origin,
            "pct_consumed": pct_consumed,
        })

    for arch, records in per_arch.items():
        lm = [r["latency_min"] for r in records]
        lp = [r["pts_lost"] for r in records if r["pts_lost"] >= 0]
        pc = [r["pct_consumed"] for r in records if r["pct_consumed"] is not None and 0 <= r["pct_consumed"] <= 200]
        print(f"\n  {arch}  (n={len(records)})")
        pm = _percentiles(lm)
        pp = _percentiles(lp)
        ppc = _percentiles(pc)
        print(f"    latency minutes   — p25: {pm[25]:.0f}   p50: {pm[50]:.0f}   p75: {pm[75]:.0f}   p90: {pm[90]:.0f}   mean: {statistics.mean(lm):.1f}")
        print(f"    latency points    — p25: {pp[25]:.1f}   p50: {pp[50]:.1f}   p75: {pp[75]:.1f}   p90: {pp[90]:.1f}   mean: {statistics.mean(lp):.1f}")
        if pc:
            print(f"    % of move used    — p25: {ppc[25]:.0f}%  p50: {ppc[50]:.0f}%  p75: {ppc[75]:.0f}%  p90: {ppc[90]:.0f}%  mean: {statistics.mean(pc):.0f}%")

    # ── Phase 2 — Recalculate expectancy from EXECUTABLE entries ──
    print("\n" + "="*70)
    print(" PHASE 2 — EXECUTABLE EDGE (theoretical vs production vs M5-close)")
    print("="*70)

    def _classify_arch(arch, tp1_pts, tp2_pts):
        records = per_arch.get(arch, [])
        if not records: return None
        # Theoretical: enter at origin price with stop above level+5
        theo_trades = []
        prod_trades = []
        m5c_trades = []
        for r in records:
            s = r["signal"]
            stop = s["stop"]
            prod_idx = s["bar_idx"]
            # Theoretical entry = origin price at origin bar (use next M15 as replay start)
            theo_trades.append(_replay_from(m15, prod_idx, r["origin"]["price"], stop, "SELL", tp1_pts, tp2_pts))
            # Production entry = M15 close price at production bar
            prod_trades.append(_replay_from(m15, prod_idx, s["entry_prod"], stop, "SELL", tp1_pts, tp2_pts))
            # M5-close variant — only if we found an earlier M5 close below level
            if r["m5close"] is not None:
                m5c_trades.append(_replay_from(m15, prod_idx, r["m5close"]["price"], stop, "SELL", tp1_pts, tp2_pts))
        return {
            "theoretical": _basic_stats(theo_trades),
            "production":  _basic_stats(prod_trades),
            "m5_close":    _basic_stats(m5c_trades),
        }

    exec_stats = {}
    for arch, (tp1, tp2) in (("ASIAN_BREAKDOWN", (30, 50)),
                              ("PDL_BREAK",       (40, 60))):
        r = _classify_arch(arch, tp1, tp2)
        if r is None: continue
        exec_stats[arch] = r
        print(f"\n  {arch}   TP1={tp1}pt   TP2={tp2}pt")
        print(f"    {'variant':16s} {'n':>5s} {'wr':>6s} {'expct':>7s} {'pf':>6s} {'tp1_rate':>8s} {'tp2_rate':>8s} {'max_dd':>8s}")
        for v_name in ("theoretical", "production", "m5_close"):
            v = r[v_name]
            if v is None:
                print(f"    {v_name:16s} — no data")
                continue
            print(f"    {v_name:16s} {v['n']:5d} {v['wr']:6.3f} {v['expct']:+7.2f} {v['pf']:6.2f} {v['tp1_hit_rate']:8.3f} {v['tp2_hit_rate']:8.3f} {v['max_dd']:8.1f}")

    # ── Phase 3 — Target reachability from EXECUTABLE (production) entry ──
    print("\n" + "="*70)
    print(" PHASE 3 — TARGET REACHABILITY FROM PRODUCTION ENTRY")
    print("="*70)
    for arch, records in per_arch.items():
        remaining_moves = []
        for r in records:
            s = r["signal"]
            prod_idx = s["bar_idx"]
            future = m15[prod_idx+1 : prod_idx+33]
            if not future: continue
            max_favorable = s["entry_prod"] - min(b[3] for b in future)
            remaining_moves.append(max_favorable)
        if not remaining_moves: continue
        median_rem = statistics.median(remaining_moves)
        # % of trades where remaining_move >= given targets
        buckets = [10, 20, 30, 40, 50, 60, 70, 90, 100]
        print(f"\n  {arch}")
        print(f"    median REMAINING move from production entry: {median_rem:.1f} pts")
        print(f"    {'target':8s}  {'reachable %':>12s}")
        for tgt in buckets:
            reach = sum(1 for m in remaining_moves if m >= tgt) / len(remaining_moves) * 100
            print(f"    {tgt}pt      {reach:11.1f}%")

    # ── Phase 5 — ARMED state analysis (bars-to-fire distribution) ──
    print("\n" + "="*70)
    print(" PHASE 5 — ARMED-state feasibility (bars between origin and fire)")
    print("="*70)
    for arch, records in per_arch.items():
        gap_bars = []
        for r in records:
            gap_min = r["latency_min"]
            gap_bars.append(gap_min / 5)   # M5 bars = min/5
        if not gap_bars: continue
        pb = _percentiles(gap_bars)
        armed_feasible = sum(1 for g in gap_bars if g >= 2) / len(gap_bars) * 100
        print(f"\n  {arch}: ARMED window ≥ 2 M5 bars available in {armed_feasible:.0f}% of signals")
        print(f"    M5 bars origin→fire — p25: {pb[25]:.1f}  p50: {pb[50]:.1f}  p75: {pb[75]:.1f}  p90: {pb[90]:.1f}")

    # ── Phase 7 — Extension filter buckets ──
    print("\n" + "="*70)
    print(" PHASE 7 — EXTENSION FILTER (expectancy by pct-consumed bucket)")
    print("="*70)
    for arch, records in per_arch.items():
        bucketed = defaultdict(list)
        for r in records:
            pc = r["pct_consumed"]
            if pc is None: continue
            if pc < 30:                bucket = "EARLY (<30%)"
            elif pc < 60:              bucket = "OPTIMAL (30-60%)"
            elif pc < 100:             bucket = "LATE (60-100%)"
            else:                      bucket = "EXHAUSTED (>100%)"
            s = r["signal"]
            tp1_pts = 30 if arch == "ASIAN_BREAKDOWN" else 40
            tp2_pts = 50 if arch == "ASIAN_BREAKDOWN" else 60
            trade = _replay_from(m15, s["bar_idx"], s["entry_prod"], s["stop"], "SELL", tp1_pts, tp2_pts)
            bucketed[bucket].append(trade)
        print(f"\n  {arch}")
        print(f"    {'bucket':22s} {'n':>4s} {'wr':>6s} {'expct':>7s} {'pf':>6s}")
        for bucket in ("EARLY (<30%)", "OPTIMAL (30-60%)", "LATE (60-100%)", "EXHAUSTED (>100%)"):
            trades = bucketed.get(bucket, [])
            stats = _basic_stats(trades) if trades else None
            if stats:
                print(f"    {bucket:22s} {stats['n']:4d} {stats['wr']:6.3f} {stats['expct']:+7.2f} {stats['pf']:6.2f}")

    # ── Phase 10 — Regime silence justification ──
    print("\n" + "="*70)
    print(" PHASE 10 — CURRENT REGIME (weak_bull × compressed) BACKTEST STATS")
    print("="*70)
    # Reuse regime classifier
    try:
        from services.regime_detector import classify_direction_regime, classify_vol_regime
        # For each M15 bar in history, classify regime and check if a signal fired
        matrix = defaultdict(list)
        closes = [b[4] for b in m15]
        bars_for_vol = [(b[0], b[2], b[3], b[4]) for b in m15]
        for i in range(210, len(m15) - 32):
            d = classify_direction_regime(closes[:i+1])
            v = classify_vol_regime(bars_for_vol[:i+1])
            matrix[(d, v)].append(i)
        # For each cell, count how many predator signals fell in it and their expectancy
        sig_by_bar = {s["bar_idx"]: s for s in signals}
        for (d, v) in [("weak_bull", "compressed"), ("weak_bull", "normal"),
                        ("range", "compressed"), ("strong_bull", "compressed"),
                        ("strong_bull", "normal"), ("strong_bull", "extreme")]:
            cell_bars = matrix.get((d, v), [])
            cell_signals = [sig_by_bar[i] for i in cell_bars if i in sig_by_bar]
            if not cell_signals:
                print(f"  {d:12s} × {v:12s}: bars={len(cell_bars):4d}  SIGNALS: 0  (predator silent — no historical trades to compute)")
                continue
            trades = []
            for s in cell_signals:
                tp1 = 30 if s["archetype"] == "ASIAN_BREAKDOWN" else 40
                trades.append(_replay_from(m15, s["bar_idx"], s["entry_prod"], s["stop"], "SELL", tp1, tp1 + 20))
            stats = _basic_stats(trades)
            if stats:
                print(f"  {d:12s} × {v:12s}: bars={len(cell_bars):4d}  signals={stats['n']:3d}  wr={stats['wr']:.2f}  expct={stats['expct']:+7.2f}  pf={stats['pf']:.2f}")
    except Exception as exc:
        print(f"  regime backtest failed: {exc}")

    # ── Phase 11 — Final table ──
    print("\n" + "="*70)
    print(" PHASE 11 — REQUIRED FINAL TABLE per archetype")
    print("="*70)
    for arch, stats in exec_stats.items():
        t = stats["theoretical"]
        p = stats["production"]
        m = stats["m5_close"] or {}
        records = per_arch.get(arch, [])
        latency_p50_pts = _percentiles([r["pts_lost"] for r in records])[50]
        pct_p50 = _percentiles([r["pct_consumed"] for r in records if r["pct_consumed"] is not None])[50]
        print(f"\n  {arch}")
        rows = [
            ("Trades",             t["n"],           p["n"],           m.get("n", "—")),
            ("WR",                 t["wr"],          p["wr"],          m.get("wr", "—")),
            ("Expectancy (pts)",   t["expct"],       p["expct"],       m.get("expct", "—")),
            ("PF",                 t["pf"],          p["pf"],          m.get("pf", "—")),
            ("Median MFE",         t["median_mfe"],  p["median_mfe"],  m.get("median_mfe", "—")),
            ("Median MAE",         t["median_mae"],  p["median_mae"],  m.get("median_mae", "—")),
            ("TP1 hit rate",       t["tp1_hit_rate"],p["tp1_hit_rate"],m.get("tp1_hit_rate", "—")),
            ("TP2 hit rate",       t["tp2_hit_rate"],p["tp2_hit_rate"],m.get("tp2_hit_rate", "—")),
            ("Max DD",             t["max_dd"],      p["max_dd"],      m.get("max_dd", "—")),
        ]
        print(f"    {'Metric':22s} {'Theoretical':>12s} {'Production':>12s} {'M5-close':>12s}")
        for name, tv, pv, mv in rows:
            print(f"    {name:22s} {str(tv):>12s} {str(pv):>12s} {str(mv):>12s}")
        print(f"    Median latency pts:  {latency_p50_pts:.1f}   Median %-consumed: {pct_p50:.0f}%")
        # Verdict
        prod_expct = p["expct"]
        m5_expct = m.get("expct") if m else None
        if prod_expct > 5: verdict = "PRODUCTION VIABLE"
        elif prod_expct > 0: verdict = "SHADOW ONLY"
        else: verdict = "REJECT"
        print(f"    → {verdict}")
        if m5_expct is not None and m5_expct > prod_expct + 2:
            print(f"    → M5-close variant delivers +{m5_expct - prod_expct:.1f} pts/trade uplift — recommend ship")


if __name__ == "__main__":
    main()
