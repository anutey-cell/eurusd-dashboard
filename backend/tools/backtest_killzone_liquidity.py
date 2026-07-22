"""
Killzone Liquidity Analyzer
============================

Per-killzone developing volume profile (dPOC, dVAH, dVAL, VWAP) across
the full historical dataset. Identifies:

  1. Where liquidity concentrates within each killzone (POC location)
  2. Which killzones have TIGHT vs SPREAD-out value areas
  3. Whether prior-killzone POC/VAH/VAL act as MAGNETS/S/R for the next
  4. High-probability liquidity reference levels the operator can layer
     onto signals from other engines

Killzones (UTC):
  asian_early  22-24
  asian         0-6
  london_pre    6-7
  london_kz     7-10
  overlap      10-13
  ny_kz        13-16
  ny_pm        16-22

Metrics per killzone per day:
  session_high   / session_low
  dPOC           highest-volume price bin
  dVAH / dVAL    70% value area boundaries
  VWAP           bar-anchored typical-price × volume
  bar_count      how many M15 bars fed the histogram
  poc_loc_range  0=at session low, 1=at session high
  poc_loc_va     -1=below VA, 0=middle, +1=above VA

Cross-killzone reference measures:
  Does NEXT killzone open ABOVE / INSIDE / BELOW prior POC?
  Does NEXT killzone TOUCH prior POC (magnet)?
  Does NEXT killzone SWEEP prior high/low?

Run:
  ssh doxau 'docker exec -e PYTHONPATH=/app xauusd-backend python /app/tools/backtest_killzone_liquidity.py'
"""
from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from statistics import mean, median, stdev
from types import SimpleNamespace
from typing import Optional

from sqlalchemy import asc


# ── Killzone definitions (UTC hours, half-open [start, end)) ────────────────

KILLZONES = [
    ("asian_early", 22, 24),
    ("asian",        0,  6),
    ("london_pre",   6,  7),
    ("london_kz",    7, 10),
    ("overlap",     10, 13),
    ("ny_kz",       13, 16),
    ("ny_pm",       16, 22),
]

# Cross-KZ reference chains — "what does the CURRENT KZ do relative to the PRIOR KZ?"
KZ_TRANSITIONS = [
    ("asian",       "london_pre"),
    ("asian",       "london_kz"),
    ("london_pre",  "london_kz"),
    ("london_kz",   "overlap"),
    ("london_kz",   "ny_kz"),
    ("overlap",     "ny_kz"),
    ("ny_kz",       "ny_pm"),
]

VALUE_AREA_PCT = 0.70


# ── Data helpers ─────────────────────────────────────────────────────────────

def _load_bars(db, timeframe: str, start: datetime, end: datetime) -> list:
    from db_models import HistoricalCandle
    rows = (db.query(HistoricalCandle)
              .filter(HistoricalCandle.timeframe == timeframe)
              .filter(HistoricalCandle.candle_time >= start)
              .filter(HistoricalCandle.candle_time <= end)
              .order_by(asc(HistoricalCandle.candle_time))
              .all())
    return [SimpleNamespace(
        time=(r.candle_time if r.candle_time.tzinfo else r.candle_time.replace(tzinfo=timezone.utc)),
        open=r.open, high=r.high, low=r.low, close=r.close,
        volume=r.volume or 0,
    ) for r in rows]


def _bars_in_kz(bars: list, hour_lo: int, hour_hi: int, date_utc) -> list:
    """M15 bars in [hour_lo, hour_hi) UTC on the given calendar date.
    Handles asian_early wrap (22-24) by NOT wrapping — it's inclusive of the
    date's late-evening block; caller handles day boundary if needed."""
    if hour_hi > 24:  # not needed today but safe
        hour_hi = 24
    day_start = datetime(date_utc.year, date_utc.month, date_utc.day,
                         hour_lo, tzinfo=timezone.utc)
    if hour_hi == 24:
        day_end = datetime(date_utc.year, date_utc.month, date_utc.day,
                           23, 59, 59, tzinfo=timezone.utc) + timedelta(seconds=1)
    else:
        day_end = datetime(date_utc.year, date_utc.month, date_utc.day,
                           hour_hi, tzinfo=timezone.utc)
    return [c for c in bars
            if day_start <= (c.time if c.time.tzinfo else c.time.replace(tzinfo=timezone.utc))
                            .astimezone(timezone.utc) < day_end]


