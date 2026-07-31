"""
Skip-Reason Statistics
=======================

In-process counter that answers: "when the engine sat out at 3/5 or 4/5,
WHICH single condition was the primary blocker?"

Called from strategist.py on every verdict. Aggregates over a rolling
window; exposed via /api/v1/diagnostics/skip-stats. Resets on restart —
a rolling week is plenty for calibration; persistent stats come from
the strategist_verdicts table later.

Purpose: the P128 gates cut execution 10× overnight. We need to know
WHY a would-be-executable setup got downgraded. If C3 (structure) is
the top blocker in trend regimes, we know reversal patterns aren't
forming and the mandate is architecture-mismatched. If C5 (RR) is
top, the fixed-target envelope is too tight. Data over guessing.
"""
from __future__ import annotations

import threading
from collections import Counter, deque
from datetime import datetime, timezone, timedelta
from typing import Optional


_LOCK = threading.Lock()

# ── Rolling event log ───────────────────────────────────────────────────────
# Each event: (timestamp, cp, setup_score, failed_condition_names_tuple,
#              execution_status, gate_reason_short)
_EVENTS: deque = deque(maxlen=5000)     # ~4 days at 60s cadence


def record(
    *,
    conditions: list[dict],
    conditions_passed: int,
    setup_score: int,
    execution_status: str,
    exec_reason: str = "",
) -> None:
    """Record a single verdict's skip context. Never raises."""
    try:
        failed = tuple(
            c.get("name", "?").split(":", 1)[0].strip()
            for c in (conditions or [])
            if not c.get("passed")
        )
        # Extract short reason like "Q1_BLOCK" / "H1_BLOCK" from exec_reason
        short = ""
        if exec_reason:
            for tok in exec_reason.split():
                if "BLOCK" in tok or "DEMOTE" in tok:
                    short = tok.rstrip(":.,")
                    break

        with _LOCK:
            _EVENTS.append((
                datetime.now(timezone.utc),
                int(conditions_passed or 0),
                int(setup_score or 0),
                failed,
                str(execution_status or ""),
                short,
            ))
    except Exception:
        # Never raise from stats code
        pass


def summary(hours: int = 24) -> dict:
    """
    Aggregate the rolling window into an operator-readable dict.

    Returns:
      {
        "window_hours": N,
        "total_ticks":  N,
        "by_cp":        {0: ..., 1: ..., ...},
        "top_failed_conditions": [("C3", 812), ("C1", 604), ...],
        "gate_blocks":  [("Q1_BLOCK:...", 33), ("H1_BLOCK:...", 12), ...],
        "score_p50":    N,
        "score_p90":    N,
        "score_p99":    N,
        "score_max":    N,
        "would_execute_at_score": {80: N, 82: N, 85: N, 90: N},
      }
    """
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    with _LOCK:
        events = [e for e in _EVENTS if e[0] >= cutoff]

    if not events:
        return {"window_hours": hours, "total_ticks": 0}

    n = len(events)
    cp_counter = Counter(e[1] for e in events)

    # Failed condition frequency (only meaningful for cp >= 3 — high-quality
    # setups that missed by one condition)
    high_cp = [e for e in events if e[1] >= 3]
    failed_flat: list[str] = []
    for e in high_cp:
        failed_flat.extend(e[3])
    top_failed = Counter(failed_flat).most_common(6)

    gate_short = Counter(e[5] for e in events if e[5]).most_common(6)

    scores_sorted = sorted(e[2] for e in events)
    def _pct(p):
        idx = min(len(scores_sorted) - 1, int(n * p))
        return scores_sorted[idx]

    thresholds = (75, 80, 82, 85, 87, 90)
    would_exec = {t: sum(1 for e in events
                            if e[1] >= 4 and e[2] >= t) for t in thresholds}

    return {
        "window_hours":            hours,
        "total_ticks":             n,
        "by_cp":                   dict(sorted(cp_counter.items())),
        "top_failed_conditions":   top_failed,
        "gate_blocks":             gate_short,
        "score_p50":               _pct(0.50),
        "score_p90":               _pct(0.90),
        "score_p99":               _pct(0.99),
        "score_max":               scores_sorted[-1],
        "would_execute_at_score":  would_exec,
    }


def reset() -> None:
    """Manual reset (mostly for tests)."""
    with _LOCK:
        _EVENTS.clear()


__all__ = ["record", "summary", "reset"]
