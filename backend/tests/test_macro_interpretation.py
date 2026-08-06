"""Unit tests for Enhanced Macro Interpretation (Phase 10)."""
import sys, os
from datetime import datetime, timezone, timedelta

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from services.macro_interpretation import (
    compute_macro_context, MacroAssessment,
    _classify_correlation, _event_risk_level, _macro_alignment, _move_driver,
)
from services.canonical_market_data import Bar, TimeframeSlice, CanonicalSnapshot, LevelBundle


def _bars(n, start, per_bar, tf_min=60):
    base = datetime(2026, 8, 1, 0, 0, tzinfo=timezone.utc)
    bars = []
    for i in range(n):
        c = start + i * per_bar
        o = start + (i - 1) * per_bar if i > 0 else c
        h = max(o, c) + 0.1
        l = min(o, c) - 0.1
        ts = base + timedelta(minutes=i * tf_min)
        bars.append(Bar(time=ts, open=o, high=h, low=l, close=c, volume=1))
    return bars


def _snap_with_h1(bars):
    return CanonicalSnapshot(
        ts=datetime.now(timezone.utc), instrument="XAU/USD",
        timeframes={"H1": TimeframeSlice("H1", bars)},
        levels=LevelBundle(), data_quality_score=100,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Classifiers
# ─────────────────────────────────────────────────────────────────────────────

def test_correlation_classification():
    assert _classify_correlation(-0.75) == "ACTIVE_INVERSE"
    assert _classify_correlation(-0.45) == "WEAK"
    assert _classify_correlation(0.10) == "BROKEN"
    assert _classify_correlation(0.55) == "ACTIVE_POSITIVE"
    assert _classify_correlation(None) == "UNKNOWN"


def test_event_risk_level():
    assert _event_risk_level(None) == "NONE"
    assert _event_risk_level(5) == "HIGH"
    assert _event_risk_level(30) == "ELEVATED"
    assert _event_risk_level(120) == "LOW"
    assert _event_risk_level(500) == "NONE"


def test_macro_alignment_supportive_bull():
    label, reason = _macro_alignment("BULL", "DOWN", "DOWN")
    assert label == "SUPPORTIVE"


def test_macro_alignment_opposing_bull():
    label, reason = _macro_alignment("BULL", "UP", "UP")
    assert label == "OPPOSING"


def test_macro_alignment_mixed_bull():
    label, reason = _macro_alignment("BULL", "DOWN", "UP")
    assert label == "MIXED"


def test_macro_alignment_neutral_when_no_technical():
    label, reason = _macro_alignment("NEUTRAL", "DOWN", "DOWN")
    assert label == "NEUTRAL"


def test_macro_alignment_supportive_bear():
    label, reason = _macro_alignment("BEAR", "UP", "UP")
    assert label == "SUPPORTIVE"


def test_move_driver_macro_when_dxy_strong_move():
    d = _move_driver("BULL", gold_move_pct=0.8, dxy_move_pct=-0.5,
                      correlation_state="ACTIVE_INVERSE")
    assert d == "MACRO_DRIVEN"


def test_move_driver_technical_when_dxy_flat():
    d = _move_driver("BULL", gold_move_pct=0.8, dxy_move_pct=0.05,
                      correlation_state="ACTIVE_INVERSE")
    assert d == "TECHNICAL_DRIVEN"


def test_move_driver_technical_when_correlation_broken():
    d = _move_driver("BULL", gold_move_pct=0.5, dxy_move_pct=0.3,
                      correlation_state="BROKEN")
    assert d == "TECHNICAL_DRIVEN"


def test_move_driver_unclear_when_no_technical():
    d = _move_driver("NEUTRAL", gold_move_pct=0.5, dxy_move_pct=-0.3,
                      correlation_state="ACTIVE_INVERSE")
    assert d == "UNCLEAR"


# ─────────────────────────────────────────────────────────────────────────────
# Full assessment
# ─────────────────────────────────────────────────────────────────────────────

def test_no_inputs_returns_all_unknown():
    a = compute_macro_context()
    assert a.dxy_direction == "UNKNOWN"
    assert a.yield_10y_direction == "UNKNOWN"
    assert a.correlation_state == "UNKNOWN"
    assert a.macro_alignment == "NEUTRAL"
    assert a.event_risk_level == "NONE"


def test_dxy_bars_direction_up():
    dxy = _bars(40, start=100, per_bar=0.05)
    a = compute_macro_context(dxy_bars=dxy)
    assert a.dxy_direction == "UP"
    assert a.dxy_move_pct is not None and a.dxy_move_pct > 0


def test_dxy_bars_direction_down():
    dxy = _bars(40, start=105, per_bar=-0.05)
    a = compute_macro_context(dxy_bars=dxy)
    assert a.dxy_direction == "DOWN"


def test_yields_context_rising():
    y = {"available": True, "yieldsTrend": "rising",
         "dgs10Delta": 0.10, "realYieldDelta": 0.08}
    a = compute_macro_context(yields_context=y)
    assert a.yield_10y_direction == "UP"
    assert a.yield_10y_delta_bp == 10.0
    assert a.real_yield_direction == "UP"


def test_yields_context_falling():
    y = {"available": True, "yieldsTrend": "falling",
         "dgs10Delta": -0.08, "realYieldDelta": -0.07}
    a = compute_macro_context(yields_context=y)
    assert a.yield_10y_direction == "DOWN"
    assert a.real_yield_direction == "DOWN"


def test_correlation_from_snapshot():
    corr_snap = {"pairs": [{"code": "dxy", "label": "DXY",
                             "current_corr": -0.72, "corr_60": -0.72}]}
    a = compute_macro_context(correlation_snapshot=corr_snap)
    assert a.gold_dxy_correlation == -0.72
    assert a.correlation_state == "ACTIVE_INVERSE"


def test_high_impact_event_within_15min_is_high_risk():
    events = [{"time_utc": (datetime.now(timezone.utc) + timedelta(minutes=8)).isoformat(),
                "impact": "high", "name": "NFP"}]
    a = compute_macro_context(upcoming_events=events)
    assert a.event_risk_level == "HIGH"
    assert a.next_high_impact_event is not None
    assert a.next_high_impact_event["name"] == "NFP"


def test_high_impact_event_60min_out_is_elevated():
    events = [{"time_utc": (datetime.now(timezone.utc) + timedelta(minutes=45)).isoformat(),
                "impact": "high", "name": "FOMC"}]
    a = compute_macro_context(upcoming_events=events)
    assert a.event_risk_level == "ELEVATED"


def test_full_supportive_bull_scenario():
    """DXY down + yields down + gold up → SUPPORTIVE + MACRO_DRIVEN."""
    dxy = _bars(40, start=105, per_bar=-0.1)
    corr = {"pairs": [{"code": "dxy", "current_corr": -0.75}]}
    yields = {"available": True, "yieldsTrend": "falling", "realYieldDelta": -0.08}
    gold_h1 = _bars(40, start=4000, per_bar=2.0)
    snap = _snap_with_h1(gold_h1)
    a = compute_macro_context(
        snapshot=snap, tech_direction="BULL",
        dxy_bars=dxy, correlation_snapshot=corr, yields_context=yields,
    )
    assert a.macro_alignment == "SUPPORTIVE"
    assert a.move_driver in ("MACRO_DRIVEN", "HYBRID")
    assert a.correlation_state == "ACTIVE_INVERSE"


def test_opposing_bull_when_dxy_up():
    """Gold moving up but DXY also up → OPPOSING + likely TECHNICAL_DRIVEN."""
    dxy = _bars(40, start=100, per_bar=0.1)   # DXY going up
    yields = {"available": True, "yieldsTrend": "rising", "realYieldDelta": 0.08}
    gold_h1 = _bars(40, start=4000, per_bar=2.0)
    snap = _snap_with_h1(gold_h1)
    a = compute_macro_context(
        snapshot=snap, tech_direction="BULL",
        dxy_bars=dxy, yields_context=yields,
    )
    assert a.macro_alignment == "OPPOSING"


def test_never_raises_on_malformed_events():
    bad = [{"time_utc": "not-a-date", "impact": "high"}, None]
    a = compute_macro_context(upcoming_events=bad)
    # Just verify it returned without exception
    assert a.event_risk_level in ("NONE", "HIGH", "ELEVATED", "LOW")


if __name__ == "__main__":
    import subprocess
    subprocess.run(["python", "-m", "pytest", __file__, "-v"], check=False)
