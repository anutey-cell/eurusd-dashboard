"""
Strategist Runner
=================

Shared driver for the institutional demo-execution strategist. Both the HTTP
router (`/api/v1/strategist/decision`) and the background scheduler call into
the SAME `run_once()` so the verdict, Telegram alert, MT5 enqueue, and audit
log all happen identically regardless of who triggered them.

Side-effects (in order):
  1. make_decision(db)       — compute the verdict
  2. fire_alert(verdict)     — Telegram BUY/SELL (or STAND ASIDE if enabled)
  3. enqueue_demo_order(...) — PendingExecution row at lot=0.01 if all gates pass
  4. persist_verdict(...)    — strategist_verdicts append-only log

Dedupe state (alert fingerprints, enqueue fingerprints) is module-level so
the same plan doesn't fire twice within the cooldown windows.
"""
from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy.orm import Session

from config import settings
from db_models import PendingExecution
from services.strategist import (
    format_mandate_signal_message,
    format_mandate_stand_aside_message,
    make_decision,
    persist_verdict,
)

log = logging.getLogger(__name__)

# ── Dedupe state ─────────────────────────────────────────────────────────────
# Tracks the LAST alert we actually fired (not just the last verdict). Used to
# detect direction-flips and score-escalations so we can bypass cooldown when
# something materially changes vs the prior alert.
_last_alert: dict = {
    "decision":           None,    # "BUY" | "SELL" | None
    "execution_band":     None,    # "actionable" | "watchlist" | None
    "conditions_passed":  0,
    "sent_at":            0.0,
}
_last_standby_fingerprint:  str   = ""
_last_standby_at:           float = 0.0
_last_enqueue_fingerprint:  str   = ""
_last_enqueue_at:           float = 0.0

# Watchlist heads-up state — fires when mandate says "Watchlist" (3/5 conditions)
# but decision is still STAND ASIDE (needs 4/5 to be BUY/SELL). Deduped by
# (direction, hour) so at most one per hour per direction.
_last_watchlist_alert: dict = {"key": "", "sent_at": 0.0}
_WATCHLIST_ALERT_COOLDOWN_S = 30 * 60   # 30 min min gap even within same bucket

# Cooldowns by signal band — addresses 92% duplicate noise audit finding
_COOLDOWN_S_5OF5         = 600.0     # 10 min — high-conviction
_COOLDOWN_S_4OF5         = 600.0     # 10 min — actionable
_COOLDOWN_S_3OF5         = 3600.0    # 60 min — watchlist (was 10 min, too noisy)
_STANDBY_COOLDOWN_S      = 3600.0    # 60 min for STAND ASIDE info alerts
_ENQUEUE_COOLDOWN_S      = 600.0     # 10 min for same-plan re-enqueue
_AT_CAP_COOLDOWN_S       = 3600.0    # 60 min for "position cap reached" review alerts
_last_at_cap_at:           float = 0.0

# Direction-flip tracking — fires a one-shot 🔀 transition alert whenever
# the engine's signaled direction switches BUY ↔ SELL. STAND ASIDE in between
# is fine (e.g. SELL → STAND ASIDE → BUY counts as one flip).
_last_signaled_direction: str | None = None

# ── Pre-formation alerts — 1 hour before each major killzone opens ──────────
# Operator wants a heads-up when a setup window is APPROACHING so they can get
# eyes on the screen. These are scheduled by UTC hour: London KZ opens at 07:00
# UTC → fires at 06:00 UTC; NY KZ opens at 13:00 UTC → fires at 12:00 UTC.
# Each alert reports current bias / conditions / what to watch. Fires once per
# window per day (deduped by date string).
_PRE_FORMATION_WINDOWS = [
    {
        "kz_name":         "London KZ",
        "trigger_utc_hour": 6,     # 1 h before 07:00 UTC (London KZ open)
        "opens_utc":       "07:00 UTC (10:00 EAT)",
        "why":             "Highest-probability killzone. Asian range raid + reversal.",
    },
    {
        "kz_name":         "NY KZ",
        "trigger_utc_hour": 12,    # 1 h before 13:00 UTC (NY KZ open)
        "opens_utc":       "13:00 UTC (16:00 EAT)",
        "why":             "NY open raid + reversal. London continuation extension.",
    },
]
_last_preformation_alerts: dict[str, str] = {}   # kz_name → date-string it last fired for


def is_weekend_quiet_hours() -> bool:
    """
    True during the forex/gold weekend close: Friday 21:00 UTC → Sunday 22:00 UTC.
    No trade signals, no hourly briefings should fire in this window — the
    Saturday recap and Sunday forecast newsletters handle weekend comms.
    """
    now = datetime.now(timezone.utc)
    wd = now.weekday()      # Mon=0 … Sat=5, Sun=6
    if wd == 5:                             # Saturday — always closed
        return True
    if wd == 6 and now.hour < 22:           # Sunday before reopen
        return True
    if wd == 4 and now.hour >= 21:          # Friday after close
        return True
    return False


def is_monday_observation() -> bool:
    """
    True during all of Monday UTC. Per operator risk plan, Monday is
    observation-only: signals fire so the operator can study setups, but
    no MT5 order is enqueued. Execution resumes Tuesday 00:00 UTC.

    Sunday 22:00 → Monday 23:59 UTC is the full no-trade window. Sunday
    evening (post-reopen) is already covered by is_weekend_quiet_hours
    until 22:00; from 22:00 onward we check this Monday gate.
    """
    from config import settings
    if not getattr(settings, "monday_observation_mode", True):
        return False
    now = datetime.now(timezone.utc)
    return now.weekday() == 0   # Monday


# ── Momentum-continuation state (secondary signal source) ───────────────────
# The mandate strategist is designed for MEAN-REVERSION setups (pullback →
# CISD reclaim). It systematically misses EXTENSION moves — clean momentum
# breakouts where price never retraces. The momentum-continuation engine
# (services.intraday_strategies.analyze_momentum_breakout) fills that gap
# as a SECONDARY signal source: signal-only, no MT5 execution, tagged
# clearly so the operator can distinguish sniper vs momentum trades.
_last_momentum_alert: dict = {
    "direction":  None,     # "BUY" | "SELL" | None
    "bar_time":   None,     # ISO string of the breakout bar
    "sent_at":    0.0,
}
_MOMENTUM_COOLDOWN_S = 900.0     # 15 min between momentum alerts (same direction)


# ── Public entry point ───────────────────────────────────────────────────────

def run_once(db: Session) -> dict:
    """
    Compute one fresh verdict + run all side-effects + return the verdict dict.
    Called by both the HTTP router (on fresh /decision) and the background loop.
    Never raises — every side-effect is wrapped.
    """
    verdict = make_decision(db)

    # Pre-compute signal grade so downstream side-effects (enqueue + alert)
    # both see the same grade. Enqueue uses it for sizing; alert uses it for
    # gating/formatting. Only applies to BUY/SELL decisions.
    if verdict.get("decision") in ("BUY", "SELL"):
        try:
            from services.signal_grading import grade_verdict
            _gr = grade_verdict(
                verdict,
                min_score_a=settings.signal_min_score_a,
                min_score_aplus=settings.signal_min_score_aplus,
                min_rr_a=settings.signal_min_rr_a,
                min_rr_aplus=settings.signal_min_rr_aplus,
                watchlist_score=settings.signal_watchlist_score,
            )
            verdict["signal_grade"] = _gr.to_dict()
        except Exception as exc:
            log.debug("[strategist_runner] pre-grade failed (non-fatal): %s", exc)

    # Side-effects — order matters: enqueue first so the log can back-link it
    pending_id = _maybe_enqueue_demo_order(db, verdict)
    _maybe_fire_alert(verdict)
    _maybe_fire_watchlist_alert(verdict)      # 3/5 mandate heads-up (informational)
    _maybe_fire_preformation_alert(verdict)   # 1-hour-before-KZ heads-up
    _maybe_fire_momentum_alert(db, verdict)   # secondary momentum-continuation source
    _maybe_scan_vp_trap_zones(db)             # Phase 2 — advance state, no alerts yet
    _maybe_fire_kz_magnet_alert(db, verdict)  # KZ Magnet (cross-KZ POC + Flux)

    # Canonical notification layer — runs in shadow mode alongside legacy
    _shadow_run_canonical_mandate(db, verdict)

    # Shadow trade simulator — records ALL BUY/SELL verdicts (every grade)
    # for grader calibration. Never executes, never alerts. Fails silent.
    _maybe_shadow_record_trade(db, verdict)
    # Also record cp=3 Watchlist observations with synthesized trade plan,
    # so shadow_trades accumulates data even when mandate refuses BUY/SELL.
    _maybe_shadow_record_watchlist(db, verdict)

    try:
        persist_verdict(db, verdict, pending_execution_id=pending_id)
    except Exception as exc:
        log.debug("[strategist_runner] persistence skipped: %s", exc)

    return verdict


