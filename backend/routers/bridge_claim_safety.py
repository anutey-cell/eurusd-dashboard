"""Atomic bridge-claim endpoint used by the hardened Windows daemon.

The historical `/bridge/claim/{id}` endpoint performs SELECT-then-UPDATE and
can double-claim if two daemons race before either transaction commits. This
module adds `/bridge/claim-v2/{id}` using a single conditional UPDATE:

    WHERE id=:id AND status='PENDING' AND expires_at>=now

Only one transaction can change the row from PENDING to EXECUTING, so every
other claimant receives HTTP 409 and must not execute the order.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from fastapi import Depends, Header, HTTPException, Request
from sqlalchemy.orm import Session

from database import get_db
from db_models import PendingExecution
from models.common import APIResponse
from rate_limit import limiter

log = logging.getLogger(__name__)
_INSTALLED = False


def install_atomic_claim_route(bridge_module) -> bool:
    """Register the v2 route exactly once on the existing bridge router."""
    global _INSTALLED
    if _INSTALLED:
        return True

    router = bridge_module.router
    require_secret = bridge_module._require_bridge_secret
    serialise = bridge_module._serialise

    @router.post(
        "/claim-v2/{order_id}",
        response_model=APIResponse[dict],
        summary="Atomically claim a pending order (hardened bridge)",
    )
    @limiter.limit("60/minute")
    def claim_order_v2(
        request: Request,
        order_id: int,
        bridge_daemon_id: str = Header(default="unknown", alias="X-Bridge-Daemon-Id"),
        _: None = Depends(require_secret),
        db: Session = Depends(get_db),
    ) -> APIResponse[dict]:
        now = datetime.now(timezone.utc)

        # Single compare-and-set statement. This is the safety property: even
        # concurrent daemons cannot both transition the same PENDING row.
        updated = (
            db.query(PendingExecution)
            .filter(PendingExecution.id == order_id)
            .filter(PendingExecution.status == "PENDING")
            .filter(PendingExecution.expires_at >= now)
            .update(
                {
                    PendingExecution.status: "EXECUTING",
                    PendingExecution.claimed_at: now,
                    PendingExecution.claimed_by: bridge_daemon_id,
                },
                synchronize_session=False,
            )
        )

        if updated == 1:
            db.commit()
            row = db.query(PendingExecution).filter(PendingExecution.id == order_id).first()
            log.info("[bridge/v2] order %d atomically claimed by %s", order_id, bridge_daemon_id)
            return APIResponse(data=serialise(row), source="mt5_bridge")

        # Nothing changed. Roll back the no-op transaction before diagnosing.
        db.rollback()
        row = db.query(PendingExecution).filter(PendingExecution.id == order_id).first()
        if row is None:
            raise HTTPException(status_code=404, detail="Order not found")

        # Expired but still PENDING: close it rather than leaving a zombie row.
        if row.status == "PENDING" and row.expires_at and row.expires_at < now:
            expired = (
                db.query(PendingExecution)
                .filter(PendingExecution.id == order_id)
                .filter(PendingExecution.status == "PENDING")
                .update(
                    {
                        PendingExecution.status: "EXPIRED",
                        PendingExecution.resolved_at: now,
                        PendingExecution.execution_error: "Claim refused: order TTL expired",
                    },
                    synchronize_session=False,
                )
            )
            if expired:
                db.commit()
            else:
                db.rollback()
            raise HTTPException(status_code=409, detail="Order expired before claim")

        # Most commonly another daemon won the race and status is EXECUTING.
        raise HTTPException(
            status_code=409,
            detail=f"Order is {row.status}, not atomically claimable",
        )

    _INSTALLED = True
    return True
