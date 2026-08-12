"""
Shadow Trade Simulator
=======================

Records every BUY/SELL strategist verdict with a complete trade plan
(entry + SL + TP1 + TP2) — regardless of grade — into `shadow_trades`.
A background loop walks PENDING/TRIGGERED rows against live price and
marks TRIGGERED / TP1_HIT / TP2_HIT / STOPPED / INVALIDATED / EXPIRED.

Purpose:
  - Capture the outcome of every proposed trade, including grades that
    the alert path suppressed (B / C).
  - Answer empirically: does A+ actually outperform A? Does B actually
    underperform? What's the spread-adjusted expectancy per bucket?
  - Feed the calibration lookup for the improved grader.

Guarantees:
  - No execution. No Telegram. No modification of any existing signal path.
  - Idempotent by fingerprint (direction, entry rounded to 5pt, session, hour).
  - Fails silent — never raises into the strategist tick.
"""
from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from typing import Optional

from sqlalchemy import text
from sqlalchemy.orm import Session

log = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Session-conditional slippage + spread assumptions (Section 6 of strategy review)
# Tune with real MT5 tick data once bridge posts consistent heartbeats.
# ─────────────────────────────────────────────────────────────────────────────

SPREAD_PTS_BY_SESSION: dict[str, float] = {
    "ASIA":         0.5,
    "PRE_LDN":      0.6,
    "LDN_OPEN":     0.8,
    "LDN_CONT":     0.5,
    "LDN_LUNCH":    0.7,
    "NY_OPEN":      1.0,
    "LDN_NY_CLOSE": 0.7,
    "NY_LATE":      1.2,
    "OFF":          2.0,
    "NEWS_WINDOW":  4.0,
    "ROLLOVER":     5.0,
}

SLIPPAGE_PTS_BY_SESSION: dict[str, float] = {
    "ASIA":         0.3,
    "PRE_LDN":      0.4,
    "LDN_OPEN":     0.8,
    "LDN_CONT":     0.4,
    "LDN_LUNCH":    0.5,
    "NY_OPEN":      1.0,
    "LDN_NY_CLOSE": 0.6,
    "NY_LATE":      0.7,
    "OFF":          1.5,
    "NEWS_WINDOW":  3.0,
    "ROLLOVER":     5.0,
}

# When a shadow trade sits in PENDING > this many hours, mark it EXPIRED.
DEFAULT_EXPIRY_HOURS = 12

# Rollover blackout window UTC
_ROLLOVER_START_UTC = (21, 50)   # 21:50
_ROLLOVER_END_UTC   = (22, 15)   # 22:15


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _now() -> datetime:
    return datetime.now(timezone.utc)


def _naive(dt):
    """Normalize a datetime to naive UTC (SQLite strips tz on load)."""
    if dt is None:
        return None
    if isinstance(dt, str):
        try:
            dt = datetime.fromisoformat(dt.replace("Z", "+00:00").split("+")[0])
        except Exception:
            return None
    if hasattr(dt, "tzinfo") and dt.tzinfo is not None:
        return dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


def _session_label_for_now(now: Optional[datetime] = None) -> str:
    """Rough session mapping. Aligns with canonical_market_data.killzone_for_utc."""
    now = now or _now()
    h = now.hour
    if _in_rollover_window(now):
        return "ROLLOVER"
    if h < 6:     return "ASIA"
    if h < 7:     return "PRE_LDN"
    if h < 10:    return "LDN_OPEN"
    if h < 12:    return "LDN_CONT"
    if h < 13:    return "LDN_LUNCH"
    if h < 16:    return "NY_OPEN"
    if h < 17:    return "LDN_NY_CLOSE"
    return "NY_LATE"


def _in_rollover_window(now: datetime) -> bool:
    """Rollover blackout 21:50-22:15 UTC."""
    hh, mm = now.hour, now.minute
    if hh == _ROLLOVER_START_UTC[0] and mm >= _ROLLOVER_START_UTC[1]:
        return True
    if hh == _ROLLOVER_END_UTC[0] and mm < _ROLLOVER_END_UTC[1]:
        return True
    return False


