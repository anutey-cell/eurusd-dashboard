"""
Weekend XAUUSD Newsletters
==========================

Two structured newsletters delivered during the forex weekend close:

  • Saturday 09:00 GMT — RECAP: what happened, engine performance, lessons
  • Sunday   18:00 GMT — FORECAST: week-ahead key levels, events, playbook

Newsletter style (longer-form, narrative paragraphs + tables) rather than
the terse signal-block format used during trading hours.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy.orm import Session

log = logging.getLogger(__name__)


# ────────────────────────────────────────────────────────────────────────
# Saturday recap
# ────────────────────────────────────────────────────────────────────────

def build_saturday_recap(db: Session) -> str:
    """
    Compose the Saturday weekly recap newsletter. Covers the calendar week
    just closed (Mon 00:00 → Fri 21:00 UTC). Pulls from:
      • candles for price action recap
      • strategist_verdicts for engine performance
      • learnings module for buckets
    """
    from db_models import StrategistVerdict
    from sqlalchemy import func

    now = datetime.now(timezone.utc)
    # The week we're recapping = Mon 00:00 UTC of the current week
    monday = (now - timedelta(days=now.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
    friday_close = monday + timedelta(days=4, hours=21)
    week_label = f"{monday.strftime('%b %d')} — {(monday + timedelta(days=4)).strftime('%b %d, %Y')}"

    # ── 1. Price action recap ──────────────────────────────────────────
    price_block = _build_price_recap(monday, friday_close)

    # ── 2. Engine activity ─────────────────────────────────────────────
    verdicts = (db.query(StrategistVerdict)
                  .filter(StrategistVerdict.created_at >= monday)
                  .filter(StrategistVerdict.created_at <= friday_close)
                  .all())
    total_v       = len(verdicts)
    actionable    = [v for v in verdicts if v.decision in ("BUY", "SELL")]
    closed_trades = [v for v in actionable if v.result in ("WIN", "LOSS", "BREAKEVEN")]
    wins   = sum(1 for v in closed_trades if v.result == "WIN")
    losses = sum(1 for v in closed_trades if v.result == "LOSS")
    bes    = sum(1 for v in closed_trades if v.result == "BREAKEVEN")
    wr = (100.0 * wins / len(closed_trades)) if closed_trades else 0.0

    direction_split = {"BUY": 0, "SELL": 0}
    for v in actionable:
        direction_split[v.decision] = direction_split.get(v.decision, 0) + 1

    # Conditions distribution
    by_cp = {3: 0, 4: 0, 5: 0}
    for v in actionable:
        cp = v.conditions_passed or 0
        if cp in by_cp:
            by_cp[cp] += 1

    # ── 3. Top winners + losers (from learnings module) ───────────────
    from services.learnings import build_learnings
    learnings = build_learnings(db, window_days=7)
    winners = learnings.get("top_winners") or []
    losers  = learnings.get("top_losers") or []

    # ── 4. Lessons (from calibration notes) ────────────────────────────
    notes = learnings.get("calibration_notes") or []

    # ── 5. Macro context that drove the week ──────────────────────────
    macro = _build_macro_recap()

    # ── Compose ────────────────────────────────────────────────────────
    lines = [
        "📰 XAUUSD WEEKLY RECAP",
        "━━━━━━━━━━━━━━━━━━━━━",
        f"📅 {week_label}",
        "",
        "📊 PRICE ACTION",
        price_block,
        "",
        "🤖 ENGINE ACTIVITY",
        f"  • Verdicts computed:  {total_v}",
        f"  • Actionable signals: {len(actionable)}  ({direction_split.get('BUY',0)} BUY, {direction_split.get('SELL',0)} SELL)",
        f"  • Quality breakdown:  3/5={by_cp[3]}  4/5={by_cp[4]}  5/5={by_cp[5]}",
        f"  • Closed trades:      {len(closed_trades)}  ({wins}W / {losses}L / {bes}BE)",
        f"  • Win rate:           {wr:.1f}%",
    ]

    if learnings.get("overall"):
        ov = learnings["overall"]
        lines.append(f"  • Expectancy:         {ov.get('expectancy_r', 0):+.2f}R")
        lines.append(f"  • Avg MFE / MAE:      +{ov.get('avg_mfe',0)} pts / -{ov.get('avg_mae',0)} pts")

    if winners:
        lines += ["", "🏆 TOP WINNERS"]
        for w in winners[:3]:
            lines.append(f"  • {w.get('decision')} {w.get('conditionsPassed')}/5  "
                         f"→  {w.get('result')}  {w.get('rMultiple',0):+.2f}R  "
                         f"({w.get('session','')})")

    if losers:
        lines += ["", "📉 TOP LOSERS"]
        for l in losers[:3]:
            lines.append(f"  • {l.get('decision')} {l.get('conditionsPassed')}/5  "
                         f"→  {l.get('result')}  {l.get('rMultiple',0):+.2f}R  "
                         f"({l.get('session','')})")
            note = l.get("improvementNote")
            if note: lines.append(f"      what went wrong: {note[:100]}")

    if notes:
        lines += ["", "🔍 CALIBRATION NOTES"]
        for n in notes:
            lines.append(f"  • {n}")

    lines += ["", "🌍 MACRO CONTEXT", macro]

    lines += [
        "",
        "🧭 LOOKING AHEAD",
        "  Sunday 18:00 GMT — week-ahead forecast newsletter incoming",
        "  Markets reopen Sunday 22:00 GMT",
        "",
        "━━━━━━━━━━━━━━━━━━━━━",
        "Capital preservation > revenge. Demo only · 0.01 lot.",
    ]
    return "\n".join(lines)


def _build_price_recap(monday: datetime, friday_close: datetime) -> str:
    """Compose the price-action paragraph from the week's H1 candles."""
    try:
        from data.candles import get_candles
        h1 = get_candles(interval="H1", limit=200, pair="xauusd")
        if not h1 or not h1.candles:
            return "  Price data unavailable for the recap window."
        week_bars = [
            c for c in h1.candles
            if monday <= (c.time if c.time.tzinfo else c.time.replace(tzinfo=timezone.utc)) <= friday_close
        ]
        if not week_bars:
            return "  No bars closed within the recap window."
        open_p  = week_bars[0].open
        close_p = week_bars[-1].close
        high_p  = max(c.high for c in week_bars)
        low_p   = min(c.low  for c in week_bars)
        rng     = high_p - low_p
        net     = close_p - open_p
        net_pct = (net / open_p) * 100 if open_p else 0
        arrow   = "▲" if net > 0 else "▼" if net < 0 else "▬"
        return (
            f"  • Open Mon:           ${open_p:.2f}\n"
            f"  • Close Fri:          ${close_p:.2f}  {arrow} {abs(net_pct):.2f}% (${net:+.2f})\n"
            f"  • Week high:          ${high_p:.2f}\n"
            f"  • Week low:           ${low_p:.2f}\n"
            f"  • Range:              ${rng:.2f}"
        )
    except Exception as exc:
        log.warning("[recap] price block failed: %s", exc)
        return f"  Price block error: {exc}"


