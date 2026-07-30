"""Validate fixed-target trade plan generator (P130)."""
from __future__ import annotations
import os, sys
from dataclasses import dataclass
from types import SimpleNamespace

try:
    import services.strategist  # noqa
except ImportError:
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from services.strategist import _generate_trade_plan
from services import strategist as strat_mod


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


def _tight_candles(base: float = 4020.0):
    """Recent candles with a tight swing — SL fits easily inside envelope."""
    # 12 candles ranging ~4018-4022
    return [C(open=base-1, high=base+1, low=base-3, close=base+0.5) for _ in range(12)]


def _wide_candles(base: float = 4020.0):
    """Recent candles with a wide swing — SL would need > max_sl_points."""
    # 12 candles with a hi-lo spread of 50 pts
    return [C(open=base+i*4, high=base+i*4+2, low=base-25+i*3, close=base+i*4+1)
             for i in range(12)]


# ── Fixed-mode acceptance ──────────────────────────────────────────────────

def test_fixed_mode_buy_tight_swing():
    strat_mod.settings = SimpleNamespace(
        fixed_tp_enabled=True, target_tp1_points=20.0, target_tp2_points=40.0,
        max_sl_points=20.0, min_sl_points=8.0,
    )
    p = _generate_trade_plan(direction="BUY", current_price=4020.0,
                              atr_h1=10.0, candles_m15=_tight_candles())
    assert p["source"] == "strategist_fixed_target", p
    assert p["tp1"] == 4040.0    # entry + 20
    assert p["tp2"] == 4060.0    # entry + 40
    assert p["stop_loss"] < 4020.0
    assert 8.0 <= p["risk_pts"] <= 20.0
    assert p["rr"] >= 2.0        # 40 / (<= 20) = >= 2.0


def test_fixed_mode_sell_tight_swing():
    strat_mod.settings = SimpleNamespace(
        fixed_tp_enabled=True, target_tp1_points=20.0, target_tp2_points=40.0,
        max_sl_points=20.0, min_sl_points=8.0,
    )
    p = _generate_trade_plan(direction="SELL", current_price=4020.0,
                              atr_h1=10.0, candles_m15=_tight_candles())
    assert p["source"] == "strategist_fixed_target"
    assert p["tp1"] == 4000.0
    assert p["tp2"] == 3980.0
    assert p["stop_loss"] > 4020.0


def test_fixed_mode_rejects_wide_swing():
    """Structural SL demands > 20 pts → rejection."""
    strat_mod.settings = SimpleNamespace(
        fixed_tp_enabled=True, target_tp1_points=20.0, target_tp2_points=40.0,
        max_sl_points=20.0, min_sl_points=8.0,
    )
    # ATR forces wide SL
    p = _generate_trade_plan(direction="BUY", current_price=4020.0,
                              atr_h1=25.0, candles_m15=_wide_candles())
    assert p["source"] == "sl_too_wide_for_target", p
    assert p["entry"] is None
    assert "structural SL" in p["rejection"]


def test_fixed_mode_noise_floor():
    """Tiny structural SL widens to min_sl_points."""
    strat_mod.settings = SimpleNamespace(
        fixed_tp_enabled=True, target_tp1_points=20.0, target_tp2_points=40.0,
        max_sl_points=20.0, min_sl_points=8.0,
    )
    # Extremely tight candles + tiny ATR
    cs = [C(open=4020, high=4020.5, low=4019.8, close=4020.2) for _ in range(12)]
    p = _generate_trade_plan(direction="BUY", current_price=4020.0,
                              atr_h1=1.0, candles_m15=cs)
    assert p["source"] == "strategist_fixed_target"
    assert p["risk_pts"] >= 8.0    # widened to noise floor
    assert p["stop_loss"] == round(4020.0 - p["risk_pts"], 2)


def test_fixed_mode_rr_math():
    """RR should be tp2_points / sl_points."""
    strat_mod.settings = SimpleNamespace(
        fixed_tp_enabled=True, target_tp1_points=20.0, target_tp2_points=40.0,
        max_sl_points=20.0, min_sl_points=8.0,
    )
    p = _generate_trade_plan(direction="BUY", current_price=4020.0,
                              atr_h1=10.0, candles_m15=_tight_candles())
    expected_rr = round(40.0 / p["risk_pts"], 2)
    assert p["rr"] == expected_rr


