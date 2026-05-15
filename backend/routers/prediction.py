"""
High-Probability Prediction endpoint.

GET /api/v1/prediction/xauusd
  Returns a multi-layer prediction with probability score, direction,
  factor breakdown, and (when actionable) a suggested trade plan.

This is a DECISION SUPPORT tool. The user reads the prediction, decides
to execute or skip — no automatic trade placement.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from database import get_db
from models.common import APIResponse
from rate_limit import limiter

router = APIRouter(prefix="/prediction", tags=["prediction"])
log = logging.getLogger(__name__)


@router.get(
    "/xauusd",
    response_model=APIResponse[dict],
    summary="High-probability XAU/USD prediction (5-layer confluence)",
    description=(
        "Combines technical (scanner), fundamental (DXY+yields), news, "
        "volatility regime, and sentiment into a single probability score "
        "with explicit factor breakdown. Designed as decision support — "
        "the user reviews and decides whether to execute manually. "
        "Rate-limited to 30/minute."
    ),
)
@limiter.limit("30/minute")
def predict(
    request: Request,
    db: Session = Depends(get_db),
) -> APIResponse[dict]:
    try:
        from services.high_probability_predictor import predict_xauusd, prediction_to_dict
        pred = predict_xauusd(db=db)
        return APIResponse(data=prediction_to_dict(pred))
    except Exception as exc:
        log.exception("[prediction] failed")
        raise HTTPException(status_code=502, detail=f"Prediction error: {exc}") from exc
