"""
Alert service — fires notifications when BUY/SELL signal triggers with score >= 80.

Controlled by environment variables:
  ALERTS_ENABLED=false   (default) — log only, no external calls
  ALERT_WEBHOOK_URL=     — POST JSON payload when set and ALERTS_ENABLED=true
  ALERT_EMAIL_TO=        — email placeholder (not wired yet)

Alert payload:
  pair, signal, score, entry, stopLoss, takeProfit, rr, reason, timestamp
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)


def _build_payload(
    pair: str,
    signal: str,
    score: int,
    entry: float | None,
    stop_loss: float | None,
    take_profit: float | None,
    rr: float | None,
    reason: str,
) -> dict:
    return {
        "pair":       pair,
        "signal":     signal,
        "score":      score,
        "entry":      entry,
        "stopLoss":   stop_loss,
        "takeProfit": take_profit,
        "rr":         rr,
        "reason":     reason,
        "timestamp":  datetime.now(timezone.utc).isoformat(),
    }


def _send_webhook(url: str, payload: dict) -> None:
    try:
        import httpx
        resp = httpx.post(url, json=payload, timeout=10.0)
        if resp.is_success:
            logger.info("Alert webhook delivered pair=%s signal=%s", payload["pair"], payload["signal"])
        else:
            logger.warning("Alert webhook failed status=%s", resp.status_code)
    except Exception as exc:
        logger.warning("Alert webhook error: %s", exc)


def fire_alert(
    pair: str,
    signal: str,
    score: int,
    entry: float | None = None,
    stop_loss: float | None = None,
    take_profit: float | None = None,
    rr: float | None = None,
    reason: str = "",
) -> None:
    """
    Fire an alert for a BUY or SELL signal.
    WAIT signals are silently ignored.
    If ALERTS_ENABLED=false, only logs the event.
    """
    if signal not in ("BUY", "SELL"):
        return

    from config import settings

    payload = _build_payload(pair, signal, score, entry, stop_loss, take_profit, rr, reason)

    # Always log
    logger.info(
        "ALERT fired pair=%s signal=%s score=%s entry=%s rr=%s",
        pair, signal, score, entry, rr,
    )

    if not settings.alerts_enabled:
        logger.debug("ALERTS_ENABLED=false — skipping external delivery")
        return

    if settings.alert_webhook_url:
        _send_webhook(settings.alert_webhook_url, payload)
    else:
        logger.debug("No ALERT_WEBHOOK_URL configured — console only")

    if settings.alert_email_to:
        logger.info(
            "Email alert placeholder: would send to %s pair=%s signal=%s",
            settings.alert_email_to, pair, signal,
        )

    # ── Telegram channel ──────────────────────────────────────────────────────
    # Note: primary Telegram path is now directly in routers/signal.py via
    # telegram_alert_service.send_signal_alert(). This fallback fires for
    # webhook-triggered alerts that bypass the signal route (e.g. cron jobs).
    try:
        from services.telegram_alert_service import telegram_alerts_enabled, send_signal_alert as _tg_send
        if telegram_alerts_enabled():
            _tg_send({
                "pair":         payload["pair"],
                "signal":       payload["signal"],
                "qualityScore": payload["score"],
                "entry":        payload.get("entry"),
                "stopLoss":     payload.get("stopLoss"),
                "takeProfit":   payload.get("takeProfit"),
                "rr":           payload.get("rr"),
                "reason":       payload.get("reason", ""),
                "newsStatus":   "CLEAR",
                "model":        {},
            })
    except Exception as _tg_exc:
        logger.warning("Telegram alert error (non-fatal): %s", _tg_exc)