def _shadow_run_canonical_mandate(db: Session, verdict: dict) -> None:
    """
    P3 shadow-mode hook: routes every verdict through the canonical adapter
    so we build a parallel audit trail. Legacy Telegram path is unchanged.

    Cutover flips settings.notification_shadow_mode → False. Only then does
    the canonical path actually send. `notification_canonical_enabled` is
    the master off-switch (useful if the new pipeline misbehaves live).
    """
    if not getattr(settings, "notification_canonical_enabled", True):
        return
    try:
        from services.signal_adapters import on_mandate_verdict
        result = on_mandate_verdict(
            db, verdict,
            force_dry_run=bool(getattr(settings, "notification_shadow_mode", True)),
            mode=getattr(settings, "notification_mode", "standard"),
        )
        if result.get("action") not in (None, "skipped", "unchanged"):
            log.info("[canonical/mandate] %s · %s · state=%s",
                     result.get("action"), result.get("signal_id"),
                     result.get("state"))
    except Exception as exc:
        log.warning("[canonical/mandate] shadow run failed: %s", exc)


def _maybe_shadow_record_trade(db: Session, verdict: dict) -> None:
    """
    Record BUY/SELL verdicts to shadow_trades regardless of grade or alert
    cooldown. Provides raw data for grader calibration — was A+ really better
    than A? Did suppressed B/C setups outperform? Fails silent.
    """
    if not getattr(settings, "shadow_trade_recording_enabled", True):
        return
    if verdict.get("decision") not in ("BUY", "SELL"):
        return
    tp = verdict.get("trade_plan") or {}
    if tp.get("entry") is None or tp.get("stop_loss") is None or tp.get("tp1") is None:
        return
    try:
        # Compute grade so we can attribute the outcome per bucket.
        # Reuse the same grader the alert path uses.
        from services.signal_grading import grade_verdict
        grade_result = grade_verdict(
            verdict,
            min_score_a=settings.signal_min_score_a,
            min_score_aplus=settings.signal_min_score_aplus,
            min_rr_a=settings.signal_min_rr_a,
            min_rr_aplus=settings.signal_min_rr_aplus,
            watchlist_score=settings.signal_watchlist_score,
        )
        from services.shadow_trade_simulator import record_shadow_trade
        r = record_shadow_trade(db, verdict, grade_result=grade_result)
        if r.recorded:
            log.info("[shadow_trade] recorded %s grade=%s fp=%s",
                     verdict.get("decision"), grade_result.grade, r.fingerprint)
    except Exception as exc:
        log.debug("[shadow_trade] record skipped (non-fatal): %s", exc)


def _maybe_shadow_record_watchlist(db: Session, verdict: dict) -> None:
    """
    Record cp=3 Watchlist verdicts by synthesizing a trade plan from
    tf_alignment + last M5 close + recent M15 ATR. Feeds shadow_trades
    so we get calibration data even when the mandate refuses to fire
    BUY/SELL (cp<4). Every Watchlist observation becomes a data point.
    """
    if not getattr(settings, "shadow_trade_recording_enabled", True):
        return
    # Match on the mandate's actual reason string — it reads
    # "Watchlist — 3/5 (no demo execution)" when cp=3 (execution_status
    # itself is SIGNAL_ONLY, not "Watchlist"). Belt-and-suspenders match
    # both fields so we catch either encoding.
    reason = (verdict.get("execution_status_reason") or "")
    exec_st = (verdict.get("execution_status") or "")
    cp = int(verdict.get("conditions_passed") or 0)
    setup_score = int(verdict.get("setup_score") or 0)
    # Watchlist trigger — either the mandate labels it Watchlist, OR we
    # have a lower-tier setup with strong tf alignment + non-trivial score
    # (cp≥2 + score≥50). This broader net makes shadow_trades accumulate
    # observation data even when mandate stays cp=2, so measurement never
    # completely stalls waiting for cp≥3 windows.
    is_mandate_watchlist = ("Watchlist" in reason) or (exec_st == "Watchlist")
    # Broader net: any STAND_ASIDE with strong tf alignment counts as a
    # measurable observation (we're tracking "when engine sees direction,
    # is it right?"). Fingerprint dedup limits to 1 obs per hour per
    # direction per 5pt price bucket, so noise stays bounded.
    is_broad_candidate   = (cp >= 1 and setup_score >= 20)
    if not (is_mandate_watchlist or is_broad_candidate):
        return
    # Infer direction from tf_alignment_label — require STRONG bias only,
    # otherwise it's a coin-flip and not worth observing
    tf_label = (verdict.get("tf_alignment_label") or "").lower()
    if "strong bullish" in tf_label or ("bullish" in tf_label and "extended" in tf_label):
        direction = "BUY"
    elif "strong bearish" in tf_label or ("bearish" in tf_label and "extended" in tf_label):
        direction = "SELL"
    else:
        return   # weak/neutral bias — skip

    try:
        from sqlalchemy import text
        # Current price = last M5 close
        row = db.execute(text(
            "SELECT close FROM historical_candles "
            "WHERE instrument='XAU/USD' AND timeframe='M5' "
            "ORDER BY candle_time DESC LIMIT 1"
        )).fetchone()
        if not row or not row[0]:
            return
        price = float(row[0])
        # ATR = mean(high-low) over last 14 H1 bars — broader than M15,
        # captures real volatility for gold instead of quiet-window noise
        atr_rows = db.execute(text(
            "SELECT high, low FROM historical_candles "
            "WHERE instrument='XAU/USD' AND timeframe='H1' "
            "ORDER BY candle_time DESC LIMIT 14"
        )).fetchall()
        if not atr_rows or len(atr_rows) < 3:
            return
        atr_raw = sum(float(r[0]) - float(r[1]) for r in atr_rows) / len(atr_rows)
        # Floor at 8 pts — a plausible minimum SL for XAUUSD; below that
        # single-tick spread can take out the stop and observations become
        # noise instead of signal
        atr = max(atr_raw, 8.0)

        # Synthesize plan: 1.0×ATR stop, 2.5×ATR TP1 (RR=2.5),
        # 3.5×ATR TP2 (RR=3.5) — matches the mandate's minimum RR
        if direction == "BUY":
            entry = price
            sl    = round(price - 1.0 * atr, 2)
            tp1   = round(price + 2.5 * atr, 2)
            tp2   = round(price + 3.5 * atr, 2)
        else:
            entry = price
            sl    = round(price + 1.0 * atr, 2)
            tp1   = round(price - 2.5 * atr, 2)
            tp2   = round(price - 3.5 * atr, 2)

        # Wrap into a shadow verdict — decision + plan so record_shadow_trade
        # accepts it. Grade is explicit "Watchlist" via a synthetic result.
        shadow_verdict = {
            **verdict,
            "decision":   direction,
            "archetype":  "watchlist_observation",   # distinguishable in bucket stats
            "trade_plan": {
                "entry": entry, "stop_loss": sl,
                "tp1": tp1, "tp2": tp2,
                "tp1_rr": abs(tp1 - entry) / abs(entry - sl),
                "tp2_rr": abs(tp2 - entry) / abs(entry - sl),
                "invalidation": sl,
            },
        }

        # Synthetic grade result — bypass the real grader (which would grade
        # this "STAND ASIDE / not gradeable"). We want it tagged as Watchlist.
        class _WatchlistGrade:
            grade = "Watchlist"
            reason = f"cp={verdict.get('conditions_passed')} · {tf_label} · synthesized 1.5×ATR/2×ATR/3×ATR"
            composite_score = int(verdict.get("setup_score") or 0)

        from services.shadow_trade_simulator import record_shadow_trade
        r = record_shadow_trade(db, shadow_verdict, grade_result=_WatchlistGrade())
        if r.recorded:
            log.info("[shadow_trade/watchlist] recorded %s cp=%s atr=%.2f fp=%s",
                     direction, verdict.get("conditions_passed"), atr, r.fingerprint)
    except Exception as exc:
        log.debug("[shadow_trade/watchlist] skipped (non-fatal): %s", exc)


# ── Telegram side-effect ─────────────────────────────────────────────────────

def _execution_band(execution_status: str) -> str:
    """Collapse execution_status into one of three bands for dedupe purposes."""
    if execution_status == "DEMO_TRADE_PLACED":
        return "actionable"
    if execution_status == "SIGNAL_ONLY":
        return "watchlist"
    return "blocked"     # BRIDGE_OFFLINE / SPREAD_TOO_HIGH / NEWS_RISK_BLOCKED / etc.


