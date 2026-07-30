"""Validate candlestick_reader.py — each pattern in isolation + aggregator."""
from __future__ import annotations
import os, sys
from dataclasses import dataclass

try:
    import services.candlestick_reader  # noqa
except ImportError:
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from services.candlestick_reader import (
    detect_pin_bar, detect_engulfing, detect_inside_bar_break,
    detect_two_bar_reversal, detect_marubozu,
    evaluate_candlestick_confluence, MAX_BONUS, PATTERN_POINTS,
)


@dataclass
class C:
    open: float; high: float; low: float; close: float
    volume: int = 100


TOTAL, PASSED = 0, 0
def _hr(t): print("\n" + "-"*78 + f"\n {t}\n" + "-"*78)
def _run(name, fn):
    global TOTAL, PASSED
    TOTAL += 1
    try: fn(); print(f"  OK   {name}"); PASSED += 1
    except AssertionError as e: print(f"  FAIL {name}: {e}")
    except Exception as e: print(f"  FAIL {name}: {type(e).__name__}: {e}")


# ── Pin bar ─────────────────────────────────────────────────────────────────

def test_bull_pin_bar_detected():
    # Body 4020->4022 (2pt body), lower wick 4010->4020 (10pt), close near high
    c = C(open=4020.0, high=4022.5, low=4010.0, close=4022.0)
    assert detect_pin_bar(c, "BUY")

def test_bear_pin_bar_detected():
    # Body 4020->4018 (2pt), upper wick 4020->4030 (10pt), close near low
    c = C(open=4020.0, high=4030.0, low=4017.5, close=4018.0)
    assert detect_pin_bar(c, "SELL")

def test_pin_bar_wick_too_short_rejected():
    # Wick = body → not a pin
    c = C(open=4020.0, high=4023.0, low=4018.0, close=4022.0)
    assert not detect_pin_bar(c, "BUY")

def test_pin_bar_wrong_direction_rejected():
    # Bull pin can't be a sell setup
    c = C(open=4020.0, high=4022.5, low=4010.0, close=4022.0)
    assert not detect_pin_bar(c, "SELL")


# ── Engulfing ───────────────────────────────────────────────────────────────

def test_bull_engulfing_detected():
    prev = C(open=4020.0, high=4021.0, low=4015.0, close=4016.0)  # red 4pt body
    cur  = C(open=4016.0, high=4022.0, low=4015.5, close=4021.0)  # green wraps
    assert detect_engulfing(prev, cur, "BUY")

def test_bear_engulfing_detected():
    prev = C(open=4020.0, high=4025.0, low=4019.5, close=4024.0)  # green
    cur  = C(open=4024.0, high=4025.0, low=4018.0, close=4019.0)  # red wraps
    assert detect_engulfing(prev, cur, "SELL")

def test_engulfing_body_too_small_rejected():
    prev = C(open=4020.0, high=4025.0, low=4019.0, close=4024.0)  # green 4pt body
    cur  = C(open=4023.0, high=4024.0, low=4022.0, close=4022.5)  # red 0.5pt body
    assert not detect_engulfing(prev, cur, "SELL")


# ── Inside bar break ────────────────────────────────────────────────────────

def test_inside_break_up():
    mother   = C(open=4015.0, high=4025.0, low=4010.0, close=4020.0)
    inside   = C(open=4018.0, high=4022.0, low=4014.0, close=4020.0)
    breakout = C(open=4020.0, high=4028.0, low=4019.0, close=4026.0)  # closes > mother.high
    assert detect_inside_bar_break([mother, inside, breakout], "BUY")

def test_inside_break_down():
    mother   = C(open=4020.0, high=4025.0, low=4010.0, close=4015.0)
    inside   = C(open=4017.0, high=4020.0, low=4013.0, close=4014.0)
    breakout = C(open=4014.0, high=4015.0, low=4005.0, close=4008.0)
    assert detect_inside_bar_break([mother, inside, breakout], "SELL")

def test_inside_break_no_inside_bar():
    a = C(open=4015.0, high=4020.0, low=4010.0, close=4018.0)
    b = C(open=4018.0, high=4025.0, low=4017.0, close=4023.0)  # breaks a.high — not inside
    c = C(open=4023.0, high=4030.0, low=4022.0, close=4028.0)
    assert not detect_inside_bar_break([a, b, c], "BUY")


# ── Two-bar reversal ────────────────────────────────────────────────────────

def test_two_bar_reversal_bull():
    # Prior red with wick below; current green closes above prior open
    prev = C(open=4020.0, high=4021.0, low=4010.0, close=4015.0)
    cur  = C(open=4015.0, high=4023.0, low=4014.0, close=4022.0)
    assert detect_two_bar_reversal(prev, cur, "BUY", atr=10.0)

def test_two_bar_reversal_bear():
    prev = C(open=4020.0, high=4030.0, low=4019.5, close=4025.0)
    cur  = C(open=4025.0, high=4026.0, low=4017.0, close=4018.0)
    assert detect_two_bar_reversal(prev, cur, "SELL", atr=10.0)

def test_two_bar_reversal_wick_too_small():
    prev = C(open=4020.0, high=4021.0, low=4019.9, close=4015.0)  # negligible wick
    cur  = C(open=4015.0, high=4023.0, low=4014.0, close=4022.0)
    assert not detect_two_bar_reversal(prev, cur, "BUY", atr=10.0)


# ── Marubozu ────────────────────────────────────────────────────────────────

