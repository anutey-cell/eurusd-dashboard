"""
Strategist BUY outcome ledger + SELL shadow ledger.

BUY outcomes:
  - Populated by resolver after MT5 order closes (fill → close → P&L)
  - One row per canonical BUY opportunity that reached the broker
  - Table: strategist_buy_outcomes

SELL shadow:
  - Populated at verdict time (SELL execution is blocked)
  - One row per canonical SELL opportunity that would have executed
  - Table: strategist_sell_shadow
  - Resolver walks forward from entry using frozen SL/TP + conservative
    intrabar rules to compute hypothetical outcome.

Kept STRICTLY separate — BUY = actual demo, SELL = hypothetical only.
Fail-open on all helpers.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone, timedelta
from typing import Optional

from sqlalchemy import text
from sqlalchemy.orm import Session

log = logging.getLogger(__name__)


def _canonical_bucket(t: datetime, minutes: int = 15) -> str:
    """15-min bucket + direction + entry-price rounded to 1 XAU point.
    Provisional canonicalization (see sprint spec §17 for future refinement)."""
    b = t.replace(minute=(t.minute // minutes) * minutes, second=0, microsecond=0)
    return b.strftime("%Y-%m-%d %H:%M")


def record_sell_shadow(
    db: Session,
    *,
    verdict: dict,
    mt5_obj: dict,
) -> None:
    """Persist one hypothetical SELL opportunity that was blocked by
    STRATEGIST_SELL_SHADOW_ONLY guard."""
    try:
        now = datetime.now(timezone.utc)
        bucket = _canonical_bucket(now)
        entry = float(mt5_obj.get("entry", 0))
        opp_id = f"SHADOW-SELL·{bucket}·{round(entry,0):.0f}"
        # Idempotent — skip if same canonical opp already recorded within bucket
        existing = db.execute(text(
            "SELECT id FROM strategist_sell_shadow WHERE opportunity_id = :oi LIMIT 1"
        ), {"oi": opp_id}).fetchone()
        if existing: return

        diag = verdict.get("diagnostics") or {}
        db.execute(text("""
            INSERT INTO strategist_sell_shadow
              (created_at, opportunity_id, verdict_id, conditions_passed,
               setup_score, quality_band, market_state,
               hypothetical_entry, sl, tp1, tp2, rr,
               resolution_status)
            VALUES
              (:ca, :oi, :vi, :cp, :ss, :qb, :ms,
               :en, :sl, :t1, :t2, :rr,
               'PENDING')
        """), dict(
            ca=now.strftime("%Y-%m-%d %H:%M:%S"),
            oi=opp_id,
            vi=verdict.get("verdict_id"),
            cp=verdict.get("conditions_passed"),
            ss=verdict.get("setup_score"),
            qb=verdict.get("quality_band"),
            ms=verdict.get("market_state"),
            en=entry,
            sl=float(mt5_obj.get("stop_loss", 0)),
            t1=float(mt5_obj.get("take_profit_1", 0)),
            t2=float(mt5_obj.get("take_profit_2", 0)),
            rr=float(mt5_obj.get("risk_reward", 0)),
        ))
        db.commit()
        log.info("[strategist_shadow] SELL shadow recorded: %s", opp_id)
    except Exception as exc:
        log.debug("[strategist_shadow] sell shadow write failed: %s", exc)
        try: db.rollback()
        except Exception: pass


def record_buy_opportunity_at_enqueue(
    db: Session,
    *,
    verdict: dict,
    mt5_obj: dict,
    pending_execution_id: int,
    lot_size: float,
    reservation_id: Optional[str] = None,
) -> None:
    """Persist BUY opportunity at moment of enqueue (before broker fill).
    Outcome resolver fills in actual fill / exit / P&L later."""
    try:
        now = datetime.now(timezone.utc)
        bucket = _canonical_bucket(now)
        entry = float(mt5_obj.get("entry", 0))
        opp_id = f"BUY·{bucket}·{round(entry,0):.0f}"
        # Idempotent
        existing = db.execute(text(
            "SELECT id FROM strategist_buy_outcomes WHERE opportunity_id = :oi LIMIT 1"
        ), {"oi": opp_id}).fetchone()
        if existing:
            # Attach the additional pending_execution_id if not yet linked
            db.execute(text(
                "UPDATE strategist_buy_outcomes SET follow_on_execution_ids = "
                "COALESCE(follow_on_execution_ids,'') || :sep || :pid "
                "WHERE opportunity_id = :oi"
            ), {"oi": opp_id, "sep": ",", "pid": str(pending_execution_id)})
            db.commit()
            return

        db.execute(text("""
            INSERT INTO strategist_buy_outcomes
              (created_at, opportunity_id, verdict_id, pending_execution_id,
               reservation_id, conditions_passed, setup_score, quality_band,
               market_state, entry, sl, tp1, tp2, rr, lot_size,
               resolution_status)
            VALUES
              (:ca, :oi, :vi, :pi, :ri, :cp, :ss, :qb, :ms,
               :en, :sl, :t1, :t2, :rr, :ls, 'OPEN')
        """), dict(
            ca=now.strftime("%Y-%m-%d %H:%M:%S"),
            oi=opp_id,
            vi=verdict.get("verdict_id"),
            pi=pending_execution_id,
            ri=reservation_id,
            cp=verdict.get("conditions_passed"),
            ss=verdict.get("setup_score"),
            qb=verdict.get("quality_band"),
            ms=verdict.get("market_state"),
            en=entry,
            sl=float(mt5_obj.get("stop_loss", 0)),
            t1=float(mt5_obj.get("take_profit_1", 0)),
            t2=float(mt5_obj.get("take_profit_2", 0)),
            rr=float(mt5_obj.get("risk_reward", 0)),
            ls=lot_size,
        ))
        db.commit()
    except Exception as exc:
        log.debug("[strategist_shadow] buy opportunity write failed: %s", exc)
        try: db.rollback()
        except Exception: pass


def resolve_sell_shadows(db: Session, limit: int = 200) -> dict:
    """Walk forward from each PENDING SELL shadow using conservative intrabar
    rules against historical_candles M5. Populate hypothetical outcome."""
    stats = dict(scanned=0, resolved=0, no_data=0, still_open=0, errors=0)
    try:
        pending = db.execute(text("""
            SELECT id, created_at, opportunity_id, hypothetical_entry, sl, tp1, tp2
            FROM strategist_sell_shadow
            WHERE resolution_status='PENDING'
              AND created_at < datetime('now','-1 hour')
            ORDER BY id ASC LIMIT :n
        """), {"n": limit}).fetchall()

        for row in pending:
            stats["scanned"] += 1
            sid, created_at, opp_id, entry, sl, tp1, tp2 = row
            fire_ts = str(created_at).replace("T"," ").split(".")[0]
            # Forward M5 bars — mt5_backfill or live mt5 for full coverage
            bars = db.execute(text("""
                SELECT candle_time, high, low FROM historical_candles
                WHERE instrument='XAU/USD' AND timeframe='M5' AND candle_time > :ft
                ORDER BY candle_time ASC LIMIT 96
            """), {"ft": fire_ts}).fetchall()
            if not bars:
                stats["no_data"] += 1; continue
            outcome, exit_price, mfe, mae, holding_min = _walk_sell(bars, entry, sl, tp1, tp2, fire_ts)
            if outcome == "STILL_OPEN":
                stats["still_open"] += 1; continue
            pnl_pts = 0.0
            if outcome == "SL": pnl_pts = -(sl - entry)  # loss (positive number → adverse)
            elif outcome == "TP1": pnl_pts = (entry - tp1)
            elif outcome == "TP2": pnl_pts = (entry - tp2)
            db.execute(text("""
                UPDATE strategist_sell_shadow
                SET resolution_status = 'RESOLVED',
                    outcome = :oc, exit_price = :ex,
                    hypothetical_pnl_pts = :pn, mfe_pts = :mfe, mae_pts = :mae,
                    holding_min = :hm, resolved_at = :ra
                WHERE id = :sid
            """), dict(oc=outcome, ex=exit_price, pn=pnl_pts, mfe=mfe, mae=mae,
                       hm=holding_min, ra=datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
                       sid=sid))
            db.commit()
            stats["resolved"] += 1
    except Exception as exc:
        log.warning("[strategist_shadow] resolve_sell_shadows failed: %s", exc)
        stats["errors"] += 1
    return stats


def _walk_sell(bars, entry, sl, tp1, tp2, fire_ts):
    """Conservative: SL first on SL+TP same bar; TP1 first on TP1+TP2 same bar."""
    fire_dt = datetime.strptime(fire_ts, "%Y-%m-%d %H:%M:%S")
    hit_tp1 = False
    for r in bars:
        ct, h, l = r[0], float(r[1]), float(r[2])
        hit_sl = h >= sl
        hit_t1 = l <= tp1
        hit_t2 = l <= tp2
        if hit_sl and (hit_t1 or hit_t2) and not hit_tp1:
            return "SL", sl, sl - entry, 0.0, _mins(ct, fire_dt)
        if hit_sl and not hit_tp1:
            return "SL", sl, sl - entry, 0.0, _mins(ct, fire_dt)
        if hit_t2 and hit_t1 and not hit_tp1:
            return "TP1", tp1, sl - entry, entry - tp1, _mins(ct, fire_dt)
        if hit_t2:
            return "TP2", tp2, sl - entry, entry - tp2, _mins(ct, fire_dt)
        if hit_t1 and not hit_tp1:
            hit_tp1 = True
    if hit_tp1: return "TP1", tp1, sl - entry, entry - tp1, _mins(bars[-1][0], fire_dt)
    return "STILL_OPEN", None, 0.0, 0.0, _mins(bars[-1][0], fire_dt) if bars else 0.0


def _mins(ct, fire_dt) -> float:
    try:
        c = datetime.strptime(str(ct).replace("T"," ").split(".")[0], "%Y-%m-%d %H:%M:%S")
        return (c - fire_dt).total_seconds() / 60
    except Exception:
        return 0.0
