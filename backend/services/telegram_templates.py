"""
Telegram Message Templates — Canonical Renderers
=================================================

Every Telegram message emitted by the notification layer flows through
here. Input: (message_type, CanonicalSignal, extra_context, mode).
Output: {"text", "parse_mode", "message_type", "message_fingerprint"}.

Rules
-----
1. MarkdownV2 for every message. Every user-facing string passes through
   `_esc()` — no exception. Prices, times, symbols, all must escape.
2. All timestamps display as EAT (Africa/Nairobi, UTC+3, no DST).
3. Modes: minimal | standard | detailed. Section-inclusion is the only
   difference — every render path is deterministic.
4. Truncate to Telegram's 4096-byte cap; append `\n…` if truncated.
5. Idempotency: fingerprint over (signal_id, state, key_prices).
6. Header emoji + first line encode urgency (green/yellow/red band).

The router decides WHEN to render. This module decides WHAT the text is.
"""
from __future__ import annotations

from datetime import datetime, timezone, timedelta
from typing import Optional

from services.canonical_signal import (
    CanonicalSignal, message_fingerprint,
    STATE_MONITORING, STATE_ARMED, STATE_TRIGGERED, STATE_ACTIVE,
    STATE_TP1_HIT, STATE_TP2_HIT, STATE_TP3_HIT,
    STATE_BREAKEVEN, STATE_TRAILING, STATE_STOPPED,
    STATE_INVALIDATED, STATE_EXPIRED, STATE_CLOSED,
    DIRECTION_BUY, DIRECTION_SELL,
)


# ── Constants ────────────────────────────────────────────────────────────────

TELEGRAM_MAX_BYTES = 4096
EAT_OFFSET = timedelta(hours=3)          # Africa/Nairobi, no DST

# Notification-mode selectors
MODE_MINIMAL   = "minimal"
MODE_STANDARD  = "standard"
MODE_DETAILED  = "detailed"

ALL_MODES = {MODE_MINIMAL, MODE_STANDARD, MODE_DETAILED}


# ── MarkdownV2 escape ────────────────────────────────────────────────────────

_MDV2_ESCAPE = set("_*[]()~`>#+-=|{}.!\\")


def _esc(s) -> str:
    """Escape a value for MarkdownV2. Any char in _MDV2_ESCAPE gets a
    leading backslash. `None` becomes an em-dash."""
    if s is None:
        return "—"
    out = []
    for ch in str(s):
        if ch in _MDV2_ESCAPE:
            out.append("\\")
        out.append(ch)
    return "".join(out)


def _price(v: Optional[float], places: int = 2) -> str:
    r"""Escaped price: `4020.5` → `4020\.5`; None → `—`."""
    if v is None:
        return "—"
    return _esc(f"{v:.{places}f}")


def _pips(v: Optional[float]) -> str:
    if v is None:
        return "—"
    return _esc(f"{v:.1f}")


def _pct(v: Optional[float]) -> str:
    if v is None:
        return "—"
    return _esc(f"{v:.0f}%")


def _to_eat(dt: Optional[datetime]) -> str:
    """UTC datetime → 'HH:MM' in EAT (escaped)."""
    if dt is None:
        return "—"
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    eat = dt.astimezone(timezone.utc) + EAT_OFFSET
    return _esc(eat.strftime("%H:%M"))


def _to_eat_date(dt: Optional[datetime]) -> str:
    """UTC datetime → 'YYYY-MM-DD HH:MM' in EAT (escaped)."""
    if dt is None:
        return "—"
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    eat = dt.astimezone(timezone.utc) + EAT_OFFSET
    return _esc(eat.strftime("%Y-%m-%d %H:%M"))


# ── Header helpers ───────────────────────────────────────────────────────────

_STATE_EMOJI = {
    STATE_MONITORING: "🔍",
    STATE_ARMED:      "🎯",
    STATE_TRIGGERED:  "🚀",
    STATE_ACTIVE:     "⚡",
    STATE_TP1_HIT:    "✅",
    STATE_TP2_HIT:    "✅✅",
    STATE_TP3_HIT:    "🏁",
    STATE_BREAKEVEN:  "🛡️",
    STATE_TRAILING:   "📈",
    STATE_STOPPED:    "🛑",
    STATE_INVALIDATED:"❌",
    STATE_EXPIRED:    "⏱️",
    STATE_CLOSED:     "✔️",
}


