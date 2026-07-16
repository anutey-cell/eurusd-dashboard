"""
Engineered Liquidity Map
========================

Detects and ranks the seven types of "engineered liquidity" institutions
target — the price levels where retail stops cluster and get swept before
reversal. Feeds the daily brief's playbook and (in future) the strategist's
C3 confluence bonus.

Types tracked (ICT/SMC terminology, ranked by base magnetism):

  Type                         Base score
  ─────────────────────────────────────────
  weekly_high / weekly_low        80  — deep institutional pools
  today_high / today_low          65  — nearest-term intraday magnets
  prev_day_high / prev_day_low    70  — textbook stop zones
  equal_highs / equal_lows        40 + 15/touch (max 70) — obvious retail
                                         double-top/bottom stop clusters
  asian_high / asian_low          55  — London's designed sweep target
  london_high / london_low        60  — NY's designed sweep target
  round_number                    30  — psychological stops (auto-generated
                                         within ±3 ATR of current price)
  range_high / range_low          50  — false-break trap boundaries

Magnetism adjustments:
  +15  within 1.0 × ATR of current price (imminent)
  +10  aligned with predominant HTF direction (bonus for the working side)
  -30  swept within stale_lookback_bars (already tapped, less magnetic)
  -15  more than 3 × ATR from current price (too far to be relevant)

Terminology (ICT):
  "buy-side liquidity"  = pools ABOVE price (buy-stops from shorts)
  "sell-side liquidity" = pools BELOW price (sell-stops from longs)
  Institutions sweep buy-side, then sell.  Sweep sell-side, then buy.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field, asdict
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

log = logging.getLogger(__name__)


# ── Type constants ──────────────────────────────────────────────────────────

WEEKLY_HIGH   = "weekly_high"
WEEKLY_LOW    = "weekly_low"
PREV_DAY_HIGH = "prev_day_high"
PREV_DAY_LOW  = "prev_day_low"
TODAY_HIGH    = "today_high"
TODAY_LOW     = "today_low"
EQUAL_HIGHS   = "equal_highs"
EQUAL_LOWS    = "equal_lows"
ASIAN_HIGH    = "asian_high"
ASIAN_LOW     = "asian_low"
LONDON_HIGH   = "london_high"
LONDON_LOW    = "london_low"
ROUND_NUMBER  = "round_number"
RANGE_HIGH    = "range_high"
RANGE_LOW     = "range_low"

_BASE_MAGNETISM: dict[str, int] = {
    WEEKLY_HIGH:   80,
    WEEKLY_LOW:    80,
    PREV_DAY_HIGH: 70,
    PREV_DAY_LOW:  70,
    TODAY_HIGH:    65,
    TODAY_LOW:     65,
    LONDON_HIGH:   60,
    LONDON_LOW:    60,
    ASIAN_HIGH:    55,
    ASIAN_LOW:     55,
    RANGE_HIGH:    50,
    RANGE_LOW:     50,
    EQUAL_HIGHS:   40,   # +15 per extra touch
    EQUAL_LOWS:    40,
    ROUND_NUMBER:  30,
}

# Zones on the SELL side (below price) end in _low; buy-side (above) end in _high
_IS_UPPER_ZONE = lambda t: t.endswith("_high") or t.endswith("_highs")


# ── Data classes ────────────────────────────────────────────────────────────

@dataclass
class LiquidityZone:
    price:         float
    zone_type:     str
    side:          str      # "buy_side" (above price) | "sell_side" (below)
    touches:       int     = 1
    detected_at:   Optional[datetime] = None
    # Filled in during map building
    magnetism:     int     = 0
    distance_pts:  float   = 0.0
    distance_atr:  float   = 0.0
    is_stale:      bool    = False
    rationale:     str     = ""


@dataclass
class LiquidityMap:
    current_price:    float
    atr_h1:           float
    generated_at:     datetime
    buy_side_pools:   list[LiquidityZone] = field(default_factory=list)   # above price
    sell_side_pools:  list[LiquidityZone] = field(default_factory=list)   # below price
    nearest_above:    Optional[LiquidityZone] = None
    nearest_below:    Optional[LiquidityZone] = None
    highest_magnetism: Optional[LiquidityZone] = None

    def to_dict(self) -> dict:
        return {
            "current_price":     self.current_price,
            "atr_h1":            self.atr_h1,
            "generated_at":      self.generated_at.isoformat() if self.generated_at else None,
            "buy_side_pools":    [asdict(z) | {"detected_at": (z.detected_at.isoformat() if z.detected_at else None)} for z in self.buy_side_pools],
            "sell_side_pools":   [asdict(z) | {"detected_at": (z.detected_at.isoformat() if z.detected_at else None)} for z in self.sell_side_pools],
            "nearest_above":     asdict(self.nearest_above) if self.nearest_above else None,
            "nearest_below":     asdict(self.nearest_below) if self.nearest_below else None,
            "highest_magnetism": asdict(self.highest_magnetism) if self.highest_magnetism else None,
        }


# ── Detection helpers ───────────────────────────────────────────────────────

def _detect_prev_day_and_weekly(candles_d1: list) -> list[LiquidityZone]:
    """Prev-day H/L and weekly (5-day) H/L pools."""
    zones: list[LiquidityZone] = []
    if not candles_d1 or len(candles_d1) < 2:
        return zones

    prev = candles_d1[-2]   # yesterday's daily bar
    zones.append(LiquidityZone(
        price=round(prev.high, 2), zone_type=PREV_DAY_HIGH, side="buy_side",
        detected_at=prev.time, rationale=f"Yesterday's high @ ${prev.high:.2f}",
    ))
    zones.append(LiquidityZone(
        price=round(prev.low,  2), zone_type=PREV_DAY_LOW, side="sell_side",
        detected_at=prev.time, rationale=f"Yesterday's low @ ${prev.low:.2f}",
    ))

    # Weekly = last 5 daily bars
    window = candles_d1[-5:] if len(candles_d1) >= 5 else candles_d1
    if window:
        wh = max(window, key=lambda c: c.high)
        wl = min(window, key=lambda c: c.low)
        zones.append(LiquidityZone(
            price=round(wh.high, 2), zone_type=WEEKLY_HIGH, side="buy_side",
            detected_at=wh.time, rationale=f"5-day high @ ${wh.high:.2f}",
        ))
        zones.append(LiquidityZone(
            price=round(wl.low,  2), zone_type=WEEKLY_LOW, side="sell_side",
            detected_at=wl.time, rationale=f"5-day low @ ${wl.low:.2f}",
        ))
    return zones


def _detect_today_hl(candles_m15: list) -> list[LiquidityZone]:
    """Today's intraday high / low so far (from M15 candles for today's UTC date)."""
    zones: list[LiquidityZone] = []
    if not candles_m15:
        return zones
    now = datetime.now(timezone.utc)
    today = now.date()
    today_bars = [c for c in candles_m15
                  if (c.time if c.time.tzinfo else c.time.replace(tzinfo=timezone.utc))
                     .astimezone(timezone.utc).date() == today]
    if not today_bars:
        return zones
    hi = max(today_bars, key=lambda c: c.high)
    lo = min(today_bars, key=lambda c: c.low)
    zones.append(LiquidityZone(
        price=round(hi.high, 2), zone_type=TODAY_HIGH, side="buy_side",
        detected_at=hi.time, rationale=f"Today's high @ ${hi.high:.2f}",
    ))
    zones.append(LiquidityZone(
        price=round(lo.low,  2), zone_type=TODAY_LOW, side="sell_side",
        detected_at=lo.time, rationale=f"Today's low @ ${lo.low:.2f}",
    ))
    return zones


def _detect_equal_hilo(
    candles_m15: list,
    tolerance_pts: float = 2.0,
    min_touches:   int   = 2,
    max_touches_cap: int = 5,
    lookback_bars: int   = 48,
) -> list[LiquidityZone]:
    """Cluster recent M15 highs/lows within tolerance → equal H/L pools.
    A cluster of N touches gets magnetism 40 + 15*(N-2), capped at 70."""
    zones: list[LiquidityZone] = []
    if not candles_m15 or len(candles_m15) < min_touches:
        return zones
    recent = candles_m15[-lookback_bars:]

    def _cluster(values: list[tuple[float, datetime]]) -> list[tuple[float, int, datetime]]:
        """Group values within tolerance_pts. Returns list of (avg_price, count, last_time)."""
        if not values: return []
        vs = sorted(values, key=lambda x: x[0])
        clusters: list[list[tuple[float, datetime]]] = [[vs[0]]]
        for v, t in vs[1:]:
            if abs(v - clusters[-1][-1][0]) <= tolerance_pts:
                clusters[-1].append((v, t))
            else:
                clusters.append([(v, t)])
        return [
            (sum(v for v, _ in c) / len(c), len(c), max(t for _, t in c))
            for c in clusters
        ]

    highs = [(c.high, c.time) for c in recent]
    lows  = [(c.low,  c.time) for c in recent]

    for avg, n, last_t in _cluster(highs):
        if n >= min_touches:
            zones.append(LiquidityZone(
                price=round(avg, 2), zone_type=EQUAL_HIGHS, side="buy_side",
                touches=min(n, max_touches_cap), detected_at=last_t,
                rationale=f"Equal highs ×{n} within {tolerance_pts}pt @ ${avg:.2f}",
            ))
    for avg, n, last_t in _cluster(lows):
        if n >= min_touches:
            zones.append(LiquidityZone(
                price=round(avg, 2), zone_type=EQUAL_LOWS, side="sell_side",
                touches=min(n, max_touches_cap), detected_at=last_t,
                rationale=f"Equal lows ×{n} within {tolerance_pts}pt @ ${avg:.2f}",
            ))
    return zones


def _detect_session_pivots(candles_h1: list) -> list[LiquidityZone]:
    """Prior day's Asian range H/L and London H/L become the current day's pools."""
    zones: list[LiquidityZone] = []
    if not candles_h1:
        return zones
    # Prior COMPLETE day (yesterday UTC)
    now = datetime.now(timezone.utc)
    yesterday = (now - timedelta(days=1)).date()

    def _bars_in(hour_lo: int, hour_hi: int, date_: Any) -> list:
        return [c for c in candles_h1
                if (c.time if c.time.tzinfo else c.time.replace(tzinfo=timezone.utc))
                   .astimezone(timezone.utc).date() == date_
                and hour_lo <= (c.time if c.time.tzinfo else c.time.replace(tzinfo=timezone.utc))
                    .astimezone(timezone.utc).hour < hour_hi]

    asian = _bars_in(0, 6, yesterday)   # Asian range 00-06 UTC
    if asian:
        ah = max(asian, key=lambda c: c.high)
        al = min(asian, key=lambda c: c.low)
        zones.append(LiquidityZone(
            price=round(ah.high, 2), zone_type=ASIAN_HIGH, side="buy_side",
            detected_at=ah.time, rationale=f"Yesterday's Asian high @ ${ah.high:.2f}",
        ))
        zones.append(LiquidityZone(
            price=round(al.low,  2), zone_type=ASIAN_LOW, side="sell_side",
            detected_at=al.time, rationale=f"Yesterday's Asian low @ ${al.low:.2f}",
        ))

    london = _bars_in(7, 12, yesterday)   # London window 07-12 UTC
    if london:
        lh = max(london, key=lambda c: c.high)
        ll = min(london, key=lambda c: c.low)
        zones.append(LiquidityZone(
            price=round(lh.high, 2), zone_type=LONDON_HIGH, side="buy_side",
            detected_at=lh.time, rationale=f"Yesterday's London high @ ${lh.high:.2f}",
        ))
        zones.append(LiquidityZone(
            price=round(ll.low,  2), zone_type=LONDON_LOW, side="sell_side",
            detected_at=ll.time, rationale=f"Yesterday's London low @ ${ll.low:.2f}",
        ))
    return zones


def _detect_round_numbers(current_price: float, atr_h1: float, range_atr: float = 3.0,
                          step: float = 50.0) -> list[LiquidityZone]:
    """Generate round-number zones within +/- range_atr of current price."""
    zones: list[LiquidityZone] = []
    if atr_h1 <= 0 or step <= 0:
        return zones
    span = max(atr_h1 * range_atr, step)
    lower = current_price - span
    upper = current_price + span
    # Round to nearest step multiple
    start = int((lower // step) * step)
    end   = int((upper // step + 1) * step)
    for level in range(start, end + int(step), int(step)):
        if level <= 0: continue
        if abs(level - current_price) < 2.0: continue   # skip if at price
        side = "buy_side" if level > current_price else "sell_side"
        zones.append(LiquidityZone(
            price=float(level), zone_type=ROUND_NUMBER, side=side,
            rationale=f"Round number ${level}",
        ))
    return zones


def _detect_range_boundaries(candles_h1: list, min_range_bars: int = 8,
                             tolerance_atr: float = 0.3,
                             atr_h1: float = 0.0) -> list[LiquidityZone]:
    """Find recent consolidation ranges; their H/L are trap boundaries."""
    zones: list[LiquidityZone] = []
    if not candles_h1 or len(candles_h1) < min_range_bars or atr_h1 <= 0:
        return zones
    tolerance = atr_h1 * tolerance_atr
    window = candles_h1[-min_range_bars:]
    high = max(c.high for c in window)
    low  = min(c.low  for c in window)
    range_size = high - low
    # Range is meaningful only if it's tight relative to ATR
    if range_size <= atr_h1 * 2.5:
        zones.append(LiquidityZone(
            price=round(high, 2), zone_type=RANGE_HIGH, side="buy_side",
            detected_at=window[-1].time,
            rationale=f"H1 range top over last {min_range_bars} bars",
        ))
        zones.append(LiquidityZone(
            price=round(low,  2), zone_type=RANGE_LOW, side="sell_side",
            detected_at=window[-1].time,
            rationale=f"H1 range bottom over last {min_range_bars} bars",
        ))
    return zones


# ── Magnetism scoring + stale detection ─────────────────────────────────────

def _score_magnetism(z: LiquidityZone, current_price: float, atr_h1: float,
                     htf_bias: str = "") -> int:
    """Apply base + adjustments; cap 0-100."""
    base = _BASE_MAGNETISM.get(z.zone_type, 30)
    # Equal H/L touches bonus
    if z.zone_type in (EQUAL_HIGHS, EQUAL_LOWS):
        base += 15 * max(0, z.touches - 2)
        base = min(base, 70)

    score = base
    # Distance adjustment
    if atr_h1 > 0:
        d_atr = abs(z.price - current_price) / atr_h1
        if d_atr <= 1.0:
            score += 15
        elif d_atr > 3.0:
            score -= 15
    # HTF alignment bonus — buy-side pool aligns with bearish bias (target for sells)
    #                       sell-side pool aligns with bullish bias (target for buys)
    b = (htf_bias or "").lower()
    if "bear" in b and z.side == "buy_side":
        score += 10
    elif "bull" in b and z.side == "sell_side":
        score += 10

    # Stale penalty applied by caller after is_stale check

    return max(0, min(100, score))


def _is_swept_recently(z: LiquidityZone, candles_m15: list, lookback_bars: int = 10) -> bool:
    """True if price has traded through this zone in the last N M15 bars."""
    if not candles_m15 or lookback_bars <= 0:
        return False
    recent = candles_m15[-lookback_bars:]
    if _IS_UPPER_ZONE(z.zone_type):
        # Upper zone swept if any recent high pierced it
        return any(c.high >= z.price for c in recent)
    else:
        return any(c.low <= z.price for c in recent)


# ── Public entry point ──────────────────────────────────────────────────────

def build_liquidity_map(
    candles_m15:    list,
    candles_h1:     list,
    candles_d1:     list,
    current_price:  float,
    atr_h1:         float,
    htf_bias:       str  = "",
    stale_lookback: int  = 10,
    max_per_side:   int  = 5,
) -> LiquidityMap:
    """Detect + score + rank all engineered-liquidity zones."""
    zones: list[LiquidityZone] = []
    zones.extend(_detect_prev_day_and_weekly(candles_d1))
    zones.extend(_detect_today_hl(candles_m15))
    zones.extend(_detect_equal_hilo(candles_m15))
    zones.extend(_detect_session_pivots(candles_h1))
    zones.extend(_detect_round_numbers(current_price, atr_h1))
    zones.extend(_detect_range_boundaries(candles_h1, atr_h1=atr_h1))

    # Deduplicate: multiple detectors may find the same price level. Merge by
    # rounding to $1 and picking the highest-magnetism representative.
    dedup: dict[tuple[float, str], LiquidityZone] = {}
    for z in zones:
        key = (round(z.price), z.side)
        if key not in dedup or _BASE_MAGNETISM.get(z.zone_type, 0) > _BASE_MAGNETISM.get(dedup[key].zone_type, 0):
            dedup[key] = z
    zones = list(dedup.values())

    # Score, distance, stale
    for z in zones:
        z.distance_pts = round(abs(z.price - current_price), 2)
        z.distance_atr = round(z.distance_pts / atr_h1, 2) if atr_h1 > 0 else 999.0
        z.is_stale     = _is_swept_recently(z, candles_m15, stale_lookback)
        z.magnetism    = _score_magnetism(z, current_price, atr_h1, htf_bias)
        if z.is_stale:
            z.magnetism = max(0, z.magnetism - 30)

    above = sorted([z for z in zones if z.price > current_price + 1.0],
                   key=lambda z: (-z.magnetism, z.distance_pts))[:max_per_side]
    below = sorted([z for z in zones if z.price < current_price - 1.0],
                   key=lambda z: (-z.magnetism, z.distance_pts))[:max_per_side]

    # Nearest by distance (not magnetism), for playbook
    nearest_above = min(
        [z for z in above], key=lambda z: z.distance_pts, default=None
    )
    nearest_below = min(
        [z for z in below], key=lambda z: z.distance_pts, default=None
    )
    all_zones = above + below
    highest = max(all_zones, key=lambda z: z.magnetism) if all_zones else None

    return LiquidityMap(
        current_price=current_price,
        atr_h1=atr_h1,
        generated_at=datetime.now(timezone.utc),
        buy_side_pools=above,
        sell_side_pools=below,
        nearest_above=nearest_above,
        nearest_below=nearest_below,
        highest_magnetism=highest,
    )


# ── Playbook generator ──────────────────────────────────────────────────────

def sniper_playbook(lm: LiquidityMap, htf_bias: str = "") -> dict:
    """
    Convert the map + bias into concrete IF-THEN triggers for the daily brief.

    Returns dict:
      primary:      {direction, trigger, entry, sl, tp1, tp2, rr_est}
      secondary:    same shape (counter-bias play)
      avoid:        list[str]
    """
    b = (htf_bias or "").lower()
    is_bull = "bull" in b and "conflict" not in b
    is_bear = "bear" in b and "conflict" not in b

    # Walk the pool stack (already sorted by magnetism-desc / distance-asc) and
    # pick the first non-stale zone with meaningful magnetism. Falls back to
    # the first non-stale zone regardless of score, then the highest-magnetism
    # zone even if stale. This makes the playbook useful in chop where recent
    # zones are all tapped -- deeper non-stale pools become the real targets.
    def _pick(pools: list[LiquidityZone], min_magnetism: int = 40) -> Optional[LiquidityZone]:
        if not pools:
            return None
        for p in pools:
            if not p.is_stale and p.magnetism >= min_magnetism:
                return p
        for p in pools:
            if not p.is_stale:
                return p
        # All stale -- take the first (highest-magnetism-or-nearest by sort key)
        return pools[0]

    # Realistic entry is ~1 M15 candle beyond the sweep wick (CISD close).
    # Approximate as 0.4 × ATR beyond the pool. SL sits 0.15 × ATR the other
    # side of the wick (buffer for wick noise). RR is computed from ENTRY,
    # not from the pool level — the pool → SL distance alone is just buffer.
    atr = max(lm.atr_h1 or 30.0, 10.0)
    entry_offset = round(atr * 0.4, 2)
    sl_buffer    = round(atr * 0.15, 2)

    def _sell_setup() -> Optional[dict]:
        pool = _pick(lm.buy_side_pools)
        if not pool:
            return None
        tp1_zone = next((z for z in lm.sell_side_pools if not z.is_stale), None)
        tp2_zone = next((z for z in lm.sell_side_pools if z.magnetism >= 65 and not z.is_stale), None)
        tp1      = tp1_zone.price if tp1_zone else (lm.sell_side_pools[0].price if lm.sell_side_pools else None)
        tp2      = tp2_zone.price if tp2_zone else tp1
        entry_est = round(pool.price - entry_offset, 2)
        sl_price  = round(pool.price + sl_buffer, 2)
        rr_est = "—"
        if tp2 and entry_est and sl_price:
            risk   = sl_price - entry_est
            reward = entry_est - tp2
            rr_est = f"~{reward/risk:.1f}R" if risk > 0 and reward > 0 else "—"
        stale_note = "  (target already tapped in last ~10 bars — wait for retest)" if pool.is_stale else ""
        return {
            "direction": "SELL",
            "trigger":   f"Sweep of {_human_zone(pool)} @ ${pool.price:.2f} without CISD reclaim above{stale_note}",
            "entry":     f"~${entry_est:.2f} (next H1 close below sweep wick)",
            "sl":        f"${sl_price:.2f} ({sl_buffer:.1f}pt above sweep wick)",
            "tp1":       (f"${tp1:.2f}" if tp1 else "—"),
            "tp2":       (f"${tp2:.2f}" if tp2 else "—"),
            "rr_est":    rr_est,
        }

    def _buy_setup() -> Optional[dict]:
        pool = _pick(lm.sell_side_pools)
        if not pool:
            return None
        tp1_zone = next((z for z in lm.buy_side_pools if not z.is_stale), None)
        tp2_zone = next((z for z in lm.buy_side_pools if z.magnetism >= 65 and not z.is_stale), None)
        tp1      = tp1_zone.price if tp1_zone else (lm.buy_side_pools[0].price if lm.buy_side_pools else None)
        tp2      = tp2_zone.price if tp2_zone else tp1
        entry_est = round(pool.price + entry_offset, 2)
        sl_price  = round(pool.price - sl_buffer, 2)
        rr_est = "—"
        if tp2 and entry_est and sl_price:
            risk   = entry_est - sl_price
            reward = tp2 - entry_est
            rr_est = f"~{reward/risk:.1f}R" if risk > 0 and reward > 0 else "—"
        stale_note = "  (target already tapped in last ~10 bars — wait for retest)" if pool.is_stale else ""
        return {
            "direction": "BUY",
            "trigger":   f"Sweep of {_human_zone(pool)} @ ${pool.price:.2f} + CISD close above wick{stale_note}",
            "entry":     f"~${entry_est:.2f} (next H1 close above sweep wick)",
            "sl":        f"${sl_price:.2f} ({sl_buffer:.1f}pt below sweep wick)",
            "tp1":       (f"${tp1:.2f}" if tp1 else "—"),
            "tp2":       (f"${tp2:.2f}" if tp2 else "—"),
            "rr_est":    rr_est,
        }

    primary   = None
    secondary = None
    if is_bear:
        primary   = _sell_setup()
        secondary = _buy_setup()
    elif is_bull:
        primary   = _buy_setup()
        secondary = _sell_setup()
    else:
        # Neutral / conflicted: whichever side has the higher-magnetism pool wins
        best_above = max((z.magnetism for z in lm.buy_side_pools if not z.is_stale), default=0)
        best_below = max((z.magnetism for z in lm.sell_side_pools if not z.is_stale), default=0)
        if best_above >= best_below:
            primary   = _sell_setup()
            secondary = _buy_setup()
        else:
            primary   = _buy_setup()
            secondary = _sell_setup()

    return {
        "primary":   primary,
        "secondary": secondary,
        "avoid": [
            "News windows: 30 min before/after high-impact release",
            "NY PM 16:00-22:00 UTC (empirically loss-making cell)",
            "Chase entries: price > 0.6 × ATR from H1 EMA20 (pullback gate rejects)",
            "Monday UTC: observation only — signals fire, no MT5 execution until Tuesday",
        ],
    }


def _human_zone(z: LiquidityZone) -> str:
    """Convert a zone_type to human-readable text for the playbook."""
    return {
        WEEKLY_HIGH:   "weekly high",
        WEEKLY_LOW:    "weekly low",
        PREV_DAY_HIGH: "prev-day high",
        PREV_DAY_LOW:  "prev-day low",
        TODAY_HIGH:    "today's high",
        TODAY_LOW:     "today's low",
        EQUAL_HIGHS:   f"equal highs (x{z.touches})",
        EQUAL_LOWS:    f"equal lows (x{z.touches})",
        ASIAN_HIGH:    "Asian high",
        ASIAN_LOW:     "Asian low",
        LONDON_HIGH:   "London high",
        LONDON_LOW:    "London low",
        ROUND_NUMBER:  "round number",
        RANGE_HIGH:    "range top",
        RANGE_LOW:     "range bottom",
    }.get(z.zone_type, z.zone_type)


# ── Human-readable rendering ────────────────────────────────────────────────

def render_zones_for_brief(pools: list[LiquidityZone], side_label: str) -> str:
    """Render one side of the liquidity map for the daily brief."""
    if not pools:
        return f"{side_label}: (none detected)"
    lines = [f"{side_label}"]
    for z in pools:
        stars = "★" * (1 + min(2, z.magnetism // 34))   # 1-3 stars
        stale_mark = " (stale)" if z.is_stale else ""
        lines.append(
            f"  {stars.ljust(3)}  ${z.price:<8.2f} {_human_zone(z):<22}"
            f" magnetism {z.magnetism}{stale_mark}"
        )
    return "\n".join(lines)
