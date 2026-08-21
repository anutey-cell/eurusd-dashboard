"""
Predator forward-convergence diagnostics.

Read-only endpoint that surfaces:
  - Sample counters (LIFETIME vs FULLY INSTRUMENTED)
  - MODEL vs ACTUAL DEMO cumulative P&L + R capture + P&L capture
  - Convergence classification breakdown
  - Execution telemetry (spread, slippage, latency, failures)
  - Actual demo drawdown (HWM tracked chronologically)
  - Checkpoint distances (30/60/100 FULLY INSTRUMENTED closed batches)

Distinguishes HISTORICAL REPLAY, FORWARD MODEL, and ACTUAL DEMO — never
combines them into one N.

Triggering GET /diagnostics/predator-convergence?resolve=1 runs the
convergence resolver first, then reports.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, Query
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


def _compute_drawdown(db: Session, freeze_iso: str) -> dict:
    """Chronological equity walk on closed predator_positions since freeze."""
    starting = 10_000.0
    equity = starting
    peak = starting
    peak_time = None
    max_dd = 0.0
    max_dd_time = None
    max_dd_peak = starting
    max_dd_peak_time = None
    max_underwater_days = 0.0
    uw_start = None
    try:
        rows = db.execute(text("""
            SELECT closed_at, realized_pts, lot_size
            FROM predator_positions
            WHERE closed_at IS NOT NULL AND created_at >= :f
            ORDER BY closed_at ASC
        """), {"f": freeze_iso}).fetchall()
        for ct, pts, lot in rows:
            if pts is None or lot is None: continue
            equity += float(pts) * float(lot) * 100.0
            if equity > peak:
                peak = equity; peak_time = str(ct)
                uw_start = None
            else:
                dd = peak - equity
                if uw_start is None: uw_start = ct
                try:
                    uw_days = (ct - uw_start).total_seconds() / 86400 if hasattr(ct, "total_seconds") else 0
                except Exception: uw_days = 0
                if uw_days > max_underwater_days:
                    max_underwater_days = uw_days
                if dd > max_dd:
                    max_dd = dd; max_dd_time = str(ct); max_dd_peak = peak; max_dd_peak_time = peak_time
        return dict(
            starting=starting, current=round(equity, 2),
            peak=round(peak, 2), peak_time=peak_time,
            current_dd_usd=round(peak - equity, 2),
            current_dd_pct=round(100 * (peak - equity) / peak, 2) if peak else 0,
            max_dd_usd=round(max_dd, 2),
            max_dd_pct=round(100 * max_dd / max_dd_peak, 2) if max_dd_peak else 0,
            max_dd_peak_time=max_dd_peak_time,
            max_dd_trough_time=max_dd_time,
            max_underwater_days=round(max_underwater_days, 2),
        )
    except Exception as e:
        return dict(error=str(e))


@router.get("/predator-convergence")
def predator_convergence(
    db: Session = Depends(get_db),
    resolve: int = Query(0, description="if 1, run the resolver before reporting"),
):
    """Daily forward-convergence snapshot."""
    freeze_iso = CHAMPION_FREEZE.strftime("%Y-%m-%d %H:%M:%S")

    # Optional resolver pass
    resolver_result = None
    if resolve:
        try:
            from services.predator_convergence_resolver import resolve_closed_batches
            resolver_result = resolve_closed_batches(db, limit=200)
        except Exception as exc:
            resolver_result = {"error": str(exc)}

    # ── SAMPLE ─────────────────────────────────────────────────────────
    total_batches = _count(db, "SELECT COUNT(*) FROM predator_signal_batches "
                                "WHERE created_at >= :f", f=freeze_iso)
    exec_batches = _count(db, "SELECT COUNT(*) FROM predator_signal_batches "
                               "WHERE created_at >= :f AND execution_status IN ('COMPLETE','PARTIAL')",
                          f=freeze_iso)

    lifetime_closed_positions = _count(db, "SELECT COUNT(*) FROM predator_positions "
                                             "WHERE closed_at IS NOT NULL")
    closed_positions_since_freeze = _count(db, "SELECT COUNT(*) FROM predator_positions "
                                                 "WHERE closed_at IS NOT NULL AND created_at >= :f",
                                            f=freeze_iso)
    open_positions = _count(db, "SELECT COUNT(*) FROM predator_positions "
                                 "WHERE status IN ('ENQUEUED','OPEN') AND created_at >= :f",
                            f=freeze_iso)

    lifetime_closed_batches = _count(db, """
        SELECT COUNT(*) FROM (
          SELECT b.id FROM predator_signal_batches b
          WHERE b.execution_status IN ('COMPLETE','PARTIAL')
          GROUP BY b.id
          HAVING SUM(CASE WHEN EXISTS (
              SELECT 1 FROM predator_positions p
              WHERE p.batch_id = b.id AND p.closed_at IS NULL
                AND p.status IN ('ENQUEUED','OPEN')
          ) THEN 1 ELSE 0 END) = 0
        )
    """)
    closed_batches_since_freeze = _count(db, """
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

    fully_instrumented = _count(db, "SELECT COUNT(*) FROM predator_demo_convergence")

    forward_opps = _count(db, "SELECT COUNT(*) FROM predator_forward_opportunities "
                                "WHERE created_at >= :f", f=freeze_iso)
    forward_fires = _count(db, "SELECT COUNT(*) FROM predator_forward_opportunities "
                                 "WHERE created_at >= :f AND model_decision='FIRE'", f=freeze_iso)
    portfolio_skipped = _count(db, "SELECT COUNT(*) FROM predator_forward_opportunities "
                                     "WHERE created_at >= :f AND portfolio_decision LIKE 'SKIPPED%'",
                               f=freeze_iso)

    # ── ACTUAL DEMO ────────────────────────────────────────────────────
    actual_pnl_since_freeze = _scalar(db, """
        SELECT COALESCE(SUM(p.realized_pts * p.lot_size * 100), 0)
        FROM predator_positions p
        WHERE p.closed_at IS NOT NULL AND p.created_at >= :f
    """, f=freeze_iso) or 0.0
    actual_pnl_lifetime = _scalar(db, """
        SELECT COALESCE(SUM(p.realized_pts * p.lot_size * 100), 0)
        FROM predator_positions p
        WHERE p.closed_at IS NOT NULL
    """) or 0.0

    # ── CONVERGENCE (paired closed batches) ────────────────────────────
    conv_stats = db.execute(text("""
        SELECT
            COUNT(*),
            SUM(model_pnl_usd), SUM(actual_pnl_usd),
            SUM(model_r), SUM(actual_r),
            AVG(entry_slippage_pts), AVG(exit_diff_pts)
        FROM predator_demo_convergence
    """)).fetchone()
    n_paired = int(conv_stats[0]) if conv_stats[0] else 0
    model_pnl_total = float(conv_stats[1]) if conv_stats[1] else 0.0
    actual_pnl_total = float(conv_stats[2]) if conv_stats[2] else 0.0
    model_r_total = float(conv_stats[3]) if conv_stats[3] else 0.0
    actual_r_total = float(conv_stats[4]) if conv_stats[4] else 0.0
    avg_slip = float(conv_stats[5]) if conv_stats[5] is not None else None
    avg_exit_diff = float(conv_stats[6]) if conv_stats[6] is not None else None

    pnl_capture = round(actual_pnl_total / model_pnl_total, 3) if model_pnl_total else None
    r_capture = round(actual_r_total / model_r_total, 3) if model_r_total else None

    class_counts = {}
    try:
        for r in db.execute(text(
            "SELECT convergence_class, COUNT(*) FROM predator_demo_convergence "
            "GROUP BY convergence_class"
        )).fetchall():
            class_counts[r[0] or "UNCLASSIFIED"] = int(r[1])
    except Exception:
        pass

    # ── REJECTIONS split ───────────────────────────────────────────────
    strategy_rej = 0; portfolio_rej = 0; rejections_by_reason = {}
    try:
        for r in db.execute(text(
            "SELECT rejection_reason, COUNT(*) FROM predator_rejections "
            "WHERE created_at >= :f GROUP BY rejection_reason"
        ), {"f": freeze_iso}).fetchall():
            rejections_by_reason[r[0]] = int(r[1])
            if str(r[0]).startswith("PORTFOLIO_"): portfolio_rej += int(r[1])
            else: strategy_rej += int(r[1])
    except Exception:
        pass

    # ── EXPOSURE + ORDERING ────────────────────────────────────────────
    current_open_lots = _scalar(db, """
        SELECT COALESCE(SUM(lot_size), 0) FROM predator_positions
        WHERE status IN ('ENQUEUED','OPEN')
    """) or 0.0
    exposure_breach = current_open_lots > MAX_EXPOSURE + 1e-6

    ordering_mismatches = 0
    try:
        prio = {"ASIAN_BREAKDOWN": 0, "PDL_BREAK": 1, "VOL_CONTINUATION": 2}
        rows = db.execute(text(
            "SELECT created_at, archetype FROM predator_signal_batches "
            "WHERE created_at >= :f ORDER BY created_at, id"
        ), {"f": freeze_iso}).fetchall()
        by_ts = {}
        for ts, arch in rows:
            by_ts.setdefault(str(ts)[:19], []).append(arch)
        for archs in by_ts.values():
            if len(archs) < 2: continue
            expected = sorted(archs, key=lambda a: prio.get(a, 99))
            if archs != expected: ordering_mismatches += 1
    except Exception:
        pass

    orphan_positions = _count(db, """
        SELECT COUNT(*) FROM predator_positions p
        WHERE p.mt5_ticket IS NOT NULL
          AND NOT EXISTS (SELECT 1 FROM predator_signal_batches b WHERE b.id = p.batch_id)
    """)

    # ── DRAWDOWN (actual demo, since freeze) ───────────────────────────
    dd = _compute_drawdown(db, freeze_iso)

    # ── LOSING STREAK ─────────────────────────────────────────────────
    longest_streak = 0
    try:
        rows = db.execute(text("""
            SELECT SUM(p.realized_pts * p.lot_size * 100) as batch_pnl
            FROM predator_positions p
            JOIN predator_signal_batches b ON p.batch_id = b.id
            WHERE p.closed_at IS NOT NULL AND p.created_at >= :f
            GROUP BY p.batch_id
            ORDER BY MAX(p.closed_at)
        """), {"f": freeze_iso}).fetchall()
        cur = 0
        for row in rows:
            if row[0] is not None and float(row[0]) < 0:
                cur += 1
                if cur > longest_streak: longest_streak = cur
            else:
                cur = 0
    except Exception:
        pass

    # ── CHECKPOINTS (FULLY INSTRUMENTED) ───────────────────────────────
    checkpoints = {
        "fully_instrumented_closed_batches": fully_instrumented,
        "distance_to_30": max(0, 30 - fully_instrumented),
        "distance_to_60": max(0, 60 - fully_instrumented),
        "distance_to_100": max(0, 100 - fully_instrumented),
    }

    # ── STATUS ─────────────────────────────────────────────────────────
    if fully_instrumented < 30:
        status = "EARLY SAMPLE — INSUFFICIENT (< 30 fully instrumented closed batches)"
    elif fully_instrumented < 60:
        status = "CONVERGING — continue observation to Checkpoint B (60)"
    elif fully_instrumented < 100:
        status = "MID-STAGE — continue observation to Checkpoint C (100)"
    else:
        status = "READY FOR FORWARD VALIDATION REVIEW"

    return {
        "as_of": datetime.now(timezone.utc).isoformat(),
        "champion": {
            "version": "PREDATOR_v1.0_M5",
            "freeze": freeze_iso,
            "archetype_order": ["ASIAN_BREAKDOWN", "PDL_BREAK", "VOL_CONTINUATION"],
        },
        "governance": {
            "max_exposure_lots": MAX_EXPOSURE,
            "press_disabled": True,
            "additional_spend": "$0",
            "expansion_config_flag": True,
            "expansion_actual_triggered_since_freeze": False,
        },
        "sample_since_freeze": {
            "canonical_forward_opportunities": forward_opps,
            "champion_approved_fires": forward_fires,
            "total_batches_created": total_batches,
            "executed_batches": exec_batches,
            "closed_batches": closed_batches_since_freeze,
            "closed_positions": closed_positions_since_freeze,
            "open_positions": open_positions,
            "portfolio_skipped": portfolio_skipped,
        },
        "sample_lifetime": {
            "closed_batches": lifetime_closed_batches,
            "closed_positions": lifetime_closed_positions,
            "note": "Includes batches from BEFORE convergence instrumentation existed",
        },
        "fully_instrumented": {
            "count": fully_instrumented,
            "note": ("Batches with a row in predator_demo_convergence — "
                     "have frozen journal + model + actual + pairing"),
        },
        "actual_demo": {
            "cumulative_pnl_usd_since_freeze": round(actual_pnl_since_freeze, 2),
            "cumulative_pnl_usd_lifetime": round(actual_pnl_lifetime, 2),
            "note": "P&L = SUM(realized_pts × lot_size × $100/pt/lot)",
        },
        "convergence": {
            "paired_batches": n_paired,
            "model_pnl_total": round(model_pnl_total, 2),
            "actual_pnl_total": round(actual_pnl_total, 2),
            "model_r_total": round(model_r_total, 2),
            "actual_r_total": round(actual_r_total, 2),
            "pnl_capture_ratio": pnl_capture,
            "r_capture_ratio": r_capture,
            "avg_entry_slippage_pts": avg_slip,
            "avg_exit_diff_pts": avg_exit_diff,
            "classifications": class_counts,
        },
        "drawdown_actual_demo": dd,
        "risk": {
            "longest_losing_streak_batches": longest_streak,
        },
        "execution": {
            "orphan_positions": orphan_positions,
            "ordering_mismatches": ordering_mismatches,
            "current_open_lots": current_open_lots,
            "exposure_breach": exposure_breach,
            "avg_spread_at_fire": _scalar(db,
                "SELECT AVG(spread_at_fire) FROM predator_signal_batches "
                "WHERE spread_at_fire IS NOT NULL AND created_at >= :f",
                f=freeze_iso),
            "avg_spread_at_enqueue": _scalar(db,
                "SELECT AVG(spread_at_enqueue) FROM predator_positions "
                "WHERE spread_at_enqueue IS NOT NULL AND created_at >= :f",
                f=freeze_iso),
        },
        "rejections": {
            "total": strategy_rej + portfolio_rej,
            "strategy_level": strategy_rej,
            "portfolio_level": portfolio_rej,
            "by_reason": rejections_by_reason,
        },
        "checkpoints": checkpoints,
        "status": status,
        "resolver": resolver_result,
        "historical_reference": {
            "note": "From CLOSURE-2 conservative baseline. NOT combined with forward N.",
            "conservative_portfolio_pnl_normal": 35737,
            "mean_batch_r_normal": 0.210,
            "max_dd_pct_normal": 21.3,
            "rolling_90d_positive": "5/6",
            "baseline_n_executed_batches": 508,
        },
    }


