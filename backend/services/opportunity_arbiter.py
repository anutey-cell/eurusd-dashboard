"""Canonical XAU/USD opportunity-arbiter context.

This is not another strategy and does not place, allow, or deny orders. It is
one normalized decision-context object built from the existing intelligence
stack:

canonical data -> HTF alignment -> regime -> evidence -> breakout acceptance
-> opportunity state -> separated verdicts -> key levels -> macro -> CME.

The arbiter exists so Strategist, Predator, Telegram and diagnostics can speak
from one market-state vocabulary instead of maintaining independent narratives.
Only the core market-data actionability gate can hard-block a signal. CME,
macro and other context remain enrichment layers.
"""
from __future__ import annotations

import copy
import threading
from datetime import datetime, timezone
from typing import Any, Optional

_LOCK = threading.Lock()
_LATEST: Optional[dict[str, Any]] = None

_MANIPULATION_CLASSES = {"LIQUIDITY_PROBE", "FAILED_BREAKOUT", "BREAKOUT_INVALIDATED"}
_ACTIVE_CLASSES = {
    "LIQUIDITY_PROBE", "FAILED_BREAKOUT", "BREAKOUT_DEVELOPING",
    "BREAKOUT_CONFIRMED", "BREAKOUT_ACCEPTANCE", "BREAKOUT_RETEST",
    "CONTINUATION", "EXHAUSTED_BREAKOUT", "BREAKOUT_INVALIDATED",
}
_CLASS_PRIORITY = {
    "BREAKOUT_INVALIDATED": 100,
    "FAILED_BREAKOUT": 95,
    "LIQUIDITY_PROBE": 90,
    "BREAKOUT_RETEST": 85,
    "BREAKOUT_ACCEPTANCE": 80,
    "BREAKOUT_CONFIRMED": 75,
    "CONTINUATION": 70,
    "BREAKOUT_DEVELOPING": 60,
    "EXHAUSTED_BREAKOUT": 55,
}


def _value(obj: Any, name: str, default: Any = None) -> Any:
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def _safe_dict(obj: Any) -> dict[str, Any]:
    if obj is None:
        return {}
    if isinstance(obj, dict):
        return copy.deepcopy(obj)
    if hasattr(obj, "to_dict"):
        try:
            return obj.to_dict()
        except Exception:
            return {}
    return {}