def _build_macro_recap() -> str:
    """Current macro snapshot — DXY / yields / news risk."""
    try:
        from services.strategist import make_decision
        from database import SessionLocal
        with SessionLocal() as db:
            v = make_decision(db)
        mc = v.get("macro_context") or {}
        return (
            f"  • Gold macro bias: {mc.get('gold_macro_bias', '—')}\n"
            f"  • DXY direction:   {_macro_word(mc.get('dxy_bias'))}\n"
            f"  • Yields:          {_macro_word(mc.get('yields_bias'))}\n"
            f"  • Macro alignment: {mc.get('macro_alignment', '—')}"
        )
    except Exception as exc:
        log.warning("[recap] macro block failed: %s", exc)
        return "  Macro block unavailable."


def _macro_word(raw: str | None) -> str:
    """Translate predictor BUY/SELL to plain English."""
    if raw == "BUY":  return "supportive of gold (USD weakening / yields falling)"
    if raw == "SELL": return "pressuring gold (USD strengthening / yields rising)"
    return "neutral"


# ────────────────────────────────────────────────────────────────────────
# Sunday forecast
# ────────────────────────────────────────────────────────────────────────

def build_sunday_forecast(db: Session) -> str:
    """
    Compose the Sunday week-ahead forecast. Covers Mon-Fri ahead with:
      • Friday's close + current weekend price (if any)
      • Key levels: last week's H/L, prev day H/L, round numbers
      • Upcoming high-impact USD events
      • Current macro positioning
      • Engine's read going into Monday
      • Specific BUY / SELL triggers + risk factors
    """
    now = datetime.now(timezone.utc)
    # Next week opens Sunday 22:00 UTC; the week we're forecasting:
    next_monday = now + timedelta(days=(7 - now.weekday()) % 7)
    if now.weekday() == 6:
        next_monday = now + timedelta(days=1)
    next_monday = next_monday.replace(hour=0, minute=0, second=0, microsecond=0)
    next_friday = next_monday + timedelta(days=4)
    week_label = f"{next_monday.strftime('%b %d')} — {next_friday.strftime('%b %d, %Y')}"

    # ── 1. Current price state ─────────────────────────────────────────
    price_state = _build_current_price_block()

    # ── 2. Key levels for next week ────────────────────────────────────
    levels = _build_key_levels()

    # ── 3. Upcoming USD high-impact events ─────────────────────────────
    events = _build_upcoming_events(next_monday, next_friday)

    # ── 4. Macro positioning ───────────────────────────────────────────
    macro = _build_macro_recap()

    # ── 5. Engine read + triggers ──────────────────────────────────────
    engine_read = _build_engine_read(db)

    lines = [
        "📅 XAUUSD WEEK-AHEAD FORECAST",
        "━━━━━━━━━━━━━━━━━━━━━",
        f"🗓 Trading week: {week_label}",
        f"🔔 Markets reopen: Sunday 22:00 GMT",
        "",
        "💰 CURRENT PRICE STATE",
        price_state,
        "",
        "📌 KEY LEVELS FOR THE WEEK",
        levels,
        "",
        "📆 HIGH-IMPACT USD EVENTS",
        events,
        "",
        "🌍 MACRO POSITIONING",
        macro,
        "",
        "🤖 ENGINE'S READ",
        engine_read,
        "",
        "🧠 TRADING-WEEK PLAYBOOK",
        "  📅 MONDAY = OBSERVATION ONLY. Signals fire so you can study setups",
        "      and assess weekly directional bias — NO MT5 orders are placed.",
        "      Trading resumes Tuesday 00:00 UTC.",
        "  • Stick to London (07:00-11:00 UTC) + NY-open (13:00-16:00 UTC) killzones",
        "  • 4/5 or better setups → demo execution (0.01 lot)",
        "  • 3/5 setups → watchlist only, no exec",
        "  • Hard pause 60 min before / 30 min after high-impact USD events",
        "  • If session opens with a DXY surprise → re-evaluate macro alignment",
        "",
        "━━━━━━━━━━━━━━━━━━━━━",
        "Plan the trade. Trade the plan. Demo only · 0.01 lot.",
    ]
    return "\n".join(lines)


