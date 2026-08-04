"""
External Confluence Layer — FastBull + CME
============================================

READ-ONLY confirmation layer. Reads external market-positioning
references (FastBull retail crowd + CME GC futures) and returns
a structured verdict that the strategist uses to nudge setup_score
or downgrade execution — never to CREATE a trade.

Hard rules baked into this module:
  1. Never generates a signal on its own.
  2. Never upgrades a no-trade setup into a trade.
  3. Never bypasses news / stale-data / RR / freshness gates.
  4. Fails open — provider errors return unavailable/neutral, not error.
  5. Caches for `external_confluence_cache_seconds` (default 10 min).
  6. Total wall-clock ≤ 2 × http_timeout (default 6s) — never delays scanner.

Public API:
  - get_external_confluence(db, engine_direction, spot_price, engine_levels)
    Returns the full structured dict per the operator brief.
"""
from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timezone
from typing import Optional

log = logging.getLogger(__name__)


# ── Execution status constants (also imported by strategist) ────────────────

EXEC_EXT_CONFIRMED   = "EXTERNAL_CONFLUENCE_CONFIRMED"
EXEC_EXT_NEUTRAL     = "EXTERNAL_CONFLUENCE_NEUTRAL"
EXEC_EXT_CONFLICT    = "EXTERNAL_CONFLUENCE_CONFLICT"
EXEC_EXT_UNAVAILABLE = "EXTERNAL_CONFLUENCE_UNAVAILABLE"


# ── In-process cache (TTL) ───────────────────────────────────────────────────

_CACHE: dict = {"value": None, "expires_at": 0.0}
_LOCK = threading.Lock()


def _cache_get() -> Optional[dict]:
    with _LOCK:
        if _CACHE["value"] is not None and time.monotonic() < _CACHE["expires_at"]:
            return _CACHE["value"]
    return None


def _cache_set(value: dict, ttl_s: int) -> None:
    with _LOCK:
        _CACHE["value"] = value
        _CACHE["expires_at"] = time.monotonic() + ttl_s


def invalidate_cache() -> None:
    """Test / operator hook."""
    with _LOCK:
        _CACHE["value"] = None
        _CACHE["expires_at"] = 0.0


# ── FastBull bias mapping ────────────────────────────────────────────────────

def _fastbull_directional_bias(fb: dict) -> str:
    """Contrarian read of retail positioning: heavy-long = bearish tell."""
    b = fb.get("long_short_bias") or "unknown"
    if b == "long_heavy":  return "bearish"
    if b == "short_heavy": return "bullish"
    if b == "balanced":    return "neutral"
    return "unknown"


# ── Scoring ──────────────────────────────────────────────────────────────────

def _score_and_bias(
    fb_bias: str,           # bullish | bearish | neutral | unknown
    cme_bias: str,          # bullish | bearish | neutral | unknown
    engine_direction: str,  # BUY | SELL | STAND_ASIDE
    max_up: int,
    max_down: int,
    fb: dict,
    cme: dict,
) -> tuple[str, int, str, bool]:
    """
    Return (confluence_bias, score_adjustment, reason, blocks_trade).
    """
    # Normalise engine direction into a bias axis
    engine_bias = ({"BUY": "bullish", "SELL": "bearish"}
                    .get(engine_direction, "neutral"))

    signals = [b for b in (fb_bias, cme_bias) if b not in ("unknown",)]

    # Case: both sources unknown/unavailable
    if not signals:
        return ("unknown", 0,
                "External sources unavailable — treated as neutral", False)

    # Combined confluence bias
    bull = sum(1 for b in signals if b == "bullish")
    bear = sum(1 for b in signals if b == "bearish")
    if bull > bear:
        conf_bias = "bullish"
    elif bear > bull:
        conf_bias = "bearish"
    elif bull == bear and bull > 0:
        conf_bias = "conflicted"    # one bull + one bear
    else:
        conf_bias = "neutral"

    # If engine is standing aside, external can neither block nor upgrade
    if engine_bias == "neutral":
        return (conf_bias, 0,
                "Engine not directional — external ignored per mandate", False)

    # Confirmation vs conflict math
    confirms = (conf_bias == engine_bias)
    conflicts = (conf_bias in ("bullish", "bearish")
                  and conf_bias != engine_bias)

    if confirms:
        # +10 when BOTH sources confirm, +5 when only one available/confirms
        both_confirm = (fb_bias == engine_bias and cme_bias == engine_bias)
        adj = max_up if both_confirm else min(max_up, 5)
        return (conf_bias, adj,
                f"Both FastBull and CME align with engine {engine_direction}."
                if both_confirm else
                f"One external source aligns with engine {engine_direction}.",
                False)

    if conflicts:
        # -5 when only one source conflicts; -15 when both conflict AND price
        # is heading into unmitigated opposing liquidity (block).
        both_conflict = (fb_bias not in ("unknown", engine_bias)
                          and cme_bias not in ("unknown", engine_bias)
                          and fb_bias != "neutral" and cme_bias != "neutral")
        if both_conflict:
            return (conf_bias, -max_down,
                    f"Both FastBull and CME oppose engine {engine_direction} — "
                    "high conflict; downgrade execution",
                    True)
        return (conf_bias, -5,
                f"One external source opposes engine {engine_direction}.",
                False)

    # conf_bias == "conflicted" or "neutral" with engine directional
    if conf_bias == "conflicted":
        return ("conflicted", -5,
                "External sources conflict internally — reduce confidence",
                False)
    return ("neutral", 0, "External sources neutral", False)


