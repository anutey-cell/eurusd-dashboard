"""
VP Trap Zone State Machine — Phase 2
====================================

Computes the current state of each candidate trap zone by walking forward
through the M15 bars since the profile was frozen at prev-day close.

Zones progress through:

  LEVEL_DETECTED   (initial state when zone is created from profile)
        ↓  price wick or close crosses the level
  BREAKOUT_SEEN    (either side has been breached)
        ↓  price returns through the level with body-close
  TRAP_ARMED       (failed acceptance confirmed)
        ↓  counter-displacement of ≥ min_displacement_pts
  WAITING_RETEST   (armed but hasn't been revisited)
        ↓  price returns to within retest tolerance of the level
  RETEST_ACTIVE    (in the zone right now)
        ↓  rejection candle in signal direction (M15 close beyond entry side)
  TRIGGERED        (Phase 3 will emit signal from this state)

Failure paths:
  INVALIDATED  — acceptance confirmed (multiple closes + time beyond level)
  EXPIRED       — beyond expires_at OR retested max_retests without rejection

Design invariant: state is a PURE FUNCTION of (profile + M15 bars).
No accumulated in-memory state. Restartable, deterministic, backtestable.
Persistence to VpTrapZone is upsert-only (final state per zone_id).
"""
from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass, field, asdict
from datetime import datetime, timedelta, timezone
from typing import Optional

log = logging.getLogger(__name__)


# ── State enum ──────────────────────────────────────────────────────────────

STATE_LEVEL_DETECTED  = "LEVEL_DETECTED"
STATE_BREAKOUT_SEEN   = "BREAKOUT_SEEN"
STATE_TRAP_ARMED      = "TRAP_ARMED"
STATE_WAITING_RETEST  = "WAITING_RETEST"
STATE_RETEST_ACTIVE   = "RETEST_ACTIVE"
STATE_TRIGGERED       = "TRIGGERED"
STATE_INVALIDATED     = "INVALIDATED"
STATE_EXPIRED         = "EXPIRED"

# Convenience: order of progression — used for state-monotonicity checks
_STATE_ORDER = {
    STATE_LEVEL_DETECTED: 0,
    STATE_BREAKOUT_SEEN:  1,
    STATE_TRAP_ARMED:     2,
    STATE_WAITING_RETEST: 3,
    STATE_RETEST_ACTIVE:  4,
    STATE_TRIGGERED:      5,
    STATE_INVALIDATED:   -1,   # terminal
    STATE_EXPIRED:       -1,   # terminal
}


# ── Candidate zone ──────────────────────────────────────────────────────────

# The 4 headline zones we derive from every profile. Each has a side that
# determines whether a trap here would produce a SELL setup (upper zones,
# trapped buyers) or a BUY setup (lower zones, trapped sellers).
UPPER_ZONE_TYPES = ("PDH", "VAH")   # SELL setups when trapped
LOWER_ZONE_TYPES = ("PDL", "VAL")   # BUY setups when trapped


@dataclass
class TrapZone:
    """Runtime representation of a candidate trap zone. Serialisable."""
    zone_id:          str
    profile_date:     str            # YYYY-MM-DD
    level_type:       str            # PDH | PDL | VAH | VAL
    level_side:       str            # SELL (upper) | BUY (lower)
    reference_price:  float

    # Evolving state (computed each scan)
    state:            str  = STATE_LEVEL_DETECTED
    state_reason:     str  = ""

    breakout_time:    Optional[datetime] = None
    breakout_extreme: Optional[float]    = None
    reclaim_time:     Optional[datetime] = None
    reclaim_price:    Optional[float]    = None
    displacement_pts: Optional[float]    = None
    retest_count:     int  = 0
    last_touched_at:  Optional[datetime] = None
    expires_at:       Optional[datetime] = None
    volume_source:    str  = "tick_proxy"

    def to_dict(self) -> dict:
        d = asdict(self)
        for k in ("breakout_time", "reclaim_time", "last_touched_at", "expires_at"):
            v = getattr(self, k)
            d[k] = v.isoformat() if v else None
        return d


# ── Helpers ─────────────────────────────────────────────────────────────────

def _zone_id_hash(profile_date: str, level_type: str, level_side: str,
                  instrument: str = "XAUUSD") -> str:
    """Deterministic 16-char id for a zone. Same day + level + side always
    produces the same id — ensures upsert stability across scan cycles."""
    raw = f"{instrument}|{profile_date}|{level_type}|{level_side}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def _bar_time(c) -> datetime:
    t = c.time if c.time.tzinfo else c.time.replace(tzinfo=timezone.utc)
    return t.astimezone(timezone.utc)


