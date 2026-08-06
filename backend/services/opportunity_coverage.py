"""
Opportunity Coverage Evaluator — Phase 13
==========================================

The brief:
"Do not judge the system only by signal win rate. A system with high win
 rate but very low opportunity coverage must be reported as under-detecting.
 Measure significant directional moves that received no alert."

This module does two things:
  1. Scan historical H1 candles for QUALIFYING EXPANSIONS — significant
     directional moves that any trader would have seen and traded.
  2. For each expansion, look up alerts (intel + strategist) around it
     and compute detection delay / coverage / lateness.

Then it produces a coverage report:
  bullish_coverage_pct
  bearish_coverage_pct
  overall_coverage_pct
  median_detection_delay_min
  missed_count
  late_detection_count
  false_directional_alerts
  direction_accuracy
  entry_accuracy

Behind `xauusd_opportunity_coverage_enabled`. Read-only diagnostic +
nightly job. Detection is idempotent (fingerprint dedupe).
"""
from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone, timedelta
from statistics import median
from typing import Optional

from sqlalchemy import text
from sqlalchemy.orm import Session

log = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Config
# ─────────────────────────────────────────────────────────────────────────────

# A qualifying expansion is one where price moved this many D1 ATRs
# in a sustained way within a short window. Calibrated for gold — the
# average day moves ~1×ATR; a "qualifying" event is one where price
# stretches a full day's range in a directional half-day burst.
DEFAULT_MIN_ATR_MULT      = 1.0      # one full D1 ATR
DEFAULT_MAX_HOURS         = 12       # completed within half a day
DEFAULT_MAX_RETRACE_PCT   = 0.40     # ≤40% peak drawdown during the move
DEFAULT_MIN_HOURS_APART   = 6        # dedupe: two expansions must be ≥6h apart

# Alert-matching windows
_ALERT_MATCH_WINDOW_MIN   = 60       # alerts within ±60 min of expansion start
_LATE_DETECTION_THRESHOLD = 0.25     # >25% of move done at alert = "late"

# Metrics thresholds
_COVERAGE_TARGET_PCT      = 70.0     # brief guideline
_DELAY_TARGET_MEDIAN_MIN  = 30


# ─────────────────────────────────────────────────────────────────────────────
# Data classes
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class Expansion:
    direction:              str        # BULL | BEAR
    started_at:             datetime
    ended_at:               datetime
    trigger_level:          Optional[float]
    total_distance:         float
    atr_multiple:           float
    max_retracement_pct:    float

    def fingerprint(self, instrument: str = "XAU/USD") -> str:
        key = f"{instrument}|{self.direction}|{self.started_at.isoformat()[:16]}|{round(self.total_distance)}"
        return hashlib.sha256(key.encode()).hexdigest()[:16]


@dataclass
class CoverageReport:
    window_start:             datetime
    window_end:               datetime
    total_expansions:         int
    detected_count:           int
    missed_count:             int
    late_detection_count:     int
    bullish_expansions:       int
    bullish_detected:         int
    bearish_expansions:       int
    bearish_detected:         int
    bullish_coverage_pct:     float
    bearish_coverage_pct:     float
    overall_coverage_pct:     float
    median_detection_delay_min: Optional[float]
    false_directional_alerts: int
    direction_accuracy_pct:   Optional[float]
    entry_accuracy_pct:       Optional[float]
    verdict:                  str
    warnings:                 list[str] = field(default_factory=list)
    generated_at:             datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def to_dict(self) -> dict:
        d = asdict(self)
        d["window_start"] = self.window_start.isoformat()
        d["window_end"]   = self.window_end.isoformat()
        d["generated_at"] = self.generated_at.isoformat()
        return d


# ─────────────────────────────────────────────────────────────────────────────
# Detection: find qualifying expansions from H1 candles
# ─────────────────────────────────────────────────────────────────────────────

def _atr_d1(d1_bars: list, n: int = 14) -> Optional[float]:
    if len(d1_bars) < n + 1:
        return None
    trs = []
    for i in range(1, len(d1_bars)):
        h, l, pc = d1_bars[i].high, d1_bars[i].low, d1_bars[i-1].close
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    if len(trs) < n:
        return None
    a = sum(trs[:n]) / n
    for tr in trs[n:]:
        a = ((a * (n - 1)) + tr) / n
    return a


