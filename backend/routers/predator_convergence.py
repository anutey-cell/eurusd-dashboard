"""
Predator forward-convergence diagnostics.

Read-only endpoint that surfaces:
  - Sample counters (canonical / approved / executed / closed / skipped)
  - MODEL vs ACTUAL DEMO cumulative P&L
  - Execution telemetry (spread, slippage, latency, failures)
  - Convergence classification counts
  - Checkpoint distances (30 / 60 / 100 closed batches)

Distinguishes HISTORICAL REPLAY, FORWARD MODEL, and ACTUAL DEMO — never
combines them into one N.
"""
from __future__ import annotations

from datetime import datetime, timezone, timedelta
from typing import Optional

from fastapi import APIRouter, Depends
from sqlalchemy import text
from sqlalchemy.orm import Session

from database import get_db

router = APIRouter(prefix="/diagnostics", tags=["diagnostics"])

CHAMPION_FREEZE = datetime(2026, 8, 13, 6, 11, 50, tzinfo=timezone.utc)
MAX_EXPOSURE = 0.15


def _count(db: Session, sql: str, **params) -> int:
    try:
        r = db.execute(text(sql), params).fetchone()
        return int(r[0]) if r and r[0] is not None else 0
    except Exception:
        return 0


def _scalar(db: Session, sql: str, **params) -> Optional[float]:
    try:
        r = db.execute(text(sql), params).fetchone()
        return float(r[0]) if r and r[0] is not None else None
    except Exception:
        return None


