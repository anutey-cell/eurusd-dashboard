"""Atomic bridge-claim hardening.

The historical `/bridge/claim/{id}` endpoint performs SELECT-then-UPDATE and
can double-claim if two daemons race before either transaction commits.

At router import time this module:
1. removes the legacy non-atomic POST claim route;
2. re-registers `/bridge/claim/{id}` with an atomic compare-and-set UPDATE so
   existing daemons are protected immediately after backend deployment; and
3. adds `/bridge/claim-v2/{id}` for hardened daemons to make the protocol
   version explicit.

Both paths have identical safety semantics:

    UPDATE ... WHERE id=:id AND status='PENDING' AND expires_at>=now

Only one transaction can move an order to EXECUTING. Racing claimants receive
HTTP 409 and must never execute the order.
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
    """Replace legacy claim with atomic semantics and add versioned alias."""
    global _INSTALLED
    if _INSTALLED:
        return True

    router = bridge_module.router
    require_secret = bridge_module._require_bridge_secret
    serialise = bridge_module._serialise

    # Remove ONLY the legacy POST claim route. Other bridge routes are untouched.
    legacy_path = "/bridge/claim/{order_id}"
    retained = []
    removed = 0
    for route in router.routes:
        methods = set(getattr(route, "methods", set()) or set())
        if getattr(route, "path", None) == legacy_path and "POST" in methods:
            removed += 1
            continue
        retained.append(route)
    router.routes[:] = retained
    if removed != 1:
        raise RuntimeError(
            f"expected exactly one legacy bridge claim route, removed={removed}"
        )

    def _claim_atomic_impl(
        request: Request,
        order_id: int,
        bridge_daemon_id: str,
        db: Session,
    ) -> APIResponse[dict]:
        now = datetime.now(timezone.utc)

        # Single compare-and-set statement. Even concurrent daemons cannot both
        # transition the same PENDING row.
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
            log.info("[bridge/atomic] order %d claimed by %s", order_id, bridge_daemon_id)
            return APIResponse(data=serialise(row), source="mt5_bridge")

        db.rollback()
        row = db.query(PendingExecution).filter(PendingExecution.id == order_id).first()
        if row is None:
            raise HTTPException(status_code=404, detail="Order not found")

        # Close a stale PENDING row instead of leaving a zombie in the queue.
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

        raise HTTPException(
            status_code=409,
            detail=f"Order is {row.status}, not atomically claimable",
        )

    @router.post(
        "/claim/{order_id}",
        response_model=APIResponse[dict],
        summary="Atomically claim a pending order",
    )
    @limiter.limit("60/minute")
    def claim_order_atomic(
        request: Request,
        order_id: int,
        bridge_daemon_id: str = Header(default="unknown", alias="X-Bridge-Daemon-Id"),
        _: None = Depends(require_secret),
        db: Session = Depends(get_db),
    ) -> APIResponse[dict]:
        return _claim_atomic_impl(request, order_id, bridge_daemon_id, db)

    @router.post(
        "/claim-v2/{order_id}",
        response_model=APIResponse[dict],
        summary="Atomically claim a pending order (hardened bridge v2)",
    )
    @limiter.limit("60/minute")
    def claim_order_v2(
        request: Request,
        order_id: int,
        bridge_daemon_id: str = Header(default="unknown", alias="X-Bridge-Daemon-Id"),
        _: None = Depends(require_secret),
        db: Session = Depends(get_db),
    ) -> APIResponse[dict]:
        return _claim_atomic_impl(request, order_id, bridge_daemon_id, db)

    # Verify actual prefixed routes exist before claiming installation.
    paths = [getattr(r, "path", None) for r in router.routes]
    if paths.count("/bridge/claim/{order_id}") != 1:
        raise RuntimeError("atomic replacement for /bridge/claim/{order_id} not registered")
    if paths.count("/bridge/claim-v2/{order_id}") != 1:
        raise RuntimeError("/bridge/claim-v2/{order_id} not registered")

    _INSTALLED = True
    return True
