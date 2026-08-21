"""
Strategist BUY closure resolver.

Automatically pairs open strategist_buy_outcomes rows with closed
strategist_verdicts (populated by daemon post-trade writeback) and
computes actual P&L, R, MFE/MAE, holding time.

Daemon writeback path (verified in routers/bridge.py::_report_result):
  daemon POST /bridge/report/pending
   → bridge updates strategist_verdicts (pips_outcome, mfe_pts, mae_pts,
     result WIN|LOSS|BREAKEVEN, mt5_ticket)
   → this resolver reads those fields, converts to USD via lot × $100/pt,
     and writes to strategist_buy_outcomes.

USD_PER_PT = 100 (for XAUUSD, 1.0 lot × 1pt = $100)

Also backfills the 16 pre-instrumentation rally-week BUYs into the ledger
with cohort='PRE_INSTRUMENTATION_ACTUAL_DEMO' so they aren't silently lost
but are excluded from the forward validation counter.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import text
from sqlalchemy.orm import Session

log = logging.getLogger(__name__)

USD_PER_PT_PER_LOT = 100.0
COHORT_FORWARD = "FULLY_INSTRUMENTED_FORWARD"
COHORT_PRE = "PRE_INSTRUMENTATION_ACTUAL_DEMO"


def resolve_closed_buys(db: Session, limit: int = 100) -> dict:
    """Resolve OPEN strategist_buy_outcomes whose linked strategist_verdicts
    now has a closure result. Returns counts."""
    stats = dict(scanned=0, resolved=0, still_open=0, orphan=0, errors=0)
    try:
        rows = db.execute(text("""
            SELECT sbo.id, sbo.pending_execution_id, sbo.entry, sbo.sl, sbo.lot_size,
                   sv.pips_outcome, sv.mfe_pts, sv.mae_pts, sv.result, sv.mt5_ticket,
                   sv.created_at as verdict_time
            FROM strategist_buy_outcomes sbo
            LEFT JOIN strategist_verdicts sv
              ON sv.pending_execution_id = sbo.pending_execution_id
            WHERE sbo.resolution_status = 'OPEN'
              AND (sbo.cohort IS NULL OR sbo.cohort = :cohort_fwd)
            ORDER BY sbo.id ASC LIMIT :n
        """), {"cohort_fwd": COHORT_FORWARD, "n": limit}).fetchall()

        for r in rows:
            stats["scanned"] += 1
            sbo_id, pe_id, entry, sl, lot, pips, mfe, mae, result, ticket, verdict_time = r
            if pe_id is None:
                stats["orphan"] += 1
                _mark_orphan(db, sbo_id)
                continue
            if pips is None and not result:
                stats["still_open"] += 1
                continue

            # Compute
            sl_pts = abs(float(entry) - float(sl)) if (entry and sl) else 0.0
            initial_risk_usd = float(lot) * USD_PER_PT_PER_LOT * sl_pts if lot else 0.0
            actual_pnl_pts = float(pips) if pips is not None else 0.0
            actual_pnl_usd = actual_pnl_pts * float(lot) * USD_PER_PT_PER_LOT if lot else 0.0
            actual_r = actual_pnl_usd / initial_risk_usd if initial_risk_usd else None

            outcome_map = {"WIN": "TP", "LOSS": "SL", "BREAKEVEN": "BE"}
            outcome = outcome_map.get(str(result).upper() if result else "", None)

            db.execute(text("""
                UPDATE strategist_buy_outcomes SET
                  resolution_status = 'RESOLVED',
                  resolved_at = :ra,
                  mt5_ticket = :tk,
                  actual_pnl_usd = :pu, actual_pnl_pts = :pp, actual_r = :ar,
                  mfe_pts = :mfe, mae_pts = :mae,
                  outcome = :oc
                WHERE id = :sid
            """), dict(
                ra=datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
                tk=ticket,
                pu=round(actual_pnl_usd, 2),
                pp=round(actual_pnl_pts, 2),
                ar=round(actual_r, 3) if actual_r is not None else None,
                mfe=float(mfe) if mfe is not None else None,
                mae=float(mae) if mae is not None else None,
                oc=outcome,
                sid=sbo_id,
            ))
            db.commit()
            stats["resolved"] += 1
    except Exception as exc:
        log.warning("[strategist_buy_resolver] scan failed: %s", exc)
        stats["errors"] += 1
        try: db.rollback()
        except Exception: pass
    return stats


def _mark_orphan(db: Session, sbo_id: int) -> None:
    try:
        db.execute(text(
            "UPDATE strategist_buy_outcomes SET resolution_status='ORPHAN' WHERE id=:i"
        ), {"i": sbo_id})
        db.commit()
    except Exception:
        try: db.rollback()
        except Exception: pass


def backfill_pre_instrumentation_cohort(db: Session, dry_run: bool = False) -> dict:
    """One-time backfill of the 16 rally-week Strategist BUY tickets that
    predated this ledger. Labels them PRE_INSTRUMENTATION_ACTUAL_DEMO so
    they are preserved but NOT counted toward the forward N=30 gate."""
    stats = dict(scanned=0, inserted=0, skipped_existing=0, no_verdict=0)
    try:
        # All Strategist-path BUY tickets (from mt5_trade_logs) that don't
        # yet have a strategist_buy_outcomes row.
        tickets = db.execute(text("""
            SELECT mtl.ticket, mtl.created_at, mtl.signal, mtl.entry,
                   mtl.stop_loss, mtl.take_profit, mtl.volume, mtl.raw_response_json
            FROM mt5_trade_logs mtl
            WHERE mtl.pair='xauusd' AND mtl.status='accepted' AND mtl.signal='BUY'
              AND NOT EXISTS (
                SELECT 1 FROM strategist_buy_outcomes sbo
                WHERE sbo.mt5_ticket = mtl.ticket
              )
            ORDER BY mtl.created_at ASC
        """)).fetchall()

        for r in tickets:
            stats["scanned"] += 1
            ticket, ct, signal, entry, sl, tp, vol, raw = r
            # Try to find the linked strategist_verdicts row
            sv = db.execute(text("""
                SELECT id, created_at, conditions_passed, setup_score, quality_band,
                       market_state, pips_outcome, mfe_pts, mae_pts, result, pending_execution_id
                FROM strategist_verdicts
                WHERE mt5_ticket = :tk
                ORDER BY created_at DESC LIMIT 1
            """), {"tk": ticket}).fetchone()
            if not sv:
                stats["no_verdict"] += 1
            # Even without verdict link, record the ticket with best-effort fields
            opp_id = f"PRE·{ct}·{round(entry or 0, 1)}·tk{ticket}"
            if dry_run: continue
            try:
                db.execute(text("""
                    INSERT INTO strategist_buy_outcomes
                      (created_at, opportunity_id, verdict_id, pending_execution_id,
                       mt5_ticket, conditions_passed, setup_score, quality_band,
                       market_state, entry, sl, tp1, tp2, lot_size,
                       resolution_status, cohort, canonicalization_version)
                    VALUES
                      (:ca, :oi, :vi, :pe, :tk, :cp, :ss, :qb, :ms,
                       :en, :sl, :t1, :t2, :ls, 'OPEN', :co, 'PROVISIONAL_V0')
                """), dict(
                    ca=str(ct),
                    oi=opp_id,
                    vi=sv[0] if sv else None,
                    pe=sv[10] if sv else None,
                    tk=ticket,
                    cp=sv[2] if sv else None,
                    ss=sv[3] if sv else None,
                    qb=sv[4] if sv else None,
                    ms=sv[5] if sv else None,
                    en=float(entry) if entry else None,
                    sl=float(sl) if sl else None,
                    t1=float(tp) if tp else None,
                    t2=None,
                    ls=float(vol) if vol else None,
                    co=COHORT_PRE,
                ))
                db.commit()
                stats["inserted"] += 1
            except Exception as _iex:
                log.debug("[strategist_buy_resolver] insert failed for ticket %s: %s",
                          ticket, _iex)
                try: db.rollback()
                except Exception: pass
    except Exception as exc:
        log.warning("[strategist_buy_resolver] backfill failed: %s", exc)
    return stats


def fully_instrumented_closed_count(db: Session) -> int:
    """Count of resolved BUY outcomes in the FORWARD cohort — the counter
    that drives the 30/60/100 checkpoints."""
    try:
        r = db.execute(text("""
            SELECT COUNT(*) FROM strategist_buy_outcomes
            WHERE resolution_status='RESOLVED' AND cohort = :c
        """), {"c": COHORT_FORWARD}).fetchone()
        return int(r[0]) if r else 0
    except Exception:
        return 0


def resolved_sell_shadow_count(db: Session) -> int:
    try:
        r = db.execute(text("""
            SELECT COUNT(*) FROM strategist_sell_shadow
            WHERE resolution_status='RESOLVED'
        """)).fetchone()
        return int(r[0]) if r else 0
    except Exception:
        return 0
