"""
Outcome-first XAU/USD edge discovery.
=====================================

DO NOT start from ICT / mandate rules. Start from historical price outcomes.

For every M15 bar in historical_candles, look 16 bars (~4h) forward:
  BUY  event fires when max(high) - close >= threshold BEFORE
          drawdown (close - min(low)) crosses threshold/2
  SELL event fires when close - min(low) >= threshold BEFORE
          adverse (max(high) - close) crosses threshold/2

Thresholds: 30, 50, 70, 100 points (typical XAU/USD move brackets).

For each event, capture ~15 pre-move features computed from OHLCV alone.
Then rank features by univariate WR and expectancy contribution.

Usage:  docker exec xauusd-backend python /app/scripts/edge_discovery.py
"""
from __future__ import annotations

import sys, os
sys.path.insert(0, "/app")

import json
import statistics
from collections import Counter, defaultdict
from datetime import datetime, timezone, timedelta

from database import SessionLocal
from sqlalchemy import text


# ─────────────────────────────────────────────────────────────────────────────
# Config
# ─────────────────────────────────────────────────────────────────────────────

LOOKAHEAD_BARS = 16                         # 16 × M15 = 4 hours forward
THRESHOLDS     = [30.0, 50.0, 70.0, 100.0]  # points
MAE_TOLERANCE  = 0.5                        # DD must stay under thr * this
TRAIN_FRAC     = 0.70                       # first 70% of bars for discovery


# ─────────────────────────────────────────────────────────────────────────────
# Load bars
# ─────────────────────────────────────────────────────────────────────────────

def _load_bars(db, tf: str):
    rows = db.execute(text(
        "SELECT candle_time, open, high, low, close, volume "
        "FROM historical_candles "
        "WHERE instrument='XAU/USD' AND timeframe=:tf "
        "ORDER BY candle_time"
    ), {"tf": tf}).fetchall()
    out = []
    for r in rows:
        t = r[0]
        # Normalise: SQLite returns strings, Postgres returns datetime
        if isinstance(t, str):
            for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S",
                          "%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S"):
                try:
                    t = datetime.strptime(t.split("+")[0], fmt); break
                except ValueError:
                    continue
        if hasattr(t, "tzinfo") and t.tzinfo is not None:
            t = t.replace(tzinfo=None)
        out.append((t, float(r[1]), float(r[2]), float(r[3]), float(r[4]), float(r[5] or 0)))
    return out


# ─────────────────────────────────────────────────────────────────────────────
# Move-event detection (outcome-first)
# ─────────────────────────────────────────────────────────────────────────────

def _detect_events(bars, thresholds, lookahead, mae_tolerance):
    """
    For each bar i, look forward `lookahead` bars. Emit a BUY event for
    threshold `thr` if max(high) reaches close+thr BEFORE drawdown crosses
    thr*mae_tolerance. Analogous for SELL.
    """
    events = []
    for i in range(len(bars) - lookahead):
        t, o, h, l, c, v = bars[i]
        future = bars[i+1 : i+1+lookahead]
        # BUY scan
        for thr in thresholds:
            running_low = float("inf")
            for j, (ft, fo, fh, fl, fc, fv) in enumerate(future):
                running_low = min(running_low, fl)
                dd = c - running_low
                if dd >= thr * mae_tolerance:
                    break   # invalidated before target
                if fh - c >= thr:
                    events.append({
                        "bar_idx":   i,
                        "time":      t,
                        "threshold": thr,
                        "direction": "BUY",
                        "mfe":       round(fh - c, 2),
                        "mae":       round(dd, 2),
                        "bars":      j + 1,
                        "entry":     c,
                    })
                    break
        # SELL scan
        for thr in thresholds:
            running_high = float("-inf")
            for j, (ft, fo, fh, fl, fc, fv) in enumerate(future):
                running_high = max(running_high, fh)
                dd = running_high - c
                if dd >= thr * mae_tolerance:
                    break
                if c - fl >= thr:
                    events.append({
                        "bar_idx":   i,
                        "time":      t,
                        "threshold": thr,
                        "direction": "SELL",
                        "mfe":       round(c - fl, 2),
                        "mae":       round(dd, 2),
                        "bars":      j + 1,
                        "entry":     c,
                    })
                    break
    return events