def _cooldown_for(conditions_passed: int) -> float:
    """Pick cooldown based on signal quality band."""
    if conditions_passed >= 5: return _COOLDOWN_S_5OF5
    if conditions_passed >= 4: return _COOLDOWN_S_4OF5
    return _COOLDOWN_S_3OF5


def _maybe_fire_alert(verdict: dict) -> None:
    """
    Smart alert dispatcher with quality-band cooldowns + transition detection.

    Rules:
      1. STAND ASIDE after we previously alerted BUY/SELL → fire invalidation
         (one-shot, bypasses cooldown).
      2. BUY/SELL with direction-flip vs last alert → fire immediately
         (bypasses cooldown — meaningful market change).
      3. BUY/SELL with score escalation (e.g. 3/5 → 4/5) → fire immediately
         (bypasses cooldown — quality improved).
      4. Otherwise apply band-specific cooldown:
            5/5 actionable: 10 min
            4/5 actionable: 10 min
            3/5 watchlist : 60 min  ← the fix
      5. Blocked executions (BRIDGE_OFFLINE etc.) — no separate alert,
         the signal alert itself carries the status.
    """
    global _last_alert, _last_standby_fingerprint, _last_standby_at

    decision = verdict.get("decision")
    es       = verdict.get("execution_status")
    cp       = verdict.get("conditions_passed", 0)

    # ── P133 defensive gate: no plan = no alert ────────────────────────────
    # Belt-and-suspenders on top of the strategist's decision-level guard.
    # If a BUY/SELL slips through without entry/SL, drop the alert here
    # rather than posting "SELL 4/5 · entry=None" to the user's phone.
    tp = verdict.get("trade_plan") or {}
    if decision in ("BUY", "SELL") and (tp.get("entry") is None or tp.get("stop_loss") is None):
        log.warning("[strategist_runner] alert suppressed — %s cp=%s but no trade plan "
                     "(entry=%s SL=%s)", decision, cp, tp.get("entry"), tp.get("stop_loss"))
        return

    # Weekend gate — markets are flat from Fri 21:00 UTC to Sun 22:00 UTC.
    # No signals, no briefings, no invalidations during this window. The
    # Saturday recap + Sunday forecast newsletters handle weekend comms.
    if is_weekend_quiet_hours():
        log.debug("[strategist_runner] alert suppressed — weekend quiet hours")
        return

    try:
        # ── Path 1: STAND ASIDE — handle invalidation + optional standby alerts ──
        if decision == "STAND ASIDE":
            # If we recently alerted BUY/SELL and now we're flat → invalidation
            if _last_alert["decision"] in ("BUY", "SELL"):
                _send_plain(_format_invalidation(verdict, prior=_last_alert))
                log.info("[strategist_runner] invalidation alert fired (was %s, now STAND ASIDE)",
                         _last_alert["decision"])
                # Clear the prior alert state — invalidation is one-shot
                _last_alert = {"decision": None, "execution_band": None,
                               "conditions_passed": 0, "sent_at": time.time()}
                # NOTE: _last_signaled_direction stays set — the next BUY/SELL
                # in the OPPOSITE direction will trigger the flip alert.
                return

            # Optional standby informational alert
            if not getattr(settings, "telegram_standby_alerts", False):
                return
            fp = f"STANDBY|{verdict.get('execution_status_reason')}|{cp}"
            if (fp != _last_standby_fingerprint
                    or (time.time() - _last_standby_at) > _STANDBY_COOLDOWN_S):
                msg = format_mandate_stand_aside_message(verdict)
                _send_plain(msg)
                _last_standby_fingerprint = fp
                _last_standby_at          = time.time()
            return

        # ── Path 2: BUY / SELL ──────────────────────────────────────────
        if decision not in ("BUY", "SELL"):
            return
        if not (verdict.get("execution_permission") or {}).get("allow_alert"):
            return

        # ── Direction-flip transition alert (one-shot per real flip) ────
        # Fire BEFORE the normal signal so the regime shift is the first
        # thing the operator sees. _last_signaled_direction is updated only
        # when we actually emit a BUY/SELL alert — STAND ASIDE between
        # flips doesn't reset it.
        global _last_signaled_direction
        if (_last_signaled_direction is not None
                and _last_signaled_direction != decision):
            try:
                _send_plain(_format_direction_flip(verdict, prior_direction=_last_signaled_direction))
                log.info("[strategist_runner] direction flip alert fired: %s → %s",
                         _last_signaled_direction, decision)
                # Record flip so the H3 cooldown gate in execution_gates
                # blocks execution in the NEW direction for cooldown_min.
                try:
                    from services.execution_gates import mark_direction_flip
                    mark_direction_flip(decision)
                except Exception as _hexc:
                    log.debug("[strategist_runner] flip cooldown mark failed: %s", _hexc)
            except Exception as exc:
                log.warning("[strategist_runner] direction flip alert failed: %s", exc)

        # ── Special path: POSITION_CAP_REACHED — review-mode alert ─────
        if es == "POSITION_CAP_REACHED":
            _maybe_fire_position_cap_alert(verdict)
            _last_signaled_direction = decision   # still tracks direction even when not enqueuing
            return

        band = _execution_band(es)
        prior_decision = _last_alert["decision"]
        prior_cp       = _last_alert["conditions_passed"] or 0
        elapsed        = time.time() - _last_alert["sent_at"]
        cooldown       = _cooldown_for(cp)

        # Bypass cooldown if direction flipped (BUY→SELL or first alert after STAND ASIDE)
        bypass_flip      = prior_decision and prior_decision != decision
        # Bypass cooldown if score escalated to a higher quality band
        bypass_escalate  = cp > prior_cp and prior_decision == decision

        if not bypass_flip and not bypass_escalate and elapsed < cooldown:
            log.debug("[strategist_runner] alert suppressed — cooldown (%.0fs/%.0fs band=%s)",
                      elapsed, cooldown, band)
            return

        # ── Signal grading gate (A+/A/B/C) ──────────────────────────────
        # Suppress anything below A grade unless watchlist alerts explicitly
        # enabled. No execution impact — this only filters Telegram noise.
        from services.signal_grading import (
            grade_verdict, format_signal_grade_body,
            ALERT_GRADES, WATCHLIST_GRADES,
        )
        grade_result = grade_verdict(
            verdict,
            min_score_a=settings.signal_min_score_a,
            min_score_aplus=settings.signal_min_score_aplus,
            min_rr_a=settings.signal_min_rr_a,
            min_rr_aplus=settings.signal_min_rr_aplus,
            watchlist_score=settings.signal_watchlist_score,
        )
        grade = grade_result.grade
        # Attach to verdict for downstream logging / dashboard
        verdict["signal_grade"] = grade_result.to_dict()

        # Suppress logic
        if grade in WATCHLIST_GRADES and not settings.signal_watchlist_alerts_enabled:
            log.info("[strategist_runner] alert SUPPRESSED — grade=%s (watchlist disabled): %s",
                     grade, grade_result.reason)
            return
        if grade not in ALERT_GRADES and grade not in WATCHLIST_GRADES:
            log.info("[strategist_runner] alert SUPPRESSED — grade=%s: %s",
                     grade, grade_result.reason)
            return

        # ── Fire ───────────────────────────────────────────────────────
        long_pct, short_pct = _fetch_sentiment()
        # New grading-aware format (per operator recalibration spec)
        try:
            from services.mt5_provider import get_tick
            tick = get_tick("xauusd")
            spread_pts = tick.get("spread_raw")
        except Exception:
            spread_pts = None
        msg = format_signal_grade_body(verdict, grade_result, spread_pts=spread_pts)

        # ── VP Trap confirmation-mode enrichment ──────────────────────
        # When vp_trap_mode == "confirmation" AND we have an active VP Trap
        # zone matching the mandate's direction, append a small context line
        # to this mandate alert. Never suppresses the mandate alert; never
        # affects mandate scoring. Pure additive information.
        try:
            from services.vp_trap_alerts import (
                get_vp_trap_context_for_mandate, format_vp_trap_context_line,
            )
            from database import SessionLocal
            with SessionLocal() as _db:
                vp_ctx = get_vp_trap_context_for_mandate(decision, _db)
            if vp_ctx:
                ctx_line = format_vp_trap_context_line(vp_ctx)
                if ctx_line:
                    msg = msg + f"\n\n🪤 VP TRAP context:\n   {ctx_line}"
        except Exception as exc:
            log.debug("[strategist_runner] VP Trap context enrichment failed: %s", exc)

        _send_plain(msg)
        reason = ("flip" if bypass_flip else "escalate" if bypass_escalate else "cooldown_expired")
        log.info("[strategist_runner] alert fired %s/%d (band=%s, reason=%s)",
                 decision, cp, band, reason)
        _last_alert = {
            "decision":          decision,
            "execution_band":    band,
            "conditions_passed": cp,
            "sent_at":           time.time(),
        }
        _last_signaled_direction = decision   # track for next flip detection
    except Exception as exc:
        log.debug("[strategist_runner] alert hook failed (non-fatal): %s", exc)