@router.get("/predator-convergence/journal-sample")
def journal_sample(db: Session = Depends(get_db), limit: int = 10):
    rows = db.execute(text("""
        SELECT id, created_at, archetype, direction, opportunity_id,
               trend_context, htf_disagreement, velocity_state, compression_state,
               time_at_level_min, gc_context, spread_at_fire, transition_state
        FROM predator_signal_batches
        ORDER BY id DESC LIMIT :n
    """), {"n": limit}).fetchall()
    return [dict(id=r[0], created_at=str(r[1]), archetype=r[2], direction=r[3],
                 opportunity_id=r[4], trend_context=r[5], htf_disagreement=r[6],
                 velocity_state=r[7], compression_state=r[8], time_at_level_min=r[9],
                 gc_context=r[10], spread_at_fire=r[11], transition_state=r[12])
            for r in rows]


@router.get("/predator-convergence/rejection-sample")
def rejection_sample(db: Session = Depends(get_db), limit: int = 20):
    rows = db.execute(text("""
        SELECT id, created_at, archetype, direction, rejection_reason,
               rejection_detail, spread_at_decision, opportunity_id
        FROM predator_rejections
        ORDER BY id DESC LIMIT :n
    """), {"n": limit}).fetchall()
    return [dict(id=r[0], created_at=str(r[1]), archetype=r[2], direction=r[3],
                 rejection_reason=r[4], rejection_detail=r[5],
                 spread_at_decision=r[6], opportunity_id=r[7])
            for r in rows]