def _dir_arrow(direction: str) -> str:
    if direction == DIRECTION_BUY:
        return "🟢 BUY"
    if direction == DIRECTION_SELL:
        return "🔴 SELL"
    return "⚪ FLAT"


def _header(sig: CanonicalSignal, title: str, ctx: dict,
             emoji: Optional[str] = None) -> str:
    """Standardized header: emoji · title · instrument · direction / signal_id / time+session."""
    emo = emoji or _STATE_EMOJI.get(sig.state, "📣")
    when = _to_eat(ctx.get("__now") or datetime.now(timezone.utc))
    dir_line = _dir_arrow(sig.direction)
    lines = [
        f"{emo} *{_esc(title)}* · {_esc(sig.instrument)} · {dir_line}",
        f"`{_esc(sig.signal_id)}`",
        f"🕐 {when} EAT · {_esc(sig.session or '—')}",
    ]
    return "\n".join(lines)


def _footer(sig: CanonicalSignal, mode: str) -> str:
    """Standardized footer with confidence + strategy identity."""
    tag = f"Confidence: *{sig.confidence}*/100 · {_esc(sig.strategy_name)}"
    if mode == MODE_DETAILED and sig.data_source:
        tag += f" · src: {_esc(sig.data_source)}"
    return tag


# ── Helpers for literal text with MDV2-special chars ─────────────────────────

def _lit(s: str) -> str:
    """Alias for _esc — makes literal-text intent explicit at call sites."""
    return _esc(s)


def _entry_zone(sig: CanonicalSignal) -> str:
    if sig.entry_zone_low == sig.entry_zone_high:
        return _price(sig.entry_zone_low)
    return f"{_price(sig.entry_zone_low)} – {_price(sig.entry_zone_high)}"


def _targets_block(sig: CanonicalSignal, mode: str) -> list[str]:
    """Return a list of formatted TP lines (empty if none)."""
    lines = []
    for tp, label, rr in (
        (sig.tp1, sig.tp1_label, sig.rr_tp1),
        (sig.tp2, sig.tp2_label, sig.rr_tp2),
        (sig.tp3, sig.tp3_label, sig.rr_tp3),
    ):
        if tp is None:
            continue
        rr_str = f" \\({_esc(f'{rr:.1f}R')}\\)" if rr is not None else ""
        lbl = _esc(label) if label else ""
        prefix = f"  • *{lbl}*  " if lbl else "  • "
        lines.append(f"{prefix}{_price(tp)}{rr_str}")
    return lines


def _conditions_block(sig: CanonicalSignal, mode: str) -> list[str]:
    """Bullet list of met + missing conditions. Suppressed in minimal mode."""
    if mode == MODE_MINIMAL:
        return []
    lines = []
    if sig.conditions_met:
        lines.append("*Met:*")
        for c in sig.conditions_met[:6]:
            lines.append(f"  ✓ {_esc(c)}")
    if sig.conditions_missing:
        lines.append("*Missing:*")
        for c in sig.conditions_missing[:4]:
            lines.append(f"  ✗ {_esc(c)}")
    return lines


def _confluence_block(sig: CanonicalSignal, mode: str) -> list[str]:
    if mode == MODE_MINIMAL or not sig.confluence:
        return []
    lines = ["*Confluence:*"]
    for c in sig.confluence[:5]:
        name = c.get("strategy_name", "?")
        conf = c.get("confidence", "—")
        lines.append(f"  • {_esc(name)}: {_esc(conf)}")
    return lines


def _truncate(text: str) -> str:
    if len(text.encode("utf-8")) <= TELEGRAM_MAX_BYTES:
        return text
    # Byte-safe truncate (may drop mid-char; add ellipsis)
    b = text.encode("utf-8")[:TELEGRAM_MAX_BYTES - 8]
    # Trim to last complete UTF-8 char boundary
    while b and (b[-1] & 0xC0) == 0x80:
        b = b[:-1]
    return b.decode("utf-8", errors="ignore") + "\n…"


# ── Individual message renderers ─────────────────────────────────────────────

