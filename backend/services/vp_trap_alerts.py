"""
VP Trap Alert Dispatch — Phase 4
================================

Fires Telegram alerts when a TRIGGERED zone scores at/above the live
threshold (or countertrend-adjusted threshold). Writes VpTrapSignal
rows for the audit trail.

Signal-only. Never enqueues MT5 orders (that's `vp_trap_auto_execute`, a
future gate; default OFF for the operator's stated validation period).

Dedupe (per brief):
  - One signal per (zone_id, direction) — no re-firing on the same zone
  - Fingerprint = SHA(instrument | zone_id | direction | date)[:16]
  - Cooldown per zone_id after alert (default 30 min) — allows re-arm if
    the zone genuinely re-triggers post-cooldown

Weekend / Monday-observation gates respected. Alerts marked clearly as
🪤 VP TRAP so operator distinguishes from mandate (🎯) and momentum (⚡).
"""
from __future__ import annotations

import hashlib
import json
import logging
import time
from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy.orm import Session

from config import settings

log = logging.getLogger(__name__)

# Module-level dedupe state — { zone_id: {"fingerprint": str, "sent_at": float} }
_last_alerts: dict[str, dict] = {}


# ── Fingerprint helper ──────────────────────────────────────────────────────

def _make_fingerprint(zone_id: str, direction: str, level_type: str,
                      instrument: str = "XAUUSD") -> str:
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    raw = f"{instrument}|{zone_id}|{direction}|{level_type}|{today}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


# ── Cooldown check ─────────────────────────────────────────────────────────

def _within_cooldown(zone_id: str, fingerprint: str, cooldown_s: int) -> tuple[bool, str]:
    """
    True if we should skip this alert due to cooldown or fingerprint match.

    Returns (should_skip, reason).
    """
    rec = _last_alerts.get(zone_id)
    if not rec:
        return (False, "")
    if rec.get("fingerprint") == fingerprint:
        return (True, "same fingerprint (already alerted)")
    elapsed = time.time() - rec.get("sent_at", 0)
    if elapsed < cooldown_s:
        return (True, f"cooldown {elapsed:.0f}s < {cooldown_s}s")
    return (False, "")


# ── Confluence check with mandate + momentum ───────────────────────────────

def _check_confluence(direction: str) -> dict:
    """
    Snapshot what the mandate strategist and momentum engine currently say.
    Used to enrich the alert message so operator can distinguish agree vs
    disagree without hopping between screens.

    Never raises; degrades to unknowns on any failure.
    """
    out = {"mandate_agrees": False, "mandate_says": "unknown",
           "momentum_agrees": False, "momentum_says": "unknown"}
    try:
        from routers import strategist as st_router
        cache = getattr(st_router, "_cache", None)
        if cache and cache.get("verdict"):
            v = cache["verdict"] or {}
            mandate_dir = (v.get("decision") or "").upper()
            out["mandate_says"] = mandate_dir
            out["mandate_agrees"] = (mandate_dir == direction)
    except Exception:
        pass
    try:
        from services import strategist_runner as sr
        last_mom = getattr(sr, "_last_momentum_alert", {}) or {}
        mom_dir = (last_mom.get("direction") or "").upper()
        if mom_dir:
            out["momentum_says"] = mom_dir
            out["momentum_agrees"] = (mom_dir == direction)
    except Exception:
        pass
    return out


# ── Telegram formatter ─────────────────────────────────────────────────────

