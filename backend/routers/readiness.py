"""
Pair readiness endpoints — GET /api/v1/readiness
"""
from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy.orm import Session

from database import get_db
from db_models import SignalRecord
from models.common import APIResponse
from pair_config import SUPPORTED_PAIRS, get_pair_mode, set_pair_mode, VALID_MODES
# NOTE: get_pair_mode() and set_pair_mode() now operate on xauusd only
from services.pair_readiness import assess_pair_readiness, PairReadinessResult

router = APIRouter(prefix="/readiness", tags=["readiness"])
log = logging.getLogger(__name__)


# ── Pydantic response models ──────────────────────────────────────────────────

class DimensionOut(BaseModel):
    name:      str
    score:     int
    max_score: int
    status:    str
    detail:    str


class PairReadinessOut(BaseModel):
    pair_code:           str
    display:             str
    mode:                str
    total_score:         int
    gate_score:          int
    promotion_eligible:  bool
    dimensions:          list[DimensionOut]
    blockers:            list[str]
    warnings:            list[str]
    assessed_at:         str


class ReadinessResponse(BaseModel):
    pairs:       list[PairReadinessOut]
    gate_score:  int
    summary:     str


def _to_out(r: PairReadinessResult) -> PairReadinessOut:
    return PairReadinessOut(
        pair_code=r.pair_code,
        display=r.display,
        mode=r.mode,
        total_score=r.total_score,
        gate_score=r.gate_score,
        promotion_eligible=r.promotion_eligible,
        dimensions=[DimensionOut(**d.__dict__) for d in r.dimensions],
        blockers=r.blockers,
        warnings=r.warnings,
        assessed_at=r.assessed_at,
    )


# ── Routes ────────────────────────────────────────────────────────────────────

@router.get(
    "",
    response_model=APIResponse[ReadinessResponse],
    summary="XAU/USD readiness score and promotion eligibility",
    description=(
        "Returns the 0–100 readiness score for XAU/USD. "
        "Score ≥ 85 with no blockers required to promote above MONITOR_ONLY. "
        "Computed live from the DB — no caching."
    ),
)
def readiness_report(
    db: Session = Depends(get_db),
) -> APIResponse[ReadinessResponse]:
    results: list[PairReadinessOut] = []
    for code in SUPPORTED_PAIRS:
        cfg_display = SUPPORTED_PAIRS[code]["display"]
        records = (
            db.query(SignalRecord)
              .filter(SignalRecord.confirmed == True)
              .filter(SignalRecord.pair == cfg_display)
              .all()
        )
        assessed = assess_pair_readiness(code, records)
        results.append(_to_out(assessed))

    eligible_count = sum(1 for r in results if r.promotion_eligible)
    summary = (
        f"{eligible_count}/{len(results)} pairs eligible for mode promotion "
        f"(gate: {results[0].gate_score if results else 85}/100)"
    )

    log.info("Readiness assessed: %s", summary)
    return APIResponse(data=ReadinessResponse(
        pairs=results,
        gate_score=85,
        summary=summary,
    ))


class ModeUpdateBody(BaseModel):
    pair_code: str
    mode:      str


@router.post(
    "/mode",
    response_model=APIResponse[dict],
    summary="Update operating mode for a pair",
    description=(
        "Promote or demote a pair's operating mode at runtime. "
        "Valid modes: MONITOR_ONLY | PAPER_OBSERVATION | DEMO_ELIGIBLE | LIVE_READ_ONLY | DISABLED. "
        "Changes are in-memory only and revert on server restart. "
        "Persist by setting EURUSD_MODE / XAUUSD_MODE env vars."
    ),
)
def update_mode(body: ModeUpdateBody) -> APIResponse[dict]:
    try:
        # Only xauusd is supported
        if body.pair_code.lower().replace("/", "").replace("_", "") != "xauusd":
            from fastapi import HTTPException
            raise HTTPException(
                status_code=400,
                detail="Only XAU/USD (xauusd) is supported by this dashboard.",
            )
        set_pair_mode(body.mode)
        log.info("Mode updated instrument=XAU/USD mode=%s", body.mode.upper())
        return APIResponse(data={
            "pair_code":  "xauusd",
            "instrument": "XAU/USD",
            "mode":       body.mode.upper(),
            "message":    f"XAU/USD mode set to {body.mode.upper()}. "
                          "Set XAUUSD_MODE env var to persist across restarts.",
            "valid_modes": sorted(VALID_MODES),
        })
    except ValueError as exc:
        from fastapi import HTTPException
        raise HTTPException(status_code=400, detail=str(exc)) from exc
