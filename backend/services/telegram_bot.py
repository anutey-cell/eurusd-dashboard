"""
Telegram Bot — Command Handler
===============================

Long-poll `getUpdates` in a background loop; dispatch each `/command`
to a handler; reply via the same telegram_client that outbound
notifications use (so audit + rate-limiting are consistent).

Commands (per user brief)
-------------------------
/help                      — inline listing of all commands
/status                    — health + shadow-mode + last-tick summary
/signals                   — active (non-terminal) canonical signals
/watchlist                 — signals in MONITORING state
/xauusd                    — single-pair summary
/signal <id>               — full detail for one signal
/performance [days]        — win-rate, R stats over N days (default 7)
/mute <strategy>           — admin: mute a strategy
/unmute <strategy>         — admin: unmute a strategy
/mode <minimal|standard|detailed>
                           — admin: set global verbosity

Only chats present in TelegramChatPreference with `is_admin=True` may
execute the admin commands. Others get a polite refusal.

Security rules (per mandate)
----------------------------
- The bot NEVER discloses the bot token, chat IDs, or account credentials.
- Live trading remains hard-disabled — no /trade command exists.
- /mode changes the ROUTER's verbosity, not the strategist's engine.
- Admin changes go via settings-side effect (updates env-loaded settings
  won't survive restart; persisted values live in TelegramChatPreference).
"""
from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone, timedelta
from typing import Optional, Callable

from sqlalchemy.orm import Session

from services.canonical_signal import (
    CanonicalSignal, TERMINAL_STATES, STRATEGY_PREFIX,
    STATE_MONITORING, STATE_ARMED,
)
from services.signal_registry import (
    active_signals, monitored_signals, get_by_signal_id, recent_transitions,
)
from services.telegram_client import (
    TelegramClient, chat_id_hash,
    RESULT_DELIVERED, RESULT_FAILED,
)
from services.telegram_templates import _esc

log = logging.getLogger(__name__)


# ── EAT helpers ─────────────────────────────────────────────────────────────

_EAT_OFFSET = timedelta(hours=3)


def _to_eat(dt: Optional[datetime]) -> str:
    if dt is None:
        return "—"
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return (dt.astimezone(timezone.utc) + _EAT_OFFSET).strftime("%Y-%m-%d %H:%M")


# ── Preferences ─────────────────────────────────────────────────────────────

def get_or_create_chat_pref(db: Session, chat_id: str, chat_type: str = "private"):
    """Return the TelegramChatPreference row, creating on first sight."""
    from db_models import TelegramChatPreference as CP
    row = db.query(CP).filter(CP.chat_id == str(chat_id)).one_or_none()
    if row is not None:
        return row
    row = CP(chat_id=str(chat_id), chat_type=chat_type)
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def _is_admin(db: Session, chat_id: str) -> bool:
    pref = get_or_create_chat_pref(db, chat_id)
    return bool(pref.is_admin)


# ── Reply helpers ───────────────────────────────────────────────────────────

def _reply(client: TelegramClient, chat_id: str, text: str) -> dict:
    """
    Fire-and-forget reply. Bypasses the notification-router idempotency
    (commands are one-shot). Delivery result returned for logging.
    """
    # Byte-truncate to Telegram's cap
    if len(text.encode("utf-8")) > 4000:
        b = text.encode("utf-8")[:3992]
        while b and (b[-1] & 0xC0) == 0x80:
            b = b[:-1]
        text = b.decode("utf-8", errors="ignore") + "\n…"

    ok, err = client._post_message(chat_id, text, parse_mode="MarkdownV2")
    return {"delivered": ok, "error": err, "bytes": len(text.encode("utf-8"))}


# ── Individual command handlers ─────────────────────────────────────────────