# ── Zone creation from profile ──────────────────────────────────────────────

def zones_from_profile(profile, expiry_hours: int = 48) -> list[TrapZone]:
    """Create the 4 candidate zones (PDH, PDL, VAH, VAL) from a profile.

    Each zone starts in LEVEL_DETECTED. Downstream scan_zone() will advance
    the state by walking recent M15 bars.
    """
    if profile is None:
        return []
    computed_at = profile.computed_at if hasattr(profile, 'computed_at') else datetime.now(timezone.utc)
    if computed_at.tzinfo is None:
        computed_at = computed_at.replace(tzinfo=timezone.utc)
    expires_at = computed_at + timedelta(hours=expiry_hours)

    zones: list[TrapZone] = []
    for lvl in UPPER_ZONE_TYPES:
        price = getattr(profile, lvl.lower())
        zones.append(TrapZone(
            zone_id=_zone_id_hash(profile.profile_date, lvl, "SELL"),
            profile_date=profile.profile_date,
            level_type=lvl, level_side="SELL",
            reference_price=price, expires_at=expires_at,
            volume_source=profile.volume_source,
        ))
    for lvl in LOWER_ZONE_TYPES:
        price = getattr(profile, lvl.lower())
        zones.append(TrapZone(
            zone_id=_zone_id_hash(profile.profile_date, lvl, "BUY"),
            profile_date=profile.profile_date,
            level_type=lvl, level_side="BUY",
            reference_price=price, expires_at=expires_at,
            volume_source=profile.volume_source,
        ))
    return zones


# ── Detection primitives ────────────────────────────────────────────────────

def _find_breakout(bars: list, level: float, side: str,
                   wick_only_ok: bool = True) -> tuple[Optional[int], Optional[float]]:
    """Find the FIRST bar that broke the level.

    For a SELL zone (upper level): look for high > level.
    For a BUY zone (lower level): look for low < level.
    Returns (bar_index, extreme_price) or (None, None).
    """
    for i, c in enumerate(bars):
        if side == "SELL":
            if c.high > level:
                extreme = c.high
                return (i, extreme)
        else:
            if c.low < level:
                extreme = c.low
                return (i, extreme)
    return (None, None)


def _find_reclaim(bars: list, level: float, side: str,
                  after_idx: int) -> tuple[Optional[int], Optional[float]]:
    """After breakout, find the FIRST bar that CLOSED back through the level.

    For a SELL zone: first close < level after after_idx.
    For a BUY zone: first close > level after after_idx.
    Returns (bar_index, close_price) or (None, None).
    """
    for i in range(after_idx + 1, len(bars)):
        c = bars[i]
        if side == "SELL" and c.close < level:
            return (i, c.close)
        if side == "BUY"  and c.close > level:
            return (i, c.close)
    return (None, None)


def _measure_displacement(bars: list, from_idx: int, side: str) -> float:
    """Post-reclaim, max magnitude of counter-move in signal direction.

    SELL zone: how far DOWN did price go from reclaim close?
    BUY zone:  how far UP did price go from reclaim close?
    """
    if from_idx >= len(bars) - 1:
        return 0.0
    reclaim_close = bars[from_idx].close
    max_disp = 0.0
    for c in bars[from_idx + 1:]:
        if side == "SELL":
            disp = reclaim_close - c.low
        else:
            disp = c.high - reclaim_close
        if disp > max_disp:
            max_disp = disp
    return round(max_disp, 2)


def _is_retest(bars: list, level: float, side: str, from_idx: int,
               tolerance_pts: float) -> tuple[Optional[int], int]:
    """After TRAP_ARMED, has price returned to within tolerance of the level?

    Returns (last_touch_bar_idx, touch_count) — number of distinct retest
    events (consecutive touches count as one). Returns (None, 0) if no touch.
    """
    if from_idx >= len(bars):
        return (None, 0)
    touches = 0
    last_touch = None
    in_touch = False
    lo_band = level - tolerance_pts
    hi_band = level + tolerance_pts
    for i in range(from_idx, len(bars)):
        c = bars[i]
        touched = (c.low <= hi_band and c.high >= lo_band)
        if touched:
            if not in_touch:
                touches += 1
                in_touch = True
            last_touch = i
        else:
            in_touch = False
    return (last_touch, touches)