def _estimate_spread(session_label: Optional[str]) -> float:
    return SPREAD_PTS_BY_SESSION.get(session_label or "OFF", 2.0)


def _estimate_slippage(session_label: Optional[str]) -> float:
    return SLIPPAGE_PTS_BY_SESSION.get(session_label or "OFF", 1.5)


def _fingerprint(*, direction: str, entry: float, session: str,
                    fired_at: datetime) -> str:
    """Idempotency key. Same setup within same hour + session bucket → same fp."""
    parts = [
        direction,
        f"{round(float(entry) / 5) * 5:.0f}",   # nearest 5-pt bucket
        session or "OFF",
        fired_at.strftime("%Y%m%d%H"),          # per-hour bucket
    ]
    return hashlib.sha256("|".join(parts).encode()).hexdigest()[:16]


def _last_price(db: Session, instrument: str = "XAU/USD") -> Optional[float]:
    """Latest close from historical_candles (M5) as the shadow-tick price."""
    try:
        row = db.execute(text(
            "SELECT close FROM historical_candles "
            "WHERE instrument=:i AND timeframe='M5' "
            "ORDER BY candle_time DESC LIMIT 1"
        ), {"i": instrument}).fetchone()
        return float(row[0]) if row and row[0] is not None else None
    except Exception as exc:
        log.debug("[shadow_trade] _last_price failed: %s", exc)
        return None


def _bar_extremes_since(db: Session, since_ts: datetime,
                          instrument: str = "XAU/USD") -> tuple[Optional[float], Optional[float]]:
    """Return (max_high, min_low) across all M5 bars since `since_ts`."""
    try:
        row = db.execute(text(
            "SELECT MAX(high), MIN(low) FROM historical_candles "
            "WHERE instrument=:i AND timeframe='M5' AND candle_time >= :s"
        ), {"i": instrument, "s": since_ts}).fetchone()
        if not row:
            return (None, None)
        return (float(row[0]) if row[0] is not None else None,
                float(row[1]) if row[1] is not None else None)
    except Exception as exc:
        log.debug("[shadow_trade] _bar_extremes_since failed: %s", exc)
        return (None, None)


# ─────────────────────────────────────────────────────────────────────────────
# Public: record_shadow_trade
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class ShadowTradeRecordResult:
    recorded:   bool
    reason:     str
    fingerprint: str = ""