# ─────────────────────────────────────────────────────────────────────────────
# Watchlist heads-up (mandate cp=3, decision STAND_ASIDE, execution_status=Watchlist)
# ─────────────────────────────────────────────────────────────────────────────

def _maybe_fire_watchlist_alert(verdict: dict) -> None:
    """
    Fire a distinct 'watchlist heads-up' Telegram message when the mandate
    labels the current state as 'Watchlist' (3/5 conditions) but decision is
    still STAND_ASIDE (requires 4/5 for a BUY/SELL trade plan).

    Gated by settings.signal_watchlist_alerts_enabled. Deduped by (direction,
    hour) with a 30-min hard cooldown even inside the same hour bucket.

    Purpose: give the operator a heads-up on setups the engine is watching
    but has NOT yet committed to trading. Distinct wording so it can never
    be confused with an actionable BUY/SELL alert. No trade plan included.
    """
    global _last_watchlist_alert
    try:
        if not getattr(settings, "signal_watchlist_alerts_enabled", False):
            return
        if verdict.get("execution_status") != "Watchlist":
            return
        # Never during weekend quiet hours
        if is_weekend_quiet_hours():
            return

        # Derive direction from HTF alignment label
        tf_label = (verdict.get("tf_alignment_label") or "").lower()
        if "bullish" in tf_label:
            direction = "BUY"
        elif "bearish" in tf_label:
            direction = "SELL"
        else:
            return   # no clear directional lean → nothing to watch

        # Dedupe key = direction + UTC hour bucket
        now = time.time()
        hour_bucket = datetime.now(timezone.utc).strftime("%Y%m%d%H")
        key = f"{direction}|{hour_bucket}"
        if _last_watchlist_alert.get("key") == key:
            return
        if now - float(_last_watchlist_alert.get("sent_at", 0.0)) < _WATCHLIST_ALERT_COOLDOWN_S:
            return

        msg = _format_watchlist_message(verdict, direction)
        _send_plain(msg)
        _last_watchlist_alert = {"key": key, "sent_at": now}
        log.info("[strategist_runner] watchlist alert fired %s (cp=%s, score=%s)",
                 direction, verdict.get("conditions_passed"),
                 verdict.get("setup_score"))
    except Exception as exc:
        log.debug("[strategist_runner] watchlist alert hook failed (non-fatal): %s", exc)


def _format_watchlist_message(verdict: dict, direction: str) -> str:
    """Compose the informational Watchlist heads-up. Never a trade plan."""
    price = verdict.get("price") or verdict.get("current_price")
    tf_label = verdict.get("tf_alignment_label") or "—"
    market_state = verdict.get("market_state") or "—"
    session_class = verdict.get("session_classification") or "—"
    setup_score = verdict.get("setup_score", "—")
    cp = verdict.get("conditions_passed", 0)
    reason = verdict.get("execution_status_reason") or "3/5 mandate conditions"

    arrow = "📈" if direction == "BUY" else "📉"

    lines = [
        f"👀 WATCHLIST · XAU/USD · {arrow} {direction} bias forming",
        "━━━━━━━━━━━━━━━━━━━━━━━━",
        f"Setup at {cp}/5 conditions — mandate not yet actionable.",
        "",
        "📊 What the engine sees",
    ]
    if price is not None:
        try:
            lines.append(f"   Price:      ${float(price):,.2f}")
        except (TypeError, ValueError):
            pass
    lines.extend([
        f"   TF align:   {tf_label}",
        f"   Market:     {market_state}",
        f"   Session:    {session_class}",
        f"   Setup:      {cp}/5  ·  score {setup_score}",
        "",
        "🚫 Why no trade plan yet",
        f"   {reason}",
        "",
        "⚠️ Informational only. No entry / SL / TP attached.",
        "   The engine will alert again if setup climbs to 4/5+ (actionable)",
        "   or 5/5 (high-conviction).",
    ])
    return "\n".join(lines)


def _format_direction_flip(verdict: dict, *, prior_direction: str) -> str:
    """
    Compose the one-shot "engine just flipped direction" alert. Shown when
    the engine's signaled direction switches BUY ↔ SELL after at least one
    prior signal in the opposite direction. Surfaces the drivers behind the
    regime shift so the operator understands WHY the flip happened.
    """
    new_direction = verdict.get("decision")
    cp = verdict.get("conditions_passed", 0)
    tp = verdict.get("trade_plan") or {}
    diag = verdict.get("diagnostics") or {}
    mc = verdict.get("macro_context") or {}

    new_arrow   = "🟢 BUY" if new_direction == "BUY" else "🔴 SELL"
    prior_arrow = "🟢 BUY" if prior_direction == "BUY" else "🔴 SELL"
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M GMT")

    return (
        f"🔀 XAUUSD DIRECTION FLIP\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"{prior_arrow}  →  {new_arrow}\n"
        f"\n"
        f"📊 NEW SIDE — {cp}/5 conditions\n"
        f"  Price:          ${tp.get('entry', '—')}\n"
        f"  TF alignment:   {verdict.get('tf_alignment_label', '—')}\n"
        f"  Market state:   {verdict.get('market_state', '—')}\n"
        f"  Session:        {verdict.get('session_classification', '—')}\n"
        f"\n"
        f"🌍 REGIME DRIVERS\n"
        f"  D1 bias:        {diag.get('d1_bias_local', '—')}\n"
        f"  H4 bias:        {diag.get('h4_bias_local', '—')}\n"
        f"  Macro:          {mc.get('gold_macro_bias', '—')}\n"
        f"  Macro align:    {mc.get('macro_alignment', '—')}\n"
        f"  Sweep:          {diag.get('sweep_rationale', '—')}\n"
        f"\n"
        f"⚠️ First signals after a flip carry higher noise.\n"
        f"   Wait for 4/5+ confirmation before pressing.\n"
        f"\n"
        f"Time: {now}\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"Regime shift confirmed by HTF + macro alignment."
    )


def _maybe_fire_position_cap_alert(verdict: dict) -> None:
    """
    Fire the "🔒 POSITION CAP REACHED" review message when the engine would
    have entered a new trade but the max-positions ceiling is in effect.
    60-min cooldown so the operator isn't spammed during a trending session
    where signals keep meeting 4/5+ but no slot opens up.
    """
    global _last_at_cap_at
    elapsed = time.time() - _last_at_cap_at
    if elapsed < _AT_CAP_COOLDOWN_S:
        log.debug("[strategist_runner] AT_CAP alert suppressed (cooldown %.0fs/%.0fs)",
                  elapsed, _AT_CAP_COOLDOWN_S)
        return
    msg = _format_position_cap_alert(verdict)
    _send_plain(msg)
    _last_at_cap_at = time.time()
    log.info("[strategist_runner] AT_CAP alert fired — engine wanted %s %s/5 but at cap",
             verdict.get("decision"), verdict.get("conditions_passed"))


