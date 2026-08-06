"""
Market Intelligence Telegram Alerts — Phase 11
================================================

The 18 spec-defined alert types delivered on a separate channel from
TRADE SIGNALS. These NEVER represent an entry — they represent a
directional / opportunity / structural event the operator wants to
know about even when no compliant entry exists.

Header convention:  "XAUUSD MARKET INTELLIGENCE"
Trade entry alerts (Phase 12 preserved) keep their existing header:
"XAUUSD SIGNAL". This is the operator's cognitive separator.

Behind two flags (both must be TRUE to actually send):
  xauusd_market_intelligence_telegram_enabled
  xauusd_market_intel_shadow_mode  (when TRUE → audit-only, no send)
"""
from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone, timedelta
from typing import Optional

from sqlalchemy import text
from sqlalchemy.orm import Session

log = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# 18 alert type constants (spec-complete)
# ─────────────────────────────────────────────────────────────────────────────

ALERT_BULLISH_CONDITIONS_BUILDING       = "BULLISH_CONDITIONS_BUILDING"
ALERT_BEARISH_CONDITIONS_BUILDING       = "BEARISH_CONDITIONS_BUILDING"
ALERT_BULLISH_TRANSITION_DETECTED       = "BULLISH_TRANSITION_DETECTED"
ALERT_BEARISH_TRANSITION_DETECTED       = "BEARISH_TRANSITION_DETECTED"
ALERT_ASIAN_HIGH_UNDER_PRESSURE         = "ASIAN_HIGH_UNDER_PRESSURE"
ALERT_ASIAN_LOW_UNDER_PRESSURE          = "ASIAN_LOW_UNDER_PRESSURE"
ALERT_PDH_BROKEN                         = "PREV_DAY_HIGH_BROKEN"
ALERT_PDL_BROKEN                         = "PREV_DAY_LOW_BROKEN"
ALERT_BULLISH_BREAKOUT_ACCEPTANCE       = "BULLISH_BREAKOUT_ACCEPTANCE_CONFIRMED"
ALERT_BEARISH_BREAKDOWN_ACCEPTANCE      = "BEARISH_BREAKDOWN_ACCEPTANCE_CONFIRMED"
ALERT_BULLISH_PULLBACK_ZONE             = "BULLISH_PULLBACK_ZONE_APPROACHING"
ALERT_BEARISH_PULLBACK_ZONE             = "BEARISH_PULLBACK_ZONE_APPROACHING"
ALERT_BULLISH_MOVE_EXTENDED             = "BULLISH_MOVE_EXTENDED"
ALERT_BEARISH_MOVE_EXTENDED             = "BEARISH_MOVE_EXTENDED"
ALERT_BULLISH_THESIS_INVALIDATED        = "BULLISH_THESIS_INVALIDATED"
ALERT_BEARISH_THESIS_INVALIDATED        = "BEARISH_THESIS_INVALIDATED"
ALERT_MARKET_RETURNED_TO_BALANCE        = "MARKET_RETURNED_TO_BALANCE"
ALERT_HIGH_IMPACT_EVENT_RISK            = "HIGH_IMPACT_EVENT_RISK"
ALERT_DATA_QUALITY_DEGRADED             = "DATA_QUALITY_DEGRADED"

ALL_ALERT_TYPES = {
    ALERT_BULLISH_CONDITIONS_BUILDING, ALERT_BEARISH_CONDITIONS_BUILDING,
    ALERT_BULLISH_TRANSITION_DETECTED, ALERT_BEARISH_TRANSITION_DETECTED,
    ALERT_ASIAN_HIGH_UNDER_PRESSURE, ALERT_ASIAN_LOW_UNDER_PRESSURE,
    ALERT_PDH_BROKEN, ALERT_PDL_BROKEN,
    ALERT_BULLISH_BREAKOUT_ACCEPTANCE, ALERT_BEARISH_BREAKDOWN_ACCEPTANCE,
    ALERT_BULLISH_PULLBACK_ZONE, ALERT_BEARISH_PULLBACK_ZONE,
    ALERT_BULLISH_MOVE_EXTENDED, ALERT_BEARISH_MOVE_EXTENDED,
    ALERT_BULLISH_THESIS_INVALIDATED, ALERT_BEARISH_THESIS_INVALIDATED,
    ALERT_MARKET_RETURNED_TO_BALANCE,
    ALERT_HIGH_IMPACT_EVENT_RISK, ALERT_DATA_QUALITY_DEGRADED,
}


