"""
VP Trap Strategy — Previous-Day Volume Profile + Trapped Traders
================================================================

Phase 1 scope: previous-day profile computation ONLY.
  - compute_prev_day_profile() → structured profile of the completed prior day
  - No signal generation, no state machine, no alerts yet

The profile identifies price levels institutions consider structurally
important — Point of Control, Value Area boundaries, High/Low Volume
Nodes — the levels retail stops cluster around and institutions engineer
sweeps of.

Phases 2+ (state machine, trap detection, scoring, alerts, backtest) live
in follow-up commits behind the Phase-1 review gate.

Volume-source hierarchy per brief:
  1. COMEX GC futures (not currently plumbed — will show `not_available`)
  2. Broker "real" volume (Exness MT5 does not expose separate real volume)
  3. Tick volume from OHLCV bars (current reality)

Every profile object declares its `volume_source` so downstream consumers
can penalize signals derived from tick-proxy data. See config
`vp_trap_penalize_tick_volume`.

Architectural notes:
  - Pure functions. No I/O side effects. Persistence lives in vp_trap_state.py
  - Uses only OHLCV data via services.candles / data.candles get_candles()
  - Reuses services.liquidity_map primitives where possible (session pivots,
    equal H/L clustering) to avoid duplicating detection logic
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field, asdict
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

log = logging.getLogger(__name__)


# ── Volume-source enum ──────────────────────────────────────────────────────

VOL_SOURCE_COMEX_GC   = "comex_gc"
VOL_SOURCE_BROKER     = "broker_real"
VOL_SOURCE_TICK_PROXY = "tick_proxy"


# ── Data classes ────────────────────────────────────────────────────────────

@dataclass
class VolumeNode:
    """One row of the volume histogram: a price level and the volume that
    traded within a small band around it."""
    price:    float       # midpoint of the price band
    volume:   float       # total volume that traded in this band
    pct_of_total: float   # volume as fraction of profile total (0-1)


@dataclass
class PrevDayProfile:
    """Complete profile of a single completed prior trading day.

    All price levels are the actual mid-band or extreme prices at the time
    the day closed. `computed_at` freezes the profile at day-boundary; consumers
    downstream should NOT recompute during the current day.
    """
    profile_date:   str            # YYYY-MM-DD (the day that was profiled)
    instrument:     str
    computed_at:    datetime       # when this profile was frozen
    volume_source:  str            # comex_gc | broker_real | tick_proxy

    # Reference levels (the "trap magnet" levels the brief cares about)
    pdh:            float          # Previous Day High
    pdl:            float          # Previous Day Low
    pdo:            float          # Previous Day Open
    pdc:            float          # Previous Day Close
    poc:            float          # Point of Control (highest-volume price)
    vah:            float          # Value Area High (upper 70%-vol boundary)
    val:            float          # Value Area Low  (lower 70%-vol boundary)

    # Derived metrics
    day_range_pts:  float          # PDH - PDL
    close_location_in_range: float # 0.0 = at low, 1.0 = at high
    close_location_in_va:    float # -1.0 = far below VAL, 0 = middle VA, 1.0 = far above VAH
    close_inside_va:         bool  # True if PDC between VAL and VAH
    day_type:                str   # "normal" | "trend" | "double_dist" | "neutral"

    # Distribution shape
    hvn_levels:     list[VolumeNode] = field(default_factory=list)  # top ~5 HVN
    lvn_levels:     list[VolumeNode] = field(default_factory=list)  # top ~5 LVN
    total_volume:   float = 0.0
    bar_count:      int   = 0

    # VWAP (session-anchored)
    vwap:           Optional[float] = None

    # Diagnostics
    value_area_pct: float = 0.70    # what pct threshold was used
    bin_size_pts:   float = 0.0     # price-bin width used for the histogram

    def to_dict(self) -> dict:
        d = asdict(self)
        d["computed_at"] = self.computed_at.isoformat()
        return d


# ── Volume-source detection ─────────────────────────────────────────────────

def _detect_volume_source(candles: list) -> str:
    """
    Decide which of the volume-source tiers the current candle data represents.

    In Phase 1 there is no COMEX GC integration and Exness MT5 does not expose
    a distinct 'real' volume field — so effectively every profile computed
    today is tick_proxy. The function is written as a hook so a future COMEX
    provider can be inserted without changing detection callers.

    Reserved detection rules for later:
      - COMEX GC feed sets `candle.volume_source` attribute per bar
      - Broker real volume marked similarly
      - Anything else → tick_proxy
    """
    if not candles:
        return VOL_SOURCE_TICK_PROXY
    src = getattr(candles[0], "volume_source", None)
    if src == VOL_SOURCE_COMEX_GC:
        return VOL_SOURCE_COMEX_GC
    if src == VOL_SOURCE_BROKER:
        return VOL_SOURCE_BROKER
    return VOL_SOURCE_TICK_PROXY


# ── Day-boundary helpers ────────────────────────────────────────────────────

def _prev_day_bounds_utc(reference: Optional[datetime] = None) -> tuple[datetime, datetime]:
    """Return (start_utc, end_utc) for the most recent COMPLETED trading day.

    Gold trades Sun 22:00 UTC → Fri 21:00 UTC. So on Saturday, Sunday,
    or Monday before 22:00 UTC, "literal yesterday" is not a valid
    trading day. This helper walks backwards from `now` and returns the
    most recent weekday whose UTC calendar day had a full trading session:

      - Reference on Sat / Sun → prev-day = Friday
      - Reference on Mon       → prev-day = Friday (Sun evening is partial)
      - Reference Tue-Fri      → prev-day = literal yesterday (weekday)

    Result windows are half-open: [start_utc, end_utc). Bars whose OPEN
    time equals end_utc are NOT included.
    """
    now = (reference or datetime.now(timezone.utc)).astimezone(timezone.utc)
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)

    # Walk back day-by-day, skipping non-trading days.
    candidate = today_start - timedelta(days=1)
    for _ in range(7):     # safety cap — worst case a full week
        wd = candidate.weekday()   # Mon=0 … Sat=5, Sun=6
        # Skip Saturday (5) and Sunday (6) — no full session.
        # Also skip Monday (0) when we're looking at it from a Monday reference,
        # because Monday's "previous day" is Sunday which is invalid — jump to Fri.
        if wd == 5 or wd == 6:
            candidate -= timedelta(days=1)
            continue
        # Sunday-evening opening (Sun 22:00 → Mon 00:00) only gives ~2 hours of
        # data. If our candidate IS a Sunday, we already skipped above; if it's
        # a Monday and we came from Tue-Fri reference, that's valid.
        break
    return candidate, candidate + timedelta(days=1)


def _filter_bars_to_window(candles: list, start_utc: datetime, end_utc: datetime) -> list:
    """Keep only bars whose OPEN time falls within [start_utc, end_utc)."""
    out = []
    for c in candles:
        t = c.time if c.time.tzinfo else c.time.replace(tzinfo=timezone.utc)
        t = t.astimezone(timezone.utc)
        if start_utc <= t < end_utc:
            out.append(c)
    return out


# ── Volume histogram / Value-Area calculation ───────────────────────────────

def _build_price_bins(bars: list, bin_size_pts: float) -> dict[float, float]:
    """
    Build the volume-by-price histogram from a list of bars.

    Method: for each bar, distribute its total volume UNIFORMLY across the
    price bins its range spans. This is a common TPO-style approximation
    when we don't have tick-level data. It's coarse but statistically stable
    for the value-area calculation which is our real target.

    Returns a dict {bin_midpoint_price: cumulative_volume}.
    """
    if not bars or bin_size_pts <= 0:
        return {}
    hist: dict[float, float] = {}
    for bar in bars:
        lo = min(bar.low,  bar.high)
        hi = max(bar.low,  bar.high)
        n_bins = max(1, int(round((hi - lo) / bin_size_pts)))
        vol_per_bin = float(bar.volume or 0) / n_bins
        if vol_per_bin <= 0:
            continue
        start_price = round(lo / bin_size_pts) * bin_size_pts
        for i in range(n_bins):
            key = round(start_price + i * bin_size_pts, 2)
            hist[key] = hist.get(key, 0.0) + vol_per_bin
    return hist


def _compute_value_area(hist: dict[float, float], target_pct: float = 0.70,
                        poc_price: Optional[float] = None) -> tuple[float, float, float]:
    """
    Standard TPO-style value-area expansion.

    Start at the POC (highest-volume bin). Expand outward: at each step, add
    the neighbouring bin (above or below) with more volume. Continue until
    accumulated volume ≥ target_pct * total_volume.

    Returns (val_price, vah_price, poc_price).
    """
    if not hist:
        return (0.0, 0.0, 0.0)
    total = sum(hist.values())
    if total <= 0:
        return (0.0, 0.0, 0.0)
    if poc_price is None:
        poc_price = max(hist.keys(), key=lambda k: hist[k])

    sorted_bins = sorted(hist.keys())
    poc_idx = sorted_bins.index(poc_price) if poc_price in sorted_bins else \
              min(range(len(sorted_bins)), key=lambda i: abs(sorted_bins[i] - poc_price))

    lo_idx = hi_idx = poc_idx
    accumulated = hist[sorted_bins[poc_idx]]
    target = total * target_pct
    while accumulated < target and (lo_idx > 0 or hi_idx < len(sorted_bins) - 1):
        # Look one below and one above; pick heavier neighbor. If tie or one
        # is exhausted, take the available side.
        below_vol = hist[sorted_bins[lo_idx - 1]] if lo_idx > 0 else -1.0
        above_vol = hist[sorted_bins[hi_idx + 1]] if hi_idx < len(sorted_bins) - 1 else -1.0
        if below_vol < 0 and above_vol < 0:
            break
        if below_vol >= above_vol:
            lo_idx -= 1
            accumulated += hist[sorted_bins[lo_idx]]
        else:
            hi_idx += 1
            accumulated += hist[sorted_bins[hi_idx]]

    val = sorted_bins[lo_idx]
    vah = sorted_bins[hi_idx]
    return (val, vah, poc_price)


def _extract_hvn_lvn(hist: dict[float, float], total: float,
                     hvn_pct_threshold: float = 0.015,
                     lvn_pct_threshold: float = 0.001,
                     max_count: int = 5) -> tuple[list[VolumeNode], list[VolumeNode]]:
    """
    Extract top High-Volume-Nodes (magnets) and Low-Volume-Nodes (voids).

    HVN: bins whose volume is ≥ hvn_pct_threshold of total. Sorted by volume desc.
    LVN: bins whose volume is ≤ lvn_pct_threshold of total. Sorted by volume asc.
    Both capped at max_count.
    """
    if total <= 0:
        return ([], [])
    hvn: list[VolumeNode] = []
    lvn: list[VolumeNode] = []
    for price, vol in hist.items():
        frac = vol / total
        if frac >= hvn_pct_threshold:
            hvn.append(VolumeNode(price=round(price, 2), volume=vol, pct_of_total=frac))
        elif frac <= lvn_pct_threshold:
            lvn.append(VolumeNode(price=round(price, 2), volume=vol, pct_of_total=frac))
    hvn.sort(key=lambda n: -n.volume)
    lvn.sort(key=lambda n:  n.volume)
    return (hvn[:max_count], lvn[:max_count])


# ── VWAP (bar-anchored) ──────────────────────────────────────────────────────

def _compute_prev_day_vwap(bars: list) -> Optional[float]:
    """Standard (H+L+C)/3 × volume VWAP for the prior day's bars."""
    if not bars:
        return None
    num = 0.0
    denom = 0.0
    for b in bars:
        tp = (b.high + b.low + b.close) / 3.0
        v  = float(b.volume or 0)
        num   += tp * v
        denom += v
    if denom <= 0:
        return None
    return round(num / denom, 2)


