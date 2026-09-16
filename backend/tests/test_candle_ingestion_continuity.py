"""Regression tests for VPS XAUUSD cloud candle continuity."""
import os
import sys
from datetime import datetime, timedelta, timezone

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from services import candle_ingestion as ci
from services.provider_health import reset_provider_health


@pytest.fixture(autouse=True)
def _reset_provider_state():
    reset_provider_health()
    yield
    reset_provider_health()


def _bar(ts: datetime, source: str = "test") -> dict:
    return {
        "time": ts,
        "open": 4300.0,
        "high": 4302.0,
        "low": 4298.0,
        "close": 4301.0,
        "volume": 1,
        "source": source,
    }


def test_payload_fresh_accepts_recent_m5():
    now = datetime(2026, 9, 9, 12, 0, tzinfo=timezone.utc)  # Wednesday
    ok, detail = ci._payload_fresh([_bar(now - timedelta(minutes=7))], "M5", now=now)
    assert ok is True
    assert "age=" in detail


def test_payload_fresh_rejects_stale_m5():
    now = datetime(2026, 9, 9, 12, 0, tzinfo=timezone.utc)  # Wednesday
    ok, detail = ci._payload_fresh([_bar(now - timedelta(minutes=30))], "M5", now=now)
    assert ok is False
    assert "stale" in detail


def test_fetch_prefers_fresh_twelvedata(monkeypatch):
    td = [_bar(datetime.now(timezone.utc), "td")]

    monkeypatch.setattr(ci, "_fetch_twelvedata", lambda *args, **kwargs: td)
    monkeypatch.setattr(ci, "_payload_fresh", lambda candles, interval: (True, "fresh"))

    def _tv_must_not_run(*args, **kwargs):
        raise AssertionError("TradingView should not run when TwelveData is fresh")

    monkeypatch.setattr(ci, "_fetch_tradingview", _tv_must_not_run)

    candles, source = ci._fetch_with_fallback("xauusd", "M5", 20)
    assert candles is td
    assert source == "twelvedata"


def test_fetch_falls_back_to_tradingview_when_twelvedata_stale(monkeypatch):
    td = [_bar(datetime.now(timezone.utc), "td")]
    tv = [_bar(datetime.now(timezone.utc), "tv")]

    monkeypatch.setattr(ci, "_fetch_twelvedata", lambda *args, **kwargs: td)
    monkeypatch.setattr(ci, "_fetch_tradingview", lambda *args, **kwargs: tv)

    def _freshness(candles, interval):
        return (candles[0]["source"] == "tv", "mock freshness")

    monkeypatch.setattr(ci, "_payload_fresh", _freshness)

    candles, source = ci._fetch_with_fallback("xauusd", "M5", 20)
    assert candles is tv
    assert source == "tradingview"


def test_fetch_fails_closed_when_no_fresh_spot_provider(monkeypatch):
    monkeypatch.setattr(
        ci, "_fetch_twelvedata",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("td down")),
    )
    monkeypatch.setattr(ci, "_fetch_tradingview", lambda *args, **kwargs: [])
    monkeypatch.setattr(ci.time, "sleep", lambda *_: None)

    with pytest.raises(RuntimeError, match="No fresh XAU/USD spot provider"):
        ci._fetch_with_fallback("xauusd", "M5", 20)