def _find_expansions(
    h1_bars: list,
    atr_d1: float,
    *,
    min_atr_mult: float = DEFAULT_MIN_ATR_MULT,
    max_hours: int = DEFAULT_MAX_HOURS,
    max_retrace_pct: float = DEFAULT_MAX_RETRACE_PCT,
    min_hours_apart: int = DEFAULT_MIN_HOURS_APART,
) -> list[Expansion]:
    """
    Scan H1 bars for sustained directional moves.

    Algorithm:
      For each starting bar i:
        - Look forward up to `max_hours` bars
        - Track running high (for bullish scan) or low (for bearish)
        - Move qualifies if (final_extreme - start_low) ≥ min_atr_mult × atr_d1
        - AND max retracement during move ≤ max_retrace_pct × total_distance
      Then dedupe: keep expansions ≥ min_hours_apart from each other.
    """
    expansions: list[Expansion] = []
    if not h1_bars or len(h1_bars) < 3 or atr_d1 is None or atr_d1 <= 0:
        return expansions

    min_dist = min_atr_mult * atr_d1

    for i in range(len(h1_bars) - 2):
        start_bar = h1_bars[i]

        # ── Bullish scan ────────────────────────────────────────────────
        # Track running peak; retracement = max drop from running-peak-so-far.
        running_peak = start_bar.high
        max_drawdown_from_peak = 0.0
        max_high_idx = i
        for j in range(i + 1, min(len(h1_bars), i + max_hours + 1)):
            b = h1_bars[j]
            if b.high > running_peak:
                running_peak = b.high
                max_high_idx = j
            drawdown = running_peak - b.low
            if drawdown > max_drawdown_from_peak:
                max_drawdown_from_peak = drawdown
            distance = running_peak - start_bar.low
            if distance >= min_dist:
                retrace_pct = max_drawdown_from_peak / distance if distance > 0 else 1.0
                if retrace_pct <= max_retrace_pct:
                    expansions.append(Expansion(
                        direction="BULL",
                        started_at=start_bar.time,
                        ended_at=h1_bars[max_high_idx].time,
                        trigger_level=start_bar.high,
                        total_distance=round(distance, 2),
                        atr_multiple=round(distance / atr_d1, 2),
                        max_retracement_pct=round(retrace_pct * 100, 1),
                    ))
                    break

        # ── Bearish scan (mirror) ───────────────────────────────────────
        running_trough = start_bar.low
        max_rally_from_trough = 0.0
        min_low_idx = i
        for j in range(i + 1, min(len(h1_bars), i + max_hours + 1)):
            b = h1_bars[j]
            if b.low < running_trough:
                running_trough = b.low
                min_low_idx = j
            rally = b.high - running_trough
            if rally > max_rally_from_trough:
                max_rally_from_trough = rally
            distance = start_bar.high - running_trough
            if distance >= min_dist:
                rally_pct = max_rally_from_trough / distance if distance > 0 else 1.0
                if rally_pct <= max_retrace_pct:
                    expansions.append(Expansion(
                        direction="BEAR",
                        started_at=start_bar.time,
                        ended_at=h1_bars[min_low_idx].time,
                        trigger_level=start_bar.low,
                        total_distance=round(distance, 2),
                        atr_multiple=round(distance / atr_d1, 2),
                        max_retracement_pct=round(rally_pct * 100, 1),
                    ))
                    break

    # Dedupe: sort by start, drop entries too close
    expansions.sort(key=lambda e: e.started_at)
    deduped: list[Expansion] = []
    for e in expansions:
        if deduped and (e.started_at - deduped[-1].started_at) < timedelta(hours=min_hours_apart):
            # Same direction near neighbour — keep the larger
            if e.direction == deduped[-1].direction:
                if e.total_distance > deduped[-1].total_distance:
                    deduped[-1] = e
                continue
        deduped.append(e)
    return deduped


# ─────────────────────────────────────────────────────────────────────────────
# Alert matching
# ─────────────────────────────────────────────────────────────────────────────

