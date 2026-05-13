"""
Backtest regression tests.

Purpose:
  - Ensure signal engine does not crash on demo candles
  - Verify output contains all required fields
  - Confirm WAIT is default; BUY/SELL only when gates pass
  - Check expectancy calculation does not error

Run with: cd backend && python -m pytest tests/ -v

Thresholds (not enforced until 100+ live trades):
  MIN_EXPECTANCY = 0
  MIN_PROFIT_FACTOR = 1.1
  MAX_DRAWDOWN_ALLOWED = -200
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

# ── Signal engine regression ──────────────────────────────────────────────────

def _get_demo_candles(pair="eurusd", interval="H4", limit=300):
    from data.candles import get_candles
    return get_candles(interval=interval, limit=limit, pair=pair)


def test_signal_engine_eurusd_no_crash():
    from services.signal_engine import analyze_signal
    candles = _get_demo_candles("eurusd")
    result = analyze_signal(candles.candles, pair="eurusd")
    assert result.signal in ("BUY", "SELL", "WAIT")
    assert 0 <= result.quality_score <= 100


def test_signal_engine_xauusd_no_crash():
    from services.signal_engine import analyze_signal
    candles = _get_demo_candles("xauusd")
    result = analyze_signal(candles.candles, pair="xauusd")
    assert result.signal in ("BUY", "SELL", "WAIT")
    assert 0 <= result.quality_score <= 100


def test_signal_output_required_fields():
    from services.signal_engine import analyze_signal
    candles = _get_demo_candles("eurusd")
    result = analyze_signal(candles.candles, pair="eurusd")
    assert hasattr(result, "signal")
    assert hasattr(result, "quality_score")
    assert hasattr(result, "reason")
    assert hasattr(result, "news_status")
    assert hasattr(result, "model")
    assert "higherTimeframeBias" in result.model
    assert "liquidity" in result.model
    assert "structure" in result.model
    assert "fvg" in result.model
    assert "session" in result.model


def test_wait_is_default_without_gates():
    """WAIT must be the output when no candles are provided."""
    from services.signal_engine import analyze_signal
    result = analyze_signal([], pair="eurusd")
    # With no candles, all gates fail → WAIT
    assert result.signal == "WAIT"


def test_xauusd_uses_correct_pip_size():
    from services.signal_engine import analyze_signal
    from data.candles import get_candles
    candles = get_candles(interval="H4", limit=300, pair="xauusd")
    result = analyze_signal(candles.candles, pair="xauusd")
    # XAU/USD target is 50, not 40
    assert result.target_pips == 50


def test_eurusd_uses_correct_pip_size():
    from services.signal_engine import analyze_signal
    candles = _get_demo_candles("eurusd")
    result = analyze_signal(candles.candles, pair="eurusd")
    assert result.target_pips == 40


# ── Analytics regression ──────────────────────────────────────────────────────

def _make_mock_trade(result="WIN", pips=40, signal="BUY", session="London", rr=2.5, news="CLEAR"):
    class T:
        pass
    t = T()
    t.result = result
    t.pips = pips
    t.signal = signal
    t.session = session
    t.rr = rr
    t.news_status = news
    return t


def test_analytics_no_crash():
    from services.analytics_engine import compute_analytics
    trades = [_make_mock_trade("WIN", 40), _make_mock_trade("LOSS", -16), _make_mock_trade("WIN", 40)]
    result = compute_analytics(trades, total_signals=5, confirmed_trades=3)
    assert result.win_rate > 0
    assert result.completed_trades == 3


def test_analytics_expectancy_formula():
    from services.analytics_engine import compute_analytics
    # 2 wins (+40 each), 1 loss (-16)
    trades = [_make_mock_trade("WIN", 40), _make_mock_trade("WIN", 40), _make_mock_trade("LOSS", -16)]
    r = compute_analytics(trades)
    # win_rate=66.7%, avg_win=40, loss_rate=33.3%, avg_loss=16
    # expectancy = 0.667*40 - 0.333*16 ≈ 26.7 - 5.3 ≈ 21.3
    assert r.expectancy > 0


def test_analytics_zero_state():
    from services.analytics_engine import compute_analytics
    result = compute_analytics([], total_signals=0, confirmed_trades=0)
    assert result.win_rate == 0.0
    assert result.completed_trades == 0


# ── Backtest regression ───────────────────────────────────────────────────────

def test_backtester_eurusd_no_crash():
    from services.backtester import run_backtest
    candles = _get_demo_candles("eurusd", "H4", 200)
    result = run_backtest(candles.candles)
    assert "total_trades" in result or hasattr(result, "total_trades")


# Threshold placeholders (not enforced until stable history):
MIN_EXPECTANCY = 0
MIN_PROFIT_FACTOR = 1.1
MAX_DRAWDOWN_ALLOWED = -200