def _format_alert(*, zone: dict, breakdown: dict, plan: dict,
                  profile: dict, confluence: dict) -> str:
    direction = zone.get("level_side", "?")
    icon      = "🟢" if direction == "BUY" else "🔴"
    band_emoji = {
        "EXCEPTIONAL": "🌟",
        "VALID":       "✨",
        "DEVELOPING":  "•",
        "WATCH":       "·",
    }.get(breakdown.get("band", ""), "")

    trap_side = "Trapped buyers (SELL back)" if direction == "SELL" else "Trapped sellers (BUY back)"

    entry = plan.get("entry")
    sl    = plan.get("sl")
    tp1   = plan.get("tp1")
    tp2   = plan.get("tp2")
    tp3   = plan.get("tp3")
    rr    = plan.get("rr", 0)

    # Compute pt-distances + R-multiples for TPs
    def _rr_of(tp):
        if entry is None or sl is None or tp is None:
            return "—"
        risk = abs(entry - sl)
        if risk <= 0:
            return "—"
        reward = abs(tp - entry)
        return f"{reward/risk:.1f}R"

    def _pt_of(tp):
        if entry is None or tp is None:
            return "—"
        return f"{abs(tp - entry):.1f}pt"

    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    # Confluence block
    mandate_line  = f"Mandate:   {confluence.get('mandate_says', '?')}"
    if confluence.get("mandate_agrees"):
        mandate_line += "  ✓ agrees"
    else:
        mandate_line += "  · differs"
    momentum_line = f"Momentum:  {confluence.get('momentum_says', 'silent')}"
    if confluence.get("momentum_agrees"):
        momentum_line += "  ✓ agrees"

    # Reasons from the scoring breakdown
    met = breakdown.get("conditions_met") or []
    missing = breakdown.get("conditions_missing") or []
    met_lines = "\n".join(f"  ✓ {m}" for m in met[:5]) if met else "  (none)"
    missing_lines = "\n".join(f"  ✗ {m}" for m in missing[:3]) if missing else ""

    ct_marker = "  · COUNTERTREND" if breakdown.get("is_countertrend") else ""

    msg = (
        f"🪤 VP TRAP — {direction}\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"{icon} {band_emoji} {breakdown.get('band', '')}  ·  score "
        f"{breakdown.get('total', 0)}/100{ct_marker}\n"
        f"\n"
        f"Setup:      {zone.get('level_type', '?')} failed breakout @ "
        f"${zone.get('reference_price', 0):.2f}\n"
        f"Trap side:  {trap_side}\n"
        f"Displacement: {zone.get('displacement_pts', 0):.1f} pts post-reclaim\n"
        f"\n"
        f"Entry:      ${entry:.2f}\n" if entry else "Entry:      —\n"
    )
    msg += (
        f"SL:         ${sl:.2f}\n" if sl else "SL:         —\n"
    )
    msg += (
        f"TP1 (POC):  ${tp1:.2f}  ·  {_pt_of(tp1)}  ·  {_rr_of(tp1)}\n" if tp1 else ""
    )
    msg += (
        f"TP2 (VA):   ${tp2:.2f}  ·  {_pt_of(tp2)}  ·  {_rr_of(tp2)}\n" if tp2 else ""
    )
    if tp3:
        msg += f"TP3:        ${tp3:.2f}  ·  {_pt_of(tp3)}  ·  {_rr_of(tp3)}\n"

    msg += (
        f"\n"
        f"Conditions met:\n"
        f"{met_lines}\n"
    )
    if missing_lines:
        msg += (
            f"\n"
            f"Conditions missing:\n"
            f"{missing_lines}\n"
        )
    msg += (
        f"\n"
        f"Confluence:\n"
        f"  {mandate_line}\n"
        f"  {momentum_line}\n"
        f"\n"
        f"Profile date:  {profile.get('profile_date', '?')}\n"
        f"POC ${profile.get('poc', 0):.2f}  VAH ${profile.get('vah', 0):.2f}"
        f"  VAL ${profile.get('val', 0):.2f}\n"
        f"⏰ Detected: {now}\n"
        f"\n"
        f"⚠ Signal-only. No MT5 execution.\n"
        f"    Volume source: {zone.get('volume_source', 'tick_proxy')}\n"
    )
    return msg


# ── VpTrapSignal persistence ───────────────────────────────────────────────

