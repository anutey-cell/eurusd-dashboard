"""
Advanced ICT Concepts Layer
===========================

The next layer of ICT/SMC integration on top of the existing engine. Each
function detects one ICT concept; `compute_ict_alignment()` combines them
into a single 0-100 score that gates auto-executor firings.

CONCEPTS IMPLEMENTED:

  1. Power of 3 (PO3) — daily Accumulation → Manipulation → Distribution cycle
     - Accumulation: low-volatility range builds (typically Asian session)
     - Manipulation: false move sweeps liquidity (typically London open Judas)
     - Distribution: real directional move (typically NY session)
     - Used to select WHEN to trade — Distribution phase strongly preferred.

  2. Daily Open as Primary Bias — ICT's "the daily open is the most important
     level of the day." Price above daily open = bullish bias; below = bearish.
     We use this as a DIRECTIONAL FILTER: longs only above DO, shorts only below.

  3. Premium / Discount Zones — the dealing range's 50% equilibrium splits it
     into Premium (upper 50%) and Discount (lower 50%). ICT entry rule:
       BUYs only valid in DISCOUNT (lower half)
       SELLs only valid in PREMIUM (upper half)
     Entries at equilibrium ±10% are penalised (low edge in the middle).

  4. Judas Swing Detection — the first 60-90 minutes after London open
     (07:00-08:30 UTC) and NY open (13:00-14:30 UTC) typically contain a
     manipulation leg that sweeps Asian range or pre-market highs/lows.
     A trade taken DURING the Judas window is low quality (you're being
     trapped); a trade taken AFTER the Judas has completed (reversal +
     return to range) is high quality.

COMBINED SCORE:

  100 = perfect ICT confluence (Distribution phase + DO-aligned bias +
        entry in correct zone + post-Judas reversal)
   60 = minimum acceptable for auto-execution
    0 = anti-ICT setup (Accumulation phase + counter-bias + wrong zone +
        mid-Judas chase)

Each component contributes up to 25 points.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from typing import Optional

log = logging.getLogger(__name__)


# ────────────────────────────────────────────────────────────────────────────
# 1. POWER OF 3 (PO3) — Daily Accumulation / Manipulation / Distribution
# ────────────────────────────────────────────────────────────────────────────

@dataclass
class PO3Phase:
    phase:       str           # "ACCUMULATION" | "MANIPULATION" | "DISTRIBUTION" | "UNKNOWN"
    score:       int           # 0-25 (Distribution=25, Manipulation=10, Accumulation=0)
    range_pts:   float
    body_pct:    float         # range-relative body of today so far
    reason:      str


def compute_po3_phase(candles_m15, at: datetime) -> PO3Phase:
    """
    Determine the PO3 phase for the current trading day.

    Heuristic (XAU/USD M15):
      • If today's range so far < 0.4 × 20-day average range → ACCUMULATION
        (still building the range, not ready to expand)
      • If today's range >= 0.4× AND large directional displacement (body/range
        on the dominant direction > 60%) has happened → DISTRIBUTION
        (real move underway)
      • Else → MANIPULATION (range expanding but not yet directional)
    """
    if not candles_m15 or len(candles_m15) < 100:
        return PO3Phase("UNKNOWN", 0, 0.0, 0.0, "Need >= 100 M15 bars for PO3")

    now = at if at.tzinfo else at.replace(tzinfo=timezone.utc)
    today_utc = now.replace(hour=0, minute=0, second=0, microsecond=0)

    # Group bars by UTC date
    bars_today = []
    bars_history: list[list] = []
    current_day: list = []
    current_date = None
    for c in candles_m15:
        ct = c.time if c.time.tzinfo else c.time.replace(tzinfo=timezone.utc)
        d = ct.astimezone(timezone.utc).date()
        if current_date is None:
            current_date = d
        if d != current_date:
            bars_history.append(current_day)
            current_day = []
            current_date = d
        current_day.append(c)
    if current_day:
        if current_date == now.astimezone(timezone.utc).date():
            bars_today = current_day
        else:
            bars_history.append(current_day)

    if not bars_today:
        return PO3Phase("UNKNOWN", 0, 0.0, 0.0, "No bars for today yet")

    today_high = max(b.high for b in bars_today)
    today_low  = min(b.low  for b in bars_today)
    today_range = today_high - today_low

    today_open = bars_today[0].open
    today_close = bars_today[-1].close
    body_directional = today_close - today_open       # +ve = bullish day so far
    body_pct = abs(body_directional) / today_range if today_range > 0 else 0.0

    # 20-day average daily range as reference
    if len(bars_history) >= 5:
        last_n = bars_history[-20:]
        daily_ranges = [
            max(b.high for b in day) - min(b.low for b in day)
            for day in last_n if day
        ]
        avg_range = sum(daily_ranges) / len(daily_ranges) if daily_ranges else 1.0
    else:
        avg_range = today_range or 1.0

    range_ratio = today_range / avg_range if avg_range > 0 else 0.0

    # Decide phase
    if range_ratio < 0.4:
        phase = "ACCUMULATION"
        score = 0
        reason = (
            f"Today's range {today_range:.1f}pts is only {range_ratio*100:.0f}% "
            f"of 20-day avg ({avg_range:.1f}pts). Range still building."
        )
    elif range_ratio >= 0.4 and body_pct >= 0.6:
        phase = "DISTRIBUTION"
        score = 25
        direction = "bullish" if body_directional > 0 else "bearish"
        reason = (
            f"Range {range_ratio*100:.0f}% of avg, body {body_pct*100:.0f}% "
            f"of range, directional {direction} expansion."
        )
    else:
        phase = "MANIPULATION"
        score = 10
        reason = (
            f"Range expanded ({range_ratio*100:.0f}%) but body indecisive "
            f"({body_pct*100:.0f}%). Likely manipulation leg — wait for "
            f"directional commitment."
        )

    return PO3Phase(
        phase=phase, score=score,
        range_pts=round(today_range, 1),
        body_pct=round(body_pct, 3),
        reason=reason,
    )


# ────────────────────────────────────────────────────────────────────────────
# 2. DAILY OPEN as PRIMARY BIAS
# ────────────────────────────────────────────────────────────────────────────

@dataclass
class DailyOpenBias:
    daily_open:   float
    current:      float
    bias:         str       # "BULLISH" | "BEARISH" | "NEUTRAL"
    distance_pts: float
    aligned:      bool      # vs the proposed signal direction
    score:        int       # 0-25
    reason:       str


def daily_open_bias(candles_m15, at: datetime, signal_direction: Optional[str] = None) -> DailyOpenBias:
    """
    The Daily Open is ICT's most important reference price.
      Price above DO → bullish bias → BUYs preferred
      Price below DO → bearish bias → SELLs preferred
      Within ±0.1% of DO → NEUTRAL (no bias)

    If `signal_direction` is provided, returns aligned=True only if the
    signal matches the bias (or DO is neutral). Score: aligned = 25,
    counter-bias = 0, neutral = 12.
    """
    if not candles_m15:
        return DailyOpenBias(0, 0, "NEUTRAL", 0, False, 12, "No candles")

    now = at if at.tzinfo else at.replace(tzinfo=timezone.utc)
    today = now.astimezone(timezone.utc).date()

    today_bars = [
        c for c in candles_m15
        if (c.time if c.time.tzinfo else c.time.replace(tzinfo=timezone.utc))
            .astimezone(timezone.utc).date() == today
    ]
    if not today_bars:
        return DailyOpenBias(0, 0, "NEUTRAL", 0, False, 12, "No candles today")

    daily_open = today_bars[0].open
    current = today_bars[-1].close
    distance = current - daily_open
    distance_pct = abs(distance) / daily_open * 100 if daily_open > 0 else 0

    if distance_pct < 0.1:
        bias = "NEUTRAL"
    elif distance > 0:
        bias = "BULLISH"
    else:
        bias = "BEARISH"

    aligned = True
    score = 12   # default for neutral
    reason = f"DO {daily_open:.2f}, current {current:.2f}, {distance:+.2f}pts ({distance_pct:.2f}%)"

    if signal_direction in ("BUY", "SELL"):
        if bias == "NEUTRAL":
            aligned = True
            score = 12
            reason += " — NEUTRAL bias, no penalty"
        elif (bias == "BULLISH" and signal_direction == "BUY") or \
             (bias == "BEARISH" and signal_direction == "SELL"):
            aligned = True
            score = 25
            reason += f" — {bias} aligns with {signal_direction}"
        else:
            aligned = False
            score = 0
            reason += f" — {bias} CONTRADICTS {signal_direction} (counter-DO trade)"

    return DailyOpenBias(
        daily_open=round(daily_open, 2),
        current=round(current, 2),
        bias=bias,
        distance_pts=round(distance, 2),
        aligned=aligned,
        score=score,
        reason=reason,
    )


# ────────────────────────────────────────────────────────────────────────────
# 3. PREMIUM / DISCOUNT ZONES
# ────────────────────────────────────────────────────────────────────────────

@dataclass
class PremiumDiscount:
    range_high:   float
    range_low:    float
    equilibrium:  float
    current:      float
    position:     str       # "PREMIUM" | "DISCOUNT" | "EQUILIBRIUM"
    position_pct: float     # 0-100, where 0=low, 50=eq, 100=high
    score:        int       # 0-25
    aligned:      bool
    reason:       str


def premium_discount_zone(
    candles_h4,
    at: datetime,
    signal_direction: Optional[str] = None,
    lookback_bars: int = 60,       # ~10 days of H4 = recent dealing range
) -> PremiumDiscount:
    """
    Compute the recent dealing range from H4 candles and classify
    the current price into Premium, Discount, or Equilibrium.

    ICT rule:
      BUY in DISCOUNT (lower 50%)  → score 25
      SELL in PREMIUM (upper 50%)  → score 25
      BUY  in PREMIUM (counter-edge)→ score 0
      SELL in DISCOUNT             → score 0
      Either in EQUILIBRIUM ±10%   → score 10 (no edge)
    """
    if not candles_h4 or len(candles_h4) < 10:
        return PremiumDiscount(0, 0, 0, 0, "EQUILIBRIUM", 50, 12, True,
                                "Need >= 10 H4 bars")

    recent = candles_h4[-lookback_bars:]
    range_high = max(c.high for c in recent)
    range_low  = min(c.low  for c in recent)
    equilibrium = (range_high + range_low) / 2
    current = recent[-1].close
    range_size = range_high - range_low

    position_pct = (current - range_low) / range_size * 100 if range_size > 0 else 50

    if position_pct >= 55:
        position = "PREMIUM"
    elif position_pct <= 45:
        position = "DISCOUNT"
    else:
        position = "EQUILIBRIUM"

    aligned = True
    score = 12
    reason = (
        f"Range {range_low:.2f}-{range_high:.2f}, equil {equilibrium:.2f}, "
        f"current {current:.2f} = {position_pct:.0f}% ({position})"
    )

    if signal_direction in ("BUY", "SELL"):
        if position == "EQUILIBRIUM":
            aligned = True
            score = 10
            reason += " — no edge in mid-range"
        elif (position == "DISCOUNT" and signal_direction == "BUY") or \
             (position == "PREMIUM" and signal_direction == "SELL"):
            aligned = True
            score = 25
            reason += f" — {signal_direction} in {position} aligns with ICT edge"
        else:
            aligned = False
            score = 0
            reason += f" — {signal_direction} in {position} is COUNTER-EDGE (chasing)"

    return PremiumDiscount(
        range_high=round(range_high, 2),
        range_low=round(range_low, 2),
        equilibrium=round(equilibrium, 2),
        current=round(current, 2),
        position=position,
        position_pct=round(position_pct, 1),
        score=score,
        aligned=aligned,
        reason=reason,
    )


# ────────────────────────────────────────────────────────────────────────────
# 4. JUDAS SWING DETECTION
# ────────────────────────────────────────────────────────────────────────────

@dataclass
class JudasState:
    in_window:        bool    # currently inside a Judas window?
    judas_detected:   bool    # has the manipulation leg printed?
    judas_direction:  str     # "UP" | "DOWN" | "NONE"
    swept_level:      Optional[float]
    reversed:         bool    # did price reverse back through the swept level?
    score:            int     # 0-25
    reason:           str


def detect_judas_swing(candles_m15, at: datetime) -> JudasState:
    """
    The Judas swing is the FIRST manipulation leg after the London (07:00 UTC)
    or NY (13:00 UTC) session opens. It sweeps Asian-range or pre-market
    highs/lows then reverses.

    Detection windows:
      London Judas:  07:00-08:30 UTC
      NY Judas:      13:00-14:30 UTC

    Score logic:
      During Judas window, NO judas yet:           score 5  (too early — wait)
      During Judas window, judas printed, no rev:  score 10 (in the trap)
      Post-Judas, reversal confirmed:               score 25 (real move begins)
      Outside Judas window entirely:                score 18 (acceptable but no Judas confluence)
    """
    if not candles_m15 or len(candles_m15) < 50:
        return JudasState(False, False, "NONE", None, False, 12, "Need >= 50 bars")

    now = at if at.tzinfo else at.replace(tzinfo=timezone.utc)
    now_utc = now.astimezone(timezone.utc)
    h = now_utc.hour + now_utc.minute / 60

    london_window = 7.0  <= h < 8.5
    ny_window     = 13.0 <= h < 14.5
    in_window = london_window or ny_window

    # Pre-window reference range (Asian for London Judas, AM for NY Judas)
    today = now_utc.date()
    today_bars = [
        c for c in candles_m15
        if (c.time if c.time.tzinfo else c.time.replace(tzinfo=timezone.utc))
            .astimezone(timezone.utc).date() == today
    ]
    if not today_bars:
        return JudasState(in_window, False, "NONE", None, False, 12, "No bars today")

    if london_window or h < 13:
        # Reference = Asian range (00:00-06:00 UTC)
        ref_bars = [
            b for b in today_bars
            if 0 <= (b.time if b.time.tzinfo else b.time.replace(tzinfo=timezone.utc))
                .astimezone(timezone.utc).hour < 6
        ]
        ref_label = "Asian"
    else:
        # NY Judas reference = London AM range (07:00-12:00 UTC)
        ref_bars = [
            b for b in today_bars
            if 7 <= (b.time if b.time.tzinfo else b.time.replace(tzinfo=timezone.utc))
                .astimezone(timezone.utc).hour < 12
        ]
        ref_label = "London AM"

    if not ref_bars:
        return JudasState(in_window, False, "NONE", None, False, 18,
                           f"No {ref_label} reference range yet")

    ref_high = max(b.high for b in ref_bars)
    ref_low  = min(b.low  for b in ref_bars)

    # Check if any post-window bars swept and reversed
    window_bars = []
    if london_window or 8.5 <= h < 13:
        window_bars = [
            b for b in today_bars
            if (b.time if b.time.tzinfo else b.time.replace(tzinfo=timezone.utc))
                .astimezone(timezone.utc).hour >= 7
        ]
    elif ny_window or h >= 14.5:
        window_bars = [
            b for b in today_bars
            if (b.time if b.time.tzinfo else b.time.replace(tzinfo=timezone.utc))
                .astimezone(timezone.utc).hour >= 13
        ]

    judas_detected = False
    judas_dir = "NONE"
    swept_level = None
    reversed_ = False

    if window_bars:
        swept_high = any(b.high > ref_high + 0.5 for b in window_bars)
        swept_low  = any(b.low  < ref_low  - 0.5 for b in window_bars)
        if swept_high and not swept_low:
            judas_detected = True
            judas_dir = "UP"
            swept_level = ref_high
        elif swept_low and not swept_high:
            judas_detected = True
            judas_dir = "DOWN"
            swept_level = ref_low

        # Reversal: did current price return BELOW swept_high or ABOVE swept_low?
        if judas_detected:
            current = window_bars[-1].close
            if judas_dir == "UP" and current < ref_high:
                reversed_ = True
            elif judas_dir == "DOWN" and current > ref_low:
                reversed_ = True

    # Score logic
    if in_window and not judas_detected:
        score = 5
        reason = f"In {ref_label} Judas window, manipulation leg not yet printed — too early"
    elif in_window and judas_detected and not reversed_:
        score = 10
        reason = f"Judas {judas_dir} swept {ref_label} {swept_level:.2f} — IN THE TRAP, wait for reversal"
    elif judas_detected and reversed_:
        score = 25
        reason = f"Judas {judas_dir} swept {ref_label} {swept_level:.2f} and REVERSED — real move begins"
    elif not in_window and judas_detected:
        score = 22
        reason = f"Post-Judas: {ref_label} {swept_level:.2f} swept, reversal {'confirmed' if reversed_ else 'pending'}"
    else:
        score = 18
        reason = f"No Judas swing detected on {ref_label} range; trade acceptable but no Judas confluence"

    return JudasState(
        in_window=in_window,
        judas_detected=judas_detected,
        judas_direction=judas_dir,
        swept_level=round(swept_level, 2) if swept_level else None,
        reversed=reversed_,
        score=score,
        reason=reason,
    )


# ────────────────────────────────────────────────────────────────────────────
# COMBINED ICT FRAMEWORK SCORE
# ────────────────────────────────────────────────────────────────────────────

@dataclass
class ICTAlignment:
    score:            int                  # 0-100 (sum of 4 components)
    posture:          str                  # "ALIGNED" | "CAUTIOUS" | "MISALIGNED"
    po3:              PO3Phase
    daily_open:       DailyOpenBias
    premium_discount: PremiumDiscount
    judas:            JudasState
    signal_direction: Optional[str]
    blocking:         list[str] = field(default_factory=list)
    summary:          str = ""


def compute_ict_alignment(
    candles_m15,
    candles_h4,
    at: datetime,
    signal_direction: Optional[str] = None,
) -> ICTAlignment:
    """
    Combine all 4 ICT detectors into a single 0-100 alignment score.

    Posture mapping:
      80+ → ALIGNED      — institutional-grade ICT confluence
      60-79 → CAUTIOUS   — acceptable but not optimal
      <60 → MISALIGNED   — auto-executor refuses this trade
    """
    po3   = compute_po3_phase(candles_m15, at)
    do    = daily_open_bias(candles_m15, at, signal_direction)
    pd    = premium_discount_zone(candles_h4, at, signal_direction)
    judas = detect_judas_swing(candles_m15, at)

    total = po3.score + do.score + pd.score + judas.score

    blocking = []
    if not do.aligned: blocking.append("Counter-Daily-Open direction")
    if not pd.aligned: blocking.append(f"{signal_direction} in {pd.position} zone (counter-edge)")
    if po3.phase == "ACCUMULATION": blocking.append("PO3 still in Accumulation (no directional expansion yet)")
    if judas.in_window and not judas.judas_detected: blocking.append("Inside Judas window with no swing yet (early trap)")

    if total >= 80: posture = "ALIGNED"
    elif total >= 60: posture = "CAUTIOUS"
    else: posture = "MISALIGNED"

    summary = (
        f"ICT alignment {total}/100 — {posture}. "
        f"PO3:{po3.phase}/{po3.score} · DO:{do.bias}/{do.score} · "
        f"P/D:{pd.position}/{pd.score} · Judas:{judas.score}"
    )

    return ICTAlignment(
        score=total, posture=posture,
        po3=po3, daily_open=do,
        premium_discount=pd, judas=judas,
        signal_direction=signal_direction,
        blocking=blocking,
        summary=summary,
    )