# ─────────────────────────────────────────────────────────────────────────────
# Cooldown policy
# ─────────────────────────────────────────────────────────────────────────────

_COOLDOWN_MIN_BY_TYPE = {
    # Directional state transitions — 30 min between same-type
    ALERT_BULLISH_CONDITIONS_BUILDING:  30,
    ALERT_BEARISH_CONDITIONS_BUILDING:  30,
    ALERT_BULLISH_TRANSITION_DETECTED:  30,
    ALERT_BEARISH_TRANSITION_DETECTED:  30,
    ALERT_BULLISH_BREAKOUT_ACCEPTANCE:  30,
    ALERT_BEARISH_BREAKDOWN_ACCEPTANCE: 30,
    ALERT_BULLISH_PULLBACK_ZONE:        30,
    ALERT_BEARISH_PULLBACK_ZONE:        30,
    ALERT_BULLISH_MOVE_EXTENDED:        60,
    ALERT_BEARISH_MOVE_EXTENDED:        60,
    ALERT_BULLISH_THESIS_INVALIDATED:   30,
    ALERT_BEARISH_THESIS_INVALIDATED:   30,
    ALERT_MARKET_RETURNED_TO_BALANCE:   60,
    # Pressure/breakout — 15 min (these move fast)
    ALERT_ASIAN_HIGH_UNDER_PRESSURE:    15,
    ALERT_ASIAN_LOW_UNDER_PRESSURE:     15,
    ALERT_PDH_BROKEN:                   15,
    ALERT_PDL_BROKEN:                   15,
    # System alerts — long cooldown, we don't want repeats
    ALERT_HIGH_IMPACT_EVENT_RISK:       120,
    ALERT_DATA_QUALITY_DEGRADED:        120,
}
_DEFAULT_COOLDOWN_MIN = 30
_MAX_ALERTS_PER_DIRECTION_PER_DAY = 6


# ─────────────────────────────────────────────────────────────────────────────
# Data classes
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class IntelAlertCandidate:
    alert_type:    str
    trigger_price: Optional[float]
    trigger_reason: str
    priority:      int = 50           # 0-100; higher fires first if multiple queued

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class DeliveryOutcome:
    alert_type:      str
    result:          str    # sent | shadow | suppressed | failed
    reason:          str
    body:            str
    fingerprint:     str

    def to_dict(self) -> dict:
        return asdict(self)


# ─────────────────────────────────────────────────────────────────────────────
# Detection: state / breakout / evidence deltas → alert candidates
# ─────────────────────────────────────────────────────────────────────────────

_STATE_TO_ALERT_TYPE = {
    "BULLISH_EARLY_WARNING":   ALERT_BULLISH_CONDITIONS_BUILDING,
    "BEARISH_EARLY_WARNING":   ALERT_BEARISH_CONDITIONS_BUILDING,
    "BULLISH_TRANSITION":      ALERT_BULLISH_TRANSITION_DETECTED,
    "BEARISH_TRANSITION":      ALERT_BEARISH_TRANSITION_DETECTED,
    "BULLISH_PULLBACK_PENDING": ALERT_BULLISH_PULLBACK_ZONE,
    "BEARISH_PULLBACK_PENDING": ALERT_BEARISH_PULLBACK_ZONE,
    "BULLISH_EXTENDED":        ALERT_BULLISH_MOVE_EXTENDED,
    "BEARISH_EXTENDED":        ALERT_BEARISH_MOVE_EXTENDED,
    "BULLISH_INVALIDATED":     ALERT_BULLISH_THESIS_INVALIDATED,
    "BEARISH_INVALIDATED":     ALERT_BEARISH_THESIS_INVALIDATED,
    "BALANCED_RANGE":          ALERT_MARKET_RETURNED_TO_BALANCE,
    "EVENT_RISK":              ALERT_HIGH_IMPACT_EVENT_RISK,
    "INSUFFICIENT_DATA":       ALERT_DATA_QUALITY_DEGRADED,
}