def _price_in_zone(current_price: float, level: float, tolerance_pts: float) -> bool:
    return abs(current_price - level) <= tolerance_pts


def _acceptance_seen(bars: list, level: float, side: str, from_idx: int,
                     min_closes_outside: int = 3, min_bars_beyond: int = 6) -> bool:
    """Detect ACCEPTANCE — the breakout is real, not a trap.

    Rule: after the initial breakout, if `min_closes_outside` consecutive
    or majority closes remain beyond the level AND `min_bars_beyond` bars
    have been spent beyond the level with no reclaim → acceptance.
    """
    if from_idx >= len(bars):
        return False
    closes_beyond = 0
    beyond_bars = 0
    for c in bars[from_idx:]:
        beyond = (c.close > level) if side == "SELL" else (c.close < level)
        if beyond:
            closes_beyond += 1
            beyond_bars += 1
        else:
            # broke back — acceptance not yet
            closes_beyond = 0
    return closes_beyond >= min_closes_outside and beyond_bars >= min_bars_beyond


# ── Main state advancer ────────────────────────────────────────────────────

def scan_zone(zone: TrapZone, bars_since_profile: list,
              min_displacement_pts: float = 5.0,
              retest_tolerance_pts:  float = 3.0,
              max_retests:           int   = 3,
              now_utc:               Optional[datetime] = None) -> TrapZone:
    """Compute the current state of a single zone by walking bars_since_profile.

    Bars are M15 bars (or H1 if M15 unavailable) with time >= profile.computed_at.
    Deterministic and pure — running twice yields the same zone state.
    """
    if now_utc is None:
        now_utc = datetime.now(timezone.utc)

    # Expiry check first — if past expires_at, transition to EXPIRED regardless
    if zone.expires_at and now_utc > zone.expires_at:
        zone.state = STATE_EXPIRED
        zone.state_reason = f"beyond expiry ({zone.expires_at.isoformat()})"
        return zone

    if not bars_since_profile:
        zone.state = STATE_LEVEL_DETECTED
        zone.state_reason = "no bars since profile"
        return zone

    # Step 1: breakout detection
    b_idx, b_extreme = _find_breakout(bars_since_profile,
                                     zone.reference_price, zone.level_side)
    if b_idx is None:
        zone.state = STATE_LEVEL_DETECTED
        zone.state_reason = "level not yet tested"
        return zone

    zone.breakout_time    = _bar_time(bars_since_profile[b_idx])
    zone.breakout_extreme = round(b_extreme, 2)

    # Step 2: acceptance check — if price accepted beyond level, INVALIDATE
    if _acceptance_seen(bars_since_profile, zone.reference_price,
                         zone.level_side, b_idx):
        zone.state = STATE_INVALIDATED
        zone.state_reason = "acceptance beyond level — not a trap"
        return zone

    # Step 3: reclaim
    r_idx, r_close = _find_reclaim(bars_since_profile, zone.reference_price,
                                   zone.level_side, b_idx)
    if r_idx is None:
        zone.state = STATE_BREAKOUT_SEEN
        zone.state_reason = "breakout not yet reclaimed"
        return zone

    zone.reclaim_time  = _bar_time(bars_since_profile[r_idx])
    zone.reclaim_price = round(r_close, 2)

    # Step 4: displacement
    displacement = _measure_displacement(bars_since_profile, r_idx, zone.level_side)
    zone.displacement_pts = displacement
    if displacement < min_displacement_pts:
        zone.state = STATE_BREAKOUT_SEEN
        zone.state_reason = (
            f"reclaimed but displacement only {displacement:.1f}pt "
            f"(need >= {min_displacement_pts})"
        )
        return zone

    # Step 5: TRAP_ARMED. Look for retest.
    last_touch_idx, touches = _is_retest(bars_since_profile,
                                         zone.reference_price, zone.level_side,
                                         r_idx + 1, retest_tolerance_pts)
    zone.retest_count = touches
    if last_touch_idx is not None:
        zone.last_touched_at = _bar_time(bars_since_profile[last_touch_idx])

    if touches == 0:
        zone.state = STATE_WAITING_RETEST
        zone.state_reason = f"armed, displacement {displacement:.1f}pt, awaiting retest"
        return zone

    if touches > max_retests:
        zone.state = STATE_EXPIRED
        zone.state_reason = f"retested {touches}x (> max {max_retests}) — stale"
        return zone

    # Step 6: is the LAST bar currently in the retest zone?
    last_bar = bars_since_profile[-1]
    if _price_in_zone(last_bar.close, zone.reference_price, retest_tolerance_pts):
        zone.state = STATE_RETEST_ACTIVE
        zone.state_reason = (
            f"in retest zone: close {last_bar.close:.2f} vs level "
            f"{zone.reference_price:.2f} ± {retest_tolerance_pts:.1f}"
        )
        return zone

    # Step 7: post-retest rejection candle in signal direction?
    # SELL: last bar closed below entry area AFTER touching level
    # BUY:  last bar closed above entry area AFTER touching level
    post_retest_bars = bars_since_profile[last_touch_idx:]
    if len(post_retest_bars) >= 2:
        # take the last 2 bars — the touch bar and the one after
        rejection_confirmed = False
        touch_bar = bars_since_profile[last_touch_idx]
        for c in bars_since_profile[last_touch_idx + 1:]:
            if zone.level_side == "SELL":
                # bearish close AND close < touch_bar.low (moving away in signal dir)
                if c.close < c.open and c.close < touch_bar.low:
                    rejection_confirmed = True
                    break
            else:
                if c.close > c.open and c.close > touch_bar.high:
                    rejection_confirmed = True
                    break
        if rejection_confirmed:
            zone.state = STATE_TRIGGERED
            zone.state_reason = (
                f"retested + rejected. Direction {zone.level_side} against "
                f"level {zone.reference_price:.2f}"
            )
            return zone

    # Retested but no rejection yet
    zone.state = STATE_WAITING_RETEST
    zone.state_reason = (
        f"retested {touches}x, awaiting rejection candle"
    )
    return zone


