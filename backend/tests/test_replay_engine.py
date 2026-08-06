"""Unit tests for the Replay Validation Harness (Phase 14)."""
import sys, os
from types import SimpleNamespace
from datetime import datetime, timezone, timedelta

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from services.replay_engine import (
    classify_day_scenario, _judge_replay,
    ReplayReport, EngineMetrics, DayScenario,
)


def _bar(hh, o, h, l, c):
    return SimpleNamespace(
        time=datetime(2026,8,5,hh,0,tzinfo=timezone.utc),
        open=o, high=h, low=l, close=c, volume=1,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Scenario classifier — one test per tag
# ─────────────────────────────────────────────────────────────────────────────

def test_no_data_tag_when_empty_bars():
    tag, direction = classify_day_scenario([], d1_atr=15)
    assert tag == "no_data" and direction == "NEUTRAL"


def test_no_data_when_zero_atr():
    tag, direction = classify_day_scenario([_bar(0, 4000, 4001, 3999, 4000)], d1_atr=0)
    assert tag == "no_data"


def test_low_volatility_tag():
    """Whole day inside a tiny range vs D1 ATR."""
    bars = [_bar(h, 4000, 4001, 3999, 4000) for h in range(24)]
    tag, direction = classify_day_scenario(bars, d1_atr=30)
    assert tag == "low_volatility"


def test_strong_bullish_trend_tag():
    """Monotonic uptrend closing near high."""
    bars = []
    for h in range(24):
        c = 4000 + h * 4
        bars.append(_bar(h, c - 1, c + 1, c - 2, c))
    # Move = 24*4 = 96, D1 ATR = 15 → 6.4× — strong bullish
    tag, direction = classify_day_scenario(bars, d1_atr=15)
    assert tag == "strong_bullish_trend" and direction == "BULL"


def test_strong_bearish_trend_tag():
    bars = []
    for h in range(24):
        c = 4100 - h * 4
        bars.append(_bar(h, c + 1, c + 2, c - 1, c))
    tag, direction = classify_day_scenario(bars, d1_atr=15)
    assert tag == "strong_bearish_trend" and direction == "BEAR"


def test_range_tag_when_net_small_but_intraday_active():
    """Small net move but active intraday range."""
    bars = []
    for h in range(24):
        # Oscillate around 4000 with 10pt range
        if h % 4 < 2:
            o, c = 4000, 4010
        else:
            o, c = 4010, 4000
        bars.append(_bar(h, o, max(o, c) + 1, min(o, c) - 1, c))
    tag, direction = classify_day_scenario(bars, d1_atr=15)
    assert tag == "range"
    assert direction == "NEUTRAL"


def test_reversal_tag_when_direction_flips_mid_day():
    """First half up, second half down."""
    bars = []
    for h in range(12):     # first 12h up
        c = 4000 + h * 2
        bars.append(_bar(h, c - 1, c + 1, c - 2, c))
    for h in range(12, 24):   # next 12h back down
        c = 4022 - (h - 12) * 2
        bars.append(_bar(h, c + 1, c + 2, c - 1, c))
    tag, direction = classify_day_scenario(bars, d1_atr=8)
    assert tag == "reversal"


def test_news_day_tag_when_events_present_plus_big_range():
    """When news_events_that_day >=1 AND atr_ratio >= 1.2 → news_day."""
    bars = []
    for h in range(24):
        c = 4000 + h * 3
        bars.append(_bar(h, c - 1, c + 3, c - 3, c))
    # d1_atr=20, day range = ~72 → ratio 3.6
    tag, direction = classify_day_scenario(bars, d1_atr=20, news_events_that_day=1)
    assert tag == "news_day"


def test_london_expansion_tag():
    """Big move in London KZ, quiet elsewhere."""
    bars = []
    for h in range(24):
        if 7 <= h < 13:
            c = 4000 + (h - 7) * 8    # London up thrust
        else:
            c = 4040
        bars.append(_bar(h, c, c + 1, c - 1, c))
    tag, direction = classify_day_scenario(bars, d1_atr=15)
    assert tag in ("london_expansion", "strong_bullish_trend"), f"got {tag}"


def test_ny_reversal_tag():
    """London up, NY reverses down."""
    bars = []
    for h in range(24):
        if 7 <= h < 13:
            c = 4000 + (h - 7) * 3         # London up
        elif 13 <= h < 17:
            c = 4018 - (h - 13) * 4        # NY reverses
        else:
            c = 4002
        bars.append(_bar(h, c, c + 1, c - 1, c))
    tag, direction = classify_day_scenario(bars, d1_atr=15)
    assert tag in ("ny_reversal", "reversal", "range"), f"got {tag}"


# ─────────────────────────────────────────────────────────────────────────────
# Verdict judge
# ─────────────────────────────────────────────────────────────────────────────

def _metrics(engine, coverage, alerts_per_day, false_alerts):
    return EngineMetrics(
        engine=engine, alerts_per_day=alerts_per_day,
        coverage_pct=coverage, median_detection_delay_min=30,
        false_alert_count=false_alerts, late_alert_pct=10,
        direction_accuracy_pct=None,
        matched_expansions=int(coverage / 10), total_alerts=int(alerts_per_day * 30),
    )


def test_verdict_insufficient_when_few_expansions():
    old = _metrics("old", 10, 1, 0); new = _metrics("new", 50, 3, 5)
    v = _judge_replay(old, new, {"coverage_pct_delta": 40, "false_alerts_delta": 5}, n_expansions=3)
    assert "INSUFFICIENT_SAMPLE" in v


def test_verdict_better_when_big_coverage_gain_and_low_false():
    old = _metrics("old", 10, 1, 2); new = _metrics("new", 55, 4, 5)
    v = _judge_replay(old, new, {"coverage_pct_delta": 45, "false_alerts_delta": 3},
                        n_expansions=20)
    assert v.startswith("BETTER")


def test_verdict_mixed_when_moderate_gain_but_false_alerts_up():
    old = _metrics("old", 20, 1, 0); new = _metrics("new", 35, 5, 20)
    v = _judge_replay(old, new,
                        {"coverage_pct_delta": 15, "false_alerts_delta": 20},
                        n_expansions=20)
    assert v.startswith("MIXED")


def test_verdict_worse_when_coverage_drops():
    old = _metrics("old", 50, 3, 5); new = _metrics("new", 30, 2, 3)
    v = _judge_replay(old, new,
                        {"coverage_pct_delta": -20, "false_alerts_delta": -2},
                        n_expansions=20)
    assert v.startswith("WORSE")


def test_verdict_neutral_when_no_meaningful_change():
    old = _metrics("old", 30, 2, 4); new = _metrics("new", 32, 2, 4)
    v = _judge_replay(old, new,
                        {"coverage_pct_delta": 2, "false_alerts_delta": 0},
                        n_expansions=20)
    assert v.startswith("NEUTRAL")


# ─────────────────────────────────────────────────────────────────────────────
# Data class round-trips
# ─────────────────────────────────────────────────────────────────────────────

def test_engine_metrics_to_dict_has_all_keys():
    m = _metrics("old", 25, 2, 3)
    d = m.to_dict()
    for k in ("engine", "alerts_per_day", "coverage_pct",
               "median_detection_delay_min", "false_alert_count",
               "late_alert_pct", "direction_accuracy_pct",
               "matched_expansions", "total_alerts"):
        assert k in d


def test_day_scenario_to_dict_has_all_keys():
    s = DayScenario(day="2026-08-05", scenario_tag="strong_bullish_trend",
                     direction="BULL", total_move_pct=1.2, intraday_range_pct=1.5,
                     d1_atr_ratio=1.3, old_engine_alerts=3, new_engine_alerts=5,
                     ground_truth_expansions=1)
    d = s.to_dict()
    assert d["scenario_tag"] == "strong_bullish_trend"
    assert d["ground_truth_expansions"] == 1


if __name__ == "__main__":
    import subprocess
    subprocess.run(["python", "-m", "pytest", __file__, "-v"], check=False)