def record_shadow_trade(db: Session, verdict: dict,
                          grade_result=None, *,
                          instrument: str = "XAU/USD") -> ShadowTradeRecordResult:
    """
    Called on every strategist tick that produced a BUY/SELL verdict with a
    complete trade plan. Idempotent — dedupes by (direction, rounded entry,
    session, hour). Never raises.
    """
    try:
        decision = verdict.get("decision")
        if decision not in ("BUY", "SELL"):
            return ShadowTradeRecordResult(False, f"decision={decision}")

        tp = verdict.get("trade_plan") or {}
        entry = tp.get("entry")
        sl = tp.get("stop_loss")
        tp1 = tp.get("tp1")
        tp2 = tp.get("tp2")
        if entry is None or sl is None or tp1 is None or tp2 is None:
            return ShadowTradeRecordResult(False, "incomplete trade plan")

        now = _now()
        session = _session_label_for_now(now)
        fp = _fingerprint(direction=decision, entry=float(entry),
                           session=session, fired_at=now)

        # Idempotency check
        existing = db.execute(text(
            "SELECT id FROM shadow_trades WHERE fingerprint=:f LIMIT 1"
        ), {"f": fp}).fetchone()
        if existing:
            return ShadowTradeRecordResult(False, "duplicate fingerprint", fp)

        # Extract grading context if provided
        grade = "UNGRADED"
        grade_reason = None
        composite_score = None
        if grade_result is not None:
            grade = getattr(grade_result, "grade", "UNGRADED")
            grade_reason = getattr(grade_result, "reason", None)
            composite_score = getattr(grade_result, "composite_score", None)
        elif isinstance(verdict.get("signal_grade"), dict):
            sg = verdict["signal_grade"]
            grade = sg.get("grade", "UNGRADED")
            grade_reason = sg.get("reason")

        # Session-conditional cost estimates
        est_spread = _estimate_spread(session)
        est_slippage = _estimate_slippage(session)

        from db_models import ShadowTrade
        row = ShadowTrade(
            fingerprint=fp,
            verdict_id=verdict.get("_verdict_id"),
            instrument=instrument,
            grade=grade,
            grade_reason=(grade_reason or "")[:255],
            composite_score=composite_score,
            archetype=verdict.get("archetype"),
            regime_at_entry=verdict.get("regime") or verdict.get("market_regime"),
            session_at_entry=session,
            direction=decision,
            setup_score=verdict.get("setup_score"),
            conditions_passed=verdict.get("conditions_passed"),
            fired_at=now,
            entry_price=float(entry),
            stop_loss=float(sl),
            tp1_price=float(tp1),
            tp2_price=float(tp2) if tp2 is not None else None,
            tp3_price=float(tp.get("tp3")) if tp.get("tp3") is not None else None,
            invalidation_price=(float(tp.get("invalidation"))
                                  if tp.get("invalidation") is not None else float(sl)),
            tp1_rr=tp.get("tp1_rr") or (
                abs(float(tp1) - float(entry)) / abs(float(entry) - float(sl))
                if float(entry) != float(sl) else None),
            tp2_rr=tp.get("tp2_rr") or (
                abs(float(tp2) - float(entry)) / abs(float(entry) - float(sl))
                if tp2 is not None and float(entry) != float(sl) else None),
            est_spread_pts=est_spread,
            est_slippage_pts=est_slippage,
            status="PENDING",
            notes_json=json.dumps({
                "grade_reason": grade_reason,
                "verdict_excerpt": {
                    "execution_status": verdict.get("execution_status"),
                    "market_state": verdict.get("market_state"),
                    "tf_alignment_label": verdict.get("tf_alignment_label"),
                    "liquidity_model_type":
                        (verdict.get("liquidity_model") or {}).get("type"),
                },
            }, default=str)[:60000],
        )
        db.add(row)
        db.commit()
        return ShadowTradeRecordResult(True, "recorded", fp)
    except Exception as exc:
        log.warning("[shadow_trade] record failed (non-fatal): %s", exc)
        try:
            db.rollback()
        except Exception:
            pass
        return ShadowTradeRecordResult(False, f"error: {type(exc).__name__}: {exc}")


# ─────────────────────────────────────────────────────────────────────────────
# Public: advance_outcomes (background loop entry point)
# ─────────────────────────────────────────────────────────────────────────────

