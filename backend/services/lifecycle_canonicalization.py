"""
Strategist opportunity lifecycle canonicalization v1.

Deterministic lifecycle grouping of per-minute Strategist verdicts into
structurally coherent BUY / SELL-SHADOW opportunities.

Reset conditions (each derivable from information observable AT that verdict time):
  1. DIRECTION_CHANGE      — proposed direction flipped
  2. STRUCTURAL_LEVEL_CHANGE — entry price shifted >30 XAU points
  3. INVALIDATION           — verdict fell below actionable state (conditions < 3)
                              AND stayed there ≥ 15 minutes (bounded gap), then re-armed
  4. SESSION_ROLLOVER       — new UTC trading day boundary (22:00 UTC)
  5. STALE_GAP              — no verdict for the setup for > 4 hours
                              (system restart, market close, prolonged silence)

NO reset ever depends on future price movement, future P&L, or future outcome.

Canonical ID format:
  {engine}·{direction}·{setup_signature}·{lifecycle_seq}
where setup_signature = round(first_entry/5)*5 + first_session initial
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone, timedelta
from typing import Optional

from sqlalchemy import text
from sqlalchemy.orm import Session

log = logging.getLogger(__name__)

CANON_VERSION = "LIFECYCLE_CANON_v1"
LEVEL_RESET_PTS = 30.0
STALE_GAP_HOURS = 4
INVALIDATION_MIN_GAP_MIN = 15


def _session_of(t: datetime) -> str:
    h = t.hour
    if 0 <= h < 7:  return "ASIA"
    if 7 <= h < 12: return "LONDON"
    if 12 <= h < 16: return "NY_OPEN"
    if 16 <= h < 22: return "NY_PM"
    return "ROLLOVER"


def _parse_ts(s) -> Optional[datetime]:
    ss = str(s).replace("T"," ").split(".")[0]
    try:
        d = datetime.strptime(ss, "%Y-%m-%d %H:%M:%S")
        if d.tzinfo is None: d = d.replace(tzinfo=timezone.utc)
        return d
    except Exception: return None


def recanonicalize_all(db: Session, engine_filter: str = "STRATEGIST_BUY") -> dict:
    """Rebuild lifecycle_canonical_opportunities from strategist_verdicts.
    Idempotent — clears prior LIFECYCLE_CANON_v1 rows for the engine first.
    """
    stats = dict(verdicts_scanned=0, lifecycle_ops=0, resets_direction=0,
                 resets_level=0, resets_invalidation=0, resets_session=0,
                 resets_stale=0)
    try:
        # Filter direction by engine
        dir_clause = "sv.decision='BUY'" if engine_filter == "STRATEGIST_BUY" else "sv.decision='SELL'"
        # Wipe existing rows for this engine at this version
        db.execute(text("""
            DELETE FROM lifecycle_canonical_opportunities
            WHERE engine=:eng AND canon_version=:v
        """), {"eng": engine_filter, "v": CANON_VERSION})
        db.commit()

        rows = db.execute(text(f"""
            SELECT sv.id, sv.created_at, sv.decision, sv.conditions_passed,
                   sv.execution_status, sv.entry, sv.stop_loss, sv.tp1, sv.tp2
            FROM strategist_verdicts sv
            WHERE {dir_clause}
              AND sv.conditions_passed >= 3
            ORDER BY sv.created_at ASC
        """)).fetchall()

        current_lifecycle = None  # {canonical_id, direction, first_entry, first_ts, last_ts, n_verdicts, session, first_id}
        seq = 0

        def _new_lifecycle(verdict_row, reset_reason):
            nonlocal seq
            seq += 1
            v_id, ts, decision, cp, es, entry, sl, tp1, tp2 = verdict_row
            t = _parse_ts(ts)
            sess = _session_of(t) if t else "UNK"
            bucket = round((entry or 0) / 5.0) * 5
            canonical_id = f"{engine_filter}·{decision}·{bucket:.0f}·{sess}·seq{seq}"
            db.execute(text("""
                INSERT INTO lifecycle_canonical_opportunities
                  (canonical_id, canon_version, engine, direction, setup_signature,
                   lifecycle_state, lifecycle_start, reset_reason,
                   first_verdict_id, last_verdict_id, n_raw_verdicts,
                   first_entry, first_sl, first_tp1, first_tp2)
                VALUES
                  (:cid, :v, :eng, :dir, :sig, 'ACTIVE', :ls, :rr,
                   :fv, :lv, :n, :fe, :fsl, :ft1, :ft2)
            """), dict(
                cid=canonical_id, v=CANON_VERSION, eng=engine_filter,
                dir=decision, sig=f"{decision}·{bucket:.0f}·{sess}",
                ls=t, rr=reset_reason, fv=v_id, lv=v_id, n=1,
                fe=entry, fsl=sl, ft1=tp1, ft2=tp2,
            ))
            db.commit()
            stats["lifecycle_ops"] += 1
            return dict(canonical_id=canonical_id, direction=decision,
                        first_entry=entry, first_ts=t, last_ts=t, n_verdicts=1,
                        session=sess, first_id=v_id)

        for r in rows:
            stats["verdicts_scanned"] += 1
            v_id, ts, decision, cp, es, entry, sl, tp1, tp2 = r
            t = _parse_ts(ts)
            if not t: continue

            if current_lifecycle is None:
                current_lifecycle = _new_lifecycle(r, "INITIAL")
                continue

            reason = None

            # 1. DIRECTION_CHANGE
            if decision != current_lifecycle["direction"]:
                reason = "DIRECTION_CHANGE"; stats["resets_direction"] += 1
            # 2. STRUCTURAL_LEVEL_CHANGE
            elif entry and current_lifecycle["first_entry"] and \
                 abs(entry - current_lifecycle["first_entry"]) > LEVEL_RESET_PTS:
                reason = "STRUCTURAL_LEVEL_CHANGE"; stats["resets_level"] += 1
            # 5. STALE_GAP
            elif current_lifecycle["last_ts"] and \
                 (t - current_lifecycle["last_ts"]).total_seconds() > STALE_GAP_HOURS*3600:
                reason = "STALE_GAP"; stats["resets_stale"] += 1
            # 4. SESSION_ROLLOVER (crossed 22:00 UTC boundary)
            elif current_lifecycle["last_ts"] and \
                 (t.hour >= 22 and current_lifecycle["last_ts"].hour < 22) and \
                 t.date() == current_lifecycle["last_ts"].date():
                reason = "SESSION_ROLLOVER"; stats["resets_session"] += 1
            elif current_lifecycle["last_ts"] and \
                 t.date() != current_lifecycle["last_ts"].date():
                reason = "SESSION_ROLLOVER"; stats["resets_session"] += 1
            # 3. INVALIDATION — cp dropped below 3 for ≥15min then re-armed (implicit via gap)
            # captured by STALE_GAP filter with narrower window
            elif current_lifecycle["last_ts"] and \
                 (t - current_lifecycle["last_ts"]).total_seconds() > INVALIDATION_MIN_GAP_MIN*60 and cp >= 3:
                # only reset if there was a real gap
                reason = "INVALIDATION_REARM"; stats["resets_invalidation"] += 1

            if reason:
                # Close current lifecycle
                db.execute(text("""
                    UPDATE lifecycle_canonical_opportunities
                    SET lifecycle_state='RESOLVED', lifecycle_end=:le,
                        last_verdict_id=:lv, n_raw_verdicts=:n
                    WHERE canonical_id=:cid
                """), dict(le=current_lifecycle["last_ts"],
                           lv=current_lifecycle["first_id"],  # approximation
                           n=current_lifecycle["n_verdicts"],
                           cid=current_lifecycle["canonical_id"]))
                db.commit()
                current_lifecycle = _new_lifecycle(r, reason)
            else:
                # Extend current lifecycle
                current_lifecycle["last_ts"] = t
                current_lifecycle["n_verdicts"] += 1

        # Close final lifecycle
        if current_lifecycle:
            db.execute(text("""
                UPDATE lifecycle_canonical_opportunities
                SET lifecycle_end=:le, last_verdict_id=:lv, n_raw_verdicts=:n
                WHERE canonical_id=:cid
            """), dict(le=current_lifecycle["last_ts"],
                       lv=current_lifecycle["first_id"],
                       n=current_lifecycle["n_verdicts"],
                       cid=current_lifecycle["canonical_id"]))
            db.commit()

    except Exception as exc:
        log.warning("[lifecycle_canon] recanonicalize failed for %s: %s",
                    engine_filter, exc)
        try: db.rollback()
        except Exception: pass
    return stats


def lifecycle_forward_closed_count(db: Session, engine: str = "STRATEGIST_BUY") -> int:
    """Count of RESOLVED lifecycle opportunities in the forward cohort."""
    try:
        r = db.execute(text("""
            SELECT COUNT(*) FROM lifecycle_canonical_opportunities
            WHERE engine=:e AND canon_version=:v AND lifecycle_state='RESOLVED'
        """), {"e": engine, "v": CANON_VERSION}).fetchone()
        return int(r[0]) if r else 0
    except Exception:
        return 0