def cmd_help(db: Session, client: TelegramClient, chat_id: str, args: str) -> dict:
    text = (
        "*Available commands*\n"
        "`/status` — engine health + shadow\\-mode + last tick\n"
        "`/signals` — every non\\-terminal canonical signal\n"
        "`/watchlist` — signals in *MONITORING*\n"
        "`/xauusd` — single\\-pair snapshot\n"
        "`/signal <id>` — full detail of one signal \\(e\\.g\\. `/signal MDT-XAU-20260724-001`\\)\n"
        "`/performance [days]` — R stats \\(default 7 days\\)\n"
        "`/help` — this listing\n"
        "\n"
        "*Admin only*\n"
        "`/mute <strategy>` · `/unmute <strategy>`\n"
        "`/mode <minimal|standard|detailed>`\n"
        "\n"
        "Strategy IDs: `mandate` `vp_trap` `momentum` `kz_magnet` `aggregated`"
    )
    return _reply(client, chat_id, text)


def cmd_status(db: Session, client: TelegramClient, chat_id: str, args: str) -> dict:
    """Overall system status: shadow flag, active count, last tick."""
    from db_models import Signal as SM, TelegramNotification as TN
    from config import settings

    try:
        active_count = (db.query(SM)
                          .filter(~SM.state.in_(list(TERMINAL_STATES)))
                          .count())
        monitoring_count = db.query(SM).filter(SM.state == STATE_MONITORING).count()
        armed_count      = db.query(SM).filter(SM.state == STATE_ARMED).count()

        last_tn = db.query(TN).order_by(TN.id.desc()).first()
        last_notif_ts = _to_eat(last_tn.created_at) if last_tn else "—"

        shadow  = "ON" if getattr(settings, "notification_shadow_mode", True) else "OFF"
        canon   = "enabled" if getattr(settings, "notification_canonical_enabled", True) else "disabled"

        text = (
            "*XAU/USD Engine — Status*\n"
            f"Canonical layer: *{_esc(canon)}*\n"
            f"Shadow mode: *{_esc(shadow)}*\n"
            f"Active signals: *{active_count}* "
            f"\\(monitor {monitoring_count}, armed {armed_count}\\)\n"
            f"Last notification: {_esc(last_notif_ts)}\n"
        )
    except Exception as exc:
        text = f"⚠️ status query failed: {_esc(str(exc))}"
    return _reply(client, chat_id, text)


def cmd_signals(db: Session, client: TelegramClient, chat_id: str, args: str) -> dict:
    """List all active (non-terminal) signals."""
    try:
        sigs = active_signals(db, "XAUUSD")
    except Exception as exc:
        return _reply(client, chat_id, f"⚠️ registry query failed: {_esc(str(exc))}")
    if not sigs:
        return _reply(client, chat_id, "_no active signals_")

    lines = ["*Active signals*"]
    for s in sigs[:15]:
        arrow = "🟢" if s.direction == "BUY" else "🔴" if s.direction == "SELL" else "⚪"
        rr = f" · {s.rr_tp1:.1f}R" if s.rr_tp1 else ""
        lines.append(
            f"{arrow} `{_esc(s.signal_id)}` · *{_esc(s.state)}* · "
            f"{s.confidence}/100{_esc(rr)}"
        )
    if len(sigs) > 15:
        lines.append(f"…and {len(sigs) - 15} more")
    return _reply(client, chat_id, "\n".join(lines))


def cmd_watchlist(db: Session, client: TelegramClient, chat_id: str, args: str) -> dict:
    try:
        sigs = monitored_signals(db, "XAUUSD")
    except Exception as exc:
        return _reply(client, chat_id, f"⚠️ registry query failed: {_esc(str(exc))}")
    if not sigs:
        return _reply(client, chat_id, "_watchlist empty_")

    lines = ["*Watchlist (MONITORING)*"]
    for s in sigs[:15]:
        arrow = "🟢" if s.direction == "BUY" else "🔴"
        entry = f"{s.entry_zone_low:.2f}"
        if s.entry_zone_high != s.entry_zone_low:
            entry += f"\\-{s.entry_zone_high:.2f}"
        lines.append(
            f"{arrow} `{_esc(s.signal_id)}` · zone {_esc(entry)} · "
            f"conf {s.confidence}"
        )
    return _reply(client, chat_id, "\n".join(lines))