def _persist_signal(db: Session, *, zone: dict, breakdown: dict, plan: dict,
                    profile: dict, confluence: dict, fingerprint: str) -> Optional[int]:
    """Write the VpTrapSignal row. Returns the new row id, or None on failure."""
    from db_models import VpTrapSignal as SM
    from db_models import VpTrapZone as ZM
    from datetime import timedelta

    # Look up zone_row_id for soft link
    zone_row = db.query(ZM).filter(ZM.zone_id == zone.get("zone_id", "")).one_or_none()

    row = SM(
        instrument="XAU/USD",
        zone_id=zone.get("zone_id", ""),
        zone_row_id=zone_row.id if zone_row else None,
        signal=zone.get("level_side", ""),
        entry=plan.get("entry") or 0.0,
        stop_loss=plan.get("sl") or 0.0,
        tp1=plan.get("tp1"),
        tp2=plan.get("tp2"),
        tp3=plan.get("tp3"),
        rr=plan.get("rr"),
        risk_points=abs((plan.get("entry") or 0) - (plan.get("sl") or 0)) or None,
        score_total=breakdown.get("total", 0),
        score_breakdown_json=json.dumps(breakdown),
        trap_side=("trapped_buyers" if zone.get("level_side") == "SELL"
                   else "trapped_sellers"),
        setup_type=f"{zone.get('level_type', '')}_fail",
        session=None,
        market_regime=None,
        htf_context=None,
        is_countertrend=breakdown.get("is_countertrend", False),
        volume_source=zone.get("volume_source", "tick_proxy"),
        mandate_agrees=confluence.get("mandate_agrees", False),
        momentum_agrees=confluence.get("momentum_agrees", False),
        liquidity_map_agrees=False,     # TODO Phase 5
        fingerprint=fingerprint,
        state="ALERTED",
        expires_at=datetime.now(timezone.utc) + timedelta(hours=24),
        reason_qualifies=breakdown.get("reason_qualifies", "")[:2000],
        conditions_met_json=json.dumps(breakdown.get("conditions_met", [])),
        conditions_missing_json=json.dumps(breakdown.get("conditions_missing", [])),
    )
    try:
        db.add(row)
        db.commit()
        return row.id
    except Exception as exc:
        log.warning("[vp_trap_alerts] persist failed: %s", exc)
        db.rollback()
        return None


# ── Telegram send ──────────────────────────────────────────────────────────

def _send_plain(text: str) -> bool:
    try:
        import httpx
        if not (settings.telegram_bot_token and settings.telegram_chat_id):
            log.debug("[vp_trap_alerts] Telegram credentials missing — skip")
            return False
        url = f"https://api.telegram.org/bot{settings.telegram_bot_token}/sendMessage"
        resp = httpx.post(url, json={
            "chat_id":                  settings.telegram_chat_id,
            "text":                     text,
            "disable_web_page_preview": True,
        }, timeout=15.0)
        if not resp.is_success:
            log.warning("[vp_trap_alerts] Telegram send failed status=%s", resp.status_code)
            return False
        return True
    except Exception as exc:
        log.warning("[vp_trap_alerts] Telegram send error: %s", exc)
        return False


# ── Public entry point ─────────────────────────────────────────────────────

def _recent_mandate_alert_direction(within_seconds: int = 300) -> Optional[str]:
    """Returns the mandate's most-recent alert direction if fired within the
    window. Used by confluence mode to detect same-direction agreement between
    mandate and vp_trap. Reads strategist_runner's module state (no DB query).
    """
    try:
        from services import strategist_runner as sr
        rec = getattr(sr, "_last_alert", None) or {}
        elapsed = time.time() - (rec.get("sent_at") or 0)
        if elapsed <= within_seconds:
            d = rec.get("decision")
            if d in ("BUY", "SELL"):
                return d
    except Exception:
        pass
    return None


def _format_consolidated_alert(*, zone: dict, breakdown: dict, plan: dict,
                                profile: dict, mandate_direction: str) -> str:
    """
    Consolidated alert format for CONFLUENCE mode when mandate AND vp_trap
    agree on direction. One message instead of two — clearly labelled so the
    operator sees the confluence at a glance.
    """
    direction = zone.get("level_side", "?")
    icon      = "🟢" if direction == "BUY" else "🔴"
    band_emoji = {
        "EXCEPTIONAL": "🌟", "VALID": "✨",
        "DEVELOPING": "•", "WATCH": "·",
    }.get(breakdown.get("band", ""), "")

    trap_side = "Trapped buyers" if direction == "SELL" else "Trapped sellers"
    entry = plan.get("entry"); sl = plan.get("sl")
    tp1 = plan.get("tp1"); tp2 = plan.get("tp2"); rr = plan.get("rr", 0)

    # Pull mandate's cached conditions_passed for the confluence header
    mandate_cp = "?"
    try:
        from routers import strategist as st_router
        cache = getattr(st_router, "_cache", None) or {}
        v = cache.get("verdict") or {}
        mandate_cp = f"{v.get('conditions_passed', '?')}/5"
    except Exception:
        pass

    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    return (
        f"🎯🪤 MANDATE + VP TRAP AGREE — {direction}\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"{icon} HIGHEST CONVICTION  ·  both engines aligned\n"
        f"\n"
        f"Mandate:  {mandate_direction}  ·  {mandate_cp} conditions\n"
        f"VP Trap:  {band_emoji} {breakdown.get('band', '')}  ·  "
        f"score {breakdown.get('total', 0)}/100\n"
        f"\n"
        f"Setup:      {zone.get('level_type', '')} failed breakout @ "
        f"${zone.get('reference_price', 0):.2f}\n"
        f"Trap side:  {trap_side}\n"
        f"Entry:      ${entry:.2f}\n"
        f"SL:         ${sl:.2f}\n"
        f"TP1 (POC):  ${tp1:.2f}\n"
        f"TP2 (VA):   ${tp2:.2f}  ·  RR {rr:.1f}\n"
        f"\n"
        f"⚠ Signal-only. No MT5 execution.\n"
        f"⏰ Detected: {now}\n"
    )


