"""Compatibility wrapper for Predator Engine safety/correctness controls.

The frozen engine implementation is preserved verbatim in predator_engine_legacy.py.
This module patches operational defects and production governance controls:
1) previous-day levels use the previous XAU trading session (22:00 UTC boundary),
2) Asian and PDL FIRE events must be fresh, not recycled from a 4h lookback,
3) PDL narrative says previous-session instead of yesterday,
4) alert regime/session display uses the signal event session,
5) legacy pre-fix performance claims are suppressed,
6) a fail-closed FIRE freshness guard validates signal-bar and M5-feed recency,
7) ASIAN_BREAKDOWN and PDL_BREAK FIREs are quarantined to shadow research after
   full-history post-fix validation showed no robust production edge,
8) VOL_CONTINUATION is quarantined too because the frozen engine creates it only
   as a derivative of one of those primary FIREs and copies its trade plan.

Sizing, SL/TP geometry and SELL mandate are unchanged.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

from services import predator_engine_legacy as _legacy

log = logging.getLogger(__name__)

# Re-export the frozen public API first; selected functions are replaced below.
PredatorSignal = _legacy.PredatorSignal
_EXPECTED_TOTAL_MOVE_PTS = _legacy._EXPECTED_TOTAL_MOVE_PTS
_EXTENSION_LIMIT = _legacy._EXTENSION_LIMIT
format_telegram_invalidated = _legacy.format_telegram_invalidated
format_predator_execution_summary = _legacy.format_predator_execution_summary
detect_vol_continuation = _legacy.detect_vol_continuation

# 2026-09-14 full-history post-fix governance decision.
# These archetypes remain DETECTABLE for research/backtests, but their live FIRE
# events are shadow-recorded and withheld from the scheduler. That means no
# actionable Telegram, no batch creation and no execution while quarantined.
#
# Evidence basis (2025-03-21 -> 2026-09-14, conservative SL-first replay):
#   ASIAN_BREAKDOWN production-regime-matched: n=92, E=-0.55, PF=0.91
#   PDL_BREAK       production-regime-matched: n=105, E=-3.97, PF=0.65
# VOL_CONTINUATION has no independent thesis in the frozen engine: it is created
# only when one of those primaries fires, then copies the parent's entry/SL/TP.
_QUARANTINED_FIRE_ARCHETYPES = frozenset({
    "ASIAN_BREAKDOWN", "PDL_BREAK", "VOL_CONTINUATION",
})

# The old figures were measured against pre-fix trigger semantics and must not
# be advertised as performance of the corrected production trigger.
_ARCHETYPE_STATS = {
    "ASIAN_BREAKDOWN": {
        "sample": "QUARANTINED · POST-FIX EDGE NOT VALIDATED",
        "wr": "—", "expectancy": "—", "pf": "—",
    },
    "PDL_BREAK": {
        "sample": "QUARANTINED · NEGATIVE POST-FIX EDGE",
        "wr": "—", "expectancy": "—", "pf": "—",
    },
    "VOL_CONTINUATION": {
        "sample": "QUARANTINED · DERIVATIVE OF PRIMARY",
        "wr": "—", "expectancy": "—", "pf": "—",
    },
}
# Frozen formatter resolves this global in the legacy module.
_legacy._ARCHETYPE_STATS = _ARCHETYPE_STATS

# Preserve references to frozen helpers before monkey-patching the legacy module.
_legacy_load_recent = _legacy._load_recent
_legacy_first_m5_close_below = _legacy._first_m5_close_below
_legacy_evaluate = _legacy.evaluate


def _trading_date(t):
    """XAU trading date with a 22:00 UTC boundary (Sunday 22:00 => Monday)."""
    d = t.date()
    return d + timedelta(days=1) if t.hour >= 22 else d


def _prev_day_hl(m5_bars: list[tuple]) -> tuple[Optional[float], Optional[float]]:
    """High/low of the most recent completed XAU trading session."""
    if not m5_bars:
        return None, None
    current_td = _trading_date(m5_bars[-1][0])
    prior_dates = sorted({
        _trading_date(b[0]) for b in m5_bars
        if _trading_date(b[0]) < current_td
    })
    if not prior_dates:
        return None, None
    prev_td = prior_dates[-1]
    prev_bars = [b for b in m5_bars if _trading_date(b[0]) == prev_td]
    if not prev_bars:
        return None, None
    return max(b[2] for b in prev_bars), min(b[3] for b in prev_bars)


def _fresh_m5_cross_below(m5_bars: list[tuple], level: float, lookback_bars: int = 48):
    """Return only a cross made by the latest M5 bar."""
    if len(m5_bars) < 2:
        return None
    prev, cur = m5_bars[-2], m5_bars[-1]
    if prev[4] >= level and cur[4] < level:
        return {
            "time": cur[0], "close": cur[4], "high": cur[2], "low": cur[3],
            "idx_in_slice": len(m5_bars) - 1,
        }
    return None


def _fresh_m5_acceptance_below(m5_bars: list[tuple], level: float):
    """Require at/above -> below -> below, returning the second below close."""
    if len(m5_bars) < 3:
        return None
    pre, first, confirm = m5_bars[-3], m5_bars[-2], m5_bars[-1]
    if pre[4] >= level and first[4] < level and confirm[4] < level:
        return {
            "time": confirm[0], "close": confirm[4], "high": confirm[2],
            "low": confirm[3], "idx_in_slice": len(m5_bars) - 1,
        }
    return None


def _as_utc(value):
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def validate_fire_freshness(signal, m5_bars: list[tuple], *,
                            now_utc=None, max_lag_bars: int = 1,
                            max_data_age_min: int = 15) -> tuple[bool, str]:
    """Fail-closed FIRE guard at the engine boundary.

    A FIRE must reference the latest M5 event (one-bar tolerance) and the
    latest M5 feed itself must be recent. This prevents a future detector
    regression from turning a historical breach into a current alert/order.
    """
    if getattr(signal, "state", None) != "FIRE":
        return True, "not_fire"
    if not m5_bars:
        return False, "no_m5_reference"
    try:
        signal_t = _as_utc(getattr(signal, "bar_time", None))
        latest_t = _as_utc(m5_bars[-1][0])
        now_t = _as_utc(now_utc or datetime.now(timezone.utc))
    except Exception:
        return False, "invalid_fire_timestamp"

    if signal_t - latest_t > timedelta(minutes=1):
        return False, "future_signal_bar"

    max_lag = timedelta(minutes=5 * max(0, int(max_lag_bars)))
    if latest_t - signal_t > max_lag:
        return False, "stale_signal_bar"

    if latest_t - now_t > timedelta(minutes=1):
        return False, "future_m5_feed"
    if now_t - latest_t > timedelta(minutes=max(1, int(max_data_age_min))):
        return False, "stale_m5_feed"

    return True, "ok"


def _load_recent(db, tf: str, n: int):
    """Ensure enough M5 history to contain the prior full trading session."""
    return _legacy_load_recent(db, tf, 700 if tf == "M5" and n < 700 else n)


def detect_pdl_break(m5_bars: list[tuple], vol_r: Optional[float] = None):
    """PDL_BREAK with previous-session PDL and fresh two-close acceptance."""
    if len(m5_bars) < 20:
        return None
    _, prev_l = _prev_day_hl(m5_bars)
    if prev_l is None:
        return None

    trigger_level = prev_l - 3.0
    hit = _fresh_m5_acceptance_below(m5_bars, trigger_level)
    if hit is None:
        return None

    t, c = hit["time"], hit["close"]
    _, a_low = _legacy._asian_range(m5_bars)
    stacked = a_low is not None and c < a_low
    has_vol = vol_r is not None and vol_r >= 1.2
    if not (has_vol or stacked):
        return None

    passes, pct = _legacy._apply_extension_filter("PDL_BREAK", prev_l, c, c)
    if not passes:
        return None

    entry = c
    stop = round(prev_l + 5.0, 2)
    tp1 = round(entry - 40.0, 2)
    tp2 = round(entry - 60.0, 2)
    rr = round(abs(tp1 - entry) / max(abs(stop - entry), 0.1), 2)
    confidence = "HIGH" if stacked and has_vol else "MED" if stacked or has_vol else "LOW"

    return PredatorSignal(
        archetype="PDL_BREAK", direction="SELL", state="FIRE",
        entry=entry, stop_loss=stop, tp1=tp1, tp2=tp2, rr=rr,
        thesis=(
            f"Fresh M5 acceptance below previous-session low {prev_l:.2f}. "
            f"Previous-session dip-buyers underwater. "
            f"{'Stacked with Asian-low.' if stacked else ''} "
            f"{'Vol surge.' if has_vol else ''}"
        ).strip(),
        trigger="fresh two-close M5 acceptance below PDL-3pt",
        confidence=confidence,
        counterparty=(
            "Previous-session dip-buyers whose stops sit below PDL; "
            "retail 'PDL=support' holders"
        ),
        session=_legacy._session_label(t.hour),
        bar_time=t.isoformat(),
        fingerprint=_legacy._fingerprint("PDL_BREAK", "SELL", entry, t),
        pct_consumed=round(pct, 3),
        latency_pts=round(abs(prev_l - c), 1),
        m5_used=True,
    )


def format_telegram_alert(sig, regime=None, key_level=None, current_price=None,
                          current_price_ts=None, deployment_plan=None):
    """Keep the alert's regime session aligned with the signal event session."""
    aligned_regime = None
    if regime is not None:
        aligned_regime = dict(regime)
        aligned_regime["session"] = sig.session
    return _legacy.format_telegram_alert(
        sig,
        regime=aligned_regime,
        key_level=key_level,
        current_price=current_price,
        current_price_ts=current_price_ts,
        deployment_plan=deployment_plan,
    )