def test_fixed_mode_target_metadata():
    strat_mod.settings = SimpleNamespace(
        fixed_tp_enabled=True, target_tp1_points=25.0, target_tp2_points=50.0,
        max_sl_points=25.0, min_sl_points=8.0,
    )
    p = _generate_trade_plan(direction="BUY", current_price=4020.0,
                              atr_h1=10.0, candles_m15=_tight_candles())
    assert p["target_pts_tp1"] == 25.0
    assert p["target_pts_tp2"] == 50.0
    assert p["tp1"] == 4045.0
    assert p["tp2"] == 4070.0


def test_fixed_mode_disabled_falls_back_to_atr():
    strat_mod.settings = SimpleNamespace(fixed_tp_enabled=False)
    p = _generate_trade_plan(direction="BUY", current_price=4020.0,
                              atr_h1=10.0, candles_m15=_tight_candles())
    assert p["source"] == "strategist_atr"
    # ATR mode uses R-multiples so TPs are variable
    assert p["tp1"] is not None
    assert p["tp2"] is not None


# ── Sanity ─────────────────────────────────────────────────────────────────

def test_returns_none_on_bad_direction():
    strat_mod.settings = SimpleNamespace(fixed_tp_enabled=True)
    p = _generate_trade_plan(direction="FLAT", current_price=4020.0,
                              atr_h1=10.0, candles_m15=_tight_candles())
    assert p["entry"] is None
    assert p["source"] == "none"


def test_tp_ordering_buy():
    strat_mod.settings = SimpleNamespace(
        fixed_tp_enabled=True, target_tp1_points=20.0, target_tp2_points=40.0,
        max_sl_points=20.0, min_sl_points=8.0,
    )
    p = _generate_trade_plan(direction="BUY", current_price=4020.0,
                              atr_h1=10.0, candles_m15=_tight_candles())
    assert p["entry"] < p["tp1"] < p["tp2"]
    assert p["stop_loss"] < p["entry"]


def test_tp_ordering_sell():
    strat_mod.settings = SimpleNamespace(
        fixed_tp_enabled=True, target_tp1_points=20.0, target_tp2_points=40.0,
        max_sl_points=20.0, min_sl_points=8.0,
    )
    p = _generate_trade_plan(direction="SELL", current_price=4020.0,
                              atr_h1=10.0, candles_m15=_tight_candles())
    assert p["entry"] > p["tp1"] > p["tp2"]
    assert p["stop_loss"] > p["entry"]


def main():
    print("=" * 78)
    print(" FIXED-TARGET PLAN VALIDATION (P130)")
    print("=" * 78)

    _hr("1. Fixed mode acceptance")
    _run("BUY with tight swing", test_fixed_mode_buy_tight_swing)
    _run("SELL with tight swing", test_fixed_mode_sell_tight_swing)
    _run("noise floor widens tiny SL", test_fixed_mode_noise_floor)
    _run("RR math correct", test_fixed_mode_rr_math)
    _run("target metadata surfaced", test_fixed_mode_target_metadata)

    _hr("2. Rejection")
    _run("wide swing → sl_too_wide_for_target", test_fixed_mode_rejects_wide_swing)
    _run("bad direction returns None", test_returns_none_on_bad_direction)

    _hr("3. TP ordering")
    _run("BUY: entry < tp1 < tp2, sl < entry", test_tp_ordering_buy)
    _run("SELL: entry > tp1 > tp2, sl > entry", test_tp_ordering_sell)

    _hr("4. Fallback")
    _run("fixed_tp_enabled=false uses ATR R-multiples", test_fixed_mode_disabled_falls_back_to_atr)

    print("\n" + "=" * 78)
    if PASSED == TOTAL:
        print(f" ALL PASS {PASSED}/{TOTAL} — fixed-target plan ready")
        return 0
    print(f" FAIL {PASSED}/{TOTAL}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