def _summarize_breakouts(breakouts: Optional[list]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    active: list[dict[str, Any]] = []
    for b in breakouts or []:
        cls = str(_value(b, "classification", "NO_BREAKOUT") or "NO_BREAKOUT")
        if cls not in _ACTIVE_CLASSES:
            continue
        row = {
            "level_name": _value(b, "level_name"),
            "level": _value(b, "level"),
            "direction": _value(b, "direction"),
            "classification": cls,
            "confidence": int(_value(b, "confidence", 0) or 0),
            "followthrough_bars": int(_value(b, "followthrough_bars", 0) or 0),
            "time_outside_min": int(_value(b, "time_outside_min", 0) or 0),
            "returned_to_range": bool(_value(b, "returned_to_range", False)),
            "distance_from_level_atr": float(_value(b, "distance_from_level_atr", 0.0) or 0.0),
        }
        active.append(row)

    active.sort(
        key=lambda x: (_CLASS_PRIORITY.get(x["classification"], 0), x["confidence"]),
        reverse=True,
    )
    if not active:
        return ({
            "state": "NO_ACTIVE_BREAKOUT",
            "manipulation_candidate": False,
            "liquidity_side": None,
            "reason": "no active breakout/liquidity classification",
        }, [])

    lead = active[0]
    cls = lead["classification"]
    direction = lead.get("direction")
    manipulation = cls in _MANIPULATION_CLASSES
    liquidity_side = None
    if manipulation:
        if direction == "UP":
            liquidity_side = "BUY_SIDE_SWEEP_CANDIDATE"
        elif direction == "DOWN":
            liquidity_side = "SELL_SIDE_SWEEP_CANDIDATE"

    if cls == "LIQUIDITY_PROBE":
        state = "LIQUIDITY_PROBE"
    elif cls in ("FAILED_BREAKOUT", "BREAKOUT_INVALIDATED"):
        state = "FAILED_BREAKOUT"
    elif cls == "BREAKOUT_DEVELOPING":
        state = "BREAKOUT_DEVELOPING"
    elif cls in ("BREAKOUT_CONFIRMED", "BREAKOUT_ACCEPTANCE"):
        state = "BREAKOUT_ACCEPTED"
    elif cls == "BREAKOUT_RETEST":
        state = "BREAKOUT_RETEST"
    elif cls == "CONTINUATION":
        state = "CONTINUATION"
    elif cls == "EXHAUSTED_BREAKOUT":
        state = "EXHAUSTED_BREAKOUT"
    else:
        state = cls

    return ({
        "state": state,
        "classification": cls,
        "direction": direction,
        "level_name": lead.get("level_name"),
        "level": lead.get("level"),
        "confidence": lead.get("confidence"),
        "manipulation_candidate": manipulation,
        "liquidity_side": liquidity_side,
        "reason": (
            "liquidity-event candidate; intent is not inferred"
            if manipulation else "breakout acceptance state from M15 evidence"
        ),
    }, active[:8])


def build_arbiter_context(
    *,
    snapshot=None,
    htf_alignment=None,
    regime=None,
    evidence=None,
    breakouts: Optional[list] = None,
    state_transition=None,
    separated_verdict=None,
    ranking=None,
    macro=None,
    cme: Optional[dict[str, Any]] = None,
    actionability: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """Pure synthesizer: convert existing engine outputs into one context."""
    structure, active_breakouts = _summarize_breakouts(breakouts)

    directional = _value(separated_verdict, "directional_assessment", "Balanced")
    opportunity = _value(separated_verdict, "opportunity_status", "Data quality insufficient")
    entry = _value(separated_verdict, "entry_status", "No compliant entry")
    opp_state = _value(state_transition, "new_state", "INSUFFICIENT_DATA")

    cme = copy.deepcopy(cme or {})
    cme_summary = {
        "status": cme.get("status", "UNAVAILABLE"),
        "source": cme.get("source"),
        "bulletin_date": cme.get("bulletin_date"),
        "bulletin_status": cme.get("bulletin_status"),
        "gc_xau_basis": cme.get("gc_xau_basis"),
        "directional_bias": cme.get("directional_bias", "UNSIGNED_NEUTRAL"),
        "nearest_zones": (cme.get("nearest_zones") or [])[:3],
        "strongest_zones": (cme.get("strongest_zones") or [])[:3],
        "is_signal_gate": False,
    }

    contradictions = []
    for item in (_value(evidence, "contradictions", []) or []):
        if isinstance(item, dict):
            contradictions.append(item)
        elif hasattr(item, "to_dict"):
            try:
                contradictions.append(item.to_dict())
            except Exception:
                contradictions.append({"name": str(item)})
        else:
            contradictions.append({"name": str(item)})

    macro_dict = _safe_dict(macro)
    ranking_dict = _safe_dict(ranking)

    ctx = {
        "schema_version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "status": "OBSERVED",
        "directional_assessment": directional,
        "opportunity_status": opportunity,
        "entry_status": entry,
        "opportunity_state": opp_state,
        "confidence": int(_value(separated_verdict, "confidence", 0) or 0),
        "structure": structure,
        "active_breakouts": active_breakouts,
        "htf": {
            "direction": _value(htf_alignment, "direction", "NEUTRAL"),
            "strength": _value(htf_alignment, "strength", "NONE"),
            "score": _value(htf_alignment, "score", 0),
        },
        "regime": {
            "regime": _value(regime, "regime"),
            "directional_bias": _value(regime, "directional_bias", "NEUTRAL"),
            "invalidation_price": _value(regime, "invalidation_price"),
        },
        "evidence": {
            "dominant_direction": _value(evidence, "dominant_direction", "NEUTRAL"),
            "bull_score": _value(evidence, "bull_evidence_score", 0),
            "bear_score": _value(evidence, "bear_evidence_score", 0),
            "directional_confidence": _value(evidence, "directional_confidence", 0),
            "entry_quality_confidence": _value(evidence, "entry_quality_confidence", 0),
            "extension_risk_score": _value(evidence, "extension_risk_score", 0),
            "contradiction_score": _value(evidence, "contradiction_score", 0),
            "contradictions": contradictions[:8],
        },
        "data": copy.deepcopy(actionability or {
            "actionable": False,
            "status": "UNKNOWN",
            "reason": "actionability not supplied",
        }),
        "cme": cme_summary,
        "macro": {
            "event_risk_level": macro_dict.get("event_risk_level"),
            "minutes_to_next_event": macro_dict.get("minutes_to_next_event"),
            "macro_bias": macro_dict.get("macro_bias") or macro_dict.get("directional_bias"),
            "is_signal_gate": False,
        },
        "key_levels": ranking_dict,
        "policy": {
            "core_market_data_is_gate": True,
            "cme_is_gate": False,
            "macro_enrichment_is_gate": False,
            "arbiter_is_execution_authority": False,
        },
    }
    return ctx


def publish_arbiter_context(context: dict[str, Any]) -> dict[str, Any]:
    global _LATEST
    with _LOCK:
        _LATEST = copy.deepcopy(context)
    return context


def get_latest_arbiter_context(*, max_age_s: int = 180) -> dict[str, Any]:
    with _LOCK:
        ctx = copy.deepcopy(_LATEST)
    if not ctx:
        return {
            "status": "NO_DATA",
            "stale": True,
            "reason": "market-intelligence pipeline has not published yet",
            "policy": {"arbiter_is_execution_authority": False},
        }
    try:
        ts = datetime.fromisoformat(str(ctx.get("generated_at")).replace("Z", "+00:00"))
        age_s = max(0, int((datetime.now(timezone.utc) - ts).total_seconds()))
    except Exception:
        age_s = 999999
    ctx["age_s"] = age_s
    ctx["stale"] = age_s > max_age_s
    if ctx["stale"]:
        ctx["status"] = "STALE"
    return ctx


def reset_arbiter_context() -> None:
    global _LATEST
    with _LOCK:
        _LATEST = None


__all__ = [
    "build_arbiter_context", "publish_arbiter_context",
    "get_latest_arbiter_context", "reset_arbiter_context",
]
