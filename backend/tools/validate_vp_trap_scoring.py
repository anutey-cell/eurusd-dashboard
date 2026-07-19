"""
Validate Phase 3 scoring on the historic TRIGGERED zones from Phase 2.

For each day in the historic dataset, compute the profile of THAT day,
walk NEXT day's M15 bars, find any zones that reach TRIGGERED state, then
run the full scoring pass and report the distribution of composite scores.

Expected outputs:
  - Distribution of scores 0-100
  - Fraction reaching each band (WATCH/DEVELOPING/VALID/EXCEPTIONAL)
  - Estimated live-signal frequency at threshold 80
  - Countertrend vs aligned split
"""
from __future__ import annotations

from collections import Counter
from datetime import datetime, timedelta, timezone
from statistics import mean, median
from types import SimpleNamespace

from sqlalchemy import asc

from services.vp_trap_strategy import compute_prev_day_profile
from services.vp_trap_state import (
    zones_from_profile, scan_zone, STATE_TRIGGERED,
)
from services.vp_trap_scoring import score_zone, MarketContext, BAND_VALID, BAND_EXCEPTIONAL


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


def _atr(highs, lows, closes, n=14):
    if len(closes) < n + 1:
        return 0.0
    trs = []
    for i in range(1, len(closes)):
        tr = max(highs[i] - lows[i],
                 abs(highs[i] - closes[i - 1]),
                 abs(lows[i]  - closes[i - 1]))
        trs.append(tr)
    return round(sum(trs[-n:]) / n, 2)


def _htf_bias(closes, lookback=20):
    if len(closes) < lookback:
        return ""
    recent = closes[-lookback:]
    slope = recent[-1] - recent[0]
    return "Bullish" if slope > 0 else "Bearish" if slope < 0 else "Neutral"


