"""
Telegram alert management endpoints.

GET  /api/v1/alerts/telegram/status  — returns config state (token masked, never exposed)
POST /api/v1/alerts/telegram/test    — sends a test message (only when TELEGRAM_ALERTS_ENABLED=true)

Backwards-compat aliases (deprecated, kept for older clients):
GET  /api/v1/telegram/status
POST /api/v1/telegram/test

Security: TELEGRAM_BOT_TOKEN is NEVER included in any API response.
"""
import logging

from fastapi import APIRouter, HTTPException, Request

from rate_limit import limiter

router = APIRouter(tags=["telegram"])
logger = logging.getLogger(__name__)


def _build_status() -> dict:
    """Build the status payload shared by both route paths."""
    from config import settings
    from services.telegram_alert_service import mask_telegram_token, telegram_alerts_enabled

    token = settings.telegram_bot_token
    chat  = settings.telegram_chat_id

    token_configured = bool(token)
    chat_configured  = bool(chat)

    # Mask chat_id — show last 4 chars only
    if chat:
        masked_chat = "***" + str(chat)[-4:] if len(str(chat)) > 4 else "***" + str(chat)
    else:
        masked_chat = ""

    enabled = settings.telegram_alerts_enabled or settings.telegram_enabled  # new + legacy

    return {
        "enabled":            enabled,
        "botTokenConfigured": token_configured,   # True/False only — token never returned
        "chatIdConfigured":   chat_configured,
        "chatIdMasked":       masked_chat,
        "parseMode":          settings.telegram_parse_mode,
        "cooldownMinutes":    settings.telegram_alert_cooldown_minutes,
        "signalAlerts":       settings.telegram_signal_alerts,
        "confirmAlerts":      settings.telegram_confirm_alerts,
        "tradeAlerts":        settings.telegram_trade_alerts,
        "ready":              bool(enabled and token_configured and chat_configured),
    }


# ── Primary spec-compliant routes: /alerts/telegram/* ─────────────────────────

@router.get(
    "/alerts/telegram/status",
    summary="Telegram alert configuration status",
)
def alerts_telegram_status() -> dict:
    """
    Return Telegram alert configuration state.
    Token is NEVER returned — only whether it is configured (True/False).
    """
    return _build_status()


@router.post(
    "/alerts/telegram/test",
    summary="Send a test Telegram alert (requires TELEGRAM_ALERTS_ENABLED=true)",
    description=(
        "Sends a test notification to verify credentials. "
        "Requires TELEGRAM_ALERTS_ENABLED=true in .env — "
        "returns 422 if alerts are disabled. "
        "Rate-limited to 3 requests/minute."
    ),
)
@limiter.limit("3/minute")
def alerts_telegram_test(request: Request) -> dict:
    """
    Send a test message. Returns ok:true on success, raises on failure.
    """
    from config import settings
    from services.telegram_alert_service import send_telegram_message, telegram_alerts_enabled

    # Per spec: only works when TELEGRAM_ALERTS_ENABLED=true
    enabled = settings.telegram_alerts_enabled or settings.telegram_enabled
    if not enabled:
        raise HTTPException(
            status_code=422,
            detail="TELEGRAM_ALERTS_ENABLED is false — set it to true in .env before testing.",
        )
    if not settings.telegram_bot_token:
        raise HTTPException(
            status_code=422,
            detail="TELEGRAM_BOT_TOKEN not set in .env — add it before testing.",
        )
    if not settings.telegram_chat_id:
        raise HTTPException(
            status_code=422,
            detail="TELEGRAM_CHAT_ID not set in .env — add it before testing.",
        )

    logger.info(
        "Telegram test alert requested ip=%s",
        request.client.host if request.client else "?",
    )

    ok = send_telegram_message("✅ Telegram alerts connected successfully.")
    if not ok:
        raise HTTPException(
            status_code=502,
            detail="Telegram send failed — check bot token and chat ID",
        )
    return {"ok": True, "message": "Test alert delivered successfully."}


# ── Legacy backwards-compat routes: /telegram/* ───────────────────────────────
# Kept so existing dashboards/scripts that call the old paths continue to work.

@router.get(
    "/telegram/status",
    summary="[Deprecated] Use /alerts/telegram/status",
    include_in_schema=False,
)
def telegram_status_legacy() -> dict:
    return _build_status()


@router.post(
    "/telegram/test",
    summary="[Deprecated] Use /alerts/telegram/test",
    include_in_schema=False,
)
@limiter.limit("3/minute")
def telegram_test_legacy(request: Request) -> dict:
    return alerts_telegram_test(request)