# ── Volume histogram + Value Area ───────────────────────────────────────────

def _histogram(bars: list, bin_size: float) -> dict[float, float]:
    """Distribute each bar's volume uniformly across its price range bins."""
    if not bars or bin_size <= 0:
        return {}
    hist: dict[float, float] = {}
    for b in bars:
        lo, hi = min(b.low, b.high), max(b.low, b.high)
        n_bins = max(1, int(round((hi - lo) / bin_size)))
        v_per = (b.volume or 0) / n_bins
        if v_per <= 0:
            continue
        start = round(lo / bin_size) * bin_size
        for i in range(n_bins):
            k = round(start + i * bin_size, 2)
            hist[k] = hist.get(k, 0.0) + v_per
    return hist


def _value_area(hist: dict[float, float], pct: float = 0.70) -> tuple[float, float, float]:
    if not hist:
        return (0.0, 0.0, 0.0)
    total = sum(hist.values())
    if total <= 0:
        return (0.0, 0.0, 0.0)
    poc = max(hist.keys(), key=lambda k: hist[k])
    sorted_bins = sorted(hist.keys())
    idx = sorted_bins.index(poc)
    lo_i = hi_i = idx
    acc = hist[sorted_bins[idx]]
    target = total * pct
    while acc < target and (lo_i > 0 or hi_i < len(sorted_bins) - 1):
        below = hist[sorted_bins[lo_i - 1]] if lo_i > 0 else -1.0
        above = hist[sorted_bins[hi_i + 1]] if hi_i < len(sorted_bins) - 1 else -1.0
        if below < 0 and above < 0:
            break
        if below >= above:
            lo_i -= 1
            acc += hist[sorted_bins[lo_i]]
        else:
            hi_i += 1
            acc += hist[sorted_bins[hi_i]]
    return (sorted_bins[lo_i], sorted_bins[hi_i], poc)


def _vwap(bars: list) -> Optional[float]:
    if not bars:
        return None
    num, den = 0.0, 0.0
    for b in bars:
        tp = (b.high + b.low + b.close) / 3.0
        v  = b.volume or 0
        num += tp * v
        den += v
    if den <= 0:
        return None
    return round(num / den, 2)


def _kz_metrics(bars: list) -> Optional[dict]:
    if not bars:
        return None
    hi = max(b.high for b in bars)
    lo = min(b.low  for b in bars)
    rng = hi - lo
    if rng <= 0:
        return None
    bin_size = max(0.5, min(5.0, rng / 30.0))
    hist = _histogram(bars, bin_size)
    if not hist:
        return None
    val, vah, poc = _value_area(hist, VALUE_AREA_PCT)
    vwap = _vwap(bars)
    total_vol = sum(hist.values())
    return {
        "session_high":  round(hi, 2),
        "session_low":   round(lo, 2),
        "range":         round(rng, 2),
        "dpoc":          round(poc, 2),
        "dvah":          round(vah, 2),
        "dval":          round(val, 2),
        "vwap":          vwap,
        "va_width":      round(vah - val, 2),
        "bar_count":     len(bars),
        "total_volume":  round(total_vol, 0),
        "first_time":    (bars[0].time if bars[0].time.tzinfo
                          else bars[0].time.replace(tzinfo=timezone.utc)
                          ).astimezone(timezone.utc).isoformat(),
        "last_close":    round(bars[-1].close, 2),
        "poc_loc_range": round((poc - lo) / rng, 3),
        "poc_loc_va":    round(((poc - val) / max(0.01, vah - val)) * 2 - 1, 3),
    }


# ── Cross-killzone reference analysis ───────────────────────────────────────

