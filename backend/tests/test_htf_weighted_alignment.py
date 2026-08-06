"""
Unit tests for Weighted HTF Alignment (Phase 4).

Covers:
  - Full bull stack → STRONG_BULL
  - Full bear stack → STRONG_BEAR
  - The exact scenario the current code misses: D1 neutral + H4 neutral +
    H1 bull + M15 bull → WEAK_BULL / MEDIUM_BULL (not NEUTRAL)
  - D1 bear + H1 bull mixed → NEUTRAL near zero
  - No data → snapshot returns NEUTRAL NONE, no exception
  - Symmetric bear equivalents
"""
import sys, os
from datetime import datetime, timezone, timedelta

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from services.htf_weighted_alignment import (
    compute_htf_alignment, HTFAlignment, _WEIGHTS,
)
from services.canonical_market_data import (
    Bar, CanonicalSnapshot, TimeframeSlice, LevelBundle,
)


def _mk_bar(ts, o, h, l, c):
    return Bar(time=ts, open=o, high=h, low=l, close=c, volume=1)


def _bars_uptrend(n=80, start=4000.0, per_bar=1.5, tf_min=60):
    """Simple uptrend with realistic HL wicks."""
    now = datetime(2026, 8, 5, 12, 0, tzinfo=timezone.utc)
    bars = []
    for i in range(n):
        ts = now - timedelta(minutes=(n - i - 1) * tf_min)
        c = start + i * per_bar
        o = start + (i - 1) * per_bar if i > 0 else c
        h = max(c, o) + 1
        l = min(c, o) - 1
        bars.append(_mk_bar(ts, o, h, l, c))
    return bars


def _bars_downtrend(n=80, start=4100.0, per_bar=-1.5, tf_min=60):
    return _bars_uptrend(n=n, start=start, per_bar=per_bar, tf_min=tf_min)


def _bars_flat(n=80, mid=4050.0, jitter=0.5, tf_min=60):
    import random
    random.seed(42)
    now = datetime(2026, 8, 5, 12, 0, tzinfo=timezone.utc)
    bars = []
    for i in range(n):
        ts = now - timedelta(minutes=(n - i - 1) * tf_min)
        c = mid + random.uniform(-jitter, jitter)
        o = mid + random.uniform(-jitter, jitter)
        h = max(c, o) + 0.5
        l = min(c, o) - 0.5
        bars.append(_mk_bar(ts, o, h, l, c))
    return bars