def _find_first_matching_alert(db: Session, expansion: Expansion) -> tuple[Optional[datetime], Optional[str], Optional[str]]:
    """
    Return (first_alert_ts, source, alert_type) for the earliest alert
    within ±_ALERT_MATCH_WINDOW_MIN of expansion.started_at whose direction
    aligns with expansion.direction. None if no matching alert found.
    """
    window_start = expansion.started_at - timedelta(minutes=_ALERT_MATCH_WINDOW_MIN)
    window_end   = expansion.ended_at   + timedelta(minutes=_ALERT_MATCH_WINDOW_MIN)

    # 1) Intel alerts (Phase 11)
    intel_alert_ts = None
    intel_type = None
    try:
        direction_pattern = "%BULLISH%" if expansion.direction == "BULL" else "%BEARISH%"
        row = db.execute(text(
            "SELECT ts, alert_type FROM market_intelligence_alerts "
            "WHERE ts BETWEEN :s AND :e AND delivery_result IN ('sent','shadow') "
            "AND alert_type LIKE :d ORDER BY ts ASC LIMIT 1"
        ), {"s": window_start, "e": window_end, "d": direction_pattern}).fetchone()
        if row is not None:
            intel_alert_ts = row[0] if isinstance(row[0], datetime) else _parse_ts(row[0])
            intel_type = row[1]
    except Exception as exc:
        log.debug("[opp-coverage] intel alert lookup failed: %s", exc)

    # 2) Strategist verdicts (mandate)
    strategist_alert_ts = None
    strategist_type = None
    try:
        decision = "BUY" if expansion.direction == "BULL" else "SELL"
        row = db.execute(text(
            "SELECT created_at, decision FROM strategist_verdicts "
            "WHERE created_at BETWEEN :s AND :e AND decision = :d "
            "AND conditions_passed >= 3 ORDER BY created_at ASC LIMIT 1"
        ), {"s": window_start, "e": window_end, "d": decision}).fetchone()
        if row is not None:
            strategist_alert_ts = row[0] if isinstance(row[0], datetime) else _parse_ts(row[0])
            strategist_type = f"STRATEGIST_{decision}"
    except Exception as exc:
        log.debug("[opp-coverage] strategist verdict lookup failed: %s", exc)

    # Pick the earliest of the two
    candidates = [(ts, src, tp) for ts, src, tp in
                    ((intel_alert_ts, "intel", intel_type),
                     (strategist_alert_ts, "strategist", strategist_type))
                    if ts is not None]
    if not candidates:
        return (None, None, None)
    candidates.sort(key=lambda c: c[0])
    return candidates[0]


def _parse_ts(s):
    if isinstance(s, datetime):
        return s if s.tzinfo else s.replace(tzinfo=timezone.utc)
    if isinstance(s, str):
        try:
            return datetime.fromisoformat(s.replace("Z", "+00:00").split("+")[0]).replace(tzinfo=timezone.utc)
        except Exception:
            return None
    return None


def _pct_of_move_captured(expansion: Expansion, alert_ts: Optional[datetime]) -> Optional[float]:
    """If alert fired X min into a Y-min move, we captured (Y-X)/Y of it."""
    if alert_ts is None:
        return None
    total_min = (expansion.ended_at - expansion.started_at).total_seconds() / 60
    if total_min <= 0:
        return 100.0
    delay_min = (alert_ts - expansion.started_at).total_seconds() / 60
    if delay_min <= 0:
        return 100.0    # alert BEFORE the move started
    if delay_min >= total_min:
        return 0.0
    return round((1.0 - delay_min / total_min) * 100.0, 1)


# ─────────────────────────────────────────────────────────────────────────────
# Persistence
# ─────────────────────────────────────────────────────────────────────────────

def _upsert_expansion(db: Session, expansion: Expansion, *, alert_ts: Optional[datetime],
                       alert_source: Optional[str], alert_type: Optional[str],
                       instrument: str = "XAU/USD") -> int:
    """Idempotent upsert by fingerprint. Returns row id."""
    from db_models import QualifyingExpansion, ExpansionAlertMatch
    fp = expansion.fingerprint(instrument)
    row = db.execute(text(
        "SELECT id FROM qualifying_expansions WHERE fingerprint=:f LIMIT 1"
    ), {"f": fp}).fetchone()
    detection_delay = None
    pct_captured = _pct_of_move_captured(expansion, alert_ts)
    if alert_ts is not None:
        detection_delay = (alert_ts - expansion.started_at).total_seconds() / 60
    was_extended = detection_delay is not None and detection_delay > (
        (expansion.ended_at - expansion.started_at).total_seconds() / 60) * _LATE_DETECTION_THRESHOLD

    if row:
        # Update if we have new alert info
        db.execute(text(
            "UPDATE qualifying_expansions SET detected=:d, "
            "first_intel_alert_at=:ia, first_entry_alert_at=:ea, "
            "detection_delay_min=:dl, pct_of_move_captured=:pc, "
            "was_extended_at_detect=:we "
            "WHERE id=:id"
        ), {"d": alert_ts is not None,
              "ia": alert_ts if alert_source == "intel" else None,
              "ea": alert_ts if alert_source == "strategist" else None,
              "dl": detection_delay, "pc": pct_captured,
              "we": was_extended, "id": row[0]})
        db.commit()
        expansion_id = row[0]
    else:
        new_row = QualifyingExpansion(
            instrument=instrument, direction=expansion.direction,
            started_at=expansion.started_at, ended_at=expansion.ended_at,
            trigger_level=expansion.trigger_level,
            total_distance=expansion.total_distance,
            atr_multiple=expansion.atr_multiple,
            max_retracement=expansion.max_retracement_pct,
            detected=alert_ts is not None,
            first_intel_alert_at=alert_ts if alert_source == "intel" else None,
            first_entry_alert_at=alert_ts if alert_source == "strategist" else None,
            detection_delay_min=detection_delay,
            pct_of_move_captured=pct_captured,
            was_extended_at_detect=was_extended,
            fingerprint=fp,
        )
        db.add(new_row)
        db.commit()
        expansion_id = new_row.id

    if alert_ts is not None and alert_source is not None:
        # Add the match row (dedupe on unique combo)
        exists = db.execute(text(
            "SELECT id FROM expansion_alert_matches "
            "WHERE expansion_id=:e AND alert_source=:s AND alert_sent_at=:ts"
        ), {"e": expansion_id, "s": alert_source, "ts": alert_ts}).fetchone()
        if not exists:
            match = ExpansionAlertMatch(
                expansion_id=expansion_id, alert_source=alert_source,
                alert_type=alert_type or "?",
                alert_sent_at=alert_ts,
                delay_seconds=int(detection_delay * 60) if detection_delay is not None else 0,
            )
            db.add(match); db.commit()
    return expansion_id


