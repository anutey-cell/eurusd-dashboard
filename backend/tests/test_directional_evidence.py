"""
Unit tests for Directional Evidence & Contradiction Scoring (Phase 5).
"""
import sys, os
from datetime import datetime, timezone, timedelta

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from services.directional_evidence import (
    compute_directional_evidence, EvidenceAssessment,
    _BULL_WEIGHTS, _BEAR_WEIGHTS, _CONTRADICTION_WEIGHTS,
)
from services.canonical_market_data import (
    Bar, CanonicalSnapshot, TimeframeSlice, LevelBundle, SessionInfo,
)


def _mk(ts, o, h, l, c, v=1):
    return Bar(time=ts, open=o, high=h, low=l, close=c, volume=v)


def _uptrend_bars(n, start=4000, per_bar=1.5, tf_min=15):
    now = datetime(2026, 8, 5, 12, 0, tzinfo=timezone.utc)
    bars = []
    for i in range(n):
        ts = now - timedelta(minutes=(n - i - 1) * tf_min)
        c = start + i * per_bar
        o = start + (i - 1) * per_bar if i > 0 else c
        h = max(c, o) + 1
        l = min(c, o) - 1
        bars.append(_mk(ts, o, h, l, c))
    return bars


def _downtrend_bars(n, start=4100, per_bar=-1.5, tf_min=15):
    return _uptrend_bars(n=n, start=start, per_bar=per_bar, tf_min=tf_min)


def _flat_bars(n, mid=4050, tf_min=15):
    import random; random.seed(42)
    now = datetime(2026, 8, 5, 12, 0, tzinfo=timezone.utc)
    bars = []
    for i in range(n):
        ts = now - timedelta(minutes=(n - i - 1) * tf_min)
        c = mid + random.uniform(-0.5, 0.5)
        o = mid + random.uniform(-0.5, 0.5)
        bars.append(_mk(ts, o, max(c, o) + 0.3, min(c, o) - 0.3, c))
    return bars


def _snap_bull(with_h4_supply=False):
    """Full bull-evidence snapshot — HH/HL, big M15 bodies, level acceptance."""
    m15 = _uptrend_bars(n=80, start=4000, per_bar=2.0, tf_min=15)
    # Overwrite last 6 with big-body greens (displacement)
    for i in range(6):
        b = m15[-6 + i]
        m15[-6 + i] = _mk(b.time, b.open, b.open + 12, b.open - 1, b.open + 10)
    h1 = _uptrend_bars(n=60, start=4000, per_bar=2.5, tf_min=60)
    h4 = _uptrend_bars(n=60, start=3800, per_bar=3.0, tf_min=240) if not with_h4_supply \
         else _flat_bars(n=60, mid=m15[-1].close + 2, tf_min=240)  # H4 flat right at current price = supply
    d1 = _uptrend_bars(n=60, start=3700, per_bar=5.0, tf_min=1440)
    m5 = _uptrend_bars(n=80, start=m15[-1].close - 15, per_bar=0.5, tf_min=5)
    levels = LevelBundle(pdh=m15[-1].close - 20, pdl=m15[0].close - 30,
                          asian_high=m15[-1].close - 15, asian_low=m15[0].close - 20,
                          daily_open=m15[0].close, pwh=m15[-1].close + 20, pwl=m15[0].close - 100)
    return CanonicalSnapshot(
        ts=datetime.now(timezone.utc), instrument="XAU/USD",
        bid=m15[-1].close - 0.1, ask=m15[-1].close + 0.1, spread=0.2,
        timeframes={"D1": TimeframeSlice("D1", d1), "H4": TimeframeSlice("H4", h4),
                     "H1": TimeframeSlice("H1", h1), "M15": TimeframeSlice("M15", m15),
                     "M5": TimeframeSlice("M5", m5)},
        levels=levels,
        session=SessionInfo("LDN_OPEN", "London open", is_active=True,
                             session_open=datetime.now(timezone.utc) - timedelta(hours=2)),
        data_quality_score=100,
    )