def _build_current_price_block() -> str:
    try:
        from data.candles import get_candles
        m5 = get_candles(interval="M5", limit=300, pair="xauusd")
        if not m5 or not m5.candles:
            return "  Price feed unavailable."
        last = m5.candles[-1]
        last_close_ts = last.time if hasattr(last, "time") else None
        # Use the most recent H1 candle for "Friday close" approximation
        h1 = get_candles(interval="H1", limit=120, pair="xauusd")
        friday_close = h1.candles[-1].close if h1 and h1.candles else last.close
        return (
            f"  • Last close:         ${last.close:.2f}\n"
            f"  • Reference (latest H1): ${friday_close:.2f}\n"
            f"  • Source:             {m5.source}\n"
            f"  • Timestamp:          {last_close_ts.isoformat() if last_close_ts else '—'}"
        )
    except Exception as exc:
        return f"  Price block error: {exc}"


def _sanitize_d1_bars(d1_bars, mad_factor: float = 3.0, window: int = 20) -> list:
    """
    Drop bars whose high or low deviates more than mad_factor × MAD from
    the median close, computed over the RECENT window only. Catches
    data-feed glitches like the $5602 spike on 2026-01-28 that bled into
    a current-regime ~$4500 sample.

    Two principles:
      1. Window-restricted — only the last `window` bars define the
         "normal" range, so old regime-shift bars don't widen the threshold.
      2. MAD-based — robust to the outliers being filtered (std-dev would
         let a single extreme tick poison its own detection threshold).

    Tighter factor (3 vs 4) since restricting window already narrows MAD.
    """
    bars = list(d1_bars)
    if len(bars) < 5:
        return bars

    # Sample for threshold computation = most recent `window` bars
    recent = bars[-window:] if len(bars) > window else bars
    closes_sorted = sorted(c.close for c in recent)
    median = closes_sorted[len(closes_sorted) // 2]
    if median <= 0:
        return bars

    deviations = sorted(abs(c.close - median) for c in recent)
    mad = deviations[len(deviations) // 2] or (median * 0.01)
    threshold = mad_factor * mad

    # Apply filter to ALL bars (not just window) — older spike bars get dropped
    clean = [c for c in bars
             if abs(c.high - median) <= threshold
             and abs(c.low  - median) <= threshold]
    rejected = len(bars) - len(clean)
    if rejected:
        log.info(
            "[levels] dropped %d outlier D1 bar(s) (median=%.2f, MAD=%.2f, threshold=%.2f)",
            rejected, median, mad, threshold,
        )
    return clean if len(clean) >= 3 else bars


def _build_key_levels() -> str:
    """Compute next week's reference levels from D1 + H4 history."""
    try:
        from data.candles import get_candles
        import math
        d1 = get_candles(interval="D1", limit=30, pair="xauusd")
        if not d1 or not d1.candles:
            return "  Level data unavailable."

        # get_candles can return more than `limit` due to provider quirks.
        # Slice to last 30 trading days BEFORE sanitizing so the "30-day"
        # claim is honest and the MAD threshold reflects the recent regime.
        recent_30 = d1.candles[-30:]
        clean_d1 = _sanitize_d1_bars(recent_30)

        # Last week's H/L (last 5 D1 bars)
        last_week = clean_d1[-5:]
        week_high = max(c.high for c in last_week)
        week_low  = min(c.low  for c in last_week)
        # Last 30 trading days range (post-sanitization)
        month_high = max(c.high for c in clean_d1)
        month_low  = min(c.low  for c in clean_d1)
        # Friday's H/L (last full D1 bar)
        friday    = clean_d1[-1]
        # Round numbers around the current price
        cur = friday.close
        rn_below = math.floor(cur / 50) * 50
        rounds = sorted({rn_below - 100, rn_below - 50, rn_below, rn_below + 50, rn_below + 100})

        return (
            f"  🔴 Resistance:\n"
            f"      Last week high:   ${week_high:.2f}\n"
            f"      30-day high:      ${month_high:.2f}\n"
            f"      Friday high:      ${friday.high:.2f}\n"
            f"  🟢 Support:\n"
            f"      Last week low:    ${week_low:.2f}\n"
            f"      30-day low:       ${month_low:.2f}\n"
            f"      Friday low:       ${friday.low:.2f}\n"
            f"  🎯 Round numbers:    " + "  ·  ".join(f"${r}" for r in rounds)
        )
    except Exception as exc:
        return f"  Levels block error: {exc}"


def _build_upcoming_events(monday: datetime, friday: datetime) -> str:
    """Pull high-impact USD events from the calendar for next week."""
    try:
        from data.calendar import get_calendar
        all_events = []
        for delta in range(7):
            d_str = (monday + timedelta(days=delta)).strftime("%Y-%m-%d")
            try:
                day = get_calendar(date=d_str, high_impact_only=True)
                all_events.extend(day.events or [])
            except Exception as exc:
                log.debug("[forecast] calendar day %s failed: %s", d_str, exc)
                continue
        if not all_events:
            return "  No high-impact USD events scheduled for the week."

        lines = []
        for e in sorted(all_events, key=lambda x: x.time):
            t = e.time if isinstance(e.time, datetime) else None
            if t is None: continue
            t = t if t.tzinfo else t.replace(tzinfo=timezone.utc)
            lines.append(f"  • {t.strftime('%a %m-%d %H:%MZ')}  {e.currency:3}  {e.event[:60]}")
        return "\n".join(lines) if lines else "  No high-impact USD events scheduled."
    except Exception as exc:
        return f"  Events block error: {exc}"


def _build_engine_read(db: Session) -> str:
    """Current strategist verdict + next-trigger conditions."""
    try:
        from services.strategist import make_decision
        v = make_decision(db)
        nt = v.get("next_trigger") or {}
        return (
            f"  • Current decision:     {v.get('decision')}  ({v.get('conditions_passed')}/5 conditions)\n"
            f"  • Market state:         {v.get('market_state', '—')}\n"
            f"  • HTF alignment:        {v.get('tf_alignment_label', '—')}\n"
            f"  • Improvement note:     {v.get('improvement_note', '—')[:140]}\n"
            f"\n"
            f"  🟢 BUY unlock:  {(nt.get('long_trigger') or 'wait for confirmation')[:120]}\n"
            f"  🔴 SELL unlock: {(nt.get('short_trigger') or 'wait for confirmation')[:120]}"
        )
    except Exception as exc:
        return f"  Engine block error: {exc}"
