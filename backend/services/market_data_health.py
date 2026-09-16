"""Canonical market-data health snapshot for production diagnostics.

This module is deliberately read-only. It reports whether the XAU/USD signal
history is fresh, which provider supplied the latest bar for each timeframe,
and the most recent ingestion failure. It performs no external HTTP calls so
/api/v1/health stays fast and safe for Docker health checks.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import text


def _latest_source(db, instrument: str, timeframe: str) -> dict[str, Any]:
    row = db.execute(text(
        "SELECT candle_time, source FROM historical_candles "
        "WHERE instrument = :instrument AND timeframe = :timeframe "
        "ORDER BY candle_time DESC LIMIT 1"
    ), {"instrument": instrument, "timeframe": timeframe}).fetchone()
    if not row:
        return {"latest": None, "source": None}

    ts, source = row[0], row[1]
    if isinstance(ts, str):
        try:
            ts = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        except ValueError:
            pass
    if isinstance(ts, datetime) and ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)

    return {
        "latest": ts.isoformat() if isinstance(ts, datetime) else str(ts),
        "source": source,
    }


def market_data_health(db, instrument: str = "XAU/USD") -> dict[str, Any]:
    """Return one authoritative market-data health snapshot.

    Overall states:
      fresh    - all tracked timeframes inside their freshness thresholds
      degraded - some timeframes stale/missing, but at least one is still fresh
      stale    - no tracked timeframe is fresh
      closed   - expected weekend market closure
    """
    from services.data_freshness import check_freshness
    from services.candle_ingestion import get_last_ingest_error

    freshness = check_freshness(db, instrument=instrument)
    details = freshness.get("details", {})

    provider_by_timeframe: dict[str, Any] = {}
    for tf in ("M5", "M15", "H1", "H4", "D1"):
        latest = _latest_source(db, instrument, tf)
        provider_by_timeframe[tf] = {
            **latest,
            "freshness": details.get(tf, {}).get("status") if isinstance(details.get(tf), dict) else None,
            "age_min": details.get(tf, {}).get("age_min") if isinstance(details.get(tf), dict) else None,
            "threshold_min": details.get(tf, {}).get("threshold_min") if isinstance(details.get(tf), dict) else None,
        }

    if freshness.get("weekend"):
        state = "closed"
    elif not freshness.get("stale"):
        state = "fresh"
    elif freshness.get("fresh"):
        state = "degraded"
    else:
        state = "stale"

    # M5 is the execution-refinement timeframe and therefore the clearest
    # single provider label for operators. Fall back to the freshest available
    # timeframe if M5 is missing.
    active_provider = provider_by_timeframe["M5"].get("source")
    last_bar_time = provider_by_timeframe["M5"].get("latest")
    if not active_provider:
        for tf in ("M15", "H1", "H4", "D1"):
            if provider_by_timeframe[tf].get("source"):
                active_provider = provider_by_timeframe[tf]["source"]
                last_bar_time = provider_by_timeframe[tf]["latest"]
                break

    try:
        from services.tradingview_provider import TV_ENABLED
        tradingview_enabled = bool(TV_ENABLED)
    except Exception:
        tradingview_enabled = False

    return {
        "status": state,
        "data_quality_score": freshness.get("data_quality_score", 0),
        "stale_timeframes": freshness.get("stale", []),
        "fresh_timeframes": freshness.get("fresh", []),
        "active_provider": active_provider,
        "last_bar_time": last_bar_time,
        "provider_by_timeframe": provider_by_timeframe,
        "last_ingest_error": get_last_ingest_error(),
        "tradingview_enabled": tradingview_enabled,
        "weekend": bool(freshness.get("weekend")),
    }


__all__ = ["market_data_health"]
