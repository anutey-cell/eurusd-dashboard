"""
XAUUSD Daily Market Briefing — Tomorrow's Playbook
==================================================

Fires at 23:00 EAT (20:00 UTC), end of the New York session, before Asian open.
Rewritten around the engineered-liquidity map — the operator gets tomorrow's
sniper playbook, not a state dump.

Structure (top to bottom):
  1. Header             — close price + 24h move + tomorrow's date
  2. Directional bias   — one-line HTF-driven read: BUY the pullback / SELL rallies
  3. Liquidity map      — buy-side + sell-side pools ranked by magnetism
  4. Triggers           — PRIMARY (aligned with bias) + SECONDARY (counter-bias)
                          with specific IF-THEN entry / SL / TP / RR
  5. Avoid              — news windows, bad sessions, chase-entry warnings
  6. Regime stability   — score + narrative (existing block)
  7. Footer             — DEMO / 0.01 lot disclaimer

Triggered by background_scheduler._daily_briefing_loop() at 20:00 UTC daily.
Suppressed on weekends. Operator opt-in via settings.telegram_hourly_briefing.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy.orm import Session

log = logging.getLogger(__name__)


# ────────────────────────────────────────────────────────────────────────────
# Public entry point
# ────────────────────────────────────────────────────────────────────────────

def build_briefing(db: Session) -> Optional[str]:
    """Compose the daily briefing. Returns text or None on failure. Never raises.

    The strategist verdict already carries the engineered-liquidity map as
    `verdict["liquidity_map"]`. We reconstruct a lightweight object here from
    that dict so we can call the same `render_zones_for_brief` / `sniper_playbook`
    helpers without rebuilding the map (avoiding a double-compute per cycle).
    """
    try:
        verdict = _pull_strategist_verdict(db)
        market  = _pull_market_snapshot()
        liquidity_map = _rehydrate_liquidity_map(verdict)
        try:
            from services.regime_stability import compute_regime_stability, format_regime_stability_block
            regime_block = format_regime_stability_block(compute_regime_stability(db))
        except Exception as exc:
            log.warning("[briefing] regime stability failed: %s", exc)
            regime_block = None
        return _format_message(
            verdict=verdict, market=market,
            liquidity_map=liquidity_map, regime_block=regime_block,
        )
    except Exception as exc:
        log.warning("[briefing] build failed: %s", exc)
        return None


# ────────────────────────────────────────────────────────────────────────────
# Data pulls
# ────────────────────────────────────────────────────────────────────────────

def _pull_strategist_verdict(db: Session) -> dict:
    from services.strategist import make_decision
    return make_decision(db)


def _pull_market_snapshot() -> dict:
    """Freshest price + 24h move + intraday range + 5-day H/L. Also returns
    the M15/H1/D1 candle lists themselves so the liquidity map can reuse them."""
    from data.candles import get_candles
    m5 = h1 = d1 = m15 = None
    try:
        m5  = get_candles(interval="M5",  limit=300, pair="xauusd")
        m15 = get_candles(interval="M15", limit=200, pair="xauusd")
        h1  = get_candles(interval="H1",  limit=200, pair="xauusd")
        d1  = get_candles(interval="D1",  limit=30,  pair="xauusd")
    except Exception as exc:
        log.warning("[briefing] candle fetch failed: %s", exc)

    if not m5 or not m5.candles:
        return {"current": None, "candles_m15": [], "candles_h1": [], "candles_d1": []}

    current = m5.candles[-1].close

    # 24h delta
    if h1 and h1.candles and len(h1.candles) >= 25:
        yesterday = h1.candles[-25].close
    else:
        yesterday = m5.candles[0].close
    move     = current - yesterday
    move_pct = (move / yesterday) * 100 if yesterday else 0.0

    # Today's intraday range
    now_utc = datetime.now(timezone.utc)
    today_bars = [c for c in m5.candles
                  if (c.time if c.time.tzinfo else c.time.replace(tzinfo=timezone.utc))
                     .astimezone(timezone.utc).date() == now_utc.date()]
    session_high = max((c.high for c in today_bars), default=None)
    session_low  = min((c.low  for c in today_bars), default=None)
    daily_open   = today_bars[0].open if today_bars else None

    return {
        "current":       round(current, 2),
        "move_24h_pct":  round(move_pct, 2),
        "session_high":  round(session_high, 2) if session_high else None,
        "session_low":   round(session_low,  2) if session_low  else None,
        "daily_open":    round(daily_open,   2) if daily_open   else None,
        # Retain candle lists for the liquidity map builder
        "candles_m15":   m15.candles if m15 else [],
        "candles_h1":    h1.candles  if h1  else [],
        "candles_d1":    d1.candles  if d1  else [],
    }


def _rehydrate_liquidity_map(verdict: dict):
    """Reconstruct a LiquidityMap-like object from the verdict's `liquidity_map`
    dict so we can reuse render_zones_for_brief / sniper_playbook without a
    double compute. Returns None if the verdict didn't carry a map (e.g. on
    upstream failure)."""
    lm_dict = verdict.get("liquidity_map")
    if not lm_dict:
        return None
    from types import SimpleNamespace

    def _to_zone(d: dict):
        return SimpleNamespace(**d)

    return SimpleNamespace(
        current_price=lm_dict.get("current_price"),
        atr_h1=lm_dict.get("atr_h1"),
        generated_at=None,
        buy_side_pools=[_to_zone(z) for z in (lm_dict.get("buy_side_pools") or [])],
        sell_side_pools=[_to_zone(z) for z in (lm_dict.get("sell_side_pools") or [])],
        nearest_above=_to_zone(lm_dict["nearest_above"]) if lm_dict.get("nearest_above") else None,
        nearest_below=_to_zone(lm_dict["nearest_below"]) if lm_dict.get("nearest_below") else None,
        highest_magnetism=_to_zone(lm_dict["highest_magnetism"]) if lm_dict.get("highest_magnetism") else None,
    )


# ────────────────────────────────────────────────────────────────────────────
# Message formatter
# ────────────────────────────────────────────────────────────────────────────

def _directional_bias_line(verdict: dict) -> str:
    """One-line bias summary: 'BUY the pullback' / 'SELL rallies' / 'RANGE'."""
    tf = (verdict.get("tf_alignment_label") or "").lower()
    mc = verdict.get("macro_context") or {}
    macro = (mc.get("gold_macro_bias") or "").lower()

    if "strong bull" in tf:
        return "🎯 BUY the pullback  (HTF strong bullish + macro backing)"
    if "strong bear" in tf:
        return "🎯 SELL rallies  (HTF strong bearish + macro backing)"
    if "bull" in tf and "bull" in macro:
        return "🎯 BUY quality dips  (bullish bias, macro aligned)"
    if "bear" in tf and "bear" in macro:
        return "🎯 SELL quality bounces  (bearish bias, macro aligned)"
    if "conflict" in tf:
        return "⚠️ RANGE mode  (HTF conflicted — trade the extremes, tight risk)"
    return "⚪ NEUTRAL  (no strong bias — wait for clean HTF alignment)"


def _bias_direction(verdict: dict) -> str:
    """Return 'BUY' | 'SELL' | 'NEUTRAL' for playbook selection."""
    tf = (verdict.get("tf_alignment_label") or "").lower()
    if "bull" in tf and "conflict" not in tf:
        return "BUY"
    if "bear" in tf and "conflict" not in tf:
        return "SELL"
    return "NEUTRAL"


def _format_trigger_block(setup: dict, icon: str, label: str) -> str:
    """Render one PRIMARY or SECONDARY setup as an IF-THEN block."""
    if not setup:
        return f"{icon} {label}: —  (no clean sweep target detected)"
    return (
        f"{icon} {label}: {setup['direction']}\n"
        f"   IF   {setup['trigger']}\n"
        f"   ➜ ENTER: {setup['entry']}\n"
        f"   ➜ SL:    {setup['sl']}\n"
        f"   ➜ TP1:   {setup['tp1']}\n"
        f"   ➜ TP2:   {setup['tp2']}  ·  RR est {setup['rr_est']}"
    )


def _format_message(*, verdict: dict, market: dict, liquidity_map,
                    regime_block: str | None = None) -> str:
    from services.liquidity_map import render_zones_for_brief, sniper_playbook

    now      = datetime.now(timezone.utc)
    tomorrow = (now + timedelta(days=1)).strftime("%Y-%m-%d")
    ts       = now.strftime("%Y-%m-%d %H:%M UTC")

    # ── Header
    current = market.get("current")
    mv_pct  = market.get("move_24h_pct") or 0
    arrow   = "▲" if mv_pct > 0 else "▼" if mv_pct < 0 else "▬"
    price_line = (
        f"Close ${current}  {arrow} {abs(mv_pct):.2f}% 24h"
        if current is not None else "Close unavailable"
    )

    # ── Directional bias
    bias_line = _directional_bias_line(verdict)
    htf_bias  = verdict.get("tf_alignment_label") or ""

    # ── Liquidity map
    if liquidity_map:
        buy_side_block  = render_zones_for_brief(
            liquidity_map.buy_side_pools,
            "📈 BUY-SIDE POOLS (stops above price — sweep targets for SELLS)",
        )
        sell_side_block = render_zones_for_brief(
            liquidity_map.sell_side_pools,
            "📉 SELL-SIDE POOLS (stops below price — sweep targets for BUYS)",
        )
        playbook = sniper_playbook(liquidity_map, htf_bias=htf_bias)
    else:
        buy_side_block  = "📈 BUY-SIDE POOLS: (map unavailable)"
        sell_side_block = "📉 SELL-SIDE POOLS: (map unavailable)"
        playbook = {"primary": None, "secondary": None, "avoid": []}

    primary_block   = _format_trigger_block(playbook["primary"],   "🎯", "PRIMARY")
    secondary_block = _format_trigger_block(playbook["secondary"], "🔄", "SECONDARY")

    avoid_lines = "\n".join(f"🚫 {rule}" for rule in playbook.get("avoid", [])) or "🚫 (none)"

    # ── Macro (compressed)
    mc     = verdict.get("macro_context") or {}
    dxy    = mc.get("dxy_bias", "NEUTRAL")
    yields = mc.get("yields_bias", "NEUTRAL")
    news   = mc.get("news_risk", "—")
    news_icon = "🟢" if news == "CLEAR" else "🟠"
    macro_line = f"DXY {dxy}  ·  Yields {yields}  ·  News {news_icon} {news}"

    # ── Compose
    msg = (
        f"🌅 XAUUSD TOMORROW'S PLAYBOOK\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"📅 Outlook for {tomorrow}  ·  as of {ts}\n"
        f"💰 {price_line}\n"
        f"\n"
        f"━━ DIRECTIONAL BIAS ━━\n"
        f"{bias_line}\n"
        f"TF alignment: {htf_bias or '—'}  ·  {macro_line}\n"
        f"\n"
        f"━━ ENGINEERED LIQUIDITY MAP ━━\n"
        f"{buy_side_block}\n"
        f"\n"
        f"{sell_side_block}\n"
        f"\n"
        f"━━ TOMORROW'S TRIGGERS ━━\n"
        f"{primary_block}\n"
        f"\n"
        f"{secondary_block}\n"
        f"\n"
        f"━━ AVOID ━━\n"
        f"{avoid_lines}\n"
    )
    if regime_block:
        msg += f"\n{regime_block}\n"
    msg += (
        f"\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"DEMO learning only  ·  0.01 lot  ·  capital preservation first."
    )
    return msg


# ────────────────────────────────────────────────────────────────────────────
# Telegram send + dedupe
# ────────────────────────────────────────────────────────────────────────────

_last_briefing_date_key: str = ""


def send_briefing_if_due(db: Session) -> bool:
    """Called by the scheduler. Sends once per calendar day (UTC).

    Suppressed when tomorrow's session is closed — Friday's "Tomorrow's
    Playbook" for Saturday is misleading (market shut all day). The
    Saturday recap + Sunday forecast newsletters cover the weekend.
    """
    global _last_briefing_date_key
    from config import settings
    from services.strategist_runner import is_weekend_quiet_hours

    if not getattr(settings, "telegram_hourly_briefing", False):
        return False
    if is_weekend_quiet_hours():
        log.debug("[briefing] suppressed — weekend quiet hours")
        return False

    now = datetime.now(timezone.utc)
    tomorrow_wd = (now + timedelta(days=1)).weekday()   # 5=Sat, 6=Sun
    if tomorrow_wd in (5, 6):
        log.debug("[briefing] suppressed — tomorrow is weekend (%d), "
                  "Sat recap / Sun forecast handles it", tomorrow_wd)
        return False

    date_key = now.strftime("%Y-%m-%d")
    if date_key == _last_briefing_date_key:
        return False

    msg = build_briefing(db)
    if not msg:
        return False

    if _send_plain(msg):
        _last_briefing_date_key = date_key
        log.info("[briefing] sent daily briefing for %s", date_key)
        return True
    return False


def _send_plain(text: str) -> bool:
    try:
        import httpx
        from config import settings
        if not (settings.telegram_bot_token and settings.telegram_chat_id):
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