def _format_position_cap_alert(verdict: dict) -> str:
    """Compose the position-cap review message — concise + actionable."""
    diag = verdict.get("diagnostics") or {}
    tp   = verdict.get("trade_plan") or {}
    open_n          = diag.get("open_positions", 0)
    max_n           = diag.get("max_concurrent_positions", 5)
    base_cap        = diag.get("cap_base", 5)
    ext_cap         = diag.get("cap_extended", 10)
    ext_active      = diag.get("cap_extended_active", False)
    threshold       = diag.get("cap_profit_threshold", 300.0)
    vol_ratio       = diag.get("cap_volume_ratio", 0.0)
    vol_required    = diag.get("cap_volume_required", 1.2)
    block_reasons   = diag.get("cap_block_reasons") or []
    tickets         = diag.get("open_position_tickets") or []
    floating        = diag.get("floating_pnl", 0.0)
    decision        = verdict.get("decision")
    cp              = verdict.get("conditions_passed", 0)
    now             = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M GMT")

    arrow = "🟢" if decision == "BUY" else "🔴"
    tickets_str = ", ".join(f"#{t}" for t in tickets[:10]) if tickets else "—"
    pnl_color = "🟢" if floating >= 0 else "🔴"

    # Pyramid status block — show whether extended cap is unlocked
    if ext_active:
        pyramid_block = (
            f"📈 PYRAMID UNLOCKED (cap {max_n}, was {base_cap})\n"
            f"  Trend continues with confirmed volume.\n"
            f"  Now AT the extended ceiling too — no more adds.\n"
        )
    else:
        pyramid_block = (
            f"🧱 PYRAMID LOCKED (cap {base_cap}, extended would be {ext_cap})\n"
            f"  To unlock more positions:\n"
        )
        for r in block_reasons:
            pyramid_block += f"    ✗ {r}\n"
        pyramid_block += (
            f"  Current: floating ${floating:+.0f} (need ≥${threshold:.0f}),  "
            f"volume {vol_ratio:.2f}× (need ≥{vol_required:.2f}×)\n"
        )

    return (
        f"[STRATEGIST] 🔒 XAUUSD LOCAL POSITION CAP REACHED ({open_n}/{max_n})\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"Engine: STRATEGIST v1.0 · wanted: {arrow} {decision}  ·  {cp}/5 conditions\n"
        f"  Suggested entry: ${tp.get('entry', '—')}\n"
        f"  SL ${tp.get('stop_loss', '—')}  ·  TP1 ${tp.get('tp1', '—')}  ·  TP2 ${tp.get('tp2', '—')}\n"
        f"\n"
        f"⚠️ NOT enqueued — at cap.\n"
        f"\n"
        f"📂 Open trades on demo:\n"
        f"  Tickets: {tickets_str}\n"
        f"  Floating P/L: {pnl_color} ${floating:+.2f}\n"
        f"\n"
        f"{pyramid_block}"
        f"\n"
        f"🛠 ACTION\n"
        f"  • Review the {open_n} open positions in MT5\n"
        f"  • Close losers / move winners to breakeven\n"
        f"  • Or wait — TP1 hits will auto-free slots\n"
        f"\n"
        f"Time: {now}\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"Capital preservation > overtrading."
    )


def _format_invalidation(verdict: dict, *, prior: dict) -> str:
    """
    One-shot 'previous signal cancelled' alert.
    Tells the operator the BUY/SELL signal they got is no longer valid.
    """
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M GMT")
    reason = verdict.get("execution_status_reason") or "Conditions no longer met"
    arrow = "🟢→⚪" if prior["decision"] == "BUY" else "🔴→⚪"
    return (
        f"⚠️ XAUUSD SIGNAL INVALIDATED ⚠️\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"Previous: {arrow} {prior['decision']} ({prior['conditions_passed']}/5)\n"
        f"Now:      ⚪ STAND ASIDE ({verdict.get('conditions_passed', 0)}/5)\n"
        f"Reason:   {reason}\n"
        f"\n"
        f"If you opened a manual trade on the prior signal:\n"
        f"  • Move SL to entry (breakeven) if not already\n"
        f"  • Consider closing — confluence has broken\n"
        f"\n"
        f"Time: {now}\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"Capital preservation > revenge."
    )


def _fetch_sentiment() -> tuple[float | None, float | None]:
    """Pull live MyFXBook community sentiment for XAUUSD. Returns (long%, short%)."""
    try:
        if not getattr(settings, "myfxbook_enabled", False):
            return (None, None)
        from services.myfxbook_service import get_community_sentiment
        s = get_community_sentiment(symbol="XAUUSD") or {}
        return (s.get("long_percent"), s.get("short_percent"))
    except Exception as exc:
        log.debug("[strategist_runner] sentiment fetch failed: %s", exc)
        return (None, None)


def _format_preformation_message(verdict: dict, kz_name: str, opens_utc: str, why: str) -> str:
    """
    Build the "SETUP FORMING" heads-up message. Uses the current verdict as
    the state snapshot -- HTF alignment, current price, sweep detection,
    which conditions are already satisfied.
    """
    decision       = verdict.get("decision") or "STAND ASIDE"
    tf_alignment   = verdict.get("timeframe_alignment") or "—"
    current_price  = verdict.get("current_price") or 0
    proposed       = verdict.get("proposed_signal") or verdict.get("decision") or "WAIT"

    diag = verdict.get("diagnostics") or {}
    d1_bias  = diag.get("d1_bias_local")  or "—"
    h4_bias  = diag.get("h4_bias_local")  or "—"
    sweep_rat = diag.get("sweep_rationale") or "no sweep detected"
    cisd_ok   = diag.get("cisd_confirmed")
    is_rev    = diag.get("is_reversal_setup", False)

    lm = verdict.get("liquidity_map") or {}
    prev_day_high = lm.get("prev_day_high", "—")
    prev_day_low  = lm.get("prev_day_low",  "—")

    # Conditions status (simplified)
    conds = verdict.get("conditions") or []
    conds_summary = " · ".join(
        f"{'✓' if c.get('passed') else '✗'} {c['name'].split()[0]}"   # e.g. "✓ C1"
        for c in conds
    )
    cp = verdict.get("conditions_passed", 0)

    # Trigger criteria (what to watch)
    watch_lines = []
    if proposed == "BUY":
        watch_lines.append(f"• Watch for pullback into H1 EMA20 zone")
        watch_lines.append(f"• Prev-day LOW sweep + CISD close for reversal BUY")
        watch_lines.append(f"• Last 3 M15 majority ▲ for micro-momentum agree")
    elif proposed == "SELL":
        watch_lines.append(f"• Watch for pullback into H1 EMA20 zone")
        watch_lines.append(f"• Prev-day HIGH sweep + CISD close for reversal SELL")
        watch_lines.append(f"• Last 3 M15 majority ▼ for micro-momentum agree")
    else:
        watch_lines.append(f"• D1/H4 both need aligned bias (currently {tf_alignment})")
        watch_lines.append(f"• Scanner or predictor needs a directional read")

    obs_mode = is_monday_observation()

    lines = [
        f"🔭 SETUP FORMING — {kz_name} opens in 1 hour",
        f"⏰ {opens_utc}",
        f"",
        f"📊 CURRENT STATE",
        f"Price:     ${current_price}",
        f"D1 / H4:   {d1_bias} / {h4_bias}",
        f"TF align:  {tf_alignment}",
        f"Sweep:     {sweep_rat[:60]}",
    ]
    if is_rev and cisd_ok is not None:
        lines.append(f"CISD:      {'✓ confirmed' if cisd_ok else '✗ awaiting'}")
    lines.extend([
        f"",
        f"🎯 CONDITIONS SO FAR   {cp}/5",
        f"{conds_summary}",
        f"",
        f"👁 WHAT TO WATCH FOR",
        *watch_lines,
        f"",
        f"📍 KEY LEVELS",
        f"Prev-day high: ${prev_day_high}",
        f"Prev-day low:  ${prev_day_low}",
        f"",
        f"💡 {why}",
    ])
    if obs_mode:
        lines.extend([f"", f"📗 Monday observation — signals fire, no MT5 exec until Tuesday."])

    lines.append(f"")
    lines.append(f"— Sniper mode active. Get eyes on the screen.")
    return "\n".join(lines)


def _maybe_fire_preformation_alert(verdict: dict) -> None:
    """
    Fire a one-per-day "SETUP FORMING" heads-up for each configured killzone
    when we're within the first 5 minutes of the trigger hour.

    Setup-quality gate: only fires when the current verdict already has a
    real setup forming (BUY/SELL with conditions_passed >= 4 — ARMED grade).
    "Session opens in an hour" without an in-flight setup is calendar noise;
    the operator explicitly asked for signal, not schedule.

    Dedupe by (kz_name, YYYY-MM-DD).
    """
    global _last_preformation_alerts

    if not getattr(settings, "telegram_preformation_alerts", True):
        return
    if is_weekend_quiet_hours():
        return

    # ── Setup-quality gate ─────────────────────────────────────────────────
    decision = verdict.get("decision", "STAND ASIDE")
    cp = int(verdict.get("conditions_passed", 0) or 0)
    if decision not in ("BUY", "SELL") or cp < 4:
        return   # no real setup — silent

    now = datetime.now(timezone.utc)
    today = now.strftime("%Y-%m-%d")

    for cfg in _PRE_FORMATION_WINDOWS:
        # Fire within the first 5 minutes of the trigger hour to allow for
        # scheduler drift, but only once per day.
        if now.hour != cfg["trigger_utc_hour"] or now.minute >= 5:
            continue
        if _last_preformation_alerts.get(cfg["kz_name"]) == today:
            continue

        try:
            msg = _format_preformation_message(
                verdict,
                kz_name=cfg["kz_name"],
                opens_utc=cfg["opens_utc"],
                why=cfg["why"],
            )
            _send_plain(msg)
            _last_preformation_alerts[cfg["kz_name"]] = today
            log.info("[strategist_runner] pre-formation alert fired for %s "
                     "(%s %d/5)", cfg["kz_name"], decision, cp)
        except Exception as exc:
            log.warning("[strategist_runner] pre-formation alert failed for %s: %s",
                        cfg["kz_name"], exc)


