"""Unit tests for Opportunity Coverage Evaluator (Phase 13)."""
import sys, os
from datetime import datetime, timezone, timedelta
from types import SimpleNamespace

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from services.opportunity_coverage import (
    _find_expansions, _atr_d1, _pct_of_move_captured,
    _judge, Expansion, CoverageReport,
)


def _bar(ts, o, h, l, c):
    return SimpleNamespace(time=ts, open=o, high=h, low=l, close=c, volume=1)


def _flat_d1(n=20, mid=4000, wick=15):
    """D1 bars with reasonable ATR. Returns bars sorted oldest-first."""
    base = datetime(2026, 7, 1, tzinfo=timezone.utc)
    bars = []
    for i in range(n):
        ts = base + timedelta(days=i)
        # Alternating up/down bars — ATR ~15
        bars.append(_bar(ts, mid, mid + wick, mid - wick, mid))
    return bars


def _uptrend_h1(n=6, start_price=4000, step_per_bar=5, base_wick=1):
    """N H1 bars that trend up strongly."""
    base = datetime(2026, 8, 1, 0, 0, tzinfo=timezone.utc)
    bars = []
    for i in range(n):
        ts = base + timedelta(hours=i)
        c = start_price + i * step_per_bar
        o = start_price + (i - 1) * step_per_bar if i > 0 else c
        bars.append(_bar(ts, o, max(o, c) + base_wick, min(o, c) - base_wick, c))
    return bars


def _downtrend_h1(n=6, start_price=4100, step_per_bar=-5, base_wick=1):
    return _uptrend_h1(n=n, start_price=start_price, step_per_bar=step_per_bar,
                        base_wick=base_wick)


def _flat_h1(n=6, mid=4000):
    base = datetime(2026, 8, 1, 0, 0, tzinfo=timezone.utc)
    return [_bar(base + timedelta(hours=i), mid, mid + 1, mid - 1, mid)
             for i in range(n)]


# ─────────────────────────────────────────────────────────────────────────────
# ATR helper
# ─────────────────────────────────────────────────────────────────────────────

def test_atr_d1_computed_from_15_bars():
    d1 = _flat_d1(20, mid=4000, wick=15)
    atr = _atr_d1(d1, n=14)
    assert atr is not None and atr > 0


def test_atr_d1_none_when_insufficient_bars():
    d1 = _flat_d1(5, mid=4000)
    assert _atr_d1(d1, n=14) is None


# ─────────────────────────────────────────────────────────────────────────────
# Detection
# ─────────────────────────────────────────────────────────────────────────────

def test_no_expansion_on_flat_bars():
    """Flat H1 with tiny wicks — no move qualifies."""
    h1 = _flat_h1(10, mid=4000)
    exps = _find_expansions(h1, atr_d1=20.0, min_atr_mult=1.5)
    assert exps == []


def test_bullish_expansion_detected_when_large_move():
    """H1 up 40pts in 6 bars, D1 ATR 20 → 2× ATR → detected."""
    h1 = _uptrend_h1(n=6, start_price=4000, step_per_bar=7)  # ends ~4035
    exps = _find_expansions(h1, atr_d1=15.0, min_atr_mult=1.5, max_hours=8)
    bulls = [e for e in exps if e.direction == "BULL"]
    assert len(bulls) >= 1
    assert bulls[0].total_distance >= 22.5  # >= 1.5 * 15
    assert bulls[0].atr_multiple >= 1.5


def test_bearish_expansion_detected_when_large_move():
    h1 = _downtrend_h1(n=6, start_price=4100, step_per_bar=-7)
    exps = _find_expansions(h1, atr_d1=15.0, min_atr_mult=1.5, max_hours=8)
    bears = [e for e in exps if e.direction == "BEAR"]
    assert len(bears) >= 1
    assert bears[0].total_distance >= 22.5


def test_no_expansion_when_over_retraced():
    """Big move followed by 60% retrace — should NOT qualify."""
    base = datetime(2026, 8, 1, tzinfo=timezone.utc)
    bars = [
        _bar(base, 4000, 4001, 3999, 4000),
        _bar(base + timedelta(hours=1), 4000, 4030, 3999, 4028),  # up thrust
        _bar(base + timedelta(hours=2), 4028, 4030, 4010, 4012),  # partial fade
        _bar(base + timedelta(hours=3), 4012, 4013, 3995, 3998),  # bigger fade — most of move gone
    ]
    exps = _find_expansions(bars, atr_d1=15.0, min_atr_mult=1.5,
                              max_retrace_pct=0.30)
    # Because retracement went to 3995 while peak was 4030, retrace_pct is
    # (4030-3995)/(4030-3999) ≈ 113% → fails max_retrace_pct
    bulls = [e for e in exps if e.direction == "BULL"]
    assert bulls == [], f"expected no bulls, got {bulls}"


def test_dedupe_keeps_larger_of_overlapping_bull_expansions():
    """Two overlapping bullish expansions — keep the larger."""
    base = datetime(2026, 8, 1, tzinfo=timezone.utc)
    bars = []
    # 15 bars trending up 6pts each = 90pts total
    for i in range(15):
        c = 4000 + i * 6
        o = 4000 + (i - 1) * 6 if i > 0 else c
        bars.append(_bar(base + timedelta(hours=i), o,
                          max(o, c) + 1, min(o, c) - 1, c))
    exps = _find_expansions(bars, atr_d1=15.0, min_atr_mult=1.5,
                              min_hours_apart=4)
    bulls = [e for e in exps if e.direction == "BULL"]
    # After dedupe there should be ≤3 bulls (~15 bars / 4 hrs apart)
    assert len(bulls) <= 4


