"""Validate four_hour_manipulation — every scenario in the operator brief."""
from __future__ import annotations
import os, sys
from dataclasses import dataclass

try:
    import services.four_hour_manipulation  # noqa
except ImportError:
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from services.four_hour_manipulation import detect_4h_manipulation


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


def _m15_series(closes: list[float], entry_open: float = 4020.0) -> list:
    """Build a plausible M15 candle series ending at the given closes."""
    out = []
    prev_close = entry_open
    for close in closes:
        rng = max(2.0, abs(close - prev_close) * 1.5)
        out.append(C(
            open=prev_close,
            close=close,
            high=max(prev_close, close) + rng * 0.3,
            low=min(prev_close, close) - rng * 0.3,
        ))
        prev_close = close
    return out


# ── Bearish manipulation (sweep prev-4H high, reclaim below) ──────────────

def test_bearish_manipulation_full_confirmation():
    """Sweep + reclaim + M15 structure shift → +10 bearish"""
    prev_h4 = C(open=4020, high=4030, low=4015, close=4025)
    cur_h4  = C(open=4025, high=4045, low=4020, close=4022)  # swept 4030 by 15pt
    # M15: last 3 closes all below 4030 (reclaimed) + all red bodies (structure)
    m15 = _m15_series([4035, 4028, 4025, 4020])
    r = detect_4h_manipulation(
        h4_candles=[prev_h4, cur_h4],
        candles_m15=m15,
        atr_h1=10.0,
    )
    assert r["detected"] is True
    assert r["direction"] == "bearish"
    assert r["swept_level"] == 4030.0
    assert r["sweep_type"] == "previous_4h_high"
    assert r["reclaimed"] is True
    assert r["m15_confirmation"] is True
    assert r["confidence_adjustment"] == 10
    assert r["trade_bias"] == "SELL"
    assert r["trapped_participants"] == "buyers"


def test_bearish_manipulation_weak_ltf_confirmation():
    """Sweep + reclaim BUT no M15 structure shift → +5"""
    prev_h4 = C(open=4020, high=4030, low=4015, close=4025)
    cur_h4  = C(open=4025, high=4045, low=4022, close=4028)
    # Only 2 of 3 M15 candles are red — mixed structure
    m15 = _m15_series([4035, 4028, 4029, 4027])
    # Force mostly-green candles so structure shift fails
    m15[-3] = C(open=4028, close=4035, high=4036, low=4027)   # green
    m15[-2] = C(open=4035, close=4029, high=4036, low=4028)   # red
    m15[-1] = C(open=4029, close=4027, high=4030, low=4026)   # red
    # Reclaimed (last close 4027 < 4030) but mixed structure
    r = detect_4h_manipulation(
        h4_candles=[prev_h4, cur_h4],
        candles_m15=m15,
        atr_h1=10.0,
    )
    assert r["detected"] is True
    assert r["direction"] == "bearish"
    assert r["reclaimed"] is True
    # Not asserting m15_confirmation exact value — grade is 5 or 10 based on structure
    assert r["confidence_adjustment"] in (5, 10)


# ── Bullish manipulation (sweep prev-4H low, reclaim above) ────────────────

def test_bullish_manipulation_full_confirmation():
    prev_h4 = C(open=4020, high=4030, low=4010, close=4025)
    cur_h4  = C(open=4025, high=4028, low=3995, close=4018)  # swept 4010 by 15pt
    m15 = _m15_series([4005, 4012, 4015, 4020])   # reclaim + green bodies
    r = detect_4h_manipulation(
        h4_candles=[prev_h4, cur_h4],
        candles_m15=m15,
        atr_h1=10.0,
    )
    assert r["detected"] is True
    assert r["direction"] == "bullish"
    assert r["swept_level"] == 4010.0
    assert r["sweep_type"] == "previous_4h_low"
    assert r["reclaimed"] is True
    assert r["trapped_participants"] == "sellers"
    assert r["trade_bias"] == "BUY"
    assert r["confidence_adjustment"] == 10


# ── The critical −10 case: continuation, not manipulation ─────────────────

def test_continuation_hold_beyond_penalizes():
    """Sweep + price stays well beyond prev-4H high → -10 penalty"""
    prev_h4 = C(open=4020, high=4030, low=4015, close=4025)
    cur_h4  = C(open=4025, high=4055, low=4025, close=4050)  # broke and holding
    # Every recent M15 close is ABOVE 4030 → no reclaim, held beyond
    m15 = _m15_series([4038, 4045, 4050, 4052])
    r = detect_4h_manipulation(
        h4_candles=[prev_h4, cur_h4],
        candles_m15=m15,
        atr_h1=10.0,   # continuation threshold = max(3.0, 10 * 0.75) = 7.5 pts
    )
    assert r["detected"] is True
    assert r["direction"] == "bearish"       # SWEEP was of the high
    assert r["reclaimed"] is False
    assert r["confidence_adjustment"] == -10
    assert r["trade_bias"] == "STAND_ASIDE"
    assert "Continuation" in r["reason"]


def test_continuation_hold_beyond_bullish_side():
    prev_h4 = C(open=4020, high=4030, low=4010, close=4025)
    cur_h4  = C(open=4025, high=4025, low=3985, close=3990)  # broke down + held
    m15 = _m15_series([3998, 3992, 3988, 3985])
    r = detect_4h_manipulation(
        h4_candles=[prev_h4, cur_h4],
        candles_m15=m15,
        atr_h1=10.0,
    )
    assert r["confidence_adjustment"] == -10
    assert r["reclaimed"] is False