def advance_outcomes(db: Session, *,
                      instrument: str = "XAU/USD",
                      expiry_hours: int = DEFAULT_EXPIRY_HOURS) -> dict:
    """
    Walks every PENDING and TRIGGERED row. For each, checks live price:
      - PENDING + price crosses entry → TRIGGERED
      - PENDING + opposing move breaks invalidation → INVALIDATED
      - PENDING + expiry_hours passed since fired_at → EXPIRED
      - TRIGGERED + price hits SL → STOPPED, r_realized = -1
      - TRIGGERED + price hits TP2 → TP2_HIT, r_realized = 0.5*tp1_rr + 0.5*tp2_rr
      - TRIGGERED + price hits TP1 → TP1_HIT (single-shot), r_realized = tp1_rr

    Returns counters {pending_walked, triggered, closed_tp1, closed_tp2,
                       stopped, invalidated, expired}.
    """
    counters = {
        "pending_walked": 0, "triggered": 0,
        "closed_tp1": 0, "closed_tp2": 0,
        "stopped": 0, "invalidated": 0, "expired": 0,
    }
    now = _now()

    price = _last_price(db, instrument)
    if price is None:
        return {**counters, "warning": "no live price"}

    # Walk PENDING first
    try:
        rows = db.execute(text(
            "SELECT id, direction, entry_price, stop_loss, invalidation_price, "
            "tp1_price, tp2_price, tp1_rr, tp2_rr, fired_at, "
            "est_spread_pts, est_slippage_pts "
            "FROM shadow_trades WHERE instrument=:i AND status='PENDING'"
        ), {"i": instrument}).fetchall()
    except Exception as exc:
        log.warning("[shadow_trade] pending query failed: %s", exc)
        rows = []

    for r in rows:
        (sid, direction, entry, sl, invalidation, tp1, tp2, tp1_rr, tp2_rr,
         fired_at, est_spread, est_slippage) = r
        counters["pending_walked"] += 1
        fired_naive = _naive(fired_at) or _naive(now)
        now_naive = _naive(now)

        # Expiry check
        if now_naive and fired_naive and (now_naive - fired_naive) > timedelta(hours=expiry_hours):
            db.execute(text(
                "UPDATE shadow_trades SET status='EXPIRED', closed_at=:ts "
                "WHERE id=:id"
            ), {"ts": now_naive, "id": sid})
            counters["expired"] += 1
            continue

        # Fetch the extreme bars since fired_at
        max_high, min_low = _bar_extremes_since(db, fired_naive, instrument)
        if max_high is None or min_low is None:
            continue

        # Invalidation check (opposing side)
        inv = float(invalidation) if invalidation is not None else float(sl)
        if direction == "BUY" and min_low <= inv:
            db.execute(text(
                "UPDATE shadow_trades SET status='INVALIDATED', closed_at=:ts, "
                "closed_price=:p WHERE id=:id"
            ), {"ts": now_naive, "p": inv, "id": sid})
            counters["invalidated"] += 1
            continue
        if direction == "SELL" and max_high >= inv:
            db.execute(text(
                "UPDATE shadow_trades SET status='INVALIDATED', closed_at=:ts, "
                "closed_price=:p WHERE id=:id"
            ), {"ts": now_naive, "p": inv, "id": sid})
            counters["invalidated"] += 1
            continue

        # Trigger check: has price traded through entry?
        entry_f = float(entry)
        if direction == "BUY" and min_low <= entry_f <= max_high:
            db.execute(text(
                "UPDATE shadow_trades SET status='TRIGGERED', "
                "triggered_at=:ts, triggered_price=:p WHERE id=:id"
            ), {"ts": now_naive, "p": entry_f, "id": sid})
            counters["triggered"] += 1
        elif direction == "SELL" and min_low <= entry_f <= max_high:
            db.execute(text(
                "UPDATE shadow_trades SET status='TRIGGERED', "
                "triggered_at=:ts, triggered_price=:p WHERE id=:id"
            ), {"ts": now_naive, "p": entry_f, "id": sid})
            counters["triggered"] += 1

    db.commit()

    # Walk TRIGGERED
    try:
        rows = db.execute(text(
            "SELECT id, direction, entry_price, stop_loss, tp1_price, tp2_price, "
            "tp1_rr, tp2_rr, triggered_at, est_spread_pts, est_slippage_pts "
            "FROM shadow_trades WHERE instrument=:i AND status='TRIGGERED'"
        ), {"i": instrument}).fetchall()
    except Exception as exc:
        log.warning("[shadow_trade] triggered query failed: %s", exc)
        rows = []

    for r in rows:
        (sid, direction, entry, sl, tp1, tp2, tp1_rr, tp2_rr, triggered_at,
         est_spread, est_slippage) = r
        trig_naive = _naive(triggered_at) or _naive(now)
        now_naive = _naive(now)

        max_high, min_low = _bar_extremes_since(db, trig_naive, instrument)
        if max_high is None or min_low is None:
            continue

        entry_f = float(entry); sl_f = float(sl); tp1_f = float(tp1)
        tp2_f = float(tp2) if tp2 is not None else None

        # MFE / MAE for eventual close (recompute at each poll — safe over-write)
        if direction == "BUY":
            mfe = max_high - entry_f
            mae = entry_f - min_low
        else:
            mfe = entry_f - min_low
            mae = max_high - entry_f

        # Direction-conditional resolution
        if direction == "BUY":
            hit_sl = min_low <= sl_f
            hit_tp1 = max_high >= tp1_f
            hit_tp2 = tp2_f is not None and max_high >= tp2_f
        else:  # SELL
            hit_sl = max_high >= sl_f
            hit_tp1 = min_low <= tp1_f
            hit_tp2 = tp2_f is not None and min_low <= tp2_f

        # Resolution rule: TP2 wins if hit; then TP1; then SL. Ties resolve
        # to the earlier target in the M5 bar sequence — approximated by
        # requiring TP2 to be strictly hit AFTER TP1 was hit.
        cost_pts = float(est_spread or 0) + float(est_slippage or 0)

        if hit_tp2 and hit_tp1:
            # Full run to TP2 — realized 0.5 * tp1_rr + 0.5 * tp2_rr
            tp1_r = float(tp1_rr or 1)
            tp2_r = float(tp2_rr or 2)
            r_nominal = 0.5 * tp1_r + 0.5 * tp2_r
            # Spread-adjusted: subtract cost as fraction of SL distance
            sl_distance_pts = abs(entry_f - sl_f)
            r_adj = r_nominal - (cost_pts / max(sl_distance_pts, 0.1))
            _close_row(db, sid, "TP2_HIT", tp2_f, r_nominal, r_adj, mfe, mae,
                       trig_naive, now_naive)
            counters["closed_tp2"] += 1
        elif hit_sl:
            # Stopped out — realized -1R nominal, worse spread-adjusted
            r_nominal = -1.0
            sl_distance_pts = abs(entry_f - sl_f)
            r_adj = -1.0 - (cost_pts / max(sl_distance_pts, 0.1))
            _close_row(db, sid, "STOPPED", sl_f, r_nominal, r_adj, mfe, mae,
                       trig_naive, now_naive)
            counters["stopped"] += 1
        elif hit_tp1:
            # TP1 hit only (yet)
            tp1_r = float(tp1_rr or 1)
            r_nominal = tp1_r
            sl_distance_pts = abs(entry_f - sl_f)
            r_adj = tp1_r - (cost_pts / max(sl_distance_pts, 0.1))
            _close_row(db, sid, "TP1_HIT", tp1_f, r_nominal, r_adj, mfe, mae,
                       trig_naive, now_naive)
            counters["closed_tp1"] += 1

    db.commit()
    return counters


