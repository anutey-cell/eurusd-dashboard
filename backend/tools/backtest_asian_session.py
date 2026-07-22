"""
Asian Session Opportunity Assessment
=====================================

Tests 4 distinct Asian-session (00:00-06:00 UTC) strategy variants on the
full historical dataset to determine which — if any — deserve activation.

Variants:
  ARB   — Asian Range Breakout. Range = first 4h. Trade breakout in
          direction of break with target = 1× range width, SL = other
          side of the range.
  ARF   — Asian Range Fade. During second half of session, if price
          hits range extreme with rejection wick, fade to range mid.
          SL beyond extreme, TP at range midpoint.
  HTF   — HTF-aligned Asian pullback. Only trade WITH the D1 trend.
          For bullish D1: enter LONG on any Asian dip to prev-day low
          area with bounce candle. Mirror for bearish D1.
  PDL   — Prev-day extreme rejection. During Asian, if price hits PDH
          or PDL and prints a rejection candle, fade back inside range.

For each hypothetical trade:
  - SL sized from ATR (H1) with 1.5× multiplier, capped at 30pt / 80pt
  - TP1 / TP2 per variant rules
  - Walk forward 12 M15 bars (3h) OR 8 H1 bars (8h) — whichever first
    to determine outcome: WIN_TP1 | WIN_TP2 | LOSS | TIMEOUT | BE

Report: per-variant win rate, expectancy R, avg RR, sample size, best
day-of-week, best hour-of-entry.

Run:
  ssh doxau 'docker exec -e PYTHONPATH=/app xauusd-backend python /app/tools/backtest_asian_session.py'
"""
from __future__ import annotations

import sys
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from statistics import mean, median
from types import SimpleNamespace
from typing import Optional

from sqlalchemy import asc


# ── Constants ────────────────────────────────────────────────────────────────

ASIAN_START_HOUR = 0    # UTC
ASIAN_END_HOUR   = 6    # exclusive
RANGE_FORMED_BY  = 4    # first 4h define the Asian range
FORWARD_H1_BARS  = 6    # walk 6 H1 bars = through London KZ (was 8, too generous)
SL_ATR_MULT      = 1.5
SL_MIN_PTS       = 15.0
SL_MAX_PTS       = 60.0
TP_R_MULT        = 2.0  # ALL variants target 2R — realistic scalp target


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


def _atr_h1(bars: list, n: int = 14) -> float:
    if len(bars) < n + 1:
        return 20.0
    trs = []
    for i in range(1, len(bars)):
        tr = max(bars[i].high - bars[i].low,
                 abs(bars[i].high - bars[i - 1].close),
                 abs(bars[i].low  - bars[i - 1].close))
        trs.append(tr)
    return round(sum(trs[-n:]) / n, 2)


def _classify_d1_trend(d1_bars: list, lookback: int = 20) -> str:
    """Simple slope-based trend classifier."""
    if len(d1_bars) < lookback:
        return "neutral"
    closes = [b.close for b in d1_bars[-lookback:]]
    slope = (closes[-1] - closes[0]) / closes[0]
    if slope > 0.01:  return "bullish"
    if slope < -0.01: return "bearish"
    return "neutral"


def _asian_bars_of(m15: list, date_utc) -> list:
    """M15 bars within the Asian window of the given UTC calendar date."""
    day_start = datetime(date_utc.year, date_utc.month, date_utc.day,
                         ASIAN_START_HOUR, tzinfo=timezone.utc)
    day_end   = datetime(date_utc.year, date_utc.month, date_utc.day,
                         ASIAN_END_HOUR, tzinfo=timezone.utc)
    out = []
    for c in m15:
        t = c.time if c.time.tzinfo else c.time.replace(tzinfo=timezone.utc)
        if day_start <= t < day_end:
            out.append(c)
    return out


# ── Outcome simulation ──────────────────────────────────────────────────────

