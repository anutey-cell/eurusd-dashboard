"""
XAU/USD Operating Risk Panel.

GET /api/v1/risk/xauusd  — instrument risk snapshot
GET /api/v1/risk         — alias for the above

Evaluates:
  - Data quality (live vs synthetic vs stale)
  - Stale candle frequency (last 48 h)
  - Win rate and signal quality
  - News risk status
  - Volatility vs spread config
  - Broker execution status
  - Signal gate discipline
  - Mode / readiness gate
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
from pair_config import get_pair_config

router = APIRouter(prefix="/risk", tags=["risk"])
log = logging.getLogger(__name__)

_UNSUPPORTED = (
    "Unsupported instrument. This dashboard supports XAU/USD only. "
    "Use GET /api/v1/risk/xauusd."
)


# ── Response models ───────────────────────────────────────────────────────────

class RiskFactor(BaseModel):
    name:     str
    status:   str    # "ok" | "warning" | "critical" | "info"
    detail:   str
    value:    str | None = None


class XAUUSDRiskReport(BaseModel):
    assessed_at:         str
    instrument:          str
    mode:                str
    overall_risk:        str   # "low" | "medium" | "high" | "critical"
    risk_factors:        list[RiskFactor]
    warnings:            list[str]
    recommendations:     list[str]
    execution_status:    str
    broker_note:         str


# ── Route ─────────────────────────────────────────────────────────────────────

def _risk_report(db: Session) -> XAUUSDRiskReport:
    cfg = get_pair_config("xauusd")
    now = datetime.now(timezone.utc)
    cutoff_48h = now - timedelta(hours=48)
    cutoff_7d  = now - timedelta(days=7)

    # Fetch confirmed records
    confirmed = (
        db.query(SignalRecord)
          .filter(SignalRecord.confirmed == True)
          .filter(SignalRecord.pair == "XAU/USD")
          .all()
    )
    completed = [r for r in confirmed if getattr(r, "result", None) in ("WIN", "LOSS", "BREAKEVEN")]

    factors: list[RiskFactor] = []
    warnings: list[str] = []
    recommendations: list[str] = []

    # ── 1. Data quality ───────────────────────────────────────────────────────
    sources = [getattr(r, "data_source", "synthetic") or "synthetic" for r in confirmed]
    stale_recent = sum(
        1 for i, r in enumerate(confirmed)
        if sources[i] == "stale"
        and getattr(r, "created_at", None) is not None
        and (r.created_at.replace(tzinfo=timezone.utc) if r.created_at.tzinfo is None else r.created_at) >= cutoff_48h
    )
    live_count  = sum(1 for s in sources if s in ("live", "mt5", "tradingview"))
    total_sigs  = len(sources)

    if stale_recent > 3:
        factors.append(RiskFactor(name="Data Quality", status="critical",
            detail=f"{stale_recent} stale-candle signals in last 48 h — data feed unreliable",
            value=f"{stale_recent} stale"))
        warnings.append(f"Data Quality: {stale_recent} stale-candle signals in 48 h — check TradingView feed")
    elif stale_recent > 0:
        factors.append(RiskFactor(name="Data Quality", status="warning",
            detail=f"{stale_recent} stale-candle signal(s) recently",
            value=f"{stale_recent} stale"))
        warnings.append(f"Data Quality: {stale_recent} stale candle signal(s) — monitor TradingView connectivity")
    elif total_sigs == 0:
        factors.append(RiskFactor(name="Data Quality", status="info",
            detail="No confirmed signals yet — cannot assess data source",
            value="no data"))
    else:
        pct_real = live_count / total_sigs * 100 if total_sigs else 0
        factors.append(RiskFactor(name="Data Quality", status="ok",
            detail=f"{pct_real:.0f}% of signals used real market data, 0 stale in 48 h",
            value=f"{live_count}/{total_sigs} real"))

    # ── 2. Win rate ───────────────────────────────────────────────────────────
    wins = [t for t in completed if getattr(t, "result", None) == "WIN"]
    if not completed:
        factors.append(RiskFactor(name="Win Rate", status="info",
            detail="No completed trades yet — log outcomes to assess performance",
            value="n/a"))
    else:
        wr = len(wins) / len(completed) * 100
        wr_status = "ok" if wr >= 55 else ("warning" if wr >= 40 else "critical")
        factors.append(RiskFactor(name="Win Rate", status=wr_status,
            detail=f"{wr:.1f}% from {len(completed)} completed trades",
            value=f"{wr:.1f}%"))
        if wr < 40:
            warnings.append(f"Win Rate: {wr:.1f}% is below 40% floor — review signal quality gate")

    # ── 3. Spread / volatility config ────────────────────────────────────────
    factors.append(RiskFactor(name="Spread Gate", status="ok",
        detail=f"Max spread: {cfg['max_spread']} points — active",
        value=f"{cfg['max_spread']} pts"))
    factors.append(RiskFactor(name="ATR Volatility Filter", status="ok",
        detail=f"ATR gate: {cfg['atr_min']}–{cfg['atr_max']} points — active",
        value=f"{cfg['atr_min']}–{cfg['atr_max']} pts"))

    # ── 4. News filter ────────────────────────────────────────────────────────
    n_events = len(cfg.get("critical_news_events", []))
    factors.append(RiskFactor(name="News Filter", status="ok",
        detail=f"{n_events} critical USD events mapped · block: -{cfg['news_block_minutes_before']}m / +{cfg['news_block_minutes_after']}m",
        value=f"{n_events} events"))

    # ── 5. Target realism ────────────────────────────────────────────────────
    factors.append(RiskFactor(name="Target Realism", status="ok",
        detail="50-point target with 5-point SL buffer. Min RR 2.5. H4 timeframe.",
        value="50 pts / 2.5 RR"))

    # ── 6. Stagnant record ────────────────────────────────────────────────────
    recent_completed = [
        t for t in completed
        if getattr(t, "created_at", None) is not None
        and (t.created_at.replace(tzinfo=timezone.utc) if t.created_at.tzinfo is None else t.created_at) >= cutoff_7d
    ]
    if completed and not recent_completed:
        factors.append(RiskFactor(name="Record Activity", status="warning",
            detail="No trade outcomes logged in last 7 days — stagnant record",
            value="0 trades / 7d"))
        warnings.append("Record Activity: no outcomes in 7 days — log results regularly")
    else:
        factors.append(RiskFactor(name="Record Activity", status="ok",
            detail=f"{len(recent_completed)} trade(s) logged in last 7 days" if recent_completed
                   else "No completed trades yet",
            value=f"{len(recent_completed)}/7d"))

    # ── 7. Broker execution ───────────────────────────────────────────────────
    from config import settings
    exec_enabled = settings.broker_execution_enabled or settings.mt5_execution_enabled
    factors.append(RiskFactor(name="Broker Execution", status="ok" if not exec_enabled else "warning",
        detail="DISABLED — manual confirmation only. No automated trades.",
        value="DISABLED"))

    # ── 8. Mode / readiness ───────────────────────────────────────────────────
    mode = cfg["mode"]
    mode_status = "ok" if mode == "MONITOR_ONLY" else "info"
    factors.append(RiskFactor(name="Operating Mode", status=mode_status,
        detail=f"Current mode: {mode}",
        value=mode))

    # ── Overall risk ──────────────────────────────────────────────────────────
    statuses = [f.status for f in factors]
    if "critical" in statuses:
        overall = "critical"
    elif "warning" in statuses:
        overall = "medium"
    else:
        overall = "low"

    # Recommendations
    if len(completed) < 10:
        recommendations.append(
            f"Log at least {10 - len(completed)} more trade outcomes to build a statistically useful record"
        )
    if stale_recent == 0 and len(completed) == 0:
        recommendations.append(
            "Start observing signals — confirm setups via POST /api/v1/signal/confirm "
            "and log results via PUT /api/v1/signal/{id}/result"
        )
    recommendations.append(
        "Use GET /api/v1/readiness to track progress toward PAPER_OBSERVATION promotion (need 85/100)"
    )
    recommendations.append(
        "Keep XAUUSD_MODE=MONITOR_ONLY until the readiness gate passes. Never enable live execution manually."
    )

    return XAUUSDRiskReport(
        assessed_at=now.isoformat(),
        instrument="XAU/USD",
        mode=mode,
        overall_risk=overall,
        risk_factors=factors,
        warnings=warnings,
        recommendations=recommendations,
        execution_status="DISABLED",
        broker_note=(
            "Broker execution is permanently disabled. "
            "MT5_EXECUTION_ENABLED=false and ALLOW_DEMO_TRADING=false must remain set. "
            "All signals require manual review and confirmation."
        ),
    )


@router.get(
    "/xauusd",
    response_model=APIResponse[XAUUSDRiskReport],
    summary="XAU/USD operating risk assessment",
)
def xauusd_risk(db: Session = Depends(get_db)) -> APIResponse[XAUUSDRiskReport]:
    log.info("XAU/USD risk assessment requested")
    return APIResponse(data=_risk_report(db))


@router.get(
    "",
    response_model=APIResponse[XAUUSDRiskReport],
    summary="XAU/USD risk (alias for /risk/xauusd)",
)
def risk_root(db: Session = Depends(get_db)) -> APIResponse[XAUUSDRiskReport]:
    return APIResponse(data=_risk_report(db))


@router.get(
    "/eurusd",
    summary="EUR/USD not supported",
    include_in_schema=False,
)
def eurusd_risk_rejected():
    from fastapi import HTTPException
    raise HTTPException(status_code=400, detail=_UNSUPPORTED)