# ── Persistence helpers ─────────────────────────────────────────────────────

def upsert_zone(db, zone: TrapZone, instrument: str = "XAU/USD") -> None:
    """Insert or update a VpTrapZone row keyed by zone_id."""
    from db_models import VpTrapZone as ZM
    row = db.query(ZM).filter(ZM.zone_id == zone.zone_id).one_or_none()
    if row is None:
        row = ZM(
            instrument=instrument,
            zone_id=zone.zone_id,
            profile_date=zone.profile_date,
            level_type=zone.level_type,
            level_side=zone.level_side,
            reference_price=zone.reference_price,
            expires_at=zone.expires_at or (datetime.now(timezone.utc) + timedelta(hours=48)),
            volume_source=zone.volume_source,
        )
        db.add(row)
    row.state             = zone.state
    row.state_reason      = zone.state_reason[:255]
    row.breakout_time     = zone.breakout_time
    row.breakout_extreme  = zone.breakout_extreme
    row.reclaim_time      = zone.reclaim_time
    row.reclaim_price     = zone.reclaim_price
    row.displacement_pts  = zone.displacement_pts
    row.retest_count      = zone.retest_count
    row.last_touched_at   = zone.last_touched_at
    row.volume_source     = zone.volume_source


def load_active_zones(db, instrument: str = "XAU/USD",
                      exclude_terminal: bool = True) -> list[dict]:
    """Read zones from DB. If exclude_terminal, skip EXPIRED and INVALIDATED."""
    from db_models import VpTrapZone as ZM
    q = db.query(ZM).filter(ZM.instrument == instrument)
    if exclude_terminal:
        q = q.filter(~ZM.state.in_([STATE_EXPIRED, STATE_INVALIDATED]))
    rows = q.order_by(ZM.updated_at.desc()).limit(50).all()
    out = []
    for r in rows:
        out.append({
            "zone_id":         r.zone_id,
            "profile_date":    r.profile_date,
            "level_type":      r.level_type,
            "level_side":      r.level_side,
            "reference_price": r.reference_price,
            "state":           r.state,
            "state_reason":    r.state_reason,
            "breakout_time":   r.breakout_time.isoformat() if r.breakout_time else None,
            "breakout_extreme": r.breakout_extreme,
            "reclaim_time":    r.reclaim_time.isoformat() if r.reclaim_time else None,
            "reclaim_price":   r.reclaim_price,
            "displacement_pts": r.displacement_pts,
            "retest_count":    r.retest_count,
            "last_touched_at": r.last_touched_at.isoformat() if r.last_touched_at else None,
            "expires_at":      r.expires_at.isoformat() if r.expires_at else None,
            "volume_source":   r.volume_source,
            "updated_at":      r.updated_at.isoformat() if r.updated_at else None,
        })
    return out