def _cross_kz_stats(prior_metrics: dict, current_bars: list) -> dict:
    """
    How does the CURRENT killzone's price interact with the PRIOR killzone's
    POC / VAH / VAL / VWAP? Returns per-metric touch / cross / reject stats.
    """
    if not prior_metrics or not current_bars:
        return {}
    cur_hi = max(b.high for b in current_bars)
    cur_lo = min(b.low  for b in current_bars)
    cur_open  = current_bars[0].open
    cur_close = current_bars[-1].close

    def _rel(level: Optional[float]) -> dict:
        if level is None:
            return {"present": False}
        opened_above = cur_open > level
        closed_above = cur_close > level
        touched = cur_lo <= level <= cur_hi
        magnetized = touched
        crossed = opened_above != closed_above
        return {
            "present":       True,
            "level":         round(level, 2),
            "opened_above":  opened_above,
            "closed_above":  closed_above,
            "touched":       touched,       # price crossed the level at some point
            "crossed":       crossed,       # open one side, close other side
        }

    return {
        "poc":  _rel(prior_metrics.get("dpoc")),
        "vah":  _rel(prior_metrics.get("dvah")),
        "val":  _rel(prior_metrics.get("dval")),
        "vwap": _rel(prior_metrics.get("vwap")),
    }


# ── Main analysis ───────────────────────────────────────────────────────────