def cmd_xauusd(db: Session, client: TelegramClient, chat_id: str, args: str) -> dict:
    """Consolidated pair snapshot — pulls the latest strategist verdict."""
    try:
        from db_models import Signal as SM
        latest_active = (db.query(SM)
                           .filter(SM.instrument == "XAUUSD")
                           .filter(~SM.state.in_(list(TERMINAL_STATES)))
                           .order_by(SM.updated_at.desc()).first())
        if latest_active is None:
            return _reply(client, chat_id, "_no active XAU/USD signal_")
        s = latest_active
        arrow = "🟢 BUY" if s.direction == "BUY" else "🔴 SELL" if s.direction == "SELL" else "⚪"
        parts = [
            f"*XAU/USD — Latest Signal*",
            f"{arrow} · state *{_esc(s.state)}* · conf *{s.confidence}*/100",
            f"`{_esc(s.signal_id)}` · {_esc(s.strategy_name)}",
            f"Entry: {s.entry_zone_low:.2f}" + (f"–{s.entry_zone_high:.2f}" if s.entry_zone_high != s.entry_zone_low else ""),
            f"Stop: {s.stop_loss:.2f}",
        ]
        if s.tp1: parts.append(f"TP1: {s.tp1:.2f}")
        if s.tp2: parts.append(f"TP2: {s.tp2:.2f}")
        if s.tp3: parts.append(f"TP3: {s.tp3:.2f}")
        parts.append(f"Session: {_esc(s.session or '—')}")
        # Escape all newline-separated content since some parts have literal ':'
        text = "\n".join(_esc(p) if not p.startswith("*") and not p.startswith("`") else p for p in parts)
        # The above double-escapes — simpler: escape at generation time
        text = "\n".join([
            "*XAU/USD — Latest Signal*",
            f"{arrow} · state *{_esc(s.state)}* · conf *{s.confidence}*/100",
            f"`{_esc(s.signal_id)}` · {_esc(s.strategy_name)}",
            f"Entry: {_esc(f'{s.entry_zone_low:.2f}')}" + (
                f"–{_esc(f'{s.entry_zone_high:.2f}')}" if s.entry_zone_high != s.entry_zone_low else ""
            ),
            f"Stop: {_esc(f'{s.stop_loss:.2f}')}",
        ] + ([f"TP1: {_esc(f'{s.tp1:.2f}')}"] if s.tp1 else []
        ) + ([f"TP2: {_esc(f'{s.tp2:.2f}')}"] if s.tp2 else []
        ) + ([f"TP3: {_esc(f'{s.tp3:.2f}')}"] if s.tp3 else []
        ) + [f"Session: {_esc(s.session or '—')}"])
        return _reply(client, chat_id, text)
    except Exception as exc:
        return _reply(client, chat_id, f"⚠️ query failed: {_esc(str(exc))}")


def cmd_signal(db: Session, client: TelegramClient, chat_id: str, args: str) -> dict:
    signal_id = (args or "").strip()
    if not signal_id:
        return _reply(client, chat_id, "Usage: `/signal <id>` e\\.g\\. `/signal MDT-XAU-20260724-001`")
    try:
        s = get_by_signal_id(db, signal_id)
    except Exception as exc:
        return _reply(client, chat_id, f"⚠️ lookup failed: {_esc(str(exc))}")
    if s is None:
        return _reply(client, chat_id, f"_signal {_esc(signal_id)} not found_")

    parts = [
        f"*{_esc(s.signal_id)}*",
        f"State: *{_esc(s.state)}* \\(from *{_esc(s.previous_state or '—')}*\\)",
        f"Strategy: {_esc(s.strategy_name)} \\({_esc(s.strategy_id)}\\)",
        f"Direction: *{_esc(s.direction)}* · Confidence: *{s.confidence}*/100",
        f"Entry: {_esc(f'{s.entry_zone_low:.2f}')}"
            + (f"–{_esc(f'{s.entry_zone_high:.2f}')}" if s.entry_zone_high != s.entry_zone_low else ""),
        f"Stop: {_esc(f'{s.stop_loss:.2f}')} \\(current {_esc(f'{s.current_stop:.2f}')}\\)",
    ]
    if s.tp1: parts.append(f"TP1: {_esc(f'{s.tp1:.2f}')}"
                            + (f" \\({_esc(f'{s.rr_tp1:.1f}R')}\\)" if s.rr_tp1 else ""))
    if s.tp2: parts.append(f"TP2: {_esc(f'{s.tp2:.2f}')}"
                            + (f" \\({_esc(f'{s.rr_tp2:.1f}R')}\\)" if s.rr_tp2 else ""))
    if s.tp3: parts.append(f"TP3: {_esc(f'{s.tp3:.2f}')}"
                            + (f" \\({_esc(f'{s.rr_tp3:.1f}R')}\\)" if s.rr_tp3 else ""))
    parts += [
        f"Session: {_esc(s.session or '—')}",
        f"Created: {_esc(_to_eat(s.created_at))} EAT",
    ]
    if s.valid_until:  parts.append(f"Valid until: {_esc(_to_eat(s.valid_until))} EAT")
    if s.triggered_at: parts.append(f"Triggered: {_esc(_to_eat(s.triggered_at))} EAT")
    if s.closed_at:    parts.append(f"Closed: {_esc(_to_eat(s.closed_at))} EAT")
    if s.r_realized is not None: parts.append(f"Realized: *{_esc(f'{s.r_realized:+.2f}R')}*")
    if s.invalidation: parts.append(f"Invalidation: {_esc(s.invalidation)}")
    if s.rationale:    parts.append(f"_{_esc(s.rationale[:400])}_")
    return _reply(client, chat_id, "\n".join(parts))