def _record_quarantined_shadow(db, sig) -> None:
    """Preserve forward research evidence for a quarantined FIRE.

    Mirrors the scheduler's existing shadow-record payload, but happens before
    the signal is withheld from downstream actionability. Fails silent by design
    so research bookkeeping can never destabilise the detector loop.
    """
    try:
        from services.shadow_trade_simulator import record_shadow_trade

        synthetic_verdict = {
            "decision": sig.direction,
            "archetype": sig.archetype,
            "setup_score": 85 if sig.confidence == "HIGH" else 75 if sig.confidence == "MED" else 65,
            "conditions_passed": 4,
            "trade_plan": {
                "entry": sig.entry,
                "stop_loss": sig.stop_loss,
                "tp1": sig.tp1,
                "tp2": sig.tp2,
                "tp1_rr": abs(sig.tp1 - sig.entry) / max(abs(sig.entry - sig.stop_loss), 0.1),
                "tp2_rr": abs(sig.tp2 - sig.entry) / max(abs(sig.entry - sig.stop_loss), 0.1),
                "invalidation": sig.stop_loss,
                "risk_reward": sig.rr,
            },
        }

        class _QuarantineGrade:
            grade = f"PRED_{sig.archetype}_{sig.confidence}_QUARANTINED"
            reason = f"Research-only quarantine: {sig.thesis}"
            composite_score = 85 if sig.confidence == "HIGH" else 75

        result = record_shadow_trade(db, synthetic_verdict, grade_result=_QuarantineGrade())
        log.info(
            "[predator] QUARANTINE shadow-record %s %s @ %.2f recorded=%s reason=%s",
            sig.archetype, sig.direction, sig.entry,
            getattr(result, "recorded", None), getattr(result, "reason", None),
        )
    except Exception as exc:
        log.warning("[predator] quarantine shadow-record failed: %s", exc)


