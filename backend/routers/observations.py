"""
Paper Observation API.

Endpoints
---------
GET    /api/v1/observations             list observations (newest first)
GET    /api/v1/observations/stats       aggregated metrics + readiness verdict
POST   /api/v1/observations/resolve     forward-resolve pending observations
DELETE /api/v1/observations/{id}        purge an observation row (admin)
DELETE /api/v1/observations             purge ALL observations (with confirm)

Safety:
  - No live execution
  - No Telegram alerts
  - No MT5 order placement
  - Read-only against historical candles
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy.orm import Session

from database import get_db
from db_models import PaperObservation
from models.common import APIResponse
from rate_limit import limiter
from services.paper_observation_tracker import (
    get_observation_stats, resolve_pending, serialise_observation,
)

router = APIRouter(prefix="/observations", tags=["observations"])
log = logging.getLogger(__name__)


@router.get(
    "",
    response_model=APIResponse[dict],
    summary="List paper observations (newest first)",
)
@limiter.limit("30/minute")
def list_observations(
    request: Request,
    limit:    int = Query(default=50, ge=1, le=500),
    resolved: str = Query(default="all", description="all | resolved | pending"),
    engine_id: str | None = Query(default=None, description="Filter by engine: swing | trend_pullback"),
    db:       Session = Depends(get_db),
) -> APIResponse[dict]:
    q = db.query(PaperObservation).order_by(PaperObservation.observed_at.desc())
    if resolved == "resolved":
        q = q.filter(PaperObservation.result.isnot(None))
    elif resolved == "pending":
        q = q.filter(PaperObservation.result.is_(None))
    if engine_id:
        q = q.filter(PaperObservation.engine_id == engine_id)

    rows = q.limit(limit).all()
    return APIResponse(data={
        "total":        len(rows),
        "filter":       resolved,
        "engineId":     engine_id,
        "observations": [serialise_observation(r) for r in rows],
    })


@router.get(
    "/stats",
    response_model=APIResponse[dict],
    summary="Aggregated paper observation metrics + readiness verdict",
)
@limiter.limit("30/minute")
def observation_stats(
    request: Request,
    engine_id: str | None = Query(default=None, description="Filter by engine: swing | trend_pullback"),
    db: Session = Depends(get_db),
) -> APIResponse[dict]:
    return APIResponse(data=get_observation_stats(db, engine_id=engine_id))


@router.get(
    "/compare",
    response_model=APIResponse[dict],
    summary="Side-by-side comparison of two engines' paper observations",
)
@limiter.limit("30/minute")
def compare_engines(
    request: Request,
    engine_a: str = Query(default="swing"),
    engine_b: str = Query(default="trend_pullback"),
    db: Session = Depends(get_db),
) -> APIResponse[dict]:
    stats_a = get_observation_stats(db, engine_id=engine_a)
    stats_b = get_observation_stats(db, engine_id=engine_b)
    # Determine which engine is currently ahead
    leader = None
    if stats_a["resolved"] >= 5 and stats_b["resolved"] >= 5:
        if stats_a["expectancyR"] > stats_b["expectancyR"]:
            leader = engine_a
        elif stats_b["expectancyR"] > stats_a["expectancyR"]:
            leader = engine_b
    return APIResponse(data={
        "engineA":      engine_a,
        "engineB":      engine_b,
        "statsA":       stats_a,
        "statsB":       stats_b,
        "leader":       leader,
        "leaderMargin": abs(stats_a["expectancyR"] - stats_b["expectancyR"]),
    })


@router.post(
    "/run-dual-engines",
    response_model=APIResponse[dict],
    summary="Trigger dual-engine paper observation logging (swing + trend_pullback)",
    description=(
        "Runs both the swing-H1 ICT scanner AND the trend-pullback H1 "
        "strategy against current data. Logs any qualifying signals to "
        "paper_observations tagged with their engine_id. Rate-limited to 6/min."
    ),
)
@limiter.limit("6/minute")
def run_dual_engines_endpoint(
    request: Request,
    db: Session = Depends(get_db),
) -> APIResponse[dict]:
    from services.dual_engine_runner import run_dual_engines
    try:
        return APIResponse(data=run_dual_engines(db))
    except Exception as exc:
        log.exception("[observations] dual engine run failed")
        raise HTTPException(status_code=502, detail=f"Dual engine error: {exc}") from exc


@router.post(
    "/resolve",
    response_model=APIResponse[dict],
    summary="Forward-resolve pending observations against historical candles",
    description=(
        "Walks unresolved observations forward through H4 historical candles "
        "to determine WIN / LOSS / EXPIRED. Conservative collision rule: "
        "if a single candle touches both TP and SL, count as LOSS. "
        "Rate-limited to 4/minute."
    ),
)
@limiter.limit("4/minute")
def resolve_observations(
    request: Request,
    max_observations: int = Query(default=100, ge=1, le=500),
    db:       Session = Depends(get_db),
) -> APIResponse[dict]:
    try:
        result = resolve_pending(db, max_observations=max_observations)
        return APIResponse(data=result)
    except Exception as exc:
        log.exception("[observations] resolve failed")
        raise HTTPException(status_code=502, detail=f"Resolve error: {exc}") from exc


@router.post(
    "/backfill-from-backtest",
    response_model=APIResponse[dict],
    summary="Backfill observations from a saved backtest run",
    description=(
        "Imports every trade from a saved backtest run as a paper observation "
        "(already resolved with its known outcome). Use this to bootstrap the "
        "observation tally with real-data backtest evidence before live observation "
        "begins. Rate-limited to 2/minute."
    ),
)
@limiter.limit("2/minute")
def backfill_from_backtest(
    request: Request,
    run_id:  int   = Query(..., description="backtest_runs.id to import from"),
    db:      Session = Depends(get_db),
) -> APIResponse[dict]:
    from db_models import BacktestRun
    import json
    import hashlib
    from datetime import datetime, timezone

    run = db.get(BacktestRun, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail=f"Backtest run {run_id} not found")

    trades = json.loads(run.trades_json or "[]")
    if not trades:
        return APIResponse(data={"imported": 0, "skipped": 0,
                                  "note": "Run has no trades"})

    imported = 0
    duplicates = 0
    for t in trades:
        # Build fingerprint
        raw = "{}:{}:{:.2f}:{:.2f}:{:.2f}:{}".format(
            t.get("signal", ""), t.get("signal", ""),
            float(t.get("adjustedEntry", 0)),
            float(t.get("stopLoss", 0)),
            float(t.get("takeProfit", 0)),
            t.get("score", 0),
        )
        fingerprint = hashlib.sha256(raw.encode()).hexdigest()[:16]

        # Dedupe by fingerprint
        if db.query(PaperObservation).filter(
            PaperObservation.fingerprint == fingerprint
        ).first():
            duplicates += 1
            continue

        # Parse times
        obs_t = t.get("entryTime")
        exit_t = t.get("exitTime")
        try:
            obs_dt = datetime.fromisoformat(str(obs_t).replace("Z", "+00:00"))
            if obs_dt.tzinfo is None: obs_dt = obs_dt.replace(tzinfo=timezone.utc)
        except Exception:
            continue
        try:
            exit_dt = datetime.fromisoformat(str(exit_t).replace("Z", "+00:00"))
            if exit_dt.tzinfo is None: exit_dt = exit_dt.replace(tzinfo=timezone.utc)
        except Exception:
            exit_dt = None

        row = PaperObservation(
            observed_at=obs_dt,
            instrument=run.instrument,
            timeframe=run.timeframe,
            signal=t.get("signal", ""),
            entry=t.get("adjustedEntry") or t.get("entry") or 0.0,
            stop_loss=t.get("stopLoss") or 0.0,
            take_profit=t.get("takeProfit") or 0.0,
            risk_points=t.get("riskPoints"),
            target_points=t.get("targetPoints"),
            rr=t.get("rr"),
            score=t.get("score"),
            session=t.get("session"),
            setup_type=t.get("setupType", "unknown"),
            market_state="SIGNAL_READY",
            confidence=t.get("score"),
            grade=None,
            fingerprint=fingerprint,
            # Already resolved from backtest
            resolved_at=exit_dt or datetime.now(timezone.utc),
            result=t.get("result"),
            exit_price=None,    # backtest result doesn't always include exit price
            points_captured=t.get("points"),
            r_multiple=t.get("rMultiple"),
            bars_held=t.get("barsHeld"),
            engine_model_json=json.dumps({"backfilled_from_run": run_id}),
        )
        db.add(row)
        imported += 1

    db.commit()
    return APIResponse(data={
        "runId":      run_id,
        "imported":   imported,
        "duplicates": duplicates,
        "totalInRun": len(trades),
    })


@router.get(
    "/equity-curve",
    response_model=APIResponse[dict],
    summary="Compute paper equity curve for a given engine",
    description=(
        "Walks resolved paper observations chronologically and produces "
        "the running equity curve with peak / drawdown at each point. "
        "Used by the dashboard equity chart."
    ),
)
@limiter.limit("30/minute")
def equity_curve(
    request: Request,
    engine_id:     str   = Query(default="swing"),
    initial_equity: float = Query(default=10000.0, gt=0),
    risk_percent:  float = Query(default=0.25, gt=0, le=5),
    db: Session = Depends(get_db),
) -> APIResponse[dict]:
    from services.equity_tracker import compute_equity_curve
    return APIResponse(data=compute_equity_curve(
        db, engine_id=engine_id,
        initial_equity=initial_equity,
        risk_percent=risk_percent,
    ))


@router.post(
    "/check-drawdown",
    response_model=APIResponse[dict],
    summary="Check drawdown for an engine and fire Telegram alert if breached",
)
@limiter.limit("10/minute")
def check_drawdown(
    request: Request,
    engine_id: str = Query(default="swing"),
    db: Session = Depends(get_db),
) -> APIResponse[dict]:
    from services.equity_tracker import check_drawdown_alert
    return APIResponse(data=check_drawdown_alert(db, engine_id=engine_id))


@router.delete(
    "/{obs_id}",
    response_model=APIResponse[dict],
    summary="Delete a single paper observation",
)
@limiter.limit("10/minute")
def delete_observation(
    request: Request,
    obs_id:  int,
    db:      Session = Depends(get_db),
) -> APIResponse[dict]:
    row = db.get(PaperObservation, obs_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"Observation {obs_id} not found")
    db.delete(row)
    db.commit()
    return APIResponse(data={"deleted": True, "id": obs_id})


@router.delete(
    "",
    response_model=APIResponse[dict],
    summary="Purge ALL observations (requires confirm=true)",
)
@limiter.limit("2/minute")
def purge_observations(
    request: Request,
    confirm: bool = Query(default=False),
    db:      Session = Depends(get_db),
) -> APIResponse[dict]:
    if not confirm:
        raise HTTPException(status_code=400, detail="Pass confirm=true to purge all observations")
    n = db.query(PaperObservation).delete(synchronize_session=False)
    db.commit()
    return APIResponse(data={"deleted": n})