# ── Match FastBull zones against engine levels (informational) ──────────────

def _matched_levels(fb: dict, engine_levels: list[float],
                     tolerance_pts: float = 5.0) -> list[float]:
    """Return engine liquidity levels that appear near FastBull zones.
    FastBull's list-position page rarely exposes numeric zones, so this
    is often empty until we get richer data."""
    matches: list[float] = []
    fb_zones = list(fb.get("liquidity_above", [])) + list(fb.get("liquidity_below", []))
    if not fb_zones or not engine_levels:
        return matches
    for lvl in engine_levels:
        for z in fb_zones:
            try:
                if abs(float(lvl) - float(z)) <= tolerance_pts:
                    matches.append(round(float(lvl), 2))
                    break
            except Exception:
                continue
    return sorted(set(matches))


# ── Public API ───────────────────────────────────────────────────────────────

def get_external_confluence(
    db=None,
    *,
    engine_direction: str = "STAND_ASIDE",
    spot_price: Optional[float] = None,
    engine_levels: Optional[list[float]] = None,
    settings=None,
    force_refresh: bool = False,
) -> dict:
    """
    Compose the full external-confluence verdict.

    engine_direction : "BUY" | "SELL" | "STAND_ASIDE"
    spot_price       : current XAU/USD spot (for CME basis calc)
    engine_levels    : list of numeric liquidity levels from the mandate's
                        liquidity_map — used for matched_engine_levels output
    settings         : config.settings (or None to import lazily)
    force_refresh    : bypass cache (mainly for tests)

    Returns the full structured dict per the operator brief. Never raises.
    """
    try:
        if settings is None:
            from config import settings as _s
            settings = _s

        # Master switch
        if not getattr(settings, "external_confluence_enabled", True):
            return _shape_disabled()

        # Cache hit?
        if not force_refresh:
            cached = _cache_get()
            if cached is not None:
                # Re-run only the confluence scoring — engine direction may
                # have changed since the last fetch — but keep the cached
                # provider payloads.
                return _finalize(
                    cached["fastbull"], cached["cme"],
                    engine_direction=engine_direction,
                    engine_levels=engine_levels or [],
                    settings=settings,
                    from_cache=True,
                )

        # Live fetch
        ttl        = int(getattr(settings, "external_confluence_cache_seconds", 600))
        timeout    = float(getattr(settings, "external_confluence_http_timeout_s", 3.0))
        fb_enabled = bool(getattr(settings, "fastbull_confluence_enabled", True))
        cme_enabled = bool(getattr(settings, "cme_confluence_enabled", True))

        # Both providers are wrapped fail-open — no try/except needed
        if fb_enabled:
            from services.fastbull_provider import fetch_fastbull_positioning
            fb = fetch_fastbull_positioning(timeout_s=timeout)
        else:
            fb = {"available": False, "long_short_bias": "unknown",
                  "interpretation": "disabled by config"}

        if cme_enabled:
            from services.cme_provider import fetch_cme_context
            cme = fetch_cme_context(db=db, timeout_s=timeout, spot_price=spot_price)
        else:
            cme = {"available": False, "futures_bias": "unknown",
                   "interpretation": "disabled by config"}

        _cache_set({"fastbull": fb, "cme": cme}, ttl)

        return _finalize(fb, cme,
                          engine_direction=engine_direction,
                          engine_levels=engine_levels or [],
                          settings=settings,
                          from_cache=False)

    except Exception as exc:
        # Absolute last-resort catch — we NEVER let this module raise
        log.warning("[external_confluence] unexpected: %s", exc)
        return _shape_error(str(exc))