def main():
    from database import SessionLocal
    from db_models import HistoricalCandle

    print("=" * 78)
    print(" KILLZONE LIQUIDITY ANALYZER — dPOC / dVAH / dVAL / VWAP")
    print("=" * 78)

    with SessionLocal() as db:
        first = db.query(HistoricalCandle.candle_time).filter(
            HistoricalCandle.timeframe == "M15").order_by(asc(HistoricalCandle.candle_time)).first()
        last  = db.query(HistoricalCandle.candle_time).filter(
            HistoricalCandle.timeframe == "M15").order_by(HistoricalCandle.candle_time.desc()).first()
        if not first or not last:
            print("No M15 data — abort.")
            return
        earliest = first[0] if first[0].tzinfo else first[0].replace(tzinfo=timezone.utc)
        latest   = last[0]  if last[0].tzinfo  else last[0].replace(tzinfo=timezone.utc)
        print(f"M15 coverage: {earliest.date()} → {latest.date()}")
        print()

        # per_kz[kz_name] = list of daily metric dicts
        per_kz: dict[str, list] = defaultdict(list)
        # transition[(prior, current)] = list of cross-kz stat dicts
        transition_stats: dict[tuple, list] = defaultdict(list)

        cur = earliest.replace(hour=0, minute=0, second=0, microsecond=0)
        end = latest.replace(hour=0, minute=0, second=0, microsecond=0)
        days_walked = 0
        while cur <= end:
            if cur.weekday() >= 5:   # skip weekends
                cur += timedelta(days=1)
                continue
            m15 = _load_bars(db, "M15", cur - timedelta(hours=2),
                                          cur + timedelta(hours=25))
            if not m15:
                cur += timedelta(days=1)
                continue
            days_walked += 1

            # Per-day per-KZ metrics
            day_kz_metrics: dict[str, dict] = {}
            for name, lo, hi in KILLZONES:
                bars = _bars_in_kz(m15, lo, hi, cur.date())
                m = _kz_metrics(bars)
                if m is not None:
                    day_kz_metrics[name] = m
                    per_kz[name].append(m)

            # Cross-KZ reference
            for prior_name, current_name in KZ_TRANSITIONS:
                prior_m = day_kz_metrics.get(prior_name)
                # current_bars = the bars of the current KZ
                cur_lo, cur_hi = next((lo, hi) for n, lo, hi in KILLZONES if n == current_name)
                current_bars = _bars_in_kz(m15, cur_lo, cur_hi, cur.date())
                if not prior_m or not current_bars:
                    continue
                stats = _cross_kz_stats(prior_m, current_bars)
                if stats:
                    transition_stats[(prior_name, current_name)].append(stats)

            cur += timedelta(days=1)

        # ── REPORT — per-killzone liquidity profile ────────────────────────
        print(f"Weekdays walked: {days_walked}")
        print()
        print("=" * 78)
        print(" PER-KILLZONE LIQUIDITY PROFILE  (median values)")
        print("=" * 78)
        print()
        print(f"  {'Killzone':<13} {'N':>4} {'Range':>7} {'VA width':>9} "
              f"{'POC loc':>8} {'Vol':>10}")
        print("  " + "-" * 70)
        for name, _, _ in KILLZONES:
            days = per_kz.get(name, [])
            if not days:
                print(f"  {name:<13} {'-':>4}")
                continue
            rng     = median(d["range"]     for d in days)
            va_w    = median(d["va_width"]  for d in days)
            poc_loc = median(d["poc_loc_range"] for d in days)
            vol     = median(d["total_volume"] for d in days)
            print(f"  {name:<13} {len(days):>4} {rng:>7.1f} {va_w:>9.1f} "
                  f"{poc_loc:>7.2f}  {vol:>10.0f}")

        print()
        print("  POC location: 0=at session low, 0.5=middle, 1=at session high")
        print()

        # ── REPORT — POC location distribution ──────────────────────────
        print("=" * 78)
        print(" POC LOCATION DISTRIBUTION per killzone")
        print("=" * 78)
        print("  Where does dPOC settle within the killzone's own range?")
        print()
        print(f"  {'Killzone':<13} {'Bottom 20%':>11} {'20-40':>8} {'Mid':>8} "
              f"{'60-80':>8} {'Top 20%':>10}")
        print("  " + "-" * 68)
        for name, _, _ in KILLZONES:
            days = per_kz.get(name, [])
            if not days:
                continue
            buckets = [0, 0, 0, 0, 0]
            for d in days:
                loc = d["poc_loc_range"]
                if   loc < 0.2:  buckets[0] += 1
                elif loc < 0.4:  buckets[1] += 1
                elif loc < 0.6:  buckets[2] += 1
                elif loc < 0.8:  buckets[3] += 1
                else:            buckets[4] += 1
            n = len(days)
            pcts = [100 * b / n for b in buckets]
            print(f"  {name:<13} {pcts[0]:>10.1f}% {pcts[1]:>7.1f}% "
                  f"{pcts[2]:>7.1f}% {pcts[3]:>7.1f}% {pcts[4]:>9.1f}%")

        print()

        # ── REPORT — RANGE distribution ─────────────────────────────────
        print("=" * 78)
        print(" RANGE distribution per killzone (points)")
        print("=" * 78)
        print(f"  {'Killzone':<13} {'p25':>6} {'median':>7} {'p75':>6} {'mean':>7}")
        print("  " + "-" * 45)
        for name, _, _ in KILLZONES:
            days = per_kz.get(name, [])
            if not days:
                continue
            ranges = sorted(d["range"] for d in days)
            print(f"  {name:<13} {ranges[len(ranges)//4]:>6.1f} "
                  f"{median(ranges):>7.1f} {ranges[3*len(ranges)//4]:>6.1f} "
                  f"{mean(ranges):>7.1f}")

        print()

        # ── REPORT — CROSS-KZ magnet behavior ───────────────────────────
        print("=" * 78)
        print(" CROSS-KILLZONE MAGNET / TOUCH RATES")
        print("=" * 78)
        print()
        print("  How often does the CURRENT killzone TOUCH the PRIOR killzone's")
        print("  key levels? A high touch rate means the prior level is a real")
        print("  magnet the operator can use as reference.")
        print()
        print(f"  {'Transition':<32} {'POC':>7} {'VAH':>7} {'VAL':>7} {'VWAP':>7}")
        print("  " + "-" * 66)
        for (prior_name, current_name), records in transition_stats.items():
            if not records:
                continue
            n = len(records)
            # touch rates
            touched = {
                "poc":  sum(1 for r in records if r.get("poc",  {}).get("touched")),
                "vah":  sum(1 for r in records if r.get("vah",  {}).get("touched")),
                "val":  sum(1 for r in records if r.get("val",  {}).get("touched")),
                "vwap": sum(1 for r in records if r.get("vwap", {}).get("touched")),
            }
            label = f"{prior_name} → {current_name}"
            print(f"  {label:<32} {100*touched['poc']/n:>6.1f}% "
                  f"{100*touched['vah']/n:>6.1f}% "
                  f"{100*touched['val']/n:>6.1f}% "
                  f"{100*touched['vwap']/n:>6.1f}%  (n={n})")

        print()

        # ── REPORT — CROSSED (open one side, close other) ───────────────
        print("=" * 78)
        print(" CROSS-KILLZONE 'CROSSED THROUGH' RATES")
        print("=" * 78)
        print()
        print("  When the current KZ CROSSES the prior POC (open one side, close")
        print("  other), that's a regime-shift signal.")
        print()
        print(f"  {'Transition':<32} {'POC-x':>7} {'VAH-x':>7} {'VAL-x':>7} {'VWAP-x':>7}")
        print("  " + "-" * 66)
        for (prior_name, current_name), records in transition_stats.items():
            if not records:
                continue
            n = len(records)
            crossed = {
                "poc":  sum(1 for r in records if r.get("poc",  {}).get("crossed")),
                "vah":  sum(1 for r in records if r.get("vah",  {}).get("crossed")),
                "val":  sum(1 for r in records if r.get("val",  {}).get("crossed")),
                "vwap": sum(1 for r in records if r.get("vwap", {}).get("crossed")),
            }
            label = f"{prior_name} → {current_name}"
            print(f"  {label:<32} {100*crossed['poc']/n:>6.1f}% "
                  f"{100*crossed['vah']/n:>6.1f}% "
                  f"{100*crossed['val']/n:>6.1f}% "
                  f"{100*crossed['vwap']/n:>6.1f}%  (n={n})")

        print()

        # ── REPORT — Actionable takeaways ───────────────────────────────
        print("=" * 78)
        print(" ACTIONABLE TAKEAWAYS")
        print("=" * 78)
        print()

        # Which killzone has POC settling near an extreme most often?
        for name, _, _ in KILLZONES:
            days = per_kz.get(name, [])
            if not days:
                continue
            near_high = sum(1 for d in days if d["poc_loc_range"] >= 0.8)
            near_low  = sum(1 for d in days if d["poc_loc_range"] <= 0.2)
            n = len(days)
            if near_high / n >= 0.30:
                print(f"  {name}: POC near HIGH in {100*near_high/n:.0f}% of sessions "
                      f"→ bullish-persistent killzone")
            if near_low / n >= 0.30:
                print(f"  {name}: POC near LOW in {100*near_low/n:.0f}% of sessions "
                      f"→ bearish-persistent killzone")

        print()

        # Highest touch rates across transitions (most reliable magnets)
        print("  Strongest liquidity magnets (highest touch rate on prior POC):")
        magnets = []
        for (prior_name, current_name), records in transition_stats.items():
            if not records:
                continue
            n = len(records)
            poc_touch_rate = sum(1 for r in records if r.get("poc", {}).get("touched")) / n
            magnets.append((poc_touch_rate, prior_name, current_name, n))
        magnets.sort(reverse=True)
        for rate, p, c, n in magnets[:5]:
            print(f"    {p:<12} POC magnetizes {c:<12} in {100*rate:>5.1f}% "
                  f"of sessions  (n={n})")

        print()
        print("  Widest ranges (highest volatility killzones):")
        rng_ranking = sorted(
            [(median(d["range"] for d in per_kz[name]), name) for name, _, _ in KILLZONES
             if per_kz.get(name)], reverse=True)
        for rng, name in rng_ranking[:3]:
            print(f"    {name:<13} median range {rng:.1f} pts")

        print()
        print("  These reference levels can be used to enrich signals from any")
        print("  engine (mandate / VP Trap / momentum). For example: if mandate")
        print("  fires SELL and current price is 20pt+ above prior-KZ POC, that")
        print("  amplifies the signal (mean-reversion toward the magnet).")


if __name__ == "__main__":
    main()
