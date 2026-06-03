"""
XAUUSD Daily Market Briefing
============================

Produces a once-per-day structured market briefing for Telegram, fired at
23:00 EAT (20:00 UTC) — end of the New York session, before Asian open.
Mandate-style plain-text + emojis. Pulls from the SAME strategist verdict
the rest of the system uses, so what the briefing says matches what the
dashboard / signal alerts say — no two sources of truth.

The briefing covers:
  • Market movement     — current price, 24h change, intraday range
  • Market state        — current mandate classification + session
  • Setup status        — N/5 conditions, what's blocking, what would unlock
  • Key price levels    — supply / demand zones, prev-day H/L, round numbers
  • Macro snapshot      — DXY / yields / news risk
  • Opportunity watch   — specific BUY / SELL next-triggers
  • Risk note           — news windows in the next 60 min

Triggered by background_scheduler._daily_briefing_loop() — fires at
20:00 UTC daily (= 23:00 Africa/Nairobi). Suppressed on weekends (Sat
recap + Sun forecast newsletters handle that window). Operator opt-in
via settings.telegram_hourly_briefing (env var name preserved for
back-compat; gates the daily briefing now).
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy.orm import Session

log = logging.getLogger(__name__)


# ────────────────────────────────────────────────────────────────────────────
# Public entry point
# ────────────────────────────────────────────────────────────────────────────

def build_briefing(db: Session) -> Optional[str]:
    """
    Compose the daily briefing message. Returns the formatted text, or None
    if there's no fresh data (rare — strategist normally degrades to STAND ASIDE
    rather than failing). Never raises.
    """
    try:
        verdict = _pull_strategist_verdict(db)
        market  = _pull_market_snapshot()
        # Regime-stability index — gives early warning of an incoming flip
        try:
            from services.regime_stability import compute_regime_stability, format_regime_stability_block
            regime  = compute_regime_stability(db)
            regime_block = format_regime_stability_block(regime)
        except Exception as exc:
            log.warning("[briefing] regime stability failed: %s", exc)
            regime_block = None
        return _format_message(verdict=verdict, market=market, regime_block=regime_block)
    except Exception as exc:
        log.warning("[briefing] build failed: %s", exc)
        return None


# ────────────────────────────────────────────────────────────────────────────
# Data pulls
# ────────────────────────────────────────────────────────────────────────────

def _pull_strategist_verdict(db: Session) -> dict:
    """Use the cached/freshly-computed mandate verdict — single source of truth."""
    from services.strategist import make_decision
    return make_decision(db)


def _pull_market_snapshot() -> dict:
    """
    24h price movement + intraday range + freshest current price.

    Uses M5 for `current` (max 5-min stale) so the briefing matches what the
    dashboard ticker shows. Uses H1 for 24h delta (24 H1 bars = 24h). Uses
    D1 for the 5-day H/L context line.
    """
    from data.candles import get_candles
    try:
        m5 = get_candles(interval="M5", limit=300, pair="xauusd")   # 25h on M5
        h1 = get_candles(interval="H1", limit=30,  pair="xauusd")
        d1 = get_candles(interval="D1", limit=5,   pair="xauusd")
    except Exception as exc:
        log.warning("[briefing] candle fetch failed: %s", exc)
        return {"current": None, "move_24h": None, "move_24h_pct": None,
                "session_high": None, "session_low": None}

    if not m5 or not m5.candles:
        return {"current": None}

    # Freshest price = last M5 close (matches the dashboard header ticker)
    current = m5.candles[-1].close

    # 24h delta — prefer H1 (24 bars = 24h); fall back to M5 first close
    if h1 and h1.candles and len(h1.candles) >= 25:
        yesterday = h1.candles[-25].close
    elif len(m5.candles) >= 289:
        yesterday = m5.candles[-289].close
    else:
        yesterday = m5.candles[0].close
    move      = current - yesterday
    move_pct  = (move / yesterday) * 100 if yesterday else 0.0

    # Today's intraday range from M5 (more accurate than H1 high/low rollups)
    now_utc = datetime.now(timezone.utc)
    today_bars = [c for c in m5.candles
                  if (c.time if c.time.tzinfo else c.time.replace(tzinfo=timezone.utc))
                     .astimezone(timezone.utc).date() == now_utc.date()]
    session_high = max((c.high for c in today_bars), default=None)
    session_low  = min((c.low  for c in today_bars), default=None)
    daily_open   = today_bars[0].open if today_bars else None

    # 5-day H/L from D1
    week_high = max(c.high for c in d1.candles) if d1 and d1.candles else None
    week_low  = min(c.low  for c in d1.candles) if d1 and d1.candles else None

    return {
        "current":      round(current, 2),
        "move_24h":     round(move, 2),
        "move_24h_pct": round(move_pct, 2),
        "session_high": round(session_high, 2) if session_high else None,
        "session_low":  round(session_low, 2)  if session_low  else None,
        "daily_open":   round(daily_open, 2)   if daily_open   else None,
        "week_high":    round(week_high, 2)    if week_high    else None,
        "week_low":     round(week_low, 2)     if week_low     else None,
    }


# ────────────────────────────────────────────────────────────────────────────
# Message formatter
# ────────────────────────────────────────────────────────────────────────────

def _format_message(*, verdict: dict, market: dict, regime_block: str | None = None) -> str:
    now = datetime.now(timezone.utc)
    ts  = now.strftime("%Y-%m-%d %H:00 GMT")

    # ── HEADER ──────────────────────────────────────────────────────────
    current = market.get("current")
    move_pct = market.get("move_24h_pct") or 0
    move_arrow = "▲" if move_pct > 0 else "▼" if move_pct < 0 else "▬"
    price_line = (
        f"💰 Price: ${current}  {move_arrow} {abs(move_pct):.2f}% 24h"
        if current is not None else "💰 Price: unavailable"
    )

    # ── INTRADAY / WEEK CONTEXT ────────────────────────────────────────
    daily_open = market.get("daily_open")
    range_line = "—"
    if market.get("session_high") and market.get("session_low"):
        rng = round(market["session_high"] - market["session_low"], 2)
        range_line = f"H ${market['session_high']}  ·  L ${market['session_low']}  ·  range ${rng}"
    week_line = "—"
    if market.get("week_high") and market.get("week_low"):
        week_line = f"5-day H ${market['week_high']}  ·  L ${market['week_low']}"

    # Position relative to daily open
    do_line = "—"
    if daily_open is not None and current is not None:
        delta = round(current - daily_open, 2)
        side = "above" if delta > 0 else "below" if delta < 0 else "at"
        do_line = f"${daily_open}  ({side} by ${abs(delta)})"

    # ── VERDICT BLOCK ──────────────────────────────────────────────────
    cp     = verdict.get("conditions_passed", 0)
    state  = verdict.get("market_state", "—")
    sess   = verdict.get("session_classification", "—")
    tf_lbl = verdict.get("tf_alignment_label", "—")
    liq    = verdict.get("liquidity_behaviour", "—")
    es     = verdict.get("execution_status", "STAND_ASIDE")
    es_reason = verdict.get("execution_status_reason", "—")
    decision = verdict.get("decision", "STAND ASIDE")
    band   = verdict.get("quality_band", "—")
    wr     = verdict.get("estimated_win_rate_range", "—")

    decision_emoji = "🟢" if decision == "BUY" else "🔴" if decision == "SELL" else "⚪"

    # ── KEY LEVELS ─────────────────────────────────────────────────────
    kz = verdict.get("key_zones") or {}
    resistance = (kz.get("resistance") or []) + (kz.get("immediate_supply") or [])
    support    = (kz.get("support")    or []) + (kz.get("immediate_demand") or [])
    rn         = kz.get("round_numbers") or []

    def _fmt_levels(levels: list, max_n: int = 3) -> str:
        if not levels: return "—"
        uniq = sorted({round(float(x), 2) for x in levels if x is not None})
        return "  ·  ".join(f"${x}" for x in uniq[:max_n])

    # ── MACRO ──────────────────────────────────────────────────────────
    # Predictor's `direction` field is the implied signal for GOLD, not DXY's
    # own direction. Translate to plain-English for the briefing reader.
    mc = verdict.get("macro_context") or {}
    dxy_raw  = mc.get("dxy_bias",  "NEUTRAL")
    yld_raw  = mc.get("yields_bias", "NEUTRAL")
    gold_bias = mc.get("gold_macro_bias", "—")
    news = mc.get("news_risk", "—")
    news_icon = "🟢" if news == "CLEAR" else "🟠"

    def _macro_label(d: str, factor: str) -> str:
        """Translate predictor direction → human-readable narrative."""
        if d == "BUY":    return f"{factor} supportive of gold"
        if d == "SELL":   return f"{factor} pressuring gold"
        return f"{factor} neutral"

    dxy  = _macro_label(dxy_raw, "DXY")
    yld  = _macro_label(yld_raw, "Yields")

    # ── OPPORTUNITY WATCH ──────────────────────────────────────────────
    nt = verdict.get("next_trigger") or {}
    long_trigger  = (nt.get("long_trigger")  or "wait for confirmation")[:110]
    short_trigger = (nt.get("short_trigger") or "wait for confirmation")[:110]
    blockers      = (verdict.get("improvement_note") or verdict.get("stand_aside_reason") or "—")[:140]

    # ── COMPOSE MESSAGE ────────────────────────────────────────────────
    msg = (
        f"🕘 XAUUSD DAILY BRIEFING\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"📅 {ts}  (23:00 EAT close)\n"
        f"{price_line}\n"
        f"📊 Today: {range_line}\n"
        f"📈 vs Daily Open: {do_line}\n"
        f"📆 Week:  {week_line}\n"
        f"\n"
        f"🎯 VERDICT\n"
        f"{decision_emoji} {decision}  ·  {cp}/5 conditions  ·  est WR {wr}\n"
        f"Execution: {es}\n"
        f"Reason:    {es_reason}\n"
        f"State:     {state}\n"
        f"Session:   {sess}\n"
        f"TF align:  {tf_lbl}\n"
        f"Liquidity: {liq}\n"
        f"\n"
        f"📌 KEY LEVELS\n"
        f"🔴 Resistance: {_fmt_levels(resistance)}\n"
        f"🟢 Support:    {_fmt_levels(support)}\n"
        f"🎯 Round nums: {_fmt_levels(rn, max_n=4)}\n"
        f"\n"
        f"📊 MACRO\n"
        f"DXY:    {dxy}\n"
        f"Yields: {yld}\n"
        f"Gold:   {gold_bias}\n"
        f"News:   {news_icon} {news}\n"
        f"\n"
        f"⚡ OPPORTUNITY WATCH\n"
        f"🟢 BUY unlock:  {long_trigger}\n"
        f"🔴 SELL unlock: {short_trigger}\n"
        f"Blockers: {blockers}\n"
    )

    # Regime-stability index — appended before footer when available
    if regime_block:
        msg += f"\n{regime_block}\n"

    msg += (
        f"\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"DEMO learning only · 0.01 lot · capital preservation first."
    )
    return msg


# ────────────────────────────────────────────────────────────────────────────
# Telegram send + dedupe (one briefing per hour, deterministic timing)
# ────────────────────────────────────────────────────────────────────────────

# Module-level: track which calendar date we've already briefed
_last_briefing_date_key: str = ""   # "YYYY-MM-DD"


def send_briefing_if_due(db: Session) -> bool:
    """
    Called by the scheduler loop. Sends the daily briefing if we haven't
    already sent one for today. Returns True if a message was sent.
    """
    global _last_briefing_date_key
    from config import settings
    from services.strategist_runner import is_weekend_quiet_hours

    if not getattr(settings, "telegram_hourly_briefing", False):
        return False

    # Weekend gate — daily briefing pauses; Sat recap + Sun forecast take over.
    if is_weekend_quiet_hours():
        log.debug("[briefing] suppressed — weekend quiet hours")
        return False

    now = datetime.now(timezone.utc)
    date_key = now.strftime("%Y-%m-%d")
    if date_key == _last_briefing_date_key:
        return False    # already briefed today

    msg = build_briefing(db)
    if not msg:
        return False

    if _send_plain(msg):
        _last_briefing_date_key = date_key
        log.info("[briefing] sent daily briefing for %s", date_key)
        return True
    return False


def _send_plain(text: str) -> bool:
    """Send plain-text Telegram (no HTML — preserve mandate emoji layout)."""
    try:
        import httpx
        from config import settings
        if not (settings.telegram_bot_token and settings.telegram_chat_id):
            log.debug("[briefing] Telegram credentials missing — skip send")
            return False
        url = f"https://api.telegram.org/bot{settings.telegram_bot_token}/sendMessage"
        resp = httpx.post(url, json={
            "chat_id":                  settings.telegram_chat_id,
            "text":                     text,
            "disable_web_page_preview": True,
        }, timeout=15.0)
        if not resp.is_success:
            log.warning("[briefing] Telegram send failed status=%s", resp.status_code)
            return False
        return True
    except Exception as exc:
        log.warning("[briefing] Telegram send error: %s", exc)
        return False