def get_vp_trap_context_for_mandate(direction: str, db: Session) -> Optional[dict]:
    """
    Confirmation mode helper: called from the mandate alert path. Returns
    the current best-matching VP Trap zone context (if any) so the mandate
    alert can append a "🪤 VP TRAP context" section. Returns None if no
    active zone matches the mandate's direction, or if the mode isn't set
    for confirmation/confluence.

    Never raises. Fast DB read (one row).
    """
    if direction not in ("BUY", "SELL"):
        return None
    mode = getattr(settings, "vp_trap_mode", "independent")
    if mode not in ("confirmation", "confluence"):
        return None

    try:
        from db_models import VpTrapZone as ZM
        # Only zones that MATCH mandate direction (both are BUY, or both SELL)
        # Prefer zones with state in {RETEST_ACTIVE, TRIGGERED, WAITING_RETEST}
        # ordered by how "hot" they are
        active_states = ("TRIGGERED", "RETEST_ACTIVE", "WAITING_RETEST", "TRAP_ARMED")
        row = (db.query(ZM)
                 .filter(ZM.instrument == "XAU/USD")
                 .filter(ZM.level_side == direction)
                 .filter(ZM.state.in_(active_states))
                 .order_by(ZM.updated_at.desc())
                 .first())
        if not row:
            return None
        return {
            "zone_id":         row.zone_id,
            "level_type":      row.level_type,
            "reference_price": row.reference_price,
            "state":           row.state,
            "state_reason":    row.state_reason or "",
            "displacement_pts": row.displacement_pts or 0,
            "retest_count":    row.retest_count or 0,
        }
    except Exception as exc:
        log.debug("[vp_trap_alerts] context lookup failed: %s", exc)
        return None


def format_vp_trap_context_line(ctx: dict) -> str:
    """Short human-readable line for embedding in a mandate Telegram alert."""
    if not ctx:
        return ""
    lt   = ctx.get("level_type", "?")
    ref  = ctx.get("reference_price", 0)
    st   = ctx.get("state", "?")
    disp = ctx.get("displacement_pts") or 0
    retests = ctx.get("retest_count") or 0
    parts = [f"{lt} @ ${ref:.2f}", st.replace("_", " ")]
    if disp:
        parts.append(f"disp {disp:.0f}pt")
    if retests:
        parts.append(f"{retests}x retest")
    return " · ".join(parts)


