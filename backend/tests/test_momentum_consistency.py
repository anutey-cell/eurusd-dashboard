"""Regression tests for momentum-signal consistency across runtime paths."""
from __future__ import annotations

import inspect

from services.dual_engine_runner import run_dual_engines
from services.intraday_strategies import analyze_momentum_breakout
from services.momentum_gate_audit import _canonical_defaults


def test_gate_audit_reads_live_strategy_defaults() -> None:
    """The diagnostic must report the defaults the live signal actually uses."""
    sig = inspect.signature(analyze_momentum_breakout)
    expected = {
        "min_body_atr_mult": float(sig.parameters["min_body_atr_mult"].default),
        "min_volume_mult": float(sig.parameters["min_volume_mult"].default),
        "min_close_pct": float(sig.parameters["min_close_pct"].default),
        "max_sl_pts": float(sig.parameters["max_sl_pts"].default),
    }
    assert _canonical_defaults() == expected


def test_paper_runner_does_not_override_momentum_thresholds() -> None:
    """Paper observation and live Telegram paths must call the same defaults."""
    source = inspect.getsource(run_dual_engines)
    momentum_section = source.split("# ── Engine 3: Momentum Breakout M15", 1)[1]

    # These parameters belong to analyze_momentum_breakout itself. Repeating
    # them here previously left the paper runner on obsolete 2.0/1.5/0.80/25
    # values while the live alert path had already been tuned.
    assert "min_body_atr_mult=" not in momentum_section
    assert "min_volume_mult=" not in momentum_section
    assert "min_close_pct=" not in momentum_section
    assert "max_sl_pts=" not in momentum_section
