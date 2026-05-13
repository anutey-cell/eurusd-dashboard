"""
Telegram signal alert service — spec-compliant implementation.

Exports the five required public functions:
  - telegram_alerts_enabled()        → bool
  - mask_telegram_token(token)       → str   (safe for logging)
  - format_signal_alert(payload)     → str   (HTML message body)
  - send_telegram_message(message)   → bool  (raw send)
  - send_signal_alert(payload)       → str   (returns alertStatus)

Alert trigger rules (all must pass):
  signal in BUY/SELL · qualityScore >= 80 · rr >= 2.5 · newsStatus == CLEAR
  pair in eurusd/xauusd · entry/stopLoss/takeProfit/invalidation present
  liquidity/FVG/structure model fields non-empty
  TELEGRAM_ALERTS_ENABLED=true · BOT_TOKEN set · CHAT_ID set

Duplicate prevention:
  Fingerprint = sha256(pair+signal+entry+sl+tp+score)[:16]
  In-memory dict with configurable cooldown (TELEGRAM_ALERT_COOLDOWN_MINUTES).
  Every attempt (sent/skipped/failed) is logged to telegram_alert_logs table.

Security:
  Bot token NEVER logged or included in any API response.
  Token appears only inside the HTTPS URL sent to api.telegram.org.
  All functions are fire-and-forget — exceptions are swallowed.
"""
from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any

logger = logging.getLogger(__name__)

# ── In-memory duplicate tracker ───────────────────────────────────────────────
# Maps fingerprint → datetime of last successful send
_recent_fingerprints: dict[str, datetime] = {}

# ── Emoji helpers ──────────────────────────────────────────────────────────────
_SIGNAL_EMOJI = {"BUY": "🟢", "SELL": "🔴"}


# ═══════════════════════════════════════════════════════════════════════════════
# Public API
# ═══════════════════════════════════════════════════════════════════════════════

def telegram_alerts_enabled() -> bool:
    """
    Return True only when ALL of TELEGRAM_ALERTS_ENABLED, BOT_TOKEN,
    and CHAT_ID are set and valid.  Never raises.
    """
    try:
        from config import settings
        primary  = settings.telegram_alerts_enabled
        fallback = settings.telegram_enabled   # legacy .env compat
        return bool(
            (primary or fallback)
            and settings.telegram_bot_token
            and settings.telegram_chat_id
        )
    except Exception:
        return False


def mask_telegram_token(token: str) -> str:
    """
    Return a safe masked representation for logging.
    Format:  {numeric_prefix}:{first2}{'*'*n}{last3}
    Example: 7123456789:AAF****xyz
    Token is NEVER returned in API responses — this is log-safe only.
    """
    if not token or ":" not in token:
        return "***"
    prefix, secret = token.split(":", 1)
    if len(secret) <= 5:
        return f"{prefix}:***"
    stars = "*" * (len(secret) - 5)
    return f"{prefix}:{secret[:2]}{stars}{secret[-3:]}"


def format_signal_alert(signal_payload: dict) -> str:
    """
    Build the HTML-formatted Telegram message body from a signal payload dict.

    The payload uses camelCase keys as emitted by SignalAnalysisOutput.model_dump(by_alias=True):
      pair, signal, qualityScore, entry, stopLoss, takeProfit, riskPips, targetPips,
      rr, invalidation, reason, newsStatus, model{liquidity, structure, fvg, session}
    """
    pair         = signal_payload.get("pair", "").lower()
    signal       = signal_payload.get("signal", "")
    score        = signal_payload.get("qualityScore", 0)
    entry        = signal_payload.get("entry")
    sl           = signal_payload.get("stopLoss")
    tp           = signal_payload.get("takeProfit")
    risk_pips    = signal_payload.get("riskPips")
    target_pips  = signal_payload.get("targetPips", 40)
    rr           = signal_payload.get("rr")
    invalidation = signal_payload.get("invalidation")
    reason       = signal_payload.get("reason", "")
    news_status  = signal_payload.get("newsStatus", "CLEAR")

    model_dict   = signal_payload.get("model") or {}
    liquidity    = model_dict.get("liquidity", "")
    structure    = model_dict.get("structure", "")
    fvg          = model_dict.get("fvg", "")
    session_str  = model_dict.get("session", "")

    # Display names and units
    display_pair = "XAU/USD" if pair == "xauusd" else "EUR/USD"
    unit         = "points" if pair == "xauusd" else "pips"
    decimals     = 2 if pair == "xauusd" else 5

    def _fmt(v: float | None) -> str:
        if v is None:
            return "—"
        return f"{v:.{decimals}f}"

    rr_str = f"1:{rr:.2f}" if rr else "—"

    # Invalidation text
    if invalidation is not None:
        inv_text = (
            f"Below {_fmt(invalidation)}"
            if signal == "BUY"
            else f"Above {_fmt(invalidation)}"
        )
    else:
        inv_text = "See trade plan"

    # Reason — truncate at 160 chars
    reason_short = (reason[:157] + "…") if len(reason) > 160 else reason

    msg = (
        f"🚨 <b>{display_pair} {signal} Signal Ready</b>\n"
        f"\n"
        f"<b>Quality Score:</b> {score}/100\n"
        f"<b>Entry:</b> {_fmt(entry)}\n"
        f"<b>Stop Loss:</b> {_fmt(sl)}\n"
        f"<b>Take Profit:</b> {_fmt(tp)}\n"
        f"<b>Target:</b> {target_pips} {unit}\n"
        f"<b>Risk:</b> {risk_pips or '—'} {unit}\n"
        f"<b>RR:</b> {rr_str}\n"
        f"\n"
        f"<b>Model:</b>\n"
        f"Liquidity: {liquidity or '—'}\n"
        f"Structure: {structure or '—'}\n"
        f"FVG: {fvg or '—'}\n"
        f"News: {news_status}\n"
        f"Session: {session_str or '—'}\n"
        f"\n"
        f"<b>Invalidation:</b>\n"
        f"{inv_text}\n"
        f"\n"
        f"<b>Reason:</b>\n"
        f"{reason_short}\n"
        f"\n"
        f"⚠️ Manual confirmation required. This is not automatic execution."
    )
    return msg


