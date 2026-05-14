"""
Paper Observation Tracker — PAPER_OBSERVATION_ONLY mode.

Auto-logs every SIGNAL_READY scanner state into the paper_observations
table without manual confirmation. Forward-resolves outcomes against
future H4 candles. Acts as the second validation dataset alongside the
backtest.

Workflow
--------
1. Scanner runs (auto every 60s or manual force)
2. When marketState == SIGNAL_READY, log_observation() is called
3. Dedupe by fingerprint (signal+entry+SL+TP+score) — one row per unique setup
4. Periodically, resolve_pending() walks unresolved observations forward
   through recent H4 candles to determine WIN/LOSS/EXPIRED
5. get_observation_stats() reports running win rate, expectancy R, and
   progress toward the n>=30 certification threshold

Safety
------
This module does NOT:
  - Place trades or call MT5
  - Send Telegram alerts (use telegram_alert_service for that)
  - Recommend live execution
  - Modify the signal engine's gate logic

Observations are passive records — diagnostic input for the validator only.
"""
from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy.orm import Session

from db_models import PaperObservation

log = logging.getLogger(__name__)

# Dedupe window — once a fingerprint is logged, suppress duplicates for this duration
DEDUPE_COOLDOWN_HOURS = 4    # H4 candle duration — same setup persists across scans

# Forward-resolution max holding period
MAX_RESOLUTION_BARS = 96     # 96 H4 bars = 16 days


# ═══════════════════════════════════════════════════════════════════════════════
# Logging
# ═══════════════════════════════════════════════════════════════════════════════

