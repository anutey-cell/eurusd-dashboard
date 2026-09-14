"""
Diagnose WHY analyze_momentum_breakout fires or rejects bars on a historical window.

Replicates the strategy's core gate sequence but, instead of early-returning on
the first failure, records how many bars pass each gate. Canonical production
thresholds are read from analyze_momentum_breakout's function signature so the
audit cannot silently drift from the live momentum alert path again.
"""
from __future__ import annotations

import inspect
from datetime import timezone
from typing import Iterable

from services.intraday_strategies import (
    _atr,
    _ema,
    _in_killzone,
    analyze_momentum_breakout,
)


def _canonical_defaults() -> dict[str, float]:
    """Read the live strategy defaults directly from its public signature."""
    sig = inspect.signature(analyze_momentum_breakout)
    return {
        "min_body_atr_mult": float(sig.parameters["min_body_atr_mult"].default),
        "min_volume_mult": float(sig.parameters["min_volume_mult"].default),
        "min_close_pct": float(sig.parameters["min_close_pct"].default),
        "max_sl_pts": float(sig.parameters["max_sl_pts"].default),
    }


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
      - combined pass count using the SAME canonical thresholds as the live path
      - candidate tuning combinations for research only

    This audit intentionally does not label looser grid combinations as
    production settings. Production truth comes only from
    analyze_momentum_breakout's defaults.
    """
    if not candles or len(candles) < 22:
        return {"error": "Need >= 22 candles"}

    defaults = _canonical_defaults()
    prod_body = defaults["min_body_atr_mult"]
    prod_volume = defaults["min_volume_mult"]
    prod_close = defaults["min_close_pct"]

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

    combined_pass = 0
    # Research grid only — not production truth.
    grid: dict[tuple, int] = {}

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

        # Volume relative to 20-bar avg ending at i-1. The live strategy skips
        # the volume gate if no usable volume baseline exists, so preserve that
        # behavior in the combined pass calculation.
        prev_vols = [c.volume for c in candles[max(0, i-20):i] if c.volume]
        volume_gate_available = bool(prev_vols)
        if prev_vols:
            avg_vol = sum(prev_vols) / len(prev_vols)
            if avg_vol > 0:
                vol_mult = bar.volume / avg_vol
            else:
                vol_mult = 0.0
                volume_gate_available = False
        else:
            vol_mult = 0.0

        # Close-position
        bull = bar.close > bar.open
        close_pct = (bar.close - bar.low) / rng if bull else (bar.high - bar.close) / rng

        # Killzone
        ct = bar.time if bar.time.tzinfo else bar.time.replace(tzinfo=timezone.utc)
        in_kz = _in_killzone(ct)
        if in_kz:
            gate_killzone_pass += 1

        # EMA21 slope
        ema21 = _ema(closes_all[:i+1], 21)
        ema_ok = (bull and ema21[-1] > ema21[-3]) or ((not bull) and ema21[-1] < ema21[-3])
        if ema_ok:
            gate_ema_pass += 1

        # Per-threshold body
        for m in body_atr_mults:
            if body_mult >= m:
                gate_body_pass[m] += 1
        # Per-threshold volume (descriptive only when volume exists)
        for m in volume_mults:
            if volume_gate_available and vol_mult >= m:
                gate_volume_pass[m] += 1
        # Per-threshold close_pct
        for p in close_pcts:
            if close_pct >= p:
                gate_close_pass[p] += 1

        # Combined canonical defaults. No-volume data follows live behavior:
        # volume does not veto a bar when a usable baseline is absent.
        volume_ok = (not volume_gate_available) or (vol_mult >= prod_volume)
        gates = (not enable_killzone) or in_kz
        gates = (
            gates
            and body_mult >= prod_body
            and volume_ok
            and close_pct >= prod_close
            and ema_ok
        )
        if gates:
            combined_pass += 1

        # Grid scan for research diagnostics. If volume is unavailable, mirror
        # the live strategy and treat the volume gate as non-vetoing.
        for bm in body_atr_mults:
            for vm in volume_mults:
                for cp in close_pcts:
                    grid_volume_ok = (not volume_gate_available) or (vol_mult >= vm)
                    if (
                        bm <= body_mult
                        and grid_volume_ok
                        and cp <= close_pct
                        and ema_ok
                        and (not enable_killzone or in_kz)
                    ):
                        grid[(bm, vm, cp)] = grid.get((bm, vm, cp), 0) + 1

    grid_sorted = sorted(grid.items(), key=lambda kv: (-kv[1], kv[0]))
    research_configs = []
    for (bm, vm, cp), count in grid_sorted[:8]:
        research_configs.append({
            "min_body_atr_mult": bm,
            "min_volume_mult": vm,
            "min_close_pct": cp,
            "trades_would_fire": count,
        })

    return {
        "auditedCandles": audited,
        "killzonePass": gate_killzone_pass,
        "emaSlopePass": gate_ema_pass,
        "bodyAtrPass": gate_body_pass,
        "volumeMultPass": gate_volume_pass,
        "closePctPass": gate_close_pass,
        "combinedProductionPass": combined_pass,
        "productionDefaults": defaults,
        "researchConfigs": research_configs,
    }