def _render_monitoring(sig: CanonicalSignal, ctx: dict, mode: str) -> list[str]:
    """State: DETECTED/MONITORING → MONITORING. 'On the watchlist.'"""
    body = [
        _header(sig, "MONITORING", ctx),
        "",
        f"Watch zone: *{_entry_zone(sig)}*",
        f"Invalidation: {_esc(sig.invalidation or '—')}",
    ]
    if sig.trap_side:
        body.append(f"Trap side: {_esc(sig.trap_side)}")
    if sig.market_regime:
        body.append(f"Regime: {_esc(sig.market_regime)}")
    if mode != MODE_MINIMAL and sig.rationale:
        body += ["", f"_{_esc(sig.rationale)}_"]
    body += _conditions_block(sig, mode)
    body += ["", _footer(sig, mode)]
    return body


def _render_actionable(sig: CanonicalSignal, ctx: dict, mode: str) -> list[str]:
    """State: DETECTED/MONITORING → ARMED. 'Ready to trigger.'"""
    body = [
        _header(sig, "ACTIONABLE SETUP", ctx),
        "",
        f"Entry: *{_entry_zone(sig)}*",
        f"Stop:  *{_price(sig.stop_loss)}*  \\(risk: {_pips(sig.risk_points())} pts\\)",
    ]
    if sig.no_chase_price is not None:
        body.append(f"No\\-chase past: {_price(sig.no_chase_price)}")
    tps = _targets_block(sig, mode)
    if tps:
        body += ["", "*Targets:*", *tps]
    body.append(f"Invalidation: {_esc(sig.invalidation or '—')}")
    if mode != MODE_MINIMAL and sig.rationale:
        body += ["", f"_{_esc(sig.rationale)}_"]
    body += _conditions_block(sig, mode)
    body += _confluence_block(sig, mode)
    body += ["", _footer(sig, mode)]
    return body


def _render_entry_triggered(sig: CanonicalSignal, ctx: dict, mode: str) -> list[str]:
    """State: ARMED → TRIGGERED. 'Live trade opened.'"""
    fill = ctx.get("fill_price")
    body = [
        _header(sig, "ENTRY TRIGGERED", ctx, emoji="🚀"),
        "",
        f"Fill: *{_price(fill) if fill is not None else _entry_zone(sig)}*",
        f"Stop: *{_price(sig.stop_loss)}*  \\(risk: {_pips(sig.risk_points())} pts\\)",
    ]
    tps = _targets_block(sig, mode)
    if tps:
        body += ["*Targets:*", *tps]
    if ctx.get("mt5_ticket"):
        body.append(f"MT5 ticket: `{_esc(ctx['mt5_ticket'])}`")
    if mode == MODE_DETAILED:
        body.append(f"Session: {_esc(sig.session or '—')} · Regime: {_esc(sig.market_regime or '—')}")
    body += ["", _footer(sig, mode)]
    return body


def _render_tp_hit(sig: CanonicalSignal, ctx: dict, mode: str, level: int) -> list[str]:
    """Generic TP-hit renderer for TP1/TP2/TP3."""
    tp_price = ctx.get("tp_price") or {1: sig.tp1, 2: sig.tp2, 3: sig.tp3}.get(level)
    tp_label = {1: sig.tp1_label, 2: sig.tp2_label, 3: sig.tp3_label}.get(level) or f"TP{level}"
    rr       = {1: sig.rr_tp1, 2: sig.rr_tp2, 3: sig.rr_tp3}.get(level)
    r_str    = f" \\({_esc(f'{rr:.1f}R')}\\)" if rr else ""
    partial  = ctx.get("partial_closed_pct")
    title    = "TARGET HIT" if level < 3 else "FINAL TARGET"
    body = [
        _header(sig, title, ctx, emoji="✅" if level == 1 else "✅✅" if level == 2 else "🏁"),
        "",
        f"*{_esc(tp_label)}* · {_price(tp_price)}{r_str}",
    ]
    if partial:
        body.append(f"Partial closed: {_pct(partial)}")
    if ctx.get("moved_to_be"):
        body.append("Stop moved to breakeven")
    elif level >= 1 and sig.current_stop != sig.stop_loss:
        body.append(f"Current stop: {_price(sig.current_stop)}")
    if level == 3 and ctx.get("total_r") is not None:
        total_r = float(ctx["total_r"])
        body.append(f"Total: *{_esc(f'{total_r:+.2f}R')}*")
    body += ["", _footer(sig, mode)]
    return body


def _render_tp1(sig: CanonicalSignal, ctx: dict, mode: str) -> list[str]:
    return _render_tp_hit(sig, ctx, mode, 1)


