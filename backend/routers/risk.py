"""
Risk endpoints — cross-pair sustainability analysis.

GET /api/v1/risk/two-pair-sustainability
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.orm import Session

from database import get_db
from db_models import SignalRecord
from models.common import APIResponse
from pair_config import SUPPORTED_PAIRS, get_pair_config

router = APIRouter(prefix="/risk", tags=["risk"])
log = logging.getLogger(__name__)


# ── Response models ───────────────────────────────────────────────────────────

class PairRiskSnapshot(BaseModel):
    pair_code:          str
    display:            str
    mode:               str
    completed_trades:   int
    win_rate:           float | None
    avg_quality_score:  float | None
    recent_stale_count: int
    data_source_mix:    dict[str, int]
    blocker_count:      int
    risk_level:         str     # "low" | "medium" | "high" | "critical"


class CorrelationRisk(BaseModel):
    description:  str
    severity:     str           # "info" | "warning" | "critical"
    detail:       str


class TwoPairSustainabilityReport(BaseModel):
    assessed_at:       str
    overall_risk:      str      # "low" | "medium" | "high" | "critical"
    pairs:             list[PairRiskSnapshot]
    correlation_risks: list[CorrelationRisk]
    combined_warnings: list[str]
    recommendations:   list[str]
    r_multiple_note:   str


# ── Helpers ───────────────────────────────────────────────────────────────────

def _pair_snapshot(
    pair_code: str,
    confirmed: list[Any],
    completed: list[Any],
) -> tuple[PairRiskSnapshot, list[str]]:
    cfg = get_pair_config(pair_code)
    blockers: list[str] = []

    now = datetime.now(timezone.utc)
    cutoff_48h = now - timedelta(hours=48)

    # Data source mix
    source_mix: dict[str, int] = {}
    stale_recent = 0
    for r in confirmed:
        src = getattr(r, "data_source", "synthetic") or "synthetic"
        source_mix[src] = source_mix.get(src, 0) + 1
        created = getattr(r, "created_at", None)
        if src == "stale" and created is not None:
            if created.replace(tzinfo=timezone.utc) if created.tzinfo is None else created >= cutoff_48h:
                stale_recent += 1

    # Win rate
    wins = [t for t in completed if getattr(t, "result", None) == "WIN"]
    win_rate = round(len(wins) / len(completed) * 100, 1) if completed else None

    # Avg quality score
    qs_vals = [getattr(r, "quality_score", None) for r in confirmed if getattr(r, "quality_score", None) is not None]
    avg_qs = round(sum(qs_vals) / len(qs_vals), 1) if qs_vals else None

    # Risk level
    risk_level = "low"
    if stale_recent > 3:
        risk_level = "high"
        blockers.append(f"{stale_recent} stale-candle signals in last 48h")
    elif stale_recent > 0:
        risk_level = "medium"

    if win_rate is not None and win_rate < 40:
        risk_level = "critical" if risk_level in ("high",) else "high"
        blockers.append(f"Win rate {win_rate}% below 40% floor")

    if not confirmed:
        risk_level = "medium"

    return PairRiskSnapshot(
        pair_code=pair_code,
        display=cfg["display"],
        mode=cfg["mode"],
        completed_trades=len(completed),
        win_rate=win_rate,
        avg_quality_score=avg_qs,
        recent_stale_count=stale_recent,
        data_source_mix=source_mix,
        blocker_count=len(blockers),
        risk_level=risk_level,
    ), blockers


def _correlation_risks(
    eurusd_completed: list[Any],
    xauusd_completed: list[Any],
) -> list[CorrelationRisk]:
    risks: list[CorrelationRisk] = []

    # Risk: simultaneous active signals (both pairs BUY/SELL at same time)
    risks.append(CorrelationRisk(
        description="Simultaneous active signals",
        severity="warning",
        detail=(
            "EUR/USD and XAU/USD can both trigger signals during USD-heavy news events. "
            "Running both pairs live simultaneously doubles USD exposure. "
            "In MONITOR_ONLY mode this is safe — flag if either pair is promoted."
        ),
    ))

    # Risk: pip vs point units in combined analytics
    risks.append(CorrelationRisk(
        description="Units mismatch in combined analytics",
        severity="warning",
        detail=(
            "EUR/USD pips (0.0001 increments) and XAU/USD points (1.0 increments) "
            "cannot be summed directly. Combined analytics must use R-multiple, "
            "not raw pips. Always use per-pair analytics for absolute performance."
        ),
    ))

    # Risk: USD correlation
    risks.append(CorrelationRisk(
        description="High USD correlation between pairs",
        severity="info",
        detail=(
            "Both EUR/USD and XAU/USD are USD-priced instruments. "
            "Strong USD news events (NFP, FOMC, CPI) move both pairs simultaneously. "
            "A directional bias error on USD affects both positions."
        ),
    ))

    # Risk: news window overlap
    risks.append(CorrelationRisk(
        description="News window timing overlap",
        severity="info",
        detail=(
            "EUR/USD blocks 30m before / 60m after USD events. "
            "XAU/USD blocks 45m before / 90m after. "
            "Their news windows differ — a signal may clear for EUR/USD "
            "while XAU/USD is still blocked. Handle each pair's news filter independently."
        ),
    ))

    return risks


# ── Route ─────────────────────────────────────────────────────────────────────

@router.get(
    "/two-pair-sustainability",
    response_model=APIResponse[TwoPairSustainabilityReport],
    summary="Cross-pair sustainability risk report",
    description=(
        "Evaluates the risks of running EUR/USD and XAU/USD concurrently. "
        "Checks data quality, win rates, stale candle frequency, mode compatibility, "
        "and cross-pair correlation risks. Returns actionable recommendations."
    ),
)
def two_pair_sustainability(db: Session = Depends(get_db)) -> APIResponse[TwoPairSustainabilityReport]:
    snapshots: list[PairRiskSnapshot] = []
    all_blockers: list[str] = []
    warnings: list[str] = []
    recommendations: list[str] = []

    pair_data: dict[str, tuple[list, list]] = {}   # code → (confirmed, completed)

    for code in SUPPORTED_PAIRS:
        cfg = get_pair_config(code)
        confirmed = (
            db.query(SignalRecord)
              .filter(SignalRecord.confirmed == True)
              .filter(SignalRecord.pair == cfg["display"])
              .all()
        )
        completed = [r for r in confirmed if getattr(r, "result", None) in ("WIN", "LOSS", "BREAKEVEN")]
        pair_data[code] = (confirmed, completed)

        snap, bl = _pair_snapshot(code, confirmed, completed)
        snapshots.append(snap)
        all_blockers.extend(bl)

    # Correlation risks
    eu_completed = pair_data.get("eurusd", ([], []))[1]
    xau_completed = pair_data.get("xauusd", ([], []))[1]
    corr_risks = _correlation_risks(eu_completed, xau_completed)

    # Combined warnings
    both_monitor_only = all(s.mode == "MONITOR_ONLY" for s in snapshots)
    if both_monitor_only:
        warnings.append("Both pairs in MONITOR_ONLY — safe to run concurrently, no execution risk")
    else:
        promoted = [s.display for s in snapshots if s.mode != "MONITOR_ONLY"]
        warnings.append(
            f"Promoted pairs: {', '.join(promoted)} — ensure independent position sizing "
            f"and account for correlated USD exposure"
        )

    any_stale = any(s.recent_stale_count > 0 for s in snapshots)
    if any_stale:
        warnings.append("One or more pairs had stale candles recently — verify data feeds before any promotion")

    no_history = [s.display for s in snapshots if s.completed_trades == 0]
    if no_history:
        warnings.append(f"No completed trade history for: {', '.join(no_history)} — cannot assess real performance")

    # Recommendations
    for snap in snapshots:
        if snap.completed_trades < 10:
            recommendations.append(
                f"{snap.display}: Log at least {10 - snap.completed_trades} more trade outcomes before Week 1 assessment"
            )
        if snap.recent_stale_count > 0:
            recommendations.append(f"{snap.display}: Investigate stale candle source — check TradingView connectivity")
        if snap.mode == "MONITOR_ONLY" and snap.completed_trades >= 20 and snap.win_rate and snap.win_rate >= 55:
            recommendations.append(
                f"{snap.display}: Performance looks promising — run readiness check (GET /api/v1/readiness) "
                f"to assess PAPER_OBSERVATION promotion"
            )

    if not eu_completed and not xau_completed:
        recommendations.append(
            "Start logging trade outcomes via PUT /{signal_id}/result to build a real performance record"
        )

    recommendations.append(
        "Use GET /api/v1/analytics?pair=eurusd and ?pair=xauusd for per-pair analytics — "
        "never compare raw pips between pairs (use R-multiple)"
    )

    # Overall risk
    risk_levels = [s.risk_level for s in snapshots]
    if "critical" in risk_levels:
        overall = "critical"
    elif "high" in risk_levels:
        overall = "high"
    elif "medium" in risk_levels:
        overall = "medium"
    else:
        overall = "low"

    if all_blockers:
        overall = "high" if overall == "medium" else overall

    log.info("Two-pair sustainability assessed overall_risk=%s", overall)

    return APIResponse(data=TwoPairSustainabilityReport(
        assessed_at=datetime.now(timezone.utc).isoformat(),
        overall_risk=overall,
        pairs=snapshots,
        correlation_risks=corr_risks,
        combined_warnings=warnings,
        recommendations=recommendations,
        r_multiple_note=(
            "EUR/USD pips and XAU/USD points are incompatible units. "
            "Combined performance must be expressed in R-multiples (profit / initial risk). "
            "A 2R trade on EUR/USD (40 pips) equals a 2R trade on XAU/USD (50 points) — "
            "the R-multiple is the only valid cross-pair comparison metric."
        ),
    ))