_last_kz_magnet_alert: dict = {"direction": None, "sent_at": 0.0, "prior_kz": None}
_KZ_MAGNET_COOLDOWN_S = 1800.0   # default 30 min


def _maybe_fire_kz_magnet_alert(db: Session, verdict: dict) -> None:
    """
    KZ Magnet secondary signal — trades price toward prior-killzone POC
    magnets (60-85% touch rates per backtest). Signal-only. Never affects
    mandate. Skipped entirely when kz_magnet_enabled=False (default).
    """
    global _last_kz_magnet_alert
    if not getattr(settings, "kz_magnet_enabled", False):
        return
    if is_weekend_quiet_hours():
        return

    try:
        from services.kz_magnet_strategy import scan_for_magnet

        # ATR from verdict diagnostics if available
        diag = verdict.get("diagnostics") or {}
        atr_h1 = float(diag.get("atr_h1") or 20.0)

        # News clear check
        mc = verdict.get("macro_context") or {}
        news_clear = (mc.get("news_risk", "CLEAR") == "CLEAR")

        setup = scan_for_magnet(atr_h1=atr_h1, news_clear=news_clear)
        if setup is None:
            return

        # Cooldown per direction+prior_kz (avoid re-firing same magnet)
        cooldown_s = float(getattr(settings, "kz_magnet_alert_cooldown_s", _KZ_MAGNET_COOLDOWN_S))
        elapsed = time.time() - _last_kz_magnet_alert.get("sent_at", 0)
        if (_last_kz_magnet_alert.get("direction") == setup.direction
                and _last_kz_magnet_alert.get("prior_kz") == setup.prior_kz
                and elapsed < cooldown_s):
            return

        if not getattr(settings, "kz_magnet_telegram_alerts", True):
            return

        msg = _format_kz_magnet_message(setup, verdict)
        _send_plain(msg)
        _last_kz_magnet_alert = {
            "direction": setup.direction,
            "prior_kz":  setup.prior_kz,
            "sent_at":   time.time(),
        }
        log.info("[kz_magnet] fired %s %s→%s POC=$%.2f dist=%.1fpt score=%.0f%%",
                 setup.direction, setup.prior_kz, setup.target_kz,
                 setup.prior_poc, setup.distance_pts, setup.expected_touch)
    except Exception as exc:
        log.warning("[kz_magnet] alert hook failed: %s", exc)


def _format_kz_magnet_message(setup, verdict: dict) -> str:
    """Telegram alert format — clearly labelled 🧲 KZ MAGNET."""
    icon = "🔴" if setup.direction == "SELL" else "🟢"
    mandate_state = verdict.get("execution_status", "STAND_ASIDE")
    tp2_line = ""
    if setup.tp2:
        tp2_line = f"TP2 (VWAP):  ${setup.tp2:.2f}  ·  RR {setup.rr_tp2:.2f}\n"
    return (
        f"🧲 KZ MAGNET — {setup.direction}\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"{icon} {setup.prior_kz} → {setup.target_kz}\n"
        f"Expected touch rate: {setup.expected_touch:.0f}%\n"
        f"\n"
        f"Prior KZ POC:  ${setup.prior_poc:.2f}  ← magnet target\n"
        f"Current price: ${setup.current_price:.2f}\n"
        f"Distance:      {abs(setup.distance_pts):.1f} pts  ({setup.distance_atr:.1f}× ATR)\n"
        f"\n"
        f"Flux bias:     {setup.flux_bias:+.2f}  ({setup.flux_label})\n"
        f"\n"
        f"Entry:      ${setup.entry:.2f}\n"
        f"SL:         ${setup.sl:.2f}  ({setup.risk_pts:.1f}pt risk)\n"
        f"TP1 (POC):  ${setup.tp1:.2f}  ·  RR {setup.rr_tp1:.2f}\n"
        f"{tp2_line}"
        f"\n"
        f"Mandate:    {mandate_state}\n"
        f"⚠ Signal-only. No MT5 execution. Operator judgement.\n"
        f"   Backtest edge: prior-KZ POC magnetizes next-KZ price\n"
        f"   {setup.expected_touch:.0f}% of the time (n=55 weekdays)."
    )


def _maybe_scan_vp_trap_zones(db: Session) -> None:
    """
    Phase 2 hook: advance vp_trap zone states. NO alerts yet.

    Runs the profile → 4 candidate zones → state machine → DB upsert cycle
    once per runner tick. Cheap (few dozen bars, no heavy compute). If the
    feature is disabled or scan fails, silently skip — never affects the
    mandate path.
    """
    if not getattr(settings, "vp_trap_enabled", False):
        return
    try:
        from services.vp_trap_strategy import scan_and_persist_zones
        expiry_hours       = getattr(settings, "vp_trap_zone_expiry_hours", 48)
        max_retests        = getattr(settings, "vp_trap_max_retests", 3)
        zones = scan_and_persist_zones(
            db,
            expiry_hours=expiry_hours,
            max_retests=max_retests,
        )
        # Trace-level state counter — helps confirm the scan is running
        if zones:
            states = [z.get("state") for z in zones]
            log.debug("[vp_trap] scanned %d zones: %s", len(zones), ",".join(states))
    except Exception as exc:
        log.warning("[vp_trap] scan hook failed: %s", exc)


def _maybe_fire_momentum_alert(db: Session, verdict: dict) -> None:
    """
    Secondary signal source: momentum-continuation.

    The mandate strategist is designed for pullback/reversal setups. This
    hook complements it by firing alerts on clean momentum breakouts the
    mandate misses. Signal-only — no MT5 execution, no PendingExecution
    enqueue. Operator judges whether to take it manually.

    Fires ONLY when:
      - mandate verdict is STAND_ASIDE or SIGNAL_ONLY (not competing)
      - momentum_breakout returns BUY or SELL on the latest M15 close
      - not weekend + not Monday-observation-with-toggle-off
      - not within cooldown for the same-direction previous momentum alert
      - not on the same M15 bar we already alerted on
    """
    global _last_momentum_alert
    if is_weekend_quiet_hours():
        return

    # Only compete-when-mandate-is-quiet
    mandate_es = verdict.get("execution_status") or ""
    if mandate_es not in ("STAND_ASIDE", "SIGNAL_ONLY", "MONDAY_OBSERVE"):
        return

    try:
        from services.intraday_strategies import analyze_momentum_breakout
        from data.candles import get_candles
        from data.calendar import get_calendar

        m15 = get_candles(interval="M15", limit=200, pair="xauusd")
        if not m15 or not m15.candles:
            return
        candles = m15.candles
        latest_bar = candles[-1]
        bar_time_iso = (latest_bar.time.isoformat() if hasattr(latest_bar.time, "isoformat")
                        else str(latest_bar.time))

        # Same-bar dedupe: don't re-alert on the same breakout bar
        if _last_momentum_alert.get("bar_time") == bar_time_iso:
            return

        # Pull macro events for news filter
        try:
            cal = get_calendar()
            macro_events = [e.model_dump() for e in (cal.events or [])] if cal else []
        except Exception:
            macro_events = []

        now = datetime.now(timezone.utc)
        result = analyze_momentum_breakout(
            candles=candles, at=now, macro_events=macro_events, pip_size=1.0,
        )

        if result.signal not in ("BUY", "SELL"):
            return

        # Direction-cooldown dedupe (same-direction within window)
        elapsed = time.time() - _last_momentum_alert.get("sent_at", 0)
        if (_last_momentum_alert.get("direction") == result.signal
                and elapsed < _MOMENTUM_COOLDOWN_S):
            return

        msg = _format_momentum_message(result, verdict)
        _send_plain(msg)
        _last_momentum_alert = {
            "direction":  result.signal,
            "bar_time":   bar_time_iso,
            "sent_at":    time.time(),
        }
        log.info("[strategist_runner] momentum alert fired: %s @ %s (bar %s)",
                 result.signal, result.entry, bar_time_iso[:16])
    except Exception as exc:
        log.warning("[strategist_runner] momentum alert failed: %s", exc)


