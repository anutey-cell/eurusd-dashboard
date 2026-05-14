"""
Institutional XAU/USD scanner endpoints.

GET  /api/v1/scan/xauusd           — run or return cached scan
POST /api/v1/scan/xauusd/force     — force fresh scan (user-triggered)
GET  /api/v1/scan/history          — recent scan history (DB)
GET  /api/v1/market-view/xauusd    — latest stored market view
GET  /api/v1/scan/status           — scanner health / cache state

Rate limits:
  GET  /scan/xauusd        — 30/minute per IP  (cached, cheap)
  POST /scan/xauusd/force  — 6/minute  per IP  (forced fresh analysis)
"""
from __future__ import annotations

import json
import logging

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from database import get_db
from db_models import InstitutionalScan
from models.common import APIResponse
from rate_limit import limiter
from services.institutional_scanner import scan_xauusd_market, get_scan_status

router = APIRouter(tags=["scanner"])
log = logging.getLogger(__name__)


# ── GET /scan/xauusd ──────────────────────────────────────────────────────────

@router.get(
    "/scan/xauusd",
    response_model=APIResponse[dict],
    summary="XAU/USD institutional scan (cached)",
    description=(
        "Returns the latest institutional market view for XAU/USD. "
        "Result is cached for SCAN_CACHE_TTL_SECONDS (default 45 s). "
        "Use POST /scan/xauusd/force to bypass the cache."
    ),
)
@limiter.limit("30/minute")
def scan_xauusd(
    request: Request,
    db: Session = Depends(get_db),
) -> APIResponse[dict]:
    try:
        result = scan_xauusd_market(force_refresh=False, db=db)
        log.info(
            "[scan] GET xauusd market_state=%s signal=%s score=%s",
            result.get("marketState"), result.get("signal"), result.get("qualityScore"),
        )
        return APIResponse(data=result)
    except Exception as exc:
        log.exception("[scan] GET xauusd failed")
        raise HTTPException(status_code=502, detail=f"Scanner error: {exc}") from exc


# ── POST /scan/xauusd/force ────────────────────────────────────────────────────

@router.post(
    "/scan/xauusd/force",
    response_model=APIResponse[dict],
    summary="Force fresh XAU/USD institutional scan",
    description=(
        "Bypasses the cache and runs a fresh full analysis: multi-timeframe "
        "bias, liquidity, structure, FVG, news, session, and signal engine. "
        "Triggered by the 'Scan Institutional Setup' button. "
        "Rate-limited to 6 requests/minute per IP."
    ),
)
@limiter.limit("6/minute")
def force_scan_xauusd(
    request: Request,
    db: Session = Depends(get_db),
) -> APIResponse[dict]:
    ip = request.client.host if request.client else "unknown"
    log.info("[scan] POST force scan requested ip=%s", ip)
    try:
        result = scan_xauusd_market(force_refresh=True, db=db)
        log.info(
            "[scan] Force scan complete market_state=%s signal=%s score=%s ip=%s",
            result.get("marketState"), result.get("signal"),
            result.get("qualityScore"), ip,
        )
        return APIResponse(data=result)
    except Exception as exc:
        log.exception("[scan] Force scan failed ip=%s", ip)
        raise HTTPException(status_code=502, detail=f"Force scan error: {exc}") from exc


# ── GET /scan/history ─────────────────────────────────────────────────────────

@router.get(
    "/scan/history",
    response_model=APIResponse[dict],
    summary="Recent scan history from database",
    description="Returns the 20 most recent institutional scan records.",
)
@limiter.limit("20/minute")
def scan_history(
    request: Request,
    limit: int = 20,
    db: Session = Depends(get_db),
) -> APIResponse[dict]:
    limit = max(1, min(limit, 100))
    records = (
        db.query(InstitutionalScan)
          .order_by(InstitutionalScan.created_at.desc())
          .limit(limit)
          .all()
    )

    def _row(r: InstitutionalScan) -> dict:
        return {
            "id":                r.id,
            "createdAt":         r.created_at.isoformat() if r.created_at else None,
            "instrument":        r.instrument,
            "scanMode":          r.scan_mode,
            "marketState":       r.market_state,
            "signal":            r.signal,
            "institutionalBias": r.institutional_bias,
            "confidence":        r.confidence,
            "readiness":         r.readiness,
            "summary":           r.summary,
            "action":            r.action,
            "keyDrivers":        _safe_json(r.key_drivers_json, []),
            "blockers":          _safe_json(r.blockers_json, []),
            "opportunity":       _safe_json(r.opportunity_json, {}),
            "model":             _safe_json(r.model_json, {}),
            "liquidity":         _safe_json(r.liquidity_json, {}),
            "fvg":               _safe_json(r.fvg_json, {}),
            "news":              _safe_json(r.news_json, {}),
            "risk":              _safe_json(r.risk_json, {}),
        }

    return APIResponse(data={
        "total":   len(records),
        "limit":   limit,
        "scans":   [_row(r) for r in records],
    })


# ── GET /market-view/xauusd ───────────────────────────────────────────────────

@router.get(
    "/market-view/xauusd",
    response_model=APIResponse[dict],
    summary="Latest stored market view for XAU/USD",
    description="Returns the most recently stored institutional market view from the database.",
)
@limiter.limit("30/minute")
def market_view_xauusd(
    request: Request,
    db: Session = Depends(get_db),
) -> APIResponse[dict]:
    record = (
        db.query(InstitutionalScan)
          .filter(InstitutionalScan.instrument == "XAU/USD")
          .order_by(InstitutionalScan.created_at.desc())
          .first()
    )
    if record is None:
        # Return live scan if no DB record yet
        try:
            result = scan_xauusd_market(force_refresh=False, db=db)
            return APIResponse(data=result)
        except Exception as exc:
            raise HTTPException(status_code=404, detail="No market view available yet.") from exc

    payload = _safe_json(record.raw_payload_json, {})
    if not payload:
        # Reconstruct from individual columns
        payload = {
            "instrument":        record.instrument,
            "timestamp":         record.created_at.isoformat() if record.created_at else None,
            "marketState":       record.market_state,
            "signal":            record.signal,
            "institutionalBias": record.institutional_bias,
            "confidence":        record.confidence,
            "readiness":         record.readiness,
            "summary":           record.summary,
            "action":            record.action,
        }
    return APIResponse(data=payload)


# ── GET /scan/status ──────────────────────────────────────────────────────────

@router.get(
    "/scan/status",
    response_model=APIResponse[dict],
    summary="Institutional scanner health and cache status",
)
@limiter.limit("60/minute")
def scan_status(request: Request) -> APIResponse[dict]:
    return APIResponse(data=get_scan_status())


# ── Utility ───────────────────────────────────────────────────────────────────

def _safe_json(raw: str | None, default):
    if not raw:
        return default
    try:
        return json.loads(raw)
    except Exception:
        return default