def detect_alert_candidates(
    *,
    prev_state: Optional[str],
    new_state: Optional[str],
    trigger_condition: str,
    trigger_price: Optional[float],
    breakouts: Optional[list] = None,
    macro=None,
    snapshot=None,
) -> list[IntelAlertCandidate]:
    """
    Given the state transition + supporting artifacts, decide which alert
    types would fire NOW. Returns 0+ candidates. Cooldown/dedupe is applied
    later — this function only detects "would this be alert-worthy?".
    """
    candidates: list[IntelAlertCandidate] = []

    # State transitions
    if new_state != prev_state and new_state in _STATE_TO_ALERT_TYPE:
        # BALANCED_RANGE is only interesting if we CAME FROM a directional state
        if new_state == "BALANCED_RANGE":
            if prev_state not in (None, "BALANCED_RANGE", "INSUFFICIENT_DATA"):
                candidates.append(IntelAlertCandidate(
                    alert_type=ALERT_MARKET_RETURNED_TO_BALANCE,
                    trigger_price=trigger_price,
                    trigger_reason=f"state {prev_state} → BALANCED_RANGE",
                    priority=40,
                ))
        else:
            atype = _STATE_TO_ALERT_TYPE[new_state]
            priority = 70 if "TRANSITION" in new_state else (
                80 if "INVALIDATED" in new_state else
                60 if "EARLY_WARNING" in new_state else
                65
            )
            candidates.append(IntelAlertCandidate(
                alert_type=atype, trigger_price=trigger_price,
                trigger_reason=trigger_condition, priority=priority,
            ))

    # Breakout classifications → PDH/PDL/Acceptance alerts
    for b in breakouts or []:
        if b.classification in ("BREAKOUT_CONFIRMED", "BREAKOUT_ACCEPTANCE",
                                 "CONTINUATION"):
            level_name = (b.level_name or "").upper()
            if b.direction == "UP":
                if level_name == "PDH":
                    candidates.append(IntelAlertCandidate(
                        alert_type=ALERT_PDH_BROKEN,
                        trigger_price=b.level,
                        trigger_reason=f"PDH {b.level:.2f} — {b.classification.lower()}",
                        priority=75,
                    ))
                if b.classification == "BREAKOUT_ACCEPTANCE":
                    candidates.append(IntelAlertCandidate(
                        alert_type=ALERT_BULLISH_BREAKOUT_ACCEPTANCE,
                        trigger_price=b.level,
                        trigger_reason=f"{level_name} accepted (up)",
                        priority=80,
                    ))
            elif b.direction == "DOWN":
                if level_name == "PDL":
                    candidates.append(IntelAlertCandidate(
                        alert_type=ALERT_PDL_BROKEN,
                        trigger_price=b.level,
                        trigger_reason=f"PDL {b.level:.2f} — {b.classification.lower()}",
                        priority=75,
                    ))
                if b.classification == "BREAKOUT_ACCEPTANCE":
                    candidates.append(IntelAlertCandidate(
                        alert_type=ALERT_BEARISH_BREAKDOWN_ACCEPTANCE,
                        trigger_price=b.level,
                        trigger_reason=f"{level_name} accepted (down)",
                        priority=80,
                    ))

    # Asian high/low under pressure — price within 0.5 ATR + attacking
    if snapshot and snapshot.timeframes and snapshot.timeframes.get("M15"):
        m15 = snapshot.timeframes["M15"].candles
        if m15 and len(m15) >= 5:
            last = m15[-1].close
            asian_hi = getattr(snapshot.levels, "asian_high", None) if snapshot.levels else None
            asian_lo = getattr(snapshot.levels, "asian_low", None) if snapshot.levels else None
            # ATR from H1 if available, else fall back to M15 range
            atr = None
            if snapshot.timeframes.get("H1"):
                h1 = snapshot.timeframes["H1"].candles
                if h1 and len(h1) >= 15:
                    trs = [max(h1[i].high - h1[i].low,
                                abs(h1[i].high - h1[i-1].close),
                                abs(h1[i].low - h1[i-1].close))
                           for i in range(1, min(15, len(h1)))]
                    if trs:
                        atr = sum(trs) / len(trs)
            atr = atr or max(1.0, (max(b.high for b in m15[-14:])
                                     - min(b.low for b in m15[-14:])) / 14)
            if (asian_hi and asian_hi >= last
                    and (asian_hi - last) <= 0.5 * atr
                    and m15[-1].high > m15[-2].high):     # currently pushing
                candidates.append(IntelAlertCandidate(
                    alert_type=ALERT_ASIAN_HIGH_UNDER_PRESSURE,
                    trigger_price=asian_hi,
                    trigger_reason=f"price {last:.2f} within 0.5×ATR of Asian high {asian_hi:.2f}",
                    priority=55,
                ))
            if (asian_lo and asian_lo <= last
                    and (last - asian_lo) <= 0.5 * atr
                    and m15[-1].low < m15[-2].low):
                candidates.append(IntelAlertCandidate(
                    alert_type=ALERT_ASIAN_LOW_UNDER_PRESSURE,
                    trigger_price=asian_lo,
                    trigger_reason=f"price {last:.2f} within 0.5×ATR of Asian low {asian_lo:.2f}",
                    priority=55,
                ))

    # High-impact event risk — from macro
    if macro is not None:
        risk = getattr(macro, "event_risk_level", "NONE")
        if risk in ("HIGH",):
            candidates.append(IntelAlertCandidate(
                alert_type=ALERT_HIGH_IMPACT_EVENT_RISK,
                trigger_price=trigger_price,
                trigger_reason=f"high-impact event in {getattr(macro,'minutes_to_next_event','?')} min",
                priority=90,
            ))

    return candidates