def _render_tp2(sig: CanonicalSignal, ctx: dict, mode: str) -> list[str]:
    return _render_tp_hit(sig, ctx, mode, 2)


def _render_final(sig: CanonicalSignal, ctx: dict, mode: str) -> list[str]:
    return _render_tp_hit(sig, ctx, mode, 3)


def _render_breakeven(sig: CanonicalSignal, ctx: dict, mode: str) -> list[str]:
    body = [
        _header(sig, "BREAKEVEN MOVED", ctx, emoji="🛡️"),
        "",
        f"Stop moved to: *{_price(sig.current_stop)}* \\(entry\\)",
        "Trade now risk\\-free\\.",
    ]
    body += ["", _footer(sig, mode)]
    return body


def _render_trailing(sig: CanonicalSignal, ctx: dict, mode: str) -> list[str]:
    body = [
        _header(sig, "TRAILING STOP", ctx, emoji="📈"),
        "",
        f"Trailing stop: *{_price(sig.current_stop)}*",
    ]
    if ctx.get("mfe"):
        body.append(f"MFE: {_pips(ctx['mfe'])} pts")
    if ctx.get("unrealized_r") is not None:
        unrealized = float(ctx["unrealized_r"])
        body.append(f"Unrealized: *{_esc(f'{unrealized:+.2f}R')}*")
    body += ["", _footer(sig, mode)]
    return body


def _render_stop(sig: CanonicalSignal, ctx: dict, mode: str) -> list[str]:
    body = [
        _header(sig, "STOP HIT", ctx, emoji="🛑"),
        "",
        f"Stopped at: *{_price(ctx.get('stop_price') or sig.current_stop)}*",
    ]
    r_realized = ctx.get("r_realized") if ctx.get("r_realized") is not None else sig.r_realized
    if r_realized is not None:
        body.append(f"Realized: *{_esc(f'{r_realized:+.2f}R')}*")
    if ctx.get("mfe"):
        body.append(f"MFE before stop: {_pips(ctx['mfe'])} pts")
    if mode != MODE_MINIMAL and ctx.get("stop_reason"):
        body.append(f"Reason: {_esc(ctx['stop_reason'])}")
    body += ["", _footer(sig, mode)]
    return body


def _render_invalidated(sig: CanonicalSignal, ctx: dict, mode: str) -> list[str]:
    body = [
        _header(sig, "SETUP INVALIDATED", ctx, emoji="❌"),
        "",
        f"Reason: {_esc(ctx.get('reason') or sig.invalidation or '—')}",
    ]
    if ctx.get("trigger_price"):
        body.append(f"Trigger price: {_price(ctx['trigger_price'])}")
    body += ["", _footer(sig, mode)]
    return body


def _render_expired(sig: CanonicalSignal, ctx: dict, mode: str) -> list[str]:
    body = [
        _header(sig, "SETUP EXPIRED", ctx, emoji="⏱️"),
        "",
        f"Valid until: {_to_eat(sig.valid_until)} EAT",
        "No trigger before window closed\\.",
    ]
    if mode == MODE_DETAILED and sig.rationale:
        body += ["", f"_{_esc(sig.rationale)}_"]
    body += ["", _footer(sig, mode)]
    return body


def _render_high_confluence(sig: CanonicalSignal, ctx: dict, mode: str) -> list[str]:
    """Aggregated high-confluence alert. Suppresses individual strategy alerts."""
    body = [
        _header(sig, "HIGH CONFLUENCE", ctx, emoji="⭐"),
        "",
        f"Entry: *{_entry_zone(sig)}*",
        f"Stop:  *{_price(sig.stop_loss)}*",
    ]
    tps = _targets_block(sig, mode)
    if tps:
        body += ["*Targets:*", *tps]
    body += _confluence_block(sig, mode)
    if mode != MODE_MINIMAL and sig.rationale:
        body += ["", f"_{_esc(sig.rationale)}_"]
    body += ["", _footer(sig, mode)]
    return body