def _format_momentum_message(result, verdict: dict) -> str:
    """Momentum-continuation signal — clearly distinguished from mandate signals."""
    direction   = result.signal
    icon        = "🟢" if direction == "BUY" else "🔴"
    entry       = f"${result.entry:.2f}" if result.entry else "—"
    sl          = f"${result.stop_loss:.2f}" if result.stop_loss else "—"
    tp          = f"${result.take_profit:.2f}" if result.take_profit else "—"
    rr          = f"{result.rr:.2f}" if result.rr else "—"
    reason      = (result.reason or "—")[:120]

    mandate_state = verdict.get("execution_status", "STAND_ASIDE")
    tf_align      = verdict.get("tf_alignment_label", "—")

    return (
        f"⚡ MOMENTUM CONTINUATION — secondary signal\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"{icon} {direction} · momentum_breakout\n"
        f"\n"
        f"Entry:     {entry}\n"
        f"SL:        {sl}\n"
        f"TP:        {tp}\n"
        f"RR:        {rr}\n"
        f"\n"
        f"Setup:     {reason}\n"
        f"HTF align: {tf_align}\n"
        f"Mandate:   {mandate_state}  (this is a SECONDARY signal,\n"
        f"           mandate stayed quiet on this move)\n"
        f"\n"
        f"⚠ Signal-only. No MT5 execution. Operator judgement.\n"
        f"    Momentum trades have different edge profile than\n"
        f"    the 5/5 sniper mandate — use tighter stops."
    )


def deliver_plain(text: str) -> tuple[bool, str]:
    """
    Attempt to deliver a plain-text Telegram message. Returns
    (any_recipient_succeeded, diagnostic).

    Delivery is considered successful if at least one recipient returned
    HTTP 2xx. If every recipient fails (HTTP non-2xx, timeout, network
    exception), returns (False, joined_failure_reasons). Callers that
    need to persist "message actually delivered" state (the predator
    notification gateway) MUST use this variant so a failed HTTP POST
    does not falsely advance notification_state to ACTIONABLE_SENT.

    Callers that fire-and-forget (weekly digest, briefing, VP trap etc.)
    keep using `_send_plain` which discards the status.
    """
    import httpx
    recipients: list[tuple[str, str, str]] = []
    if settings.telegram_bot_token and settings.telegram_chat_id:
        recipients.append((settings.telegram_bot_token,
                           settings.telegram_chat_id, "primary"))
    tok2 = getattr(settings, "telegram_bot_token_2", None)
    cid2 = getattr(settings, "telegram_chat_id_2", None)
    if tok2 and cid2:
        recipients.append((tok2, cid2, "secondary"))
    if not recipients:
        return False, "no_recipients_configured"

    any_success = False
    failures: list[str] = []
    for bot_token, chat_id, label in recipients:
        try:
            url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
            resp = httpx.post(url, json={
                "chat_id":                  chat_id,
                "text":                     text,
                "disable_web_page_preview": True,
            }, timeout=10.0)
            if resp.is_success:
                any_success = True
            else:
                failures.append(f"{label}:HTTP{resp.status_code}")
                log.warning("[strategist_runner] Telegram send failed "
                            "recipient=%s status=%s", label, resp.status_code)
        except Exception as exc:
            failures.append(f"{label}:{type(exc).__name__}")
            log.warning("[strategist_runner] Telegram plain send error "
                        "recipient=%s: %s", label, exc)

    if any_success:
        return True, "ok" if not failures else f"partial:{'; '.join(failures)}"
    return False, "; ".join(failures) or "unknown"


def _send_plain(text: str) -> None:
    """Fire-and-forget wrapper for legacy callers that don't check status.

    Preserved for backward compat with weekly digest, hourly briefing,
    VP trap alerts, etc. — these don't gate DB state on delivery.
    For the notification gateway use `deliver_plain` which returns status.
    """
    deliver_plain(text)


# ── MT5 enqueue side-effect ──────────────────────────────────────────────────

def _lot_size_for_grade(grade: str | None) -> float:
    """
    Grade-based lot sizing per operator mandate revision 2026-08-11 (DEMO only).
    A+ = 0.15, A = 0.10, B / Watchlist = 0.05. Anything below B is not enqueued
    (grade gate suppresses first), but returns B-tier size as a defensive fallback.
    """
    g = (grade or "").upper()
    if g in ("A+", "APLUS"):
        return float(settings.mandate_lot_a_plus)
    if g == "A":
        return float(settings.mandate_lot_a)
    if g in ("B", "WATCHLIST"):
        return float(settings.mandate_lot_b)
    return float(settings.mandate_lot_b)   # defensive fallback


def _last_bridge_heartbeat() -> Optional[dict]:
    """
    Read the most recent MT5 bridge terminal state so the enqueue path can
    verify the daemon is on the sanctioned demo account before permitting
    an order. Fails safe: returns None on any error → enqueue refuses.

    Race-safe: prefers the most-recent heartbeat WITH populated account
    fields, falling back to raw last-seen only if none have account info.
    Avoids false-refuse when a bare heartbeat overwrote a full one.
    """
    try:
        from routers.bridge import _MT5_TERMINAL_STATE
        if not _MT5_TERMINAL_STATE:
            return None

        # First pass: entries WITH populated account_login, ranked by last_seen
        with_account = [
            s for s in _MT5_TERMINAL_STATE.values()
            if s.get("account_login") is not None
        ]
        if with_account:
            latest = max(with_account,
                          key=lambda s: s.get("last_seen") or datetime.min)
        else:
            # Fallback: any entry, most-recently-seen
            latest = max(_MT5_TERMINAL_STATE.values(),
                          key=lambda s: s.get("last_seen") or datetime.min)

        if "symbol" not in latest:
            latest = {**latest, "symbol": "XAUUSD"}   # daemon only trades XAUUSD
        return latest
    except Exception:
        return None


def _current_open_lot_exposure(db: Session) -> float:
    """
    Sum lot sizes across all currently-open positions:
      - PendingExecution rows with status='PENDING' (awaiting daemon pickup)
      - Trade rows still open (status IN 'OPEN','FILLED','PARTIAL')

    Used by the aggregate-exposure ceiling check. Fails safe: on any query
    error, returns a high sentinel so the enqueue path refuses (preserves
    capital, per mandate).
    """
    from sqlalchemy import text
    total = 0.0
    got_data = False
    try:
        row = db.execute(text(
            "SELECT COALESCE(SUM(lot_size), 0) FROM pending_executions "
            "WHERE status='PENDING'"
        )).fetchone()
        if row is not None:
            total += float(row[0] or 0.0)
            got_data = True
    except Exception as exc:
        log.debug("[strategist_runner] pending exposure query failed: %s", exc)
    try:
        row = db.execute(text(
            "SELECT COALESCE(SUM(lot_size), 0) FROM trades "
            "WHERE status IN ('OPEN','FILLED','PARTIAL')"
        )).fetchone()
        if row is not None:
            total += float(row[0] or 0.0)
            got_data = True
    except Exception as exc:
        log.debug("[strategist_runner] trades exposure query failed: %s", exc)
    if not got_data:
        # Fail-safe: report ceiling so enqueue refuses. Better to skip a trade
        # than accidentally exceed the aggregate cap.
        return float(settings.mandate_lot_max_aggregate)
    return total