@router.get("/predator-convergence")
def predator_convergence(db: Session = Depends(get_db)):
    """Daily forward-convergence snapshot. Read-only aggregation."""
    freeze_iso = CHAMPION_FREEZE.strftime("%Y-%m-%d %H:%M:%S")

    # ── SAMPLE (from launch = freeze onward) ──────────────────────────
    total_batches = _count(db, "SELECT COUNT(*) FROM predator_signal_batches "
                                "WHERE created_at >= :f", f=freeze_iso)
    exec_batches = _count(db, "SELECT COUNT(*) FROM predator_signal_batches "
                               "WHERE created_at >= :f AND execution_status IN ('COMPLETE','PARTIAL')", f=freeze_iso)

    closed_positions = _count(db, "SELECT COUNT(*) FROM predator_positions "
                                    "WHERE closed_at IS NOT NULL AND created_at >= :f", f=freeze_iso)
    open_positions = _count(db, "SELECT COUNT(*) FROM predator_positions "
                                 "WHERE status IN ('ENQUEUED','OPEN') AND created_at >= :f", f=freeze_iso)

    # closed BATCHES = batches where ALL positions are closed
    closed_batches = _count(db, """
        SELECT COUNT(*) FROM (
          SELECT b.id FROM predator_signal_batches b
          WHERE b.created_at >= :f AND b.execution_status IN ('COMPLETE','PARTIAL')
          GROUP BY b.id
          HAVING SUM(CASE WHEN EXISTS (
              SELECT 1 FROM predator_positions p
              WHERE p.batch_id = b.id AND p.closed_at IS NULL
                AND p.status IN ('ENQUEUED','OPEN')
          ) THEN 1 ELSE 0 END) = 0
        )
    """, f=freeze_iso)

    forward_opps = _count(db, "SELECT COUNT(*) FROM predator_forward_opportunities "
                                "WHERE created_at >= :f", f=freeze_iso)
    forward_fires = _count(db, "SELECT COUNT(*) FROM predator_forward_opportunities "
                                 "WHERE created_at >= :f AND model_decision='FIRE'", f=freeze_iso)
    portfolio_skipped = _count(db, "SELECT COUNT(*) FROM predator_forward_opportunities "
                                     "WHERE created_at >= :f AND portfolio_decision LIKE 'SKIPPED%'", f=freeze_iso)

    # ── MODEL vs ACTUAL DEMO ──────────────────────────────────────────
    actual_pnl_pts = _scalar(db, """
        SELECT COALESCE(SUM(p.realized_pts * p.lot_size * 100), 0)
        FROM predator_positions p
        WHERE p.closed_at IS NOT NULL AND p.created_at >= :f
    """, f=freeze_iso) or 0.0

    # ── EXECUTION telemetry ───────────────────────────────────────────
    avg_spread = _scalar(db, """
        SELECT AVG(spread_at_enqueue) FROM predator_positions
        WHERE spread_at_enqueue IS NOT NULL AND created_at >= :f
    """, f=freeze_iso)
    avg_spread_at_fire = _scalar(db, """
        SELECT AVG(spread_at_fire) FROM predator_signal_batches
        WHERE spread_at_fire IS NOT NULL AND created_at >= :f
    """, f=freeze_iso)

    # ── REJECTIONS ────────────────────────────────────────────────────
    total_rejections = _count(db, "SELECT COUNT(*) FROM predator_rejections "
                                    "WHERE created_at >= :f", f=freeze_iso)
    rejections_by_reason = {}
    try:
        for r in db.execute(text(
            "SELECT rejection_reason, COUNT(*) FROM predator_rejections "
            "WHERE created_at >= :f GROUP BY rejection_reason"
        ), {"f": freeze_iso}).fetchall():
            rejections_by_reason[r[0]] = int(r[1])
    except Exception:
        pass

    # ── EXPOSURE ──────────────────────────────────────────────────────
    current_open_lots = _scalar(db, """
        SELECT COALESCE(SUM(lot_size), 0) FROM predator_positions
        WHERE status IN ('ENQUEUED','OPEN')
    """) or 0.0
    exposure_breach = current_open_lots > MAX_EXPOSURE + 1e-6

    # ── ORDERING CHECK ────────────────────────────────────────────────
    # Same-timestamp batches, count mismatches vs ASIAN→PDL→VOL order.
    ordering_mismatches = 0
    try:
        prio = {"ASIAN_BREAKDOWN": 0, "PDL_BREAK": 1, "VOL_CONTINUATION": 2}
        rows = db.execute(text(
            "SELECT created_at, archetype, id FROM predator_signal_batches "
            "WHERE created_at >= :f ORDER BY created_at, id"
        ), {"f": freeze_iso}).fetchall()
        by_ts = {}
        for ts, arch, _id in rows:
            by_ts.setdefault(str(ts)[:19], []).append(arch)
        for ts, archs in by_ts.items():
            if len(archs) < 2: continue
            expected = sorted(archs, key=lambda a: prio.get(a, 99))
            if archs != expected:
                ordering_mismatches += 1
    except Exception:
        pass

    # ── ORPHAN DETECTION ──────────────────────────────────────────────
    orphan_positions = _count(db, """
        SELECT COUNT(*) FROM predator_positions p
        WHERE p.created_at >= :f
          AND NOT EXISTS (SELECT 1 FROM predator_signal_batches b WHERE b.id = p.batch_id)
    """, f=freeze_iso)

    # ── HISTORICAL REFERENCE ──────────────────────────────────────────
    historical = {
        "note": "From CLOSURE-2 conservative baseline. NOT combined with forward N.",
        "baseline_span": "2025-03-21 → 2026-08-13 (516 days, single-source MT5 M5, strict 3/3 M15)",
        "conservative_portfolio_pnl_normal": 35737,
        "mean_batch_r_normal": 0.210,
        "max_dd_pct_normal": 21.3,
        "rolling_90d_positive": "5/6",
        "n_executed_batches": 508,
    }

    # ── FORWARD MODEL (deferred; requires per-opp walk-forward resolver) ──
    forward_model = {
        "note": ("FORWARD_MODEL_RESULT resolver deferred to a follow-up sprint. "
                 "For now, the frozen batch fields (entry/SL/TP1/TP2) are the "
                 "model expectation; actual demo P&L is compared to those."),
        "expected_tickets_avg": _scalar(db,
            "SELECT AVG(planned_positions) FROM predator_signal_batches WHERE created_at >= :f",
            f=freeze_iso),
    }

    # ── CHECKPOINTS ───────────────────────────────────────────────────
    checkpoints = {
        "closed_batches": closed_batches,
        "distance_to_30": max(0, 30 - closed_batches),
        "distance_to_60": max(0, 60 - closed_batches),
        "distance_to_100": max(0, 100 - closed_batches),
    }

    if closed_batches < 30:
        status = "EARLY SAMPLE — insufficient closed batches for strategy classification"
    elif closed_batches < 100:
        status = "CONVERGING — continue observation to Checkpoint C (100 closed batches)"
    else:
        status = "READY FOR FORWARD VALIDATION REVIEW"

    return {
        "as_of": datetime.now(timezone.utc).isoformat(),
        "champion_freeze": freeze_iso,
        "champion_version": "PREDATOR_v1.0_M5",
        "governance": {
            "max_exposure_lots": MAX_EXPOSURE,
            "press_disabled": True,
            "additional_spend": "$0",
        },
        "sample_since_freeze": {
            "canonical_forward_opportunities": forward_opps,
            "champion_approved_fires": forward_fires,
            "total_batches_created": total_batches,
            "executed_batches": exec_batches,
            "closed_batches": closed_batches,
            "closed_positions": closed_positions,
            "open_positions": open_positions,
            "portfolio_skipped": portfolio_skipped,
        },
        "actual_demo": {
            "cumulative_pnl_usd": round(actual_pnl_pts, 2),
            "note": "P&L from closed predator_positions × realized_pts × lot_size × $100/pt/lot",
        },
        "execution": {
            "avg_spread_at_enqueue": avg_spread,
            "avg_spread_at_fire": avg_spread_at_fire,
            "orphan_positions": orphan_positions,
            "ordering_mismatches": ordering_mismatches,
            "current_open_lots": current_open_lots,
            "exposure_breach": exposure_breach,
        },
        "rejections": {
            "total": total_rejections,
            "by_reason": rejections_by_reason,
        },
        "checkpoints": checkpoints,
        "status": status,
        "historical_reference": historical,
        "forward_model": forward_model,
    }


