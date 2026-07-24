"""
Notification Router — Policy Layer Between Adapters and Client
===============================================================

Every dispatch goes through here. The router:
  1. Determines the message type from the state transition.
  2. Applies score/confidence thresholds (per strategy).
  3. Picks the verbosity mode (per strategy).
  4. Enforces mute rules (per strategy, per chat, global quiet hours).
  5. Renders the template.
  6. Delegates the actual send to telegram_client (which is the sole
     I/O + audit chokepoint).

Adapters call `route(db, signal, from_state, to_state, extra=...)` and
never touch the client or templates directly. This keeps the policy
decisions in one place — a settings change here immediately affects
every strategy without touching adapter code.

Thresholds
----------
Per the mandate brief:
    ≥ 65  monitoring_threshold  → dispatch monitoring / actionable / etc.
    ≥ 80  actionable_threshold  → allow ARMED-tier alerts
    ≥ 90  high_confluence       → allow aggregator alert (P8)

Below the monitoring threshold a message-eligible transition is
recorded as `suppression_reason="below_monitoring_threshold"` — full
audit row, no wire traffic.

Modes
-----
Per-strategy verbosity: `settings.notification_mode_<strategy>` if set,
else `settings.notification_mode`, else "standard".
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone, timedelta, time as dtime
from typing import Optional

from sqlalchemy.orm import Session

from services.canonical_signal import (
    CanonicalSignal, message_type_for,
)
from services.telegram_templates import (
    render as render_template,
    ALL_MODES, MODE_STANDARD,
)
from services.telegram_client import get_client, TelegramClient

log = logging.getLogger(__name__)


# ── Policy defaults ──────────────────────────────────────────────────────────

_DEFAULT_THRESHOLDS = {
    "monitoring":       65,
    "actionable":       80,
    "high_confluence":  90,
    # Post-entry (TP/BE/stop/trail) uses actionable threshold — if a signal
    # made it past ARMED it's already above 80 by construction, but we still
    # gate here in case something odd created the row.
    "tp1_hit":          80,
    "tp2_hit":          80,
    "final_target":     80,
    "breakeven":        80,
    "trailing":         80,
    "stop_hit":         0,      # ALWAYS send stop-hits, regardless of score
    "invalidated":      65,     # tell operator when a watched setup breaks
    "expired":          80,     # only ARMED expiries, not stale MONITORING
    "entry_triggered":  80,
    "post_trade_review": 0,     # always send — retrospective is unconditional
    "end_of_session":   0,      # always send — recap is scheduled
}

# Quiet hours (EAT local — the operator's timezone) during which non-critical
# alerts are suppressed. Stop-hits still fire.
_QUIET_START_EAT = 0    # 00:00 EAT
_QUIET_END_EAT   = 5    # 05:00 EAT  (nothing but stop_hit)
_ALWAYS_ON_TYPES = {"stop_hit", "post_trade_review"}

_EAT_OFFSET = timedelta(hours=3)


def _in_quiet_hours(now: datetime) -> bool:
    eat = (now.astimezone(timezone.utc) + _EAT_OFFSET).time()
    if _QUIET_START_EAT <= _QUIET_END_EAT:
        return dtime(_QUIET_START_EAT, 0) <= eat < dtime(_QUIET_END_EAT, 0)
    # wraps midnight
    return eat >= dtime(_QUIET_START_EAT, 0) or eat < dtime(_QUIET_END_EAT, 0)


# ── Policy helpers ───────────────────────────────────────────────────────────

def _resolve_threshold(msg_type: str, settings) -> int:
    """Threshold for this message type. Settings can override defaults."""
    key = f"notification_threshold_{msg_type}"
    override = getattr(settings, key, None)
    if isinstance(override, int) and override >= 0:
        return override
    return _DEFAULT_THRESHOLDS.get(msg_type, 65)


def _resolve_mode(strategy_id: str, settings) -> str:
    """Verbosity mode for this strategy: per-strategy override else global."""
    per_strategy = getattr(settings, f"notification_mode_{strategy_id}", None)
    if per_strategy and per_strategy in ALL_MODES:
        return per_strategy
    global_mode = getattr(settings, "notification_mode", MODE_STANDARD)
    if global_mode not in ALL_MODES:
        return MODE_STANDARD
    return global_mode


def _resolve_muted(strategy_id: str, settings) -> Optional[str]:
    """None if strategy is active, else the suppression reason."""
    muted = getattr(settings, f"notification_mute_{strategy_id}", False)
    if muted:
        return f"strategy_muted:{strategy_id}"
    if not getattr(settings, "notification_canonical_enabled", True):
        return "canonical_layer_disabled"
    return None


# ── Public API ───────────────────────────────────────────────────────────────

def route(
    db: Session,
    signal: CanonicalSignal,
    from_state: str,
    to_state: str,
    *,
    extra: Optional[dict] = None,
    client: Optional[TelegramClient] = None,
    force_dry_run: bool = False,
    chat_id: Optional[str] = None,
    now: Optional[datetime] = None,
    settings_override=None,
) -> Optional[dict]:
    """
    Dispatch a state-transition notification through the full policy chain.

    Returns:
      - None                     when the transition is silent (message_type_for
                                  returned None)
      - {"delivered": bool,
         "result": str,
         "notification_id": int,
         "reason": str}          when we attempted to dispatch (may be
                                  suppressed / dry_run / delivered / failed)
    """
    extra = extra or {}
    if settings_override is not None:
        settings = settings_override
    else:
        from config import settings as _s
        settings = _s
    now = now or datetime.now(timezone.utc)

    # ── 1. Silent transitions short-circuit ────────────────────────────────
    msg_type = message_type_for(from_state, to_state)
    if msg_type is None:
        return None

    # ── 2. Determine suppression reason (if any) ───────────────────────────
    suppression = None

    muted = _resolve_muted(signal.strategy_id, settings)
    if muted:
        suppression = muted
    elif msg_type not in _ALWAYS_ON_TYPES and _in_quiet_hours(now):
        suppression = "quiet_hours_eat"
    else:
        threshold = _resolve_threshold(msg_type, settings)
        if signal.confidence < threshold:
            suppression = f"below_threshold:{msg_type}<{threshold}"

    # Shadow mode is an overlay on top of everything else — an explicit
    # "audit-only" pass. The client persists the row with suppressed reason.
    if force_dry_run:
        suppression = suppression or "shadow_mode_dry_run"

    # ── 3. Render the template ─────────────────────────────────────────────
    mode = _resolve_mode(signal.strategy_id, settings)
    try:
        payload = render_template(msg_type, signal, extra=extra, mode=mode, now=now)
    except Exception as exc:
        log.warning("[router] render failed for %s/%s: %s", signal.strategy_id, msg_type, exc)
        return {"delivered": False, "result": "render_error",
                "notification_id": None, "reason": str(exc)}

    # ── 4. Delegate to client ──────────────────────────────────────────────
    c = client or get_client()
    try:
        return c.send_notification(
            db,
            signal_id=signal.signal_id,
            strategy_id=signal.strategy_id,
            from_state=from_state,
            to_state=to_state,
            payload=payload,
            chat_id=chat_id,
            suppression_reason=suppression,
        )
    except Exception as exc:
        log.warning("[router] client send failed: %s", exc)
        return {"delivered": False, "result": "client_error",
                "notification_id": None, "reason": str(exc)}


__all__ = ["route"]
