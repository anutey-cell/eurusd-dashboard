"""Provider health registry and lightweight circuit breaker for market-data feeds.

This module is process-local by design: it protects each backend worker from
hammering a provider that is known to be unavailable while preserving the
existing provider fallback order. It does not decide trading direction and it
never treats an optional/context feed as a signal gate.

Failure policy:
- authentication/configuration failures open a provider-wide circuit for 12h;
- rate-limit failures open a provider-wide circuit for 15m;
- stale/empty payloads cool only the affected timeframe for 2m;
- transient transport failures cool the affected timeframe after repeated
  failures (1m, then 5m after three consecutive failures).

A successful request closes the affected timeframe circuit. Provider-wide
circuits expire automatically and are retried after their cooldown.
"""
from __future__ import annotations

import threading
import time
from datetime import datetime, timezone
from typing import Any, Optional

_LOCK = threading.Lock()
_STATE: dict[str, dict[str, Any]] = {}

_AUTH_COOLDOWN_S = 12 * 60 * 60
_RATE_COOLDOWN_S = 15 * 60
_STALE_COOLDOWN_S = 2 * 60
_TRANSIENT_COOLDOWN_S = 60
_TRANSIENT_ESCALATED_S = 5 * 60


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _key(provider: str, timeframe: Optional[str] = None) -> str:
    p = (provider or "unknown").strip().lower()
    tf = (timeframe or "*").strip().upper()
    return f"{p}:{tf}"


def classify_failure(error: Any) -> str:
    """Classify a provider failure without depending on provider exception types."""
    msg = str(error or "").lower()
    if any(token in msg for token in (
        "invalid or expired api key", "invalid api key", "expired api key",
        "authentication", "unauthorized", "forbidden", "http 401", "http 403",
        "status code 401", "status code 403",
    )):
        return "auth"
    if any(token in msg for token in (
        "rate limit", "too many requests", "http 429", "status code 429",
        "quota exceeded", "credits exceeded",
    )):
        return "rate_limit"
    if any(token in msg for token in (
        "stale", "empty payload", "no parseable candle", "rejected",
    )):
        return "stale_payload"
    return "transient"


def _entry(provider: str, timeframe: Optional[str]) -> dict[str, Any]:
    k = _key(provider, timeframe)
    if k not in _STATE:
        _STATE[k] = {
            "provider": (provider or "unknown").strip().lower(),
            "timeframe": (timeframe or "*").upper(),
            "status": "unknown",
            "consecutive_failures": 0,
            "last_success_at": None,
            "last_failure_at": None,
            "last_failure_category": None,
            "last_error": "",
            "cooldown_until_ts": 0.0,
        }
    return _STATE[k]


def should_attempt(provider: str, timeframe: Optional[str] = None,
                   *, now_ts: Optional[float] = None) -> tuple[bool, str]:
    """Return whether the provider should be attempted for this timeframe."""
    now_ts = time.time() if now_ts is None else float(now_ts)
    with _LOCK:
        # Provider-wide failures (auth/rate-limit) take precedence.
        for scope_tf in (None, timeframe):
            ent = _STATE.get(_key(provider, scope_tf))
            if not ent:
                continue
            until = float(ent.get("cooldown_until_ts") or 0.0)
            if until > now_ts:
                remaining = int(max(1, until - now_ts))
                return False, (
                    f"circuit_open category={ent.get('last_failure_category') or 'unknown'} "
                    f"retry_in={remaining}s"
                )
        return True, "available"


def note_success(provider: str, timeframe: Optional[str] = None) -> None:
    now = _now_iso()
    with _LOCK:
        ent = _entry(provider, timeframe)
        ent.update({
            "status": "healthy",
            "consecutive_failures": 0,
            "last_success_at": now,
            "last_error": "",
            "cooldown_until_ts": 0.0,
        })


def note_failure(provider: str, timeframe: Optional[str], error: Any,
                 *, category: Optional[str] = None,
                 now_ts: Optional[float] = None) -> str:
    """Record a failure and open the appropriate circuit. Returns category."""
    now_ts = time.time() if now_ts is None else float(now_ts)
    cat = category or classify_failure(error)
    target_tf: Optional[str] = timeframe
    if cat in ("auth", "rate_limit"):
        target_tf = None  # same credential/quota applies across all timeframes

    with _LOCK:
        ent = _entry(provider, target_tf)
        failures = int(ent.get("consecutive_failures") or 0) + 1
        if cat == "auth":
            cooldown = _AUTH_COOLDOWN_S
        elif cat == "rate_limit":
            cooldown = _RATE_COOLDOWN_S
        elif cat == "stale_payload":
            cooldown = _STALE_COOLDOWN_S
        else:
            cooldown = _TRANSIENT_ESCALATED_S if failures >= 3 else _TRANSIENT_COOLDOWN_S

        ent.update({
            "status": "circuit_open",
            "consecutive_failures": failures,
            "last_failure_at": _now_iso(),
            "last_failure_category": cat,
            "last_error": str(error)[:400],
            "cooldown_until_ts": now_ts + cooldown,
        })
    return cat


def provider_health_snapshot(*, now_ts: Optional[float] = None) -> dict[str, Any]:
    """Return a serialization-safe operator snapshot."""
    now_ts = time.time() if now_ts is None else float(now_ts)
    with _LOCK:
        items: dict[str, Any] = {}
        for key, value in sorted(_STATE.items()):
            item = dict(value)
            until = float(item.pop("cooldown_until_ts", 0.0) or 0.0)
            item["circuit_open"] = until > now_ts
            item["retry_in_s"] = int(max(0, until - now_ts))
            item["cooldown_until"] = (
                datetime.fromtimestamp(until, tz=timezone.utc).isoformat()
                if until > 0 else None
            )
            if not item["circuit_open"] and item.get("status") == "circuit_open":
                item["status"] = "retry_due"
            items[key] = item
        return {"providers": items, "count": len(items)}


def reset_provider_health() -> None:
    """Test/maintenance helper. Does not touch persistent market data."""
    with _LOCK:
        _STATE.clear()


__all__ = [
    "should_attempt", "note_success", "note_failure", "classify_failure",
    "provider_health_snapshot", "reset_provider_health",
]
