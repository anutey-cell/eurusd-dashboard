"""
Unit tests for the Canonical Market Data Service (Phase 2).

Focus:
  - killzone_for_utc weekend + weekday transitions
  - _key_levels PDH/PDL/PDC + PWH/PWL + asian range
  - _session_hi_lo picks the right bars
  - Snapshot cache TTL respects windows
  - Snapshot ALWAYS returns even on partial failure
"""
import sys
import os
from datetime import datetime, timezone, timedelta
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from services.canonical_market_data import (
    Bar, killzone_for_utc, _key_levels, _session_hi_lo,
    CanonicalMarketData, get_canonical,
)


# ─────────────────────────────────────────────────────────────────────────────
# killzone_for_utc
# ─────────────────────────────────────────────────────────────────────────────

def test_killzone_saturday_all_day():
    sat_noon = datetime(2026, 8, 1, 12, 0, tzinfo=timezone.utc)   # Saturday
    label, pretty, active = killzone_for_utc(sat_noon)
    assert label == "OFF" and not active

def test_killzone_sunday_before_reopen():
    sun_afternoon = datetime(2026, 8, 2, 20, 0, tzinfo=timezone.utc)  # Sun < 22
    label, _, active = killzone_for_utc(sun_afternoon)
    assert label == "OFF" and not active

def test_killzone_sunday_after_reopen():
    sun_night = datetime(2026, 8, 2, 22, 30, tzinfo=timezone.utc)
    label, _, active = killzone_for_utc(sun_night)
    assert label != "OFF" and active

def test_killzone_friday_close():
    fri_late = datetime(2026, 7, 31, 21, 0, tzinfo=timezone.utc)  # Fri >= 21
    label, _, active = killzone_for_utc(fri_late)
    assert label == "OFF" and not active

def test_killzone_asian():
    t = datetime(2026, 8, 5, 3, 0, tzinfo=timezone.utc)          # Wed 03:00 UTC
    label, pretty, active = killzone_for_utc(t)
    assert label == "ASIA" and active and "Asian" in pretty

def test_killzone_london_open():
    t = datetime(2026, 8, 5, 8, 0, tzinfo=timezone.utc)          # Wed 08:00 UTC
    label, _, active = killzone_for_utc(t)
    assert label == "LDN_OPEN" and active

def test_killzone_ny_open():
    t = datetime(2026, 8, 5, 14, 0, tzinfo=timezone.utc)
    label, _, active = killzone_for_utc(t)
    assert label == "NY_OPEN" and active


# ─────────────────────────────────────────────────────────────────────────────
# _key_levels
# ─────────────────────────────────────────────────────────────────────────────

def _mk_d1(day, o, h, l, c):
    return Bar(time=datetime(day.year, day.month, day.day, tzinfo=timezone.utc),
               open=o, high=h, low=l, close=c, volume=1)


def test_key_levels_pdh_pdl_pdc():
    now = datetime(2026, 8, 5, 12, 0, tzinfo=timezone.utc)
    d1 = [_mk_d1(now.date() - timedelta(days=3), 4000, 4020, 3980, 4010),
          _mk_d1(now.date() - timedelta(days=2), 4010, 4040, 4000, 4030),
          _mk_d1(now.date() - timedelta(days=1), 4030, 4080, 4020, 4070),  # yesterday
          _mk_d1(now.date(),                     4070, 4090, 4065, 4085)]  # today
    lb = _key_levels(d1, [], now)
    assert lb.pdh == 4080
    assert lb.pdl == 4020
    assert lb.pdc == 4070
    assert lb.daily_open == 4070   # today's D1 open


def test_key_levels_no_today_uses_prev_close_as_daily_open():
    now = datetime(2026, 8, 5, 3, 0, tzinfo=timezone.utc)   # before today's D1 forms
    d1 = [_mk_d1(now.date() - timedelta(days=2), 4000, 4020, 3980, 4010),
          _mk_d1(now.date() - timedelta(days=1), 4010, 4050, 4000, 4045)]
    lb = _key_levels(d1, [], now)
    assert lb.pdh == 4050
    assert lb.pdl == 4000
    assert lb.daily_open == 4045     # proxy from yesterday's close


def test_key_levels_prev_week():
    now = datetime(2026, 8, 5, 12, 0, tzinfo=timezone.utc)
    d1 = [_mk_d1(now.date() - timedelta(days=k), 4000+k, 4020+k, 3980+k, 4010+k)
          for k in range(7, 0, -1)]  # 7..1 days ago
    lb = _key_levels(d1, [], now)
    # window = last 5 completed bars = day-5..day-1
    assert lb.pwh == max(4020 + k for k in range(1, 6))    # 4025 (k=5)
    assert lb.pwl == min(3980 + k for k in range(1, 6))    # 3981 (k=1)