def _snap_bear():
    m15 = _downtrend_bars(n=80, start=4100, per_bar=-2.0, tf_min=15)
    for i in range(6):
        b = m15[-6 + i]
        m15[-6 + i] = _mk(b.time, b.open, b.open + 1, b.open - 12, b.open - 10)
    h1 = _downtrend_bars(n=60, start=4100, per_bar=-2.5, tf_min=60)
    h4 = _downtrend_bars(n=60, start=4300, per_bar=-3.0, tf_min=240)
    d1 = _downtrend_bars(n=60, start=4400, per_bar=-5.0, tf_min=1440)
    m5 = _downtrend_bars(n=80, start=m15[-1].close + 15, per_bar=-0.5, tf_min=5)
    levels = LevelBundle(pdh=m15[0].close + 30, pdl=m15[-1].close + 20,
                          asian_high=m15[0].close + 20, asian_low=m15[-1].close + 15,
                          daily_open=m15[0].close, pwh=m15[0].close + 100, pwl=m15[-1].close - 20)
    return CanonicalSnapshot(
        ts=datetime.now(timezone.utc), instrument="XAU/USD",
        bid=m15[-1].close - 0.1, ask=m15[-1].close + 0.1, spread=0.2,
        timeframes={"D1": TimeframeSlice("D1", d1), "H4": TimeframeSlice("H4", h4),
                     "H1": TimeframeSlice("H1", h1), "M15": TimeframeSlice("M15", m15),
                     "M5": TimeframeSlice("M5", m5)},
        levels=levels,
        session=SessionInfo("NY_OPEN", "NY open", is_active=True,
                             session_open=datetime.now(timezone.utc) - timedelta(hours=2)),
        data_quality_score=100,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Basic contract
# ─────────────────────────────────────────────────────────────────────────────

def test_none_snapshot_returns_zero_scores():
    ev = compute_directional_evidence(None)
    assert ev.dominant_direction == "NEUTRAL"
    assert ev.bull_evidence_score == 0
    assert ev.bear_evidence_score == 0
    assert ev.directional_confidence == 0
    assert "snapshot is None" in " ".join(ev.warnings)


def test_empty_timeframes_returns_zero_scores():
    snap = CanonicalSnapshot(
        ts=datetime.now(timezone.utc), instrument="XAU/USD",
        timeframes={}, levels=LevelBundle(), data_quality_score=100,
    )
    ev = compute_directional_evidence(snap)
    assert ev.dominant_direction == "NEUTRAL"
    assert ev.directional_confidence == 0


# ─────────────────────────────────────────────────────────────────────────────
# Bull evidence stacking
# ─────────────────────────────────────────────────────────────────────────────

def test_bull_scenario_produces_high_bull_score():
    ev = compute_directional_evidence(_snap_bull())
    assert ev.dominant_direction == "BULL", f"dir={ev.dominant_direction} bull={ev.bull_evidence_score} bear={ev.bear_evidence_score}"
    assert ev.bull_evidence_score > ev.bear_evidence_score
    assert ev.bull_evidence_score >= 40, f"expected >=40, got {ev.bull_evidence_score}"


def test_bull_scenario_lists_expected_bull_items():
    ev = compute_directional_evidence(_snap_bull())
    names = {i.name for i in ev.bull_items}
    # We should have at least: displacement + PDH broken + Asian high broken + BOS + acceptance
    assert "BULLISH_DISPLACEMENT" in names
    assert "PDH_BROKEN" in names or "ASIAN_HIGH_BROKEN" in names


# ─────────────────────────────────────────────────────────────────────────────
# Bear symmetry
# ─────────────────────────────────────────────────────────────────────────────

def test_bear_scenario_produces_high_bear_score():
    ev = compute_directional_evidence(_snap_bear())
    assert ev.dominant_direction == "BEAR", f"dir={ev.dominant_direction} bull={ev.bull_evidence_score} bear={ev.bear_evidence_score}"
    assert ev.bear_evidence_score > ev.bull_evidence_score
    assert ev.bear_evidence_score >= 40


def test_bear_scenario_lists_expected_bear_items():
    ev = compute_directional_evidence(_snap_bear())
    names = {i.name for i in ev.bear_items}
    assert "BEARISH_DISPLACEMENT" in names
    assert "PDL_BROKEN" in names or "ASIAN_LOW_BROKEN" in names


# ─────────────────────────────────────────────────────────────────────────────
# Contradictions
# ─────────────────────────────────────────────────────────────────────────────

def test_contradiction_reduces_confidence_but_keeps_direction():
    """Bull scenario with H4 supply overhead → contradiction fires,
    directional_confidence drops but dominant_direction stays BULL."""
    ev_clean = compute_directional_evidence(_snap_bull(with_h4_supply=False))
    ev_conflict = compute_directional_evidence(_snap_bull(with_h4_supply=True))
    # Direction unchanged
    assert ev_clean.dominant_direction == "BULL"
    assert ev_conflict.dominant_direction == "BULL"
    # Contradiction present
    assert ev_conflict.contradiction_score > ev_clean.contradiction_score
    # Confidence reduced
    assert ev_conflict.directional_confidence <= ev_clean.directional_confidence


def test_excessive_spread_contradiction():
    snap = _snap_bull()
    snap.spread = 12.5   # above default 5.0
    ev = compute_directional_evidence(snap)
    assert any(c.name == "EXCESSIVE_SPREAD" for c in ev.contradictions)


def test_news_within_30min_registers_as_contradiction_and_event_risk():
    snap = _snap_bull()
    events = [{"time_utc": (datetime.now(timezone.utc) + timedelta(minutes=10)).isoformat(),
                "impact": "high"}]
    ev = compute_directional_evidence(snap, upcoming_events=events)
    assert any(c.name == "NEWS_APPROACHING" for c in ev.contradictions)
    assert ev.event_risk_score >= 60


# ─────────────────────────────────────────────────────────────────────────────
# Extension risk
# ─────────────────────────────────────────────────────────────────────────────

def test_extension_risk_scales_with_atr_multiple():
    """Two custom snapshots — one where H1 EMA21 matches M15 close (low
    extension), one where H1 EMA21 is far below (high extension)."""
    from services.canonical_market_data import (
        Bar as CBar, CanonicalSnapshot as CS, TimeframeSlice as TS,
        LevelBundle as LB, SessionInfo as SI,
    )
    # LOW extension: H1 flat near 4100, M15 also flat near 4100
    now = datetime.now(timezone.utc)
    def _flat_at(n, mid, tf_min):
        bars = []
        for i in range(n):
            ts = now - timedelta(minutes=(n - i - 1) * tf_min)
            bars.append(CBar(time=ts, open=mid, high=mid+1, low=mid-1,
                              close=mid, volume=1))
        return bars
    snap_low = CS(
        ts=now, instrument="XAU/USD",
        timeframes={
            "H4": TS("H4", _flat_at(60, 4100, 240)),
            "H1": TS("H1", _flat_at(60, 4100, 60)),
            "M15": TS("M15", _flat_at(30, 4100, 15)),
            "D1": TS("D1", _flat_at(60, 4100, 1440)),
        },
        levels=LB(pdh=4110, pdl=4090), data_quality_score=100,
    )
    ev_low = compute_directional_evidence(snap_low)
    assert ev_low.extension_risk_score < 20    # essentially zero

    # HIGH extension: H1 flat at 3900, M15 shot up to 4200 (30 pts × 10 ATR)
    snap_high = CS(
        ts=now, instrument="XAU/USD",
        timeframes={
            "H4": TS("H4", _flat_at(60, 3900, 240)),
            "H1": TS("H1", _flat_at(60, 3900, 60)),
            "M15": TS("M15", _flat_at(30, 4200, 15)),
            "D1": TS("D1", _flat_at(60, 3950, 1440)),
        },
        levels=LB(pdh=4100, pdl=3900), data_quality_score=100,
    )
    ev_high = compute_directional_evidence(snap_high)
    assert ev_high.extension_risk_score > ev_low.extension_risk_score


# ─────────────────────────────────────────────────────────────────────────────
# Data quality gating
# ─────────────────────────────────────────────────────────────────────────────

def test_low_data_quality_reduces_entry_quality_confidence():
    """Custom-built snapshot with H1 EMA21 close to M15 (low extension) so
    directional_confidence stays > 0 after penalties, then compare dq=100
    vs dq=40 entry-quality confidence."""
    from services.canonical_market_data import (
        Bar as CBar, CanonicalSnapshot as CS, TimeframeSlice as TS,
        LevelBundle as LB, SessionInfo as SI,
    )
    import random; random.seed(42)
    now = datetime.now(timezone.utc)

    def _flat_wide(n, mid, jitter, tf_min):
        bars = []
        for i in range(n):
            ts = now - timedelta(minutes=(n - i - 1) * tf_min)
            o = mid + random.uniform(-jitter, jitter)
            c = mid + random.uniform(-jitter, jitter)
            bars.append(CBar(time=ts, open=o, close=c,
                              high=max(o, c) + random.uniform(0, jitter*0.4),
                              low=min(o, c) - random.uniform(0, jitter*0.4),
                              volume=1))
        return bars

    def _mk_snap(dq):
        # M15: last 6 bars big greens breaking PDH 4155
        m15 = _flat_wide(30, 4155, 3, 15)
        for i in range(6):
            b = m15[-6 + i]
            m15[-6 + i] = CBar(time=b.time, open=b.open, close=b.open + 5,
                                high=b.open + 6, low=b.open - 1, volume=1)
        # H1 flat around 4160 with wide jitter → high ATR, EMA21 ≈ 4160
        h1 = _flat_wide(60, 4160, 8, 60)
        h4 = _flat_wide(60, 4160, 12, 240)
        d1 = _flat_wide(60, 4160, 20, 1440)
        m5 = _flat_wide(80, 4165, 3, 5)
        levels = LB(pdh=4155, pdl=4100, asian_high=4158, asian_low=4130,
                     daily_open=4150, pwh=4200, pwl=4080)
        return CS(
            ts=now, instrument="XAU/USD",
            bid=m15[-1].close - 0.1, ask=m15[-1].close + 0.1, spread=0.2,
            timeframes={"D1": TS("D1", d1), "H4": TS("H4", h4),
                         "H1": TS("H1", h1), "M15": TS("M15", m15),
                         "M5": TS("M5", m5)},
            levels=levels,
            session=SI("LDN_OPEN", "London open", is_active=True,
                        session_open=now - timedelta(hours=2)),
            data_quality_score=dq,
        )

    ev_full = compute_directional_evidence(_mk_snap(100))
    ev_low = compute_directional_evidence(_mk_snap(40))
    assert ev_full.directional_confidence > 0, f"dc={ev_full.directional_confidence} bull={ev_full.bull_evidence_score} contra={ev_full.contradiction_score} ext={ev_full.extension_risk_score}"
    assert ev_low.entry_quality_confidence < ev_full.entry_quality_confidence, \
        f"full={ev_full.entry_quality_confidence} low={ev_low.entry_quality_confidence}"


# ─────────────────────────────────────────────────────────────────────────────
# Never raises
# ─────────────────────────────────────────────────────────────────────────────

def test_never_raises_on_malformed_events():
    snap = _snap_bull()
    bad = [{"time_utc": "not-a-timestamp", "impact": "high"}, None]
    ev = compute_directional_evidence(snap, upcoming_events=bad)
    assert ev.bull_evidence_score >= 0    # completed without exception


def test_weight_tables_are_populated():
    assert len(_BULL_WEIGHTS) == 14
    assert len(_BEAR_WEIGHTS) == 14
    assert len(_CONTRADICTION_WEIGHTS) == 10


if __name__ == "__main__":
    import subprocess
    subprocess.run(["python", "-m", "pytest", __file__, "-v"], check=False)
