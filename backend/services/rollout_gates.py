"""
Rollout Gates & Promotion Readiness — Phase 15
================================================

Every Phase 2-14 flag ships OFF. This module encodes:
  1. The promotion criteria for each flag ("what does 'earn its way in' mean?")
  2. Live evaluation of those criteria against captured data
  3. Kill-switch helpers for emergency disable

The final rule: no flag flips ON without both:
  - live evidence its criteria are met (coverage %, shadow days, false-alert rate)
  - explicit human approval (this module never writes to config)

Behind `xauusd_replay_validation_enabled`. Read-only.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone, timedelta
from typing import Optional

from sqlalchemy import text
from sqlalchemy.orm import Session

log = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Promotion criteria per flag
# ─────────────────────────────────────────────────────────────────────────────

# Every Phase 2-14 flag with its criteria + rationale.
# The criteria are read-only: an operator + this module recommend, humans decide.
_PROMOTION_CRITERIA: dict[str, dict] = {
    "xauusd_canonical_data_enabled": {
        "requires":   "endpoint returns data_quality_score >= 90 for 7 days",
        "risk":       "low — read-only observability",
        "gate_flag":  None,
        "phase":      2,
    },
    "xauusd_market_regime_enabled": {
        "requires":   "shadow endpoint stable for 7 days, no exceptions in logs",
        "risk":       "low — additive, doesn't touch strategist path",
        "gate_flag":  None,
        "phase":      3,
    },
    "xauusd_weighted_htf_alignment_enabled": {
        "requires":   "replay verdict = BETTER or NEUTRAL, direction accuracy >= old engine",
        "risk":       "medium — changes C1 gate; could reduce or increase signals",
        "gate_flag":  None,
        "phase":      4,
    },
    "xauusd_directional_intelligence_enabled": {
        "requires":   "evidence scoring stable, false-alert rate < 20%",
        "risk":       "low — additive to snapshot",
        "gate_flag":  None,
        "phase":      5,
    },
    "xauusd_breakout_acceptance_enabled": {
        "requires":   "no exceptions in scan_key_levels for 7 days",
        "risk":       "low — additive",
        "gate_flag":  None,
        "phase":      6,
    },
    "xauusd_opportunity_state_machine_enabled": {
        "requires":   "state transitions persist correctly for 7 days, restart-safe verified",
        "risk":       "medium — persisted state, migration path if disabled",
        "gate_flag":  None,
        "phase":      7,
    },
    "xauusd_separated_verdicts_enabled": {
        "requires":   "verdict endpoint stable, does not surface as entry signal",
        "risk":       "low — additive fields on strategist verdict, decision unchanged",
        "gate_flag":  None,
        "phase":      8,
    },
    "xauusd_key_level_ranking_enabled": {
        "requires":   "tier assignments stable, Tier 1 count <= 4 always",
        "risk":       "low — additive",
        "gate_flag":  None,
        "phase":      9,
    },
    "xauusd_macro_interpretation_enabled": {
        "requires":   "no auto-vetos on technical direction, correlation state populated",
        "risk":       "low — supporting context only",
        "gate_flag":  None,
        "phase":      10,
    },
    "xauusd_market_intelligence_telegram_enabled": {
        "requires":   "7 days shadow parity, coverage delta > +10% vs old, false alerts < 20% of intel alerts",
        "risk":       "HIGH — user-visible; alerts land on Telegram",
        "gate_flag":  "xauusd_market_intel_shadow_mode",
        "phase":      11,
    },
    "xauusd_opportunity_coverage_enabled": {
        "requires":   "nightly job runs without exceptions for 7 days",
        "risk":       "low — read-only measurement",
        "gate_flag":  None,
        "phase":      13,
    },
    "xauusd_replay_validation_enabled": {
        "requires":   "replay endpoint returns non-empty scenarios for 30-day window",
        "risk":       "low — diagnostic only",
        "gate_flag":  None,
        "phase":      14,
    },
}


# ─────────────────────────────────────────────────────────────────────────────
# Output
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class FlagStatus:
    flag:                 str
    phase:                int
    currently_enabled:    bool
    gate_flag:            Optional[str]
    gate_currently:       Optional[bool]
    requires:             str
    risk:                 str
    ready:                Optional[bool]        # None = cannot judge (missing data)
    ready_reason:         str

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class RolloutStatusReport:
    generated_at:         datetime
    flags:                list[FlagStatus] = field(default_factory=list)
    ready_count:          int = 0
    not_ready_count:      int = 0
    unable_to_judge:      int = 0
    kill_switch_hint:     str = ""

    def to_dict(self) -> dict:
        d = {
            "generated_at": self.generated_at.isoformat(),
            "flags":         [f.to_dict() for f in self.flags],
            "ready_count":   self.ready_count,
            "not_ready_count": self.not_ready_count,
            "unable_to_judge": self.unable_to_judge,
            "kill_switch_hint": self.kill_switch_hint,
        }
        return d


# ─────────────────────────────────────────────────────────────────────────────
# Live readiness evaluation
# ─────────────────────────────────────────────────────────────────────────────

def _judge_ready(flag: str, db: Session) -> tuple[Optional[bool], str]:
    """
    Return (ready?, reason). None means we can't judge yet.
    """
    try:
        if flag == "xauusd_canonical_data_enabled":
            # Data quality >= 90 for last 7 days: proxy via freshness_details
            from services.data_freshness import check_freshness
            fresh = check_freshness(db)
            dq = fresh.get("data_quality_score", 0)
            if dq >= 90:
                return (True, f"data_quality_score={dq} ≥ 90")
            return (False, f"data_quality_score={dq} < 90")

        if flag == "xauusd_market_intelligence_telegram_enabled":
            # Requires 7+ days shadow parity + coverage delta > +10%
            row = db.execute(text(
                "SELECT COUNT(*), MIN(ts), MAX(ts) FROM market_intelligence_alerts "
                "WHERE delivery_result IN ('shadow','sent')"
            )).fetchone()
            n = int(row[0]) if row else 0
            if n < 20:
                return (False, f"only {n} shadow alerts — need at least 20 across 7 days")
            first_ts = row[1]; last_ts = row[2]
            if first_ts and last_ts:
                if hasattr(first_ts, "isoformat"):
                    span_days = (last_ts - first_ts).total_seconds() / 86400
                else:
                    span_days = 0
                if span_days < 7:
                    return (False, f"only {span_days:.1f} days of shadow data — need 7")
            return (None, f"{n} shadow alerts recorded — check replay delta before flipping")

        if flag == "xauusd_opportunity_coverage_enabled":
            row = db.execute(text(
                "SELECT COUNT(*) FROM qualifying_expansions"
            )).fetchone()
            n = int(row[0]) if row else 0
            if n == 0:
                return (False, "no expansions in table yet — run detect_and_score first")
            return (True, f"{n} expansions recorded — job runs successfully")

        if flag == "xauusd_replay_validation_enabled":
            row = db.execute(text(
                "SELECT COUNT(*) FROM qualifying_expansions"
            )).fetchone()
            n = int(row[0]) if row else 0
            if n < 5:
                return (False, f"only {n} expansions — need >=5 for meaningful replay")
            return (True, f"{n} expansions available — replay is meaningful")

        # For other flags — additive layers that emit no side effects.
        # If their diagnostic endpoint has been hit successfully (basic
        # existence check), assume ready. We don't have per-endpoint hit
        # counters, so return None with instruction.
        return (None, "additive layer — flip when you want it in the pipeline")
    except Exception as exc:
        log.warning("[rollout] judge failed for %s: %s", flag, exc)
        return (None, f"judge error: {exc}")


def evaluate_rollout(db: Session) -> RolloutStatusReport:
    """Read every criterion, compare against live state, produce a report."""
    from config import settings

    flags: list[FlagStatus] = []
    ready = 0
    not_ready = 0
    unknown = 0

    for flag_name, meta in _PROMOTION_CRITERIA.items():
        currently = bool(getattr(settings, flag_name, False))
        gate_flag = meta.get("gate_flag")
        gate_current = bool(getattr(settings, gate_flag, False)) if gate_flag else None
        judged, reason = _judge_ready(flag_name, db)
        if judged is True:
            ready += 1
        elif judged is False:
            not_ready += 1
        else:
            unknown += 1
        flags.append(FlagStatus(
            flag=flag_name, phase=meta["phase"],
            currently_enabled=currently,
            gate_flag=gate_flag, gate_currently=gate_current,
            requires=meta["requires"], risk=meta["risk"],
            ready=judged, ready_reason=reason,
        ))

    hint = (
        "Emergency disable: set every XAUUSD_*_ENABLED env var to false and "
        "XAUUSD_MARKET_INTEL_SHADOW_MODE=true, restart the container. See "
        "scripts/emergency_disable.py for the exact commands."
    )
    return RolloutStatusReport(
        generated_at=datetime.now(timezone.utc),
        flags=flags, ready_count=ready,
        not_ready_count=not_ready, unable_to_judge=unknown,
        kill_switch_hint=hint,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Kill-switch helper
# ─────────────────────────────────────────────────────────────────────────────

def emergency_env_reset() -> dict[str, str]:
    """
    Return the exact env-var overrides needed to disable every Phase 2-14
    layer. Does NOT write to the environment — caller writes to .env.prod
    or exports and restarts.

    Usage:
        vars = emergency_env_reset()
        # Copy into .env.prod, restart backend
    """
    out: dict[str, str] = {}
    for flag_name in _PROMOTION_CRITERIA.keys():
        out[flag_name.upper()] = "false"
    out["XAUUSD_MARKET_INTEL_SHADOW_MODE"] = "true"
    return out


__all__ = [
    "evaluate_rollout", "emergency_env_reset",
    "FlagStatus", "RolloutStatusReport",
]