# ── Day-type classification ─────────────────────────────────────────────────

def _classify_day_type(pdh: float, pdl: float, pdo: float, pdc: float,
                       vah: float, val: float, close_loc: float) -> str:
    """
    Broad day-type label per Market Profile conventions.

    - "trend"       : close within 15% of one extreme AND wide range vs VA
    - "double_dist" : range extends significantly BOTH sides of VA
    - "normal"      : close inside VA, moderate range
    - "neutral"     : close outside VA but no strong directional bias
    """
    day_range = pdh - pdl
    if day_range <= 0:
        return "unknown"
    va_range  = max(0.01, vah - val)
    # Trend day: close near one extreme + wide range
    if (close_loc >= 0.85 or close_loc <= 0.15) and day_range > 2.0 * va_range:
        return "trend"
    # Double-distribution: range >> VA on both sides
    upper_ext = pdh - vah
    lower_ext = val - pdl
    if upper_ext > 0.5 * va_range and lower_ext > 0.5 * va_range and day_range > 2.5 * va_range:
        return "double_dist"
    # Normal: close inside value area
    if val <= pdc <= vah:
        return "normal"
    return "neutral"


# ── Public entry point ──────────────────────────────────────────────────────

def compute_prev_day_profile(
    candles_h1:    list,
    candles_m15:   Optional[list] = None,
    reference_time: Optional[datetime] = None,
    value_area_pct: float = 0.70,
    bin_size_pts:   Optional[float] = None,
    instrument:     str = "XAU/USD",
) -> Optional[PrevDayProfile]:
    """
    Compute the complete previous-day volume profile.

    Args:
        candles_h1:     H1 bars covering AT LEAST the previous day + today so
                        we can slice to the correct window.
        candles_m15:    Optional M15 bars for finer-grained histogram. When
                        provided, uses M15 for the volume distribution (more
                        accurate) and H1 for the range extremes.
        reference_time: For backtest determinism. Defaults to now.
        value_area_pct: 0.70 = standard TPO 70%.
        bin_size_pts:   Price bin width. Auto-selected if None: ~1/50th of
                        the day's range, clamped to [0.5, 10.0].
        instrument:     Instrument label for the returned profile.

    Returns:
        PrevDayProfile, or None if insufficient data.
    """
    if not candles_h1:
        log.debug("[vp_trap] compute_prev_day_profile: no H1 candles")
        return None

    prev_start, prev_end = _prev_day_bounds_utc(reference_time)
    h1_prev = _filter_bars_to_window(candles_h1, prev_start, prev_end)
    if not h1_prev:
        log.debug("[vp_trap] no H1 bars in prev-day window [%s, %s)", prev_start, prev_end)
        return None

    # Prefer M15 for histogram if we have coverage of the whole prev day
    hist_source = h1_prev
    if candles_m15:
        m15_prev = _filter_bars_to_window(candles_m15, prev_start, prev_end)
        # Need at least ~half the day (~48 M15 bars) to prefer M15 over H1
        if len(m15_prev) >= 48:
            hist_source = m15_prev

    # Basic OHLC from H1 (extremes are H1-derived — same as M15's max/min)
    pdo = round(h1_prev[0].open, 2)
    pdc = round(h1_prev[-1].close, 2)
    pdh = round(max(b.high for b in h1_prev), 2)
    pdl = round(min(b.low  for b in h1_prev), 2)
    day_range = pdh - pdl
    if day_range <= 0:
        log.warning("[vp_trap] degenerate day: range=0 on %s", prev_start.date())
        return None

    # Auto bin size
    if bin_size_pts is None:
        bin_size_pts = max(0.5, min(10.0, day_range / 50.0))
    bin_size_pts = round(bin_size_pts, 2)

    # Histogram
    hist = _build_price_bins(hist_source, bin_size_pts)
    total_vol = sum(hist.values())
    if total_vol <= 0:
        log.warning("[vp_trap] histogram has zero volume for %s", prev_start.date())
        return None

    # POC + Value Area
    poc = max(hist.keys(), key=lambda k: hist[k])
    val, vah, poc = _compute_value_area(hist, target_pct=value_area_pct, poc_price=poc)

    # HVN / LVN
    hvn, lvn = _extract_hvn_lvn(hist, total_vol)

    # VWAP (from bars, not histogram — uses typical price × volume weighting)
    vwap = _compute_prev_day_vwap(hist_source)

    # Close-location metrics
    close_loc_range = (pdc - pdl) / day_range if day_range > 0 else 0.5
    va_span         = max(0.01, vah - val)
    close_loc_va    = ((pdc - val) / va_span) if va_span > 0 else 0.0
    # Normalize -1..1 with 0 at VA midpoint
    va_mid = (vah + val) / 2.0
    close_loc_va_norm = (pdc - va_mid) / (va_span / 2.0) if va_span > 0 else 0.0
    close_inside_va = (val - 0.5) <= pdc <= (vah + 0.5)

    day_type = _classify_day_type(pdh, pdl, pdo, pdc, vah, val, close_loc_range)

    volume_source = _detect_volume_source(hist_source)

    return PrevDayProfile(
        profile_date  = prev_start.strftime("%Y-%m-%d"),
        instrument    = instrument,
        computed_at   = datetime.now(timezone.utc),
        volume_source = volume_source,
        pdh=pdh, pdl=pdl, pdo=pdo, pdc=pdc,
        poc=round(poc, 2), vah=round(vah, 2), val=round(val, 2),
        day_range_pts=round(day_range, 2),
        close_location_in_range=round(close_loc_range, 3),
        close_location_in_va=round(max(-1.5, min(1.5, close_loc_va_norm)), 3),
        close_inside_va=close_inside_va,
        day_type=day_type,
        hvn_levels=hvn,
        lvn_levels=lvn,
        total_volume=round(total_vol, 2),
        bar_count=len(hist_source),
        vwap=vwap,
        value_area_pct=value_area_pct,
        bin_size_pts=bin_size_pts,
    )


# ── Public convenience (used by router / hook points) ──────────────────────

def compute_current_prev_day_profile(
    reference_time: Optional[datetime] = None,
    value_area_pct: float = 0.70,
) -> Optional[PrevDayProfile]:
    """Fetch fresh candles and compute the previous-day profile in one call.

    Convenience wrapper so callers don't need to plumb the data layer. Uses
    the same get_candles() path the rest of the strategist uses; degrades to
    None on any provider failure.
    """
    try:
        from data.candles import get_candles
        h1  = get_candles(interval="H1",  limit=200, pair="xauusd")
        m15 = get_candles(interval="M15", limit=200, pair="xauusd")
        return compute_prev_day_profile(
            candles_h1=h1.candles if h1 else [],
            candles_m15=m15.candles if m15 else None,
            reference_time=reference_time,
            value_area_pct=value_area_pct,
        )
    except Exception as exc:
        log.warning("[vp_trap] compute_current_prev_day_profile failed: %s", exc)
        return None