def _snap(tf_bars: dict[str, list]) -> CanonicalSnapshot:
    tfs = {tf: TimeframeSlice(tf=tf, candles=bars)
           for tf, bars in tf_bars.items()}
    return CanonicalSnapshot(
        ts=datetime.now(timezone.utc), instrument="XAU/USD",
        timeframes=tfs, levels=LevelBundle(), data_quality_score=100,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Direction unanimity tests
# ─────────────────────────────────────────────────────────────────────────────

def test_full_bull_stack_is_strong_bull():
    """All 5 TFs strongly bullish → strong bull. Uses steeper trends so LTF
    EMA separation exceeds the DEAD_ZONE."""
    snap = _snap({
        "D1":  _bars_uptrend(n=80, start=3800, per_bar=3.0, tf_min=1440),
        "H4":  _bars_uptrend(n=80, start=3900, per_bar=2.0, tf_min=240),
        "H1":  _bars_uptrend(n=80, start=4000, per_bar=1.5, tf_min=60),
        "M15": _bars_uptrend(n=80, start=4100, per_bar=0.8, tf_min=15),
        "M5":  _bars_uptrend(n=80, start=4150, per_bar=1.5, tf_min=5),   # steeper so EMA sep > dead_zone
    })
    a = compute_htf_alignment(snap)
    assert a.direction == "BULL", f"got {a.direction} score={a.score}"
    assert a.strength == "STRONG", f"got {a.strength} score={a.score}"
    assert a.score >= 60
    # At least the three most-weighted TFs must be bullish (H4+H1+M15 = 75%)
    assert {"H4", "H1", "M15"}.issubset(set(a.bull_tfs)), f"bull_tfs={a.bull_tfs}"


def test_full_bear_stack_is_strong_bear():
    snap = _snap({
        "D1":  _bars_downtrend(n=80, start=4200, per_bar=-3.0, tf_min=1440),
        "H4":  _bars_downtrend(n=80, start=4100, per_bar=-2.0, tf_min=240),
        "H1":  _bars_downtrend(n=80, start=4050, per_bar=-1.5, tf_min=60),
        "M15": _bars_downtrend(n=80, start=4030, per_bar=-0.8, tf_min=15),
        "M5":  _bars_downtrend(n=80, start=4020, per_bar=-0.3, tf_min=5),
    })
    a = compute_htf_alignment(snap)
    assert a.direction == "BEAR", f"got {a.direction} score={a.score}"
    assert a.strength == "STRONG"
    assert a.score <= -60
    assert a.unanimous is True


# ─────────────────────────────────────────────────────────────────────────────
# The CORE fix: bullish transition where D1 + H4 are neutral
# ─────────────────────────────────────────────────────────────────────────────

def test_bullish_transition_from_neutral_higher_htf():
    """D1 flat, H4 flat, H1 bull, M15 bull. Old code = STAND ASIDE. New = WEAK+ BULL."""
    snap = _snap({
        "D1":  _bars_flat(n=80, mid=4050, jitter=1.0, tf_min=1440),
        "H4":  _bars_flat(n=80, mid=4050, jitter=1.5, tf_min=240),
        "H1":  _bars_uptrend(n=80, start=4000, per_bar=2.0, tf_min=60),
        "M15": _bars_uptrend(n=80, start=4100, per_bar=1.0, tf_min=15),
        "M5":  _bars_uptrend(n=80, start=4150, per_bar=0.4, tf_min=5),
    })
    a = compute_htf_alignment(snap)
    assert a.direction == "BULL", f"got {a.direction} score={a.score} per_tf={a.per_tf}"
    assert a.strength in ("WEAK", "MEDIUM", "STRONG"), f"got {a.strength} score={a.score}"
    # Must have at least H1+M15+M5 registering as BULL
    assert set(a.bull_tfs) >= {"H1", "M15", "M5"}, f"bull_tfs={a.bull_tfs}"


def test_bearish_transition_from_neutral_higher_htf():
    """Mirror of the bullish transition case."""
    snap = _snap({
        "D1":  _bars_flat(n=80, mid=4050, jitter=1.0, tf_min=1440),
        "H4":  _bars_flat(n=80, mid=4050, jitter=1.5, tf_min=240),
        "H1":  _bars_downtrend(n=80, start=4100, per_bar=-2.0, tf_min=60),
        "M15": _bars_downtrend(n=80, start=4000, per_bar=-1.0, tf_min=15),
        "M5":  _bars_downtrend(n=80, start=3950, per_bar=-0.4, tf_min=5),
    })
    a = compute_htf_alignment(snap)
    assert a.direction == "BEAR", f"got {a.direction} score={a.score}"
    assert a.strength in ("WEAK", "MEDIUM", "STRONG")
    assert set(a.bear_tfs) >= {"H1", "M15", "M5"}


# ─────────────────────────────────────────────────────────────────────────────
# Conflict + neutral
# ─────────────────────────────────────────────────────────────────────────────

def test_conflicting_htf_scores_near_zero():
    """D1 bear + H4 bear vs H1 bull + M15 bull → close to neutral."""
    snap = _snap({
        "D1":  _bars_downtrend(n=80, start=4200, per_bar=-3.0, tf_min=1440),
        "H4":  _bars_downtrend(n=80, start=4100, per_bar=-2.0, tf_min=240),
        "H1":  _bars_uptrend(n=80, start=4000, per_bar=2.0, tf_min=60),
        "M15": _bars_uptrend(n=80, start=4100, per_bar=1.0, tf_min=15),
        "M5":  _bars_flat(n=80, mid=4100, tf_min=5),
    })
    a = compute_htf_alignment(snap)
    # Absolute score should be modest — not STRONG
    assert a.strength in ("NONE", "WEAK", "MEDIUM"), f"strength={a.strength} score={a.score}"
    # Should NOT be unanimous
    assert a.unanimous is False


def test_all_flat_is_neutral():
    snap = _snap({tf: _bars_flat(n=80, mid=4050, jitter=0.3,
                                    tf_min={"D1":1440,"H4":240,"H1":60,"M15":15,"M5":5}[tf])
                   for tf in _WEIGHTS.keys()})
    a = compute_htf_alignment(snap)
    assert a.direction == "NEUTRAL"
    assert a.strength == "NONE"
    assert abs(a.score) < 15


# ─────────────────────────────────────────────────────────────────────────────
# Failing open
# ─────────────────────────────────────────────────────────────────────────────

def test_no_snapshot_returns_neutral_none():
    a = compute_htf_alignment(None)
    assert a.direction == "NEUTRAL"
    assert a.strength == "NONE"
    assert a.score == 0.0
    assert "snapshot is None" in " ".join(a.warnings)


def test_empty_timeframes_returns_neutral_none():
    snap = CanonicalSnapshot(
        ts=datetime.now(timezone.utc), instrument="XAU/USD",
        timeframes={}, levels=LevelBundle(), data_quality_score=100,
    )
    a = compute_htf_alignment(snap)
    assert a.direction == "NEUTRAL"
    assert a.score == 0.0


def test_partial_timeframes_still_scores():
    """Only H1 + M15 available (D1/H4/M5 missing) — must not raise."""
    snap = _snap({
        "H1":  _bars_uptrend(n=80, start=4000, per_bar=2.0, tf_min=60),
        "M15": _bars_uptrend(n=80, start=4100, per_bar=1.0, tf_min=15),
    })
    a = compute_htf_alignment(snap)
    # Even with only H1+M15 contributing (weight 45), score should still be BULL
    assert a.direction == "BULL"
    # D1, H4, M5 should be in neutral_tfs
    assert "D1" in a.neutral_tfs
    assert "H4" in a.neutral_tfs
    assert "M5" in a.neutral_tfs


# ─────────────────────────────────────────────────────────────────────────────
# Weight totals sanity check
# ─────────────────────────────────────────────────────────────────────────────

def test_weights_sum_to_100():
    assert sum(_WEIGHTS.values()) == 100


if __name__ == "__main__":
    import subprocess
    subprocess.run(["python", "-m", "pytest", __file__, "-v"], check=False)