def _simulate_trade(direction: str, entry: float, sl: float, tp: float,
                    forward_bars: list) -> tuple[str, float, int]:
    """Walk forward through H1 bars. Returns (outcome, R_realized, bars_held).

    Sanity checks first — an invalid setup (SL/TP on wrong side of entry)
    is REJECTED, not fake-won. This avoids the "TP behind entry = instant
    WIN" bug that inflates variant WRs when the setup logic accidentally
    inverts the target relative to entry.
    """
    if not forward_bars or not entry or not sl or not tp:
        return ("INVALID", 0.0, 0)
    risk = abs(entry - sl)
    if risk <= 0:
        return ("INVALID", 0.0, 0)
    # Direction validation: TP must be on the correct side of entry.
    if direction == "BUY" and (tp <= entry or sl >= entry):
        return ("INVALID", 0.0, 0)
    if direction == "SELL" and (tp >= entry or sl <= entry):
        return ("INVALID", 0.0, 0)
    for i, b in enumerate(forward_bars):
        if direction == "BUY":
            if b.low <= sl:
                return ("LOSS", -1.0, i)
            if b.high >= tp:
                return ("WIN", (tp - entry) / risk, i)
        else:
            if b.high >= sl:
                return ("LOSS", -1.0, i)
            if b.low <= tp:
                return ("WIN", (entry - tp) / risk, i)
    return ("TIMEOUT", 0.0, len(forward_bars))


# ── Variant detectors ───────────────────────────────────────────────────────

def _detect_arb(asian_m15: list, atr: float, min_body_pts: float = 3.0) -> Optional[dict]:
    """
    Asian Range Breakout — after first 4h defines range, look for break
    in remaining 2h. Enter on breakout close, TP = range width from entry.
    """
    if len(asian_m15) < 20:  # ~5h of M15 bars
        return None
    range_bars = [c for c in asian_m15
                  if (c.time if c.time.tzinfo else c.time.replace(tzinfo=timezone.utc))
                     .astimezone(timezone.utc).hour < RANGE_FORMED_BY]
    late_bars  = [c for c in asian_m15
                  if (c.time if c.time.tzinfo else c.time.replace(tzinfo=timezone.utc))
                     .astimezone(timezone.utc).hour >= RANGE_FORMED_BY]
    if len(range_bars) < 12 or len(late_bars) < 2:
        return None
    r_hi = max(b.high for b in range_bars)
    r_lo = min(b.low  for b in range_bars)
    r_width = r_hi - r_lo
    # Require a meaningful range (~0.5×ATR to 2×ATR)
    if r_width < atr * 0.4 or r_width > atr * 3.0:
        return None

    # Find first late bar that CLOSED beyond range with a real body
    for c in late_bars:
        body = abs(c.close - c.open)
        if body < min_body_pts:
            continue
        if c.close > r_hi:
            entry = c.close
            sl    = round(r_lo - 0.5, 2)
            tp    = round(entry + r_width, 2)
            return {"direction": "BUY", "entry": entry, "sl": sl, "tp": tp,
                    "trigger_time": c.time, "range_width": round(r_width, 2)}
        if c.close < r_lo:
            entry = c.close
            sl    = round(r_hi + 0.5, 2)
            tp    = round(entry - r_width, 2)
            return {"direction": "SELL", "entry": entry, "sl": sl, "tp": tp,
                    "trigger_time": c.time, "range_width": round(r_width, 2)}
    return None


def _tp_from_rr(direction: str, entry: float, sl: float, rr: float = TP_R_MULT) -> float:
    """Compute a TP that's exactly `rr`× the risk from entry, in the trade direction."""
    risk = abs(entry - sl)
    if direction == "BUY":
        return round(entry + rr * risk, 2)
    return round(entry - rr * risk, 2)


def _detect_arf(asian_m15: list, atr: float,
                wick_min_atr: float = 0.15) -> Optional[dict]:
    """
    Asian Range Fade — during second half of session, if a bar's WICK
    touches the range extreme and the body closes back inside, fade
    with 2R target. SL beyond wick + buffer.
    """
    if len(asian_m15) < 16:
        return None
    range_bars = [c for c in asian_m15
                  if (c.time if c.time.tzinfo else c.time.replace(tzinfo=timezone.utc))
                     .astimezone(timezone.utc).hour < RANGE_FORMED_BY]
    late_bars  = [c for c in asian_m15
                  if (c.time if c.time.tzinfo else c.time.replace(tzinfo=timezone.utc))
                     .astimezone(timezone.utc).hour >= RANGE_FORMED_BY]
    if len(range_bars) < 12 or len(late_bars) < 2:
        return None
    r_hi = max(b.high for b in range_bars)
    r_lo = min(b.low  for b in range_bars)
    r_width = r_hi - r_lo
    if r_width < atr * 0.4:
        return None

    min_wick = atr * wick_min_atr
    for c in late_bars:
        if c.high > r_hi and c.close < r_hi:
            wick_up = c.high - max(c.open, c.close)
            if wick_up >= min_wick:
                entry = c.close
                sl = round(c.high + 0.5, 2)
                tp = _tp_from_rr("SELL", entry, sl)
                return {"direction": "SELL", "entry": entry, "sl": sl, "tp": tp,
                        "trigger_time": c.time, "range_width": round(r_width, 2)}
        if c.low < r_lo and c.close > r_lo:
            wick_dn = min(c.open, c.close) - c.low
            if wick_dn >= min_wick:
                entry = c.close
                sl = round(c.low - 0.5, 2)
                tp = _tp_from_rr("BUY", entry, sl)
                return {"direction": "BUY", "entry": entry, "sl": sl, "tp": tp,
                        "trigger_time": c.time, "range_width": round(r_width, 2)}
    return None


