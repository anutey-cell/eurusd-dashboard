"""
Telegram Client — Rate-limited HTTP + Delivery Audit
=====================================================

Single choke point for outbound Telegram messages. Every call:
  1. Idempotency check via TelegramNotification.message_fingerprint —
     re-sends silently return the prior row (dedupe surviving restart).
  2. Persists a `TelegramNotification` row BEFORE the HTTP call so a crash
     leaves a `pending` row visible in the audit trail.
  3. Rate-limits: 1 msg/sec per chat, 30 msg/sec globally (Telegram limits).
  4. Retries on 429 (respects `Retry-After`) and transient 5xx.
  5. Dry-run mode: env var TELEGRAM_DRY_RUN=1 OR settings.telegram_alerts_enabled
     is False. Rows are persisted with delivery_result="dry_run".
  6. Never logs raw chat_id or full bot_token. Uses `chat_id_hash()` for audit.

Public API
----------
    client = TelegramClient(settings)
    result = client.send_notification(db, signal, payload, chat_id=...)
    ok = client.health_check()                    # getMe
"""
from __future__ import annotations

import hashlib
import logging
import os
import threading
import time
from datetime import datetime, timezone
from typing import Optional

import requests
from sqlalchemy.orm import Session

log = logging.getLogger(__name__)


# ── Constants ────────────────────────────────────────────────────────────────

TELEGRAM_API_BASE = "https://api.telegram.org"
MAX_RETRIES       = 3
DEFAULT_TIMEOUT_S = 10
PER_CHAT_MIN_INTERVAL_S = 1.05     # slightly > 1 msg/sec ceiling
GLOBAL_MIN_INTERVAL_S   = 1.0 / 28  # slightly < 30 msg/sec ceiling

# Delivery-result enum values (must match TelegramNotification.delivery_result column)
RESULT_PENDING     = "pending"
RESULT_DELIVERED   = "delivered"
RESULT_FAILED      = "failed"
RESULT_SUPPRESSED  = "suppressed"
RESULT_DRY_RUN     = "dry_run"


# ── Helpers ──────────────────────────────────────────────────────────────────

def chat_id_hash(chat_id: str) -> str:
    """SHA256(chat_id)[:16]. Used in logs so raw chat IDs never leak."""
    if not chat_id:
        return ""
    return hashlib.sha256(str(chat_id).encode()).hexdigest()[:16]


def _mask_token(token: str) -> str:
    """Show only first 8 chars of the token for debug lines."""
    if not token:
        return "(unset)"
    return token[:8] + "…"


# ── Rate limiter ─────────────────────────────────────────────────────────────