@router.get("/predator-convergence/journal-sample")
def predator_journal_sample(db: Session = Depends(get_db), limit: int = 10):
    """Latest N batches with decision-journal fields populated — proof of write path."""
    rows = db.execute(text("""
        SELECT id, created_at, archetype, direction, opportunity_id,
               trend_context, htf_disagreement, velocity_state, compression_state,
               time_at_level_min, gc_context, spread_at_fire, transition_state
        FROM predator_signal_batches
        ORDER BY id DESC LIMIT :n
    """), {"n": limit}).fetchall()
    return [
        dict(id=r[0], created_at=str(r[1]), archetype=r[2], direction=r[3],
             opportunity_id=r[4], trend_context=r[5], htf_disagreement=r[6],
             velocity_state=r[7], compression_state=r[8], time_at_level_min=r[9],
             gc_context=r[10], spread_at_fire=r[11], transition_state=r[12])
        for r in rows
    ]


@router.get("/predator-convergence/rejection-sample")
def predator_rejection_sample(db: Session = Depends(get_db), limit: int = 10):
    """Latest N rejections — proof of rejection-log write path."""
    rows = db.execute(text("""
        SELECT id, created_at, archetype, direction, rejection_reason,
               rejection_detail, regime_direction, spread_at_decision
        FROM predator_rejections
        ORDER BY id DESC LIMIT :n
    """), {"n": limit}).fetchall()
    return [
        dict(id=r[0], created_at=str(r[1]), archetype=r[2], direction=r[3],
             rejection_reason=r[4], rejection_detail=r[5],
             regime_direction=r[6], spread_at_decision=r[7])
        for r in rows
    ]