@router.get("/predator-convergence/convergence-sample")
def convergence_sample(db: Session = Depends(get_db), limit: int = 20):
    rows = db.execute(text("""
        SELECT id, created_at, opportunity_id, batch_id, convergence_class,
               model_pnl_usd, actual_pnl_usd, model_r, actual_r,
               entry_slippage_pts, exit_diff_pts
        FROM predator_demo_convergence
        ORDER BY id DESC LIMIT :n
    """), {"n": limit}).fetchall()
    return [dict(id=r[0], created_at=str(r[1]), opportunity_id=r[2], batch_id=r[3],
                 convergence_class=r[4], model_pnl_usd=r[5], actual_pnl_usd=r[6],
                 model_r=r[7], actual_r=r[8], entry_slippage_pts=r[9],
                 exit_diff_pts=r[10])
            for r in rows]


@router.get("/dual-mandate-health")
def dual_mandate_health(db: Session = Depends(get_db)):
    """Startup-and-runtime health block for the dual-mandate architecture."""
    from services.portfolio_governor import snapshot as gov_snapshot, MAX_GROSS_LOTS
    from services.strategist import STRATEGIST_MODEL_VERSION
    snap = gov_snapshot(db)
    return {
        "as_of": datetime.now(timezone.utc).isoformat(),
        "engines": {
            "PREDATOR_SELL_MT5":        "ACTIVE",
            "PREDATOR_BUY_MT5":         "DISABLED",
            "STRATEGIST_BUY_MT5":       "ACTIVE",
            "STRATEGIST_SELL_MT5":      "DISABLED — SHADOW ONLY",
            "STRATEGIST_SELL_OBSERVE":  "ACTIVE",
        },
        "governance": {
            "GLOBAL_GOVERNOR":                    "ACTIVE",
            "ATOMIC_CAPACITY_RESERVATION":        "ACTIVE",
            "MT5_AUTHORITATIVE_RECONCILIATION":   ("ACTIVE" if snap["mt5_authoritative"] else "PARTIAL"),
            "GLOBAL_XAUUSD_GROSS_MAX":            MAX_GROSS_LOTS,
            "PREDATOR_PRESS_0_30":                "DISABLED",
            "STRATEGIST_PYRAMID":                 "DISABLED",
        },
        "ledgers": {
            "STRATEGIST_BUY_OUTCOME_LEDGER":  "ACTIVE",
            "STRATEGIST_SELL_SHADOW_LEDGER":  "ACTIVE",
        },
        "parked": {
            "VAL_RECLAIM":                   "PARKED",
            "NEW_BUY_CONTINUATION":          "PARKED",
        },
        "spend":  "$0",
        "regime": "DEMO ONLY",
        "versions": {
            "predator":   "PREDATOR_v1.0_M5",
            "strategist": STRATEGIST_MODEL_VERSION,
        },
        "exposure": snap,
    }