def send_telegram_message(message: str) -> bool:
    """
    Low-level public send.  Returns True on success, False on failure.
    Token is embedded in the HTTPS URL — it is NEVER passed to logger.
    """
    try:
        from config import settings
        import httpx

        # URL contains token — do NOT log this variable
        url = (
            f"https://api.telegram.org/bot"
            f"{settings.telegram_bot_token}"
            f"/sendMessage"
        )
        payload = {
            "chat_id":                  settings.telegram_chat_id,
            "text":                     message,
            "parse_mode":               settings.telegram_parse_mode,
            "disable_web_page_preview": True,
        }
        resp = httpx.post(url, json=payload, timeout=10.0)
        if resp.is_success:
            logger.info(
                "Telegram alert delivered to chat_id=%s",
                settings.telegram_chat_id,
            )
            return True
        # Log status only — never the URL or token
        logger.warning(
            "Telegram API error status=%s body=%s",
            resp.status_code,
            resp.text[:200],
        )
        return False
    except Exception as exc:
        logger.warning("Telegram send failed: %s", exc)
        return False


def send_signal_alert(signal_payload: dict) -> str:
    """
    Validate, deduplicate, format, and send a signal alert.

    Returns one of:
      "sent"              — message delivered to Telegram
      "disabled"          — TELEGRAM_ALERTS_ENABLED=false or credentials missing
      "not_qualified"     — signal did not pass all 13 gate checks
      "duplicate_skipped" — same fingerprint sent within cooldown window
      "failed"            — Telegram API call failed

    Fire-and-forget — never raises.
    """
    try:
        return _send_signal_alert_inner(signal_payload)
    except Exception as exc:
        logger.warning("Unexpected error in send_signal_alert (non-fatal): %s", exc)
        return "failed"


# ═══════════════════════════════════════════════════════════════════════════════
# Internal helpers
# ═══════════════════════════════════════════════════════════════════════════════

def _send_signal_alert_inner(payload: dict) -> str:
    """Core logic — called by send_signal_alert(), wrapped in try/except."""
    # ── Gate 1: alerts enabled ────────────────────────────────────────────────
    if not telegram_alerts_enabled():
        return "disabled"

    from config import settings

    # ── Gate 2: all 13 qualification checks ───────────────────────────────────
    qualified, reasons = _qualify(payload)
    if not qualified:
        logger.debug(
            "Signal alert not_qualified pair=%s signal=%s reasons=%s",
            payload.get("pair"), payload.get("signal"), reasons,
        )
        _log_to_db(payload, fingerprint="", status="not_qualified", error="; ".join(reasons))
        return "not_qualified"

    # ── Gate 3: duplicate check ────────────────────────────────────────────────
    fingerprint = _build_fingerprint(payload)
    if _is_duplicate(fingerprint, settings.telegram_alert_cooldown_minutes):
        logger.info(
            "Telegram alert duplicate_skipped pair=%s signal=%s fingerprint=%s",
            payload.get("pair"), payload.get("signal"), fingerprint,
        )
        _log_to_db(payload, fingerprint=fingerprint, status="duplicate_skipped")
        return "duplicate_skipped"

    # ── Send ───────────────────────────────────────────────────────────────────
    message = format_signal_alert(payload)
    ok = send_telegram_message(message)

    status = "sent" if ok else "failed"
    if ok:
        _record_fingerprint(fingerprint)
        logger.info(
            "Telegram signal alert sent pair=%s signal=%s score=%s",
            payload.get("pair"), payload.get("signal"), payload.get("qualityScore"),
        )
    else:
        logger.warning(
            "Telegram signal alert failed pair=%s signal=%s",
            payload.get("pair"), payload.get("signal"),
        )

    _log_to_db(payload, fingerprint=fingerprint, status=status)
    return status


