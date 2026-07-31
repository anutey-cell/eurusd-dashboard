"""
Data-Freshness Sentinel
========================

Periodic check that `historical_candles` isn't quietly aging out.
The scanner runs on live TwelveData ticks so a stale historical
table doesn't fail loud — it fails silent (lookback features drift
to stale values, HTF alignment misreads, ICT structure lags).

Rule: if MAX(candle_time) for XAU/USD H1 is more than `staleness_h`
hours behind now DURING market hours, fire a Telegram alert (one
per staleness episode, deduped by day).

Skipped over the weekend (Sat + Sun before Sunday reopen) — market
closed, no fresh candles expected.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone, timedelta
from typing import Optional

from sqlalchemy import text

log = logging.getLogger(__name__)


DEFAULT_STALE_H = 6              # H1 candle > 6h old = stale
_ALERT_STATE = {"date": None, "fired_for_tf": set()}


def _last_candle_at(db, instrument: str, tf: str) -> Optional[datetime]:
    """Returns the most recent candle_time for (instrument, tf), UTC-aware."""
    row = db.execute(text(
        "SELECT MAX(candle_time) FROM historical_candles "
        "WHERE instrument = :i AND timeframe = :t"
    ), {"i": instrument, "t": tf}).fetchone()
    ts = row[0] if row else None
    if ts is None:
        return None
    if isinstance(ts, str):
        for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S"):
            try:
                ts = datetime.strptime(ts.split("+")[0], fmt); break
            except ValueError:
                continue
    if isinstance(ts, datetime):
        return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)
    return None


def _is_weekend_closed(now: datetime) -> bool:
    """Same window the strategist uses to skip weekend logic."""
    wd, h = now.weekday(), now.hour
    if wd == 5:                        # Saturday all day
        return True
    if wd == 6 and h < 22:             # Sunday before reopen (22:00 UTC)
        return True
    if wd == 4 and h >= 21:            # Friday after close
        return True
    return False


def check_freshness(db, *, instrument: str = "XAU/USD",
                     timeframes: tuple = ("M15", "H1", "H4"),
                     staleness_h: int = DEFAULT_STALE_H,
                     now: Optional[datetime] = None) -> dict:
    """
    Returns {"stale": [tf, ...], "fresh": [tf, ...], "details": {...}}.
    No side effects — caller decides whether to alert.
    """
    now = now or datetime.now(timezone.utc)
    if _is_weekend_closed(now):
        return {"stale": [], "fresh": list(timeframes), "weekend": True,
                "details": {tf: "weekend-closed" for tf in timeframes}}

    stale, fresh, details = [], [], {}
    for tf in timeframes:
        latest = _last_candle_at(db, instrument, tf)
        if latest is None:
            stale.append(tf)
            details[tf] = "no candles at all"
            continue
        age_h = (now - latest).total_seconds() / 3600
        details[tf] = f"latest {latest.isoformat()} · age {age_h:.1f}h"
        (stale if age_h > staleness_h else fresh).append(tf)
    return {"stale": stale, "fresh": fresh, "weekend": False, "details": details}


def maybe_alert(db, client=None) -> Optional[dict]:
    """
    Called by scheduler on a periodic tick (e.g. every 30 min).
    Alerts via Telegram once per calendar day per stale timeframe.
    Returns the freshness check dict or None if nothing to do.
    """
    now = datetime.now(timezone.utc)
    result = check_freshness(db, now=now)

    if result.get("weekend") or not result["stale"]:
        return result

    # Dedupe per day per timeframe
    today = now.strftime("%Y-%m-%d")
    if _ALERT_STATE["date"] != today:
        _ALERT_STATE["date"] = today
        _ALERT_STATE["fired_for_tf"] = set()

    to_alert = [tf for tf in result["stale"] if tf not in _ALERT_STATE["fired_for_tf"]]
    if not to_alert:
        return result

    # Send Telegram alert
    try:
        from services.telegram_templates import _esc
        text = ("*[ALERT] DATA-FEED STALE*\n"
                f"Instrument: {_esc('XAU/USD')}\n"
                f"Stale timeframes: {', '.join(_esc(t) for t in to_alert)}\n\n"
                + "\n".join(f"  • {_esc(tf)}: {_esc(result['details'].get(tf, ''))}"
                              for tf in to_alert)
                + "\n\nEngine will keep running on live ticks but any "
                  "lookback feature is on stale data\\.")
        if client is None:
            from services.telegram_client import get_client
            client = get_client()
        # Direct client._post_message bypasses canonical dedup — sentinel is one-shot
        chat_id = getattr(__import__("config", fromlist=["settings"]).settings,
                           "telegram_chat_id", "")
        if chat_id and not client.dry_run:
            client._post_message(chat_id, text, parse_mode="MarkdownV2")
        log.warning("[freshness] alerted for stale timeframes: %s", to_alert)
        for tf in to_alert:
            _ALERT_STATE["fired_for_tf"].add(tf)
    except Exception as exc:
        log.warning("[freshness] alert failed: %s", exc)

    return result


__all__ = ["check_freshness", "maybe_alert"]