def test_bull_marubozu():
    # Body 4020->4030 = 10pt, range 4019.5->4030.5 = 11pt → 91% body
    c = C(open=4020.0, high=4030.5, low=4019.5, close=4030.0)
    assert detect_marubozu(c, "BUY")

def test_marubozu_wick_too_big():
    # Body 4020->4025 = 5pt, range 4010->4028 = 18pt → 28% body
    c = C(open=4020.0, high=4028.0, low=4010.0, close=4025.0)
    assert not detect_marubozu(c, "BUY")


# ── Aggregator ──────────────────────────────────────────────────────────────

def test_aggregator_no_zone_no_bonus():
    bull_pin = C(open=4020.0, high=4022.5, low=4010.0, close=4022.0)
    prev = C(open=4025.0, high=4026.0, low=4024.0, close=4025.5)
    older = C(open=4025.0, high=4026.0, low=4024.0, close=4025.0)
    v = evaluate_candlestick_confluence(
        candles=[older, prev, bull_pin], direction="BUY",
        entry_zone_low=4200.0, entry_zone_high=4200.0,  # nowhere near
    )
    assert v.bonus == 0
    assert v.patterns == ("pin_bar",)
    assert v.at_zone is False


def test_aggregator_at_zone_scores():
    bull_pin = C(open=4020.0, high=4022.5, low=4010.0, close=4022.0)
    prev = C(open=4025.0, high=4026.0, low=4024.0, close=4025.5)
    older = C(open=4025.0, high=4026.0, low=4024.0, close=4025.0)
    v = evaluate_candlestick_confluence(
        candles=[older, prev, bull_pin], direction="BUY",
        entry_zone_low=4020.0, entry_zone_high=4023.0,   # close within
    )
    assert v.bonus == PATTERN_POINTS["pin_bar"]
    assert v.at_zone is True


def test_aggregator_liquidity_zone_counts():
    bull_pin = C(open=4020.0, high=4022.5, low=4010.0, close=4022.0)
    prev = C(open=4025.0, high=4026.0, low=4024.0, close=4025.5)
    older = C(open=4025.0, high=4026.0, low=4024.0, close=4025.0)
    v = evaluate_candlestick_confluence(
        candles=[older, prev, bull_pin], direction="BUY",
        entry_zone_low=4200.0, entry_zone_high=4200.0,
        liquidity_zones=[4021.0, 4100.0, 4300.0],   # 4021 is near close 4022
    )
    assert v.bonus > 0
    assert v.at_zone is True


def test_aggregator_bonus_caps():
    # Force multiple patterns simultaneously — hard: use engulfing + marubozu
    prev = C(open=4025.0, high=4026.0, low=4019.0, close=4020.0)   # small red
    cur  = C(open=4020.0, high=4030.5, low=4019.5, close=4030.0)   # bull engulfing + marubozu
    older = C(open=4030.0, high=4031.0, low=4029.0, close=4030.5)
    v = evaluate_candlestick_confluence(
        candles=[older, prev, cur], direction="BUY",
        entry_zone_low=4029.0, entry_zone_high=4031.0,
    )
    assert v.bonus <= MAX_BONUS


def test_aggregator_insufficient_candles():
    v = evaluate_candlestick_confluence(candles=[], direction="BUY")
    assert v.bonus == 0
    assert "insufficient" in v.detail


def test_aggregator_bad_direction():
    v = evaluate_candlestick_confluence(candles=[C(4020,4021,4019,4020.5)]*3,
                                          direction="FLAT")
    assert v.bonus == 0


# ── Main ────────────────────────────────────────────────────────────────────

def main() -> int:
    print("=" * 78)
    print(" CANDLESTICK-READER VALIDATION")
    print("=" * 78)

    _hr("1. Pin bar")
    _run("bull pin detected", test_bull_pin_bar_detected)
    _run("bear pin detected", test_bear_pin_bar_detected)
    _run("short wick rejected", test_pin_bar_wick_too_short_rejected)
    _run("wrong direction rejected", test_pin_bar_wrong_direction_rejected)

    _hr("2. Engulfing")
    _run("bull engulfing", test_bull_engulfing_detected)
    _run("bear engulfing", test_bear_engulfing_detected)
    _run("body too small rejected", test_engulfing_body_too_small_rejected)

    _hr("3. Inside bar break")
    _run("inside break-up", test_inside_break_up)
    _run("inside break-down", test_inside_break_down)
    _run("no inside bar rejected", test_inside_break_no_inside_bar)

    _hr("4. Two-bar reversal")
    _run("bull reversal", test_two_bar_reversal_bull)
    _run("bear reversal", test_two_bar_reversal_bear)
    _run("wick too small rejected", test_two_bar_reversal_wick_too_small)

    _hr("5. Marubozu")
    _run("bull marubozu", test_bull_marubozu)
    _run("wick too big rejected", test_marubozu_wick_too_big)

    _hr("6. Aggregator")
    _run("no zone → bonus=0 (pattern still detected)", test_aggregator_no_zone_no_bonus)
    _run("at entry zone scores", test_aggregator_at_zone_scores)
    _run("liquidity zone counts", test_aggregator_liquidity_zone_counts)
    _run("bonus caps at MAX_BONUS", test_aggregator_bonus_caps)
    _run("insufficient candles handled", test_aggregator_insufficient_candles)
    _run("bad direction handled", test_aggregator_bad_direction)

    print("\n" + "=" * 78)
    if PASSED == TOTAL:
        print(f" ALL PASS {PASSED}/{TOTAL} — pattern engine ready")
        return 0
    print(f" FAIL {PASSED}/{TOTAL}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
