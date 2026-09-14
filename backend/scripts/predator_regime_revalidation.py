"""Read-only full-history post-fix Predator regime revalidation.

Replays the CURRENT corrected Asian Breakdown and PDL Break detectors across the
largest reliable historical_candles window available on the VPS.

Each historical chunk runs in a fresh subprocess. This deliberately resets
Python/DB-driver memory after every chunk so the audit can traverse long history
without accumulating memory inside the production backend container.

No database writes, no Telegram sends, no execution calls.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from bisect import bisect_right
from collections import defaultdict
from datetime import datetime, timedelta

from sqlalchemy import text

from database import SessionLocal
from services import predator_engine as pe
from services.regime_detector import (
    classify_direction_regime,
    classify_vol_regime,
    regime_confidence_multiplier,
)

CHUNK_DAYS = 3
LOOKBACK_DAYS = 4
FORWARD_HOURS = 10
MAX_FORWARD_M5 = 96


def _ts(v):
    t = pe._legacy._parse_ts(v)
    if getattr(t, "tzinfo", None) is not None:
        t = t.replace(tzinfo=None)
    return t


def _parse_iso(v):
    t = datetime.fromisoformat(v)
    if getattr(t, "tzinfo", None) is not None:
        t = t.replace(tzinfo=None)
    return t


def _load_range(db, tf, start, end):
    rows = db.execute(text(
        "SELECT candle_time, open, high, low, close, volume "
        "FROM historical_candles "
        "WHERE instrument='XAU/USD' AND timeframe=:tf "
        "AND candle_time >= :start AND candle_time < :end "
        "ORDER BY candle_time"
    ), {"tf": tf, "start": start, "end": end}).fetchall()
    by_time = {}
    for r in rows:
        t = _ts(r[0])
        by_time[t] = (t, float(r[1]), float(r[2]), float(r[3]),
                      float(r[4]), float(r[5] or 0.0))
    return [by_time[k] for k in sorted(by_time)]


def _bounds(db):
    row = db.execute(text(
        "SELECT MIN(candle_time), MAX(candle_time), COUNT(*) "
        "FROM historical_candles WHERE instrument='XAU/USD' AND timeframe='M5'"
    )).first()
    return _ts(row[0]), _ts(row[1]), int(row[2] or 0)


def _vol_ratio(bars, i, window=50):
    if i < window:
        return None
    avg = sum(b[5] for b in bars[i-window:i]) / window
    return (bars[i][5] / avg) if avg > 0 else None


def _rsi_series(h1, n=14):
    times, vals = [], []
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
        vals.append(100.0 if al == 0 else 100 - 100 / (1 + ag / al))
        times.append(h1[i][0])
    return times, vals


def _rsi_at(t, times, vals):
    j = bisect_right(times, t) - 1
    return vals[j] if j >= 0 else None


def _precompute_m15_regimes(m15):
    times, vals = [], []
    for i in range(len(m15)):
        start = max(0, i - 299)
        hist = m15[start:i+1]
        closes = [b[4] for b in hist]
        bars_for_vol = [(b[0], b[2], b[3], b[4]) for b in hist]
        d = classify_direction_regime(closes)
        v = classify_vol_regime(bars_for_vol)
        vals.append((d, v, regime_confidence_multiplier(d, v)))
        times.append(m15[i][0])
    return times, vals


def _regime_at(t, times, vals):
    j = bisect_right(times, t) - 1
    return vals[j] if j >= 0 else ("unknown", "unknown", 0.0)


def _replay(m5, idx, sig):
    entry, stop, tp1 = sig.entry, sig.stop_loss, sig.tp1
    mfe = mae = 0.0
    for j in range(idx + 1, min(len(m5), idx + MAX_FORWARD_M5 + 1)):
        _, _, high, low, _, _ = m5[j]
        mfe = max(mfe, entry - low)
        mae = max(mae, high - entry)
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
    pnl = [r["pnl"] for r in rows]
    equity = peak = 0.0
    max_dd = 0.0
    max_loss_streak = streak = 0
    for r, x in zip(rows, pnl):
        equity += x
        peak = max(peak, equity)
        max_dd = min(max_dd, equity - peak)
        if r["outcome"] == "SL":
            streak += 1
            max_loss_streak = max(max_loss_streak, streak)
        else:
            streak = 0
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
        "max_loss_streak": max_loss_streak,
        "avg_mfe_pts": round(sum(r["mfe"] for r in rows)/n, 1),
        "avg_mae_pts": round(sum(r["mae"] for r in rows)/n, 1),
    }


def _group_stats(rows, key):
    groups = defaultdict(list)
    for r in rows:
        groups[str(r[key])].append(r)
    return {k: _stats(v) for k, v in sorted(groups.items())}


def _worker(core_start, core_end, output_path):
    q_start = core_start - timedelta(days=LOOKBACK_DAYS)
    q_end = core_end + timedelta(hours=FORWARD_HOURS)
    local_seen = set()
    rows_out = []

    with SessionLocal() as db:
        m5 = _load_range(db, "M5", q_start, q_end)
        m15 = _load_range(db, "M15", q_start, q_end)
        h1 = _load_range(db, "H1", q_start, q_end)

    rsi_times, rsi_vals = _rsi_series(h1)
    regime_times, regime_vals = _precompute_m15_regimes(m15)

    if m5:
        for i, bar in enumerate(m5):
            t = bar[0]
            if not (core_start <= t < core_end):
                continue
            if i < 700 or i + MAX_FORWARD_M5 >= len(m5):
                continue

            window = m5[i-699:i+1]
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
                dedupe = (sig.archetype, pe._trading_date(t).isoformat(), key_level)
                if dedupe in local_seen:
                    continue
                local_seen.add(dedupe)
                row = _replay(m5, i, sig)
                row.update({
                    "time": t.isoformat(),
                    "archetype": sig.archetype,
                    "entry": sig.entry,
                    "stop": sig.stop_loss,
                    "rr": sig.rr,
                    "session": sig.session,
                    "confidence": sig.confidence,
                    "regime_direction": d,
                    "regime_volatility": v,
                    "regime_cell": f"{d}×{v}",
                    "regime_multiplier": mult,
                    "dedupe_key": [sig.archetype, pe._trading_date(t).isoformat(), key_level],
                })
                rows_out.append(row)

    with open(output_path, "a", encoding="utf-8") as f:
        for row in rows_out:
            f.write(json.dumps(row, separators=(",", ":")) + "\n")

    print(json.dumps({
        "worker": True,
        "core_start": core_start.isoformat(),
        "core_end": core_end.isoformat(),
        "rows": len(rows_out),
        "asian": sum(1 for r in rows_out if r["archetype"] == "ASIAN_BREAKDOWN"),
        "pdl": sum(1 for r in rows_out if r["archetype"] == "PDL_BREAK"),
    }), flush=True)


def _parent():
    with SessionLocal() as db:
        start, end, raw_count = _bounds(db)

    fd, output_path = tempfile.mkstemp(prefix="predator_regime_", suffix=".jsonl", dir="/tmp")
    os.close(fd)
    try:
        cursor = start
        chunk_count = 0
        while cursor <= end:
            core_start = cursor
            core_end = min(end + timedelta(minutes=5), core_start + timedelta(days=CHUNK_DAYS))
            cmd = [
                sys.executable, os.path.abspath(__file__), "--worker",
                core_start.isoformat(), core_end.isoformat(), output_path,
            ]
            proc = subprocess.run(cmd, text=True, capture_output=True)
            if proc.stdout:
                print(proc.stdout.strip(), flush=True)
            if proc.returncode != 0:
                if proc.stderr:
                    print(proc.stderr, file=sys.stderr, flush=True)
                raise SystemExit(f"chunk worker failed rc={proc.returncode} start={core_start.isoformat()}")
            chunk_count += 1
            cursor = core_end

        all_rows = defaultdict(list)
        matched_rows = defaultdict(list)
        global_seen = set()
        with open(output_path, "r", encoding="utf-8") as f:
            for line in f:
                if not line.strip():
                    continue
                row = json.loads(line)
                key = tuple(row.pop("dedupe_key"))
                if key in global_seen:
                    continue
                global_seen.add(key)
                all_rows[row["archetype"]].append(row)
                if float(row.get("regime_multiplier", 0.0) or 0.0) >= 0.5:
                    matched_rows[row["archetype"]].append(row)

        report = {
            "mode": "READ_ONLY_FULL_HISTORY_POSTFIX_REGIME_REVALIDATION",
            "raw_m5_rows": raw_count,
            "history_start": start.isoformat(),
            "history_end": end.isoformat(),
            "chunk_days": CHUNK_DAYS,
            "chunks_completed": chunk_count,
            "execution_architecture": "fresh_subprocess_per_chunk",
            "regime_rule": "production favorable iff multiplier >= 0.5",
            "same_bar_rule": "SL_FIRST_CONSERVATIVE",
            "replay_horizon_m5_bars": MAX_FORWARD_M5,
            "dedupe": "first unique archetype+XAU_trading_date+key_level",
            "all_corrected_triggers": {},
            "production_regime_matched": {},
        }
        for arch in ("ASIAN_BREAKDOWN", "PDL_BREAK"):
            rows = all_rows[arch]
            mrows = matched_rows[arch]
            report["all_corrected_triggers"][arch] = {
                "overall": _stats(rows),
                "by_regime": _group_stats(rows, "regime_cell"),
                "by_session": _group_stats(rows, "session"),
            }
            report["production_regime_matched"][arch] = {
                "overall": _stats(mrows),
                "by_regime": _group_stats(mrows, "regime_cell"),
                "by_session": _group_stats(mrows, "session"),
            }
        print("=== FINAL REPORT ===")
        print(json.dumps(report, indent=2, sort_keys=True))
    finally:
        try:
            os.unlink(output_path)
        except OSError:
            pass


def main():
    if len(sys.argv) >= 5 and sys.argv[1] == "--worker":
        _worker(_parse_iso(sys.argv[2]), _parse_iso(sys.argv[3]), sys.argv[4])
        return
    _parent()


if __name__ == "__main__":
    main()