def main():
    from database import SessionLocal
    from db_models import HistoricalCandle

    print("=" * 78)
    print(" VP TRAP PHASE 3 — SCORING VALIDATION ON HISTORIC TRIGGERS")
    print("=" * 78)

    with SessionLocal() as db:
        first = db.query(HistoricalCandle.candle_time).filter(
            HistoricalCandle.timeframe == "H1").order_by(asc(HistoricalCandle.candle_time)).first()
        last  = db.query(HistoricalCandle.candle_time).filter(
            HistoricalCandle.timeframe == "H1").order_by(HistoricalCandle.candle_time.desc()).first()
        earliest = first[0] if first[0].tzinfo else first[0].replace(tzinfo=timezone.utc)
        latest   = last[0]  if last[0].tzinfo  else last[0].replace(tzinfo=timezone.utc)
        print(f"Dataset: {earliest.date()} → {latest.date()}")

        cur = (earliest + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
        end = (latest - timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)

        all_scores: list[dict] = []
        countertrend_scores: list[int] = []
        aligned_scores: list[int] = []
        band_counts: Counter = Counter()
        would_fire_count = 0

        while cur <= end:
            if cur.weekday() >= 5:
                cur += timedelta(days=1)
                continue

            h1_prev = _load_bars(db, "H1",  cur - timedelta(days=1), cur + timedelta(hours=1))
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

            next_day_start = cur + timedelta(days=1)
            next_day_end   = cur + timedelta(days=2)
            m15_next = _load_bars(db, "M15", next_day_start, next_day_end)
            h1_next  = _load_bars(db, "H1",  next_day_start - timedelta(hours=1),
                                              next_day_end + timedelta(hours=1))
            d1_next  = _load_bars(db, "D1",  cur - timedelta(days=20), next_day_start)
            h4_next  = _load_bars(db, "H4",  cur - timedelta(days=10), next_day_start)
            if not m15_next:
                cur += timedelta(days=1)
                continue

            zones = zones_from_profile(profile, expiry_hours=48)
            walk_now = next_day_end

            for z in zones:
                scan_zone(z, m15_next,
                          min_displacement_pts=5.0, retest_tolerance_pts=3.0,
                          max_retests=3, now_utc=walk_now)
                if z.state != STATE_TRIGGERED:
                    continue

                # Build market context AT THE TRIGGER MOMENT
                # The state machine sets last_touched_at when retest happens.
                # Find the bar AT that time and use IT as the "current" price
                # so entry / SL / TP get computed as they would be at the
                # rejection candle (the actual trigger point).
                trigger_time = z.last_touched_at or (z.reclaim_time or walk_now)
                trigger_idx = 0
                for i, b in enumerate(m15_next):
                    bt = b.time if b.time.tzinfo else b.time.replace(tzinfo=timezone.utc)
                    if bt.astimezone(timezone.utc) >= trigger_time:
                        trigger_idx = i
                        break
                # Look one bar AFTER the retest for the rejection close
                rejection_idx = min(len(m15_next) - 1, trigger_idx + 1)
                current_price = m15_next[rejection_idx].close
                # Session = the session at the ACTUAL trigger moment
                now_at_trigger = m15_next[rejection_idx].time
                if now_at_trigger.tzinfo is None:
                    now_at_trigger = now_at_trigger.replace(tzinfo=timezone.utc)

                # Bars available at trigger time (not future ones)
                m15_at_trigger = m15_next[:rejection_idx + 1]

                # HTF context up to trigger moment
                d1_closes_at = [b.close for b in d1_next
                                if (b.time if b.time.tzinfo else b.time.replace(tzinfo=timezone.utc))
                                   .astimezone(timezone.utc) <= now_at_trigger]
                h4_closes_at = [b.close for b in h4_next
                                if (b.time if b.time.tzinfo else b.time.replace(tzinfo=timezone.utc))
                                   .astimezone(timezone.utc) <= now_at_trigger]
                h1_bars_at = [b for b in h1_next
                              if (b.time if b.time.tzinfo else b.time.replace(tzinfo=timezone.utc))
                                 .astimezone(timezone.utc) <= now_at_trigger]
                h1_closes = [b.close for b in h1_bars_at]
                h1_highs  = [b.high  for b in h1_bars_at]
                h1_lows   = [b.low   for b in h1_bars_at]

                ctx = MarketContext(
                    now_utc=now_at_trigger,
                    current_price=current_price,
                    atr_h1=_atr(h1_highs, h1_lows, h1_closes) or 15.0,
                    h1_bars=h1_bars_at,
                    m15_bars=m15_at_trigger,
                    d1_bias=_htf_bias(d1_closes_at, lookback=20),
                    h4_bias=_htf_bias(h4_closes_at, lookback=50),
                    liquidity_map=None,     # not available historically
                    news_clear=True,        # assume clear (historical proxy)
                    volume_source=profile.volume_source,
                )
                breakdown, plan = score_zone(z, profile, ctx)

                all_scores.append({
                    "date": profile.profile_date,
                    "level": z.level_type,
                    "side": z.level_side,
                    "score": breakdown.total,
                    "band": breakdown.band,
                    "is_countertrend": breakdown.is_countertrend,
                    "would_fire": breakdown.would_fire,
                    "factors": breakdown.factors,
                    "rr": plan.get("rr", 0),
                })
                band_counts[breakdown.band] += 1
                if breakdown.would_fire:
                    would_fire_count += 1
                if breakdown.is_countertrend:
                    countertrend_scores.append(breakdown.total)
                else:
                    aligned_scores.append(breakdown.total)

            cur += timedelta(days=1)

        # ── REPORT ──────────────────────────────────────────────────────
        print()
        print(f"── SCORE DISTRIBUTION (n={len(all_scores)} triggered zones) ─")
        if not all_scores:
            print("  No TRIGGERED zones found. Cannot analyse.")
            return

        scores = sorted([s['score'] for s in all_scores])
        print(f"  min:    {scores[0]}")
        print(f"  p25:    {scores[len(scores)//4]}")
        print(f"  median: {scores[len(scores)//2]}")
        print(f"  mean:   {mean(scores):.0f}")
        print(f"  p75:    {scores[3*len(scores)//4]}")
        print(f"  p90:    {scores[9*len(scores)//10]}")
        print(f"  max:    {scores[-1]}")

        print()
        print("── BAND DISTRIBUTION ────────────────────────────────────────")
        for band in ("EXCEPTIONAL", "VALID", "DEVELOPING", "WATCH", "NO_SIGNAL"):
            n = band_counts.get(band, 0)
            pct = 100.0 * n / len(all_scores)
            bar = "█" * min(30, int(pct * 30 / 100))
            print(f"  {band:<12}  {n:>3}  ({pct:>5.1f}%)  {bar}")

        print()
        print(f"── WOULD-FIRE (score >= threshold, no hard gates) ──────────")
        print(f"  n = {would_fire_count} of {len(all_scores)} triggered zones ({100*would_fire_count/len(all_scores):.1f}%)")

        print()
        print("── COUNTERTREND vs ALIGNED ─────────────────────────────────")
        if aligned_scores:
            print(f"  Aligned (n={len(aligned_scores)}):"
                  f"  median score {median(aligned_scores):.0f},"
                  f" mean {mean(aligned_scores):.0f}")
        if countertrend_scores:
            print(f"  Countertrend (n={len(countertrend_scores)}):"
                  f"  median score {median(countertrend_scores):.0f},"
                  f" mean {mean(countertrend_scores):.0f}")

        print()
        print("── FACTOR DIAGNOSTICS ──────────────────────────────────────")
        factor_names = list(all_scores[0]["factors"].keys())
        for fname in factor_names:
            vals = [s["factors"][fname] for s in all_scores]
            print(f"  {fname:<20}  median {median(vals):.1f}  mean {mean(vals):.1f}")

        print()
        print("── SAMPLE HIGH-SCORE SETUPS ────────────────────────────────")
        top = sorted(all_scores, key=lambda s: -s["score"])[:10]
        for t in top:
            ct = " CT" if t["is_countertrend"] else "   "
            wf = " FIRE" if t["would_fire"] else "     "
            print(f"  {t['date']}  {t['level']:>3} {t['side']:>4} {ct}  "
                  f"score={t['score']:>3}  band={t['band']:<12}{wf}  RR={t['rr']:.2f}")


if __name__ == "__main__":
    main()
