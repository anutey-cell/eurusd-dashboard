"""
Mandate Adapter — Verdict → CanonicalSignal → Registry → Router
================================================================

Translates each mandate strategist verdict into a CanonicalSignal, upserts
into the registry, advances the state machine, and dispatches through the
new notification client (in dry-run during SHADOW mode).

Runs alongside the legacy Telegram path in shadow mode: both fire on the
same verdict, but only the legacy path actually emits messages. The
canonical layer builds an audit trail via TelegramNotification rows we
can diff against the legacy `alert_log` before cutover.

State mapping (mandate → canonical):
    STAND ASIDE                 → skip (no persistent signal)
    3/5   (SIGNAL_ONLY, ws)     → MONITORING
    4/5+ (any exec status)      → ARMED
    4/5+ that regressed to <3   → INVALIDATED (transition existing)

Downgrade / regression handling:
    If the current tick's decision is STAND ASIDE OR conditions_passed
    drops below 3, and the last-persisted matching signal is not yet
    terminal, transition it → INVALIDATED and dispatch the
    'invalidated' template.

The adapter is idempotent: calling it multiple times with the same verdict
produces at most one persisted signal per (session, day, direction, zone).
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone, timedelta
from typing import Optional

from sqlalchemy.orm import Session

from services.canonical_signal import (
    STATE_MONITORING, STATE_ARMED, STATE_INVALIDATED,
    STATE_DETECTED,
    DIRECTION_BUY, DIRECTION_SELL,
    STRATEGY_MANDATE,
    message_type_for,
)
from services.signal_registry import (
    upsert as reg_upsert,
    transition as reg_transition,
    active_signals,
)
from services.telegram_templates import render as render_template, MODE_STANDARD
from services.telegram_client import get_client, TelegramClient

log = logging.getLogger(__name__)


# ── Config ───────────────────────────────────────────────────────────────────

_SIGNAL_VALIDITY_HOURS = 4   # how long an ARMED/MONITORING signal stays valid


def _session_from_verdict(verdict: dict) -> str:
    """Extract a stable session name for fingerprinting."""
    sess = verdict.get("session_classification") or ""
    if sess:
        return str(sess)
    # Fallback via killzone label from diagnostics
    diag = verdict.get("diagnostics", {}) or {}
    kz = diag.get("scanner_state") or "unknown"
    return f"scanner:{kz}"


def _confidence_from_verdict(verdict: dict) -> int:
    """Map mandate 0-100 setup_score → CanonicalSignal.confidence."""
    score = verdict.get("setup_score")
    if isinstance(score, (int, float)) and 0 <= score <= 100:
        return int(score)
    # Fallback: 30/60/75/90 by conditions_passed
    cp = int(verdict.get("conditions_passed", 0) or 0)
    return {0: 20, 1: 30, 2: 45, 3: 60, 4: 75, 5: 90}.get(cp, 30)


def _desired_state(conditions_passed: int, decision: str) -> Optional[str]:
    """State the current tick wants the signal to be in.
    Returns None when there's nothing to persist."""
    if decision not in ("BUY", "SELL"):
        return None
    if conditions_passed < 3:
        return None
    if conditions_passed == 3:
        return STATE_MONITORING
    return STATE_ARMED    # 4 or 5


def _conditions_lists(verdict: dict) -> tuple[list[str], list[str]]:
    """Split verdict.conditions into met + missing lists.

    Handles both prod shape (list of dicts with `name`/`passed`) and the
    dict-of-dicts shape used in some tests.
    """
    conds = verdict.get("conditions")
    met, missing = [], []
    if isinstance(conds, list):
        for obj in conds:
            if not isinstance(obj, dict):
                continue
            key   = obj.get("name") or obj.get("id") or "?"
            label = obj.get("label") or obj.get("summary") or ""
            text  = f"{key}: {label}" if label else str(key)
            (met if obj.get("passed") else missing).append(text)
    elif isinstance(conds, dict):
        for key, obj in conds.items():
            if not isinstance(obj, dict):
                continue
            passed = obj.get("passed") or obj.get("result") == "pass"
            label  = obj.get("label") or obj.get("summary") or key
            (met if passed else missing).append(f"{key}: {label}")
    return met, missing


def _plan_targets(verdict: dict) -> tuple:
    tp = verdict.get("trade_plan", {}) or {}
    return (tp.get("tp1"), tp.get("tp2"), tp.get("tp3"),
            tp.get("risk_reward"))


