"""
Validate VP Trap Phase 1 — compute prev-day profiles across the full historic
dataset and report statistical distributions.

Sanity checks:
  1. Every weekday should yield a profile (no failures)
  2. VAL <= POC <= VAH (basic ordering)
  3. VAL >= PDL and VAH <= PDH (VA can't exceed the day's range)
  4. Total volume > 0
  5. Bar count reasonable (~24 H1 or ~96 M15 for a full day)
  6. Day type distribution has all four categories represented

Also prints per-cell diagnostics for a spot-checkable sample.

Run inside backend container:
  docker exec -e PYTHONPATH=/app xauusd-backend python /app/tools/validate_vp_trap_profiles.py
"""
from __future__ import annotations

import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from statistics import mean, median
from types import SimpleNamespace

from sqlalchemy import asc

from services.vp_trap_strategy import compute_prev_day_profile


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


def main():
    from database import SessionLocal
    from db_models import HistoricalCandle

    print("=" * 78)
    print(" VP TRAP PHASE 1 — HISTORIC VALIDATION")
    print("=" * 78)

    with SessionLocal() as db:
        # Discover the coverage window
        first = db.query(HistoricalCandle.candle_time).filter(
            HistoricalCandle.timeframe == "H1").order_by(asc(HistoricalCandle.candle_time)).first()
        last  = db.query(HistoricalCandle.candle_time).filter(
            HistoricalCandle.timeframe == "H1").order_by(HistoricalCandle.candle_time.desc()).first()
        if not first or not last:
            print("No H1 historical candles found. Abort.")
            return
        earliest = first[0] if first[0].tzinfo else first[0].replace(tzinfo=timezone.utc)
        latest   = last[0]  if last[0].tzinfo  else last[0].replace(tzinfo=timezone.utc)
        print(f"H1 coverage: {earliest.date()} → {latest.date()}  ({(latest-earliest).days} days)")

        # Walk each calendar day; skip weekends
        cur = (earliest + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
        end = latest.replace(hour=0, minute=0, second=0, microsecond=0)

        counters = {"attempted": 0, "succeeded": 0, "failed": 0, "skipped_weekend": 0}
        day_type_counts = Counter()
        vol_source_counts = Counter()
        ranges: list[float] = []
        va_widths: list[float] = []
        va_pcts_of_range: list[float] = []
        bar_counts: list[int] = []
        close_locations: list[float] = []
        pdc_above_vah = 0
        pdc_below_val = 0
        pdc_inside_va = 0
        ordering_failures = []
        va_bounds_failures = []
        zero_volume_failures = []
        samples: list[tuple[str, dict]] = []

        while cur <= end:
            counters["attempted"] += 1
            if cur.weekday() >= 5:
                counters["skipped_weekend"] += 1
                cur += timedelta(days=1)
                continue

            # Load a 4-day window covering the target day + 1 day of grace on each side
            h1  = _load_bars(db, "H1",  cur - timedelta(days=2), cur + timedelta(days=1, hours=6))
            m15 = _load_bars(db, "M15", cur - timedelta(days=2), cur + timedelta(days=1, hours=6))
            if not h1:
                counters["failed"] += 1
                cur += timedelta(days=1)
                continue

            # reference_time = start of the day AFTER target so `cur` is "yesterday"
            profile = compute_prev_day_profile(
                candles_h1=h1, candles_m15=m15 or None,
                reference_time=cur + timedelta(days=1),
            )
            if profile is None:
                counters["failed"] += 1
                cur += timedelta(days=1)
                continue

            counters["succeeded"] += 1
            day_type_counts[profile.day_type] += 1
            vol_source_counts[profile.volume_source] += 1
            ranges.append(profile.day_range_pts)
            va_widths.append(profile.vah - profile.val)
            va_pcts_of_range.append((profile.vah - profile.val) / max(0.01, profile.day_range_pts))
            bar_counts.append(profile.bar_count)
            close_locations.append(profile.close_location_in_range)
            if profile.pdc > profile.vah:
                pdc_above_vah += 1
            elif profile.pdc < profile.val:
                pdc_below_val += 1
            else:
                pdc_inside_va += 1

            # Correctness invariants
            if not (profile.val <= profile.poc <= profile.vah):
                ordering_failures.append(
                    f"{profile.profile_date}: VAL {profile.val} POC {profile.poc} VAH {profile.vah}")
            if not (profile.pdl - 1.0 <= profile.val and profile.vah <= profile.pdh + 1.0):
                va_bounds_failures.append(
                    f"{profile.profile_date}: PDL {profile.pdl} VAL {profile.val} VAH {profile.vah} PDH {profile.pdh}")
            if profile.total_volume <= 0:
                zero_volume_failures.append(profile.profile_date)

            # Keep first + last + a few middle samples
            if len(samples) < 3 or (counters["succeeded"] % 30 == 0):
                samples.append((profile.profile_date, {
                    "type":  profile.day_type,
                    "pdh":   profile.pdh, "pdl": profile.pdl, "pdc": profile.pdc,
                    "poc":   profile.poc, "vah": profile.vah, "val": profile.val,
                    "range": profile.day_range_pts, "bars": profile.bar_count,
                    "vwap":  profile.vwap,
                }))

            cur += timedelta(days=1)

        # ── REPORT ────────────────────────────────────────────────────────
        print()
        print("── ATTEMPTS ─────────────────────────────────────────────────")
        print(f"  Days walked:       {counters['attempted']}")
        print(f"  Weekends skipped:  {counters['skipped_weekend']}")
        print(f"  Weekday attempts:  {counters['attempted'] - counters['skipped_weekend']}")
        print(f"  Profiles ok:       {counters['succeeded']}")
        print(f"  Failures:          {counters['failed']}")

        if counters["succeeded"] == 0:
            print("\nNo successful profiles. Investigation needed.")
            return

        success_pct = 100.0 * counters["succeeded"] / max(1, counters["attempted"] - counters["skipped_weekend"])
        print(f"  Success rate:      {success_pct:.1f}%")

        print()
        print("── DAY-TYPE DISTRIBUTION ────────────────────────────────────")
        for dt, n in day_type_counts.most_common():
            print(f"  {dt:14}  {n:>4}  ({100*n/counters['succeeded']:5.1f}%)")

        print()
        print("── VOLUME SOURCE ────────────────────────────────────────────")
        for src, n in vol_source_counts.most_common():
            print(f"  {src:14}  {n:>4}  ({100*n/counters['succeeded']:5.1f}%)")

        print()
        print("── DAY RANGE (points) ───────────────────────────────────────")
        print(f"  min:    {min(ranges):.2f}")
        print(f"  p25:    {sorted(ranges)[len(ranges)//4]:.2f}")
        print(f"  median: {median(ranges):.2f}")
        print(f"  mean:   {mean(ranges):.2f}")
        print(f"  p75:    {sorted(ranges)[3*len(ranges)//4]:.2f}")
        print(f"  max:    {max(ranges):.2f}")

        print()
        print("── VALUE AREA ───────────────────────────────────────────────")
        print(f"  VA width median: {median(va_widths):.2f} pts")
        print(f"  VA/range median: {median(va_pcts_of_range):.2%}")
        print(f"  Bar count median: {median(bar_counts)} ({int(min(bar_counts))} .. {int(max(bar_counts))})")

        print()
        print("── CLOSE LOCATION ───────────────────────────────────────────")
        print(f"  Median location in range: {median(close_locations):.2f}  (0=at low, 1=at high)")
        print(f"  PDC above VAH:            {pdc_above_vah}  ({100*pdc_above_vah/counters['succeeded']:.1f}%)")
        print(f"  PDC below VAL:            {pdc_below_val}  ({100*pdc_below_val/counters['succeeded']:.1f}%)")
        print(f"  PDC inside VA:            {pdc_inside_va}  ({100*pdc_inside_va/counters['succeeded']:.1f}%)")

        print()
        print("── CORRECTNESS INVARIANTS ───────────────────────────────────")
        print(f"  Ordering VAL<=POC<=VAH failures:   {len(ordering_failures)}")
        if ordering_failures[:3]:
            for line in ordering_failures[:3]:
                print(f"    {line}")
        print(f"  VA within PDL..PDH bounds failures: {len(va_bounds_failures)}")
        if va_bounds_failures[:3]:
            for line in va_bounds_failures[:3]:
                print(f"    {line}")
        print(f"  Zero-volume day failures:           {len(zero_volume_failures)}")

        print()
        print("── SAMPLE PROFILES ──────────────────────────────────────────")
        for date, s in samples[:8]:
            print(f"  {date}  [{s['type']:<11}]  PDH={s['pdh']:<8} PDL={s['pdl']:<8} PDC={s['pdc']:<8}  "
                  f"POC={s['poc']:<8} VAH={s['vah']:<8} VAL={s['val']:<8}  "
                  f"range={s['range']:<6}  bars={s['bars']}")

        # Trap-buyer / trap-seller candidate summary — where the strategy will find setups
        print()
        print("── TRAP-CANDIDATE DAYS (Phase 2 targets) ────────────────────")
        print("  Days closing OUTSIDE value area are the highest-conviction")
        print("  trap-zone candidates. Both above-VAH and below-VAL trap")
        print("  setups become active for the following session.")
        print()
        print(f"  Trapped-BUYER candidates (PDC > VAH):  {pdc_above_vah} days ({100*pdc_above_vah/counters['succeeded']:.1f}%)")
        print(f"  Trapped-SELLER candidates (PDC < VAL): {pdc_below_val} days ({100*pdc_below_val/counters['succeeded']:.1f}%)")
        print(f"  Balanced (PDC inside VA):              {pdc_inside_va} days ({100*pdc_inside_va/counters['succeeded']:.1f}%)")

        expected_trap_frequency = (pdc_above_vah + pdc_below_val) / counters["succeeded"]
        print()
        print(f"  Expected trap-candidate frequency: {expected_trap_frequency:.1%}")
        print(f"  Of those, only a fraction develop into TRIGGERED signals")
        print(f"  after Phase 2 detection + Phase 3 scoring gates.")


if __name__ == "__main__":
    main()