# ─────────────────────────────────────────────────────────────────────────────
# Public: scan + score
# ─────────────────────────────────────────────────────────────────────────────

def detect_and_score(
    db: Session, *,
    lookback_hours: int = 168,       # default 7 days
    instrument: str = "XAU/USD",
) -> dict:
    """
    Pull H1 candles for the lookback window, detect qualifying expansions,
    match each to alerts, persist.
    Returns { scanned, detected_new, matched_alerts, warnings }.
    """
    from services.canonical_market_data import _fetch_bars

    warnings: list[str] = []
    # Get H1 bars — need enough for a proper D1 ATR baseline
    h1 = _fetch_bars(db, instrument, "H1", lookback_hours + 24)
    d1 = _fetch_bars(db, instrument, "D1", 30)
    if not h1 or not d1:
        warnings.append(f"insufficient bars (h1={len(h1)}, d1={len(d1)})")
        return {"scanned": 0, "detected_new": 0, "matched_alerts": 0,
                 "warnings": warnings}

    atr_d1 = _atr_d1(d1, 14)
    if atr_d1 is None:
        warnings.append("could not compute D1 ATR")
        return {"scanned": 0, "detected_new": 0, "matched_alerts": 0,
                 "warnings": warnings}

    # Filter H1 to lookback window
    cutoff = datetime.now(timezone.utc) - timedelta(hours=lookback_hours)
    h1_window = [b for b in h1 if b.time >= cutoff]

    expansions = _find_expansions(h1_window, atr_d1)

    detected_new = 0
    matched_alerts = 0
    for exp in expansions:
        alert_ts, alert_source, alert_type = _find_first_matching_alert(db, exp)
        _upsert_expansion(db, exp, alert_ts=alert_ts,
                            alert_source=alert_source, alert_type=alert_type,
                            instrument=instrument)
        if alert_ts is not None:
            matched_alerts += 1
        detected_new += 1

    return {
        "scanned": len(h1_window),
        "detected_new": detected_new,
        "matched_alerts": matched_alerts,
        "atr_d1": round(atr_d1, 2),
        "warnings": warnings,
    }