# ── Verdict → CanonicalSignal params ─────────────────────────────────────────

def mandate_verdict_to_signal(verdict: dict, *, now: Optional[datetime] = None) -> Optional[dict]:
    """
    Convert a mandate verdict dict into kwargs suitable for signal_registry.upsert().
    Returns None if the verdict shouldn't produce a canonical signal.

    Pure function — no DB, no side effects. Good for testing.
    """
    if not verdict:
        return None
    decision = verdict.get("decision", "STAND ASIDE")
    cp = int(verdict.get("conditions_passed", 0) or 0)
    desired = _desired_state(cp, decision)
    if desired is None:
        return None

    tp = verdict.get("trade_plan", {}) or {}
    entry = tp.get("entry")
    sl    = tp.get("stop_loss")
    if entry is None or sl is None:
        return None       # no tradeable levels — can't fingerprint

    tol = float(tp.get("entry_tolerance") or 0)
    entry_low, entry_high = (entry - tol, entry + tol) if tol > 0 else (entry, entry)

    tp1, tp2, tp3, rr = _plan_targets(verdict)
    session = _session_from_verdict(verdict)
    met, missing = _conditions_lists(verdict)

    direction = DIRECTION_BUY if decision == "BUY" else DIRECTION_SELL
    conf = _confidence_from_verdict(verdict)
    now = now or datetime.now(timezone.utc)

    return dict(
        strategy_id=STRATEGY_MANDATE,
        strategy_name="Mandate 5-Gate",
        instrument="XAUUSD",
        direction=direction,
        confidence=conf,
        entry_zone_low=float(entry_low),
        entry_zone_high=float(entry_high),
        stop_loss=float(sl),
        invalidation=str(tp.get("invalidation") or f"Close through {sl}"),
        session=session,
        tp1=(float(tp1) if tp1 is not None else None),
        tp2=(float(tp2) if tp2 is not None else None),
        tp3=(float(tp3) if tp3 is not None else None),
        tp1_label="TP1",
        tp2_label="TP2",
        tp3_label="Runner",
        rr_tp1=(float(rr) if rr else None),   # legacy verdict only carries one RR
        market_regime=str(verdict.get("market_sentiment") or ""),
        htf_bias=str((verdict.get("timeframe_alignment") or {}).get("alignment_summary") or ""),
        conditions_met=met,
        conditions_missing=missing,
        rationale=str(verdict.get("final_verdict") or "")[:1024],
        data_source="mandate_verdict",
        valid_until=now + timedelta(hours=_SIGNAL_VALIDITY_HOURS),
        initial_state=STATE_DETECTED,   # registry will up-transition below
        now=now,
        _desired_state=desired,          # NOT a real upsert param — adapter uses it below
    )


# ── Full pipeline: verdict → registry → templates → client ───────────────────

def on_mandate_verdict(
    db: Session,
    verdict: dict,
    *,
    client: Optional[TelegramClient] = None,
    force_dry_run: bool = False,
    mode: str = MODE_STANDARD,
    now: Optional[datetime] = None,
) -> dict:
    """
    Called every strategist tick with the freshly computed verdict.

    Returns a summary dict describing what happened:
        {
          "action":            "created" | "transitioned" | "unchanged" | "invalidated" | "skipped",
          "signal_id":         "MDT-XAU-20260724-001" | None,
          "state":             "MONITORING" | "ARMED" | ...,
          "notification":      {...} | None,     # from client.send_notification
          "reason":            str,
        }

    Never raises — every failure is caught + logged + reflected in the return.
    """
    try:
        now = now or datetime.now(timezone.utc)

        # ── 1. Regression path: current tick says no-trade, invalidate active ──
        decision = verdict.get("decision", "STAND ASIDE") if verdict else "STAND ASIDE"
        cp = int(verdict.get("conditions_passed", 0) or 0) if verdict else 0
        if decision not in ("BUY", "SELL") or cp < 3:
            return _maybe_invalidate_prior(db, verdict, client, force_dry_run, mode, now)

        # ── 2. Convert verdict → registry.upsert kwargs ─────────────────────
        params = mandate_verdict_to_signal(verdict, now=now)
        if params is None:
            return {"action": "skipped", "reason": "verdict_not_actionable"}

        desired_state = params.pop("_desired_state")

        # ── 3. Upsert into registry (idempotent by fingerprint) ─────────────
        sig = reg_upsert(db, **params)

        # ── 4. Transition if state needs advancing ──────────────────────────
        from_state = sig.state
        if from_state == desired_state:
            action = "unchanged"
        else:
            try:
                sig = reg_transition(
                    db,
                    signal_id=sig.signal_id,
                    to_state=desired_state,
                    reason=f"mandate {cp}/5 → {desired_state}",
                    now=now,
                )
                action = "transitioned" if from_state != STATE_DETECTED else "created"
            except ValueError as exc:
                log.warning("[mandate_adapter] invalid transition %s → %s: %s",
                            from_state, desired_state, exc)
                return {"action": "skipped", "signal_id": sig.signal_id,
                        "state": sig.state, "reason": f"invalid_transition: {exc}"}

        db.commit()

        # ── 5. Dispatch notification if this transition has a template ──────
        notif = _maybe_dispatch(db, sig, from_state, desired_state,
                                 client=client, force_dry_run=force_dry_run,
                                 mode=mode, now=now)

        return {
            "action":       action,
            "signal_id":    sig.signal_id,
            "state":        sig.state,
            "notification": notif,
            "reason":       f"{cp}/5 · {decision}",
        }

    except Exception as exc:
        log.exception("[mandate_adapter] failure: %s", exc)
        try:
            db.rollback()
        except Exception:
            pass
        return {"action": "skipped", "reason": f"error: {exc}"}


