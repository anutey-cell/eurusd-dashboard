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

# Bar duration in minutes per timeframe — every provider (MT5, TV, Yahoo, TD)
# serves bars only AFTER they close. The "latest" bar's OPEN time can therefore
# be up to 2 * duration behind wall-clock in normal operation:
#   - 1 duration for the currently-forming (unavailable) bar
#   - 1 duration since the previous bar closed
# Plus provider ingest lag. STALE only when we've missed a CLOSED bar arrival.
BAR_DURATION_MIN: dict[str, int] = {
    "M1":  1,
    "M5":  5,
    "M15": 15,
    "H1":  60,
    "H4":  240,
    "D1":  1440,
}

# Ingest-lag tolerance (how long we're willing to wait after a bar closes
# before considering it overdue). MT5 push is fast; TV/Yahoo can lag a min or two.
LAG_TOLERANCE_MIN_BY_TF: dict[str, int] = {
    "M1":  2,
    "M5":  5,
    "M15": 5,
    "H1":  15,
    "H4":  30,
    "D1":  120,
}

# Derived stale thresholds: age (measured from bar OPEN) beyond which we
# conclude the NEXT expected bar hasn't arrived on time. Formula:
#     stale = age > (2 * bar_duration) + lag_tolerance
# Rationale: normal max age at any moment = (bar duration for the currently
# forming bar) + (bar duration since previous bar closed) = 2 * duration.
# Add lag tolerance for the ingest window. Anything past that means we missed
# a bar that SHOULD be available.
STALENESS_MIN_BY_TF: dict[str, int] = {
    tf: 2 * BAR_DURATION_MIN[tf] + LAG_TOLERANCE_MIN_BY_TF[tf]
    for tf in BAR_DURATION_MIN
}
# Result:
#   M1  =  4 min       M5  = 15 min       M15 = 35 min
#   H1  = 135 min      H4  = 510 min      D1  = 3000 min (~50h)
_ALERT_STATE = {"date": None, "fired_for_tf": set()}

# Reminder cadence: after the first stale-alert, re-alert every N seconds
# while the stale condition persists so the operator gets nudged to fix.
# Reset when data goes fresh again.
_LAST_ALERT_STATE: dict = {
    "stale_key":       "",       # fingerprint of the current stale set
    "first_sent_at":   0.0,      # unix ts of the first alert in this outage
    "last_sent_at":    0.0,      # unix ts of the most recent alert
}
_REMINDER_INTERVAL_S = 2 * 60 * 60   # 2 hours


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


def _send_operator_alert(text: str) -> bool:
    """
    Direct httpx POST to Telegram, bypassing the canonical client's dry-run.
    Freshness alerts are operational infrastructure — they must fire regardless
    of shadow / dry-run flags. Returns True on success.
    """
    try:
        import httpx
        from config import settings
        token = getattr(settings, "telegram_bot_token", None)
        chat_id = getattr(settings, "telegram_chat_id", None)
        if not (token and chat_id):
            log.warning("[freshness] cannot alert — missing bot_token or chat_id")
            return False
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        r = httpx.post(url, json={
            "chat_id":                  chat_id,
            "text":                     text,
            "disable_web_page_preview": True,
        }, timeout=10.0)
        if not r.is_success:
            log.warning("[freshness] Telegram send failed status=%s body=%s",
                        r.status_code, r.text[:200])
            return False
        return True
    except Exception as exc:
        log.warning("[freshness] Telegram send error: %s", exc)
        return False


def _stale_fingerprint(stale: list, details: dict) -> str:
    """Compact key so re-alerts don't fire while the same set stays stale."""
    parts = []
    for tf in sorted(stale):
        info = details.get(tf) or {}
        parts.append(f"{tf}:{info.get('status', '?')}")
    return "|".join(parts)