def _close_row(db, sid, status, price, r_nom, r_adj, mfe, mae,
                trig_naive, now_naive):
    duration_min = None
    if trig_naive and now_naive:
        duration_min = round((now_naive - trig_naive).total_seconds() / 60, 1)
    db.execute(text(
        "UPDATE shadow_trades SET status=:s, closed_at=:ts, closed_price=:p, "
        "r_realized=:r, r_spread_adjusted=:ra, mfe_pts=:mfe, mae_pts=:mae, "
        "duration_min=:d WHERE id=:id"
    ), {"s": status, "ts": now_naive, "p": price,
          "r": round(r_nom, 3), "ra": round(r_adj, 3),
          "mfe": round(mfe, 2), "mae": round(mae, 2),
          "d": duration_min, "id": sid})


# ─────────────────────────────────────────────────────────────────────────────
# Public: compute_bucket_stats (calibration data)
# ─────────────────────────────────────────────────────────────────────────────

def compute_bucket_stats(db: Session, *, days: int = 30,
                           instrument: str = "XAU/USD") -> dict:
    """
    Aggregate resolved shadow trades by (grade, archetype, regime, session).
    Returns list of buckets with n, mean_r, hit_rate, mean_r_adjusted.
    """
    cutoff = _now() - timedelta(days=days)
    try:
        rows = db.execute(text(
            "SELECT grade, archetype, regime_at_entry, session_at_entry, "
            "r_realized, r_spread_adjusted, status "
            "FROM shadow_trades WHERE instrument=:i AND created_at >= :s "
            "AND status IN ('TP1_HIT','TP2_HIT','STOPPED','INVALIDATED','EXPIRED')"
        ), {"i": instrument, "s": cutoff}).fetchall()
    except Exception as exc:
        return {"error": str(exc), "buckets": []}

    from collections import defaultdict
    buckets: dict = defaultdict(list)
    for r in rows:
        key = f"{r[0]}|{r[1] or 'none'}|{r[2] or 'none'}|{r[3] or 'none'}"
        buckets[key].append({"r": r[4], "r_adj": r[5], "status": r[6]})

    output = []
    for key, trades in sorted(buckets.items()):
        r_values = [t["r"] for t in trades if t["r"] is not None]
        r_adj_values = [t["r_adj"] for t in trades if t["r_adj"] is not None]
        wins = sum(1 for t in trades if t["r"] is not None and t["r"] > 0)
        output.append({
            "bucket_key": key,
            "n": len(trades),
            "wins": wins,
            "losses": len(trades) - wins,
            "hit_rate": round(wins / len(trades), 3) if trades else 0.0,
            "mean_r": round(sum(r_values) / len(r_values), 3) if r_values else None,
            "mean_r_adjusted": round(sum(r_adj_values) / len(r_adj_values), 3)
                                if r_adj_values else None,
            "meets_min_sample": len(trades) >= 20,
        })

    # Also overall summary
    all_r = [t["r"] for tr in buckets.values() for t in tr if t["r"] is not None]
    all_r_adj = [t["r_adj"] for tr in buckets.values() for t in tr if t["r_adj"] is not None]
    overall = {
        "n_total": sum(b["n"] for b in output),
        "mean_r_all": round(sum(all_r) / len(all_r), 3) if all_r else None,
        "mean_r_adjusted_all": round(sum(all_r_adj) / len(all_r_adj), 3) if all_r_adj else None,
    }
    return {
        "window_days": days,
        "overall": overall,
        "buckets": output,
    }