# ─────────────────────────────────────────────────────────────────────────────
# Coverage helpers
# ─────────────────────────────────────────────────────────────────────────────

def test_pct_of_move_captured_full_when_alert_before():
    exp = Expansion(direction="BULL",
                     started_at=datetime(2026,8,1,12,tzinfo=timezone.utc),
                     ended_at=datetime(2026,8,1,14,tzinfo=timezone.utc),
                     trigger_level=4000, total_distance=30,
                     atr_multiple=2.0, max_retracement_pct=10)
    pct = _pct_of_move_captured(exp, datetime(2026,8,1,11,tzinfo=timezone.utc))
    assert pct == 100.0


def test_pct_of_move_captured_zero_when_alert_after():
    exp = Expansion(direction="BULL",
                     started_at=datetime(2026,8,1,12,tzinfo=timezone.utc),
                     ended_at=datetime(2026,8,1,14,tzinfo=timezone.utc),
                     trigger_level=4000, total_distance=30,
                     atr_multiple=2.0, max_retracement_pct=10)
    pct = _pct_of_move_captured(exp, datetime(2026,8,1,15,tzinfo=timezone.utc))
    assert pct == 0.0


def test_pct_of_move_captured_partial():
    """Alert 30 min into a 120-min move → captured 75%."""
    exp = Expansion(direction="BULL",
                     started_at=datetime(2026,8,1,12,tzinfo=timezone.utc),
                     ended_at=datetime(2026,8,1,14,tzinfo=timezone.utc),
                     trigger_level=4000, total_distance=30,
                     atr_multiple=2.0, max_retracement_pct=10)
    pct = _pct_of_move_captured(exp, datetime(2026,8,1,12,30,tzinfo=timezone.utc))
    assert 70 <= pct <= 80


def test_pct_none_when_alert_none():
    exp = Expansion(direction="BULL",
                     started_at=datetime(2026,8,1,12,tzinfo=timezone.utc),
                     ended_at=datetime(2026,8,1,14,tzinfo=timezone.utc),
                     trigger_level=4000, total_distance=30,
                     atr_multiple=2.0, max_retracement_pct=10)
    assert _pct_of_move_captured(exp, None) is None


# ─────────────────────────────────────────────────────────────────────────────
# Verdict function
# ─────────────────────────────────────────────────────────────────────────────

def test_verdict_insufficient_sample_when_under_5():
    v = _judge(overall_pct=100, med_delay=10, missed=0, total=3, late=0)
    assert "INSUFFICIENT_SAMPLE" in v


def test_verdict_on_target_when_high_coverage_low_delay():
    v = _judge(overall_pct=75, med_delay=15, missed=5, total=20, late=1)
    assert "ON TARGET" in v


def test_verdict_under_detecting_when_coverage_low():
    v = _judge(overall_pct=30, med_delay=15, missed=14, total=20, late=1)
    assert "UNDER-DETECTING" in v


def test_verdict_late_detection_when_delay_too_high():
    v = _judge(overall_pct=80, med_delay=90, missed=4, total=20, late=15)
    assert "LATE DETECTION" in v


def test_verdict_below_target_when_moderate_coverage():
    v = _judge(overall_pct=50, med_delay=20, missed=10, total=20, late=1)
    assert "BELOW TARGET" in v


# ─────────────────────────────────────────────────────────────────────────────
# Fingerprint idempotency
# ─────────────────────────────────────────────────────────────────────────────

def test_expansion_fingerprint_deterministic():
    exp1 = Expansion(direction="BULL",
                      started_at=datetime(2026,8,1,12,0,tzinfo=timezone.utc),
                      ended_at=datetime(2026,8,1,14,0,tzinfo=timezone.utc),
                      trigger_level=4000, total_distance=30,
                      atr_multiple=2.0, max_retracement_pct=10)
    exp2 = Expansion(direction="BULL",
                      started_at=datetime(2026,8,1,12,0,tzinfo=timezone.utc),
                      ended_at=datetime(2026,8,1,15,0,tzinfo=timezone.utc),  # different end
                      trigger_level=4000, total_distance=30,
                      atr_multiple=2.0, max_retracement_pct=10)
    assert exp1.fingerprint() == exp2.fingerprint()      # same start + distance = same fp


def test_expansion_fingerprint_differs_by_direction():
    exp_b = Expansion(direction="BULL",
                       started_at=datetime(2026,8,1,12,0,tzinfo=timezone.utc),
                       ended_at=datetime(2026,8,1,14,tzinfo=timezone.utc),
                       trigger_level=4000, total_distance=30,
                       atr_multiple=2.0, max_retracement_pct=10)
    exp_s = Expansion(direction="BEAR",
                       started_at=datetime(2026,8,1,12,0,tzinfo=timezone.utc),
                       ended_at=datetime(2026,8,1,14,tzinfo=timezone.utc),
                       trigger_level=4000, total_distance=30,
                       atr_multiple=2.0, max_retracement_pct=10)
    assert exp_b.fingerprint() != exp_s.fingerprint()


if __name__ == "__main__":
    import subprocess
    subprocess.run(["python", "-m", "pytest", __file__, "-v"], check=False)