# ─────────────────────────────────────────────────────────────────────────────
# Pre-move feature extraction
# ─────────────────────────────────────────────────────────────────────────────

def _session_label(hour: int) -> str:
    if 22 <= hour or hour < 6:     return "ASIA"
    if 6 <= hour < 7:              return "PRE_LDN"
    if 7 <= hour < 10:             return "LDN_OPEN"
    if 10 <= hour < 12:            return "LDN_CONT"
    if 12 <= hour < 13:            return "LDN_LUNCH"
    if 13 <= hour < 16:            return "NY_OPEN"
    if 16 <= hour < 17:            return "LDN_NY_CLOSE"
    return "NY_LATE"


def _killzone(hour: int) -> str:
    if 7 <= hour < 10:  return "LONDON_KZ"     # 07-10 UTC
    if 13 <= hour < 16: return "NY_KZ"         # 13-16 UTC
    return "OFF_KZ"


def _atr(bars, i, n=14):
    """Simple mean of (high-low) over last n bars ending at i-1."""
    if i < n: return None
    slc = bars[i-n:i]
    return sum(b[2] - b[3] for b in slc) / n


def _atr_percentile(bars, i, current_atr, window=200, n=14):
    """Rank current_atr among ATRs from the last `window` bars ending at i-1."""
    if i < window: return None
    atrs = []
    for k in range(i - window, i):
        if k < n: continue
        atrs.append(sum(b[2] - b[3] for b in bars[k-n:k]) / n)
    if not atrs: return None
    ranked = sorted(atrs)
    below = sum(1 for a in ranked if a < current_atr)
    return int(round(100 * below / len(ranked)))


def _rsi(bars, i, n=14):
    if i < n + 1: return None
    gains, losses = [], []
    for k in range(i-n, i):
        delta = bars[k+1][4] - bars[k][4] if k+1 < len(bars) else 0
        (gains if delta > 0 else losses).append(abs(delta))
    if not gains and not losses: return 50.0
    avg_g = sum(gains) / n if gains else 0
    avg_l = sum(losses) / n if losses else 0
    if avg_l == 0: return 100.0
    rs = avg_g / avg_l
    return round(100 - 100 / (1 + rs), 1)


def _ema(bars, i, n):
    if i < n: return None
    k = 2 / (n + 1)
    ema = bars[i-n][4]
    for j in range(i-n+1, i):
        ema = bars[j][4] * k + ema * (1 - k)
    return ema


def _prev_day_hl(bars, i):
    """Return (prev_day_high, prev_day_low) — the calendar day before bars[i]."""
    t = bars[i][0]
    prev_day_end = t.replace(hour=0, minute=0, second=0, microsecond=0)
    prev_day_start = prev_day_end - timedelta(days=1)
    highs, lows = [], []
    for k in range(max(0, i-200), i):
        if prev_day_start <= bars[k][0] < prev_day_end:
            highs.append(bars[k][2])
            lows.append(bars[k][3])
    if not highs: return None, None
    return max(highs), min(lows)


def _asian_range(bars, i):
    """Return (asian_high, asian_low) — 22:00-06:00 UTC of the CURRENT day."""
    t = bars[i][0]
    if 22 <= t.hour or t.hour < 6:
        # We're inside Asian session — use whatever's formed
        session_start = t.replace(hour=22, minute=0, second=0, microsecond=0)
        if t.hour < 6:
            session_start = session_start - timedelta(days=1)
    else:
        # Past Asian session — use overnight window (yesterday 22:00 → today 06:00)
        today_6 = t.replace(hour=6, minute=0, second=0, microsecond=0)
        session_start = today_6 - timedelta(hours=8)
    highs, lows = [], []
    for k in range(max(0, i-100), i):
        if session_start <= bars[k][0] < session_start + timedelta(hours=8):
            highs.append(bars[k][2])
            lows.append(bars[k][3])
    if not highs: return None, None
    return max(highs), min(lows)