def cmd_performance(db: Session, client: TelegramClient, chat_id: str, args: str) -> dict:
    """Basic performance stats over the last N days."""
    try:
        days = int((args or "7").strip())
    except ValueError:
        days = 7
    days = max(1, min(days, 90))

    from db_models import Signal as SM
    since = datetime.now(timezone.utc) - timedelta(days=days)
    try:
        rows = (db.query(SM)
                  .filter(SM.instrument == "XAUUSD")
                  .filter(SM.created_at >= since)
                  .filter(SM.state.in_(["CLOSED", "STOPPED", "TP3_HIT"]))
                  .all())
    except Exception as exc:
        return _reply(client, chat_id, f"⚠️ query failed: {_esc(str(exc))}")

    if not rows:
        return _reply(client, chat_id,
                       f"_no closed signals in the last {days}d_")

    wins   = sum(1 for r in rows if (r.r_realized or 0) > 0)
    losses = sum(1 for r in rows if (r.r_realized or 0) <= 0)
    total  = wins + losses
    total_r = sum((r.r_realized or 0) for r in rows)
    avg_r   = total_r / total if total else 0.0
    wr      = 100 * wins / total if total else 0.0

    lines = [
        f"*Performance — last {days}d*",
        f"Closed trades: *{total}* \\(W {wins} / L {losses}\\)",
        f"Win rate: *{_esc(f'{wr:.1f}%')}*",
        f"Total: *{_esc(f'{total_r:+.2f}R')}*",
        f"Avg per trade: *{_esc(f'{avg_r:+.2f}R')}*",
    ]
    return _reply(client, chat_id, "\n".join(lines))


def cmd_mute(db: Session, client: TelegramClient, chat_id: str, args: str) -> dict:
    if not _is_admin(db, chat_id):
        return _reply(client, chat_id, "⛔ admin only")
    strat = (args or "").strip().lower()
    if strat not in STRATEGY_PREFIX:
        return _reply(client, chat_id,
                       f"Unknown strategy `{_esc(strat)}`\\. Try one of: "
                       + ", ".join(f"`{k}`" for k in STRATEGY_PREFIX))
    pref = get_or_create_chat_pref(db, chat_id)
    mutes = set(json.loads(pref.strategy_mutes_json or "[]"))
    mutes.add(strat)
    pref.strategy_mutes_json = json.dumps(sorted(mutes))
    db.commit()
    return _reply(client, chat_id, f"🔕 muted *{_esc(strat)}* for this chat")


