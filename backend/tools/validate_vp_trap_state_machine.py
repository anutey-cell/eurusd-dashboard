"""
Validate the state machine — for each of the 133 historic profiles, walk
forward through the NEXT day's M15 bars and check what state each of the
4 zones reaches. Reports the distribution of final states so we can
verify the state machine is discriminating properly.

Expected sanity signals:
  - LEVEL_DETECTED count should be low (most zones do get touched)
  - Terminal states EXPIRED / INVALIDATED should appear
  - TRIGGERED count should be a small minority (that's the whole point —
    high signal quality, low signal count)
"""
from __future__ import annotations

import sys
from collections import Counter
from datetime import datetime, timedelta, timezone
from statistics import mean, median
from types import SimpleNamespace

from sqlalchemy import asc

from services.vp_trap_strategy import compute_prev_day_profile
from services.vp_trap_state import (
    zones_from_profile, scan_zone,
    STATE_LEVEL_DETECTED, STATE_BREAKOUT_SEEN, STATE_TRAP_ARMED,
    STATE_WAITING_RETEST, STATE_RETEST_ACTIVE, STATE_TRIGGERED,
    STATE_INVALIDATED, STATE_EXPIRED,
)


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
    print(" VP TRAP PHASE 2 — STATE MACHINE VALIDATION")
    print("=" * 78)

    with SessionLocal() as db:
        first = db.query(HistoricalCandle.candle_time).filter(
            HistoricalCandle.timeframe == "H1").order_by(asc(HistoricalCandle.candle_time)).first()
        last  = db.query(HistoricalCandle.candle_time).filter(
            HistoricalCandle.timeframe == "H1").order_by(HistoricalCandle.candle_time.desc()).first()
        earliest = first[0] if first[0].tzinfo else first[0].replace(tzinfo=timezone.utc)
        latest   = last[0]  if last[0].tzinfo  else last[0].replace(tzinfo=timezone.utc)
        print(f"Dataset: {earliest.date()} → {latest.date()}")

        # For each weekday, compute profile of THAT day and scan against NEXT day's bars
        cur = (earliest + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
        end = (latest - timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)

        stats = {
            "profile_days":       0,
            "zones_scanned":      0,
            "state_by_type":      {"PDH": Counter(), "PDL": Counter(),
                                   "VAH": Counter(), "VAL": Counter()},
            "state_total":        Counter(),
            "triggered_days":     [],   # dates when at least one zone triggered
            "triggered_details":  [],   # first 20 triggered zones with context
        }

        while cur <= end:
            if cur.weekday() >= 5:
                cur += timedelta(days=1)
                continue

            # Build profile from THIS day's bars
            h1_prev  = _load_bars(db, "H1",  cur - timedelta(days=1), cur + timedelta(hours=1))
            m15_prev = _load_bars(db, "M15", cur - timedelta(days=1), cur + timedelta(hours=1))
            if not h1_prev:
                cur += timedelta(days=1)
                continue
            profile = compute_prev_day_profile(
                candles_h1=h1_prev, candles_m15=m15_prev or None,
                reference_time=cur + timedelta(days=1),
            )
            if profile is None:
                cur += timedelta(days=1)
                continue

            # Walk against NEXT calendar day's bars
            next_day_start = cur + timedelta(days=1)
            next_day_end   = cur + timedelta(days=2)
            m15_next = _load_bars(db, "M15", next_day_start, next_day_end)
            if not m15_next:
                cur += timedelta(days=1)
                continue

            stats["profile_days"] += 1
            # Fake "now" = end of the walk window so expiry logic is honest
            walk_now = next_day_end

            zones = zones_from_profile(profile, expiry_hours=48)
            for z in zones:
                scan_zone(z, m15_next,
                          min_displacement_pts=5.0,
                          retest_tolerance_pts=3.0,
                          max_retests=3,
                          now_utc=walk_now)
                stats["zones_scanned"] += 1
                stats["state_by_type"][z.level_type][z.state] += 1
                stats["state_total"][z.state] += 1

                if z.state == STATE_TRIGGERED and len(stats["triggered_details"]) < 20:
                    stats["triggered_details"].append({
                        "date":       profile.profile_date,
                        "level":      z.level_type,
                        "side":       z.level_side,
                        "reference":  z.reference_price,
                        "displacement": z.displacement_pts,
                        "retest_count": z.retest_count,
                    })
                if z.state == STATE_TRIGGERED and profile.profile_date not in stats["triggered_days"]:
                    stats["triggered_days"].append(profile.profile_date)

            cur += timedelta(days=1)

        # ── REPORT ──────────────────────────────────────────────────────────
        print()
        print("── SCAN COUNTS ──────────────────────────────────────────────")
        print(f"  Profile days:          {stats['profile_days']}")
        print(f"  Zones scanned:         {stats['zones_scanned']}")

        print()
        print("── FINAL STATE DISTRIBUTION (all zones) ─────────────────────")
        total = sum(stats['state_total'].values()) or 1
        state_order = [
            STATE_LEVEL_DETECTED, STATE_BREAKOUT_SEEN, STATE_TRAP_ARMED,
            STATE_WAITING_RETEST, STATE_RETEST_ACTIVE, STATE_TRIGGERED,
            STATE_INVALIDATED, STATE_EXPIRED,
        ]
        for st in state_order:
            n = stats['state_total'].get(st, 0)
            pct = 100.0 * n / total
            bar = "█" * min(30, int(pct * 30 / 100))
            print(f"  {st:<18} {n:>5}  {pct:>5.1f}%  {bar}")

        print()
        print("── STATE BY LEVEL TYPE ──────────────────────────────────────")
        header = f"  {'Level':<6} "
        for st in state_order:
            header += f"{st[:12]:>13}"
        print(header)
        for level in ("PDH", "VAH", "VAL", "PDL"):
            row = f"  {level:<6} "
            for st in state_order:
                n = stats['state_by_type'][level].get(st, 0)
                row += f"{n:>13}"
            print(row)

        print()
        print("── TRIGGERED SIGNALS (Phase 2 detection candidates) ─────────")
        n_triggered = stats['state_total'].get(STATE_TRIGGERED, 0)
        pct_trigger = 100.0 * n_triggered / total
        print(f"  Total TRIGGERED zones: {n_triggered}  ({pct_trigger:.2f}% of zones)")
        print(f"  Days with 1+ trigger:  {len(stats['triggered_days'])}  "
              f"({100.0*len(stats['triggered_days'])/max(1, stats['profile_days']):.1f}% of days)")
        print()
        print("  Sample TRIGGERED zones (first 15):")
        for t in stats["triggered_details"][:15]:
            print(f"    {t['date']}  {t['level']:>3}={t['reference']:>9.2f}  {t['side']:>4}"
                  f"  disp={t['displacement']:>5.1f}pt  retests={t['retest_count']}")

        print()
        print("── INTERPRETATION ───────────────────────────────────────────")
        armed_or_beyond = sum(stats['state_total'].get(s, 0) for s in
                              (STATE_TRAP_ARMED, STATE_WAITING_RETEST,
                               STATE_RETEST_ACTIVE, STATE_TRIGGERED,
                               STATE_INVALIDATED, STATE_EXPIRED))
        print(f"  Zones that progressed past breakout:  {armed_or_beyond}  "
              f"({100*armed_or_beyond/total:.1f}%)")
        print(f"  Trigger rate (per zone):              {pct_trigger:.2f}%")
        print(f"  Selectivity (per day at least 1):     {100*len(stats['triggered_days'])/max(1, stats['profile_days']):.1f}%")
        print()
        if pct_trigger < 3.0:
            print("  ↑ TOO STRICT? Trigger rate under 3% suggests the detection")
            print("    may be over-restrictive. Loosen displacement or tolerance.")
        elif pct_trigger > 15.0:
            print("  ↑ TOO PERMISSIVE? Trigger rate over 15% suggests noise.")
            print("    Tighten min_displacement_pts or retest_tolerance_pts.")
        else:
            print("  ↑ Selectivity in target band (3-15% trigger rate per zone).")
            print("    Phase 3 scoring will further filter these to high-quality signals.")


if __name__ == "__main__":
    main()
