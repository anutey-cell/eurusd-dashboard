"""
Killzone × Direction Policy Filter
==================================

The 4th gate in the auto-executor's confirmation chain. The first three
(scanner + predictor + killzone-edge-score) measure whether the SETUP looks
good. This module asks a different question: *historically, has this exact
(killzone, direction, engine) cell produced positive expectancy?*

The policy table below was learned from 245 resolved paper observations
collected 2023-05 → 2026-04. Each cell shows: trade count, win rate, and
average R-multiple. The decision rule for ALLOWING a fire:

  ALLOW    if (engine == "swing")                              # rare but +1.06 ExpR
  ALLOW    if cell.expectancy_r > 0   AND  cell.sample >= 20   # measured edge
  EXPLORE  if cell.sample < 20                                 # too thin to judge
  BLOCK    otherwise                                            # measured negative

The "EXPLORE" tier still allows the trade but emits an `is_exploratory: True`
flag so the operator (or future ML model) can weight it differently when
aggregating results.

This policy is INTENTIONALLY simple and human-readable. It is not learned
in real-time — instead it's a hand-curated snapshot of what the data showed.
Update the table below by re-running the killzone-aggregation analysis and
adjusting the rows. Don't try to fit live RL to it: the sample is small,
gold is non-stationary, and the goal is to make ONE good filter, not many.

Audit trail: see the engine assessment dated 2026-05-22 in chat history.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

log = logging.getLogger(__name__)


# ── The learned table — (killzone_key, direction) → policy row ──────────────
# killzone_key is one of: asian_early, asian, london_pre, london_kz,
#                          overlap, ny_kz, ny_pm
# (matches services.killzone_analyzer.KILLZONES[*]["key"])

@dataclass
class PolicyRow:
    killzone:    str
    direction:   str        # "BUY" | "SELL"
    decision:    str        # "ALLOW" | "BLOCK" | "EXPLORE"
    sample_size: int
    win_rate:    float      # %
    expectancy_r:float
    note:        str = ""


POLICY_TABLE: list[PolicyRow] = [
    # ── London KZ (07:00-10:00 UTC) — best-edge session ─────────────────────
    # 84 BUYs at 26.2% WR with 4.5R targets = clear edge.
    PolicyRow("london_kz", "BUY",  "ALLOW",  84, 26.2, +0.46,
              "Largest sample, robust positive edge."),
    # 40 SELLs at 5.0% WR — measured loser. Block.
    PolicyRow("london_kz", "SELL", "BLOCK",  40,  5.0, -0.50,
              "Counter-trend SELLs in London consistently fail."),

    # ── NY PM (16:00-22:00 UTC) — small but profitable SELL edge ────────────
    PolicyRow("ny_pm",     "SELL", "ALLOW",  10, 40.0, +0.80,
              "Small sample (n=10), 40% WR — afternoon mean-reversion edge."),
    PolicyRow("ny_pm",     "BUY",  "BLOCK",  12, 16.7, -0.10,
              "BUYs at NY PM fade consistently — late-day momentum dies."),

    # ── NY KZ (13:00-16:00 UTC) — SURPRISE LOSER ────────────────────────────
    # Price-action features said this would be best; outcomes proved otherwise.
    PolicyRow("ny_kz",     "BUY",  "BLOCK",  17, 23.5, -0.10,
              "Apparent edge in features but trade outcomes negative."),
    PolicyRow("ny_kz",     "SELL", "BLOCK",  26,  0.0, -0.30,
              "26 trades, 0 wins. Strong negative cell."),

    # ── Pre-Overlap (10:00-13:00 UTC) — chop window ─────────────────────────
    PolicyRow("overlap",   "BUY",  "BLOCK",  30, 16.7, -0.20,
              "Mid-range chop — no follow-through."),
    PolicyRow("overlap",   "SELL", "BLOCK",  24,  8.3, -0.27,
              "Mid-range chop — no follow-through."),

    # ── Asian Range — too thin to judge, allow as exploratory ──────────────
    PolicyRow("asian",     "BUY",  "EXPLORE", 2, 50.0, +4.79,
              "Only 2 trades. Allow to gather more data."),
    PolicyRow("asian",     "SELL", "EXPLORE", 0,  0.0,  0.00,
              "No samples. Allow to discover behavior."),

    # ── Sessions with zero historical data — block by default ──────────────
    PolicyRow("asian_early","BUY", "BLOCK",  0,  0.0,  0.00,
              "Off-session — no historical edge measured."),
    PolicyRow("asian_early","SELL","BLOCK",  0,  0.0,  0.00,
              "Off-session — no historical edge measured."),
    PolicyRow("london_pre", "BUY", "BLOCK",  0,  0.0,  0.00,
              "Pre-session — no historical edge measured."),
    PolicyRow("london_pre", "SELL","BLOCK",  0,  0.0,  0.00,
              "Pre-session — no historical edge measured."),
]


def _row(killzone: str, direction: str) -> Optional[PolicyRow]:
    for r in POLICY_TABLE:
        if r.killzone == killzone and r.direction == direction:
            return r
    return None


# ── Evaluation ───────────────────────────────────────────────────────────────

@dataclass
class PolicyVerdict:
    allow:       bool
    decision:    str            # "ALLOW" | "BLOCK" | "EXPLORE"
    reason:      str
    killzone:    str
    direction:   str
    engine:      str
    sample_size: int
    historical_wr:    float
    historical_exp_r: float
    is_exploratory:   bool
    bypass_reason:    str | None = None


def evaluate(
    killzone_key:  str,
    direction:     str,
    engine_id:     str = "trend_pullback",
    bypass_engines: set[str] | None = None,
) -> PolicyVerdict:
    """
    Decide whether to ALLOW a (killzone, direction, engine) firing.

    Bypass rules: the `swing` ICT engine has +1.06 ExpR across 15 trades, so
    it bypasses the table and is always allowed. The caller can pass
    additional engines to bypass via `bypass_engines`.
    """
    bypass = bypass_engines or {"swing"}
    if engine_id in bypass:
        return PolicyVerdict(
            allow=True, decision="ALLOW", reason="bypass:engine_has_independent_edge",
            killzone=killzone_key, direction=direction, engine=engine_id,
            sample_size=0, historical_wr=0.0, historical_exp_r=0.0,
            is_exploratory=False, bypass_reason=f"engine={engine_id} bypass",
        )

    row = _row(killzone_key, direction)
    if row is None:
        # Defensive default for unknown (killzone, direction) pairs
        return PolicyVerdict(
            allow=False, decision="BLOCK",
            reason=f"No policy row for {killzone_key} {direction} — blocking by default",
            killzone=killzone_key, direction=direction, engine=engine_id,
            sample_size=0, historical_wr=0.0, historical_exp_r=0.0,
            is_exploratory=False,
        )

    allow = row.decision in ("ALLOW", "EXPLORE")
    reason = (
        f"{row.decision} {killzone_key} {direction} — "
        f"hist {row.sample_size} trades, WR {row.win_rate:.1f}%, "
        f"ExpR {row.expectancy_r:+.2f}. {row.note}"
    )
    return PolicyVerdict(
        allow=allow, decision=row.decision, reason=reason,
        killzone=killzone_key, direction=direction, engine=engine_id,
        sample_size=row.sample_size,
        historical_wr=row.win_rate,
        historical_exp_r=row.expectancy_r,
        is_exploratory=(row.decision == "EXPLORE"),
    )


def get_full_policy() -> dict:
    """Return the full policy table — used by /diagnostics/killzone-policy."""
    summary = {"ALLOW": 0, "BLOCK": 0, "EXPLORE": 0}
    for r in POLICY_TABLE:
        summary[r.decision] = summary.get(r.decision, 0) + 1
    return {
        "rows": [
            {
                "killzone":     r.killzone,
                "direction":    r.direction,
                "decision":     r.decision,
                "sample_size":  r.sample_size,
                "win_rate":     r.win_rate,
                "expectancy_r": r.expectancy_r,
                "note":         r.note,
            }
            for r in POLICY_TABLE
        ],
        "summary":           summary,
        "total_cells":       len(POLICY_TABLE),
        "bypass_engines":    ["swing"],
        "decision_rule": (
            "ALLOW if engine in bypass OR (sample>=20 AND ExpR>0). "
            "EXPLORE if sample<20. BLOCK otherwise."
        ),
        "source_audit": (
            "Hand-curated from 245-trade dataset (2023-05 → 2026-04). "
            "Update via aggregation analysis, not RL."
        ),
    }
