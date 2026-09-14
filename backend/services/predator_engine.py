"""Compatibility wrapper for Predator Engine calendar/freshness bugfix (2026-09-14).

The frozen engine implementation is preserved verbatim in predator_engine_legacy.py.
This module patches only four operational defects:
1) previous-day levels use the previous XAU trading session (22:00 UTC boundary),
2) Asian and PDL FIRE events must be fresh, not recycled from a 4h lookback,
3) PDL narrative says previous-session instead of yesterday,
4) alert regime/session display uses the signal event session.

Sizing, SL/TP geometry, regime direction/volatility gates and SELL mandate are unchanged.
"""
from __future__ import annotations

from datetime import timedelta
from typing import Optional

from services import predator_engine_legacy as _legacy

# Re-export the frozen public API first; selected functions are replaced below.
PredatorSignal = _legacy.PredatorSignal
_ARCHETYPE_STATS = _legacy._ARCHETYPE_STATS
_EXPECTED_TOTAL_MOVE_PTS = _legacy._EXPECTED_TOTAL_MOVE_PTS
_EXTENSION_LIMIT = _legacy._EXTENSION_LIMIT
format_telegram_invalidated = _legacy.format_telegram_invalidated
format_predator_execution_summary = _legacy.format_predator_execution_summary
detect_vol_continuation = _legacy.detect_vol_continuation

# Preserve references to frozen helpers before monkey-patching the legacy module.
_legacy_load_recent = _legacy._load_recent
_legacy_first_m5_close_below = _legacy._first_m5_close_below


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


# Patch the frozen module because its evaluate() resolves helpers/functions in
# its own module globals. This keeps every untouched production path identical.
_legacy._prev_day_hl = _prev_day_hl
_legacy._load_recent = _load_recent
_legacy._first_m5_close_below = _fresh_m5_cross_below  # Asian detector: latest cross only
_legacy.detect_pdl_break = detect_pdl_break

# evaluate() remains the frozen implementation, now resolving only the patched
# level/freshness functions above.
evaluate = _legacy.evaluate
detect_asian_breakdown = _legacy.detect_asian_breakdown

# Keep the old research helper available to explicit callers; production does
# not use it after this fix.
_first_m5_close_below = _legacy_first_m5_close_below

__all__ = [
    "PredatorSignal", "evaluate", "format_telegram_alert",
    "format_telegram_invalidated", "format_predator_execution_summary",
    "detect_asian_breakdown", "detect_pdl_break", "detect_vol_continuation",
    "_ARCHETYPE_STATS", "_EXPECTED_TOTAL_MOVE_PTS", "_EXTENSION_LIMIT",
]