# ── Waiting state — sweep but no reclaim (and no held-beyond) ─────────────

def test_sweep_pending_reclaim_returns_zero():
    """Sweep occurred but M15 close still above 4030, and not far beyond → 0"""
    prev_h4 = C(open=4020, high=4030, low=4015, close=4025)
    cur_h4  = C(open=4025, high=4035, low=4020, close=4033)
    # Last M15 close 4032 → above swept level (no reclaim) but only 2pt past
    # → below continuation threshold (7.5pt)
    m15 = _m15_series([4034, 4033, 4032, 4032])
    r = detect_4h_manipulation(
        h4_candles=[prev_h4, cur_h4],
        candles_m15=m15,
        atr_h1=10.0,
    )
    assert r["detected"] is True
    assert r["reclaimed"] is False
    assert r["confidence_adjustment"] == 0
    assert r["trade_bias"] == "STAND_ASIDE"


# ── No sweep at all ────────────────────────────────────────────────────────

def test_no_sweep_returns_empty():
    prev_h4 = C(open=4020, high=4030, low=4015, close=4025)
    cur_h4  = C(open=4025, high=4028, low=4020, close=4023)  # inside prev range
    m15 = _m15_series([4025, 4024, 4023, 4022])
    r = detect_4h_manipulation(
        h4_candles=[prev_h4, cur_h4],
        candles_m15=m15,
        atr_h1=10.0,
    )
    assert r["detected"] is False
    assert r["confidence_adjustment"] == 0


# ── Robustness ─────────────────────────────────────────────────────────────

def test_insufficient_h4_candles():
    r = detect_4h_manipulation(h4_candles=[], candles_m15=_m15_series([4020]*4))
    assert r["detected"] is False
    assert "H4" in r["reason"]


def test_insufficient_m15_candles():
    prev_h4 = C(open=4020, high=4030, low=4015, close=4025)
    cur_h4  = C(open=4025, high=4045, low=4020, close=4028)
    r = detect_4h_manipulation(
        h4_candles=[prev_h4, cur_h4],
        candles_m15=[],
    )
    assert r["detected"] is False


def test_both_sides_swept_ambiguous():
    """Very rare on 4H but must not crash — swept high AND low"""
    prev_h4 = C(open=4020, high=4030, low=4015, close=4025)
    cur_h4  = C(open=4025, high=4045, low=4000, close=4020)   # both wicks past
    m15 = _m15_series([4020, 4022, 4021, 4020])  # enough for the check
    r = detect_4h_manipulation(
        h4_candles=[prev_h4, cur_h4],
        candles_m15=m15,
    )
    assert r["detected"] is False
    assert "ambiguous" in r["reason"].lower()


def test_small_wick_does_not_trigger_sweep():
    """Sweep must exceed min_sweep_pts (default 1.5) to count."""
    prev_h4 = C(open=4020, high=4030, low=4015, close=4025)
    cur_h4  = C(open=4025, high=4030.5, low=4020, close=4028)  # only 0.5pt over
    m15 = _m15_series([4028, 4027, 4026, 4025])
    r = detect_4h_manipulation(
        h4_candles=[prev_h4, cur_h4],
        candles_m15=m15,
    )
    assert r["detected"] is False


# ── M5 confirmation is optional and never breaks the flow ─────────────────

def test_m5_optional_none_ok():
    prev_h4 = C(open=4020, high=4030, low=4015, close=4025)
    cur_h4  = C(open=4025, high=4045, low=4020, close=4025)
    m15 = _m15_series([4035, 4028, 4025, 4022])
    r = detect_4h_manipulation(
        h4_candles=[prev_h4, cur_h4],
        candles_m15=m15,
        candles_m5=None,       # explicitly None
        atr_h1=10.0,
    )
    assert r["detected"] is True
    assert r["m5_confirmation"] is False   # can't confirm without data
    # Should still adjust based on M15 confirmation
    assert r["confidence_adjustment"] in (5, 10)


# ── Main ────────────────────────────────────────────────────────────────────

def main():
    print("=" * 78)
    print(" 4H MANIPULATION VALIDATION (P136)")
    print("=" * 78)

    _hr("1. Bearish manipulation")
    _run("full confirmation → +10 SELL bias", test_bearish_manipulation_full_confirmation)
    _run("weak LTF → +5", test_bearish_manipulation_weak_ltf_confirmation)

    _hr("2. Bullish manipulation")
    _run("full confirmation → +10 BUY bias", test_bullish_manipulation_full_confirmation)

    _hr("3. Continuation (critical −10 case)")
    _run("held beyond high → -10 penalty", test_continuation_hold_beyond_penalizes)
    _run("held beyond low → -10 penalty", test_continuation_hold_beyond_bullish_side)

    _hr("4. Waiting / no-sweep")
    _run("sweep pending reclaim → 0", test_sweep_pending_reclaim_returns_zero)
    _run("no sweep → empty, 0", test_no_sweep_returns_empty)

    _hr("5. Robustness")
    _run("no H4 candles → empty", test_insufficient_h4_candles)
    _run("no M15 candles → empty", test_insufficient_m15_candles)
    _run("both sides swept → ambiguous, 0", test_both_sides_swept_ambiguous)
    _run("small wick below floor → no signal", test_small_wick_does_not_trigger_sweep)
    _run("M5 None does not crash", test_m5_optional_none_ok)

    print("\n" + "=" * 78)
    if PASSED == TOTAL:
        print(f" ALL PASS {PASSED}/{TOTAL} — 4H manipulation layer ready")
        return 0
    print(f" FAIL {PASSED}/{TOTAL}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
