"""
Diagnose WHY analyze_momentum_breakout fires zero times on a historical window.

Replicates the function's gate sequence but instead of early-returning on the
first failure, it records WHICH gate failed for every candle. Returns a per-gate
filtering breakdown so we can see which thresholds are too aggressive.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Iterable

from services.intraday_strategies import _atr, _ema, _in_killzone


def audit_momentum_gates(
    candles,
    *,
    pip_size: float = 1.0,
    body_atr_mults:  Iterable[float] = (1.0, 1.5, 2.0, 2.5),
    volume_mults:    Iterable[float] = (1.0, 1.2, 1.5, 2.0),
    close_pcts:      Iterable[float] = (0.60, 0.70, 0.80, 0.90),
    enable_killzone: bool = True,
) -> dict:
    """
    Returns a structured dict showing:
      - total candles audited
      - per-gate pass count at each threshold
      - combined gate sequence pass count for current production defaults
      - recommended thresholds based on what would produce a target number of trades
    """
    if not candles or len(candles) < 22:
        return {"error": "Need >= 22 candles"}

    # Skip warmup
    audit_start = 22
    audit_end   = len(candles)
    audited     = audit_end - audit_start

    # Per-gate independent counters
    gate_killzone_pass = 0
    gate_body_pass = {m: 0 for m in body_atr_mults}
    gate_volume_pass = {m: 0 for m in volume_mults}
    gate_close_pass  = {p: 0 for p in close_pcts}
    gate_ema_pass = 0

    # Combined (current production) sequence: kz AND body>=2 AND vol>=1.5 AND close>=0.80 AND ema-agree
    combined_pass = 0
    # Recommended tuning grid
    grid: dict[tuple, int] = {}    # (body, vol, close) -> count when all combined

    closes_all = [c.close for c in candles]

    for i in range(audit_start, audit_end):
        bar = candles[i]
        body = abs(bar.close - bar.open)
        rng  = bar.high - bar.low
        if rng <= 0:
            continue

        # ATR + EMA computed on history up to (but not including) this bar
        atr = _atr(candles[:i], period=14, pip_size=pip_size)
        if atr <= 0:
            continue
        body_mult = body / atr

        # Volume relative to 20-bar avg ending at i-1
        prev_vols = [c.volume for c in candles[max(0, i-20):i] if c.volume]
        if prev_vols:
            avg_vol = sum(prev_vols) / len(prev_vols)
            vol_mult = bar.volume / avg_vol if avg_vol > 0 else 0.0
        else:
            vol_mult = 0.0

        # Close-position
        bull = bar.close > bar.open
        close_pct = (bar.close - bar.low) / rng if bull else (bar.high - bar.close) / rng

        # Killzone
        ct = bar.time if bar.time.tzinfo else bar.time.replace(tzinfo=timezone.utc)
        in_kz = _in_killzone(ct)
        if in_kz: gate_killzone_pass += 1

        # EMA21 slope
        ema21 = _ema(closes_all[:i+1], 21)
        ema_ok = (bull and ema21[-1] > ema21[-3]) or ((not bull) and ema21[-1] < ema21[-3])
        if ema_ok: gate_ema_pass += 1

        # Per-threshold body
        for m in body_atr_mults:
            if body_mult >= m: gate_body_pass[m] += 1
        # Per-threshold volume
        for m in volume_mults:
            if vol_mult >= m: gate_volume_pass[m] += 1
        # Per-threshold close_pct
        for p in close_pcts:
            if close_pct >= p: gate_close_pass[p] += 1

        # Combined production defaults: body>=2, vol>=1.5, close>=0.80, ema agree, killzone
        gates = enable_killzone == False or in_kz
        gates = gates and (body_mult >= 2.0) and (vol_mult >= 1.5) and (close_pct >= 0.80) and ema_ok
        if gates: combined_pass += 1

        # Grid scan for tuning recommendations
        for bm in body_atr_mults:
            for vm in volume_mults:
                for cp in close_pcts:
                    if (vm <= vol_mult and bm <= body_mult and cp <= close_pct and
                        ema_ok and (not enable_killzone or in_kz)):
                        grid[(bm, vm, cp)] = grid.get((bm, vm, cp), 0) + 1

    # Sort grid: pick the LOOSEST that still produces 50+ trades, or the loosest overall
    grid_sorted = sorted(grid.items(), key=lambda kv: (-kv[1], kv[0]))
    recommend = []
    for (bm, vm, cp), count in grid_sorted[:8]:
        recommend.append({
            "min_body_atr_mult": bm, "min_volume_mult": vm, "min_close_pct": cp,
            "trades_would_fire": count,
        })

    return {
        "auditedCandles":   audited,
        "killzonePass":     gate_killzone_pass,
        "emaSlopePass":     gate_ema_pass,
        "bodyAtrPass":      gate_body_pass,
        "volumeMultPass":   gate_volume_pass,
        "closePctPass":     gate_close_pass,
        "combinedProductionPass": combined_pass,
        "productionDefaults": {
            "min_body_atr_mult": 2.0, "min_volume_mult": 1.5,
            "min_close_pct": 0.80,
        },
        "recommendedConfigs": recommend,
    }