# ─────────────────────────────────────────────────────────────────────────────
# Cooldown / dedupe
# ─────────────────────────────────────────────────────────────────────────────

def _fingerprint(alert_type: str, trigger_price: Optional[float],
                   snapshot=None) -> str:
    key_parts = [alert_type]
    if trigger_price is not None:
        # Round to nearest 5 pts so a slow drift doesn't dodge dedupe
        key_parts.append(f"{round(float(trigger_price) / 5) * 5:.0f}")
    session = None
    if snapshot and snapshot.session:
        session = snapshot.session.kz_label
    if session:
        key_parts.append(session)
    return hashlib.sha256("|".join(key_parts).encode()).hexdigest()[:16]


def _cooldown_ok(db: Session, alert_type: str, fingerprint: str,
                   instrument: str) -> tuple[bool, str]:
    """Return (ok, reason)."""
    cooldown_min = _COOLDOWN_MIN_BY_TYPE.get(alert_type, _DEFAULT_COOLDOWN_MIN)
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=cooldown_min)
    try:
        row = db.execute(text(
            "SELECT ts FROM market_intelligence_alerts "
            "WHERE instrument=:i AND alert_type=:t "
            "AND fingerprint=:f AND delivery_result IN ('sent','shadow') "
            "AND ts >= :c ORDER BY id DESC LIMIT 1"
        ), {"i": instrument, "t": alert_type, "f": fingerprint,
              "c": cutoff}).fetchone()
        if row is not None:
            return (False, f"cooldown ({cooldown_min}min) — fired at {row[0]}")
    except Exception as exc:
        log.debug("[intel-alerts] cooldown check failed: %s", exc)
    return (True, "")