# ── Helpers ──────────────────────────────────────────────────────────────────

def _maybe_dispatch(
    db: Session, sig, from_state: str, to_state: str,
    *, client: Optional[TelegramClient], force_dry_run: bool,
    mode: str, now: datetime,
) -> Optional[dict]:
    """Render template + send via client. Silent transitions → None."""
    msg_type = message_type_for(from_state, to_state)
    if msg_type is None:
        return None
    try:
        payload = render_template(msg_type, sig, mode=mode, now=now)
    except Exception as exc:
        log.warning("[mandate_adapter] template render failed for %s: %s",
                    msg_type, exc)
        return None

    c = client or get_client()
    # Shadow-mode gate: caller can force dry-run even if client is live.
    # We temporarily set the client's dry_run attr for THIS call by using
    # a suppression_reason instead — cleaner than mutating shared state.
    suppression = "shadow_mode_dry_run" if force_dry_run else None

    try:
        return c.send_notification(
            db,
            signal_id=sig.signal_id,
            strategy_id=sig.strategy_id,
            from_state=from_state,
            to_state=to_state,
            payload=payload,
            suppression_reason=suppression,
        )
    except Exception as exc:
        log.warning("[mandate_adapter] send failed: %s", exc)
        return {"delivered": False, "result": "error", "error": str(exc)}


def _maybe_invalidate_prior(
    db: Session, verdict: dict,
    client: Optional[TelegramClient], force_dry_run: bool,
    mode: str, now: datetime,
) -> dict:
    """When current tick says no-trade, invalidate any still-active mandate
    signal from a prior tick (so we emit a clean 'invalidated' message)."""
    try:
        active = active_signals(db, "XAUUSD")
    except Exception:
        return {"action": "skipped", "reason": "no_active_registry_scan"}
    mandate_actives = [s for s in active if s.strategy_id == STRATEGY_MANDATE
                        and s.state in {STATE_MONITORING, STATE_ARMED}]
    if not mandate_actives:
        return {"action": "skipped", "reason": "no_prior_mandate_signal"}

    reason = (verdict.get("stand_aside_reason") or
              verdict.get("execution_status_reason") or
              "Conditions dropped below threshold")

    results = []
    for prior in mandate_actives:
        try:
            new_sig = reg_transition(
                db, signal_id=prior.signal_id, to_state=STATE_INVALIDATED,
                reason=f"regression: {reason}", now=now,
            )
            notif = _maybe_dispatch(db, new_sig, prior.state, STATE_INVALIDATED,
                                     client=client, force_dry_run=force_dry_run,
                                     mode=mode, now=now)
            results.append({"signal_id": new_sig.signal_id, "notif": notif})
        except Exception as exc:
            log.warning("[mandate_adapter] invalidate transition failed %s: %s",
                        prior.signal_id, exc)
    db.commit()
    return {"action": "invalidated", "count": len(results),
            "results": results, "reason": reason}