def _detect_htf(asian_m15: list, atr: float, d1_trend: str,
                prev_day_low: float, prev_day_high: float) -> Optional[dict]:
    """
    HTF-aligned Asian pullback — only trade WITH D1 trend. 2R target.
    """
    if d1_trend == "neutral" or not asian_m15:
        return None
    tolerance = atr * 0.3

    for c in asian_m15:
        if d1_trend == "bullish":
            if c.low <= prev_day_low + tolerance and c.close > c.open:
                entry = c.close
                sl = round(c.low - atr * 0.3, 2)
                tp = _tp_from_rr("BUY", entry, sl)
                return {"direction": "BUY", "entry": entry, "sl": sl, "tp": tp,
                        "trigger_time": c.time, "range_width": 0}
        if d1_trend == "bearish":
            if c.high >= prev_day_high - tolerance and c.close < c.open:
                entry = c.close
                sl = round(c.high + atr * 0.3, 2)
                tp = _tp_from_rr("SELL", entry, sl)
                return {"direction": "SELL", "entry": entry, "sl": sl, "tp": tp,
                        "trigger_time": c.time, "range_width": 0}
    return None


def _detect_pdl(asian_m15: list, atr: float,
                prev_day_low: float, prev_day_high: float) -> Optional[dict]:
    """
    Prev-day extreme rejection during Asian. 2R target (fixed, not pd_mid).
    """
    if not asian_m15 or prev_day_high <= prev_day_low:
        return None
    pd_range = prev_day_high - prev_day_low
    tolerance = atr * 0.2

    for c in asian_m15:
        # SELL from PDH rejection
        if c.high >= prev_day_high - tolerance and c.close < prev_day_high:
            wick = c.high - max(c.open, c.close)
            if wick >= atr * 0.2:
                entry = c.close
                sl = round(c.high + 1.0, 2)
                tp = _tp_from_rr("SELL", entry, sl)
                return {"direction": "SELL", "entry": entry, "sl": sl, "tp": tp,
                        "trigger_time": c.time, "range_width": round(pd_range, 2)}
        # BUY from PDL rejection
        if c.low <= prev_day_low + tolerance and c.close > prev_day_low:
            wick = min(c.open, c.close) - c.low
            if wick >= atr * 0.2:
                entry = c.close
                sl = round(c.low - 1.0, 2)
                tp = _tp_from_rr("BUY", entry, sl)
                return {"direction": "BUY", "entry": entry, "sl": sl, "tp": tp,
                        "trigger_time": c.time, "range_width": round(pd_range, 2)}
    return None


# ── Main backtest ───────────────────────────────────────────────────────────