def log_observation(db: Session, scan_result: dict) -> Optional[int]:
    """
    Persist a scanner SIGNAL_READY state as a paper observation.
    Returns the new row id, or None if duplicate / not applicable.

    Called by institutional_scanner.scan_xauusd_market() after a scan
    completes with marketState == "SIGNAL_READY".
    """
    state  = scan_result.get("marketState")
    signal = scan_result.get("signal")
    rec    = scan_result.get("recommendedAction", {})
    plan   = (rec or {}).get("tradePlan") if rec else None

    if state != "SIGNAL_READY" or signal not in ("BUY", "SELL"):
        return None
    if not plan or plan.get("entry") is None or plan.get("stopLoss") is None or plan.get("takeProfit") is None:
        return None

    fingerprint = _build_fingerprint(plan, signal)

    # Suppress duplicates within cooldown window
    cutoff = datetime.now(timezone.utc) - timedelta(hours=DEDUPE_COOLDOWN_HOURS)
    existing = (
        db.query(PaperObservation)
          .filter(PaperObservation.fingerprint == fingerprint)
          .filter(PaperObservation.observed_at >= cutoff)
          .first()
    )
    if existing:
        log.debug("[paper_obs] Duplicate fingerprint %s — suppressed", fingerprint)
        return None

    row = PaperObservation(
        instrument=scan_result.get("instrument", "XAU/USD"),
        timeframe="H4",
        signal=signal,
        entry=float(plan["entry"]),
        stop_loss=float(plan["stopLoss"]),
        take_profit=float(plan["takeProfit"]),
        risk_points=plan.get("riskPoints"),
        target_points=plan.get("targetPoints"),
        rr=plan.get("rr"),
        score=plan.get("qualityScore"),
        session=plan.get("session"),
        setup_type=scan_result.get("engineModel", {}).get("setupType") or "unknown",
        market_state=state,
        confidence=scan_result.get("confidence"),
        grade=scan_result.get("grade"),
        fingerprint=fingerprint,
        engine_model_json=json.dumps(scan_result.get("engineModel", {})),
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    log.info(
        "[paper_obs] Logged observation id=%d signal=%s entry=%.2f SL=%.2f TP=%.2f score=%s",
        row.id, signal, row.entry, row.stop_loss, row.take_profit, row.score,
    )
    return row.id


def _build_fingerprint(plan: dict, signal: str) -> str:
    """Stable hash of signal + price levels + score."""
    raw = "{}:{}:{:.2f}:{:.2f}:{:.2f}:{}".format(
        signal,
        plan.get("direction", ""),
        float(plan.get("entry", 0)),
        float(plan.get("stopLoss", 0)),
        float(plan.get("takeProfit", 0)),
        plan.get("qualityScore", 0),
    )
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


# ═══════════════════════════════════════════════════════════════════════════════
# Forward resolution
# ═══════════════════════════════════════════════════════════════════════════════

def resolve_pending(db: Session, max_observations: int = 100) -> dict:
    """
    Walk unresolved observations forward through historical candles and
    determine each one's outcome (WIN/LOSS/EXPIRED).

    Conservative collision rule: if a single candle touches both TP and SL,
    count it as LOSS (intra-bar order unknown).
    """
    from services.historical_data_provider import get_historical_candles

    pending = (
        db.query(PaperObservation)
          .filter(PaperObservation.result.is_(None))
          .filter(PaperObservation.observed_at < datetime.now(timezone.utc) - timedelta(hours=4))
          .order_by(PaperObservation.observed_at.asc())
          .limit(max_observations)
          .all()
    )
    if not pending:
        return {"resolved": 0, "pending": 0, "note": "No unresolved observations >4h old"}

    # Pull recent historical candles for forward simulation
    candles, source = get_historical_candles(
        db=db, timeframe="H4", lookback=2000, allow_synthetic_fallback=False,
    )
    if not candles:
        return {"resolved": 0, "pending": len(pending),
                "note": "No H4 historical candles in DB — run /backtest/fetch-tradingview first"}

    # Index candles by time for fast forward-walk
    candle_times = [c.time if c.time.tzinfo else c.time.replace(tzinfo=timezone.utc)
                    for c in candles]

    resolved_count = 0
    still_pending = 0

    for obs in pending:
        obs_t = obs.observed_at
        if obs_t.tzinfo is None:
            obs_t = obs_t.replace(tzinfo=timezone.utc)

        # Find first candle strictly AFTER observation time
        start_idx = None
        for i, t in enumerate(candle_times):
            if t > obs_t:
                start_idx = i
                break
        if start_idx is None:
            still_pending += 1
            continue

        # Walk forward looking for TP or SL hit
        end_idx = min(start_idx + MAX_RESOLUTION_BARS, len(candles) - 1)
        result_label = None
        exit_price   = None
        bars_held    = 0

        for j in range(start_idx, end_idx + 1):
            bar = candles[j]
            if obs.signal == "BUY":
                tp_hit = bar.high >= obs.take_profit
                sl_hit = bar.low  <= obs.stop_loss
            else:
                tp_hit = bar.low  <= obs.take_profit
                sl_hit = bar.high >= obs.stop_loss

            if tp_hit and sl_hit:
                result_label = "LOSS"
                exit_price = obs.stop_loss
                bars_held = j - start_idx + 1
                break
            if tp_hit:
                result_label = "WIN"
                exit_price = obs.take_profit
                bars_held = j - start_idx + 1
                break
            if sl_hit:
                result_label = "LOSS"
                exit_price = obs.stop_loss
                bars_held = j - start_idx + 1
                break

        if result_label is None:
            # Still no resolution — check if we've genuinely exhausted the holding window
            if end_idx - start_idx >= MAX_RESOLUTION_BARS - 1 and end_idx == len(candles) - 1:
                # Out of future candles — leave pending until new data arrives
                still_pending += 1
                continue
            elif end_idx - start_idx >= MAX_RESOLUTION_BARS - 1:
                # Held to full 16 days without hitting TP or SL — EXPIRED
                result_label = "EXPIRED"
                exit_price = candles[end_idx].close
                bars_held = MAX_RESOLUTION_BARS
            else:
                still_pending += 1
                continue

        # Compute points and r_multiple
        if obs.signal == "BUY":
            points = exit_price - obs.entry
        else:
            points = obs.entry - exit_price

        risk = obs.risk_points or abs(obs.entry - obs.stop_loss)
        r_multiple = (points / risk) if risk > 0 else 0.0

        obs.result          = result_label
        obs.exit_price      = round(exit_price, 2)
        obs.points_captured = round(points, 2)
        obs.r_multiple      = round(r_multiple, 3)
        obs.bars_held       = bars_held
        obs.resolved_at     = datetime.now(timezone.utc)
        resolved_count += 1

    db.commit()
    log.info("[paper_obs] Resolution pass complete: resolved=%d still_pending=%d",
             resolved_count, still_pending)

    return {
        "resolved":     resolved_count,
        "pending":      still_pending,
        "candleSource": source,
        "candleCount":  len(candles),
    }


# ═══════════════════════════════════════════════════════════════════════════════
# Statistics
# ═══════════════════════════════════════════════════════════════════════════════

CERTIFICATION_THRESHOLD = 30      # n required before re-validation can recommend READY


def get_observation_stats(db: Session) -> dict:
    """
    Aggregate stats across all paper observations.
    Reports running WR, expectancy R, profit factor, and progress
    toward the n>=30 certification threshold.
    """
    all_obs = db.query(PaperObservation).order_by(
        PaperObservation.observed_at.desc()
    ).all()

    total = len(all_obs)
    resolved = [o for o in all_obs if o.result in ("WIN", "LOSS", "EXPIRED")]
    pending  = [o for o in all_obs if o.result is None]

    wins   = [o for o in resolved if o.result == "WIN"]
    losses = [o for o in resolved if o.result == "LOSS"]
    nw, nl = len(wins), len(losses)
    n_res  = len(resolved)

    win_rate    = round(nw / n_res * 100, 2) if n_res else 0.0
    expectancy_r = round(
        sum(o.r_multiple or 0 for o in resolved) / n_res, 3
    ) if n_res else 0.0

    total_win_pts  = sum(o.points_captured or 0 for o in wins)
    total_loss_pts = abs(sum(o.points_captured or 0 for o in losses))
    profit_factor  = round(total_win_pts / total_loss_pts, 3) if total_loss_pts > 0 else None

    avg_rr = round(
        sum(o.rr or 0 for o in all_obs) / total, 2
    ) if total else 0.0

    # Progress toward certification
    progress_pct = min(100.0, round(n_res / CERTIFICATION_THRESHOLD * 100, 1))
    samples_needed = max(0, CERTIFICATION_THRESHOLD - n_res)

    # Verdict
    if total == 0:
        verdict = "No observations yet — scanner has not logged any SIGNAL_READY states"
        readiness = "AWAITING_FIRST_SIGNAL"
    elif n_res < CERTIFICATION_THRESHOLD:
        verdict = (
            f"Observation phase in progress: {n_res} resolved / "
            f"{CERTIFICATION_THRESHOLD} required. "
            f"Need {samples_needed} more confirmed outcomes."
        )
        readiness = "OBSERVATION_IN_PROGRESS"
    elif expectancy_r <= 0:
        verdict = (
            "Observation sample reached threshold but expectancy is negative. "
            "Strategy does NOT have demonstrated edge in live conditions."
        )
        readiness = "EDGE_NOT_CONFIRMED"
    elif profit_factor and profit_factor >= 1.2 and expectancy_r > 0.1:
        verdict = (
            f"Observation sample meets threshold (n={n_res}) with positive "
            f"expectancy ({expectancy_r:+.2f}R). Eligible for structured paper test."
        )
        readiness = "EDGE_CONFIRMED_IN_OBSERVATION"
    else:
        verdict = (
            f"Observation sample meets size threshold but edge is marginal "
            f"(expR={expectancy_r:+.2f}, PF={profit_factor}). Continue observation."
        )
        readiness = "EDGE_MARGINAL"

    return {
        "total":          total,
        "resolved":       n_res,
        "pending":        len(pending),
        "wins":           nw,
        "losses":         nl,
        "expired":        sum(1 for o in resolved if o.result == "EXPIRED"),
        "winRate":        win_rate,
        "expectancyR":    expectancy_r,
        "profitFactor":   profit_factor,
        "avgRR":          avg_rr,
        "totalPointsWon": round(total_win_pts, 2),
        "totalPointsLost": round(total_loss_pts, 2),
        "certificationThreshold": CERTIFICATION_THRESHOLD,
        "samplesNeeded": samples_needed,
        "progressPercent": progress_pct,
        "readiness":     readiness,
        "verdict":       verdict,
        "firstObservedAt": resolved[-1].observed_at.isoformat() if resolved else None,
        "lastObservedAt":  all_obs[0].observed_at.isoformat() if all_obs else None,
    }


# ═══════════════════════════════════════════════════════════════════════════════
# Convenience: serialise observation rows
# ═══════════════════════════════════════════════════════════════════════════════

def serialise_observation(obs: PaperObservation) -> dict:
    return {
        "id":              obs.id,
        "observedAt":      obs.observed_at.isoformat() if obs.observed_at else None,
        "instrument":      obs.instrument,
        "timeframe":       obs.timeframe,
        "signal":          obs.signal,
        "entry":           obs.entry,
        "stopLoss":        obs.stop_loss,
        "takeProfit":      obs.take_profit,
        "riskPoints":      obs.risk_points,
        "targetPoints":    obs.target_points,
        "rr":              obs.rr,
        "score":           obs.score,
        "session":         obs.session,
        "setupType":       obs.setup_type,
        "marketState":     obs.market_state,
        "confidence":      obs.confidence,
        "grade":           obs.grade,
        "result":          obs.result,
        "resolvedAt":      obs.resolved_at.isoformat() if obs.resolved_at else None,
        "exitPrice":       obs.exit_price,
        "pointsCaptured":  obs.points_captured,
        "rMultiple":       obs.r_multiple,
        "barsHeld":        obs.bars_held,
    }