def _daily_cap_ok(db: Session, alert_type: str, instrument: str) -> tuple[bool, str]:
    """Max 6 alerts per direction per day (bullish + bearish counted separately)."""
    direction = ("BULLISH" if "BULLISH" in alert_type or "PDH" in alert_type
                             or "ASIAN_HIGH" in alert_type
                 else "BEARISH" if "BEARISH" in alert_type or "PDL" in alert_type
                                    or "ASIAN_LOW" in alert_type
                 else "SYSTEM")
    if direction == "SYSTEM":
        return (True, "")
    today0 = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    like_pat = f"%{direction}%"
    try:
        row = db.execute(text(
            "SELECT COUNT(*) FROM market_intelligence_alerts "
            "WHERE instrument=:i AND ts >= :d AND alert_type LIKE :p "
            "AND delivery_result IN ('sent','shadow')"
        ), {"i": instrument, "d": today0, "p": like_pat}).fetchone()
        n = int(row[0]) if row else 0
        if n >= _MAX_ALERTS_PER_DIRECTION_PER_DAY:
            return (False,
                    f"daily cap ({_MAX_ALERTS_PER_DIRECTION_PER_DAY}) reached "
                    f"for {direction} ({n} fired today)")
    except Exception as exc:
        log.debug("[intel-alerts] daily cap check failed: %s", exc)
    return (True, "")


# ─────────────────────────────────────────────────────────────────────────────
# Template rendering
# ─────────────────────────────────────────────────────────────────────────────

_ACTION_BY_STATE = {
    "BULLISH_OBSERVING": "Observe",
    "BULLISH_EARLY_WARNING": "Observe",
    "BULLISH_TRANSITION": "Prepare",
    "BULLISH_CONFIRMED": "Wait for pullback",
    "BULLISH_PULLBACK_PENDING": "Wait for pullback",
    "BULLISH_ENTRY_AVAILABLE": "Entry confirmed",
    "BULLISH_EXTENDED": "Do not chase",
    "BULLISH_INVALIDATED": "Stand aside",
    "BEARISH_OBSERVING": "Observe",
    "BEARISH_EARLY_WARNING": "Observe",
    "BEARISH_TRANSITION": "Prepare",
    "BEARISH_CONFIRMED": "Wait for pullback",
    "BEARISH_PULLBACK_PENDING": "Wait for pullback",
    "BEARISH_ENTRY_AVAILABLE": "Entry confirmed",
    "BEARISH_EXTENDED": "Do not chase",
    "BEARISH_INVALIDATED": "Stand aside",
    "BALANCED_RANGE": "Observe",
    "EVENT_RISK": "Stand aside",
    "INSUFFICIENT_DATA": "Stand aside",
}


def _fmt_price(p):
    return f"${p:,.2f}" if p is not None else "—"


def _fmt_eat(now_utc: datetime) -> str:
    # EAT = UTC + 3, no DST
    eat = now_utc + timedelta(hours=3)
    return eat.strftime("%d %b %Y · %H:%M EAT")


