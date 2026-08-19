"""
Predator forward-convergence observability helpers.

Pure OBSERVATION layer — never changes trading decisions. Provides:
  1. `freeze_journal_context()` — snapshot decision context at FIRE time
  2. `log_rejection()`         — persist strategy-level early-returns
  3. `record_forward_opportunity()` — append to forward opportunity ledger
  4. `bridge_spread_snapshot()` — best-effort MT5 spread at decision time
  5. `latest_gc_direction()`   — best-effort GC futures direction

Fail-open on every helper: if any field cannot be resolved, return None.
Never raise into a caller that could disrupt production logic.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone, timedelta
from typing import Optional

from sqlalchemy import text
from sqlalchemy.orm import Session

log = logging.getLogger(__name__)


def bridge_spread_snapshot() -> Optional[float]:
    """Best-effort current spread from the MT5 bridge heartbeat state."""
    try:
        from routers.bridge import _MT5_TERMINAL_STATE
        if not _MT5_TERMINAL_STATE: return None
        latest = max(
            _MT5_TERMINAL_STATE.values(),
            key=lambda s: s.get("last_seen") or datetime.min,
        )
        s = latest.get("spread_current")
        return float(s) if s is not None else None
    except Exception as exc:
        log.debug("[obs] spread lookup failed: %s", exc)
        return None


def latest_gc_direction(db: Session) -> Optional[str]:
    """Return 'UP'|'DOWN'|'FLAT' from the latest closed GC futures bar."""
    try:
        row = db.execute(text(
            "SELECT open, close FROM gc_futures_bars "
            "ORDER BY candle_time DESC LIMIT 1"
        )).fetchone()
        if not row: return None
        o, c = float(row[0]), float(row[1])
        if c > o + 0.5: return "UP"
        if c < o - 0.5: return "DOWN"
        return "FLAT"
    except Exception as exc:
        log.debug("[obs] gc lookup failed: %s", exc)
        return None


def _velocity_state(m5: list) -> Optional[str]:
    """Simple velocity classification from last 5 M5 bars vs prior 15."""
    try:
        if len(m5) < 20: return None
        recent = m5[-5:]; prior = m5[-20:-5]
        r_range = sum(abs(b[2] - b[3]) for b in recent) / 5
        p_range = sum(abs(b[2] - b[3]) for b in prior) / 15
        if p_range <= 0: return None
        ratio = r_range / p_range
        if ratio > 1.8:  return "EXPANDING"
        if ratio < 0.6:  return "COMPRESSING"
        return "STEADY"
    except Exception:
        return None


def _compression_state(m5: list) -> Optional[str]:
    """Range compression flag from last 10 M5 bars."""
    try:
        if len(m5) < 30: return None
        recent_range = max(b[2] for b in m5[-10:]) - min(b[3] for b in m5[-10:])
        prior_range = max(b[2] for b in m5[-30:-10]) - min(b[3] for b in m5[-30:-10])
        if prior_range <= 0: return None
        ratio = recent_range / prior_range
        if ratio < 0.5:  return "TIGHT"
        if ratio > 1.5:  return "EXPANDED"
        return "NORMAL"
    except Exception:
        return None


def _time_at_level_min(m5: list, level: Optional[float]) -> Optional[float]:
    """Minutes since price first came within 5pt of the key level."""
    if not level or not m5: return None
    try:
        cutoff = level
        for i in range(len(m5) - 1, -1, -1):
            b = m5[i]
            near = min(abs(b[2] - cutoff), abs(b[3] - cutoff)) < 5.0
            if not near:
                return (len(m5) - 1 - i) * 5.0
        return len(m5) * 5.0
    except Exception:
        return None


def freeze_journal_context(
    db: Session,
    *,
    signal_direction: str,
    signal_archetype: str,
    key_level: Optional[float],
    regime: Optional[dict],
    m5: list,
) -> dict:
    """Snapshot decision-time context. Never raises. Missing values → None."""
    regime_dir = (regime or {}).get("direction")
    trend_context = regime_dir  # mirror

    # HTF disagreement: SELL vs BULL regime, or BUY vs BEAR regime
    htf_disagreement = None
    if regime_dir:
        if signal_direction == "SELL" and "BULL" in str(regime_dir).upper():
            htf_disagreement = 1
        elif signal_direction == "BUY" and "BEAR" in str(regime_dir).upper():
            htf_disagreement = 1
        else:
            htf_disagreement = 0

    return dict(
        trend_context=trend_context,
        htf_disagreement=htf_disagreement,
        transition_state=None,  # requires opportunity_state module context — deferred
        velocity_state=_velocity_state(m5),
        compression_state=_compression_state(m5),
        time_at_level_min=_time_at_level_min(m5, key_level),
        gc_context=latest_gc_direction(db),
        spread_at_fire=bridge_spread_snapshot(),
    )


REJECTION_REASONS = {
    "INSUFFICIENT_M5",
    "REGIME_UNFAVORABLE",
    "REGIME_ERROR",
    "ARCHETYPE_INVALID",
    "EXTENSION_EXCEEDED",
    "DUPLICATE_SIGNAL",
    "NO_FIRE",
}


def log_rejection(
    db: Session,
    *,
    archetype: str,
    direction: str,
    rejection_reason: str,
    rejection_detail: Optional[str] = None,
    key_level: Optional[float] = None,
    hypothetical_entry: Optional[float] = None,
    hypothetical_sl: Optional[float] = None,
    hypothetical_tp1: Optional[float] = None,
    hypothetical_tp2: Optional[float] = None,
    session: Optional[str] = None,
    regime: Optional[dict] = None,
    m5: Optional[list] = None,
) -> None:
    """Persist a strategy-level rejection. Fail-open — never raise."""
    try:
        from db_models import PredatorRejection
        journal = freeze_journal_context(
            db,
            signal_direction=direction,
            signal_archetype=archetype,
            key_level=key_level,
            regime=regime,
            m5=m5 or [],
        )
        r = PredatorRejection(
            archetype=archetype,
            direction=direction,
            rejection_reason=rejection_reason,
            rejection_detail=(rejection_detail or "")[:255],
            hypothetical_entry=hypothetical_entry,
            hypothetical_sl=hypothetical_sl,
            hypothetical_tp1=hypothetical_tp1,
            hypothetical_tp2=hypothetical_tp2,
            key_level=key_level,
            session=session,
            regime_direction=(regime or {}).get("direction"),
            regime_volatility=(regime or {}).get("volatility"),
            trend_context=journal["trend_context"],
            velocity_state=journal["velocity_state"],
            compression_state=journal["compression_state"],
            transition_state=journal["transition_state"],
            gc_context=journal["gc_context"],
            spread_at_decision=journal["spread_at_fire"],
        )
        db.add(r)
        db.commit()
    except Exception as exc:
        log.debug("[obs] rejection log failed: %s", exc)
        try: db.rollback()
        except Exception: pass


def record_forward_opportunity(
    db: Session,
    *,
    opportunity_id: str,
    signal_id: str,
    archetype: str,
    direction: str,
    model_decision: str,             # FIRE | ARMED | REJECT
    strategy_rejection_reason: Optional[str] = None,
    portfolio_decision: Optional[str] = None,  # EXECUTED | SKIPPED_EXPOSURE | DUPLICATE | ERROR | PENDING
    portfolio_skip_reason: Optional[str] = None,
    expected_entry: Optional[float] = None,
    sl: Optional[float] = None,
    tp1: Optional[float] = None,
    tp2: Optional[float] = None,
    expected_tickets: Optional[int] = None,
    expected_lots: Optional[float] = None,
    actual_available_capacity: Optional[float] = None,
    actual_open_exposure: Optional[float] = None,
) -> None:
    """Append one row per canonical forward opportunity."""
    try:
        from db_models import PredatorForwardOpportunity
        row = PredatorForwardOpportunity(
            opportunity_id=opportunity_id,
            signal_id=signal_id,
            model_version="PREDATOR_v1.0_M5",
            archetype=archetype,
            direction=direction,
            model_decision=model_decision,
            strategy_rejection_reason=strategy_rejection_reason,
            portfolio_decision=portfolio_decision,
            portfolio_skip_reason=portfolio_skip_reason,
            expected_entry=expected_entry,
            sl=sl,
            tp1=tp1,
            tp2=tp2,
            expected_tickets=expected_tickets,
            expected_lots=expected_lots,
            actual_available_capacity=actual_available_capacity,
            actual_open_exposure=actual_open_exposure,
            resolution_status="PENDING",
        )
        db.add(row)
        db.commit()
    except Exception as exc:
        log.debug("[obs] forward opportunity log failed: %s", exc)
        try: db.rollback()
        except Exception: pass


def current_predator_open_lots(db: Session) -> float:
    """Sum lots currently ENQUEUED or OPEN across all Predator positions."""
    try:
        row = db.execute(text(
            "SELECT COALESCE(SUM(lot_size), 0) FROM predator_positions "
            "WHERE status IN ('ENQUEUED','OPEN')"
        )).fetchone()
        return float(row[0]) if row else 0.0
    except Exception:
        return 0.0
