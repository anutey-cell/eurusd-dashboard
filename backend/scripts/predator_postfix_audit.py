"""Read-only post-fix Predator edge revalidation.

Replays the CURRENT production detectors over a bounded, deduplicated window of
stored historical M5/H1 bars and reports unique-opportunity sample size, win
rate, expectancy, profit factor and risk diagnostics. No database writes and
no execution calls.

Usage (inside backend container):
    PYTHONPATH=/app python /app/scripts/predator_postfix_audit.py
"""
from __future__ import annotations

import json
from bisect import bisect_right
from collections import defaultdict

from sqlalchemy import text

from database import SessionLocal
from services import predator_engine as pe


def _load(db, tf, raw_limit):
    """Load only the latest bounded rows, then dedupe by candle timestamp."""
    rows = db.execute(text(
        "SELECT candle_time, open, high, low, close, volume "
        "FROM historical_candles WHERE instrument='XAU/USD' AND timeframe=:tf "
        "ORDER BY candle_time DESC LIMIT :lim"
    ), {"tf": tf, "lim": raw_limit}).fetchall()
    by_time = {}
    for r in rows:
        t = pe._legacy._parse_ts(r[0])
        if getattr(t, "tzinfo", None) is not None:
            t = t.replace(tzinfo=None)
        if t not in by_time:
            by_time[t] = (t, float(r[1]), float(r[2]), float(r[3]),
                          float(r[4]), float(r[5] or 0))
    return [by_time[t] for t in sorted(by_time)]


def _vol_ratio(bars, i, window=50):
    if i < window:
        return None
    avg = sum(b[5] for b in bars[i-window:i]) / window
    return round(bars[i][5] / avg, 4) if avg > 0 else None


def _rsi_series(h1, n=14):
    times, values = [], []
    closes = [b[4] for b in h1]
    for i in range(n, len(h1)):
        gains = losses = 0.0
        for j in range(i-n+1, i+1):
            d = closes[j] - closes[j-1]
            if d > 0:
                gains += d
            else:
                losses += -d
        ag, al = gains / n, losses / n
        rsi = 100.0 if al == 0 else 100 - 100 / (1 + ag / al)
        times.append(h1[i][0]); values.append(rsi)
    return times, values


def _rsi_at(t, times, values):
    idx = bisect_right(times, t) - 1
    return values[idx] if idx >= 0 else None


def _replay(m5, start_idx, sig, max_bars=96):
    entry, stop, tp1, tp2 = sig.entry, sig.stop_loss, sig.tp1, sig.tp2
    mfe = mae = 0.0
    for j in range(start_idx + 1, min(len(m5), start_idx + max_bars + 1)):
        _, _, high, low, _, _ = m5[j]
        mfe = max(mfe, entry - low)
        mae = max(mae, high - entry)
        hit_sl = high >= stop
        hit_tp1 = low <= tp1
        hit_tp2 = low <= tp2
        if hit_sl:
            return {"outcome": "SL", "pnl": -(stop-entry), "mfe": mfe, "mae": mae,
                    "tp1": hit_tp1, "tp2": hit_tp2}
        if hit_tp1:
            return {"outcome": "TP", "pnl": entry-tp1, "mfe": mfe, "mae": mae,
                    "tp1": True, "tp2": hit_tp2}
    return {"outcome": "TIMEOUT", "pnl": 0.0, "mfe": mfe, "mae": mae,
            "tp1": False, "tp2": False}


def _stats(rows):
    n = len(rows)
    if not n:
        return {"n": 0}
    wins = [r for r in rows if r["outcome"] == "TP"]
    losses = [r for r in rows if r["outcome"] == "SL"]
    gross_win = sum(r["pnl"] for r in wins)
    gross_loss = -sum(r["pnl"] for r in losses)
    pnl = [r["pnl"] for r in rows]
    equity = peak = 0.0
    max_dd = 0.0
    for x in pnl:
        equity += x
        peak = max(peak, equity)
        max_dd = min(max_dd, equity - peak)
    return {
        "n": n,
        "wins": len(wins),
        "losses": len(losses),
        "timeouts": n-len(wins)-len(losses),
        "win_rate": round(len(wins)/n, 4),
        "expectancy_pts": round(sum(pnl)/n, 2),
        "profit_factor": round(gross_win/gross_loss, 2) if gross_loss else None,
        "total_pnl_pts": round(sum(pnl), 1),
        "max_drawdown_pts": round(max_dd, 1),
        "tp1_hit_rate": round(sum(1 for r in rows if r["tp1"])/n, 4),
        "tp2_intrabar_rate": round(sum(1 for r in rows if r["tp2"])/n, 4),
        "avg_mfe_pts": round(sum(r["mfe"] for r in rows)/n, 1),
        "avg_mae_pts": round(sum(r["mae"] for r in rows)/n, 1),
    }


def main():
    # Deliberately small footprint on the live container. 12k M5 rows is
    # roughly six calendar weeks at continuous 5-minute sampling before
    # weekend gaps and duplicate-row removal. This is a governance sample,
    # not a replacement for an offline full-history research run.
    with SessionLocal() as db:
        m5 = _load(db, "M5", 12000)
        h1 = _load(db, "H1", 1200)

    if len(m5) < 800:
        raise SystemExit(f"insufficient unique M5 history: {len(m5)}")

    rsi_times, rsi_values = _rsi_series(h1)
    results = defaultdict(list)
    seen = set()

    for i in range(700, len(m5) - 96):
        window = m5[i-699:i+1]
        vol = _vol_ratio(m5, i)
        rsi = _rsi_at(m5[i][0], rsi_times, rsi_values)

        candidates = []
        a = pe.detect_asian_breakdown(window, rsi_h1=rsi, vol_r=vol)
        if a:
            candidates.append(a)
        p = pe.detect_pdl_break(window, vol_r=vol)
        if p:
            candidates.append(p)

        for sig in candidates:
            level = round(sig.stop_loss - 5.0, 2)
            key = (sig.archetype, pe._trading_date(m5[i][0]).isoformat(), level)
            if key in seen:
                continue
            seen.add(key)
            row = _replay(m5, i, sig)
            row.update({"time": sig.bar_time, "entry": sig.entry, "stop": sig.stop_loss,
                        "level": level, "session": sig.session, "confidence": sig.confidence})
            results[sig.archetype].append(row)

    report = {
        "mode": "READ_ONLY_POSTFIX_GOVERNANCE_SAMPLE",
        "m5_bars_unique": len(m5),
        "h1_bars_unique": len(h1),
        "m5_start": m5[0][0].isoformat(),
        "m5_end": m5[-1][0].isoformat(),
        "replay_horizon_m5_bars": 96,
        "same_bar_rule": "SL_FIRST_CONSERVATIVE",
        "dedupe": "first unique archetype+trading_date+level opportunity",
        "old_display_stats": {
            "ASIAN_BREAKDOWN": {"n": 129, "win_rate": 0.57, "expectancy_pts": 12.98, "profit_factor": 4.25},
            "PDL_BREAK": {"n": 118, "win_rate": 0.73, "expectancy_pts": 26.35, "profit_factor": 10.41},
        },
        "postfix": {k: _stats(v) for k, v in sorted(results.items())},
    }
    print(json.dumps(report, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
