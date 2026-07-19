"""
VP Trap Confluence Scoring — Phase 3
====================================

Weighted 0-100 score per brief spec. Every TRIGGERED zone gets scored;
scores are used later (Phase 4) to gate Telegram alerts.

Score bands (per brief):
  0-59    NO_SIGNAL      internal reject
  60-69   WATCH          dashboard monitoring only
  70-79   DEVELOPING     dashboard monitoring, not actionable
  80-89   VALID          Phase 4: BUY/SELL alert
  90-100  EXCEPTIONAL    Phase 4: BUY/SELL alert with 🌟 marker

Score factors (per brief, weights configurable — sum to 100):
  Trap validity              20   how clean was the failed breakout
  Rejection quality          15   strength of retest rejection candle
  Market-structure shift     15   MSS confirmation on execution TF
  Volume/order-flow          15   participation footprint (with tick_proxy penalty)
  Session quality            10   London/NY overlap preference
  Higher-timeframe alignment 10   HTF trend regime agreement
  Risk-to-reward quality      5   RR >= min gate; higher = more score
  Space-to-target             5   clear path to TP (opposing HVN/POC/VWAP)
  Data quality                5   provider health + candle freshness

Countertrend threshold bump: setups against HTF get +10 to the threshold
(need 90 instead of 80) rather than a score penalty. Preserves granularity
while still being harder to fire.

Design invariants:
  - Pure function of (zone, profile, market_context)
  - No I/O, no DB reads, no network
  - Deterministic — same inputs always give same output
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Any, Optional

log = logging.getLogger(__name__)


# ── Band constants ──────────────────────────────────────────────────────────

BAND_NO_SIGNAL   = "NO_SIGNAL"
BAND_WATCH       = "WATCH"
BAND_DEVELOPING  = "DEVELOPING"
BAND_VALID       = "VALID"
BAND_EXCEPTIONAL = "EXCEPTIONAL"


# ── Default weights (per brief) — must sum to 100 ──────────────────────────

DEFAULT_WEIGHTS = {
    "trap_validity":        20,
    "rejection_quality":    15,
    "market_structure":     15,
    "volume_orderflow":     15,
    "session_quality":      10,
    "htf_alignment":        10,
    "rr_quality":            5,
    "space_to_target":       5,
    "data_quality":          5,
}
assert sum(DEFAULT_WEIGHTS.values()) == 100


# ── Session windows (UTC hours, half-open) ─────────────────────────────────

_SESSION_WINDOWS = [
    ("LONDON_OPEN",       7,  10, 10),   # 07-10 UTC → full session weight
    ("LONDON_NY_OVERLAP", 13, 16, 10),   # 13-16 UTC → best
    ("NY_OPEN",           13, 16, 10),   # (same window; matched by killzone label)
    ("LONDON_LUNCH",      11, 12,  5),   # 11-12 UTC → chop, half score
    ("NY_PM",             16, 22,  3),   # after 16 → weak
    ("ASIAN_PRE",         22, 24,  3),
    ("ASIAN_RANGE",        0,  6,  3),
    ("PRE_LONDON",         6,  7,  5),
]


def _session_score(now_utc: datetime, max_pts: int) -> tuple[int, str]:
    hour = now_utc.hour
    for name, lo, hi, weight in _SESSION_WINDOWS:
        if lo <= hour < hi:
            # scale weight from the window's 0-10 to 0-max_pts
            pts = int(round(weight * max_pts / 10))
            return (pts, name)
    return (0, "OFF_SESSION")


# ── Data classes ────────────────────────────────────────────────────────────

@dataclass
class MarketContext:
    """The extra context each scoring pass needs. Built by the caller and
    passed in — keeps scoring itself pure."""
    now_utc:          datetime
    current_price:    float
    atr_h1:           float
    h1_bars:          list                          # recent H1 bars for MSS check
    m15_bars:         list                          # recent M15 bars for rejection quality
    d1_bias:          str = ""                      # "Bullish" | "Bearish" | "Neutral"
    h4_bias:          str = ""                      # same
    liquidity_map:    Optional[dict] = None         # optional; used for space-to-target
    news_clear:       bool = True                   # optional news gate
    volume_source:    str = "tick_proxy"


@dataclass
class ScoreBreakdown:
    total:               int
    band:                str
    factors:             dict                     # per-factor points earned
    factor_max:          dict                     # per-factor max points
    reason_qualifies:    str                      # short human-readable summary
    conditions_met:      list                     # human-readable list
    conditions_missing:  list                     # human-readable list
    is_countertrend:     bool
    effective_threshold: int                      # 80 or 90 depending on countertrend
    would_fire:          bool                     # total >= effective_threshold

    def to_dict(self) -> dict:
        return {
            "total":              self.total,
            "band":               self.band,
            "factors":            self.factors,
            "factor_max":         self.factor_max,
            "reason_qualifies":   self.reason_qualifies,
            "conditions_met":     self.conditions_met,
            "conditions_missing": self.conditions_missing,
            "is_countertrend":    self.is_countertrend,
            "effective_threshold": self.effective_threshold,
            "would_fire":         self.would_fire,
        }


# ── Factor scorers ──────────────────────────────────────────────────────────

def _score_trap_validity(zone, max_pts: int) -> tuple[int, str]:
    """
    How CLEAN was the failed breakout? Signals a genuine trap vs a wick.

    Scoring:
      +40% displacement contribution     (bigger reversal after reclaim = more pts)
      +40% reclaim quickness              (fast reclaim after breakout = more pts)
      +20% no-acceptance-signal margin    (few bars beyond level before reclaim)
    """
    if not zone.breakout_time or not zone.reclaim_time:
        return (0, "missing breakout/reclaim timestamps")

    disp = zone.displacement_pts or 0.0
    disp_score = min(1.0, disp / 30.0)     # 30pt+ = full displacement pts

    delta_min = (zone.reclaim_time - zone.breakout_time).total_seconds() / 60.0
    # <30 min reclaim = full pts; 4h+ = 0
    if delta_min <= 30:
        speed_score = 1.0
    elif delta_min >= 240:
        speed_score = 0.0
    else:
        speed_score = max(0.0, (240 - delta_min) / 210)

    # No-acceptance margin — use retest count as an inverse proxy for the
    # tightness of the failed breakout (already validated by state machine
    # not going to INVALIDATED, so this is a bonus factor)
    margin_score = 1.0 if zone.retest_count == 0 else 0.7

    total_frac = 0.4 * disp_score + 0.4 * speed_score + 0.2 * margin_score
    pts = int(round(total_frac * max_pts))
    reason = (f"disp {disp:.0f}pt ({int(disp_score*100)}%) · "
              f"reclaim {int(delta_min)}min ({int(speed_score*100)}%) · "
              f"margin ({int(margin_score*100)}%)")
    return (pts, reason)


def _score_rejection_quality(zone, ctx: MarketContext, max_pts: int) -> tuple[int, str]:
    """
    Strength of the retest rejection — the trigger candle's shape.

    Scoring based on the last few M15 bars after retest:
      +50% closing direction away from zone
      +30% closing distance from zone (deeper close = stronger rejection)
      +20% wick length at level (long wick = strong rejection touch)
    """
    if not ctx.m15_bars or len(ctx.m15_bars) < 3 or not zone.reference_price:
        return (0, "insufficient M15 bars")

    last = ctx.m15_bars[-1]
    prev = ctx.m15_bars[-2]

    # Direction: for SELL zone, we want closes BELOW level
    if zone.level_side == "SELL":
        close_dir_ok = last.close < zone.reference_price
        distance = zone.reference_price - last.close
    else:
        close_dir_ok = last.close > zone.reference_price
        distance = last.close - zone.reference_price

    dir_score = 1.0 if close_dir_ok else 0.0

    # Distance scaled by ATR — 0.5xATR full pts, 0 if wrong side
    atr = max(1.0, ctx.atr_h1)
    dist_score = max(0.0, min(1.0, distance / (0.5 * atr))) if distance > 0 else 0.0

    # Wick at level: for SELL, high should have poked ABOVE level; for BUY, low below
    if zone.level_side == "SELL":
        wick_at_level = max(0.0, last.high - zone.reference_price)
    else:
        wick_at_level = max(0.0, zone.reference_price - last.low)
    # scale wick by ATR: 0.2xATR wick = full pts
    wick_score = min(1.0, wick_at_level / (0.2 * atr))

    total_frac = 0.5 * dir_score + 0.3 * dist_score + 0.2 * wick_score
    pts = int(round(total_frac * max_pts))
    reason = (f"dir={'✓' if close_dir_ok else '✗'} · "
              f"dist {distance:.1f}pt ({int(dist_score*100)}%) · "
              f"wick {wick_at_level:.1f}pt ({int(wick_score*100)}%)")
    return (pts, reason)


def _score_market_structure(zone, ctx: MarketContext, max_pts: int) -> tuple[int, str]:
    """
    Market-structure shift on H1 in the trade direction.

    For SELL: look for a lower high AND break of a recent swing low.
    For BUY:  higher low AND break of a recent swing high.

    Simple check: last N=8 H1 bars, compare pivots.
    """
    if not ctx.h1_bars or len(ctx.h1_bars) < 8:
        return (0, "insufficient H1 bars")

    bars = ctx.h1_bars[-8:]
    highs = [b.high for b in bars]
    lows  = [b.low  for b in bars]

    if zone.level_side == "SELL":
        # Lower high test: last high < prior high
        lower_high = highs[-1] < max(highs[:-1])
        # Break of recent swing low: last close below min of previous 6 lows
        broke_low = bars[-1].close < min(lows[:-1])
        conditions = int(lower_high) + int(broke_low)
    else:
        higher_low = lows[-1] > min(lows[:-1])
        broke_high = bars[-1].close > max(highs[:-1])
        conditions = int(higher_low) + int(broke_high)

    # 2 conditions = full pts; 1 = half; 0 = nothing
    pts = int(round(max_pts * conditions / 2))
    if zone.level_side == "SELL":
        reason = f"lower-high {'✓' if lower_high else '✗'} · broke-low {'✓' if broke_low else '✗'}"
    else:
        reason = f"higher-low {'✓' if higher_low else '✗'} · broke-high {'✓' if broke_high else '✗'}"
    return (pts, reason)


def _score_volume_orderflow(zone, ctx: MarketContext, max_pts: int,
                            tick_penalty: int = 15) -> tuple[int, str]:
    """
    Volume/order-flow footprint at the breakout AND at the retest.

    Ideal pattern:
      - HIGH volume at breakout (participation) but no follow-through
      - LOW volume at retest (no confirmed acceptance)

    When only tick_proxy is available, apply the tick-volume penalty at the
    end (reduces the earned score by `tick_penalty`% of max_pts). This
    ensures signals derived from proxy data get lower composite scores per
    brief's Data Hierarchy requirement.
    """
    if not ctx.m15_bars or len(ctx.m15_bars) < 20:
        return (0, "insufficient M15 bars")

    bars = ctx.m15_bars[-20:]
    avg_vol = sum(float(b.volume or 0) for b in bars) / len(bars)
    if avg_vol <= 0:
        return (0, "zero avg volume")

    # Breakout volume: bar at reclaim_time approximately. We look for the
    # highest-volume bar in the recent range as a heuristic.
    breakout_vol = max(float(b.volume or 0) for b in bars)
    # Retest volume: use the LAST bar's volume
    retest_vol = float(bars[-1].volume or 0)

    breakout_ratio = breakout_vol / avg_vol
    retest_ratio   = retest_vol / avg_vol

    # Ideal: breakout ratio 1.5x+, retest ratio 1.0x-
    breakout_ok = min(1.0, max(0.0, (breakout_ratio - 1.0) / 1.5))  # 1.5x = full
    retest_low  = max(0.0, min(1.0, (1.5 - retest_ratio) / 1.0))    # <1.5x = full

    total_frac = 0.6 * breakout_ok + 0.4 * retest_low
    raw_pts = total_frac * max_pts

    # Tick-volume penalty
    penalty_pts = 0.0
    if ctx.volume_source == "tick_proxy":
        penalty_pts = (tick_penalty / 100.0) * max_pts

    pts = max(0, int(round(raw_pts - penalty_pts)))
    reason = (f"breakout {breakout_ratio:.1f}x · retest {retest_ratio:.1f}x · "
              f"source {ctx.volume_source}")
    if penalty_pts > 0:
        reason += f" (-{penalty_pts:.0f} tick penalty)"
    return (pts, reason)


def _score_session_quality(ctx: MarketContext, max_pts: int) -> tuple[int, str]:
    pts, name = _session_score(ctx.now_utc, max_pts)
    return (pts, f"{name} @ {ctx.now_utc.hour:02d}:{ctx.now_utc.minute:02d} UTC")


def _score_htf_alignment(zone, ctx: MarketContext, max_pts: int) -> tuple[int, str, bool]:
    """
    HTF trend alignment with the trade direction.
    Returns (points, reason, is_countertrend).
    """
    d1 = (ctx.d1_bias or "").lower()
    h4 = (ctx.h4_bias or "").lower()

    is_bull_htf = "bull" in d1 and "bull" in h4
    is_bear_htf = "bear" in d1 and "bear" in h4
    mixed       = not is_bull_htf and not is_bear_htf

    if zone.level_side == "SELL":
        if is_bear_htf:
            return (max_pts, "SELL aligned with bearish D1+H4", False)
        if is_bull_htf:
            return (max(0, int(max_pts * 0.3)), "SELL countertrend to bullish D1+H4", True)
        return (int(max_pts * 0.5), "SELL vs mixed HTF", False)
    else:
        if is_bull_htf:
            return (max_pts, "BUY aligned with bullish D1+H4", False)
        if is_bear_htf:
            return (max(0, int(max_pts * 0.3)), "BUY countertrend to bearish D1+H4", True)
        return (int(max_pts * 0.5), "BUY vs mixed HTF", False)


def _score_rr_quality(entry: float, sl: float, tp: float,
                      max_pts: int, min_rr: float = 1.8) -> tuple[int, str, float]:
    """
    Returns (points, reason, rr_computed).
    """
    if not entry or not sl or not tp:
        return (0, "entry/SL/TP missing", 0.0)
    risk = abs(entry - sl)
    reward = abs(tp - entry)
    if risk <= 0:
        return (0, "zero risk", 0.0)
    rr = round(reward / risk, 2)
    if rr < min_rr:
        return (0, f"RR {rr:.2f} < min {min_rr:.2f} (fails gate)", rr)
    # RR band scoring: min→0.5*max, min+1→full max
    frac = min(1.0, (rr - min_rr) / 1.0 + 0.5)
    pts = int(round(frac * max_pts))
    return (pts, f"RR {rr:.2f} (min {min_rr:.2f})", rr)


def _score_space_to_target(entry: float, tp: float, zone, ctx: MarketContext,
                           max_pts: int) -> tuple[int, str]:
    """
    Clean path between entry and TP1: no opposing HVN / POC / VWAP block.
    Requires liquidity_map to be passed in; if not, half-credit.
    """
    if not ctx.liquidity_map:
        return (int(max_pts * 0.5), "no liquidity_map provided — neutral")
    if not entry or not tp:
        return (0, "entry/TP missing")

    # For SELL: check if any high-magnetism zone sits BETWEEN entry (top) and TP (bottom)
    # For BUY: mirror
    lo = min(entry, tp)
    hi = max(entry, tp)
    obstacles = []
    for side in ("buy_side_pools", "sell_side_pools"):
        for z in (ctx.liquidity_map.get(side) or []):
            price = z.get("price")
            mag   = z.get("magnetism", 0)
            if price is None:
                continue
            if lo < price < hi and mag >= 65:
                # opposing high-magnetism obstacle
                obstacles.append((price, mag, z.get("zone_type", "?")))
    if not obstacles:
        return (max_pts, "clean path")
    # 1 obstacle = half; 2+ = none
    if len(obstacles) == 1:
        p, m, t = obstacles[0]
        return (int(max_pts * 0.4), f"1 obstacle: {t} @ ${p} (mag {m})")
    return (0, f"{len(obstacles)} obstacles in path")


def _score_data_quality(ctx: MarketContext, max_pts: int) -> tuple[int, str]:
    if not ctx.news_clear:
        return (0, "news window active — blocked")
    if ctx.volume_source == "comex_gc":
        return (max_pts, "comex_gc source")
    if ctx.volume_source == "broker_real":
        return (int(max_pts * 0.7), "broker_real source")
    return (int(max_pts * 0.4), "tick_proxy source (Phase 1 reality)")


# ── Trade-plan computation ──────────────────────────────────────────────────

def compute_trade_plan(zone, profile, ctx: MarketContext) -> dict:
    """
    Build entry / SL / TP1 / TP2 / TP3 from the triggered zone + profile.

    Rules:
      Entry = current price (retest rejection point)
      SL    = beyond the breakout extreme + 0.15 × ATR buffer
      TP1   = nearest opposing level (POC or opposite VA edge)
      TP2   = far side of value area
      TP3   = beyond value area (opposite VA edge extended by 1× VA width)
    """
    atr = max(1.0, ctx.atr_h1)
    buf = 0.15 * atr

    if zone.level_side == "SELL":
        entry = ctx.current_price
        breakout_ext = zone.breakout_extreme or (zone.reference_price + atr * 0.5)
        sl = round(max(breakout_ext + buf, zone.reference_price + buf), 2)
        # Target ladder: descending
        tp1 = round(profile.poc, 2)
        tp2 = round(profile.val, 2)
        va_width = max(1.0, profile.vah - profile.val)
        tp3 = round(profile.val - va_width, 2)
    else:
        entry = ctx.current_price
        breakout_ext = zone.breakout_extreme or (zone.reference_price - atr * 0.5)
        sl = round(min(breakout_ext - buf, zone.reference_price - buf), 2)
        tp1 = round(profile.poc, 2)
        tp2 = round(profile.vah, 2)
        va_width = max(1.0, profile.vah - profile.val)
        tp3 = round(profile.vah + va_width, 2)

    return {
        "entry": round(entry, 2), "sl": sl,
        "tp1": tp1, "tp2": tp2, "tp3": tp3,
    }


# ── Public entry point ──────────────────────────────────────────────────────

def score_zone(
    zone,
    profile,
    ctx: MarketContext,
    weights: dict = None,
    countertrend_bonus: int = 10,
    tick_volume_penalty: int = 15,
    min_rr: float = 1.8,
    live_threshold: int = 80,
) -> tuple[ScoreBreakdown, dict]:
    """
    Score a TRIGGERED zone against the brief's confluence hierarchy.

    Returns (breakdown, trade_plan_dict).
    Callers should only fire alerts (Phase 4) when breakdown.would_fire is True.
    """
    weights = weights or DEFAULT_WEIGHTS

    plan = compute_trade_plan(zone, profile, ctx)

    factors: dict[str, int] = {}
    reasons: dict[str, str] = {}

    # Tier 1 mandatory
    v_pts, v_r = _score_trap_validity(zone, weights["trap_validity"])
    factors["trap_validity"] = v_pts; reasons["trap_validity"] = v_r

    r_pts, r_r = _score_rejection_quality(zone, ctx, weights["rejection_quality"])
    factors["rejection_quality"] = r_pts; reasons["rejection_quality"] = r_r

    m_pts, m_r = _score_market_structure(zone, ctx, weights["market_structure"])
    factors["market_structure"] = m_pts; reasons["market_structure"] = m_r

    o_pts, o_r = _score_volume_orderflow(zone, ctx, weights["volume_orderflow"],
                                          tick_penalty=tick_volume_penalty)
    factors["volume_orderflow"] = o_pts; reasons["volume_orderflow"] = o_r

    s_pts, s_r = _score_session_quality(ctx, weights["session_quality"])
    factors["session_quality"] = s_pts; reasons["session_quality"] = s_r

    h_pts, h_r, is_ct = _score_htf_alignment(zone, ctx, weights["htf_alignment"])
    factors["htf_alignment"] = h_pts; reasons["htf_alignment"] = h_r

    rr_pts, rr_r, rr_val = _score_rr_quality(plan["entry"], plan["sl"], plan["tp2"],
                                              weights["rr_quality"], min_rr=min_rr)
    factors["rr_quality"] = rr_pts; reasons["rr_quality"] = rr_r
    plan["rr"] = rr_val

    sp_pts, sp_r = _score_space_to_target(plan["entry"], plan["tp1"], zone, ctx,
                                           weights["space_to_target"])
    factors["space_to_target"] = sp_pts; reasons["space_to_target"] = sp_r

    d_pts, d_r = _score_data_quality(ctx, weights["data_quality"])
    factors["data_quality"] = d_pts; reasons["data_quality"] = d_r

    total = sum(factors.values())
    max_map = dict(weights)

    # Hard gates (any missing = force below threshold regardless of composite):
    #   - Data quality zero (news blocked etc.)
    #   - RR below min (already returns 0 from scorer)
    hard_gate_failed = False
    hard_gate_reason = ""
    if d_pts == 0 and "news" in d_r.lower():
        hard_gate_failed = True
        hard_gate_reason = "news window blocks execution"
    if rr_pts == 0:
        hard_gate_failed = True
        hard_gate_reason = f"RR below {min_rr:.1f} floor"

    effective_threshold = live_threshold + (countertrend_bonus if is_ct else 0)
    would_fire = (total >= effective_threshold) and not hard_gate_failed

    # Band assignment
    if total >= 90:                     band = BAND_EXCEPTIONAL
    elif total >= 80:                   band = BAND_VALID
    elif total >= 70:                   band = BAND_DEVELOPING
    elif total >= 60:                   band = BAND_WATCH
    else:                               band = BAND_NO_SIGNAL

    # Human-readable summaries
    conditions_met = []
    conditions_missing = []
    for name, pts in factors.items():
        max_p = max_map.get(name, 0)
        if max_p == 0:
            continue
        pct = pts / max_p
        label = f"{name.replace('_', ' ')} ({pts}/{max_p} · {reasons.get(name, '')})"
        if pct >= 0.7:
            conditions_met.append(label)
        elif pct <= 0.3:
            conditions_missing.append(label)

    qualify_bits = [
        f"trap {zone.level_type} {zone.level_side} @ ${zone.reference_price:.2f}",
        f"disp {zone.displacement_pts:.1f}pt" if zone.displacement_pts else "no disp",
        f"session {reasons.get('session_quality', '')}",
    ]
    if is_ct:
        qualify_bits.append(f"COUNTERTREND — needs {effective_threshold}")
    if hard_gate_failed:
        qualify_bits.append(f"HARD GATE: {hard_gate_reason}")
    reason_qualifies = " · ".join(qualify_bits)

    return (ScoreBreakdown(
        total=total, band=band,
        factors=factors, factor_max=max_map,
        reason_qualifies=reason_qualifies,
        conditions_met=conditions_met,
        conditions_missing=conditions_missing,
        is_countertrend=is_ct,
        effective_threshold=effective_threshold,
        would_fire=would_fire,
    ), plan)