def build_intel_body(
    *,
    alert_type: str,
    trigger_reason: str,
    snapshot,
    verdict,                   # SeparatedVerdict
    ranking,                   # KeyLevelRanking
    macro,                     # MacroAssessment
    evidence,                  # EvidenceAssessment
    state_transition,          # StateTransition
) -> str:
    """Render the full spec-compliant intelligence body."""
    now = datetime.now(timezone.utc)
    price = snapshot.timeframes["M15"].candles[-1].close if (
        snapshot and snapshot.timeframes and snapshot.timeframes.get("M15")
        and snapshot.timeframes["M15"].candles) else None
    session = snapshot.session.kz_pretty if (snapshot and snapshot.session) else "?"

    # Header + situational
    lines = ["XAUUSD MARKET INTELLIGENCE",
             f"Alert:                  {alert_type.replace('_',' ').title()}",
             f"Time:                   {_fmt_eat(now)}",
             f"Price:                  {_fmt_price(price)}",
             f"Session:                {session}"]

    if verdict:
        lines += [
            f"Directional assessment: {verdict.directional_assessment}",
            f"Market regime:          {state_transition.new_state if state_transition else '—'}",
            f"Opportunity state:      {verdict.opportunity_status}",
            f"Directional confidence: {evidence.directional_confidence if evidence else 0}/100",
            f"Entry confidence:       {evidence.entry_quality_confidence if evidence else 0}/100",
            f"Data quality:           {evidence.data_quality_score if evidence else 0}/100",
        ]

    # What changed
    lines.append("")
    lines.append("What changed:")
    lines.append(f" · {trigger_reason}")

    # Basis of direction — top 5 evidence items for dominant side
    lines.append("")
    lines.append("Basis of direction:")
    if evidence:
        dbias = evidence.dominant_direction
        items = (evidence.bull_items if dbias == "BULL"
                  else evidence.bear_items if dbias == "BEAR"
                  else (evidence.bull_items + evidence.bear_items))
        for it in items[:5]:
            lines.append(f" · {it.description}")
        if not items:
            lines.append(" · (no directional evidence at this moment)")

    # Contradictions
    lines.append("")
    lines.append("Contradictions:")
    if evidence and evidence.contradictions:
        for c in evidence.contradictions[:5]:
            lines.append(f" · {c.description}")
    else:
        lines.append(" · (none)")

    # Tier 1 levels
    lines.append("")
    lines.append("Tier 1 levels:")
    if ranking and ranking.tier1:
        above = [l for l in ranking.tier1 if l.side == "ABOVE"]
        below = [l for l in ranking.tier1 if l.side == "BELOW"]
        if above:
            lines.append(f" · Immediate resistance:  {_fmt_price(above[0].price)} "
                          f"({above[0].label})")
        if below:
            lines.append(f" · Immediate support:     {_fmt_price(below[0].price)} "
                          f"({below[0].label})")
        # Liquidity pools above/below
        if len(above) > 1:
            lines.append(f" · Liquidity above:       {_fmt_price(above[1].price)}")
        if len(below) > 1:
            lines.append(f" · Liquidity below:       {_fmt_price(below[1].price)}")
    if state_transition and state_transition.invalidation_price is not None:
        lines.append(f" · Directional invalidation: {_fmt_price(state_transition.invalidation_price)}")

    # Macro
    lines.append("")
    lines.append("Macro context:")
    if macro:
        dxy_str = f"{macro.dxy_direction}"
        if macro.dxy_move_pct is not None:
            dxy_str += f" ({macro.dxy_move_pct:+.2f}%)"
        yld_str = f"{macro.yield_10y_direction}"
        if macro.yield_10y_delta_bp is not None:
            yld_str += f" ({macro.yield_10y_delta_bp:+.1f}bp)"
        ev_str = macro.event_risk_level
        if macro.minutes_to_next_event is not None:
            ev_str += f" (next {macro.minutes_to_next_event}min)"
        lines.append(f" · DXY:             {dxy_str}")
        lines.append(f" · US 10Y:          {yld_str}")
        lines.append(f" · Event risk:      {ev_str}")
        lines.append(f" · Correlation:     {macro.correlation_state}")

    # Interpretation + action
    action = _ACTION_BY_STATE.get(state_transition.new_state if state_transition else "", "Observe")
    lines.append("")
    lines.append("Current interpretation:")
    lines.append(f" · {_narrate(verdict, state_transition, macro, ranking)}")
    lines.append("")
    lines.append(f"Action: {action}")
    lines.append("")
    lines.append("(Market intelligence alert. Not a trade signal — entry rules unchanged.)")
    return "\n".join(lines)