def _finalize(fb: dict, cme: dict, *,
               engine_direction: str,
               engine_levels: list,
               settings,
               from_cache: bool) -> dict:
    """Combine provider outputs + scoring into the operator-facing shape."""
    fb_bias  = _fastbull_directional_bias(fb) if fb.get("available") else "unknown"
    cme_bias = cme.get("futures_bias", "unknown") if cme.get("available") else "unknown"

    max_up   = int(getattr(settings, "external_confluence_max_upgrade",   10))
    max_down = int(getattr(settings, "external_confluence_max_downgrade", 15))

    bias, adj, reason, blocks = _score_and_bias(
        fb_bias, cme_bias, engine_direction,
        max_up=max_up, max_down=max_down,
        fb=fb, cme=cme,
    )
    matched = _matched_levels(fb, engine_levels)

    return {
        "enabled":  True,
        "fastbull": fb,
        "cme":      cme,
        "confluence": {
            "bias":                  bias,
            "score_adjustment":      adj,
            "blocks_trade":          blocks,
            "reason":                reason,
            "matched_engine_levels": matched,
        },
        "cache_hit":  from_cache,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }


# ── Convenience: map confluence to an execution status ─────────────────────

def status_for(confluence: dict, engine_direction: str) -> str:
    """
    Given the confluence dict + engine direction, return the specific
    EXTERNAL_CONFLUENCE_* status string the strategist should use.
    """
    if not confluence.get("enabled"):
        return EXEC_EXT_UNAVAILABLE
    conf = confluence.get("confluence", {})
    bias = conf.get("bias")
    if bias == "unknown":
        return EXEC_EXT_UNAVAILABLE
    if bias == "neutral":
        return EXEC_EXT_NEUTRAL
    if confluence.get("fastbull", {}).get("available") is False and \
       confluence.get("cme",      {}).get("available") is False:
        return EXEC_EXT_UNAVAILABLE
    if bias == "conflicted" or (
        engine_direction in ("BUY", "SELL")
        and bias in ("bullish", "bearish")
        and bias != ({"BUY": "bullish", "SELL": "bearish"}[engine_direction])
    ):
        return EXEC_EXT_CONFLICT
    return EXEC_EXT_CONFIRMED


# ── Shape helpers ────────────────────────────────────────────────────────────

def _shape_disabled() -> dict:
    return {
        "enabled":  False,
        "fastbull": {"available": False, "long_short_bias": "unknown"},
        "cme":      {"available": False, "futures_bias": "unknown"},
        "confluence": {
            "bias":                  "unknown",
            "score_adjustment":      0,
            "blocks_trade":          False,
            "reason":                "External confluence layer disabled by config",
            "matched_engine_levels": [],
        },
        "cache_hit":  False,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }


def _shape_error(msg: str) -> dict:
    return {
        "enabled":  True,
        "fastbull": {"available": False, "long_short_bias": "unknown",
                     "interpretation": f"error: {msg}"},
        "cme":      {"available": False, "futures_bias": "unknown",
                     "interpretation": f"error: {msg}"},
        "confluence": {
            "bias":                  "unknown",
            "score_adjustment":      0,
            "blocks_trade":          False,
            "reason":                f"External layer error — ignored: {msg}",
            "matched_engine_levels": [],
        },
        "cache_hit":  False,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }


__all__ = [
    "get_external_confluence", "status_for", "invalidate_cache",
    "EXEC_EXT_CONFIRMED", "EXEC_EXT_NEUTRAL", "EXEC_EXT_CONFLICT",
    "EXEC_EXT_UNAVAILABLE",
]
