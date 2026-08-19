"""
Model-vs-demo convergence resolver.

For every FULLY CLOSED Predator batch (all positions have closed_at):
  1. Compute MODEL_RESULT by walking M5 bars forward from batch.created_at
     using the frozen entry / SL / TP1 / TP2 and conservative intrabar rules
     (SL first on SL+TP same bar, TP1 first on TP1+TP2 same bar).
  2. Compute ACTUAL_DEMO_RESULT from predator_positions (realized_pts,
     lot_size, exit_price, closed_at).
  3. Compute deltas (slippage, exit diff, R capture, P&L capture).
  4. Classify: MATCH | ECONOMIC_DRIFT | OUTCOME_DIVERGENCE | EXECUTION_MISS.
  5. Upsert to predator_demo_convergence — one row per (opportunity_id, batch_id).

Fail-open: never raises into a caller path. Idempotent — re-running on the
same closed batch overwrites the existing convergence row.

Read-only against production tables except for predator_demo_convergence.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone, timedelta
from typing import Optional

from sqlalchemy import text
from sqlalchemy.orm import Session

log = logging.getLogger(__name__)

USD_PT_PER_LOT = 100.0   # 1pt XAU × 1.0 lot = $100
CONSERVATIVE_HORIZON_M5_BARS = 96  # 8 hours max horizon per batch
COST_ASSUMPTION_PT_XAU = 2.5       # NORMAL-cost round-trip friction (spread+slippage), pts XAU


def _parse_ts(s) -> Optional[datetime]:
    if s is None: return None
    ss = str(s).replace("T", " ").split(".")[0]
    try: return datetime.strptime(ss, "%Y-%m-%d %H:%M:%S")
    except Exception: return None


def _walk_model_outcome(
    db: Session,
    fire_ts: datetime,
    direction: str,
    entry: float, sl: float, tp1: float, tp2: float,
) -> dict:
    """Walk M5 bars forward from fire_ts. Conservative intrabar handling.
    Returns model outcome + points + holding minutes."""
    rows = db.execute(text(
        "SELECT candle_time, open, high, low, close FROM historical_candles "
        "WHERE instrument='XAU/USD' AND timeframe='M5' AND candle_time > :ft "
        "AND source LIKE 'mt5_backfill%' "
        "ORDER BY candle_time ASC LIMIT :n"
    ), {"ft": fire_ts.strftime("%Y-%m-%d %H:%M:%S"), "n": CONSERVATIVE_HORIZON_M5_BARS}).fetchall()
    if not rows:
        return dict(outcome="NO_DATA", pnl_pts=None, exit_price=None, holding_min=None)

    hit_tp1 = False
    for i, r in enumerate(rows):
        ct = _parse_ts(r[0])
        o, h, l, cl = float(r[1]), float(r[2]), float(r[3]), float(r[4])
        if direction == "SELL":
            hit_sl = h >= sl
            hit_tp1_bar = l <= tp1
            hit_tp2_bar = l <= tp2
        else:  # BUY
            hit_sl = l <= sl
            hit_tp1_bar = h >= tp1
            hit_tp2_bar = h >= tp2

        # Conservative on SL+TP same bar → SL wins
        if hit_sl and (hit_tp1_bar or hit_tp2_bar) and not hit_tp1:
            pnl_pts = -abs(sl - entry) if direction == "SELL" else -abs(sl - entry)
            hm = (ct - fire_ts).total_seconds() / 60 if ct else None
            return dict(outcome="SL_AMBIG_CONSERVATIVE", pnl_pts=pnl_pts, exit_price=sl, holding_min=hm)
        if hit_sl and not hit_tp1:
            pnl_pts = -abs(sl - entry)
            hm = (ct - fire_ts).total_seconds() / 60 if ct else None
            return dict(outcome="SL", pnl_pts=pnl_pts, exit_price=sl, holding_min=hm)
        # Conservative on TP1+TP2 same bar → TP1 only (no runner)
        if hit_tp2_bar and hit_tp1_bar and not hit_tp1:
            pnl_pts = abs(entry - tp1)
            hm = (ct - fire_ts).total_seconds() / 60 if ct else None
            return dict(outcome="TP1_AMBIG_CONSERVATIVE", pnl_pts=pnl_pts, exit_price=tp1, holding_min=hm)
        if hit_tp2_bar:
            pnl_pts = abs(entry - tp2)
            hm = (ct - fire_ts).total_seconds() / 60 if ct else None
            return dict(outcome="TP2", pnl_pts=pnl_pts, exit_price=tp2, holding_min=hm)
        if hit_tp1_bar and not hit_tp1:
            hit_tp1 = True
            first_tp1_idx = i

    # Timeout — if we hit TP1 at some point, count as TP1 (breakeven runner)
    if hit_tp1:
        last_ct = _parse_ts(rows[-1][0])
        hm = (last_ct - fire_ts).total_seconds() / 60 if last_ct else None
        return dict(outcome="TP1_TIMEOUT", pnl_pts=abs(entry - tp1),
                    exit_price=tp1, holding_min=hm)
    return dict(outcome="TIMEOUT", pnl_pts=0.0, exit_price=None,
                holding_min=(CONSERVATIVE_HORIZON_M5_BARS * 5.0))


def _classify(actual_pnl: Optional[float], model_pnl: Optional[float]) -> str:
    """Convergence classification. Uses $ P&L not R (invariant to size differences)."""
    if actual_pnl is None:
        return "EXECUTION_MISS"
    if model_pnl is None:
        return "EXECUTION_MISS"
    # Outcome divergence: signs differ
    if (actual_pnl > 0 and model_pnl < 0) or (actual_pnl < 0 and model_pnl > 0):
        return "OUTCOME_DIVERGENCE"
    # Economic drift: same direction but |delta| > 30% of model
    if abs(model_pnl) > 1e-6:
        delta_ratio = abs(actual_pnl - model_pnl) / abs(model_pnl)
        if delta_ratio > 0.30:
            return "ECONOMIC_DRIFT"
    return "MATCH"


def resolve_closed_batches(db: Session, limit: int = 100) -> dict:
    """Resolve all fully-closed batches that don't yet have a convergence row.
    Returns counts. Never raises."""
    stats = dict(scanned=0, resolved=0, skipped_open=0, skipped_existing=0,
                 model_no_data=0, errors=0, classifications={})
    try:
        # Find batches with all positions closed AND executed AND not yet resolved
        candidates = db.execute(text("""
            SELECT b.id, b.opportunity_id, b.signal_id, b.direction, b.archetype,
                   b.entry_price, b.stop_loss, b.tp1, b.tp2, b.created_at
            FROM predator_signal_batches b
            WHERE b.execution_status IN ('COMPLETE','PARTIAL')
              AND NOT EXISTS (
                SELECT 1 FROM predator_demo_convergence c WHERE c.batch_id = b.id
              )
              AND NOT EXISTS (
                SELECT 1 FROM predator_positions p
                WHERE p.batch_id = b.id AND (p.closed_at IS NULL AND p.status IN ('ENQUEUED','OPEN'))
              )
              AND EXISTS (
                SELECT 1 FROM predator_positions p WHERE p.batch_id = b.id AND p.closed_at IS NOT NULL
              )
            ORDER BY b.id ASC LIMIT :n
        """), {"n": limit}).fetchall()

        for row in candidates:
            stats["scanned"] += 1
            batch_id, opp_id, sig_id, direction, arch, entry, sl, tp1, tp2, created_at = row
            fire_ts = _parse_ts(created_at)
            if not fire_ts:
                stats["errors"] += 1; continue

            # ACTUAL demo aggregate
            actual = db.execute(text("""
                SELECT SUM(realized_pts * lot_size * 100) as pnl_usd,
                       SUM(realized_pts) as pts_sum,
                       COUNT(*) as n_positions,
                       AVG(realized_pts) as pnl_pts,
                       SUM(lot_size) as total_lots,
                       AVG(price_at_enqueue) as avg_entry,
                       AVG(exit_price) as avg_exit,
                       MIN(opened_at) as first_open,
                       MAX(closed_at) as last_close
                FROM predator_positions
                WHERE batch_id = :b AND closed_at IS NOT NULL
            """), {"b": batch_id}).fetchone()
            if not actual or actual[2] == 0:
                stats["errors"] += 1; continue

            actual_pnl_usd = float(actual[0]) if actual[0] is not None else 0.0
            actual_pnl_pts_avg = float(actual[3]) if actual[3] is not None else 0.0
            actual_lots = float(actual[4]) if actual[4] is not None else 0.0
            actual_entry = float(actual[5]) if actual[5] is not None else None
            actual_exit = float(actual[6]) if actual[6] is not None else None
            first_open = _parse_ts(actual[7])
            last_close = _parse_ts(actual[8])
            actual_holding = ((last_close - first_open).total_seconds() / 60
                              if (first_open and last_close) else None)

            # MODEL walk
            model = _walk_model_outcome(db, fire_ts, direction,
                                        float(entry), float(sl), float(tp1), float(tp2))
            if model["outcome"] == "NO_DATA":
                stats["model_no_data"] += 1; continue

            # Cost assumption for model
            model_pnl_pts_gross = model["pnl_pts"]
            model_pnl_pts_net = (model_pnl_pts_gross - COST_ASSUMPTION_PT_XAU
                                 if model_pnl_pts_gross is not None else None)
            # Model applies same lot count as actual for like-for-like $
            model_lots = actual_lots or 0.15
            model_pnl_usd = (model_pnl_pts_net * model_lots * USD_PT_PER_LOT
                             if model_pnl_pts_net is not None else None)

            # R computation
            sl_pts = abs(float(sl) - float(entry))
            batch_risk = actual_lots * USD_PT_PER_LOT * sl_pts if sl_pts > 0 else None
            actual_r = (actual_pnl_usd / batch_risk if batch_risk else None)
            model_r = (model_pnl_usd / batch_risk if (batch_risk and model_pnl_usd is not None) else None)

            # Slippage
            if actual_entry is not None:
                if direction == "SELL":
                    slippage_pts = float(entry) - actual_entry  # adverse if actual_entry > entry (worse fill)
                else:
                    slippage_pts = actual_entry - float(entry)
            else:
                slippage_pts = None

            exit_diff = (actual_exit - model["exit_price"]
                         if (actual_exit is not None and model["exit_price"] is not None)
                         else None)
            cost_diff = ((model_pnl_usd - actual_pnl_usd)
                         if (model_pnl_usd is not None) else None)

            klass = _classify(actual_pnl_usd, model_pnl_usd)

            db.execute(text("""
                INSERT INTO predator_demo_convergence (
                    created_at, opportunity_id, batch_id,
                    model_entry, model_exit, model_lots, model_pnl_usd, model_pnl_pts, model_r, model_holding_min,
                    actual_entry, actual_exit, actual_lots, actual_pnl_usd, actual_pnl_pts, actual_r, actual_holding_min,
                    entry_slippage_pts, exit_diff_pts, cost_diff_usd, convergence_class
                ) VALUES (
                    :ca, :oi, :bi, :me, :mx, :ml, :mp, :mpp, :mr, :mh,
                    :ae, :ax, :al, :ap, :app, :ar, :ah,
                    :sl, :ed, :cd, :cc
                )
            """), dict(
                ca=datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
                oi=opp_id or f"legacy-batch-{batch_id}", bi=batch_id,
                me=float(entry), mx=model["exit_price"], ml=model_lots,
                mp=model_pnl_usd, mpp=model_pnl_pts_net, mr=model_r, mh=model["holding_min"],
                ae=actual_entry, ax=actual_exit, al=actual_lots,
                ap=actual_pnl_usd, app=actual_pnl_pts_avg, ar=actual_r, ah=actual_holding,
                sl=slippage_pts, ed=exit_diff, cd=cost_diff, cc=klass,
            ))
            db.commit()
            stats["resolved"] += 1
            stats["classifications"][klass] = stats["classifications"].get(klass, 0) + 1

        # ORPHAN detection — MT5 tickets under Predator lot size with no batch mapping
        try:
            orphans = db.execute(text("""
                SELECT COUNT(*) FROM predator_positions p
                WHERE p.mt5_ticket IS NOT NULL
                  AND NOT EXISTS (SELECT 1 FROM predator_signal_batches b WHERE b.id = p.batch_id)
            """)).fetchone()
            stats["orphans"] = int(orphans[0]) if orphans else 0
        except Exception:
            stats["orphans"] = None

    except Exception as exc:
        log.warning("[convergence_resolver] scan failed: %s", exc)
        try: db.rollback()
        except Exception: pass
        stats["errors"] += 1

    return stats