def _narrate(verdict, st, macro, ranking) -> str:
    """One-sentence interpretation composed from the fields."""
    if verdict is None or st is None:
        return "state undefined — awaiting data."
    dstr = verdict.directional_assessment.lower()
    ostr = verdict.opportunity_status.lower()
    macro_str = ""
    if macro and macro.macro_alignment == "SUPPORTIVE":
        macro_str = " Macro is supportive."
    elif macro and macro.macro_alignment == "OPPOSING":
        macro_str = " Macro is opposing — watch for reversal."
    elif macro and macro.macro_alignment == "MIXED":
        macro_str = " Macro is mixed."
    tier1_str = ""
    if ranking and ranking.tier1:
        tier1_str = f" Nearest levels: {ranking.tier1[0].label} @ {_fmt_price(ranking.tier1[0].price)}."
    return f"Direction {dstr}; opportunity {ostr}.{macro_str}{tier1_str}"


# ─────────────────────────────────────────────────────────────────────────────
# Persistence + delivery
# ─────────────────────────────────────────────────────────────────────────────

def _persist(db: Session, *, alert_type: str, trigger_price: Optional[float],
              verdict, evidence, body: str, delivery_result: str,
              delivery_reason: str, fingerprint: str, instrument: str):
    from db_models import MarketIntelligenceAlert
    row = MarketIntelligenceAlert(
        instrument=instrument, alert_type=alert_type,
        trigger_price=trigger_price,
        directional_assessment=(verdict.directional_assessment if verdict else None),
        opportunity_status=(verdict.opportunity_status if verdict else None),
        entry_status=(verdict.entry_status if verdict else None),
        directional_confidence=(evidence.directional_confidence if evidence else None),
        entry_confidence=(evidence.entry_quality_confidence if evidence else None),
        body_text=body[:60000],
        delivery_result=delivery_result, delivery_reason=delivery_reason[:255],
        fingerprint=fingerprint,
    )
    db.add(row); db.commit()


def _send_telegram(body: str) -> tuple[bool, str]:
    """Send via existing send_telegram_message helper. Returns (ok, reason)."""
    try:
        from services.telegram_alert_service import send_telegram_message
        ok = send_telegram_message(body)
        return (bool(ok), "sent" if ok else "telegram API returned falsy")
    except Exception as exc:
        return (False, f"{type(exc).__name__}: {exc}")


# ─────────────────────────────────────────────────────────────────────────────
# Public: fire_intel_alerts()
# ─────────────────────────────────────────────────────────────────────────────

