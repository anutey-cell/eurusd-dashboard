"""
Telegram bot notification service.

Sends formatted signal alerts, trade confirmations, and demo order notifications
to a configured Telegram chat using the Bot API.

Security rules:
  - TELEGRAM_BOT_TOKEN is NEVER logged, printed, or included in any API response.
  - The token appears only inside the HTTPS URL sent to api.telegram.org.
  - All functions are fire-and-forget — exceptions are swallowed so Telegram
    failures never block signal delivery or trade execution.

Controlled by:
  TELEGRAM_ENABLED=false          — master switch (default: off)
  TELEGRAM_BOT_TOKEN=             — from @BotFather (NEVER commit to git)
  TELEGRAM_CHAT_ID=               — your chat/channel/group ID
  TELEGRAM_SIGNAL_ALERTS=true     — fire when BUY/SELL signal triggers
  TELEGRAM_CONFIRM_ALERTS=true    — fire when signal confirmed to DB
  TELEGRAM_TRADE_ALERTS=true      — fire when demo trade placed or rejected
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

# ── Emoji helpers ─────────────────────────────────────────────────────────────

_SIGNAL_EMOJI = {"BUY": "🟢", "SELL": "🔴", "WAIT": "⏸"}
_STATUS_EMOJI  = {"accepted": "✅", "rejected": "❌", "failed": "⚠️"}


def _is_enabled() -> bool:
    """Check master Telegram switch without raising. Accepts both new and legacy env var."""
    try:
        from services.telegram_alert_service import telegram_alerts_enabled
        return telegram_alerts_enabled()
    except Exception:
        return False


def _send(text: str) -> bool:
    """
    Low-level send. Returns True on success, False on any failure.
    Bot token is embedded in the HTTPS URL — it is NEVER passed to logger.
    """
    try:
        from config import settings
        import httpx

        # URL contains token — do NOT log this URL
        url = f"https://api.telegram.org/bot{settings.telegram_bot_token}/sendMessage"
        payload = {
            "chat_id":                  settings.telegram_chat_id,
            "text":                     text,
            "parse_mode":               settings.telegram_parse_mode,
            "disable_web_page_preview": True,
        }
        resp = httpx.post(url, json=payload, timeout=10.0)
        if resp.is_success:
            logger.info("Telegram notification delivered chat_id=%s", settings.telegram_chat_id)
            return True
        else:
            # Log status only — never the URL or token
            logger.warning("Telegram API error status=%s body=%s", resp.status_code, resp.text[:200])
            return False
    except Exception as exc:
        logger.warning("Telegram send failed: %s", exc)
        return False


# ── Public notification functions ─────────────────────────────────────────────

def send_signal_alert(
    pair: str,
    signal: str,
    score: int,
    entry: float | None,
    stop_loss: float | None,
    take_profit: float | None,
    rr: float | None,
    reason: str = "",
    session: str = "",
    timeframe: str = "H4",
    news_status: str = "CLEAR",
    risk_pips: int | None = None,
    target_pips: int | None = None,
) -> None:
    """
    Send a BUY/SELL signal alert. WAIT signals are silently ignored.
    Fire-and-forget — never raises.
    """
    if signal not in ("BUY", "SELL"):
        return
    if not _is_enabled():
        return

    from config import settings
    if not settings.telegram_signal_alerts:
        return

    emoji = _SIGNAL_EMOJI.get(signal, "📡")
    news_icon = "✅" if news_status == "CLEAR" else "🚫"

    # Build price lines
    price_lines = []
    if entry is not None:
        price_lines.append(f"📍 <b>Entry:</b>  {entry}")
    if stop_loss is not None:
        sl_suffix = f"  (−{risk_pips} pips)" if risk_pips else ""
        price_lines.append(f"🛑 <b>SL:</b>     {stop_loss}{sl_suffix}")
    if take_profit is not None:
        tp_suffix = f"  (+{target_pips} pips)" if target_pips else ""
        price_lines.append(f"🎯 <b>TP:</b>     {take_profit}{tp_suffix}")

    rr_line = f"⚖️  <b>R:R:</b>    {rr:.2f}" if rr else ""
    reason_preview = (reason[:120] + "…") if len(reason) > 120 else reason

    context = " | ".join(filter(None, [session, timeframe]))
    ts = datetime.now(timezone.utc).strftime("%H:%M UTC")

    msg = (
        f"{emoji} <b>{signal} SIGNAL — {pair.upper()}</b>\n"
        f"\n"
        f"📊 <b>Score:</b>  {score}/100\n"
        f"{rr_line}\n"
        + ("\n".join(price_lines) + "\n" if price_lines else "")
        + f"\n"
        f"{news_icon} <b>News:</b>  {news_status}  |  🕐 {context}  |  {ts}\n"
        + (f"\n💬 {reason_preview}\n" if reason_preview else "")
        + f"\n"
        f"⚠️ <i>Manual confirmation required. Not financial advice.</i>"
    )

    _send(msg)


def send_signal_confirmed(
    pair: str,
    signal: str,
    signal_id: int,
    score: int,
    entry: float | None,
    stop_loss: float | None,
    take_profit: float | None,
    rr: float | None,
    session: str = "",
    timeframe: str = "H4",
) -> None:
    """
    Notify that a signal has been manually confirmed and saved to the database.
    Fire-and-forget — never raises.
    """
    if not _is_enabled():
        return

    from config import settings
    if not settings.telegram_confirm_alerts:
        return

    emoji = _SIGNAL_EMOJI.get(signal, "📡")
    context = " | ".join(filter(None, [session, timeframe]))

    price_parts = []
    if entry:      price_parts.append(f"Entry: {entry}")
    if stop_loss:  price_parts.append(f"SL: {stop_loss}")
    if take_profit: price_parts.append(f"TP: {take_profit}")
    rr_str = f" | R:R {rr:.2f}" if rr else ""

    msg = (
        f"✅ <b>SIGNAL CONFIRMED — {pair.upper()} {signal}</b>\n"
        f"\n"
        f"{emoji} Score: <b>{score}/100</b>{rr_str}\n"
        f"{'  →  '.join(price_parts)}\n"
        + (f"📍 {context}\n" if context else "")
        + f"\n"
        f"🗂 Saved to journal (ID #{signal_id}).\n"
        f"<i>Log the result after the trade closes.</i>"
    )

    _send(msg)


def send_demo_trade_placed(
    pair: str,
    signal: str,
    ticket: int,
    broker_symbol: str,
    lot_size: float,
    entry: float,
    stop_loss: float | None,
    take_profit: float | None,
    risk_percent: float,
    risk_amount: float,
    spread: float | None = None,
) -> None:
    """
    Notify that a demo trade was successfully placed via MT5.
    Fire-and-forget — never raises.
    """
    if not _is_enabled():
        return

    from config import settings
    if not settings.telegram_trade_alerts:
        return

    emoji = _SIGNAL_EMOJI.get(signal, "📡")
    ts = datetime.now(timezone.utc).strftime("%H:%M UTC")
    spread_line = f"\n📶 <b>Spread:</b>  {spread} pips" if spread is not None else ""

    msg = (
        f"🤖 <b>DEMO TRADE PLACED — {pair.upper()} {signal}</b>\n"
        f"\n"
        f"{emoji} <b>Ticket:</b>  #{ticket}\n"
        f"📊 <b>Symbol:</b>  {broker_symbol}  |  Lots: {lot_size}\n"
        f"📍 <b>Entry:</b>   {entry}\n"
        f"🛑 <b>SL:</b>      {stop_loss or '—'}\n"
        f"🎯 <b>TP:</b>      {take_profit or '—'}\n"
        f"💰 <b>Risk:</b>    {risk_percent}% (${risk_amount:.2f})"
        f"{spread_line}\n"
        f"\n"
        f"🕐 {ts}\n"
        f"<i>Demo account only. No real funds at risk.</i>"
    )

    _send(msg)


def send_demo_trade_rejected(
    pair: str,
    signal: str,
    reasons: list[str],
) -> None:
    """
    Notify that a demo order was rejected by the safety chain.
    Only fires when TELEGRAM_TRADE_ALERTS=true.
    Fire-and-forget — never raises.
    """
    if not _is_enabled():
        return

    from config import settings
    if not settings.telegram_trade_alerts:
        return

    bullet_reasons = "\n".join(f"  • {r}" for r in reasons[:5])  # cap at 5 reasons

    msg = (
        f"❌ <b>DEMO ORDER REJECTED — {pair.upper()} {signal}</b>\n"
        f"\n"
        f"<b>Reasons:</b>\n"
        f"{bullet_reasons}\n"
        f"\n"
        f"<i>All rejections are logged to the database.</i>"
    )

    _send(msg)


def send_result_logged(
    pair: str,
    signal: str,
    signal_id: int,
    result: str,
    pips: int | None,
    pnl: float | None,
) -> None:
    """
    Notify when a trade result is recorded (WIN / LOSS / BREAKEVEN).
    Fire-and-forget — never raises.
    """
    if not _is_enabled():
        return

    from config import settings
    if not settings.telegram_confirm_alerts:
        return

    result_emoji = {"WIN": "🏆", "LOSS": "📉", "BREAKEVEN": "⚖️"}.get(result, "📋")
    pips_str = f"{'+' if (pips or 0) > 0 else ''}{pips} pips" if pips is not None else ""
    pnl_str  = f"${'+' if (pnl or 0) > 0 else ''}{pnl:.2f}" if pnl is not None else ""
    summary  = "  |  ".join(filter(None, [pips_str, pnl_str]))

    msg = (
        f"{result_emoji} <b>{result} — {pair.upper()} {signal}</b>\n"
        + (f"\n{summary}\n" if summary else "\n")
        + f"\n🗂 Signal #{signal_id} result saved."
    )

    _send(msg)


def test_connection() -> dict:
    """
    Send a test message and return status. Used by /telegram/test endpoint.
    Does NOT require TELEGRAM_ENABLED=true so users can test before enabling.
    """
    try:
        from config import settings

        if not settings.telegram_bot_token:
            return {"ok": False, "error": "TELEGRAM_BOT_TOKEN not configured in .env"}
        if not settings.telegram_chat_id:
            return {"ok": False, "error": "TELEGRAM_CHAT_ID not configured in .env"}

        ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        msg = (
            f"🔔 <b>FX Signal Pro — Test Message</b>\n"
            f"\n"
            f"✅ Telegram connection verified!\n"
            f"🕐 {ts}\n"
            f"\n"
            f"<i>EUR/USD & XAU/USD signal alerts are active.</i>"
        )

        ok = _send(msg)
        if ok:
            return {"ok": True, "chat_id": settings.telegram_chat_id, "message": "Test message sent successfully"}
        else:
            return {"ok": False, "error": "Message send failed — check bot token and chat ID"}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}
