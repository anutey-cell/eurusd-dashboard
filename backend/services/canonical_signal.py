"""
Canonical Signal — Single-Source-of-Truth Shape
================================================

Every downstream consumer (Telegram router, dashboard v2, audit logs)
reads THIS shape. Existing engines don't know about it — adapters (in
services/signal_adapters/) translate each engine's native output into
a CanonicalSignal.

Design principles:
  1. Immutable value type (frozen dataclass) — never mutated after
     construction. State transitions create a NEW CanonicalSignal
     that references the previous fingerprint for audit.
  2. All timestamps UTC. EAT conversion happens at render time only.
  3. Prices always float; strategies use `_price_bucket` helper for
     fingerprint bucketing (5-point buckets prevent minor drift from
     creating a new signal).
  4. Optional fields default to None — templates render "—" for them.
  5. Fingerprint is deterministic — same setup twice always yields
     same fingerprint. Persistence layer uses this for dedupe.

See P0 assessment (2026-07-23) for architecture rationale.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Optional

# ── State machine constants ─────────────────────────────────────────────────

# Pre-entry lifecycle
STATE_DETECTED   = "DETECTED"     # newly created; below monitoring threshold
STATE_MONITORING = "MONITORING"   # score ≥ monitoring_threshold, one mandatory condition outstanding
STATE_ARMED      = "ARMED"        # all mandatory conditions met, score ≥ actionable_threshold

# Entry lifecycle
STATE_TRIGGERED  = "TRIGGERED"    # entry conditions objectively met
STATE_ACTIVE     = "ACTIVE"       # trade in flight (or theoretical trade tracked)

# Post-entry
STATE_TP1_HIT    = "TP1_HIT"
STATE_TP2_HIT    = "TP2_HIT"
STATE_TP3_HIT    = "TP3_HIT"
STATE_BREAKEVEN  = "BREAKEVEN"
STATE_TRAILING   = "TRAILING"

# Terminal states
STATE_STOPPED     = "STOPPED"
STATE_INVALIDATED = "INVALIDATED"
STATE_EXPIRED     = "EXPIRED"
STATE_CLOSED      = "CLOSED"

TERMINAL_STATES = {STATE_STOPPED, STATE_INVALIDATED, STATE_EXPIRED, STATE_CLOSED}
PRE_ENTRY_STATES = {STATE_DETECTED, STATE_MONITORING, STATE_ARMED}
POST_ENTRY_STATES = {
    STATE_TRIGGERED, STATE_ACTIVE, STATE_TP1_HIT, STATE_TP2_HIT, STATE_TP3_HIT,
    STATE_BREAKEVEN, STATE_TRAILING,
}
ALL_STATES = PRE_ENTRY_STATES | POST_ENTRY_STATES | TERMINAL_STATES

# Direction constants
DIRECTION_BUY  = "BUY"
DIRECTION_SELL = "SELL"
DIRECTION_NONE = "NONE"


# ── Strategy identity ───────────────────────────────────────────────────────

STRATEGY_MANDATE     = "mandate"
STRATEGY_VP_TRAP     = "vp_trap"
STRATEGY_MOMENTUM    = "momentum"
STRATEGY_KZ_MAGNET   = "kz_magnet"
STRATEGY_AGGREGATED  = "aggregated"

# Short prefix used in human-readable signal IDs
STRATEGY_PREFIX = {
    STRATEGY_MANDATE:    "MDT",
    STRATEGY_VP_TRAP:    "VPT",
    STRATEGY_MOMENTUM:   "MOM",
    STRATEGY_KZ_MAGNET:  "KZM",
    STRATEGY_AGGREGATED: "AGG",
}


# ── The canonical shape ─────────────────────────────────────────────────────

@dataclass(frozen=True)
class CanonicalSignal:
    """Single source of truth. Immutable — state transitions create new
    instances that reference the prior via `previous_state`."""

    # Identity
    signal_id:      str          # human-readable: MDT-XAU-20260723-001
    fingerprint:    str          # 16-char SHA (dedupe key)
    strategy_id:    str          # one of STRATEGY_*
    strategy_name:  str          # display name
    instrument:     str          # "XAUUSD"

    # Direction + confidence
    direction:      str          # BUY | SELL | NONE
    confidence:     int          # 0-100

    # Entry / stop / invalidation
    entry_zone_low:   float
    entry_zone_high:  float
    stop_loss:        float
    current_stop:     float                    # may be moved to BE / trailing
    invalidation:     str                      # human string

    # Targets (each optional; templates render "—" when missing)
    tp1:            Optional[float] = None
    tp2:            Optional[float] = None
    tp3:            Optional[float] = None
    tp1_label:      Optional[str]   = None
    tp2_label:      Optional[str]   = None
    tp3_label:      Optional[str]   = None
    rr_tp1:         Optional[float] = None
    rr_tp2:         Optional[float] = None
    rr_tp3:         Optional[float] = None

    # Chase gate
    no_chase_price: Optional[float] = None

    # Setup context
    session:            str = ""
    market_regime:      Optional[str] = None
    htf_bias:           Optional[str] = None
    trap_side:          Optional[str] = None
    reference_zone_low:  Optional[float] = None
    reference_zone_high: Optional[float] = None

    # Rationale
    conditions_met:     tuple[str, ...] = ()   # tuple for hashability
    conditions_missing: tuple[str, ...] = ()
    rationale:          str = ""
    data_source:        str = "tick_proxy"
    confluence:         tuple[dict, ...] = ()  # ({"strategy_name":..., "confidence":...},)

    # State machine
    state:              str = STATE_DETECTED
    previous_state:     Optional[str] = None

    # Timestamps (UTC)
    created_at:         datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    valid_until:        Optional[datetime] = None
    triggered_at:       Optional[datetime] = None
    closed_at:          Optional[datetime] = None

    # Execution
    is_broker_confirmed: bool = False
    r_realized:         Optional[float] = None
    partial_taken:      bool = False

    # ── Post-init validation ────────────────────────────────────────────
    def __post_init__(self):
        if self.state not in ALL_STATES:
            raise ValueError(f"invalid state {self.state!r}")
        if self.direction not in (DIRECTION_BUY, DIRECTION_SELL, DIRECTION_NONE):
            raise ValueError(f"invalid direction {self.direction!r}")
        if self.strategy_id not in STRATEGY_PREFIX and self.strategy_id != STRATEGY_AGGREGATED:
            raise ValueError(f"unknown strategy_id {self.strategy_id!r}")
        if not (0 <= self.confidence <= 100):
            raise ValueError(f"confidence out of range: {self.confidence}")
        # Entry zone sanity
        if self.entry_zone_low > self.entry_zone_high:
            raise ValueError(
                f"entry_zone_low {self.entry_zone_low} > entry_zone_high {self.entry_zone_high}"
            )
        # Timestamps must be timezone-aware
        if self.created_at.tzinfo is None:
            raise ValueError("created_at must be tz-aware (UTC)")

    # ── Convenience projections ─────────────────────────────────────────

    def entry_midpoint(self) -> float:
        return round((self.entry_zone_low + self.entry_zone_high) / 2, 2)

    def risk_points(self) -> float:
        return round(abs(self.entry_midpoint() - self.current_stop), 2)

    def is_terminal(self) -> bool:
        return self.state in TERMINAL_STATES

    def is_pre_entry(self) -> bool:
        return self.state in PRE_ENTRY_STATES

    def to_dict(self) -> dict:
        d = asdict(self)
        # Serialize timestamps
        for k in ("created_at", "valid_until", "triggered_at", "closed_at"):
            v = d.get(k)
            if isinstance(v, datetime):
                d[k] = v.isoformat()
        # Tuples → lists for JSON compat
        d["conditions_met"] = list(self.conditions_met)
        d["conditions_missing"] = list(self.conditions_missing)
        d["confluence"] = [dict(c) for c in self.confluence]
        return d


# ── Fingerprint helpers ─────────────────────────────────────────────────────

def _price_bucket(price: float, bucket_pts: float = 5.0) -> int:
    """Floor-bucket the price. Any two prices in [N*bucket, (N+1)*bucket)
    map to the same bucket → same fingerprint. Deterministic at boundaries
    (unlike round(), which flips banker's-rounding style)."""
    if price is None or bucket_pts <= 0:
        return 0
    return int((price // bucket_pts) * bucket_pts)


def signal_fingerprint(
    *,
    instrument:      str,
    direction:       str,
    strategy_id:     str,
    entry_zone_low:  float,
    entry_zone_high: float,
    stop_loss:       float,
    session:         str,
    created_at:      datetime,
    bucket_pts:      float = 5.0,
) -> str:
    """Deterministic signal identity for dedupe.

    Two setups with same instrument/direction/strategy/entry-zone (in 5-pt
    buckets)/SL (in 5-pt buckets)/session/date yield the SAME fingerprint.
    Minor price drift (<= 5pt) does NOT create a new signal.
    """
    date_bucket = created_at.strftime("%Y%m%d")
    parts = [
        instrument,
        direction,
        strategy_id,
        str(_price_bucket(entry_zone_low,  bucket_pts)),
        str(_price_bucket(entry_zone_high, bucket_pts)),
        str(_price_bucket(stop_loss,       bucket_pts)),
        session or "unknown",
        date_bucket,
    ]
    raw = "|".join(parts)
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def message_fingerprint(
    *,
    signal_id:    str,
    new_state:    str,
    key_prices:   dict[str, float] = None,
    bucket_pts:   float = 5.0,
) -> str:
    """Idempotency key for a specific state-change notification.

    Same (signal, transition, prices-in-buckets) → same fingerprint.
    Persistence layer rejects re-sends with existing fingerprint.
    """
    key_prices = key_prices or {}
    parts = [signal_id, new_state]
    for k in sorted(key_prices.keys()):
        parts.append(f"{k}={_price_bucket(key_prices[k], bucket_pts)}")
    raw = "|".join(parts)
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


# ── Signal-ID generator ─────────────────────────────────────────────────────

def make_signal_id(strategy_id: str, sequence: int, instrument: str = "XAU",
                    at: Optional[datetime] = None) -> str:
    """Human-readable signal ID: MDT-XAU-20260723-001

    Sequence numbering is caller's responsibility (registry allocates it).
    """
    if at is None:
        at = datetime.now(timezone.utc)
    prefix = STRATEGY_PREFIX.get(strategy_id, "SIG")
    return f"{prefix}-{instrument}-{at.strftime('%Y%m%d')}-{sequence:03d}"


# ── Transition rules ────────────────────────────────────────────────────────

# Every permitted transition. Attempts outside this graph raise in the registry.
# Some transitions ARE permitted but SILENT (no message emitted) — that's the
# router's decision, not this module's.

PERMITTED_TRANSITIONS: dict[str, set[str]] = {
    STATE_DETECTED:   {STATE_MONITORING, STATE_ARMED, STATE_INVALIDATED, STATE_EXPIRED},
    STATE_MONITORING: {STATE_ARMED, STATE_INVALIDATED, STATE_EXPIRED},
    STATE_ARMED:      {STATE_TRIGGERED, STATE_INVALIDATED, STATE_EXPIRED},
    STATE_TRIGGERED:  {STATE_ACTIVE, STATE_STOPPED, STATE_INVALIDATED},
    STATE_ACTIVE:     {STATE_TP1_HIT, STATE_TP2_HIT, STATE_TP3_HIT,
                       STATE_TRAILING, STATE_BREAKEVEN, STATE_STOPPED},
    STATE_TP1_HIT:    {STATE_BREAKEVEN, STATE_TP2_HIT, STATE_TP3_HIT,
                       STATE_TRAILING, STATE_STOPPED, STATE_CLOSED},
    STATE_TP2_HIT:    {STATE_TP3_HIT, STATE_TRAILING, STATE_STOPPED, STATE_CLOSED},
    STATE_TP3_HIT:    {STATE_CLOSED},
    STATE_BREAKEVEN:  {STATE_TP2_HIT, STATE_TP3_HIT, STATE_TRAILING, STATE_STOPPED, STATE_CLOSED},
    STATE_TRAILING:   {STATE_TP2_HIT, STATE_TP3_HIT, STATE_STOPPED, STATE_CLOSED},
    # Terminals accept no outgoing transitions
    STATE_STOPPED:     set(),
    STATE_INVALIDATED: set(),
    STATE_EXPIRED:     set(),
    STATE_CLOSED:      set(),
}


def is_valid_transition(from_state: str, to_state: str) -> bool:
    if from_state not in ALL_STATES or to_state not in ALL_STATES:
        return False
    return to_state in PERMITTED_TRANSITIONS.get(from_state, set())


# ── Message-type-per-transition mapping ─────────────────────────────────────

# Maps each state transition to the message-type-key the template service
# will render. Silent transitions map to None. Router uses this to decide
# whether Telegram should be notified. Some transitions are conditional
# (e.g. `entry_triggered` fires when ARMED→TRIGGERED but stays silent if the
# signal was aggregated into a high-confluence alert).

TRANSITION_MESSAGE: dict[tuple[str, str], Optional[str]] = {
    # Pre-entry
    (STATE_DETECTED,   STATE_MONITORING):  "monitoring",
    (STATE_DETECTED,   STATE_ARMED):       "actionable",    # skipped monitoring (score jumped)
    (STATE_MONITORING, STATE_ARMED):       "actionable",
    (STATE_ARMED,      STATE_TRIGGERED):   "entry_triggered",

    # Post-entry lifecycle
    (STATE_TRIGGERED,  STATE_ACTIVE):      None,            # silent — internal marker
    (STATE_ACTIVE,     STATE_TP1_HIT):     "tp1_hit",
    (STATE_TP1_HIT,    STATE_BREAKEVEN):   "breakeven",
    (STATE_ACTIVE,     STATE_TP2_HIT):     "tp2_hit",       # skipped TP1 (edge)
    (STATE_TP1_HIT,    STATE_TP2_HIT):     "tp2_hit",
    (STATE_BREAKEVEN,  STATE_TP2_HIT):     "tp2_hit",
    (STATE_ACTIVE,     STATE_TP3_HIT):     "final_target",  # edge
    (STATE_TP1_HIT,    STATE_TP3_HIT):     "final_target",
    (STATE_TP2_HIT,    STATE_TP3_HIT):     "final_target",
    (STATE_TP3_HIT,    STATE_CLOSED):      None,            # silent — final_target already sent
    (STATE_ACTIVE,     STATE_TRAILING):    "trailing",
    (STATE_TP1_HIT,    STATE_TRAILING):    "trailing",
    (STATE_BREAKEVEN,  STATE_TRAILING):    "trailing",
    (STATE_TP2_HIT,    STATE_TRAILING):    "trailing",

    # Failure paths
    (STATE_ACTIVE,     STATE_STOPPED):     "stop_hit",
    (STATE_TP1_HIT,    STATE_STOPPED):     "stop_hit",
    (STATE_BREAKEVEN,  STATE_STOPPED):     "stop_hit",
    (STATE_TP2_HIT,    STATE_STOPPED):     "stop_hit",
    (STATE_TRAILING,   STATE_STOPPED):     "stop_hit",
    (STATE_TRIGGERED,  STATE_STOPPED):     "stop_hit",

    (STATE_DETECTED,   STATE_INVALIDATED): None,            # never surfaced
    (STATE_MONITORING, STATE_INVALIDATED): "invalidated",
    (STATE_ARMED,      STATE_INVALIDATED): "invalidated",
    (STATE_TRIGGERED,  STATE_INVALIDATED): "invalidated",

    (STATE_DETECTED,   STATE_EXPIRED):     None,            # never surfaced
    (STATE_MONITORING, STATE_EXPIRED):     None,            # never crossed actionable threshold
    (STATE_ARMED,      STATE_EXPIRED):     "expired",
}


def message_type_for(from_state: str, to_state: str) -> Optional[str]:
    """Return the template-service message-type-key for this transition,
    or None if the transition is defined as silent."""
    return TRANSITION_MESSAGE.get((from_state, to_state))