def compute_coverage_report(db: Session, *, days: int = 30,
                              instrument: str = "XAU/USD") -> CoverageReport:
    """Compute the multi-metric coverage report over the last `days`."""
    now = datetime.now(timezone.utc)
    window_start = now - timedelta(days=days)
    warnings: list[str] = []
    try:
        rows = db.execute(text(
            "SELECT direction, detected, detection_delay_min, "
            "pct_of_move_captured, was_extended_at_detect "
            "FROM qualifying_expansions "
            "WHERE instrument=:i AND started_at >= :s"
        ), {"i": instrument, "s": window_start}).fetchall()
    except Exception as exc:
        warnings.append(f"expansion query failed: {exc}")
        rows = []

    total = len(rows)
    detected = sum(1 for r in rows if r[1])
    missed = total - detected
    bull = [r for r in rows if r[0] == "BULL"]
    bear = [r for r in rows if r[0] == "BEAR"]
    bull_det = sum(1 for r in bull if r[1])
    bear_det = sum(1 for r in bear if r[1])
    delays = [float(r[2]) for r in rows if r[2] is not None]
    late = sum(1 for r in rows if r[4])
    bull_cov = round(bull_det / len(bull) * 100, 1) if bull else 0.0
    bear_cov = round(bear_det / len(bear) * 100, 1) if bear else 0.0
    overall = round(detected / total * 100, 1) if total else 0.0
    med_delay = round(median(delays), 1) if delays else None

    # False directional alerts: intel alerts in the window WITHOUT a matched expansion
    false_alerts = 0
    try:
        r = db.execute(text(
            "SELECT COUNT(*) FROM market_intelligence_alerts "
            "WHERE ts >= :s AND delivery_result IN ('sent','shadow') "
            "AND alert_type IN ('BULLISH_TRANSITION_DETECTED', "
            "'BEARISH_TRANSITION_DETECTED', 'BULLISH_BREAKOUT_ACCEPTANCE_CONFIRMED', "
            "'BEARISH_BREAKDOWN_ACCEPTANCE_CONFIRMED')"
        ), {"s": window_start}).fetchone()
        total_directional = int(r[0]) if r else 0
        # Matched = distinct alerts referenced in matches
        r2 = db.execute(text(
            "SELECT COUNT(DISTINCT alert_sent_at) FROM expansion_alert_matches "
            "WHERE alert_source='intel' AND alert_sent_at >= :s"
        ), {"s": window_start}).fetchone()
        matched_alerts_count = int(r2[0]) if r2 else 0
        false_alerts = max(0, total_directional - matched_alerts_count)
    except Exception as exc:
        warnings.append(f"false-alert query failed: {exc}")

    # Direction / entry accuracy from strategist_verdicts closed trades
    dir_acc = ent_acc = None
    try:
        # Not implemented yet — leave as None for now; will be filled by
        # a future analytics query linking verdicts to outcomes.
        pass
    except Exception:
        pass

    # Verdict
    verdict = _judge(overall, med_delay, missed, total, late)

    return CoverageReport(
        window_start=window_start, window_end=now,
        total_expansions=total, detected_count=detected,
        missed_count=missed, late_detection_count=late,
        bullish_expansions=len(bull), bullish_detected=bull_det,
        bearish_expansions=len(bear), bearish_detected=bear_det,
        bullish_coverage_pct=bull_cov, bearish_coverage_pct=bear_cov,
        overall_coverage_pct=overall,
        median_detection_delay_min=med_delay,
        false_directional_alerts=false_alerts,
        direction_accuracy_pct=dir_acc, entry_accuracy_pct=ent_acc,
        verdict=verdict, warnings=warnings,
    )


def _judge(overall_pct: float, med_delay: Optional[float],
             missed: int, total: int, late: int) -> str:
    if total < 5:
        return "INSUFFICIENT_SAMPLE — need at least 5 qualifying expansions"
    if overall_pct >= _COVERAGE_TARGET_PCT and (med_delay is None or med_delay <= _DELAY_TARGET_MEDIAN_MIN):
        return f"ON TARGET — coverage {overall_pct}% ≥ {_COVERAGE_TARGET_PCT}%, delay OK"
    if overall_pct < 40:
        return f"UNDER-DETECTING — coverage {overall_pct}% is materially below target"
    if med_delay is not None and med_delay > _DELAY_TARGET_MEDIAN_MIN * 2:
        return f"LATE DETECTION — median delay {med_delay}min > {_DELAY_TARGET_MEDIAN_MIN*2}min"
    return f"BELOW TARGET — coverage {overall_pct}% (target {_COVERAGE_TARGET_PCT}%)"


def missed_expansions(db: Session, *, days: int = 30,
                        instrument: str = "XAU/USD") -> list[dict]:
    """Return the list of expansions with no matched alert — the misses."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    try:
        rows = db.execute(text(
            "SELECT direction, started_at, ended_at, total_distance, "
            "atr_multiple, max_retracement, blocking_filter "
            "FROM qualifying_expansions "
            "WHERE instrument=:i AND started_at >= :c AND detected = 0 "
            "ORDER BY started_at DESC LIMIT 100"
        ), {"i": instrument, "c": cutoff}).fetchall()
    except Exception as exc:
        log.warning("[opp-coverage] missed query failed: %s", exc)
        return []
    return [{
        "direction": r[0],
        "started_at": (r[1].isoformat() if hasattr(r[1], "isoformat") else str(r[1])),
        "ended_at":   (r[2].isoformat() if hasattr(r[2], "isoformat") else str(r[2])),
        "total_distance": r[3], "atr_multiple": r[4],
        "max_retracement_pct": r[5], "blocking_filter": r[6],
    } for r in rows]


__all__ = [
    "detect_and_score", "compute_coverage_report", "missed_expansions",
    "Expansion", "CoverageReport",
]