def test_key_levels_asian_range():
    now = datetime(2026, 8, 5, 12, 0, tzinfo=timezone.utc)
    m15 = []
    # Six M15 bars in Asian window (00:00-06:00), then two after
    for h in range(0, 6):
        m15.append(Bar(time=datetime(2026,8,5,h,0,tzinfo=timezone.utc),
                       open=4050, high=4055+h, low=4045-h, close=4050+h, volume=1))
    m15.append(Bar(time=datetime(2026,8,5,7,0,tzinfo=timezone.utc),
                   open=4080, high=4100, low=4080, close=4090, volume=1))  # London
    lb = _key_levels([], m15, now)
    assert lb.asian_high == max(4055 + h for h in range(0, 6))
    assert lb.asian_low  == min(4045 - h for h in range(0, 6))


# ─────────────────────────────────────────────────────────────────────────────
# _session_hi_lo
# ─────────────────────────────────────────────────────────────────────────────

def test_session_hi_lo_ny_open():
    now = datetime(2026, 8, 5, 15, 30, tzinfo=timezone.utc)
    m15 = [
        Bar(time=datetime(2026,8,5,12,45,tzinfo=timezone.utc),
            open=4000, high=4005, low=3995, close=4002, volume=1),  # before NY
        Bar(time=datetime(2026,8,5,13,15,tzinfo=timezone.utc),
            open=4010, high=4030, low=4005, close=4025, volume=1),  # NY
        Bar(time=datetime(2026,8,5,15,00,tzinfo=timezone.utc),
            open=4025, high=4045, low=4020, close=4040, volume=1),  # NY
    ]
    start, hi, lo = _session_hi_lo(m15, now, "NY_OPEN")
    assert start.hour == 13
    assert hi == 4045
    assert lo == 4005


def test_session_hi_lo_off():
    now = datetime(2026, 8, 1, 12, 0, tzinfo=timezone.utc)   # Saturday
    m15 = [Bar(time=datetime(2026,8,1,3,tzinfo=timezone.utc),
               open=4000, high=4010, low=3990, close=4005, volume=1)]
    start, hi, lo = _session_hi_lo(m15, now, "OFF")
    assert start is None and hi is None and lo is None


# ─────────────────────────────────────────────────────────────────────────────
# CanonicalMarketData cache TTL
# ─────────────────────────────────────────────────────────────────────────────

def test_snapshot_cache_hits_within_ttl():
    cmd = CanonicalMarketData(cache_ttl_s=60)
    db = MagicMock()
    # Any DB call returns 0 rows
    db.execute.return_value.fetchall.return_value = []
    db.execute.return_value.fetchone.return_value = (None, 0)

    with patch("services.data_freshness.check_freshness") as mock_f:
        mock_f.return_value = {"details": {}, "data_quality_score": 0, "weekend": False}
        s1 = cmd.snapshot(db)
        s2 = cmd.snapshot(db)
    assert s1 is s2  # Same instance (cache hit)


def test_snapshot_force_refresh_bypasses_cache():
    cmd = CanonicalMarketData(cache_ttl_s=60)
    db = MagicMock()
    db.execute.return_value.fetchall.return_value = []
    db.execute.return_value.fetchone.return_value = (None, 0)

    with patch("services.data_freshness.check_freshness") as mock_f:
        mock_f.return_value = {"details": {}, "data_quality_score": 0, "weekend": False}
        s1 = cmd.snapshot(db)
        s2 = cmd.snapshot(db, force_refresh=True)
    assert s1 is not s2


def test_snapshot_always_returns_even_on_empty_db():
    """Every strategy needs the snapshot to be non-None even when DB is empty."""
    cmd = CanonicalMarketData()
    db = MagicMock()
    db.execute.return_value.fetchall.return_value = []
    db.execute.return_value.fetchone.return_value = (None, 0)

    with patch("services.data_freshness.check_freshness") as mock_f:
        mock_f.return_value = {"details": {}, "data_quality_score": 0, "weekend": False}
        snap = cmd.snapshot(db)
    assert snap is not None
    assert snap.instrument == "XAU/USD"
    assert snap.data_quality_score == 0
    # Should have marked warnings
    assert isinstance(snap.warnings, list)


def test_get_canonical_returns_singleton():
    a = get_canonical()
    b = get_canonical()
    assert a is b


if __name__ == "__main__":
    import subprocess
    subprocess.run(["python", "-m", "pytest", __file__, "-v"], check=False)
