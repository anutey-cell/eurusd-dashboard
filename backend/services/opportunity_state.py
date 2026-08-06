"""
Opportunity State Machine — Phase 7
======================================

Two symmetric state graphs (bull + bear) plus three shared states. Consumes
outputs from Phases 3-6 and decides the current opportunity state. Every
transition is appended to `opportunity_state_transitions` — restart-safe.

Bullish states:
  BULLISH_OBSERVING          — early flat context, watch levels
  BULLISH_EARLY_WARNING      — bull evidence forming (>= 25)
  BULLISH_TRANSITION         — regime shifted to bull-transition
  BULLISH_CONFIRMED          — regime STRONG_BULL / CONTINUATION, high evidence
  BULLISH_PULLBACK_PENDING   — from CONFIRMED, price pulling back to level
  BULLISH_ENTRY_AVAILABLE    — pullback found + protected low intact + retest zone
  BULLISH_EXTENDED           — regime EXHAUSTION or extension_risk >= 80
  BULLISH_INVALIDATED        — invalidation broken / regime flipped bear

Bearish states: mirror.

Shared:
  BALANCED_RANGE, EVENT_RISK, INSUFFICIENT_DATA

Persistence: append-only. Every distinct `new_state` value produces one row
in the DB. Repeat evaluations that don't change state are no-ops.

Behind `xauusd_opportunity_state_machine_enabled`. Off by default. Exposed
via /api/v1/diagnostics/opportunity-state for shadow-mode observation.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import text
from sqlalchemy.orm import Session

log = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# State constants
# ─────────────────────────────────────────────────────────────────────────────

# Bullish
S_BULL_OBSERVING          = "BULLISH_OBSERVING"
S_BULL_EARLY_WARNING      = "BULLISH_EARLY_WARNING"
S_BULL_TRANSITION         = "BULLISH_TRANSITION"
S_BULL_CONFIRMED          = "BULLISH_CONFIRMED"
S_BULL_PULLBACK_PENDING   = "BULLISH_PULLBACK_PENDING"
S_BULL_ENTRY_AVAILABLE    = "BULLISH_ENTRY_AVAILABLE"
S_BULL_EXTENDED           = "BULLISH_EXTENDED"
S_BULL_INVALIDATED        = "BULLISH_INVALIDATED"

# Bearish
S_BEAR_OBSERVING          = "BEARISH_OBSERVING"
S_BEAR_EARLY_WARNING      = "BEARISH_EARLY_WARNING"
S_BEAR_TRANSITION         = "BEARISH_TRANSITION"
S_BEAR_CONFIRMED          = "BEARISH_CONFIRMED"
S_BEAR_PULLBACK_PENDING   = "BEARISH_PULLBACK_PENDING"
S_BEAR_ENTRY_AVAILABLE    = "BEARISH_ENTRY_AVAILABLE"
S_BEAR_EXTENDED           = "BEARISH_EXTENDED"
S_BEAR_INVALIDATED        = "BEARISH_INVALIDATED"

# Shared
S_BALANCED_RANGE          = "BALANCED_RANGE"
S_EVENT_RISK              = "EVENT_RISK"
S_INSUFFICIENT_DATA       = "INSUFFICIENT_DATA"

_BULL_STATES = {S_BULL_OBSERVING, S_BULL_EARLY_WARNING, S_BULL_TRANSITION,
                 S_BULL_CONFIRMED, S_BULL_PULLBACK_PENDING,
                 S_BULL_ENTRY_AVAILABLE, S_BULL_EXTENDED, S_BULL_INVALIDATED}
_BEAR_STATES = {S_BEAR_OBSERVING, S_BEAR_EARLY_WARNING, S_BEAR_TRANSITION,
                 S_BEAR_CONFIRMED, S_BEAR_PULLBACK_PENDING,
                 S_BEAR_ENTRY_AVAILABLE, S_BEAR_EXTENDED, S_BEAR_INVALIDATED}


# ─────────────────────────────────────────────────────────────────────────────
# Output
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class StateTransition:
    ts:                  datetime
    prev_state:          Optional[str]
    new_state:           str
    price:               Optional[float]
    invalidation_price:  Optional[float]
    confidence:          float
    trigger_condition:   str
    evidence:            list = field(default_factory=list)
    contradictions:      list = field(default_factory=list)
    key_levels:          dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["ts"] = self.ts.isoformat()
        return d


# ─────────────────────────────────────────────────────────────────────────────
# Persistence helpers
# ─────────────────────────────────────────────────────────────────────────────

def _current_state_from_db(db: Session, instrument: str = "XAU/USD") -> Optional[str]:
    """Read the most recent state from the DB. None if never set."""
    try:
        row = db.execute(text(
            "SELECT new_state FROM opportunity_state_transitions "
            "WHERE instrument=:i ORDER BY id DESC LIMIT 1"
        ), {"i": instrument}).fetchone()
        return row[0] if row else None
    except Exception as exc:
        log.warning("[opportunity_state] db read failed: %s", exc)
        return None


def _write_transition(db: Session, tr: StateTransition, instrument: str = "XAU/USD"):
    """Append one transition row."""
    from db_models import OpportunityStateTransition
    row = OpportunityStateTransition(
        instrument=instrument,
        prev_state=tr.prev_state,
        new_state=tr.new_state,
        price=tr.price,
        invalidation_price=tr.invalidation_price,
        confidence=tr.confidence,
        trigger_condition=tr.trigger_condition,
        evidence_json=json.dumps([e.to_dict() if hasattr(e, "to_dict") else e
                                     for e in (tr.evidence or [])],
                                    default=str)[:65000],
        contradictions_json=json.dumps([c.to_dict() if hasattr(c, "to_dict") else c
                                            for c in (tr.contradictions or [])],
                                           default=str)[:65000],
        key_levels_json=json.dumps(tr.key_levels or {}, default=str)[:65000],
    )
    db.add(row)
    db.commit()


# ─────────────────────────────────────────────────────────────────────────────
# Core decision function
# ─────────────────────────────────────────────────────────────────────────────

def decide_next_state(
    current: Optional[str],
    *,
    snapshot,
    regime,
    htf_alignment,
    evidence,
    breakouts: Optional[list] = None,
) -> tuple[str, str, float, Optional[float]]:
    """
    Pure function — no DB. Returns (new_state, trigger_condition, confidence, invalidation_price).

    Priority:
      1. INSUFFICIENT_DATA when data quality too low
      2. EVENT_RISK when high-impact within 15 min
      3. INVALIDATED when regime flips to opposite side (from confirmed)
      4. Directional path forward
      5. BALANCED_RANGE default
    """
    if current is None:
        current = S_BULL_OBSERVING   # arbitrary — we'll overwrite immediately

    # 1) Insufficient data guard
    dq = getattr(evidence, "data_quality_score", 0) if evidence else 0
    if snapshot is None or dq < 50:
        return (S_INSUFFICIENT_DATA, "data_quality<50", 0.0, None)

    # 2) Event risk
    event_score = getattr(evidence, "event_risk_score", 0) if evidence else 0
    if event_score >= 80:
        return (S_EVENT_RISK, "event_risk>=80", 100.0, None)

    # Gather signals
    dbias = getattr(evidence, "dominant_direction", "NEUTRAL")
    bull_ev = getattr(evidence, "bull_evidence_score", 0)
    bear_ev = getattr(evidence, "bear_evidence_score", 0)
    contra  = getattr(evidence, "contradiction_score", 0)
    ext_risk = getattr(evidence, "extension_risk_score", 0)
    dir_conf = getattr(evidence, "directional_confidence", 0)

    regime_label = getattr(regime, "regime", None) if regime else None
    regime_bias  = getattr(regime, "directional_bias", "NEUTRAL") if regime else "NEUTRAL"
    invalidation = getattr(regime, "invalidation_price", None) if regime else None

    htf_dir = getattr(htf_alignment, "direction", "NEUTRAL") if htf_alignment else "NEUTRAL"
    htf_strength = getattr(htf_alignment, "strength", "NONE") if htf_alignment else "NONE"

    # Any breakout classifications matching current direction?
    breakouts = breakouts or []
    def _bo_accepted_up():
        return any(b.direction == "UP" and b.classification in
                    ("BREAKOUT_ACCEPTANCE", "BREAKOUT_CONFIRMED",
                     "CONTINUATION", "BREAKOUT_RETEST")
                    for b in breakouts)
    def _bo_accepted_down():
        return any(b.direction == "DOWN" and b.classification in
                    ("BREAKOUT_ACCEPTANCE", "BREAKOUT_CONFIRMED",
                     "CONTINUATION", "BREAKOUT_RETEST")
                    for b in breakouts)
    def _bo_retest_up():
        return any(b.direction == "UP" and b.classification == "BREAKOUT_RETEST"
                    for b in breakouts)
    def _bo_retest_down():
        return any(b.direction == "DOWN" and b.classification == "BREAKOUT_RETEST"
                    for b in breakouts)

    # 3) Invalidation from a confirmed state (regime flipped to opposite)
    if current in (S_BULL_CONFIRMED, S_BULL_PULLBACK_PENDING,
                   S_BULL_ENTRY_AVAILABLE, S_BULL_EXTENDED):
        if regime_bias == "BEAR" or (invalidation and snapshot.timeframes.get("M15")
                                        and snapshot.timeframes["M15"].candles
                                        and snapshot.timeframes["M15"].candles[-1].close < invalidation):
            return (S_BULL_INVALIDATED, "regime flipped or invalidation broken",
                    max(0.0, 100 - contra), invalidation)
    if current in (S_BEAR_CONFIRMED, S_BEAR_PULLBACK_PENDING,
                   S_BEAR_ENTRY_AVAILABLE, S_BEAR_EXTENDED):
        if regime_bias == "BULL" or (invalidation and snapshot.timeframes.get("M15")
                                        and snapshot.timeframes["M15"].candles
                                        and snapshot.timeframes["M15"].candles[-1].close > invalidation):
            return (S_BEAR_INVALIDATED, "regime flipped or invalidation broken",
                    max(0.0, 100 - contra), invalidation)

    # 4) Extended override
    if regime_label == "EXHAUSTION_OVEREXTENSION" or ext_risk >= 80:
        if dbias == "BULL" or regime_bias == "BULL":
            return (S_BULL_EXTENDED, f"regime=EXHAUSTION or ext_risk>=80 ({ext_risk})",
                    float(dir_conf), invalidation)
        if dbias == "BEAR" or regime_bias == "BEAR":
            return (S_BEAR_EXTENDED, f"regime=EXHAUSTION or ext_risk>=80 ({ext_risk})",
                    float(dir_conf), invalidation)

    # 5) BULL directional path.
    # Evidence dbias wins over regime bias when they disagree — evidence is
    # measured on the last 30 M15 bars (current market action) whereas regime
    # bias comes from HTF EMAs which can lag by an entire session.
    def _prefer_bull():
        if dbias == "BULL":  return True
        if dbias == "BEAR":  return False
        return regime_bias == "BULL"
    def _prefer_bear():
        if dbias == "BEAR":  return True
        if dbias == "BULL":  return False
        return regime_bias == "BEAR"

    if _prefer_bull():
        # PULLBACK / ENTRY_AVAILABLE detection (regime = BULL_PULLBACK + protected low intact)
        if regime_label == "BULLISH_PULLBACK":
            # If bull evidence still moderate AND price near swing low → ENTRY_AVAILABLE
            if bull_ev >= 25 and _bo_retest_up():
                return (S_BULL_ENTRY_AVAILABLE, "bull pullback + retest at level held",
                        float(dir_conf), invalidation)
            return (S_BULL_PULLBACK_PENDING, "regime=BULLISH_PULLBACK",
                    float(dir_conf), invalidation)

        # CONFIRMED
        if regime_label in ("STRONG_BULLISH_EXPANSION", "BULLISH_CONTINUATION"):
            return (S_BULL_CONFIRMED, f"regime={regime_label}",
                    float(dir_conf), invalidation)
        if bull_ev >= 55 and _bo_accepted_up():
            return (S_BULL_CONFIRMED, f"bull_ev={bull_ev} + BO accepted",
                    float(dir_conf), invalidation)

        # TRANSITION
        if regime_label == "BULLISH_TRANSITION":
            return (S_BULL_TRANSITION, "regime=BULLISH_TRANSITION",
                    float(dir_conf), invalidation)
        if htf_dir == "BULL" and htf_strength in ("MEDIUM", "STRONG") and bull_ev >= 40:
            return (S_BULL_TRANSITION, f"HTF={htf_strength} bull + bull_ev={bull_ev}",
                    float(dir_conf), invalidation)

        # EARLY_WARNING
        if bull_ev >= 25 or regime_label == "BULLISH_ACCUMULATION":
            return (S_BULL_EARLY_WARNING, f"bull_ev={bull_ev} or accumulation",
                    float(dir_conf), invalidation)

        return (S_BULL_OBSERVING, "bull bias, no evidence yet",
                float(dir_conf), invalidation)

    # 6) BEAR directional path (mirror)
    if _prefer_bear():
        if regime_label == "BEARISH_PULLBACK":
            if bear_ev >= 25 and _bo_retest_down():
                return (S_BEAR_ENTRY_AVAILABLE, "bear pullback + retest at level held",
                        float(dir_conf), invalidation)
            return (S_BEAR_PULLBACK_PENDING, "regime=BEARISH_PULLBACK",
                    float(dir_conf), invalidation)
        if regime_label in ("STRONG_BEARISH_EXPANSION", "BEARISH_CONTINUATION"):
            return (S_BEAR_CONFIRMED, f"regime={regime_label}",
                    float(dir_conf), invalidation)
        if bear_ev >= 55 and _bo_accepted_down():
            return (S_BEAR_CONFIRMED, f"bear_ev={bear_ev} + BO accepted",
                    float(dir_conf), invalidation)
        if regime_label == "BEARISH_TRANSITION":
            return (S_BEAR_TRANSITION, "regime=BEARISH_TRANSITION",
                    float(dir_conf), invalidation)
        if htf_dir == "BEAR" and htf_strength in ("MEDIUM", "STRONG") and bear_ev >= 40:
            return (S_BEAR_TRANSITION, f"HTF={htf_strength} bear + bear_ev={bear_ev}",
                    float(dir_conf), invalidation)
        if bear_ev >= 25 or regime_label == "BEARISH_ACCUMULATION":
            return (S_BEAR_EARLY_WARNING, f"bear_ev={bear_ev} or accumulation",
                    float(dir_conf), invalidation)
        return (S_BEAR_OBSERVING, "bear bias, no evidence yet",
                float(dir_conf), invalidation)

    # 7) Default: BALANCED_RANGE
    return (S_BALANCED_RANGE, "no directional edge", float(dir_conf), invalidation)


# ─────────────────────────────────────────────────────────────────────────────
# Engine — DB-aware wrapper
# ─────────────────────────────────────────────────────────────────────────────

def evaluate_and_transition(
    db: Session,
    *,
    snapshot,
    regime,
    htf_alignment,
    evidence,
    breakouts: Optional[list] = None,
    instrument: str = "XAU/USD",
    persist: bool = True,
) -> StateTransition:
    """
    1. Load current state from DB.
    2. Decide next state via decide_next_state().
    3. If different, append a transition row.
    4. Return the transition (whether or not it was persisted).
    """
    current = _current_state_from_db(db, instrument=instrument)
    new_state, trigger, confidence, invalidation = decide_next_state(
        current, snapshot=snapshot, regime=regime,
        htf_alignment=htf_alignment, evidence=evidence, breakouts=breakouts,
    )
    price = None
    key_levels: dict = {}
    if snapshot:
        # Best-effort price
        if snapshot.bid is not None and snapshot.ask is not None:
            price = (snapshot.bid + snapshot.ask) / 2
        elif snapshot.timeframes and snapshot.timeframes.get("M15"):
            bars = snapshot.timeframes["M15"].candles
            price = bars[-1].close if bars else None
        # Levels snapshot
        if snapshot.levels:
            lb = snapshot.levels
            key_levels = {"pdh": lb.pdh, "pdl": lb.pdl,
                           "asian_high": lb.asian_high, "asian_low": lb.asian_low,
                           "daily_open": lb.daily_open}

    ev_items = ((evidence.bull_items if evidence and hasattr(evidence, "bull_items") else []) +
                 (evidence.bear_items if evidence and hasattr(evidence, "bear_items") else []))
    contra_items = evidence.contradictions if evidence and hasattr(evidence, "contradictions") else []

    tr = StateTransition(
        ts=datetime.now(timezone.utc),
        prev_state=current, new_state=new_state,
        price=price, invalidation_price=invalidation,
        confidence=float(confidence), trigger_condition=trigger,
        evidence=ev_items, contradictions=contra_items, key_levels=key_levels,
    )

    if persist and new_state != current:
        try:
            _write_transition(db, tr, instrument=instrument)
        except Exception as exc:
            log.warning("[opportunity_state] persist failed: %s", exc)

    return tr


def get_current(db: Session, instrument: str = "XAU/USD") -> Optional[str]:
    """Convenience — return the latest state string from DB, or None."""
    return _current_state_from_db(db, instrument=instrument)


def get_recent_transitions(db: Session, *, limit: int = 20,
                             instrument: str = "XAU/USD") -> list[dict]:
    """Return the last N transitions from DB (dicts, not ORM instances)."""
    try:
        rows = db.execute(text(
            "SELECT ts, prev_state, new_state, price, invalidation_price, "
            "confidence, trigger_condition FROM opportunity_state_transitions "
            "WHERE instrument=:i ORDER BY id DESC LIMIT :n"
        ), {"i": instrument, "n": limit}).fetchall()
        return [{"ts": (r[0].isoformat() if hasattr(r[0], "isoformat") else str(r[0])),
                 "prev_state": r[1], "new_state": r[2],
                 "price": r[3], "invalidation_price": r[4],
                 "confidence": r[5], "trigger_condition": r[6]}
                for r in rows]
    except Exception as exc:
        log.warning("[opportunity_state] get_recent_transitions failed: %s", exc)
        return []


__all__ = [
    "decide_next_state", "evaluate_and_transition",
    "get_current", "get_recent_transitions", "StateTransition",
    "S_BULL_OBSERVING", "S_BULL_EARLY_WARNING", "S_BULL_TRANSITION",
    "S_BULL_CONFIRMED", "S_BULL_PULLBACK_PENDING", "S_BULL_ENTRY_AVAILABLE",
    "S_BULL_EXTENDED", "S_BULL_INVALIDATED",
    "S_BEAR_OBSERVING", "S_BEAR_EARLY_WARNING", "S_BEAR_TRANSITION",
    "S_BEAR_CONFIRMED", "S_BEAR_PULLBACK_PENDING", "S_BEAR_ENTRY_AVAILABLE",
    "S_BEAR_EXTENDED", "S_BEAR_INVALIDATED",
    "S_BALANCED_RANGE", "S_EVENT_RISK", "S_INSUFFICIENT_DATA",
]
