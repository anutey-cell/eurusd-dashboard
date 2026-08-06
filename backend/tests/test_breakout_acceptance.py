"""
Unit tests for Breakout Acceptance (Phase 6).
"""
import sys, os
from datetime import datetime, timezone, timedelta

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from services.breakout_acceptance import (
    classify_breakout, scan_key_levels,
    BK_LIQUIDITY_PROBE, BK_FAILED, BK_DEVELOPING, BK_CONFIRMED,
    BK_ACCEPTED, BK_RETEST, BK_CONTINUATION, BK_EXHAUSTED,
    BK_INVALIDATED, BK_NONE,
)
from services.canonical_market_data import (
    Bar, CanonicalSnapshot, TimeframeSlice, LevelBundle, SessionInfo,
)


def _mk(ts, o, h, l, c, v=1):
    return Bar(time=ts, open=o, high=h, low=l, close=c, volume=v)


def _snap_with_m15(m15_bars, h1_bars=None):
    """Wrap given M15 (and optional H1) bars in a snapshot."""
    if h1_bars is None:
        h1_bars = [_mk(m15_bars[0].time - timedelta(hours=i),
                        4100, 4105, 4095, 4100) for i in range(30)][::-1]
    return CanonicalSnapshot(
        ts=m15_bars[-1].time, instrument="XAU/USD",
        timeframes={"M15": TimeframeSlice("M15", m15_bars),
                     "H1":  TimeframeSlice("H1",  h1_bars)},
        levels=LevelBundle(pdh=4100, pdl=4090),
        session=SessionInfo("NY_OPEN", "NY open", is_active=True,
                             session_open=m15_bars[0].time),
        data_quality_score=100,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Fixtures for canonical breakout patterns
# ─────────────────────────────────────────────────────────────────────────────

def _pattern_confirmed_up(level=4100, n_pre=20, n_follow=3):
    """Bars below level, then 1 breakout candle, then n_follow more above."""
    now = datetime(2026, 8, 5, 14, 0, tzinfo=timezone.utc)
    bars = []
    # Pre: flat below level
    for i in range(n_pre):
        ts = now - timedelta(minutes=(n_pre + n_follow + 1 - i) * 15)
        bars.append(_mk(ts, level - 5, level - 3, level - 7, level - 4))
    # Breakout candle
    bo_ts = now - timedelta(minutes=(n_follow + 1) * 15)
    bars.append(_mk(bo_ts, level - 3, level + 8, level - 4, level + 6))
    # Follow-through
    for i in range(n_follow):
        ts = now - timedelta(minutes=(n_follow - i) * 15)
        bars.append(_mk(ts, level + 5, level + 12, level + 4, level + 10))
    return bars


def _pattern_failed_up(level=4100):
    """Breakout candle + immediate return within 3 bars."""
    now = datetime(2026, 8, 5, 14, 0, tzinfo=timezone.utc)
    bars = []
    for i in range(20):
        ts = now - timedelta(minutes=(24 - i) * 15)
        bars.append(_mk(ts, level - 5, level - 3, level - 7, level - 4))
    # Breakout candle
    bars.append(_mk(now - timedelta(minutes=60), level - 3, level + 8, level - 4, level + 6))
    # Return
    bars.append(_mk(now - timedelta(minutes=45), level + 4, level + 5, level - 3, level - 2))
    bars.append(_mk(now - timedelta(minutes=30), level - 2, level, level - 5, level - 3))
    bars.append(_mk(now - timedelta(minutes=15), level - 3, level - 1, level - 6, level - 4))
    return bars


def _pattern_liquidity_probe_up(level=4100):
    """Wick past level, close well below — no breakout candle."""
    now = datetime(2026, 8, 5, 14, 0, tzinfo=timezone.utc)
    bars = []
    for i in range(20):
        ts = now - timedelta(minutes=(21 - i) * 15)
        bars.append(_mk(ts, level - 5, level - 3, level - 7, level - 4))
    # Wick past level but close BELOW (below body_pct_beyond threshold via low open)
    bars.append(_mk(now, level - 4, level + 5, level - 5, level - 2))
    return bars


def _pattern_retest_hold_up(level=4100):
    """Breakout + pullback to level + hold above."""
    now = datetime(2026, 8, 5, 14, 0, tzinfo=timezone.utc)
    bars = []
    for i in range(20):
        ts = now - timedelta(minutes=(28 - i) * 15)
        bars.append(_mk(ts, level - 5, level - 3, level - 7, level - 4))
    # Breakout candle
    bars.append(_mk(now - timedelta(minutes=105), level - 3, level + 8, level - 4, level + 6))
    # Follow-through
    for i in range(3):
        bars.append(_mk(now - timedelta(minutes=(90 - i * 15)),
                         level + 5, level + 10, level + 4, level + 8))
    # Retest: dip back to level+1 area (holds above)
    bars.append(_mk(now - timedelta(minutes=45), level + 8, level + 9, level + 1, level + 3))
    bars.append(_mk(now - timedelta(minutes=30), level + 3, level + 7, level + 2, level + 6))
    bars.append(_mk(now - timedelta(minutes=15), level + 6, level + 10, level + 5, level + 9))
    return bars


def _pattern_confirmed_down(level=4090):
    """Mirror of confirmed_up."""
    now = datetime(2026, 8, 5, 14, 0, tzinfo=timezone.utc)
    bars = []
    for i in range(20):
        ts = now - timedelta(minutes=(24 - i) * 15)
        bars.append(_mk(ts, level + 5, level + 7, level + 3, level + 4))
    bars.append(_mk(now - timedelta(minutes=60), level + 3, level + 4, level - 8, level - 6))
    for i in range(3):
        bars.append(_mk(now - timedelta(minutes=(45 - i * 15)),
                         level - 5, level - 4, level - 12, level - 10))
    return bars


# ─────────────────────────────────────────────────────────────────────────────
# Tests
# ─────────────────────────────────────────────────────────────────────────────

def test_no_snapshot_returns_no_breakout():
    a = classify_breakout(None, level=4100, direction="UP")
    assert a.classification == BK_NONE


def test_insufficient_bars_returns_no_breakout():
    snap = CanonicalSnapshot(
        ts=datetime.now(timezone.utc), instrument="XAU/USD",
        timeframes={"M15": TimeframeSlice("M15", [])},
        levels=LevelBundle(), data_quality_score=100,
    )
    a = classify_breakout(snap, level=4100, direction="UP")
    assert a.classification == BK_NONE


def test_confirmed_breakout_up():
    """1 breakout + 3 follow-through candles = ACCEPTED (≥3 ft + should get time≥45)."""
    snap = _snap_with_m15(_pattern_confirmed_up(level=4100, n_follow=3))
    a = classify_breakout(snap, level=4100, direction="UP", level_name="PDH")
    assert a.classification in (BK_ACCEPTED, BK_CONFIRMED, BK_CONTINUATION), \
        f"got {a.classification} ft={a.followthrough_bars} time={a.time_outside_min}"
    assert a.close_beyond is True
    assert a.followthrough_bars >= 2


def test_confirmed_breakout_down():
    snap = _snap_with_m15(_pattern_confirmed_down(level=4090))
    a = classify_breakout(snap, level=4090, direction="DOWN", level_name="PDL")
    assert a.classification in (BK_ACCEPTED, BK_CONFIRMED, BK_CONTINUATION), \
        f"got {a.classification}"


def test_failed_breakout_up():
    snap = _snap_with_m15(_pattern_failed_up(level=4100))
    a = classify_breakout(snap, level=4100, direction="UP", level_name="PDH")
    assert a.classification == BK_FAILED, f"got {a.classification} ft={a.followthrough_bars} returned={a.returned_to_range}"


def test_liquidity_probe_up():
    snap = _snap_with_m15(_pattern_liquidity_probe_up(level=4100))
    a = classify_breakout(snap, level=4100, direction="UP", level_name="PDH")
    # Wick past + close back = LIQUIDITY_PROBE (no BO index found)
    assert a.classification in (BK_LIQUIDITY_PROBE, BK_NONE), f"got {a.classification}"


def test_retest_hold_up():
    """Retest that came near level and held → BK_RETEST or CONTINUATION."""
    snap = _snap_with_m15(_pattern_retest_hold_up(level=4100))
    a = classify_breakout(snap, level=4100, direction="UP", level_name="PDH")
    assert a.classification in (BK_RETEST, BK_CONTINUATION, BK_ACCEPTED, BK_CONFIRMED), \
        f"got {a.classification} depth={a.retest_depth_pct} time={a.time_outside_min}"


def test_wick_past_but_no_close_returns_liquidity_probe():
    """Bar wicks above level but closes below — no breakout bar, wicked_past = True."""
    now = datetime(2026, 8, 5, 14, 0, tzinfo=timezone.utc)
    bars = []
    for i in range(20):
        ts = now - timedelta(minutes=(21 - i) * 15)
        bars.append(_mk(ts, 4095, 4097, 4092, 4094))
    # Recent wick past but close way below
    bars.append(_mk(now, 4095, 4108, 4094, 4093))
    snap = _snap_with_m15(bars)
    a = classify_breakout(snap, level=4100, direction="UP")
    assert a.classification == BK_LIQUIDITY_PROBE


# ─────────────────────────────────────────────────────────────────────────────
# scan_key_levels helper
# ─────────────────────────────────────────────────────────────────────────────

def test_scan_key_levels_returns_assessments_for_levels_present():
    snap = _snap_with_m15(_pattern_confirmed_up(level=4100))
    snap.levels = LevelBundle(pdh=4100, pdl=4050,
                                asian_high=4100, asian_low=4060,
                                pwh=4200, pwl=4000)
    results = scan_key_levels(snap)
    # PDH + PWH + ASIAN_HIGH (up) + PDL + PWL + ASIAN_LOW (down) = 6
    assert len(results) == 6
    up_results = [r for r in results if r.direction == "UP"]
    down_results = [r for r in results if r.direction == "DOWN"]
    assert len(up_results) == 3
    assert len(down_results) == 3


def test_scan_key_levels_empty_when_no_levels():
    snap = _snap_with_m15(_pattern_confirmed_up(level=4100))
    snap.levels = LevelBundle()  # all None
    results = scan_key_levels(snap)
    assert results == []


# ─────────────────────────────────────────────────────────────────────────────
# Never raises
# ─────────────────────────────────────────────────────────────────────────────

def test_never_raises_on_bad_direction():
    snap = _snap_with_m15(_pattern_confirmed_up(level=4100))
    a = classify_breakout(snap, level=4100, direction="SIDEWAYS")
    assert a.classification == BK_NONE


if __name__ == "__main__":
    import subprocess
    subprocess.run(["python", "-m", "pytest", __file__, "-v"], check=False)
