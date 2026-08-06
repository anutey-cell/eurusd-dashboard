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


DEFAULT_STALE_H = 6              # legacy — kept for back-compat callers

# Per-timeframe staleness thresholds in minutes (per the directional
# intelligence brief). Beyond these, the timeframe is degraded from the
# data_quality_score AND excluded from any strategy that reads it.
STALENESS_MIN_BY_TF: dict[str, int] = {
    "M1":  3,
    "M5":  10,
    "M15": 20,
    "H1":  70,
    "H4":  300,       # 5 hours
    "D1":  1560,      # 26 hours
}
_ALERT_STATE = {"date": None, "fired_for_tf": set()}


def data_quality_score(details_by_tf: dict) -> int:
    """
    Compute a 0-100 data-quality score from a freshness-check result.

    Fresh (age within threshold)                 → contributes full weight
    Degraded (age up to 3× threshold)            → contributes half weight
    Stale beyond 3× threshold, or missing entirely → contributes 0

    Weights by tier (sum=100): M15 30, H1 30, H4 20, M5 10, D1 10.
    Timeframes not scored just don't count.
    """
    weights = {"M15": 30, "H1": 30, "H4": 20, "M5": 10, "D1": 10}
    total_weight = 0
    score = 0
    for tf, w in weights.items():
        info = (details_by_tf or {}).get(tf)
        if not info:
            continue
        total_weight += w
        age_min = info.get("age_min") if isinstance(info, dict) else None
        threshold = STALENESS_MIN_BY_TF.get(tf, 999999)
        if age_min is None:
            continue  # missing entirely → 0 contribution
        if age_min <= threshold:
            score += w
        elif age_min <= 3 * threshold:
            score += w // 2
    if total_weight == 0:
        return 0
    return int(round(score * 100 / total_weight))


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
                     timeframes: tuple = ("M5", "M15", "H1", "H4", "D1"),
                     staleness_h: Optional[int] = None,
                     now: Optional[datetime] = None) -> dict:
    """
    Returns {"stale": [...], "fresh": [...], "details": {tf: {age_min,threshold_min,latest,status}},
             "data_quality_score": int, "weekend": bool}.

    Per-TF thresholds from STALENESS_MIN_BY_TF (M15=20 min, H1=70, H4=300 …).
    Passing `staleness_h` overrides the per-TF thresholds (legacy back-compat).
    """
    now = now or datetime.now(timezone.utc)
    if _is_weekend_closed(now):
        details = {tf: {"status": "weekend-closed"} for tf in timeframes}
        return {"stale": [], "fresh": list(timeframes), "weekend": True,
                "details": details, "data_quality_score": 100}

    stale, fresh, details = [], [], {}
    for tf in timeframes:
        threshold_min = (staleness_h * 60) if staleness_h else STALENESS_MIN_BY_TF.get(tf, 60)
        latest = _last_candle_at(db, instrument, tf)
        if latest is None:
            stale.append(tf)
            details[tf] = {"status": "missing", "threshold_min": threshold_min}
            continue
        age_min = (now - latest).total_seconds() / 60
        info = {
            "latest": latest.isoformat(),
            "age_min": round(age_min, 1),
            "threshold_min": threshold_min,
            "status": "fresh" if age_min <= threshold_min else "stale",
        }
        details[tf] = info
        (stale if age_min > threshold_min else fresh).append(tf)

    return {"stale": stale, "fresh": fresh, "weekend": False,
            "details": details,
            "data_quality_score": data_quality_score(details)}


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
        def _fmt_detail(tf):
            info = result["details"].get(tf, {})
            if isinstance(info, str):
                return _esc(info)
            latest = info.get("latest", "—")
            age_m = info.get("age_min")
            thr = info.get("threshold_min")
            return _esc(f"latest {latest} · age {age_m}min · threshold {thr}min")
        text = ("*[ALERT] DATA\\-FEED STALE*\n"
                f"Instrument: {_esc('XAU/USD')}\n"
                f"Stale timeframes: {', '.join(_esc(t) for t in to_alert)}\n"
                f"Data\\-quality score: {result.get('data_quality_score', 0)}/100\n\n"
                + "\n".join(f"  • {_esc(tf)}: {_fmt_detail(tf)}"
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