def _render_post_trade_review(sig: CanonicalSignal, ctx: dict, mode: str) -> list[str]:
    """Sent ~24h after a trade closes. What worked, what didn't."""
    outcome = ctx.get("outcome") or ("win" if (sig.r_realized or 0) > 0 else "loss")
    r = sig.r_realized if sig.r_realized is not None else ctx.get("r_realized")
    body = [
        _header(sig, "POST-TRADE REVIEW", ctx, emoji="📓"),
        "",
        f"Outcome: *{_esc(outcome.upper())}* · {_esc(f'{r:+.2f}R') if r is not None else '—'}",
    ]
    if ctx.get("what_worked"):
        body += ["", "*What worked:*"]
        for item in ctx["what_worked"][:4]:
            body.append(f"  ✓ {_esc(item)}")
    if ctx.get("what_missed"):
        body += ["", "*What to improve:*"]
        for item in ctx["what_missed"][:4]:
            body.append(f"  ✗ {_esc(item)}")
    if ctx.get("lesson"):
        body += ["", f"_{_esc(ctx['lesson'])}_"]
    body += ["", _footer(sig, mode)]
    return body


def _render_end_of_session(sig: CanonicalSignal, ctx: dict, mode: str) -> list[str]:
    """End-of-session recap. `sig` here is a placeholder aggregated row."""
    body = [
        _header(sig, f"{ctx.get('session_name', 'SESSION')} RECAP", ctx, emoji="📅"),
        "",
        f"Signals fired: *{_esc(ctx.get('n_fired', 0))}*",
        f"Wins / Losses: *{_esc(ctx.get('wins', 0))}* / *{_esc(ctx.get('losses', 0))}*",
    ]
    if ctx.get("total_r") is not None:
        total_r = float(ctx["total_r"])
        body.append(f"Total: *{_esc(f'{total_r:+.2f}R')}*")
    if ctx.get("headline"):
        body += ["", f"_{_esc(ctx['headline'])}_"]
    body += ["", _footer(sig, mode)]
    return body


# ── Dispatch ─────────────────────────────────────────────────────────────────

_RENDERERS = {
    "monitoring":         _render_monitoring,
    "actionable":         _render_actionable,
    "entry_triggered":    _render_entry_triggered,
    "tp1_hit":            _render_tp1,
    "tp2_hit":            _render_tp2,
    "final_target":       _render_final,
    "breakeven":          _render_breakeven,
    "trailing":           _render_trailing,
    "stop_hit":           _render_stop,
    "invalidated":        _render_invalidated,
    "expired":            _render_expired,
    "high_confluence":    _render_high_confluence,
    "post_trade_review":  _render_post_trade_review,
    "end_of_session":     _render_end_of_session,
}


def render(
    message_type: str,
    signal: CanonicalSignal,
    *,
    extra: Optional[dict] = None,
    mode: str = MODE_STANDARD,
    now: Optional[datetime] = None,
) -> dict:
    """Public entry point. Returns a payload dict ready for the telegram
    client: {"text", "parse_mode", "message_type", "message_fingerprint",
    "bytes"}."""
    if mode not in ALL_MODES:
        raise ValueError(f"invalid mode {mode!r}")
    renderer = _RENDERERS.get(message_type)
    if renderer is None:
        raise KeyError(f"no renderer for message_type={message_type!r}")

    extra = dict(extra or {})
    if now is not None:
        extra["__now"] = now
    lines = renderer(signal, extra, mode)
    text = _truncate("\n".join(lines))

    # Fingerprint keys the state + any key prices we included
    key_prices = {
        "entry_low":  signal.entry_zone_low,
        "entry_high": signal.entry_zone_high,
        "stop":       signal.current_stop,
    }
    if "fill_price" in extra and extra["fill_price"] is not None:
        key_prices["fill"] = float(extra["fill_price"])
    if "stop_price" in extra and extra["stop_price"] is not None:
        key_prices["stop_hit"] = float(extra["stop_price"])
    if "tp_price" in extra and extra["tp_price"] is not None:
        key_prices["tp"] = float(extra["tp_price"])

    fp = message_fingerprint(
        signal_id=signal.signal_id,
        new_state=signal.state,
        key_prices=key_prices,
    )

    return {
        "text":                 text,
        "parse_mode":           "MarkdownV2",
        "message_type":         message_type,
        "message_fingerprint":  fp,
        "bytes":                len(text.encode("utf-8")),
    }


# ── Public re-exports for tests / router ─────────────────────────────────────

__all__ = [
    "render",
    "MODE_MINIMAL", "MODE_STANDARD", "MODE_DETAILED", "ALL_MODES",
    "TELEGRAM_MAX_BYTES",
    "_esc",  # exported for testing only
]
