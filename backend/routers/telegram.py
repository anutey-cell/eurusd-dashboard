"""
Telegram bot management endpoints.

GET  /api/v1/telegram/status   — returns config state (token masked, never exposed)
POST /api/v1/telegram/test     — sends a test message to verify the bot

Security: TELEGRAM_BOT_TOKEN is NEVER included in any API response.
"""
import logging

from fastapi import APIRouter, HTTPException, Request

from rate_limit import limiter

router = APIRouter(prefix="/telegram", tags=["telegram"])
logger = logging.getLogger(__name__)


@router.get("/status", summary="Telegram notification status")
def telegram_status() -> dict:
    """
    Return Telegram configuration state.
    Token is NEVER returned — only whether it is configured (non-empty).
    """
    from config import settings

    token_configured = bool(settings.telegram_bot_token and settings.telegram_bot_token != "")
    chat_configured  = bool(settings.telegram_chat_id and settings.telegram_chat_id != "")

    # Mask chat_id partially for privacy (show last 4 chars)
    masked_chat = ""
    if settings.telegram_chat_id:
        cid = str(settings.telegram_chat_id)
        masked_chat = "***" + cid[-4:] if len(cid) > 4 else "***" + cid

    return {
        "enabled":           settings.telegram_enabled,
        "token_configured":  token_configured,   # True/False only — token never returned
        "chat_id_masked":    masked_chat,
        "signal_alerts":     settings.telegram_signal_alerts,
        "confirm_alerts":    settings.telegram_confirm_alerts,
        "trade_alerts":      settings.telegram_trade_alerts,
        "ready":             settings.telegram_enabled and token_configured and chat_configured,
    }


@router.post(
    "/test",
    summary="Send a test Telegram message to verify configuration",
    description=(
        "Sends a test notification to the configured chat. "
        "Works even if TELEGRAM_ENABLED=false so you can verify credentials first. "
        "Rate-limited to 3 requests/minute."
    ),
)
@limiter.limit("3/minute")
def telegram_test(request: Request) -> dict:
    """
    Send a test message. Returns ok:true on success, ok:false with error on failure.
    """
    from config import settings
    from services.telegram_service import test_connection

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
        "Telegram test message requested ip=%s",
        request.client.host if request.client else "?",
    )

    result = test_connection()
    if not result["ok"]:
        raise HTTPException(
            status_code=502,
            detail=result.get("error", "Telegram send failed"),
        )
    return result
