"""
Telegram Bot Poller — Long-poll getUpdates in the background
=============================================================

Drives services/telegram_bot.py. One process-wide poller thread reads
new updates from Telegram (long-poll, 30 s), dispatches each to
handle_update, and persists the offset so restarts don't replay old
commands.

Never uses raw prints and never crashes the loop on network errors —
transient failures back off exponentially, then resume.
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Optional

import requests

from database import SessionLocal

log = logging.getLogger(__name__)


# ── Config knobs ─────────────────────────────────────────────────────────────

TELEGRAM_API_BASE   = "https://api.telegram.org"
LONG_POLL_TIMEOUT_S = 30       # server-side long-poll (matches request timeout)
POLL_HTTP_TIMEOUT_S = 40       # network read timeout — bigger than long-poll
BACKOFF_BASE_S      = 2
BACKOFF_MAX_S       = 60


class _State:
    thread:  Optional[threading.Thread] = None
    stop:    threading.Event = threading.Event()
    started: bool = False


_state = _State()


# ── Offset persistence ──────────────────────────────────────────────────────

def _load_offset() -> int:
    from db_models import TelegramBotState as BS
    with SessionLocal() as db:
        row = db.query(BS).order_by(BS.id.desc()).first()
        return int(row.last_update_id) if row else 0


def _save_offset(update_id: int) -> None:
    from db_models import TelegramBotState as BS
    with SessionLocal() as db:
        row = db.query(BS).order_by(BS.id.desc()).first()
        if row is None:
            db.add(BS(last_update_id=update_id))
        else:
            row.last_update_id = update_id
        db.commit()


# ── Long-poll loop ──────────────────────────────────────────────────────────

def _get_updates(session: requests.Session, token: str, offset: int) -> list[dict]:
    url = f"{TELEGRAM_API_BASE}/bot{token}/getUpdates"
    r = session.get(url, params={
        "offset":         offset,
        "timeout":        LONG_POLL_TIMEOUT_S,
        "allowed_updates": ["message", "edited_message"],
    }, timeout=POLL_HTTP_TIMEOUT_S)
    r.raise_for_status()
    j = r.json()
    if not j.get("ok"):
        raise RuntimeError(f"getUpdates not ok: {j.get('description')!r}")
    return j.get("result") or []


def _loop() -> None:
    from config import settings
    from services.telegram_client import get_client
    from services.telegram_bot import handle_update

    if not getattr(settings, "telegram_bot_token", ""):
        log.info("[bot_poller] no telegram_bot_token — poller will idle")
        return

    token = settings.telegram_bot_token
    session = requests.Session()
    client = get_client()
    offset = _load_offset() + 1     # +1 so getUpdates skips the last-seen
    backoff = BACKOFF_BASE_S

    log.info("[bot_poller] starting · offset=%d", offset)

    while not _state.stop.is_set():
        try:
            updates = _get_updates(session, token, offset)
            if updates:
                log.info("[bot_poller] received %d update(s)", len(updates))
            for upd in updates:
                uid = int(upd.get("update_id") or 0)
                try:
                    with SessionLocal() as db:
                        handle_update(db, client, upd)
                except Exception as exc:
                    log.exception("[bot_poller] handler crash: %s", exc)
                if uid >= offset:
                    offset = uid + 1
                    try:
                        _save_offset(uid)
                    except Exception as exc:
                        log.warning("[bot_poller] offset persist failed: %s", exc)
            backoff = BACKOFF_BASE_S    # reset after a successful cycle
        except requests.RequestException as exc:
            log.warning("[bot_poller] network error, backoff %ds: %s", backoff, exc)
            if _state.stop.wait(backoff):
                break
            backoff = min(backoff * 2, BACKOFF_MAX_S)
        except Exception as exc:
            log.exception("[bot_poller] loop error, backoff %ds: %s", backoff, exc)
            if _state.stop.wait(backoff):
                break
            backoff = min(backoff * 2, BACKOFF_MAX_S)

    log.info("[bot_poller] stopped")


# ── Public start/stop ──────────────────────────────────────────────────────

def start_background_poller() -> bool:
    """Start the poller thread. Idempotent — safe to call multiple times."""
    from config import settings
    if _state.started:
        return False
    if not getattr(settings, "telegram_bot_enabled", True):
        log.info("[bot_poller] disabled via settings.telegram_bot_enabled")
        return False
    if not getattr(settings, "telegram_bot_token", ""):
        log.info("[bot_poller] not started — no token")
        return False

    _state.stop.clear()
    t = threading.Thread(target=_loop, name="telegram-bot-poller", daemon=True)
    _state.thread = t
    _state.started = True
    t.start()
    return True


def stop_background_poller(join_timeout_s: float = 5.0) -> None:
    if not _state.started:
        return
    _state.stop.set()
    t = _state.thread
    if t is not None:
        t.join(join_timeout_s)
    _state.thread = None
    _state.started = False


__all__ = ["start_background_poller", "stop_background_poller"]