def format_bucket_summary(stats: dict) -> str:
    """Compact Telegram-friendly digest of bucket stats."""
    lines = [f"Shadow trades — last {stats.get('window_days', 30)} days"]
    ov = stats.get("overall", {})
    lines.append(f"Total resolved: {ov.get('n_total', 0)}")
    if ov.get("mean_r_all") is not None:
        lines.append(f"Mean R (nominal):  {ov['mean_r_all']:+.2f}R")
        lines.append(f"Mean R (adjusted): {ov.get('mean_r_adjusted_all', 0):+.2f}R")
    lines.append("")
    lines.append("Top buckets (≥20 samples):")
    ready = sorted(
        [b for b in stats.get("buckets", []) if b.get("meets_min_sample")],
        key=lambda b: -(b.get("mean_r_adjusted") or -99),
    )[:5]
    if not ready:
        lines.append("  (no bucket has 20+ samples yet — keep collecting)")
    for b in ready:
        lines.append(f"  {b['bucket_key']}  n={b['n']}  "
                      f"WR={b['hit_rate']*100:.0f}%  R̄={b['mean_r']:+.2f} "
                      f"(adj {b['mean_r_adjusted']:+.2f})")
    return "\n".join(lines)


__all__ = [
    "record_shadow_trade", "advance_outcomes",
    "compute_bucket_stats", "format_bucket_summary",
    "ShadowTradeRecordResult",
    "SPREAD_PTS_BY_SESSION", "SLIPPAGE_PTS_BY_SESSION",
]