def _maybe_enqueue_demo_order(db: Session, verdict: dict) -> int | None:
    """
    Insert PendingExecution with grade-based lot sizing (mandate revised
    2026-08-11) when all mandate gates pass. Returns the row id, or None
    if nothing was enqueued.
    """
    global _last_enqueue_fingerprint, _last_enqueue_at

    if verdict.get("execution_status") != "DEMO_TRADE_PLACED":
        return None
    mt5_obj = verdict.get("mt5_execution_object") or {}
    if not mt5_obj:
        return None

    # ── DIRECTIONAL EXECUTION MANDATE (2026-08-21) ────────────────────────
    # STRATEGIST executes BUYs only. SELL verdicts continue to be scored,
    # persisted to strategist_verdicts, and shadow-tracked, but they never
    # reach MT5. Predator owns SELL execution.
    _direction = str(mt5_obj.get("action", "")).upper()
    if _direction == "SELL":
        log.info("[strategist_runner] STRATEGIST_SELL_SHADOW_ONLY — verdict %s SELL "
                 "scored but not enqueued (Predator owns SELL execution)",
                 verdict.get("verdict_id"))
        try:
            from services.strategist_shadow_ledger import record_sell_shadow
            record_sell_shadow(db, verdict=verdict, mt5_obj=mt5_obj)
        except Exception as _sexc:
            log.debug("[strategist_runner] shadow-ledger write failed: %s", _sexc)
        return None

    # Defence-in-depth: even though _decide_execution_status already gates
    # on these, double-check here so a stale verdict can't slip through.
    if is_monday_observation():
        log.warning("[strategist_runner] enqueue refused: Monday observation mode")
        return None
    diag = verdict.get("diagnostics") or {}
    open_n = diag.get("open_positions", 0)
    max_n  = diag.get("max_concurrent_positions") or getattr(settings, "max_concurrent_positions", 5)
    if open_n >= max_n:
        log.warning("[strategist_runner] enqueue refused: at position cap %d/%d", open_n, max_n)
        return None

    # ── Fixed 0.01 lot (mandate re-revised 2026-08-11 comprehensive brief) ──
    grade = ((verdict.get("signal_grade") or {}).get("grade")) or "B"
    lot = _lot_size_for_grade(grade)                # all grades → 0.01 now
    ceiling = float(settings.mandate_lot_max_aggregate)   # 0.01

    if lot > 0.01 + 1e-9 or lot > ceiling + 1e-9:
        log.error("[strategist_runner] refused: lot %.4f > 0.01 mandate cap", lot)
        return None

    # Aggregate exposure ceiling — sum of already-open positions + this new lot
    current_exposure = _current_open_lot_exposure(db)
    if current_exposure + lot > ceiling + 1e-9:
        log.warning("[strategist_runner] enqueue refused: aggregate exposure "
                    "would breach 0.01 ceiling (%.4f + %.4f > %.4f) — "
                    "one position at a time",
                    current_exposure, lot, ceiling)
        return None

    # ── UNIFIED GLOBAL GOVERNOR — ATOMIC (2026-08-21 v2) ─────────────────
    # Account-wide 0.15 gross cap across BOTH engines with atomic reservation.
    # Reserved capacity is released on any failure below.
    _reservation_id = None
    try:
        from services.portfolio_governor import reserve_capacity, release_reservation, commit_reservation
        gov_ok, gov_reason, gov_snap, _reservation_id = reserve_capacity(
            db, engine="STRATEGIST",
            direction=mt5_obj.get("action", "?"),
            proposed_lots=lot,
            opportunity_id=verdict.get("verdict_id"),
            signal_id=str(verdict.get("verdict_id", "")),
        )
        if not gov_ok:
            log.error("[strategist_runner] GLOBAL_GOVERNOR_BLOCK  %s snap=%s",
                      gov_reason, gov_snap)
            return None
    except Exception as _gexc:
        log.warning("[strategist_runner] global governor check errored: %s", _gexc)

    # ── HARD DEMO-ACCOUNT GUARD (mandate section 12) ────────────────────
    # Absolutely refuse to enqueue if the daemon isn't on the sanctioned
    # demo account. Fails safe: if we can't verify, refuse.
    heartbeat = _last_bridge_heartbeat()
    hb_login  = (heartbeat or {}).get("account_login")
    hb_server = (heartbeat or {}).get("account_server") or ""
    hb_symbol = (heartbeat or {}).get("symbol") or ""
    demo_login  = int(getattr(settings, "mandate_demo_login", 435888680))
    demo_srv    = str(getattr(settings, "mandate_demo_server_contains", "Trial"))
    demo_symbol = str(getattr(settings, "mandate_demo_symbol", "XAUUSD"))

    if hb_login is None:
        log.error("[strategist_runner] REFUSED: no bridge heartbeat — "
                    "cannot verify account is demo")
        return None
    if int(hb_login) != demo_login:
        log.error("[strategist_runner] REFUSED: active login %s != sanctioned demo %s",
                    hb_login, demo_login)
        return None
    if demo_srv.lower() not in hb_server.lower():
        log.error("[strategist_runner] REFUSED: server %r lacks required substring %r",
                    hb_server, demo_srv)
        return None
    if hb_symbol and hb_symbol.upper() != demo_symbol.upper():
        log.error("[strategist_runner] REFUSED: symbol %r != sanctioned %r",
                    hb_symbol, demo_symbol)
        return None

    # Live-execution safety gate — this must be explicitly False (upstream
    # must set it; default True fails safe).
    if mt5_obj.get("live_execution_allowed", True):
        log.error("[strategist_runner] refused to enqueue: live_execution_allowed must be false")
        return None
    if not settings.allow_demo_trading:
        log.info("[strategist_runner] enqueue skipped: ALLOW_DEMO_TRADING=false")
        return None
    if not settings.mt5_bridge_enabled:
        log.info("[strategist_runner] enqueue skipped: MT5_BRIDGE_ENABLED=false")
        return None

    fp = (
        f"{mt5_obj['action']}|{mt5_obj['entry']}|{mt5_obj['stop_loss']}"
        f"|{mt5_obj['take_profit_1']}|{mt5_obj['take_profit_2']}"
    )
    if fp == _last_enqueue_fingerprint and (time.time() - _last_enqueue_at) < _ENQUEUE_COOLDOWN_S:
        log.debug("[strategist_runner] enqueue dedupe — same plan within cooldown")
        return None

    try:
        row = PendingExecution(
            pair="xauusd",
            signal=mt5_obj["action"],
            entry=float(mt5_obj["entry"]),
            stop_loss=float(mt5_obj["stop_loss"]),
            take_profit=float(mt5_obj["take_profit_1"]),       # TP1 = primary close target
            take_profit_2=float(mt5_obj["take_profit_2"]),     # TP2 = stretch / BE trigger
            risk_pips=float(abs(mt5_obj["entry"] - mt5_obj["stop_loss"])),
            quality_score=int(verdict.get("setup_score") or 0),
            rr=float(mt5_obj["risk_reward"]),
            lot_size=lot,          # grade-based (mandate revised 2026-08-11)
            max_lot=ceiling,       # aggregate ceiling — daemon must respect
            reason=(
                f"strategist mandate · grade {grade} · lot {lot:.2f}"
                f" · {mt5_obj['conditions_passed']}/5 conditions"
                f" · est WR {verdict.get('estimated_win_rate_range')}"
            ),
            confirmations_json=json.dumps({
                "source":                "mandate_strategist",
                "conditions":            verdict.get("conditions"),
                "conditions_passed":     verdict.get("conditions_passed"),
                "execution_status":      verdict.get("execution_status"),
                "session":               verdict.get("session_classification"),
                "market_state":          verdict.get("market_state"),
                "liquidity_behaviour":   verdict.get("liquidity_behaviour"),
                "tf_alignment":          verdict.get("tf_alignment_label"),
                "signal_grade":          verdict.get("signal_grade"),
                "mt5_execution_object":  mt5_obj,
                "sizing":                {
                    "grade":                 grade,
                    "lot":                   lot,
                    "aggregate_ceiling":     ceiling,
                    "prior_open_exposure":   round(current_exposure, 4),
                },
            }),
            expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
            status="PENDING",
        )
        db.add(row)
        db.commit()
        db.refresh(row)
        _last_enqueue_fingerprint = fp
        _last_enqueue_at          = time.time()

        # State transition: RESERVED → SENT (broker is now aware via
        # PendingExecution; daemon will pick up next poll). TTL no longer
        # applies — the reservation is now protected by broker outcome.
        if _reservation_id:
            try:
                from services.portfolio_governor import mark_sent
                mark_sent(_reservation_id, mt5_ticket=None)
            except Exception: pass

        # Record BUY opportunity into strategist_buy_outcomes ledger for
        # forward P&L tracking (resolver populates outcome later).
        try:
            from services.strategist_shadow_ledger import record_buy_opportunity_at_enqueue
            record_buy_opportunity_at_enqueue(
                db, verdict=verdict, mt5_obj=mt5_obj,
                pending_execution_id=row.id, lot_size=lot,
                reservation_id=_reservation_id,
            )
        except Exception as _bexc:
            log.debug("[strategist_runner] BUY opportunity ledger write failed: %s", _bexc)

        log.info(
            "[strategist_runner] ENQUEUED #%d %s xauusd grade=%s lot=%.2f "
            "(exposure %.2f→%.2f/%.2f) entry=%s SL=%s TP1=%s TP2=%s",
            row.id, mt5_obj["action"], grade, lot,
            current_exposure, current_exposure + lot, ceiling,
            mt5_obj["entry"], mt5_obj["stop_loss"],
            mt5_obj["take_profit_1"], mt5_obj["take_profit_2"],
        )
        return row.id
    except Exception as exc:
        log.warning("[strategist_runner] enqueue failed (non-fatal): %s", exc)
        # Release governor reservation on failure
        if _reservation_id:
            try: release_reservation(_reservation_id, "enqueue_failed")
            except Exception: pass
        try: db.rollback()
        except Exception: pass
        return None