def maybe_alert(db, client=None) -> Optional[dict]:
    """
    Called by scheduler on a periodic tick (default every 10 min).

    First alert fires immediately on stale detection. While the same stale
    set persists, a reminder fires every _REMINDER_INTERVAL_S (2h) so the
    operator gets nudged to fix. When data goes fresh again, the state
    resets so the next outage produces an immediate alert.

    Uses direct httpx (bypassing canonical dry-run) — freshness is
    operational infrastructure, not a signal notification.
    """
    import time as _time
    now = datetime.now(timezone.utc)
    result = check_freshness(db, now=now)

    # Reset alert state when data goes fresh (or over weekend when closure
    # expected). This way the next stale episode always gets its first alert.
    if result.get("weekend") or not result["stale"]:
        if _LAST_ALERT_STATE.get("stale_key"):
            log.info("[freshness] data recovered — clearing alert state")
            _LAST_ALERT_STATE["stale_key"]     = ""
            _LAST_ALERT_STATE["first_sent_at"] = 0.0
            _LAST_ALERT_STATE["last_sent_at"]  = 0.0
        return result

    stale_key = _stale_fingerprint(result["stale"], result.get("details", {}))
    now_ts = _time.time()
    prior_key    = _LAST_ALERT_STATE.get("stale_key", "")
    last_sent    = float(_LAST_ALERT_STATE.get("last_sent_at", 0.0))
    first_sent   = float(_LAST_ALERT_STATE.get("first_sent_at", 0.0))

    # Decide: send if new stale set, OR reminder interval elapsed
    should_send = False
    reason      = ""
    if stale_key != prior_key:
        should_send = True
        reason      = "new_stale_set"
    elif now_ts - last_sent > _REMINDER_INTERVAL_S:
        should_send = True
        reason      = "reminder"

    if not should_send:
        return result

    # Build the alert
    try:
        from services.candle_ingestion import get_last_ingest_error
        err = get_last_ingest_error() or {}
    except Exception:
        err = {}

    now_str    = now.strftime("%Y-%m-%d %H:%M:%S UTC")
    first_seen = ""
    if reason == "reminder" and first_sent:
        first_dt = datetime.fromtimestamp(first_sent, tz=timezone.utc)
        first_seen = first_dt.strftime("%Y-%m-%d %H:%M UTC")

    lines = []
    if reason == "reminder":
        lines.append(f"⚠️ DATA STILL STALE · XAU/USD  (reminder)")
        lines.append(f"First detected: {first_seen}")
    else:
        lines.append(f"🚨 DATA STALE · XAU/USD")
    lines.append(f"Now: {now_str}")
    lines.append("")
    lines.append("Stale timeframes:")
    for tf in sorted(result["stale"]):
        info = result["details"].get(tf, {})
        if isinstance(info, dict):
            latest = info.get("latest", "—")
            age    = info.get("age_min")
            thr    = info.get("threshold_min")
            status = info.get("status", "?")
            lines.append(f"  • {tf}: {status} · latest {latest} · "
                          f"age {age} min · threshold {thr} min")
        else:
            lines.append(f"  • {tf}: {info}")

    lines.append("")
    lines.append(f"Data-quality score: {result.get('data_quality_score', 0)}/100")

    if err.get("message"):
        lines.append("")
        lines.append("Last ingestion error:")
        lines.append(f"  [{err.get('tf','?')}] {err.get('message','')}")
        if err.get("at"):
            lines.append(f"  at: {err.get('at')}")

    # Actual per-TF provider state — replaces the old hardcoded TV wording
    try:
        from sqlalchemy import text as _sqltext
        from database import SessionLocal as _SL
        with _SL() as _db:
            provider_rows = _db.execute(_sqltext(
                "SELECT timeframe, source FROM historical_candles "
                "WHERE instrument='XAU/USD' AND timeframe IN ('M5','M15','H1','H4','D1') "
                "AND candle_time = (SELECT MAX(candle_time) FROM historical_candles h2 "
                "                    WHERE h2.instrument=historical_candles.instrument "
                "                    AND h2.timeframe=historical_candles.timeframe)"
            )).fetchall()
        provider_by_tf = {r[0]: r[1] for r in provider_rows}
    except Exception:
        provider_by_tf = {}

    lines.append("")
    lines.append("Providers serving latest bars:")
    for tf in sorted(BAR_DURATION_MIN.keys()):
        if tf in ("M1",):
            continue
        src = provider_by_tf.get(tf, "(none)")
        lines.append(f"  {tf}: {src}")

    lines.append("")
    lines.append("Action:")
    if "mt5" in provider_by_tf.values():
        lines.append("  - MT5 daemon push is active. If persistently stale, check laptop daemon logs")
        lines.append("    and MT5 terminal (open a chart for the stale TF to force server subscription).")
    else:
        lines.append("  - MT5 daemon not pushing. Restart mt5_bridge_daemon.py on laptop.")
    lines.append("  - Yahoo GC=F backup engages when TV empties.")
    lines.append("  - Transient drop (recovers in 1-2 cycles): no action needed.")
    lines.append("Engine will refuse to trade on stale data until it clears.")
    text = "\n".join(lines)

    sent = _send_operator_alert(text)
    if sent:
        _LAST_ALERT_STATE["stale_key"]    = stale_key
        _LAST_ALERT_STATE["last_sent_at"] = now_ts
        if reason == "new_stale_set" or not first_sent:
            _LAST_ALERT_STATE["first_sent_at"] = now_ts
        log.warning("[freshness] Telegram alert sent (%s): stale=%s",
                    reason, result["stale"])
    else:
        log.warning("[freshness] Telegram alert BUILD OK but send FAILED — "
                    "stale=%s", result["stale"])

    return result


__all__ = ["check_freshness", "maybe_alert"]
