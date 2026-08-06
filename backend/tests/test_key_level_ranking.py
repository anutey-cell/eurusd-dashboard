"""Unit tests for Key Level Ranking (Phase 9)."""
import sys, os
from datetime import datetime, timezone, timedelta

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from services.key_level_ranking import rank_key_levels, RankedLevel
from services.canonical_market_data import (
    Bar, TimeframeSlice, LevelBundle, CanonicalSnapshot, SessionInfo,
)


def _mk(ts, o, h, l, c):
    return Bar(time=ts, open=o, high=h, low=l, close=c, volume=1)


def _bars_at(mid, n=100, tf_min=15, wick=3):
    """Produce n bars around `mid` with reasonable range so ATR ~= 2×wick."""
    base = datetime(2026, 8, 1, 0, 0, tzinfo=timezone.utc)
    import random; random.seed(mid + n + tf_min)
    bars = []
    for i in range(n):
        o = mid + random.uniform(-wick, wick)
        c = mid + random.uniform(-wick, wick)
        h = max(o, c) + random.uniform(0, wick)
        l = min(o, c) - random.uniform(0, wick)
        bars.append(_mk(base + timedelta(minutes=i * tf_min), o, h, l, c))
    return bars


def _snap(current=4200, atr_h1=5.0, levels=None):
    m15 = _bars_at(current, n=100, tf_min=15)
    h1  = _bars_at(current, n=60, tf_min=60)
    h4  = _bars_at(current, n=40, tf_min=240)
    d1  = _bars_at(current, n=20, tf_min=1440)
    return CanonicalSnapshot(
        ts=datetime.now(timezone.utc), instrument="XAU/USD",
        bid=current - 0.1, ask=current + 0.1, spread=0.2,
        timeframes={"D1": TimeframeSlice("D1", d1), "H4": TimeframeSlice("H4", h4),
                     "H1": TimeframeSlice("H1", h1), "M15": TimeframeSlice("M15", m15)},
        levels=levels or LevelBundle(),
        session=SessionInfo("NY_OPEN", "NY open", is_active=True,
                             session_open=datetime.now(timezone.utc)),
        data_quality_score=100,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Basic contract
# ─────────────────────────────────────────────────────────────────────────────

def test_no_snapshot_returns_empty_tiers():
    r = rank_key_levels(None)
    assert r.tier1 == [] and r.tier2 == [] and r.tier3 == []
    assert "snapshot is None" in " ".join(r.warnings)


def test_no_bundle_levels_yields_only_swing_pivots():
    """Empty LevelBundle → tiers populated only from H4/H1 swing pivots
    (may be zero, may be a handful — but never raises)."""
    snap = _snap(current=4200)
    r = rank_key_levels(snap)
    # Just verify the call worked and returned lists
    assert isinstance(r.tier1, list) and isinstance(r.tier2, list) and isinstance(r.tier3, list)


# ─────────────────────────────────────────────────────────────────────────────
# Ranking behaviour
# ─────────────────────────────────────────────────────────────────────────────

def test_pdh_near_price_lands_in_tier1():
    snap = _snap(current=4200, levels=LevelBundle(pdh=4205, pdl=4150))
    r = rank_key_levels(snap)
    assert any(l.tag == "PDH" for l in r.tier1), \
        f"tier1={[l.tag for l in r.tier1]} tier2={[l.tag for l in r.tier2]} tier3={[l.tag for l in r.tier3]}"


def test_pdh_far_from_price_drops_to_tier2_or_3():
    """PDH beyond tier-1 distance (>2× ATR) but within hard cap → tier 2/3."""
    snap = _snap(current=4200, levels=LevelBundle(pdh=4225, pdl=4150))  # ~4× 6-atr away
    r = rank_key_levels(snap)
    all_lvls = r.tier1 + r.tier2 + r.tier3
    pdh_in_tier1 = any(l.tag == "PDH" and l.tier == 1 for l in all_lvls)
    pdh_elsewhere = any(l.tag == "PDH" for l in all_lvls)
    assert pdh_elsewhere and not pdh_in_tier1, \
        f"tier1={[(l.tag, l.distance_atr) for l in r.tier1]}"


def test_pdl_below_price_lands_in_tier1_when_near():
    snap = _snap(current=4200, levels=LevelBundle(pdh=4250, pdl=4198))
    r = rank_key_levels(snap)
    assert any(l.tag == "PDL" for l in r.tier1), \
        f"tiers={[(l.tag, l.tier) for l in r.tier1 + r.tier2 + r.tier3]}"


def test_confluence_bumps_scoring():
    """When PDH and Asian High are at the SAME price, score is higher."""
    snap_solo = _snap(current=4200, levels=LevelBundle(pdh=4205))
    snap_conf = _snap(current=4200, levels=LevelBundle(pdh=4205, asian_high=4205))
    r_solo = rank_key_levels(snap_solo)
    r_conf = rank_key_levels(snap_conf)
    # Same underlying price → merged into one level with confluence
    all_solo = r_solo.tier1 + r_solo.tier2 + r_solo.tier3
    all_conf = r_conf.tier1 + r_conf.tier2 + r_conf.tier3
    solo_score = next((l.score for l in all_solo if l.price == 4205), 0)
    conf_score = next((l.score for l in all_conf if l.price == 4205), 0)
    assert conf_score > solo_score, f"solo={solo_score} conf={conf_score}"


def test_hard_distance_cap_drops_far_levels():
    """PDL 30× ATR away → should be dropped."""
    snap = _snap(current=4200, levels=LevelBundle(pdh=4205, pdl=4050))  # 30 ATRs away
    r = rank_key_levels(snap)
    pdl_present = any(l.tag == "PDL" for l in (r.tier1 + r.tier2 + r.tier3))
    assert not pdl_present
    assert r.dropped_count >= 1


def test_side_correctly_assigned():
    snap = _snap(current=4200, levels=LevelBundle(pdh=4210, pdl=4190))
    r = rank_key_levels(snap)
    all_lvls = r.tier1 + r.tier2 + r.tier3
    pdh_level = next((l for l in all_lvls if l.tag == "PDH"), None)
    pdl_level = next((l for l in all_lvls if l.tag == "PDL"), None)
    assert pdh_level is not None and pdh_level.side == "ABOVE"
    assert pdl_level is not None and pdl_level.side == "BELOW"


def test_breakout_flipped_role_boosts_score():
    """A retest level (flipped role) should score highly."""
    from types import SimpleNamespace
    snap = _snap(current=4205, levels=LevelBundle(pdh=4200))
    # Fake breakout: PDH broken, retest classification
    breakouts = [SimpleNamespace(
        level=4200, direction="UP", level_name="PDH",
        classification="BREAKOUT_RETEST",
    )]
    r_no_bo = rank_key_levels(snap)
    r_bo = rank_key_levels(snap, breakouts=breakouts)
    all_no = r_no_bo.tier1 + r_no_bo.tier2 + r_no_bo.tier3
    all_bo = r_bo.tier1 + r_bo.tier2 + r_bo.tier3
    score_no = max((l.score for l in all_no if l.price == 4200), default=0)
    score_bo = max((l.score for l in all_bo if l.price == 4200), default=0)
    assert score_bo > score_no, f"no_bo={score_no} bo={score_bo}"


def test_tier1_caps_at_4_levels():
    """Even with many candidates, tier 1 caps at 4."""
    snap = _snap(current=4200, levels=LevelBundle(
        pdh=4202, pdl=4198,
        pwh=4203, pwl=4197,
        asian_high=4204, asian_low=4196,
        daily_open=4201,
    ))
    r = rank_key_levels(snap)
    assert len(r.tier1) <= 4


def test_never_raises_on_missing_timeframes():
    snap = CanonicalSnapshot(
        ts=datetime.now(timezone.utc), instrument="XAU/USD",
        timeframes={}, levels=LevelBundle(pdh=4200), data_quality_score=100,
    )
    r = rank_key_levels(snap)   # no M15 → empty
    assert isinstance(r, type(rank_key_levels(_snap())))


if __name__ == "__main__":
    import subprocess
    subprocess.run(["python", "-m", "pytest", __file__, "-v"], check=False)
