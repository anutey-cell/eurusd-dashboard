"""
XAUUSD Hourly Market Briefing
=============================

Produces a once-per-hour structured market briefing for Telegram. Mandate-style
plain-text + emojis. Pulls from the SAME strategist verdict the rest of the
system uses, so what the briefing says matches what the dashboard / signal
alerts say — no two sources of truth.

The briefing covers:
  • Market movement     — current price, 24h change, intraday range
  • Market state        — current mandate classification + session
  • Setup status        — N/5 conditions, what's blocking, what would unlock
  • Key price levels    — supply / demand zones, prev-day H/L, round numbers
  • Macro snapshot      — DXY / yields / news risk
  • Opportunity watch   — specific BUY / SELL next-triggers
  • Risk note           — news windows in the next 60 min

Triggered by background_scheduler._hourly_briefing_loop() — fires on the hour
mark (NN:00 UTC). Operator opt-in via settings.telegram_hourly_briefing.
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
    Compose the hourly briefing message. Returns the formatted text, or None
    if there's no fresh data (rare — strategist normally degrades to STAND ASIDE
    rather than failing). Never raises.
    """
    try:
        verdict = _pull_strategist_verdict(db)
        market  = _pull_market_snapshot()
        return _format_message(verdict=verdict, market=market)
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
    """24h price movement + intraday range + current tick."""
    from data.candles import get_candles
    try:
        h1 = get_candles(interval="H1", limit=30, pair="xauusd")
        d1 = get_candles(interval="D1", limit=5,  pair="xauusd")
    except Exception as exc:
        log.warning("[briefing] candle fetch failed: %s", exc)
        return {"current": None, "move_24h": None, "move_24h_pct": None,
                "session_high": None, "session_low": None}

    if not h1 or not h1.candles:
        return {"current": None}

    candles = h1.candles
    current = candles[-1].close

    # 24-bar move on H1 = previous calendar-day's same-hour close
    if len(candles) >= 25:
        yesterday = candles[-25].close
    else:
        yesterday = candles[0].close
    move      = current - yesterday
    move_pct  = (move / yesterday) * 100 if yesterday else 0.0

    # Today's intraday range
    now_utc = datetime.now(timezone.utc)
    today_bars = [c for c in candles
                  if (c.time if c.time.tzinfo else c.time.replace(tzinfo=timezone.utc))
                     .astimezone(timezone.utc).date() == now_utc.date()]
    session_high = max((c.high for c in today_bars), default=None)
    session_low  = min((c.low  for c in today_bars), default=None)

    # Daily open (today's first H1 open)
    daily_open = today_bars[0].open if today_bars else None

    # 5-day H/L for the wider context line
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

def _format_message(*, verdict: dict, market: dict) -> str:
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
    mc = verdict.get("macro_context") or {}
    dxy  = mc.get("dxy_bias",  "—")
    yld  = mc.get("yields_bias", "—")
    gold_bias = mc.get("gold_macro_bias", "—")
    news = mc.get("news_risk", "—")
    news_icon = "🟢" if news == "CLEAR" else "🟠"

    # ── OPPORTUNITY WATCH ──────────────────────────────────────────────
    nt = verdict.get("next_trigger") or {}
    long_trigger  = (nt.get("long_trigger")  or "wait for confirmation")[:110]
    short_trigger = (nt.get("short_trigger") or "wait for confirmation")[:110]
    blockers      = (verdict.get("improvement_note") or verdict.get("stand_aside_reason") or "—")[:140]

    # ── COMPOSE MESSAGE ────────────────────────────────────────────────
    msg = (
        f"🕐 XAUUSD HOURLY BRIEFING\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"📅 {ts}\n"
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
        f"\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"DEMO learning only · 0.01 lot · capital preservation first."
    )
    return msg


# ────────────────────────────────────────────────────────────────────────────
# Telegram send + dedupe (one briefing per hour, deterministic timing)
# ────────────────────────────────────────────────────────────────────────────

# Module-level: track which hour-of-day we've already briefed today
_last_briefing_hour_key: str = ""   # "YYYY-MM-DD-HH"


def send_briefing_if_due(db: Session) -> bool:
    """
    Called by the scheduler loop. Sends the briefing if we haven't already
    sent one for the current hour. Returns True if a message was sent.
    """
    global _last_briefing_hour_key
    from config import settings

    if not getattr(settings, "telegram_hourly_briefing", False):
        return False

    now = datetime.now(timezone.utc)
    hour_key = now.strftime("%Y-%m-%d-%H")
    if hour_key == _last_briefing_hour_key:
        return False    # already briefed this hour

    msg = build_briefing(db)
    if not msg:
        return False

    if _send_plain(msg):
        _last_briefing_hour_key = hour_key
        log.info("[briefing] sent for %s", hour_key)
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
