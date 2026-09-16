"""Universal core market-data actionability gate for XAU/USD signals.

Only market-perception inputs are hard gates. Optional intelligence layers
(CME options/futures, CFTC, sentiment, macro enrichment, MT5 availability)
may enrich or contradict a setup but their absence never blocks a signal.

A signal is data-actionable when:
- market is open;
- M5, M15 and H1 spot candles are fresh under the canonical thresholds; and
- aggregate canonical data quality is at least 70/100.

H4/D1 freshness remains visible as context. They are not hard blockers when
M5/M15/H1 are current, which preserves signal capability during a delayed HTF
provider update while preventing entries from stale execution/structure data.
"""
from __future__ import annotations

from typing import Any

CORE_SIGNAL_TIMEFRAMES = ("M5", "M15", "H1")
CONTEXT_TIMEFRAMES = ("H4", "D1")
MIN_DATA_QUALITY = 70


def evaluate_db_actionability(db, *, instrument: str = "XAU/USD") -> dict[str, Any]:
    from services.data_freshness import check_freshness

    result = check_freshness(
        db,
        instrument=instrument,
        timeframes=CORE_SIGNAL_TIMEFRAMES + CONTEXT_TIMEFRAMES,
    )
    details = result.get("details") or {}
    stale = set(result.get("stale") or [])

    critical_stale = [tf for tf in CORE_SIGNAL_TIMEFRAMES if tf in stale]
    critical_missing = [
        tf for tf in CORE_SIGNAL_TIMEFRAMES
        if (details.get(tf) or {}).get("status") == "missing"
    ]
    context_stale = [tf for tf in CONTEXT_TIMEFRAMES if tf in stale]
    quality = int(result.get("data_quality_score") or 0)
    weekend = bool(result.get("weekend"))

    reasons: list[str] = []
    if weekend:
        reasons.append("market_closed")
    if critical_missing:
        reasons.append("critical_missing=" + ",".join(critical_missing))
    elif critical_stale:
        reasons.append("critical_stale=" + ",".join(critical_stale))
    if quality < MIN_DATA_QUALITY:
        reasons.append(f"quality={quality}<{MIN_DATA_QUALITY}")

    actionable = not weekend and not critical_stale and quality >= MIN_DATA_QUALITY
    return {
        "actionable": actionable,
        "status": "ACTIONABLE" if actionable else "DATA_STALE",
        "reason": "ok" if actionable else "; ".join(reasons) or "core_data_not_actionable",
        "data_quality_score": quality,
        "minimum_quality": MIN_DATA_QUALITY,
        "critical_timeframes": list(CORE_SIGNAL_TIMEFRAMES),
        "critical_stale": critical_stale,
        "critical_missing": critical_missing,
        "context_stale": context_stale,
        "weekend": weekend,
        "details": {tf: details.get(tf) for tf in CORE_SIGNAL_TIMEFRAMES + CONTEXT_TIMEFRAMES},
        "optional_context_is_gate": False,
    }


def attach_actionability(verdict: dict, db) -> dict[str, Any]:
    """Attach the gate to a verdict and fail closed for BUY/SELL alerts/orders."""
    gate = evaluate_db_actionability(db)
    verdict["data_actionability"] = gate
    if verdict.get("decision") in ("BUY", "SELL") and not gate["actionable"]:
        verdict["execution_status"] = "DATA_STALE"
        verdict["execution_status_reason"] = (
            "Core XAU/USD market data not actionable: " + gate["reason"]
        )
        permission = verdict.get("execution_permission")
        if not isinstance(permission, dict):
            permission = {}
            verdict["execution_permission"] = permission
        permission["allow_alert"] = False
        permission["allow_execute"] = False
        permission["data_gate"] = "BLOCKED"
    return gate


__all__ = [
    "CORE_SIGNAL_TIMEFRAMES", "CONTEXT_TIMEFRAMES", "MIN_DATA_QUALITY",
    "evaluate_db_actionability", "attach_actionability",
]