def fire_intel_alerts(
    db: Session,
    *,
    prev_state: Optional[str],
    new_state: Optional[str],
    trigger_condition: str,
    trigger_price: Optional[float],
    snapshot, verdict, evidence, ranking, macro,
    state_transition, breakouts=None,
    instrument: str = "XAU/USD",
    force_send: bool = False,
) -> list[DeliveryOutcome]:
    """
    End-to-end pipeline: detect → dedupe → render → send → persist.
    Returns per-candidate DeliveryOutcome list.

    Reads live config for enable/shadow flags UNLESS force_send=True.
    """
    from config import settings

    outcomes: list[DeliveryOutcome] = []

    # Detect candidates
    cands = detect_alert_candidates(
        prev_state=prev_state, new_state=new_state,
        trigger_condition=trigger_condition, trigger_price=trigger_price,
        breakouts=breakouts, macro=macro, snapshot=snapshot,
    )
    if not cands:
        return outcomes

    # Sort highest-priority first
    cands.sort(key=lambda c: -c.priority)

    enabled = getattr(settings, "xauusd_market_intelligence_telegram_enabled", False) \
                or force_send
    shadow = getattr(settings, "xauusd_market_intel_shadow_mode", True) and not force_send

    for cand in cands:
        fp = _fingerprint(cand.alert_type, cand.trigger_price, snapshot)

        # Cooldown
        cd_ok, cd_reason = _cooldown_ok(db, cand.alert_type, fp, instrument)
        if not cd_ok:
            _persist(db, alert_type=cand.alert_type, trigger_price=cand.trigger_price,
                      verdict=verdict, evidence=evidence, body="",
                      delivery_result="suppressed", delivery_reason=cd_reason,
                      fingerprint=fp, instrument=instrument)
            outcomes.append(DeliveryOutcome(cand.alert_type, "suppressed", cd_reason, "", fp))
            continue

        # Daily cap
        cap_ok, cap_reason = _daily_cap_ok(db, cand.alert_type, instrument)
        if not cap_ok:
            _persist(db, alert_type=cand.alert_type, trigger_price=cand.trigger_price,
                      verdict=verdict, evidence=evidence, body="",
                      delivery_result="suppressed", delivery_reason=cap_reason,
                      fingerprint=fp, instrument=instrument)
            outcomes.append(DeliveryOutcome(cand.alert_type, "suppressed", cap_reason, "", fp))
            continue

        # Render body
        body = build_intel_body(
            alert_type=cand.alert_type, trigger_reason=cand.trigger_reason,
            snapshot=snapshot, verdict=verdict, ranking=ranking,
            macro=macro, evidence=evidence, state_transition=state_transition,
        )

        if not enabled:
            _persist(db, alert_type=cand.alert_type, trigger_price=cand.trigger_price,
                      verdict=verdict, evidence=evidence, body=body,
                      delivery_result="suppressed",
                      delivery_reason="market_intelligence_telegram_enabled=False",
                      fingerprint=fp, instrument=instrument)
            outcomes.append(DeliveryOutcome(cand.alert_type, "suppressed",
                                              "flag off", body, fp))
            continue

        if shadow:
            _persist(db, alert_type=cand.alert_type, trigger_price=cand.trigger_price,
                      verdict=verdict, evidence=evidence, body=body,
                      delivery_result="shadow",
                      delivery_reason="shadow_mode_dry_run",
                      fingerprint=fp, instrument=instrument)
            outcomes.append(DeliveryOutcome(cand.alert_type, "shadow",
                                              "shadow mode", body, fp))
            continue

        # Live send
        ok, reason = _send_telegram(body)
        _persist(db, alert_type=cand.alert_type, trigger_price=cand.trigger_price,
                  verdict=verdict, evidence=evidence, body=body,
                  delivery_result="sent" if ok else "failed",
                  delivery_reason=reason,
                  fingerprint=fp, instrument=instrument)
        outcomes.append(DeliveryOutcome(cand.alert_type,
                                          "sent" if ok else "failed",
                                          reason, body, fp))

    return outcomes


def recent_intel_alerts(db: Session, *, limit: int = 20,
                          instrument: str = "XAU/USD") -> list[dict]:
    try:
        rows = db.execute(text(
            "SELECT ts, alert_type, trigger_price, directional_assessment, "
            "opportunity_status, entry_status, delivery_result, delivery_reason, "
            "fingerprint FROM market_intelligence_alerts "
            "WHERE instrument=:i ORDER BY id DESC LIMIT :n"
        ), {"i": instrument, "n": limit}).fetchall()
        return [
            {"ts": (r[0].isoformat() if hasattr(r[0], "isoformat") else str(r[0])),
             "alert_type": r[1], "trigger_price": r[2],
             "directional_assessment": r[3], "opportunity_status": r[4],
             "entry_status": r[5], "delivery_result": r[6],
             "delivery_reason": r[7], "fingerprint": r[8]}
            for r in rows
        ]
    except Exception as exc:
        log.warning("[intel-alerts] recent query failed: %s", exc)
        return []


__all__ = [
    "detect_alert_candidates", "fire_intel_alerts", "build_intel_body",
    "recent_intel_alerts",
    "IntelAlertCandidate", "DeliveryOutcome",
    "ALL_ALERT_TYPES",
]
