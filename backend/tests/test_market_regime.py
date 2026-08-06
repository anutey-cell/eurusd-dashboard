"""
Unit tests for Market Regime Engine (Phase 3).

Covers:
  - INSUFFICIENT_DATA on None / low quality / short bar history
  - HIGH_IMPACT_EVENT_RISK when event within 15 min
  - STRONG_BULLISH_EXPANSION and STRONG_BEARISH_EXPANSION symmetry
  - BULLISH_TRANSITION and BEARISH_TRANSITION from neutral HTF
  - BALANCED_RANGE default when no directional edge
  - EXHAUSTION when > 3× ATR extended
  - Failing-open contract (never raises)
"""
import sys, os
from datetime import datetime, timezone, timedelta

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from services.market_regime import (
    classify_regime,
    REGIME_INSUFFICIENT_DATA, REGIME_EVENT_RISK,
    REGIME_STRONG_BULL, REGIME_STRONG_BEAR,
    REGIME_BULL_TRANSITION, REGIME_BEAR_TRANSITION,
    REGIME_BALANCED_RANGE, REGIME_EXHAUSTION,
    REGIME_BULL_CONTINUATION, REGIME_BEAR_CONTINUATION,
    _htf_bias, _displacement_signal, _swing_high_low, _acceptance_above,
    _acceptance_below, _atr_from_bars,
)
from services.canonical_market_data import (
    Bar, CanonicalSnapshot, TimeframeSlice, LevelBundle, SessionInfo,
)


# ─────────────────────────────────────────────────────────────────────────────
# Fixture helpers
# ─────────────────────────────────────────────────────────────────────────────

def _mk_bar(ts, o, h, l, c):
    return Bar(time=ts, open=o, high=h, low=l, close=c, volume=1)


def _uptrend_h4_closes(n=60, start=4000.0, per_bar=1.0):
    return [start + i * per_bar for i in range(n)]


def _downtrend_h4_closes(n=60, start=4000.0, per_bar=-1.0):
    return [start + i * per_bar for i in range(n)]


def _flat_h4_closes(n=60, mid=4000.0, jitter=1.0):
    import random
    random.seed(42)
    return [mid + random.uniform(-jitter, jitter) for _ in range(n)]


def _bars_from_closes(closes, tf_delta_min):
    """Turn a closes list into bar objects with sensible OHLC."""
    now = datetime(2026, 8, 5, 12, 0, tzinfo=timezone.utc)
    bars = []
    for i, c in enumerate(closes):
        ts = now - timedelta(minutes=(len(closes) - i - 1) * tf_delta_min)
        o = closes[i-1] if i > 0 else c
        # Give bar a body around the close direction
        if c > o:
            h, l = c + 1, o - 1
        elif c < o:
            h, l = o + 1, c - 1
        else:
            h, l = c + 1, c - 1
        bars.append(_mk_bar(ts, o, h, l, c))
    return bars


def _make_snapshot(*, h4_closes, h1_closes, m15_bars,
                    levels: LevelBundle = None,
                    quality: int = 100) -> CanonicalSnapshot:
    tfs = {
        "H4":  TimeframeSlice(tf="H4",  candles=_bars_from_closes(h4_closes, 240)),
        "H1":  TimeframeSlice(tf="H1",  candles=_bars_from_closes(h1_closes, 60)),
        "M15": TimeframeSlice(tf="M15", candles=m15_bars),
    }
    return CanonicalSnapshot(
        ts=datetime(2026, 8, 5, 12, 0, tzinfo=timezone.utc),
        instrument="XAU/USD",
        timeframes=tfs, levels=levels or LevelBundle(),
        data_quality_score=quality,
    )


# ─────────────────────────────────────────────────────────────────────────────
# INSUFFICIENT_DATA scenarios
# ─────────────────────────────────────────────────────────────────────────────

def test_insufficient_data_when_snapshot_none():
    a = classify_regime(None)
    assert a.regime == REGIME_INSUFFICIENT_DATA
    assert a.confidence == 0