def _sweep_reclaim(bars, i, lookback=8):
    """
    Simple sweep/reclaim heuristic on last `lookback` bars.
    Returns dict {swept_high, swept_low, reclaimed}.
    """
    if i < lookback + 5: return {"swept_high": False, "swept_low": False, "reclaimed": False}
    prev_high = max(b[2] for b in bars[i-lookback-5:i-lookback])
    prev_low  = min(b[3] for b in bars[i-lookback-5:i-lookback])
    recent = bars[i-lookback:i]
    swept_high = any(b[2] > prev_high for b in recent)
    swept_low  = any(b[3] < prev_low for b in recent)
    last_close = bars[i][4]
    reclaimed = (swept_high and last_close < prev_high) or (swept_low and last_close > prev_low)
    return {"swept_high": swept_high, "swept_low": swept_low, "reclaimed": reclaimed}


def _features(bars, i):
    """Compute pre-move feature dict at bar i. Uses ONLY data available at time t."""
    if i < 210: return None      # need lookback for ATR percentile
    t, o, h, l, c, v = bars[i]
    atr = _atr(bars, i, 14)
    atr_pct = _atr_percentile(bars, i, atr) if atr else None
    ema20 = _ema(bars, i, 20)
    rsi = _rsi(bars, i, 14)
    prev_h, prev_l = _prev_day_hl(bars, i)
    asia_h, asia_l = _asian_range(bars, i)
    sweep = _sweep_reclaim(bars, i)

    # Position vs prev-day range
    prev_day_pos = None
    if prev_h is not None and prev_l is not None and prev_h > prev_l:
        prev_day_pos = (c - prev_l) / (prev_h - prev_l)
        if prev_day_pos < 0:      prev_day_pos_bucket = "below_prev_low"
        elif prev_day_pos < 0.25: prev_day_pos_bucket = "prev_low_qtr"
        elif prev_day_pos < 0.75: prev_day_pos_bucket = "prev_mid"
        elif prev_day_pos <= 1:   prev_day_pos_bucket = "prev_high_qtr"
        else:                     prev_day_pos_bucket = "above_prev_high"
    else:
        prev_day_pos_bucket = "unknown"

    # Position vs Asian range
    asia_pos_bucket = "unknown"
    if asia_h is not None and asia_l is not None and asia_h > asia_l:
        pos = (c - asia_l) / (asia_h - asia_l)
        if pos < 0:      asia_pos_bucket = "below_asia_low"
        elif pos < 0.25: asia_pos_bucket = "asia_low_qtr"
        elif pos < 0.75: asia_pos_bucket = "asia_mid"
        elif pos <= 1:   asia_pos_bucket = "asia_high_qtr"
        else:            asia_pos_bucket = "above_asia_high"

    # Distance from H1 EMA20 in ATR units (M15 proxy)
    ema20_dist_atr = None
    if ema20 and atr and atr > 0:
        ema20_dist_atr = round((c - ema20) / atr, 2)

    # Bar shape
    body = abs(c - o)
    rng = h - l if h > l else 0.001
    body_ratio = body / rng
    is_pin_bar = body_ratio < 0.35 and rng > (atr or 0)
    bar_dir = "up" if c > o else ("down" if c < o else "flat")

    # 3-bar momentum (same direction?)
    recent_closes = [b[4] for b in bars[i-3:i+1]]
    momentum_3 = "up" if all(recent_closes[k] > recent_closes[k-1] for k in range(1, 4)) \
              else "down" if all(recent_closes[k] < recent_closes[k-1] for k in range(1, 4)) \
              else "mixed"

    # ATR percentile bucket
    if atr_pct is None: atr_pct_bucket = "unknown"
    elif atr_pct < 25: atr_pct_bucket = "atr_low"
    elif atr_pct < 75: atr_pct_bucket = "atr_mid"
    else:              atr_pct_bucket = "atr_high"

    # Volume ratio vs 50-bar mean
    vol_ratio_bucket = "unknown"
    if i >= 50:
        avg_v = sum(b[5] for b in bars[i-50:i]) / 50
        if avg_v > 0:
            r = v / avg_v
            if r < 0.7:      vol_ratio_bucket = "vol_low"
            elif r < 1.3:    vol_ratio_bucket = "vol_mid"
            elif r < 2.0:    vol_ratio_bucket = "vol_high"
            else:            vol_ratio_bucket = "vol_spike"

    return {
        "session":       _session_label(t.hour),
        "killzone":      _killzone(t.hour),
        "prev_day_pos":  prev_day_pos_bucket,
        "asia_pos":      asia_pos_bucket,
        "ema20_dist":    ("above_ema" if (ema20_dist_atr or 0) > 0.5
                          else "below_ema" if (ema20_dist_atr or 0) < -0.5
                          else "at_ema"),
        "rsi_bucket":    ("rsi_ob"     if (rsi or 50) > 70
                          else "rsi_os" if (rsi or 50) < 30
                          else "rsi_mid_up" if (rsi or 50) > 50
                          else "rsi_mid_dn"),
        "atr_pct":       atr_pct_bucket,
        "vol_ratio":     vol_ratio_bucket,
        "sweep_high":    "swept_high" if sweep["swept_high"] else "no_sweep_h",
        "sweep_low":     "swept_low"  if sweep["swept_low"]  else "no_sweep_l",
        "reclaimed":     "reclaimed"  if sweep["reclaimed"]  else "no_reclaim",
        "bar_shape":     "pin_bar"    if is_pin_bar          else f"body_{bar_dir}",
        "momentum_3":    momentum_3,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Univariate feature analysis
# ─────────────────────────────────────────────────────────────────────────────

def _univariate_wr(events, base_rate):
    """
    For each (feature, bucket) pair, compute:
      n_hits    — number of events with that bucket
      wr        — event count / total bars-at-bucket (approximated using
                  events themselves as the baseline)
    We report LIFT vs base rate — how much a given feature raises event
    probability relative to picking a random bar.
    """
    # We can't compute true WR without knowing how many bars had each bucket
    # AND didn't produce an event. But we CAN rank features by how often
    # they appear IN EVENTS vs their base rate over all bars.
    pass


# ─────────────────────────────────────────────────────────────────────────────
# Main pipeline
# ─────────────────────────────────────────────────────────────────────────────

def _analyze_split(bars, events, label, i_start, i_end, direction_filter=None):
    """
    Rank pre-move fingerprints in bars[i_start:i_end]. Events use ORIGINAL
    bar_idx (which is why we take the full bars array + slice indices).
    If direction_filter is 'BUY' or 'SELL', only count events of that side.
    """
    bar_features = {}
    bar_event_summary = {}

    for i in range(max(210, i_start), min(i_end, len(bars) - LOOKAHEAD_BARS)):
        f = _features(bars, i)
        if f is None: continue
        bar_features[i] = f

    for e in events:
        if e["bar_idx"] < i_start or e["bar_idx"] >= i_end: continue
        if direction_filter and e["direction"] != direction_filter: continue
        i = e["bar_idx"]
        if i not in bar_event_summary or e["threshold"] > bar_event_summary[i]["threshold"]:
            bar_event_summary[i] = {
                "direction": e["direction"],
                "threshold": e["threshold"],
                "mfe":       e["mfe"],
                "mae":       e["mae"],
            }

    total_bars = len(bar_features)
    total_events = len(bar_event_summary)
    base_wr = total_events / max(total_bars, 1)

    # For each (feature_name, bucket_value), count:
    #   n_at_bucket, n_events_at_bucket, wr_at_bucket, lift
    # Also separate BUY vs SELL for direction-specific patterns
    from collections import defaultdict
    stats = defaultdict(lambda: {"n": 0, "events": 0, "buy_events": 0, "sell_events": 0,
                                    "sum_mfe": 0.0, "sum_mae": 0.0})

    for i, f in bar_features.items():
        ev = bar_event_summary.get(i)
        for feat_name, feat_val in f.items():
            key = (feat_name, feat_val)
            stats[key]["n"] += 1
            if ev:
                stats[key]["events"] += 1
                if ev["direction"] == "BUY":  stats[key]["buy_events"] += 1
                else:                          stats[key]["sell_events"] += 1
                stats[key]["sum_mfe"] += ev["mfe"]
                stats[key]["sum_mae"] += ev["mae"]

    rows = []
    for (feat_name, feat_val), s in stats.items():
        if s["n"] < 20: continue                      # too small
        wr = s["events"] / s["n"]
        lift = wr / base_wr if base_wr > 0 else 0
        buy_wr  = s["buy_events"] / s["n"]
        sell_wr = s["sell_events"] / s["n"]
        avg_mfe = s["sum_mfe"] / max(s["events"], 1)
        avg_mae = s["sum_mae"] / max(s["events"], 1)
        # Expectancy proxy: WR × avg_MFE − (1-WR) × avg_MAE
        expectancy = wr * avg_mfe - (1 - wr) * avg_mae
        rows.append({
            "feature":    feat_name,
            "bucket":     feat_val,
            "n_bars":     s["n"],
            "n_events":   s["events"],
            "wr":         round(wr, 3),
            "lift":       round(lift, 2),
            "buy_wr":     round(buy_wr, 3),
            "sell_wr":    round(sell_wr, 3),
            "avg_mfe":    round(avg_mfe, 1),
            "avg_mae":    round(avg_mae, 1),
            "expectancy": round(expectancy, 2),
        })

    rows.sort(key=lambda r: r["lift"], reverse=True)
    return {
        "label":         label,
        "total_bars":    total_bars,
        "total_events":  total_events,
        "base_wr":       round(base_wr, 3),
        "top_by_lift":   rows[:20],
    }


def main():
    with SessionLocal() as db:
        bars = _load_bars(db, "M15")

    print(f"Loaded {len(bars)} M15 bars")
    if not bars:
        print("No data"); return
    print(f"Range: {bars[0][0]} → {bars[-1][0]}")

    # ── Detect events on ALL data first ─────────────────────
    events = _detect_events(bars, THRESHOLDS, LOOKAHEAD_BARS, MAE_TOLERANCE)
    print(f"Total events detected: {len(events)}")

    ev_counts = Counter((e["direction"], e["threshold"]) for e in events)
    print("\nEvent counts by (direction, threshold):")
    for k in sorted(ev_counts.keys()):
        print(f"  {k[0]:4s} +{int(k[1]):3d}pts:  {ev_counts[k]:5d}")

    # ── Train/test split (chronological) ───────────────────
    split_idx = int(len(bars) * TRAIN_FRAC)
    train_events = [e for e in events if e["bar_idx"] < split_idx]
    test_events  = [e for e in events if e["bar_idx"] >= split_idx]
    print(f"\nTrain bars: 0..{split_idx} ({len(train_events)} events)")
    print(f"Test  bars: {split_idx}..{len(bars)} ({len(test_events)} events)")

    # ── Univariate discovery on train — combined direction ─
    print("\n" + "="*68)
    print(" TRAIN — Top 20 (all events) ranked by LIFT vs base")
    print("="*68)
    train_res = _analyze_split(bars, events, "train", 0, split_idx)
    print(f"  base WR: {train_res['base_wr']}   total events in train: {train_res['total_events']}")
    print(f"  {'feature':16s} {'bucket':18s} {'n':>5s} {'wr':>6s} {'lift':>5s} {'buy_wr':>7s} {'sell_wr':>7s} {'mfe':>6s} {'mae':>6s} {'expct':>6s}")
    for r in train_res["top_by_lift"]:
        print(f"  {r['feature']:16s} {r['bucket']:18s} {r['n_bars']:5d} {r['wr']:6.3f} {r['lift']:5.2f} {r['buy_wr']:7.3f} {r['sell_wr']:7.3f} {r['avg_mfe']:6.1f} {r['avg_mae']:6.1f} {r['expectancy']:6.2f}")

    # ── Per-direction analysis ─────────────────────────────
    for side in ("BUY", "SELL"):
        print("\n" + "="*68)
        print(f" TRAIN — Top 12 {side}-specific fingerprints (threshold >= 50pt)")
        print("="*68)
        side_events = [e for e in events if e["direction"] == side and e["threshold"] >= 50]
        side_res = _analyze_split(bars, side_events, f"train_{side}", 0, split_idx, direction_filter=side)
        print(f"  base WR: {side_res['base_wr']}   n_events: {side_res['total_events']}")
        print(f"  {'feature':16s} {'bucket':18s} {'n':>5s} {'wr':>6s} {'lift':>5s} {'mfe':>6s} {'mae':>6s} {'expct':>6s}")
        for r in side_res["top_by_lift"][:12]:
            print(f"  {r['feature']:16s} {r['bucket']:18s} {r['n_bars']:5d} {r['wr']:6.3f} {r['lift']:5.2f} {r['avg_mfe']:6.1f} {r['avg_mae']:6.1f} {r['expectancy']:6.2f}")

    # ── Walk-forward on test ────────────────────────────────
    print("\n" + "="*68)
    print(" TEST (walk-forward, unseen 30% of data) — same top-20 features")
    print("="*68)
    test_res = _analyze_split(bars, events, "test", split_idx, len(bars))
    # Compare — do top train features still lead on test?
    test_by_key = {(r["feature"], r["bucket"]): r for r in test_res["top_by_lift"]}
    print(f"  {'feature':16s} {'bucket':18s}  {'train_lift':>10s}  {'test_lift':>9s}  {'delta':>6s}")
    for tr in train_res["top_by_lift"][:20]:
        te = test_by_key.get((tr["feature"], tr["bucket"]))
        te_lift = te["lift"] if te else 0.0
        delta = te_lift - tr["lift"]
        marker = "✓" if delta > -0.2 else "✗" if delta < -0.5 else "~"
        print(f"  {tr['feature']:16s} {tr['bucket']:18s}  {tr['lift']:10.2f}  {te_lift:9.2f}  {delta:+6.2f}  {marker}")

    # ── Test existing mandate assumptions ───────────────────
    print("\n" + "="*68)
    print(" MANDATE RULE TEST — would current rules have blocked winners?")
    print("="*68)
    # For every event in train + test, check what current mandate rules
    # would have said. Rules we can approximate:
    #   R1: RSI > 70 → block SELL   (mandate: strong bullish alignment req'd)
    #   R2: RSI < 30 → block BUY
    #   R3: ATR pct > 75 (extended)  → mandate labels "extended"
    #   R4: NOT in London or NY killzone → C2 negative
    #   R5: EMA20 distance > 1.5 ATR (extended)
    blocked_by = Counter()
    total_winners = len(events)
    for e in events:
        i = e["bar_idx"]
        if i < 210 or i >= len(bars): continue
        f = _features(bars, i)
        if not f: continue
        # R1: strong-bullish HTF against a SELL
        if e["direction"] == "SELL" and f["rsi_bucket"] == "rsi_ob":
            blocked_by["RSI overbought blocks SELL"] += 1
        # R2: strong-bearish HTF against a BUY
        if e["direction"] == "BUY" and f["rsi_bucket"] == "rsi_os":
            blocked_by["RSI oversold blocks BUY"] += 1
        # R3: ATR extended (top quartile)
        if f["atr_pct"] == "atr_high":
            blocked_by["ATR high (top quartile — 'extended')"] += 1
        # R4: not in a killzone
        if f["killzone"] == "OFF_KZ":
            blocked_by["Off killzone (no LDN/NY session)"] += 1
        # R5: extended above/below EMA20 (proxy for 'move exhausted')
        # We can't easily compute this without ATR distance stored, but we
        # can use ema20_dist bucket
        if f["ema20_dist"] in ("above_ema", "below_ema") and \
           ((e["direction"] == "BUY" and f["ema20_dist"] == "above_ema") or
            (e["direction"] == "SELL" and f["ema20_dist"] == "below_ema")):
            blocked_by["Move already extended past EMA20"] += 1

    print(f"  {'rule':50s} {'n_blocked':>10s}  {'pct_of_winners':>14s}")
    for rule, n in blocked_by.most_common():
        pct = 100 * n / max(total_winners, 1)
        print(f"  {rule:50s} {n:10d}  {pct:13.1f}%")

    # ── OUTCOME: KEY SUMMARY ────────────────────────────────
    print("\n" + "="*68)
    print(" HEADLINES")
    print("="*68)
    print(f"  {ev_counts.get(('BUY',30), 0)} BUY +30-pt events / "
          f"{ev_counts.get(('SELL',30), 0)} SELL +30 events")
    print(f"  {ev_counts.get(('BUY',50), 0)} BUY +50-pt events / "
          f"{ev_counts.get(('SELL',50), 0)} SELL +50 events")
    print(f"  {ev_counts.get(('BUY',100), 0)} BUY +100-pt events / "
          f"{ev_counts.get(('SELL',100), 0)} SELL +100 events")


if __name__ == "__main__":
    main()