def main():
    from database import SessionLocal
    from db_models import HistoricalCandle

    print("=" * 78)
    print(" ASIAN SESSION OPPORTUNITY ASSESSMENT")
    print("=" * 78)

    with SessionLocal() as db:
        first = db.query(HistoricalCandle.candle_time).filter(
            HistoricalCandle.timeframe == "H1").order_by(asc(HistoricalCandle.candle_time)).first()
        last  = db.query(HistoricalCandle.candle_time).filter(
            HistoricalCandle.timeframe == "H1").order_by(HistoricalCandle.candle_time.desc()).first()
        if not first or not last:
            print("No H1 data. Abort.")
            return
        earliest = first[0] if first[0].tzinfo else first[0].replace(tzinfo=timezone.utc)
        latest   = last[0]  if last[0].tzinfo  else last[0].replace(tzinfo=timezone.utc)
        print(f"Dataset: {earliest.date()} → {latest.date()}")
        print(f"Asian window: {ASIAN_START_HOUR:02d}:00 - {ASIAN_END_HOUR:02d}:00 UTC")
        print(f"Range formed by hour: {RANGE_FORMED_BY:02d}:00 UTC")
        print()

        # Variant → list of trade records
        results: dict[str, list] = {
            "ARB (breakout)":   [],
            "ARF (fade to mid)": [],
            "HTF (aligned)":     [],
            "PDL (prev-day fade)": [],
        }

        cur = (earliest + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
        end = (latest - timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)

        days_walked = 0
        while cur <= end:
            if cur.weekday() >= 5:   # skip Sat/Sun
                cur += timedelta(days=1)
                continue

            # Bars we need: today's Asian window + forward window through London KZ
            # forward walk uses M15 bars starting AT trigger time (not 06:00 UTC)
            # to avoid the "unmonitored gap" bias where SL hits get hidden between
            # trigger and start of forward window
            m15_today = _load_bars(db, "M15",
                                    cur, cur + timedelta(hours=14))
            h1_recent = _load_bars(db, "H1",
                                    cur - timedelta(hours=30), cur)
            d1_recent = _load_bars(db, "D1",
                                    cur - timedelta(days=30), cur)

            asian_bars = _asian_bars_of(m15_today, cur.date())
            if not asian_bars or not m15_today:
                cur += timedelta(days=1)
                continue

            days_walked += 1
            atr = _atr_h1(h1_recent) if h1_recent else 20.0
            d1_trend = _classify_d1_trend(d1_recent)

            # Prev-day extremes
            prev_bar = d1_recent[-1] if d1_recent else None
            pdh = prev_bar.high if prev_bar else 0.0
            pdl = prev_bar.low  if prev_bar else 0.0

            # Try each variant
            arb = _detect_arb(asian_bars, atr)
            arf = _detect_arf(asian_bars, atr)
            htf = _detect_htf(asian_bars, atr, d1_trend, pdl, pdh)
            pdl_trade = _detect_pdl(asian_bars, atr, pdl, pdh)

            def _forward_from_trigger(trigger_time, bars_needed=24):
                """M15 bars strictly AFTER the trigger bar's time — covers next 6h."""
                tt = trigger_time if trigger_time.tzinfo else trigger_time.replace(tzinfo=timezone.utc)
                tt = tt.astimezone(timezone.utc)
                out = []
                for c in m15_today:
                    bt = c.time if c.time.tzinfo else c.time.replace(tzinfo=timezone.utc)
                    if bt.astimezone(timezone.utc) > tt:
                        out.append(c)
                        if len(out) >= bars_needed:
                            break
                return out

            for name, trade in [
                ("ARB (breakout)",         arb),
                ("ARF (fade to mid)",      arf),
                ("HTF (aligned)",          htf),
                ("PDL (prev-day fade)",    pdl_trade),
            ]:
                if not trade:
                    continue
                # Walk M15 bars starting AFTER the trigger bar
                fwd = _forward_from_trigger(trade["trigger_time"], bars_needed=24)
                if not fwd:
                    continue
                outcome, r_realized, bars_held = _simulate_trade(
                    trade["direction"], trade["entry"], trade["sl"], trade["tp"],
                    fwd,
                )
                # Skip invalid setups — they aren't real trades
                if outcome == "INVALID":
                    continue
                trigger_hour = (trade["trigger_time"] if trade["trigger_time"].tzinfo
                                else trade["trigger_time"].replace(tzinfo=timezone.utc)
                                ).astimezone(timezone.utc).hour
                trade_rec = {
                    "date": cur.date().isoformat(),
                    "weekday": cur.strftime("%a"),
                    "trigger_hour": trigger_hour,
                    "direction": trade["direction"],
                    "entry": trade["entry"], "sl": trade["sl"], "tp": trade["tp"],
                    "outcome": outcome, "r": r_realized, "bars_held": bars_held,
                    "atr": atr, "d1_trend": d1_trend,
                }
                results[name].append(trade_rec)

            cur += timedelta(days=1)

        # ── REPORT ──────────────────────────────────────────────────────
        print(f"Days walked: {days_walked}")
        print()

        for variant_name, trades in results.items():
            print("=" * 78)
            print(f" {variant_name}")
            print("=" * 78)
            n = len(trades)
            if n == 0:
                print("  No qualifying setups found in dataset.")
                print()
                continue
            wins   = [t for t in trades if t["outcome"] == "WIN"]
            losses = [t for t in trades if t["outcome"] == "LOSS"]
            timeouts = [t for t in trades if t["outcome"] == "TIMEOUT"]
            wr = 100.0 * len(wins) / n
            expectancy = mean([t["r"] for t in trades])
            avg_win  = mean([t["r"] for t in wins])  if wins  else 0
            avg_loss = mean([t["r"] for t in losses]) if losses else 0
            profit_factor = (
                sum(t["r"] for t in wins) / abs(sum(t["r"] for t in losses))
                if losses and sum(t["r"] for t in losses) != 0 else float("inf")
            )

            print(f"  Sample: {n} trades over {days_walked} days ({n/days_walked*100:.1f}% frequency)")
            print(f"  Win rate:      {wr:.1f}%  ({len(wins)}W · {len(losses)}L · {len(timeouts)}T)")
            print(f"  Expectancy:    {expectancy:+.3f}R per trade")
            print(f"  Avg win:       {avg_win:+.2f}R")
            print(f"  Avg loss:      {avg_loss:+.2f}R")
            print(f"  Profit factor: {profit_factor:.2f}")

            # By direction
            for dir_ in ("BUY", "SELL"):
                sub = [t for t in trades if t["direction"] == dir_]
                if not sub:
                    continue
                sub_wr = 100.0 * sum(1 for t in sub if t["outcome"] == "WIN") / len(sub)
                sub_exp = mean([t["r"] for t in sub])
                print(f"    {dir_:>4}:  n={len(sub):>3}  WR={sub_wr:>5.1f}%  exp={sub_exp:+.3f}R")

            # By day of week
            print("  By day of week:")
            by_dow = defaultdict(list)
            for t in trades:
                by_dow[t["weekday"]].append(t)
            for dow in ("Mon", "Tue", "Wed", "Thu", "Fri"):
                sub = by_dow.get(dow, [])
                if not sub:
                    continue
                sub_wr = 100.0 * sum(1 for t in sub if t["outcome"] == "WIN") / len(sub)
                sub_exp = mean([t["r"] for t in sub])
                print(f"    {dow}:  n={len(sub):>3}  WR={sub_wr:>5.1f}%  exp={sub_exp:+.3f}R")

            # By D1 trend context (for variants where it varies)
            print("  By D1 trend context:")
            by_trend = defaultdict(list)
            for t in trades:
                by_trend[t["d1_trend"]].append(t)
            for tr in ("bullish", "neutral", "bearish"):
                sub = by_trend.get(tr, [])
                if not sub:
                    continue
                sub_wr = 100.0 * sum(1 for t in sub if t["outcome"] == "WIN") / len(sub)
                sub_exp = mean([t["r"] for t in sub])
                print(f"    D1={tr:<8}  n={len(sub):>3}  WR={sub_wr:>5.1f}%  exp={sub_exp:+.3f}R")

            print()

        # ── OVERALL VERDICT ─────────────────────────────────────────────
        print("=" * 78)
        print(" ASSESSMENT")
        print("=" * 78)
        print()
        print("  Variant           | Trades | WR%   | ExpR   | Recommendation")
        print("  " + "-" * 68)
        for name, trades in results.items():
            n = len(trades)
            if n == 0:
                print(f"  {name:<17} |   0    |  --   |  --    | No data — dropped")
                continue
            wins = sum(1 for t in trades if t["outcome"] == "WIN")
            wr = 100.0 * wins / n
            exp_r = mean([t["r"] for t in trades])
            if exp_r > 0.15 and n >= 15:
                rec = "★ ACTIVATE — clear edge"
            elif exp_r > 0.05 and n >= 15:
                rec = "· Watch — marginal edge"
            elif exp_r < -0.10:
                rec = "✗ Blacklist confirmed"
            else:
                rec = "  Neutral — skip"
            print(f"  {name:<17} | {n:>4}   | {wr:>4.1f}% | {exp_r:+.3f}R | {rec}")
        print()
        print("  Currently 'Asian range formation' is in _NEVER_TRADE_SESSIONS.")
        print("  Any variant with expectancy > +0.10R and >=20 trades merits")
        print("  removal of the blanket blacklist FOR THAT SPECIFIC VARIANT.")


if __name__ == "__main__":
    main()
