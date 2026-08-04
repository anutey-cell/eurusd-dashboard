"""
VP Trap 30-Day Measurement Protocol
====================================

Ground truth for whether VP Trap has an edge in live gold trading.
Four numbers, honestly computed, over 30 rolling days:

  1. Setups fired per day               (target 1–3)
  2. Win rate at 2R average             (target ≥ 40%)
  3. Average R per closed trade         (target ≥ +0.15R)
  4. Max drawdown in R (running series) (< 20% of expected total)

If ExpR > +0.15R across ≥ 20 closed trades in the window → VP Trap is
the operator's niche and gets promoted piece-by-piece to tiny live.
Otherwise it's discarded and we look elsewhere.

This module is the append-only ledger + the outcome tracker + the
aggregator. Wired from vp_trap_alerts (on signal fire) + background
scheduler (every 60s to advance PENDING/TRIGGERED to closed states).

Public API:
    record_signal(db, *, zone_id, direction, score, entry, sl,
                   tp1, tp2, session, ...)
        Called once when a VP Trap signal fires. Idempotent by zone_id
        + fired_at bucket.

    advance_outcomes(db)
        Called every 60s. Walks PENDING+TRIGGERED rows, checks live
        price, updates status/MFE/MAE/r_realized.

    compute_stats(db, days=30)
        Returns the operator-facing metrics dict.

    format_progress_digest(stats)
        Returns a Telegram-friendly progress summary string.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone, timedelta
from typing import Optional

from sqlalchemy.orm import Session

log = logging.getLogger(__name__)


# ── Config defaults (overridable via settings.measurement_*) ────────────────

DEFAULT_ENTRY_TOLERANCE_PTS = 3.0      # price must be within N pts of entry to trigger
DEFAULT_MAX_HOLD_HOURS      = 8        # abandon TRIGGERED trades after this
DEFAULT_MAX_WAIT_HOURS      = 4        # abandon PENDING signals after this if valid_until not set
DEFAULT_TP1_PARTIAL_FRAC    = 0.5      # what fraction closes at TP1 (rest runs to TP2)


# ── Recording ──────────────────────────────────────────────────────────────

def record_signal(
    db: Session,
    *,
    zone_id: str,
    direction: str,
    score: int,
    session: str,
    entry_price: float,
    stop_loss: float,
    tp1_price: Optional[float],
    tp2_price: Optional[float],
    invalidation_price: Optional[float] = None,
    valid_until: Optional[datetime] = None,
    trap_side: Optional[str] = None,
    signal_id: Optional[str] = None,
    notes: Optional[dict] = None,
) -> Optional[int]:
    """
    Append a new PENDING measurement row. Idempotent: if a PENDING row
    for the same zone_id already exists (opened within the last 6 hours),
    return that row's id instead of creating a duplicate.

    Returns the row id or None on failure.
    """
    try:
        from db_models import VpTrapMeasurementEvent as M
        # De-dupe on active zone (any non-terminal row within last 6h)
        cutoff = datetime.now(timezone.utc) - timedelta(hours=6)
        existing = (db.query(M)
                       .filter(M.zone_id == zone_id)
                       .filter(M.status.in_(["PENDING", "TRIGGERED"]))
                       .filter(M.fired_at >= cutoff)
                       .order_by(M.id.desc())
                       .first())
        if existing is not None:
            return int(existing.id)

        # Compute RRs
        risk_pts = abs(entry_price - stop_loss)
        tp1_rr = (abs(tp1_price - entry_price) / risk_pts) if (tp1_price and risk_pts > 0) else None
        tp2_rr = (abs(tp2_price - entry_price) / risk_pts) if (tp2_price and risk_pts > 0) else None

        row = M(
            zone_id=zone_id,
            signal_id=signal_id,
            direction=direction,
            score=int(score or 0),
            session=session or "unknown",
            trap_side=trap_side,
            fired_at=datetime.now(timezone.utc),
            entry_price=float(entry_price),
            stop_loss=float(stop_loss),
            tp1_price=(float(tp1_price) if tp1_price is not None else None),
            tp2_price=(float(tp2_price) if tp2_price is not None else None),
            tp1_rr=tp1_rr,
            tp2_rr=tp2_rr,
            invalidation_price=(float(invalidation_price) if invalidation_price is not None else None),
            valid_until=valid_until,
            status="PENDING",
            notes_json=(json.dumps(notes) if notes else None),
        )
        db.add(row)
        db.commit()
        db.refresh(row)
        log.info("[vp_measurement] recorded PENDING zone=%s %s @ %.2f (SL %.2f)",
                  zone_id, direction, entry_price, stop_loss)
        return int(row.id)
    except Exception as exc:
        log.warning("[vp_measurement] record_signal failed: %s", exc)
        try: db.rollback()
        except Exception: pass
        return None


# ── Outcome tracking ───────────────────────────────────────────────────────

def _last_price() -> Optional[float]:
    """Best-effort current XAU/USD price from the live feed cache."""
    try:
        from data.candles import get_candles
        m5 = get_candles(interval="M5", limit=1, pair="xauusd")
        if m5 and m5.candles:
            return float(m5.candles[-1].close)
    except Exception:
        pass
    return None


def _touched(price: float, level: float, tolerance: float = 0.5) -> bool:
    """Cheap 'did the wick/close touch this level' proxy."""
    return abs(price - level) <= tolerance


def _crossed(direction: str, price: float, level: float) -> bool:
    """Direction-aware level cross check."""
    if direction == "BUY":
        return price >= level
    if direction == "SELL":
        return price <= level
    return False


def _compute_realized_r(row, closed_price: float) -> float:
    """
    R math with TP1 partial-take semantics:
      - Half closes at TP1 (if reached) → locks +tp1_rr on that half
      - Other half runs to closed_price
      - If SL hits before TP1 → full loss (−1R)
      - If SL hits after TP1 → 0R on runner (BE) → net = 0.5 × tp1_rr
    """
    entry = row.entry_price
    sl    = row.stop_loss
    tp1   = row.tp1_price
    risk_pts = abs(entry - sl)
    if risk_pts <= 0:
        return 0.0

    def r_of(px):
        if row.direction == "BUY":
            return (px - entry) / risk_pts
        return (entry - px) / risk_pts

    # Full-loss cases
    if row.status == "STOPPED":
        # If we hit TP1 first then stopped at BE → +0.5 × tp1_rr net
        if row.mfe_pts is not None and tp1 is not None and \
           abs(row.mfe_pts) >= abs(tp1 - entry):
            return round((row.tp1_rr or 1.0) * DEFAULT_TP1_PARTIAL_FRAC, 3)
        return -1.0

    # TP2 hit → 0.5 × TP1_R + 0.5 × TP2_R (blended)
    if row.status == "TP2_HIT":
        return round(((row.tp1_rr or 1.0) * DEFAULT_TP1_PARTIAL_FRAC +
                       (row.tp2_rr or 2.0) * (1.0 - DEFAULT_TP1_PARTIAL_FRAC)), 3)

    # TP1 hit (runner didn't reach TP2 within max-hold, closed at BE)
    if row.status == "TP1_HIT":
        return round((row.tp1_rr or 1.0) * DEFAULT_TP1_PARTIAL_FRAC, 3)

    # Fallback — use raw close price
    return round(r_of(closed_price), 3)


def _update_mfe_mae(row, price: float) -> None:
    """Track max favorable/adverse excursion in points from entry."""
    entry = row.entry_price
    excursion = (price - entry) if row.direction == "BUY" else (entry - price)
    if excursion > (row.mfe_pts or 0.0):
        row.mfe_pts = excursion
    if excursion < (row.mae_pts if row.mae_pts is not None else 0.0):
        row.mae_pts = excursion


def advance_outcomes(db: Session) -> dict:
    """
    Walk every PENDING and TRIGGERED row and update its state against
    the current price. Returns a small summary dict.
    """
    from db_models import VpTrapMeasurementEvent as M
    now = datetime.now(timezone.utc)
    price = _last_price()
    if price is None:
        return {"skipped": "no_live_price"}

    summary = {"triggered": 0, "tp1": 0, "tp2": 0, "stopped": 0,
                "expired": 0, "invalidated": 0}

    rows = (db.query(M)
              .filter(M.status.in_(["PENDING", "TRIGGERED"]))
              .all())

    for r in rows:
        try:
            _update_mfe_mae(r, price)

            # PENDING → TRIGGERED / INVALIDATED / EXPIRED
            if r.status == "PENDING":
                # Invalidation: price crossed invalidation before entry.
                # Invalidation sits on the SL-side of the setup (below entry
                # for BUY, above entry for SELL) — same-side test as SL.
                if r.invalidation_price is not None and _crossed(
                    _opposite(r.direction), price, r.invalidation_price
                ):
                    r.status = "INVALIDATED"
                    r.closed_at = now
                    r.closed_price = price
                    r.duration_min = _duration_min(r.fired_at, now)
                    r.r_realized = 0.0
                    summary["invalidated"] += 1
                    continue

                # Expired — SQLite may return naive datetimes; normalize.
                fired_utc = _naive(r.fired_at)
                expiry = _naive(r.valid_until) if r.valid_until \
                    else (fired_utc + timedelta(hours=DEFAULT_MAX_WAIT_HOURS))
                if now > expiry:
                    r.status = "EXPIRED"
                    r.closed_at = now
                    r.closed_price = price
                    r.duration_min = _duration_min(r.fired_at, now)
                    r.r_realized = 0.0
                    summary["expired"] += 1
                    continue

                # Trigger: price within tolerance of entry
                tol = DEFAULT_ENTRY_TOLERANCE_PTS
                if _touched(price, r.entry_price, tolerance=tol):
                    r.status = "TRIGGERED"
                    r.triggered_at = now
                    r.triggered_price = price
                    summary["triggered"] += 1
                continue

            # TRIGGERED → TP1_HIT / TP2_HIT / STOPPED / EXPIRED
            if r.status == "TRIGGERED":
                # SL hit
                if _crossed(_opposite(r.direction), price, r.stop_loss):
                    r.status = "STOPPED"
                    r.closed_at = now
                    r.closed_price = price
                    r.duration_min = _duration_min(r.triggered_at, now)
                    r.r_realized = _compute_realized_r(r, price)
                    summary["stopped"] += 1
                    continue

                # TP2 hit
                if r.tp2_price is not None and _crossed(r.direction, price, r.tp2_price):
                    r.status = "TP2_HIT"
                    r.closed_at = now
                    r.closed_price = price
                    r.duration_min = _duration_min(r.triggered_at, now)
                    r.r_realized = _compute_realized_r(r, price)
                    summary["tp2"] += 1
                    continue

                # TP1 hit (partial take + BE)
                if r.tp1_price is not None and _crossed(r.direction, price, r.tp1_price):
                    # Only advance status if not already TP1_HIT
                    if r.status != "TP1_HIT":
                        r.status = "TP1_HIT"
                        summary["tp1"] += 1
                    # Don't close — runner continues to TP2 unless SL hits later

                # Max-hold expiry
                if r.triggered_at and (now - _naive(r.triggered_at)).total_seconds() > DEFAULT_MAX_HOLD_HOURS * 3600:
                    # Close at current price
                    if r.status == "TP1_HIT":
                        r.status = "TP1_HIT"  # keep — runner didn't reach TP2
                    else:
                        r.status = "EXPIRED"
                    r.closed_at = now
                    r.closed_price = price
                    r.duration_min = _duration_min(r.triggered_at, now)
                    r.r_realized = _compute_realized_r(r, price)
                    summary["expired"] += 1
        except Exception as exc:
            log.debug("[vp_measurement] advance row %s failed: %s", r.id, exc)

    try:
        db.commit()
    except Exception as exc:
        log.warning("[vp_measurement] commit failed: %s", exc)
        db.rollback()

    if any(v for k, v in summary.items()):
        log.info("[vp_measurement] advance summary: %s @ price=%.2f",
                  summary, price)
    return summary


def _duration_min(start: datetime, end: datetime) -> float:
    if start is None or end is None:
        return 0.0
    return round((_naive(end) - _naive(start)).total_seconds() / 60.0, 1)


def _naive(dt: datetime) -> datetime:
    """Coerce SQLite naive datetime to UTC-aware then back to strip tz for arithmetic."""
    if dt is None: return datetime.now(timezone.utc)
    if dt.tzinfo is None: return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _opposite(direction: str) -> str:
    return "SELL" if direction == "BUY" else "BUY"


# ── Aggregation ────────────────────────────────────────────────────────────

def compute_stats(db: Session, days: int = 30) -> dict:
    """
    Compute the four protocol metrics + drill-downs. Never raises.
    """
    from db_models import VpTrapMeasurementEvent as M
    since = datetime.now(timezone.utc) - timedelta(days=days)
    try:
        rows = (db.query(M)
                   .filter(M.fired_at >= since)
                   .order_by(M.fired_at.asc())
                   .all())
    except Exception as exc:
        log.warning("[vp_measurement] compute_stats query failed: %s", exc)
        return {"error": str(exc)}

    if not rows:
        return _empty_stats(days)

    n_fired      = len(rows)
    days_actual  = max(1.0, (datetime.now(timezone.utc) -
                              _naive(rows[0].fired_at)).total_seconds() / 86400)
    per_day      = round(n_fired / days_actual, 2)

    closed = [r for r in rows if r.status in ("TP1_HIT", "TP2_HIT", "STOPPED", "EXPIRED")]
    invalid = [r for r in rows if r.status == "INVALIDATED"]
    open_   = [r for r in rows if r.status in ("PENDING", "TRIGGERED")]

    wins   = [r for r in closed if (r.r_realized or 0) > 0]
    losses = [r for r in closed if (r.r_realized or 0) < 0]
    scratches = [r for r in closed if (r.r_realized or 0) == 0]

    wr_pct  = 100.0 * len(wins) / max(1, len(closed))
    total_r = sum((r.r_realized or 0) for r in closed)
    avg_r   = total_r / max(1, len(closed))

    # Running drawdown in R (peak-to-trough on cumulative R series)
    cum, peak, max_dd = 0.0, 0.0, 0.0
    for r in closed:
        cum += (r.r_realized or 0)
        if cum > peak: peak = cum
        dd = peak - cum
        if dd > max_dd: max_dd = dd

    # By session
    by_session = {}
    for r in closed:
        s = r.session or "unknown"
        d = by_session.setdefault(s, {"n": 0, "wins": 0, "r": 0.0})
        d["n"] += 1
        if (r.r_realized or 0) > 0: d["wins"] += 1
        d["r"] += (r.r_realized or 0)

    # Verdict against protocol targets
    thresholds = {"per_day_min": 1.0, "per_day_max": 3.0,
                   "min_wr_pct":  40.0, "min_avg_r":  0.15,
                   "min_closed_for_verdict": 20}
    verdict = _verdict(n_fired=n_fired, per_day=per_day,
                        closed=len(closed), wr_pct=wr_pct,
                        avg_r=avg_r, thresholds=thresholds)

    return {
        "window_days":         days,
        "window_days_actual":  round(days_actual, 2),
        "n_fired":             n_fired,
        "n_closed":            len(closed),
        "n_invalidated":       len(invalid),
        "n_open":              len(open_),
        "n_wins":              len(wins),
        "n_losses":            len(losses),
        "n_scratches":         len(scratches),
        "signals_per_day":     per_day,
        "win_rate_pct":        round(wr_pct, 1),
        "avg_r_per_trade":     round(avg_r, 3),
        "total_r":             round(total_r, 3),
        "max_drawdown_r":      round(max_dd, 3),
        "by_session":          {
            s: {"n": d["n"], "wr_pct": round(100*d["wins"]/max(1,d["n"]),1),
                 "avg_r": round(d["r"]/max(1,d["n"]), 3)}
            for s, d in by_session.items()
        },
        "targets":             thresholds,
        "verdict":             verdict,
        "generated_at":        datetime.now(timezone.utc).isoformat(),
    }


def _empty_stats(days: int) -> dict:
    return {"window_days": days, "n_fired": 0, "n_closed": 0,
            "verdict": {"label": "NO DATA",
                         "detail": "No VP Trap signals recorded yet"}}


def _verdict(*, n_fired: int, per_day: float, closed: int,
              wr_pct: float, avg_r: float, thresholds: dict) -> dict:
    """Grade against protocol targets."""
    if closed < thresholds["min_closed_for_verdict"]:
        return {"label": "INSUFFICIENT SAMPLE",
                 "detail": f"Only {closed} closed trades — need "
                           f"{thresholds['min_closed_for_verdict']} for statistical read"}
    fails = []
    if per_day < thresholds["per_day_min"]:
        fails.append(f"underfiring ({per_day}/day < {thresholds['per_day_min']})")
    if per_day > thresholds["per_day_max"]:
        fails.append(f"overfiring ({per_day}/day > {thresholds['per_day_max']})")
    if wr_pct < thresholds["min_wr_pct"]:
        fails.append(f"WR {wr_pct:.1f}% < target {thresholds['min_wr_pct']:.0f}%")
    if avg_r < thresholds["min_avg_r"]:
        fails.append(f"avg R {avg_r:+.3f} < target {thresholds['min_avg_r']:+.2f}")
    if fails:
        return {"label": "BELOW TARGET", "detail": " · ".join(fails)}
    return {"label": "ON TARGET",
             "detail": f"WR {wr_pct:.1f}% × avg {avg_r:+.3f}R over {closed} trades — "
                        "protocol conditions met"}


# ── Telegram digest formatter ──────────────────────────────────────────────

def format_progress_digest(stats: dict) -> str:
    """Short Telegram-friendly progress summary for the weekly digest."""
    if stats.get("n_fired", 0) == 0:
        return ("*VP Trap 30-Day Protocol*\n"
                "No signals recorded yet · nothing to measure")

    v = stats.get("verdict", {})
    lines = [
        "*VP Trap 30-Day Protocol · Progress*",
        f"Window: {stats['window_days']}d actual"
        f" · signals fired: *{stats['n_fired']}*"
        f" · closed: *{stats['n_closed']}*",
        f"Signals/day: *{stats['signals_per_day']}*   (target 1–3)",
        f"Win rate: *{stats['win_rate_pct']}%*   (target ≥ 40%)",
        f"Avg R: *{stats['avg_r_per_trade']:+.3f}R*   (target ≥ +0.150R)",
        f"Total: *{stats['total_r']:+.2f}R*"
        f" · Max drawdown: *{stats['max_drawdown_r']:.2f}R*",
        "",
        f"Verdict: *{v.get('label', '?')}*",
        f"_{v.get('detail', '')}_",
    ]
    return "\n".join(lines)


__all__ = ["record_signal", "advance_outcomes",
            "compute_stats", "format_progress_digest"]