@router.get("/predator-convergence/traceability/{batch_id}")
def traceability(batch_id: int, db: Session = Depends(get_db)):
    """Demonstrate the full chain for one batch."""
    b = db.execute(text("""
        SELECT id, opportunity_id, signal_id, archetype, direction,
               entry_price, stop_loss, tp1, tp2, execution_status, created_at,
               spread_at_fire, trend_context
        FROM predator_signal_batches WHERE id = :b
    """), {"b": batch_id}).fetchone()
    if not b: return {"error": f"batch {batch_id} not found"}

    positions = db.execute(text("""
        SELECT id, seq_no, mt5_ticket, opened_at, closed_at,
               entry_price_planned, price_at_enqueue, exit_price,
               outcome, realized_pts, lot_size
        FROM predator_positions WHERE batch_id = :b ORDER BY seq_no
    """), {"b": batch_id}).fetchall()
    convergence = db.execute(text(
        "SELECT * FROM predator_demo_convergence WHERE batch_id = :b LIMIT 1"
    ), {"b": batch_id}).fetchone()

    return {
        "batch": dict(id=b[0], opportunity_id=b[1], signal_id=b[2],
                      archetype=b[3], direction=b[4], entry=b[5], sl=b[6],
                      tp1=b[7], tp2=b[8], status=b[9], created_at=str(b[10]),
                      spread_at_fire=b[11], trend_context=b[12]),
        "positions": [dict(id=p[0], seq=p[1], mt5_ticket=p[2],
                           opened=str(p[3]), closed=str(p[4]),
                           entry_planned=p[5], actual_entry=p[6],
                           exit=p[7], outcome=p[8], pts=p[9], lot=p[10])
                      for p in positions],
        "convergence": ({"present": True,
                         "classification": convergence[-1] if convergence else None,
                         "cols": len(convergence) if convergence else 0}
                        if convergence else {"present": False}),
    }
