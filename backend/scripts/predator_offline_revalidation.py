"""Off-host full-history post-fix Predator revalidation.

Consumes the gzip CSV produced by export_predator_history.py and replays the
CURRENT corrected detector functions on a CI runner with ample memory. This
keeps heavy research computation off the live VPS.
"""
from __future__ import annotations

import csv
import gzip
import json
import sys
from bisect import bisect_right
from collections import defaultdict
from datetime import datetime

from services import predator_engine as pe
from services.regime_detector import (
    classify_direction_regime,
    classify_vol_regime,
    regime_confidence_multiplier,
)

MAX_FORWARD_M5 = 96


def _ts(v):
    t = pe._legacy._parse_ts(v)
    if getattr(t, "tzinfo", None) is not None:
        t = t.replace(tzinfo=None)
    return t


def _load(path):
    out = {"M5": [], "M15": [], "H1": []}
    with gzip.open(path, "rt", newline="", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            tf = r["timeframe"]
            if tf not in out:
                continue
            out[tf].append((
                _ts(r["candle_time"]), float(r["open"]), float(r["high"]),
                float(r["low"]), float(r["close"]), float(r["volume"] or 0.0),
            ))
    for tf in out:
        out[tf].sort(key=lambda x: x[0])
    return out


def _vol_ratio(m5, i, window=50):
    if i < window:
        return None
    avg = sum(b[5] for b in m5[i-window:i]) / window
    return m5[i][5] / avg if avg > 0 else None


def _h1_rsi_series(h1, n=14):
    times, vals = [], []
    closes = [b[4] for b in h1]
    for i in range(n, len(h1)):
        gains, losses = [], []
        for j in range(i-n+1, i+1):
            d = closes[j] - closes[j-1]
            (gains if d > 0 else losses).append(abs(d))
        if not gains and not losses:
            rsi = 50.0
        else:
            avg_g = sum(gains) / n if gains else 0.0
            avg_l = sum(losses) / n if losses else 0.0
            rsi = 100.0 if avg_l == 0 else 100 - 100 / (1 + avg_g / avg_l)
        times.append(h1[i][0])
        vals.append(round(rsi, 1))
    return times, vals


def _rsi_at(t, times, vals):
    j = bisect_right(times, t) - 1
    return vals[j] if j >= 0 else None


def _precompute_regimes(m15):
    times, vals = [], []
    for i, b in enumerate(m15):
        hist = m15[max(0, i-299):i+1]
        closes = [x[4] for x in hist]
        volbars = [(x[0], x[2], x[3], x[4]) for x in hist]
        d = classify_direction_regime(closes)
        v = classify_vol_regime(volbars)
        vals.append((d, v, regime_confidence_multiplier(d, v)))
        times.append(b[0])
    return times, vals


def _regime_at(t, times, vals):
    j = bisect_right(times, t) - 1
    return vals[j] if j >= 0 else ("unknown", "unknown", 0.0)


def _replay(m5, i, sig):
    entry, stop, tp1 = sig.entry, sig.stop_loss, sig.tp1
    mfe = mae = 0.0
    for j in range(i+1, min(len(m5), i+MAX_FORWARD_M5+1)):
        _, _, high, low, _, _ = m5[j]
        mfe = max(mfe, entry-low)
        mae = max(mae, high-entry)
        hit_sl = high >= stop
        hit_tp = low <= tp1
        if hit_sl:
            return {"outcome": "SL", "pnl": -(stop-entry), "mfe": mfe, "mae": mae}
        if hit_tp:
            return {"outcome": "TP", "pnl": entry-tp1, "mfe": mfe, "mae": mae}
    return {"outcome": "TIMEOUT", "pnl": 0.0, "mfe": mfe, "mae": mae}


def _stats(rows):
    n = len(rows)
    if not n:
        return {"n": 0}
    wins = [r for r in rows if r["outcome"] == "TP"]
    losses = [r for r in rows if r["outcome"] == "SL"]
    gross_win = sum(r["pnl"] for r in wins)
    gross_loss = -sum(r["pnl"] for r in losses)
    equity = peak = 0.0
    max_dd = 0.0
    streak = max_streak = 0
    for r in rows:
        equity += r["pnl"]
        peak = max(peak, equity)
        max_dd = min(max_dd, equity-peak)
        if r["outcome"] == "SL":
            streak += 1
            max_streak = max(max_streak, streak)
        else:
            streak = 0
    total = sum(r["pnl"] for r in rows)
    return {
        "n": n, "wins": len(wins), "losses": len(losses),
        "timeouts": n-len(wins)-len(losses),
        "win_rate": round(len(wins)/n, 4),
        "expectancy_pts": round(total/n, 2),
        "profit_factor": round(gross_win/gross_loss, 2) if gross_loss else None,
        "total_pnl_pts": round(total, 1),
        "max_drawdown_pts": round(max_dd, 1),
        "max_loss_streak": max_streak,
        "avg_mfe_pts": round(sum(r["mfe"] for r in rows)/n, 1),
        "avg_mae_pts": round(sum(r["mae"] for r in rows)/n, 1),
    }


def _groups(rows, key):
    g = defaultdict(list)
    for r in rows:
        g[str(r[key])].append(r)
    return {k: _stats(v) for k, v in sorted(g.items())}


def main():
    if len(sys.argv) != 2:
        raise SystemExit("usage: predator_offline_revalidation.py predator_history.csv.gz")
    data = _load(sys.argv[1])
    m5, m15, h1 = data["M5"], data["M15"], data["H1"]
    if len(m5) < 800:
        raise SystemExit(f"insufficient M5 history: {len(m5)}")

    rsi_times, rsi_vals = _h1_rsi_series(h1)
    regime_times, regime_vals = _precompute_regimes(m15)

    all_rows = defaultdict(list)
    matched = defaultdict(list)
    seen = set()

    for i in range(700, len(m5)-MAX_FORWARD_M5):
        window = m5[i-699:i+1]
        t = m5[i][0]
        vol = _vol_ratio(m5, i)
        rsi = _rsi_at(t, rsi_times, rsi_vals)
        d, v, mult = _regime_at(t, regime_times, regime_vals)

        candidates = []
        a = pe.detect_asian_breakdown(window, rsi_h1=rsi, vol_r=vol)
        if a:
            candidates.append(a)
        p = pe.detect_pdl_break(window, vol_r=vol)
        if p:
            candidates.append(p)

        for sig in candidates:
            key_level = round(sig.stop_loss - 5.0, 2)
            key = (sig.archetype, pe._trading_date(t).isoformat(), key_level)
            if key in seen:
                continue
            seen.add(key)
            row = _replay(m5, i, sig)
            row.update({
                "time": t.isoformat(), "archetype": sig.archetype,
                "session": sig.session, "confidence": sig.confidence,
                "entry": sig.entry, "stop": sig.stop_loss, "rr": sig.rr,
                "regime_direction": d, "regime_volatility": v,
                "regime_cell": f"{d}×{v}", "regime_multiplier": mult,
            })
            all_rows[sig.archetype].append(row)
            if mult >= 0.5:
                matched[sig.archetype].append(row)

    report = {
        "mode": "OFF_HOST_FULL_HISTORY_POSTFIX_REGIME_REVALIDATION",
        "bars": {"M5": len(m5), "M15": len(m15), "H1": len(h1)},
        "history_start": m5[0][0].isoformat(),
        "history_end": m5[-1][0].isoformat(),
        "regime_rule": "production favorable iff multiplier >= 0.5",
        "same_bar_rule": "SL_FIRST_CONSERVATIVE",
        "replay_horizon_m5_bars": MAX_FORWARD_M5,
        "dedupe": "first unique archetype+XAU_trading_date+key_level",
        "all_corrected_triggers": {},
        "production_regime_matched": {},
    }
    for arch in ("ASIAN_BREAKDOWN", "PDL_BREAK"):
        rows = all_rows[arch]
        mrows = matched[arch]
        report["all_corrected_triggers"][arch] = {
            "overall": _stats(rows),
            "by_regime": _groups(rows, "regime_cell"),
            "by_session": _groups(rows, "session"),
        }
        report["production_regime_matched"][arch] = {
            "overall": _stats(mrows),
            "by_regime": _groups(mrows, "regime_cell"),
            "by_session": _groups(mrows, "session"),
        }
    print(json.dumps(report, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