# Patch the frozen module because its evaluate() resolves helpers/functions in
# its own module globals. This keeps every untouched detector path identical.
_legacy._prev_day_hl = _prev_day_hl
_legacy._load_recent = _load_recent
_legacy._first_m5_close_below = _fresh_m5_cross_below  # Asian detector: latest cross only
_legacy.detect_pdl_break = detect_pdl_break


def evaluate(db):
    """Frozen evaluation plus safety boundary and evidence-based quarantine."""
    signals = _legacy_evaluate(db)
    if not signals:
        return signals

    # Use only the latest few canonical M5 bars here; do not trigger the 700-bar
    # session-history expansion because this is a freshness check, not detection.
    try:
        latest_m5 = _legacy_load_recent(db, "M5", 3)
    except Exception as exc:
        log.warning("[predator] FIRE freshness reference unavailable: %s", exc)
        latest_m5 = []

    out = []
    for sig in signals:
        if getattr(sig, "state", None) == "FIRE":
            ok, reason = validate_fire_freshness(sig, latest_m5)
            if not ok:
                log.warning(
                    "[predator] FIRE blocked by engine freshness guard: %s %s reason=%s bar=%s",
                    getattr(sig, "archetype", "?"), getattr(sig, "direction", "?"),
                    reason, getattr(sig, "bar_time", None),
                )
                continue

            if getattr(sig, "archetype", None) in _QUARANTINED_FIRE_ARCHETYPES:
                _record_quarantined_shadow(db, sig)
                log.warning(
                    "[predator] FIRE quarantined from actionability: %s %s @ %.2f",
                    sig.archetype, sig.direction, sig.entry,
                )
                continue

        out.append(sig)
    return out


detect_asian_breakdown = _legacy.detect_asian_breakdown

# Keep the old research helper available to explicit callers; production does
# not use it after this fix.
_first_m5_close_below = _legacy_first_m5_close_below

__all__ = [
    "PredatorSignal", "evaluate", "format_telegram_alert",
    "format_telegram_invalidated", "format_predator_execution_summary",
    "detect_asian_breakdown", "detect_pdl_break", "detect_vol_continuation",
    "validate_fire_freshness", "_QUARANTINED_FIRE_ARCHETYPES",
    "_ARCHETYPE_STATS", "_EXPECTED_TOTAL_MOVE_PTS", "_EXTENSION_LIMIT",
]
