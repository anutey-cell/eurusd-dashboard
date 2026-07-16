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

    # Side-effects — order matters: enqueue first so the log can back-link it
    pending_id = _maybe_enqueue_demo_order(db, verdict)
    _maybe_fire_alert(verdict)
    _maybe_fire_preformation_alert(verdict)   # 1-hour-before-KZ heads-up
    _maybe_fire_momentum_alert(db, verdict)   # secondary momentum-continuation source

    try:
        persist_verdict(db, verdict, pending_execution_id=pending_id)
    except Exception as exc:
        log.debug("[strategist_runner] persistence skipped: %s", exc)

    return verdict


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

        # ── Fire ───────────────────────────────────────────────────────
        long_pct, short_pct = _fetch_sentiment()
        msg = format_mandate_signal_message(verdict, long_pct=long_pct, short_pct=short_pct)
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
        f"🔒 XAUUSD POSITION CAP REACHED ({open_n}/{max_n})\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"Engine wanted: {arrow} {decision}  ·  {cp}/5 conditions\n"
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
    when we're within the first 5 minutes of the trigger hour. Dedupe by
    (kz_name, YYYY-MM-DD).
    """
    global _last_preformation_alerts

    if not getattr(settings, "telegram_preformation_alerts", True):
        return
    if is_weekend_quiet_hours():
        return

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
            log.info("[strategist_runner] pre-formation alert fired for %s", cfg["kz_name"])
        except Exception as exc:
            log.warning("[strategist_runner] pre-formation alert failed for %s: %s",
                        cfg["kz_name"], exc)


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


def _send_plain(text: str) -> None:
    """Send Telegram in plain-text mode so emojis + dashes render exactly."""
    try:
        import httpx
        if not (settings.telegram_bot_token and settings.telegram_chat_id):
            return
        url = f"https://api.telegram.org/bot{settings.telegram_bot_token}/sendMessage"
        resp = httpx.post(url, json={
            "chat_id":                  settings.telegram_chat_id,
            "text":                     text,
            "disable_web_page_preview": True,
        }, timeout=10.0)
        if not resp.is_success:
            log.warning("[strategist_runner] Telegram send failed status=%s",
                        resp.status_code)
    except Exception as exc:
        log.warning("[strategist_runner] Telegram plain send error: %s", exc)


# ── MT5 enqueue side-effect ──────────────────────────────────────────────────

def _maybe_enqueue_demo_order(db: Session, verdict: dict) -> int | None:
    """
    Insert PendingExecution(lot=0.01, max_lot=0.01) when all mandate gates pass.
    Returns the row id, or None if nothing was enqueued.
    """
    global _last_enqueue_fingerprint, _last_enqueue_at

    if verdict.get("execution_status") != "DEMO_TRADE_PLACED":
        return None
    mt5_obj = verdict.get("mt5_execution_object") or {}
    if not mt5_obj:
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

    # Hard mandate guards
    if mt5_obj.get("lot") != 0.01:
        log.error("[strategist_runner] refused to enqueue: lot != 0.01 (%s)", mt5_obj.get("lot"))
        return None
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
            max_lot=0.01,
            reason=(
                f"strategist mandate · {mt5_obj['conditions_passed']}/5 conditions"
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
                "mt5_execution_object":  mt5_obj,
            }),
            expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
            status="PENDING",
        )
        db.add(row)
        db.commit()
        db.refresh(row)
        _last_enqueue_fingerprint = fp
        _last_enqueue_at          = time.time()
        log.info(
            "[strategist_runner] ENQUEUED #%d %s xauusd lot=0.01 entry=%s SL=%s TP1=%s TP2=%s",
            row.id, mt5_obj["action"], mt5_obj["entry"], mt5_obj["stop_loss"],
            mt5_obj["take_profit_1"], mt5_obj["take_profit_2"],
        )
        return row.id
    except Exception as exc:
        log.warning("[strategist_runner] enqueue failed (non-fatal): %s", exc)
        try: db.rollback()
        except Exception: pass
        return None