class _TokenBucket:
    """Thread-safe minimum-interval tracker per (per-chat + global)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._last_global: float = 0.0
        self._last_per_chat: dict[str, float] = {}

    def wait_slot(self, chat_key: str) -> None:
        """Block until we're allowed to send to `chat_key`. Fair-share:
        respects both the per-chat floor (~1s) and the global floor (~30/s)."""
        with self._lock:
            now = time.monotonic()
            last_chat = self._last_per_chat.get(chat_key, 0.0)
            wait_chat = max(0.0, PER_CHAT_MIN_INTERVAL_S - (now - last_chat))
            wait_glob = max(0.0, GLOBAL_MIN_INTERVAL_S     - (now - self._last_global))
            wait = max(wait_chat, wait_glob)
            if wait > 0:
                time.sleep(wait)
                now = time.monotonic()
            self._last_per_chat[chat_key] = now
            self._last_global = now


_bucket = _TokenBucket()


# ── The client ───────────────────────────────────────────────────────────────

class TelegramClient:
    def __init__(self, settings, *, dry_run: Optional[bool] = None,
                 session: Optional[requests.Session] = None) -> None:
        self._settings = settings
        self._token   = getattr(settings, "telegram_bot_token", "") or ""
        self._default_chat = getattr(settings, "telegram_chat_id", "") or ""

        # Dry-run resolution priority:
        #   1. explicit ctor arg
        #   2. TELEGRAM_DRY_RUN env var
        #   3. alerts disabled OR token missing → automatic dry-run
        if dry_run is not None:
            self._dry_run = bool(dry_run)
        elif os.environ.get("TELEGRAM_DRY_RUN", "").lower() in ("1", "true", "yes"):
            self._dry_run = True
        elif not getattr(settings, "telegram_alerts_enabled", False):
            self._dry_run = True
        elif not self._token:
            self._dry_run = True
        else:
            self._dry_run = False

        self._http = session or requests.Session()
        log.info("[telegram_client] init · token=%s dry_run=%s default_chat=%s",
                 _mask_token(self._token), self._dry_run,
                 chat_id_hash(self._default_chat))

    # ── Public ──────────────────────────────────────────────────────────────

    @property
    def dry_run(self) -> bool:
        return self._dry_run

    def health_check(self) -> dict:
        """Call getMe. Returns {"ok": bool, "username": str|None, "error": ...}."""
        if self._dry_run or not self._token:
            return {"ok": False, "reason": "dry_run_or_no_token"}
        try:
            r = self._http.get(f"{TELEGRAM_API_BASE}/bot{self._token}/getMe",
                                timeout=DEFAULT_TIMEOUT_S)
            j = r.json()
            if j.get("ok"):
                u = j.get("result", {})
                return {"ok": True, "username": u.get("username"), "id": u.get("id")}
            return {"ok": False, "error": j.get("description")}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def send_notification(
        self,
        db: Session,
        *,
        signal_id: str,
        strategy_id: str,
        from_state: Optional[str],
        to_state: str,
        payload: dict,
        chat_id: Optional[str] = None,
        suppression_reason: Optional[str] = None,
    ) -> dict:
        """
        Emit a notification. Idempotent by payload["message_fingerprint"].
        Persists a TelegramNotification row for audit.

        `payload` is the dict returned by telegram_templates.render().

        Returns: {"delivered": bool, "result": str, "notification_id": int}
        """
        from db_models import TelegramNotification as TN

        chat = chat_id or self._default_chat
        fp = payload["message_fingerprint"]

        # ── 1. Idempotency check ────────────────────────────────────────────
        existing = db.query(TN).filter(TN.message_fingerprint == fp).one_or_none()
        if existing is not None:
            log.debug("[telegram_client] skip duplicate fp=%s prior_result=%s",
                      fp, existing.delivery_result)
            return {
                "delivered":       existing.delivery_result == RESULT_DELIVERED,
                "result":          existing.delivery_result,
                "notification_id": existing.id,
                "reason":          "duplicate_fingerprint",
            }

        # ── 2. Create pending row up front (crash-safe) ─────────────────────
        row = TN(
            signal_id=signal_id,
            strategy_id=strategy_id,
            message_type=payload["message_type"],
            from_state=from_state,
            to_state=to_state,
            message_fingerprint=fp,
            delivered=False,
            delivery_result=RESULT_PENDING,
            retry_count=0,
            chat_id_hash=chat_id_hash(chat),
            message_text=payload["text"],
            message_bytes=payload["bytes"],
            suppression_reason=suppression_reason,
        )
        db.add(row)
        db.commit()      # commit so a crash after HTTP call still leaves the row
        db.refresh(row)

        # ── 3. Suppression / dry-run short-circuits ─────────────────────────
        if suppression_reason:
            row.delivery_result = RESULT_SUPPRESSED
            row.delivered = False
            db.commit()
            return {"delivered": False, "result": RESULT_SUPPRESSED,
                    "notification_id": row.id, "reason": suppression_reason}

        if self._dry_run:
            row.delivery_result = RESULT_DRY_RUN
            row.delivered = False
            row.delivered_at = datetime.now(timezone.utc)
            db.commit()
            log.info("[telegram_client] DRY-RUN chat=%s type=%s bytes=%d fp=%s",
                     chat_id_hash(chat), payload["message_type"],
                     payload["bytes"], fp)
            return {"delivered": False, "result": RESULT_DRY_RUN,
                    "notification_id": row.id}

        if not chat:
            row.delivery_result = RESULT_FAILED
            row.error_message = "no chat_id provided"
            db.commit()
            return {"delivered": False, "result": RESULT_FAILED,
                    "notification_id": row.id, "reason": "no_chat"}

        # ── 4. Rate-limit + HTTP send with retry ────────────────────────────
        _bucket.wait_slot(chat_id_hash(chat))
        ok, err = self._post_message(chat, payload["text"], payload["parse_mode"])

        # If HTTP returned 429, we already retried inside _post_message.
        # Update row with final result.
        row.retry_count = getattr(row, "retry_count", 0)  # server-side default may fill
        if ok:
            row.delivered = True
            row.delivery_result = RESULT_DELIVERED
            row.delivered_at = datetime.now(timezone.utc)
        else:
            row.delivery_result = RESULT_FAILED
            row.error_message = (err or "unknown")[:255]
        db.commit()

        return {"delivered": ok, "result": row.delivery_result,
                "notification_id": row.id,
                "error": None if ok else err}

    # ── Internals ───────────────────────────────────────────────────────────

    def _post_message(self, chat_id: str, text: str, parse_mode: str
                       ) -> tuple[bool, Optional[str]]:
        """Send with retry on 429 / transient 5xx."""
        url = f"{TELEGRAM_API_BASE}/bot{self._token}/sendMessage"
        body = {
            "chat_id":                    chat_id,
            "text":                       text,
            "parse_mode":                 parse_mode,
            "disable_web_page_preview":   True,
        }
        for attempt in range(MAX_RETRIES + 1):
            try:
                r = self._http.post(url, json=body, timeout=DEFAULT_TIMEOUT_S)
                if r.status_code == 200:
                    return True, None
                if r.status_code == 429:
                    # Respect Retry-After header (seconds)
                    j = {}
                    try:
                        j = r.json()
                    except Exception:
                        pass
                    retry_after = j.get("parameters", {}).get("retry_after", 3)
                    log.warning("[telegram_client] 429 rate-limited, retry_after=%ss (attempt %d)",
                                retry_after, attempt + 1)
                    time.sleep(min(int(retry_after), 30))
                    continue
                if 500 <= r.status_code < 600 and attempt < MAX_RETRIES:
                    backoff = 2 ** attempt
                    log.warning("[telegram_client] %d server error, backoff %ds",
                                r.status_code, backoff)
                    time.sleep(backoff)
                    continue
                # Non-retryable failure
                err = f"HTTP {r.status_code}: {r.text[:200]}"
                log.error("[telegram_client] send failed · %s", err)
                return False, err
            except requests.RequestException as exc:
                if attempt < MAX_RETRIES:
                    backoff = 2 ** attempt
                    log.warning("[telegram_client] network error %s, backoff %ds",
                                exc, backoff)
                    time.sleep(backoff)
                    continue
                return False, f"network: {exc}"
        return False, "max retries exceeded"


# ── Module-level convenience ─────────────────────────────────────────────────

_client_singleton: Optional[TelegramClient] = None


def get_client() -> TelegramClient:
    """Lazily-created process-wide singleton. Reads from `config.settings`."""
    global _client_singleton
    if _client_singleton is None:
        from config import settings
        _client_singleton = TelegramClient(settings)
    return _client_singleton


def reset_client_for_test() -> None:
    """Clear the cached singleton so tests can inject new settings."""
    global _client_singleton
    _client_singleton = None


__all__ = [
    "TelegramClient", "get_client", "reset_client_for_test",
    "chat_id_hash",
    "RESULT_PENDING", "RESULT_DELIVERED", "RESULT_FAILED",
    "RESULT_SUPPRESSED", "RESULT_DRY_RUN",
]