def cmd_unmute(db: Session, client: TelegramClient, chat_id: str, args: str) -> dict:
    if not _is_admin(db, chat_id):
        return _reply(client, chat_id, "⛔ admin only")
    strat = (args or "").strip().lower()
    pref = get_or_create_chat_pref(db, chat_id)
    mutes = set(json.loads(pref.strategy_mutes_json or "[]"))
    mutes.discard(strat)
    pref.strategy_mutes_json = json.dumps(sorted(mutes))
    db.commit()
    return _reply(client, chat_id, f"🔔 unmuted *{_esc(strat)}* for this chat")


def cmd_mode(db: Session, client: TelegramClient, chat_id: str, args: str) -> dict:
    if not _is_admin(db, chat_id):
        return _reply(client, chat_id, "⛔ admin only")
    mode = (args or "").strip().lower()
    if mode not in ("minimal", "standard", "detailed"):
        return _reply(client, chat_id,
                       "Usage: `/mode <minimal|standard|detailed>`")
    pref = get_or_create_chat_pref(db, chat_id)
    pref.verbosity_mode = mode
    db.commit()
    return _reply(client, chat_id, f"✅ verbosity set to *{_esc(mode)}* for this chat")


# ── Dispatch table ──────────────────────────────────────────────────────────

Handler = Callable[[Session, TelegramClient, str, str], dict]

COMMANDS: dict[str, Handler] = {
    "help":         cmd_help,
    "start":        cmd_help,     # Telegram's canonical bootstrap → same as help
    "status":       cmd_status,
    "signals":      cmd_signals,
    "watchlist":    cmd_watchlist,
    "xauusd":       cmd_xauusd,
    "signal":       cmd_signal,
    "performance":  cmd_performance,
    "perf":         cmd_performance,
    "mute":         cmd_mute,
    "unmute":       cmd_unmute,
    "mode":         cmd_mode,
}


def _parse_command(text: str) -> tuple[Optional[str], str]:
    """Parse `/cmd@botname args` → ("cmd", "args"). Non-command → (None, "")."""
    if not text or not text.startswith("/"):
        return None, ""
    parts = text[1:].split(None, 1)
    if not parts:
        return None, ""
    cmd = parts[0].split("@", 1)[0].lower()
    args = parts[1] if len(parts) > 1 else ""
    return cmd, args


def handle_update(db: Session, client: TelegramClient, update: dict) -> None:
    """Process a single Telegram Update dict. Never raises."""
    from db_models import TelegramCommandLog as CL

    update_id = int(update.get("update_id") or 0)
    msg = update.get("message") or update.get("edited_message") or {}
    text = (msg.get("text") or "").strip()
    chat = msg.get("chat") or {}
    chat_id = str(chat.get("id") or "")
    chat_type = str(chat.get("type") or "private")

    if not chat_id or not text:
        return

    cmd, args = _parse_command(text)
    if cmd is None:
        # Non-command message — ignore
        return

    # Log receipt (even if we won't handle it)
    log_row = CL(update_id=update_id, chat_id_hash=chat_id_hash(chat_id),
                 command=cmd[:64], args=(args or "")[:255], accepted=True)

    try:
        # Ensure the chat has a preference row (creates on first sight)
        get_or_create_chat_pref(db, chat_id, chat_type)
        handler = COMMANDS.get(cmd)
        if handler is None:
            log_row.accepted = False
            log_row.reject_reason = "unknown_command"
            db.add(log_row); db.commit()
            _reply(client, chat_id,
                    f"Unknown command `{_esc(cmd)}`\\. Try `/help`\\.")
            return

        res = handler(db, client, chat_id, args) or {}
        log_row.response_bytes = int(res.get("bytes") or 0)
        db.add(log_row); db.commit()
    except Exception as exc:
        log.exception("[telegram_bot] handler failure: %s", exc)
        try:
            log_row.accepted = False
            log_row.reject_reason = str(exc)[:255]
            db.add(log_row); db.commit()
        except Exception:
            db.rollback()


__all__ = [
    "handle_update", "COMMANDS",
    "cmd_help", "cmd_status", "cmd_signals", "cmd_watchlist",
    "cmd_xauusd", "cmd_signal", "cmd_performance",
    "cmd_mute", "cmd_unmute", "cmd_mode",
    "get_or_create_chat_pref", "_parse_command",
]