def maybe_dispatch_alert(
    db: Session,
    zone: dict, breakdown: dict, plan: dict, profile: dict,
) -> Optional[int]:
    """
    Called by scan_and_persist_zones() when a TRIGGERED zone's score meets
    breakdown['would_fire']. Handles dedupe, cooldown, weekend + Monday
    observation gates, formats the alert, sends Telegram, and persists the
    VpTrapSignal row.

    Mode-aware:
      independent (default) — send own 🪤 alert
      confirmation           — persist signal, DO NOT send own alert; the
                                mandate alert path will read our context
                                via get_vp_trap_context_for_mandate()
      confluence             — check for recent mandate alert same direction;
                                if match, send CONSOLIDATED 🎯🪤 alert;
                                if no match, send normal 🪤 alert

    Returns the persisted signal row id, or None if no signal was persisted.
    """
    if not getattr(settings, "vp_trap_telegram_alerts", True):
        return None
    if not breakdown.get("would_fire"):
        return None

    zone_id = zone.get("zone_id", "")
    direction = zone.get("level_side", "")
    level_type = zone.get("level_type", "")
    if not zone_id or not direction:
        return None

    # Weekend + Monday-observation respect (share the strategist_runner gates)
    try:
        from services.strategist_runner import is_weekend_quiet_hours
        if is_weekend_quiet_hours():
            log.debug("[vp_trap_alerts] suppressed — weekend quiet hours")
            return None
    except Exception:
        pass

    fingerprint = _make_fingerprint(zone_id, direction, level_type)
    cooldown_s = getattr(settings, "vp_trap_alert_cooldown_s", 1800)
    skip, reason = _within_cooldown(zone_id, fingerprint, cooldown_s)
    if skip:
        log.debug("[vp_trap_alerts] skip zone %s: %s", zone_id[:8], reason)
        return None

    confluence = _check_confluence(direction)
    mode = getattr(settings, "vp_trap_mode", "independent")

    # ── Confirmation mode: no Telegram alert; persist for audit only ───
    if mode == "confirmation":
        signal_id = _persist_signal(db, zone=zone, breakdown=breakdown, plan=plan,
                                     profile=profile, confluence=confluence,
                                     fingerprint=fingerprint)
        _last_alerts[zone_id] = {
            "fingerprint": fingerprint, "sent_at": time.time(),
        }
        log.info("[vp_trap_alerts] CONFIRMATION mode — persisted signal %d "
                 "for %s %s (no own alert)", signal_id or -1, direction, level_type)
        return signal_id

    # ── Confluence mode: check for mandate agreement, consolidate if match ─
    if mode == "confluence":
        mandate_dir = _recent_mandate_alert_direction(within_seconds=300)
        if mandate_dir == direction:
            text = _format_consolidated_alert(
                zone=zone, breakdown=breakdown, plan=plan,
                profile=profile, mandate_direction=mandate_dir,
            )
            if not _send_plain(text):
                return None
            signal_id = _persist_signal(db, zone=zone, breakdown=breakdown, plan=plan,
                                         profile=profile, confluence=confluence,
                                         fingerprint=fingerprint)
            _last_alerts[zone_id] = {
                "fingerprint": fingerprint, "sent_at": time.time(),
            }
            log.info("[vp_trap_alerts] CONFLUENCE consolidated alert fired "
                     "%s (mandate agrees) score=%d id=%s",
                     direction, breakdown.get("total", 0), signal_id)
            return signal_id
        # Confluence mode but no mandate agreement — fall through to independent

    # ── Independent mode (default) OR confluence with no mandate match ──
    text = _format_alert(zone=zone, breakdown=breakdown, plan=plan,
                         profile=profile, confluence=confluence)
    if not _send_plain(text):
        log.warning("[vp_trap_alerts] send failed for zone %s", zone_id[:8])
        return None

    signal_id = _persist_signal(db, zone=zone, breakdown=breakdown, plan=plan,
                                 profile=profile, confluence=confluence,
                                 fingerprint=fingerprint)

    _last_alerts[zone_id] = {
        "fingerprint": fingerprint,
        "sent_at":     time.time(),
    }
    # ── P135: measurement-protocol hook ─────────────────────────────────
    # Every VP Trap live-alert becomes a row in vp_trap_measurement_events.
    # Idempotent by zone_id + 6h fired-at bucket.
    try:
        from services.vp_trap_measurement import record_signal
        record_signal(
            db,
            zone_id=zone_id,
            direction=direction,
            score=int(breakdown.get("total", 0) or 0),
            session=(zone.get("session") or plan.get("session") or "unknown"),
            entry_price=float(plan.get("entry") or 0.0),
            stop_loss=float(plan.get("stop_loss") or 0.0),
            tp1_price=(float(plan["tp1"]) if plan.get("tp1") is not None else None),
            tp2_price=(float(plan["tp2"]) if plan.get("tp2") is not None else None),
            invalidation_price=(float(plan["invalidation"])
                                 if plan.get("invalidation") is not None else None),
            trap_side=zone.get("trap_side") or level_type,
            signal_id=(str(signal_id) if signal_id else None),
            notes={"mode": mode, "level_type": level_type,
                   "breakdown_total": breakdown.get("total")},
        )
    except Exception as _m_exc:
        log.debug("[vp_trap_alerts] measurement record skipped: %s", _m_exc)

    log.info("[vp_trap_alerts] fired %s %s score=%d id=%s mode=%s",
             direction, level_type, breakdown.get("total", 0), signal_id, mode)
    return signal_id
