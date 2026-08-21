"""
XAUUSD forward opportunity coverage classifier.

OBSERVATION ONLY — never feeds signal generation.

Answers:
  For each canonical UP/DOWN market expansion, did the current strategy
  library (Predator + Strategist) detect it before or during the move?

Deterministic canonical expansion detection (no overlap):
  Walk M5 bars forward. Start tracking peak/trough from each price pivot.
  Emit an expansion event when excursion from origin exceeds a threshold.
  Once emitted, the event's "lifetime" is until price retraces >50% of
  the excursion. New expansions in the same direction from within an
  active event are absorbed. Opposite-direction events start fresh.

Attribution (per event):
  Scan Predator batches + Strategist verdicts for records within a
  detection window: [expansion_start - 60min, expansion_start + 20min].
  Classify by highest-authority match:
    EXECUTED_OPPORTUNITY               → engine actually filled
    DETECTED_NOT_EXECUTED_PORTFOLIO    → engine approved but capacity/dedupe/etc.
    DETECTED_SHADOW_ONLY               → Strategist SELL shadow only
    DETECTED_BELOW_EXECUTION_THRESHOLD → 3/5 watchlist etc.
    MARKET_STATE_ONLY                  → regime engine saw direction, no strat opp
    UNREPRESENTED_MARKET_MOVE          → nothing recognized it
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone, timedelta
from typing import Optional

from sqlalchemy import text
from sqlalchemy.orm import Session

log = logging.getLogger(__name__)

THRESHOLDS = (20, 30, 50, 70)
RETRACE_END_PCT = 0.50
DETECT_LOOKBACK_MIN = 60
DETECT_LOOKFWD_MIN = 20


def _session(hr: int) -> str:
    if 0 <= hr < 7:   return "ASIA"
    if 7 <= hr < 12:  return "LONDON"
    if 12 <= hr < 16: return "NY_OPEN"
    if 16 <= hr < 22: return "NY_PM"
    return "ROLLOVER"


def _parse_ts(s) -> Optional[datetime]:
    if s is None: return None
    if isinstance(s, datetime):
        return s if s.tzinfo else s.replace(tzinfo=timezone.utc)
    ss = str(s).replace("T"," ").split(".")[0].split("+")[0]
    try:
        d = datetime.strptime(ss, "%Y-%m-%d %H:%M:%S")
        return d.replace(tzinfo=timezone.utc)
    except Exception: return None


def detect_and_seed_expansions(db: Session, since_days: int = 5) -> dict:
    """Scan M5 bars from the last `since_days` days, detect canonical UP/DOWN
    expansions, and insert missing xauusd_forward_coverage rows.
    Idempotent — existing event_ids are skipped."""
    stats = dict(bars_scanned=0, events_created=0, events_skipped_existing=0)
    try:
        bars = db.execute(text("""
            SELECT candle_time, open, high, low, close
            FROM historical_candles
            WHERE instrument='XAU/USD' AND timeframe='M5'
              AND candle_time >= datetime('now', :d)
            ORDER BY candle_time ASC
        """), {"d": f"-{since_days} days"}).fetchall()
        stats["bars_scanned"] = len(bars)
        if not bars: return stats

        # Track active expansions per direction and per threshold
        for thr in THRESHOLDS:
            for direction in ("UP", "DOWN"):
                # Anchor scan: at each bar consider it a candidate origin
                origin_idx = 0
                origin_price = float(bars[0][1])  # open
                origin_ts = _parse_ts(bars[0][0])
                if not origin_ts: continue

                # Precompute price arrays
                highs = [float(b[2]) for b in bars]
                lows = [float(b[3]) for b in bars]
                closes = [float(b[4]) for b in bars]
                times = [_parse_ts(b[0]) for b in bars]

                i = 0
                while i < len(bars):
                    origin_price = closes[i]
                    origin_ts = times[i]
                    if not origin_ts: i += 1; continue

                    # Look forward for expansion of `thr` points in `direction`
                    peak = origin_price
                    peak_idx = i
                    triggered = False
                    for j in range(i+1, min(i+96, len(bars))):  # 8h window
                        if direction == "UP":
                            if highs[j] > peak:
                                peak = highs[j]; peak_idx = j
                            excursion = peak - origin_price
                            adverse = origin_price - lows[j]
                        else:
                            if lows[j] < peak or peak == origin_price:
                                if lows[j] < peak:
                                    peak = lows[j]; peak_idx = j
                            excursion = origin_price - peak
                            adverse = highs[j] - origin_price
                        if excursion >= thr:
                            triggered = True; break
                        # Stop if adverse move exceeds threshold before expansion
                        if adverse >= thr * 0.7:
                            break
                    if not triggered:
                        i += 1; continue

                    # Insert canonical event
                    peak_ts = times[peak_idx]
                    total_exc = abs(peak - origin_price)
                    event_id = (f"{direction}{thr}·"
                                f"{origin_ts.strftime('%Y%m%d·%H%M')}·"
                                f"{round(origin_price):.0f}")
                    existing = db.execute(text(
                        "SELECT 1 FROM xauusd_forward_coverage WHERE event_id=:e"
                    ), {"e": event_id}).fetchone()
                    if existing:
                        stats["events_skipped_existing"] += 1
                    else:
                        db.execute(text("""
                            INSERT INTO xauusd_forward_coverage
                              (created_at, event_id, direction, threshold_pts,
                               expansion_start_at, expansion_start_price,
                               expansion_peak_at, expansion_peak_price,
                               total_excursion_pts, session, classification)
                            VALUES
                              (:ca, :e, :d, :t, :sa, :sp, :pa, :pp, :te, :se, 'PENDING')
                        """), dict(
                            ca=datetime.now(timezone.utc),
                            e=event_id, d=direction, t=thr,
                            sa=origin_ts, sp=origin_price,
                            pa=peak_ts, pp=peak, te=total_exc,
                            se=_session(origin_ts.hour),
                        ))
                        db.commit()
                        stats["events_created"] += 1

                    # Skip forward past the end of this expansion (until retrace)
                    i = peak_idx + 1
    except Exception as exc:
        log.warning("[forward_coverage] detect failed: %s", exc)
        try: db.rollback()
        except Exception: pass
    return stats


def classify_pending_events(db: Session, limit: int = 200) -> dict:
    """Classify PENDING xauusd_forward_coverage rows against actual
    Predator + Strategist detection records."""
    stats = dict(scanned=0, classified=0, per_class={})
    try:
        rows = db.execute(text("""
            SELECT id, event_id, direction, threshold_pts,
                   expansion_start_at, expansion_start_price
            FROM xauusd_forward_coverage
            WHERE classification IN ('PENDING', NULL)
            ORDER BY expansion_start_at DESC LIMIT :n
        """), {"n": limit}).fetchall()

        for r in rows:
            stats["scanned"] += 1
            try:
                row_id, event_id, direction, thr, exp_at, exp_price = r
                exp_dt = _parse_ts(exp_at)
                if not exp_dt: continue
                window_start = exp_dt - timedelta(minutes=DETECT_LOOKBACK_MIN)
                window_end = exp_dt + timedelta(minutes=DETECT_LOOKFWD_MIN)

                attribution = _attribute_event(db, direction, window_start, window_end, exp_dt)
                classification = _classify(attribution)

                db.execute(text("""
                    UPDATE xauusd_forward_coverage SET
                      classification = :c, updated_at = :u,
                      predator_detected = :pd, predator_executed = :pe,
                      strategist_detected = :sd, strategist_executed = :se,
                      strategist_below_threshold = :sb,
                      strategist_sell_shadow = :ss,
                      first_detection_engine = :fe, first_detection_at = :ft,
                      lead_time_min = :lt
                    WHERE id = :i
                """), dict(
                    c=classification, u=datetime.now(timezone.utc),
                    pd=1 if attribution.get("predator_detected") else 0,
                    pe=1 if attribution.get("predator_executed") else 0,
                    sd=1 if attribution.get("strategist_detected") else 0,
                    se=1 if attribution.get("strategist_executed") else 0,
                    sb=1 if attribution.get("strategist_below_threshold") else 0,
                    ss=1 if attribution.get("strategist_sell_shadow") else 0,
                    fe=attribution.get("first_engine"),
                    ft=attribution.get("first_at"),
                    lt=attribution.get("lead_time_min"),
                    i=row_id,
                ))
                db.commit()
                stats["classified"] += 1
                stats["per_class"][classification] = stats["per_class"].get(classification, 0) + 1
            except Exception as _row_exc:
                stats.setdefault("row_errors", []).append(f"row {r[0] if len(r) > 0 else '?'}: {_row_exc}")
                try: db.rollback()
                except Exception: pass
                continue
    except Exception as exc:
        log.warning("[forward_coverage] classify failed: %s", exc)
        try: db.rollback()
        except Exception: pass
    return stats


def _attribute_event(db: Session, direction: str, window_start: datetime,
                     window_end: datetime, exp_start: datetime) -> dict:
    """Find any Predator/Strategist detection records within the window."""
    attr = dict(predator_detected=False, predator_executed=False,
                strategist_detected=False, strategist_executed=False,
                strategist_below_threshold=False, strategist_sell_shadow=False,
                first_engine=None, first_at=None, lead_time_min=None)
    firsts = []  # (engine_label, timestamp)

    # PREDATOR SELL — for DOWN events
    if direction == "DOWN":
        try:
            rows = db.execute(text("""
                SELECT created_at, execution_status FROM predator_signal_batches
                WHERE direction='SELL'
                  AND created_at BETWEEN :ws AND :we
                ORDER BY created_at ASC LIMIT 20
            """), {"ws": window_start, "we": window_end}).fetchall()
            for r in rows:
                attr["predator_detected"] = True
                if str(r[1]) in ("COMPLETE", "PARTIAL"):
                    attr["predator_executed"] = True
                firsts.append(("PREDATOR", _parse_ts(r[0])))
        except Exception: pass

    # STRATEGIST BUY — for UP events
    if direction == "UP":
        try:
            rows = db.execute(text("""
                SELECT created_at, execution_status, conditions_passed FROM strategist_verdicts
                WHERE decision='BUY' AND created_at BETWEEN :ws AND :we
                  AND conditions_passed >= 3
                ORDER BY created_at ASC LIMIT 20
            """), {"ws": window_start, "we": window_end}).fetchall()
            for r in rows:
                attr["strategist_detected"] = True
                if str(r[1]) == "DEMO_TRADE_PLACED":
                    attr["strategist_executed"] = True
                elif (r[2] or 0) == 3:
                    attr["strategist_below_threshold"] = True
                firsts.append(("STRATEGIST_BUY", _parse_ts(r[0])))
        except Exception: pass

    # STRATEGIST SELL SHADOW — for DOWN events
    if direction == "DOWN":
        try:
            rows = db.execute(text("""
                SELECT created_at FROM strategist_verdicts
                WHERE decision='SELL' AND created_at BETWEEN :ws AND :we
                  AND conditions_passed >= 3
                ORDER BY created_at ASC LIMIT 20
            """), {"ws": window_start, "we": window_end}).fetchall()
            for r in rows:
                attr["strategist_sell_shadow"] = True
                firsts.append(("STRATEGIST_SELL_SHADOW", _parse_ts(r[0])))
        except Exception: pass

    # First relevant detection
    firsts = [(e, t) for e, t in firsts if t]
    if firsts:
        firsts.sort(key=lambda x: x[1])
        eng, tm = firsts[0]
        attr["first_engine"] = eng
        attr["first_at"] = tm
        try:
            attr["lead_time_min"] = round((exp_start - tm).total_seconds() / 60, 1)
        except Exception: pass

    return attr


def _classify(attr: dict) -> str:
    """Highest-authority classification."""
    if attr.get("predator_executed") or attr.get("strategist_executed"):
        return "EXECUTED_OPPORTUNITY"
    if attr.get("predator_detected") or attr.get("strategist_detected"):
        return "DETECTED_NOT_EXECUTED_PORTFOLIO"
    if attr.get("strategist_sell_shadow"):
        return "DETECTED_SHADOW_ONLY"
    if attr.get("strategist_below_threshold"):
        return "DETECTED_BELOW_EXECUTION_THRESHOLD"
    # market-state check would require regime lookup at expansion_start;
    # for simplicity, default remaining events to UNREPRESENTED.
    return "UNREPRESENTED_MARKET_MOVE"


def coverage_summary(db: Session, since_days: int = 7) -> dict:
    """Aggregate coverage rates for the daily/weekly report."""
    out = dict(period_days=since_days, by_direction={})
    try:
        for direction in ("UP", "DOWN"):
            direction_summary = dict(by_threshold={})
            for thr in THRESHOLDS:
                rows = db.execute(text("""
                    SELECT classification, COUNT(*) FROM xauusd_forward_coverage
                    WHERE direction=:d AND threshold_pts=:t
                      AND expansion_start_at >= datetime('now', :sd)
                    GROUP BY classification
                """), {"d": direction, "t": thr, "sd": f"-{since_days} days"}).fetchall()
                counts = {r[0]: int(r[1]) for r in rows}
                n = sum(counts.values())
                direction_summary["by_threshold"][thr] = dict(
                    n=n,
                    executed=counts.get("EXECUTED_OPPORTUNITY", 0),
                    detected_blocked=counts.get("DETECTED_NOT_EXECUTED_PORTFOLIO", 0),
                    shadow_only=counts.get("DETECTED_SHADOW_ONLY", 0),
                    below_threshold=counts.get("DETECTED_BELOW_EXECUTION_THRESHOLD", 0),
                    market_state_only=counts.get("MARKET_STATE_ONLY", 0),
                    unrepresented=counts.get("UNREPRESENTED_MARKET_MOVE", 0),
                    detection_coverage_pct=round(100 * (n - counts.get("UNREPRESENTED_MARKET_MOVE", 0)) / max(1, n), 1),
                    execution_pct=round(100 * counts.get("EXECUTED_OPPORTUNITY", 0) / max(1, n), 1),
                )
            out["by_direction"][direction] = direction_summary
    except Exception as exc:
        log.warning("[forward_coverage] summary failed: %s", exc)
    return out


def latest_unrepresented(db: Session, direction: str, limit: int = 10) -> list:
    """Return the largest recent unrepresented moves for research queue."""
    try:
        rows = db.execute(text("""
            SELECT event_id, threshold_pts, expansion_start_at, total_excursion_pts, session
            FROM xauusd_forward_coverage
            WHERE direction=:d AND classification='UNREPRESENTED_MARKET_MOVE'
              AND expansion_start_at >= datetime('now', '-14 days')
            ORDER BY total_excursion_pts DESC LIMIT :n
        """), {"d": direction, "n": limit}).fetchall()
        return [dict(event_id=r[0], threshold_pts=r[1], expansion_start_at=str(r[2]),
                     total_excursion_pts=r[3], session=r[4]) for r in rows]
    except Exception:
        return []
