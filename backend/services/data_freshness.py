"""
Data-Freshness Sentinel
========================

Periodic check that `historical_candles` isn't quietly aging out.
The scanner runs on live TwelveData ticks so a stale historical
table doesn't fail loud — it fails silent (lookback features drift
to stale values, HTF alignment misreads, ICT structure lags).

Freshness remains an internal control used by the actionability gate and
health diagnostics. It does NOT emit Telegram health notifications.

Skipped over the weekend (Sat + Sun before Sunday reopen) — market
closed, no fresh candles expected.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
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
        raw = ts
        try:
            ts = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S"):
                try:
                    ts = datetime.strptime(raw.split("+")[0], fmt)
                    break
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

    Per-TF thresholds come from STALENESS_MIN_BY_TF.
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
    """Run the periodic freshness check without sending Telegram health alerts.

    The scheduler still calls this function, so provider continuity and stale
    timeframes remain visible in logs/health endpoints. Telegram is intentionally
    reserved for trading/setup/lifecycle notifications; `🚨 DATA STALE` and
    `⚠️ DATA STILL STALE` messages are suppressed by policy.

    Actionable BUY/SELL signals continue to fail closed separately through
    `services.actionability_gate` when core M5/M15/H1 data is stale.
    """
    result = check_freshness(db)
    if result.get("stale") and not result.get("weekend"):
        log.warning(
            "[freshness] STALE timeframes (Telegram health alert disabled): %s",
            result.get("stale"),
        )
    return result


__all__ = ["check_freshness", "maybe_alert"]