def _qualify(payload: dict) -> tuple[bool, list[str]]:
    """
    Run all 13 qualification gates.  Returns (passed, [failure_reasons]).
    """
    failures: list[str] = []

    signal      = payload.get("signal", "")
    score       = payload.get("qualityScore") or 0
    rr          = payload.get("rr") or 0.0
    news        = payload.get("newsStatus", "")
    pair        = (payload.get("pair") or "").lower()
    entry       = payload.get("entry")
    sl          = payload.get("stopLoss")
    tp          = payload.get("takeProfit")
    invalidation = payload.get("invalidation")

    model_dict  = payload.get("model") or {}
    liquidity   = model_dict.get("liquidity") or ""
    structure   = model_dict.get("structure") or ""
    fvg         = model_dict.get("fvg") or ""

    if signal not in ("BUY", "SELL"):
        failures.append(f"signal is {signal!r} — only BUY/SELL qualify")
    if score < 80:
        failures.append(f"qualityScore {score} < 80")
    if rr < 2.5:
        failures.append(f"RR {rr:.2f} < 2.5")
    if news != "CLEAR":
        failures.append(f"newsStatus is {news!r} (must be CLEAR)")
    if pair not in ("eurusd", "xauusd"):
        failures.append(f"pair {pair!r} not supported")
    if entry is None:
        failures.append("entry price missing")
    if sl is None:
        failures.append("stopLoss missing")
    if tp is None:
        failures.append("takeProfit missing")
    if invalidation is None:
        failures.append("invalidation level missing")
    if not liquidity:
        failures.append("liquidity confirmation missing")
    if not fvg:
        failures.append("FVG confirmation missing")
    if not structure:
        failures.append("structure confirmation missing")

    return len(failures) == 0, failures


def _build_fingerprint(payload: dict) -> str:
    """
    Build a short hash from the key signal attributes.
    Identical signals within the cooldown window produce the same fingerprint.
    """
    pair  = (payload.get("pair") or "").lower()
    sig   = payload.get("signal", "")
    entry = round(payload.get("entry") or 0, 4)
    sl    = round(payload.get("stopLoss") or 0, 4)
    tp    = round(payload.get("takeProfit") or 0, 4)
    score = payload.get("qualityScore", 0)

    raw = f"{pair}:{sig}:{entry}:{sl}:{tp}:{score}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def _is_duplicate(fingerprint: str, cooldown_minutes: int) -> bool:
    """Return True if this fingerprint was sent within the cooldown window."""
    _evict_expired(cooldown_minutes)
    return fingerprint in _recent_fingerprints


def _record_fingerprint(fingerprint: str) -> None:
    """Store a sent fingerprint with current timestamp."""
    _recent_fingerprints[fingerprint] = datetime.now(timezone.utc)


def _evict_expired(cooldown_minutes: int) -> None:
    """Remove fingerprints older than 2× cooldown to keep dict small."""
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=cooldown_minutes * 2)
    expired = [k for k, ts in _recent_fingerprints.items() if ts < cutoff]
    for k in expired:
        del _recent_fingerprints[k]


def _log_to_db(
    payload: dict,
    fingerprint: str,
    status: str,
    error: str | None = None,
    raw_response: Any = None,
) -> None:
    """
    Write a row to telegram_alert_logs.  Silently swallows all DB errors
    so a logging failure never blocks the signal pipeline.
    """
    try:
        from database import SessionLocal
        from db_models import TelegramAlertLog

        db = SessionLocal()
        try:
            log = TelegramAlertLog(
                pair          = (payload.get("pair") or "").lower(),
                signal        = payload.get("signal", ""),
                quality_score = payload.get("qualityScore"),
                entry         = payload.get("entry"),
                stop_loss     = payload.get("stopLoss"),
                take_profit   = payload.get("takeProfit"),
                rr            = payload.get("rr"),
                fingerprint   = fingerprint,
                status        = status,
                error_message = error,
                raw_response_json = json.dumps(raw_response) if raw_response else None,
            )
            db.add(log)
            db.commit()
        finally:
            db.close()
    except Exception as exc:
        logger.debug("Telegram DB log failed (non-fatal): %s", exc)


# ── Backwards-compatible helpers used by telegram_service.py and alert_service.py ──

def _is_enabled_legacy() -> bool:
    """
    Checks both TELEGRAM_ALERTS_ENABLED (new) and TELEGRAM_ENABLED (legacy).
    Used internally by telegram_service.py wrappers.
    """
    return telegram_alerts_enabled()