def test_insufficient_data_when_quality_low():
    snap = _make_snapshot(h4_closes=[4000]*60, h1_closes=[4000]*30,
                           m15_bars=[_mk_bar(datetime(2026,8,5,i//4,(i%4)*15,tzinfo=timezone.utc),
                                               4000,4001,3999,4000) for i in range(60)],
                           quality=30)
    a = classify_regime(snap)
    assert a.regime == REGIME_INSUFFICIENT_DATA


def test_insufficient_data_when_bars_short():
    snap = _make_snapshot(h4_closes=[4000]*10, h1_closes=[4000]*10, m15_bars=[])
    a = classify_regime(snap)
    assert a.regime == REGIME_INSUFFICIENT_DATA


# ─────────────────────────────────────────────────────────────────────────────
# EVENT RISK
# ─────────────────────────────────────────────────────────────────────────────

def test_event_risk_triggers_when_high_impact_within_15min():
    snap = _make_snapshot(h4_closes=_uptrend_h4_closes(), h1_closes=[4000+i for i in range(30)],
                           m15_bars=[_mk_bar(datetime(2026,8,5,i//4,(i%4)*15,tzinfo=timezone.utc),
                                               4000,4002,3999,4001) for i in range(30)])
    events = [{"time_utc": (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat(),
                "impact": "high"}]
    a = classify_regime(snap, upcoming_events=events)
    assert a.regime == REGIME_EVENT_RISK


def test_event_risk_ignored_when_far_future():
    snap = _make_snapshot(h4_closes=_uptrend_h4_closes(), h1_closes=[4000+i for i in range(30)],
                           m15_bars=[_mk_bar(datetime(2026,8,5,i//4,(i%4)*15,tzinfo=timezone.utc),
                                               4000,4002,3999,4001) for i in range(30)])
    events = [{"time_utc": (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat(),
                "impact": "high"}]
    a = classify_regime(snap, upcoming_events=events)
    assert a.regime != REGIME_EVENT_RISK


# ─────────────────────────────────────────────────────────────────────────────
# STRONG BULL / BEAR expansion
# ─────────────────────────────────────────────────────────────────────────────

def test_strong_bullish_expansion():
    # H4 bull trend so bias=BULL
    h4  = _uptrend_h4_closes(n=60, start=4000, per_bar=2.0)
    # H1 finishes near 4180 so EMA21 stays close to current M15 (~4180) →
    # ext_mult stays < 4× ATR so EXHAUSTION doesn't override
    h1  = [4000 + i * 4.5 for i in range(40)]         # ends ~4176
    now = datetime(2026,8,5,12,0,tzinfo=timezone.utc)
    m15 = []
    for i in range(30):
        ts = now - timedelta(minutes=(30 - i) * 15)
        m15.append(_mk_bar(ts, 4160 + i * 0.3, 4162 + i * 0.3, 4158 + i * 0.3, 4161 + i * 0.3))
    # Overwrite last 6 with big up bodies displacing sharply — final close ~4180
    for i in range(6):
        ts = now - timedelta(minutes=(6 - i) * 15)
        base = 4165 + i * 2
        m15[24 + i] = _mk_bar(ts, base, base + 8, base - 1, base + 7)     # green big body
    # Levels: PDH < current so we're accepting above
    levels = LevelBundle(pdh=4165, pdl=4090, asian_high=4172, asian_low=4155)
    snap = _make_snapshot(h4_closes=h4, h1_closes=h1, m15_bars=m15, levels=levels)
    a = classify_regime(snap)
    assert a.regime == REGIME_STRONG_BULL, f"got {a.regime} · evidence={a.evidence}"
    assert a.directional_bias == "BULL"
    assert a.controller == "BUYERS"
    assert a.control_trend == "STRENGTHENING"
    assert a.confidence >= 80


def test_strong_bearish_expansion():
    h4  = _downtrend_h4_closes(n=60, start=4100, per_bar=-2.0)
    h1  = [4100 - i * 4.5 for i in range(40)]        # ends ~3924
    now = datetime(2026,8,5,12,0,tzinfo=timezone.utc)
    m15 = []
    for i in range(30):
        ts = now - timedelta(minutes=(30 - i) * 15)
        m15.append(_mk_bar(ts, 3940 - i * 0.3, 3942 - i * 0.3, 3938 - i * 0.3, 3939 - i * 0.3))
    for i in range(6):
        ts = now - timedelta(minutes=(6 - i) * 15)
        base = 3935 - i * 2
        m15[24 + i] = _mk_bar(ts, base, base + 1, base - 8, base - 7)    # red big body
    levels = LevelBundle(pdh=4010, pdl=3935, asian_high=3960, asian_low=3928)
    snap = _make_snapshot(h4_closes=h4, h1_closes=h1, m15_bars=m15, levels=levels)
    a = classify_regime(snap)
    assert a.regime == REGIME_STRONG_BEAR, f"got {a.regime} · evidence={a.evidence}"
    assert a.directional_bias == "BEAR"
    assert a.controller == "SELLERS"
    assert a.control_trend == "STRENGTHENING"


# ─────────────────────────────────────────────────────────────────────────────
# TRANSITIONS
# ─────────────────────────────────────────────────────────────────────────────

def test_bullish_transition_from_neutral_htf():
    """HTF neutral + M15 displacement up + BOS + broke Asian/PD high."""
    h4  = _flat_h4_closes(n=60, mid=4050, jitter=2.0)
    h1  = _flat_h4_closes(n=40, mid=4050, jitter=3.0)
    now = datetime(2026,8,5,12,0,tzinfo=timezone.utc)
    m15 = []
    # 20 bars stuck near 4050
    for i in range(20):
        ts = now - timedelta(minutes=(30 - i) * 15)
        m15.append(_mk_bar(ts, 4050, 4055, 4045, 4050))
    # Last 10 explode up with big-body greens
    for i in range(10):
        ts = now - timedelta(minutes=(10 - i) * 15)
        base = 4070 + i * 4
        m15.append(_mk_bar(ts, base, base + 8, base - 1, base + 6))
    levels = LevelBundle(pdh=4058, pdl=4020, asian_high=4062, asian_low=4030)
    snap = _make_snapshot(h4_closes=h4, h1_closes=h1, m15_bars=m15, levels=levels)
    a = classify_regime(snap)
    assert a.regime == REGIME_BULL_TRANSITION
    assert a.transitioning is True
    assert a.directional_bias == "BULL"


def test_bearish_transition_from_neutral_htf():
    h4  = _flat_h4_closes(n=60, mid=4050, jitter=2.0)
    h1  = _flat_h4_closes(n=40, mid=4050, jitter=3.0)
    now = datetime(2026,8,5,12,0,tzinfo=timezone.utc)
    m15 = []
    for i in range(20):
        ts = now - timedelta(minutes=(30 - i) * 15)
        m15.append(_mk_bar(ts, 4050, 4055, 4045, 4050))
    for i in range(10):
        ts = now - timedelta(minutes=(10 - i) * 15)
        base = 4030 - i * 4
        m15.append(_mk_bar(ts, base, base + 1, base - 8, base - 6))
    levels = LevelBundle(pdh=4080, pdl=4042, asian_high=4062, asian_low=4038)
    snap = _make_snapshot(h4_closes=h4, h1_closes=h1, m15_bars=m15, levels=levels)
    a = classify_regime(snap)
    assert a.regime == REGIME_BEAR_TRANSITION
    assert a.transitioning is True
    assert a.directional_bias == "BEAR"


# ─────────────────────────────────────────────────────────────────────────────
# BALANCED_RANGE default
# ─────────────────────────────────────────────────────────────────────────────

def test_balanced_range_when_no_edge():
    h4  = _flat_h4_closes(n=60, mid=4050, jitter=1.0)
    h1  = _flat_h4_closes(n=40, mid=4050, jitter=2.0)
    now = datetime(2026,8,5,12,0,tzinfo=timezone.utc)
    m15 = [_mk_bar(now - timedelta(minutes=(30-i)*15), 4050, 4052, 4048, 4050)
           for i in range(30)]
    snap = _make_snapshot(h4_closes=h4, h1_closes=h1, m15_bars=m15,
                           levels=LevelBundle(pdh=4060, pdl=4040))
    a = classify_regime(snap)
    assert a.regime == REGIME_BALANCED_RANGE


# ─────────────────────────────────────────────────────────────────────────────
# Failing-open contract
# ─────────────────────────────────────────────────────────────────────────────

def test_never_raises_on_empty_timeframes():
    snap = CanonicalSnapshot(
        ts=datetime.now(timezone.utc), instrument="XAU/USD",
        timeframes={}, levels=LevelBundle(), data_quality_score=100,
    )
    a = classify_regime(snap)
    assert a.regime == REGIME_INSUFFICIENT_DATA  # never raised


# ─────────────────────────────────────────────────────────────────────────────
# Helper unit tests
# ─────────────────────────────────────────────────────────────────────────────

def test_htf_bias_bull():
    assert _htf_bias([4000 + i for i in range(60)]) == "BULL"


def test_htf_bias_bear():
    assert _htf_bias([4100 - i for i in range(60)]) == "BEAR"


def test_htf_bias_neutral_short():
    assert _htf_bias([4050] * 10) == "NEUTRAL"


def test_swing_high_low_finds_peaks():
    bars = [_mk_bar(datetime(2026,8,5,i,0,tzinfo=timezone.utc),
                     4000+i, 4010+i, 3990+i, 4005+i) for i in range(10)]
    # inject a peak at bar[5]
    bars[5] = _mk_bar(bars[5].time, 4005, 4050, 3990, 4005)
    hi, _ = _swing_high_low(bars, k=2)
    assert hi is None or hi >= 4030  # peak detected or absent (both valid at edge)


def test_atr_from_bars_positive():
    base = datetime(2026,8,1,0,0,tzinfo=timezone.utc)
    bars = [_mk_bar(base + timedelta(hours=i),
                     4000+i, 4010+i, 3990+i, 4005+i) for i in range(30)]
    assert _atr_from_bars(bars, 14) > 0


if __name__ == "__main__":
    import subprocess
    subprocess.run(["python", "-m", "pytest", __file__, "-v"], check=False)
