import logging
from typing import Literal

from fastapi import APIRouter, Body, Depends, HTTPException, Query, Path, Request
from sqlalchemy.orm import Session

from exceptions import ProviderError
from models.signal import (
    SignalOutput, SignalHistoryResponse, SignalAnalysisOutput,
)
from models.common import APIResponse
from data.signals import (
    get_current_signal, get_signal_history, run_signal_analysis,
)
from database import get_db
from db_models import SignalRecord
from schemas import (
    SignalCreate, SignalRead, SignalUpdateResult,
    AnalyticsRead, SignalHistoryDB,
)
from rate_limit import limiter

router = APIRouter(prefix="/signal", tags=["signal"])
logger = logging.getLogger(__name__)


# ── Mock / static routes ──────────────────────────────────────────────────────

@router.get(
    "/current",
    response_model=APIResponse[SignalOutput],
    summary="Current EUR/USD signal + trade plan (mock)",
)
def current_signal() -> APIResponse[SignalOutput]:
    return APIResponse(data=get_current_signal())


@router.get(
    "/history",
    response_model=APIResponse[SignalHistoryResponse],
    summary="Historical signal trades — mock data (paginated)",
)
def signal_history(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    direction: Literal["BUY", "SELL"] | None = Query(default=None),
    result: Literal["WIN", "LOSS", "BREAKEVEN", "PENDING"] | None = Query(default=None),
) -> APIResponse[SignalHistoryResponse]:
    data = get_signal_history(page=page, page_size=page_size,
                              direction=direction, result=result)
    return APIResponse(data=data)


# ── Engine route ──────────────────────────────────────────────────────────────

@router.post(
    "/analyze",
    response_model=APIResponse[SignalAnalysisOutput],
    summary="Run ICT signal engine on live/demo candles",
    description=(
        "Executes the full ICT/SMC pipeline: HTF bias, liquidity sweep, "
        "market structure (BOS/CHoCH), FVG, news risk, and session timing. "
        "Rate-limited to 10 requests/minute per IP."
    ),
)
@limiter.limit("10/minute")
def analyze(
    request: Request,
    macro_events: list[dict] = Body(default=[], description="Optional macro calendar events"),
    pair: str = Query(default="eurusd", description="Trading pair code: eurusd | xauusd"),
    db: Session = Depends(get_db),
) -> APIResponse[SignalAnalysisOutput]:
    logger.info("Signal analysis requested pair=%s ip=%s", pair, request.client.host if request.client else "unknown")
    try:
        from pair_config import validate_pair as _validate_pair
        pair = _validate_pair(pair)
        result = run_signal_analysis(macro_events, pair=pair, db=db)
        logger.info(
            "Signal analyzed signal=%s quality=%s session=%s",
            getattr(result, "signal", "?"),
            getattr(result, "quality_score", "?"),
            getattr(result, "session", "?"),
        )

        # ── Telegram alert (fire-and-forget — never breaks signal response) ───
        alert_status: str = "disabled"
        try:
            from services.telegram_alert_service import send_signal_alert as _tg_send
            # Build camelCase payload matching what the service expects
            payload_dict = result.model_dump(by_alias=True)
            alert_status = _tg_send(payload_dict)
        except Exception as _tg_exc:
            logger.debug("Telegram alert (non-fatal): %s", _tg_exc)
            alert_status = "failed"

        result.alert_status = alert_status
        return APIResponse(data=result)
    except ProviderError as exc:
        logger.warning("Provider error in signal analysis: %s", exc)
        raise HTTPException(status_code=exc.http_code, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception("Unexpected error in signal analysis")
        raise HTTPException(status_code=502, detail=f"Signal analysis error: {exc}") from exc


# ── GET convenience alias ─────────────────────────────────────────────────────

@router.get(
    "/{pair}",
    response_model=APIResponse[SignalAnalysisOutput],
    summary="Run ICT signal engine via GET (pair in URL path)",
    description=(
        "Convenience alias for POST /analyze — runs the full ICT/SMC pipeline "
        "for the specified pair. No request body required. "
        "Valid values: eurusd, xauusd."
    ),
)
@limiter.limit("10/minute")
def analyze_get(
    pair: str = Path(description="Trading pair: eurusd | xauusd"),
    request: Request = ...,
    db: Session = Depends(get_db),
) -> APIResponse[SignalAnalysisOutput]:
    """GET wrapper so callers can hit /api/v1/signal/eurusd without a body."""
    logger.info("Signal GET analysis pair=%s ip=%s", pair, request.client.host if request.client else "unknown")
    try:
        from pair_config import validate_pair as _validate_pair
        pair = _validate_pair(pair)
        result = run_signal_analysis([], pair=pair, db=db)
        logger.info(
            "Signal GET analyzed signal=%s quality=%s session=%s",
            getattr(result, "signal", "?"),
            getattr(result, "quality_score", "?"),
            getattr(result, "session", "?"),
        )

        alert_status: str = "disabled"
        try:
            from services.telegram_alert_service import send_signal_alert as _tg_send
            payload_dict = result.model_dump(by_alias=True)
            alert_status = _tg_send(payload_dict)
        except Exception as _tg_exc:
            logger.debug("Telegram alert (non-fatal): %s", _tg_exc)
            alert_status = "failed"

        result.alert_status = alert_status
        return APIResponse(data=result)
    except ProviderError as exc:
        logger.warning("Provider error in GET signal analysis: %s", exc)
        raise HTTPException(status_code=exc.http_code, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception("Unexpected error in GET signal analysis")
        raise HTTPException(status_code=502, detail=f"Signal analysis error: {exc}") from exc


# ── DB-backed routes ──────────────────────────────────────────────────────────

@router.post(
    "/confirm",
    response_model=APIResponse[SignalRead],
    summary="Confirm a signal and save it to the database",
)
def confirm_and_save(
    body: SignalCreate,
    db: Session = Depends(get_db),
) -> APIResponse[SignalRead]:
    # Gate: reject confirmation when pair is in MONITOR_ONLY or DISABLED mode
    from pair_config import get_pair_mode as _get_mode, get_pair_config as _get_cfg
    try:
        _pair_code = body.pair.lower().replace("/", "").replace("_", "").replace(" ", "")
        # Map display name to code if needed
        try:
            _pair_code = _get_cfg(_pair_code)["code"]
        except Exception:
            pass
        _mode = _get_mode(_pair_code)
        if _mode in ("MONITOR_ONLY", "DISABLED"):
            raise HTTPException(
                status_code=403,
                detail={
                    "error": True,
                    "message": f"{body.pair} is in {_mode} mode — signal confirmation is not allowed.",
                    "pair_mode": _mode,
                    "hint": "Update mode via POST /api/v1/readiness/mode to PAPER_OBSERVATION or higher.",
                },
            )
    except HTTPException:
        raise
    except Exception as _mode_exc:
        logger.debug("Mode check (non-fatal): %s", _mode_exc)

    record = SignalRecord(
        pair=body.pair,
        timeframe=body.timeframe,
        signal=body.signal,
        quality_score=body.quality_score,
        entry=body.entry,
        stop_loss=body.stop_loss,
        take_profit=body.take_profit,
        risk_pips=body.risk_pips,
        target_pips=body.target_pips,
        rr=body.rr,
        invalidation=body.invalidation,
        liquidity_status=body.liquidity_status,
        fvg_status=body.fvg_status,
        structure_status=body.structure_status,
        macro_status=body.macro_status,
        news_status=body.news_status,
        session=body.session,
        reason=body.reason,
        confirmed=True,
    )
    db.add(record)
    db.commit()
    db.refresh(record)
    logger.info(
        "Signal confirmed id=%s pair=%s signal=%s quality=%s",
        record.id, body.pair, body.signal, body.quality_score,
    )

    # Fire Telegram confirmation alert (fire-and-forget)
    try:
        from services.telegram_service import send_signal_confirmed
        send_signal_confirmed(
            pair=body.pair,
            signal=body.signal,
            signal_id=record.id,
            score=body.quality_score,
            entry=body.entry,
            stop_loss=body.stop_loss,
            take_profit=body.take_profit,
            rr=body.rr,
            session=body.session,
            timeframe=body.timeframe,
        )
    except Exception as _tg_exc:
        logger.debug("Telegram confirm alert (non-fatal): %s", _tg_exc)

    return APIResponse(data=SignalRead.model_validate(record))


@router.get(
    "/db/history",
    response_model=APIResponse[SignalHistoryDB],
    summary="Signal history from database (newest first, pair-filtered)",
)
def db_signal_history(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    signal: Literal["BUY", "SELL", "WAIT"] | None = Query(default=None),
    result: Literal["WIN", "LOSS", "BREAKEVEN"] | None = Query(default=None),
    pair: str | None = Query(default=None, description="Filter by pair code: eurusd | xauusd"),
    db: Session = Depends(get_db),
) -> APIResponse[SignalHistoryDB]:
    q = db.query(SignalRecord).filter(SignalRecord.confirmed == True)
    if signal:
        q = q.filter(SignalRecord.signal == signal)
    if result:
        q = q.filter(SignalRecord.result == result)
    if pair:
        from pair_config import get_pair_config as _get_cfg
        try:
            pair_display = _get_cfg(pair)["display"]
            q = q.filter(SignalRecord.pair == pair_display)
        except ValueError:
            pass  # unknown pair code — return all

    total = q.count()
    records = (
        q.order_by(SignalRecord.created_at.desc())
         .offset((page - 1) * page_size)
         .limit(page_size)
         .all()
    )
    return APIResponse(data=SignalHistoryDB(
        total=total,
        page=page,
        page_size=page_size,
        signals=[SignalRead.model_validate(r) for r in records],
    ))


@router.put(
    "/{signal_id}/result",
    response_model=APIResponse[SignalRead],
    summary="Record the final result of a confirmed signal",
)
def record_result(
    signal_id: int = Path(description="DB signal ID returned by /confirm"),
    body: SignalUpdateResult = ...,
    db: Session = Depends(get_db),
) -> APIResponse[SignalRead]:
    record: SignalRecord | None = db.get(SignalRecord, signal_id)
    if record is None:
        raise HTTPException(status_code=404, detail=f"Signal {signal_id} not found")
    if not record.confirmed:
        raise HTTPException(status_code=409, detail="Signal has not been confirmed yet")

    record.result     = body.result
    record.exit_price = body.exit_price
    record.pips       = body.pips
    record.pnl        = body.pnl
    record.notes      = body.notes
    db.commit()
    db.refresh(record)
    logger.info(
        "Signal result recorded id=%s result=%s pips=%s",
        signal_id, body.result, body.pips,
    )

    # ── Trigger adaptive learning cycle (non-blocking, best-effort) ───────
    try:
        from services.adaptive_engine import run_learning_cycle
        updated = run_learning_cycle(db, pair="all")
        logger.info(
            "Adaptive learning updated maturity=%d%% win_rate=%.0f%% outcomes=%d",
            updated.maturity_score, updated.overall_win_rate * 100, updated.n_outcomes,
        )
    except Exception as _learn_exc:
        logger.debug("Adaptive learning (non-fatal): %s", _learn_exc)

    # Fire Telegram result alert (fire-and-forget)
    try:
        from services.telegram_service import send_result_logged
        send_result_logged(
            pair=record.pair,
            signal=record.signal,
            signal_id=signal_id,
            result=body.result,
            pips=body.pips,
            pnl=body.pnl,
        )
    except Exception as _tg_exc:
        logger.debug("Telegram result alert (non-fatal): %s", _tg_exc)

    return APIResponse(data=SignalRead.model_validate(record))


@router.get(
    "/db/analytics",
    response_model=APIResponse[AnalyticsRead],
    summary="Compute analytics from stored signal records",
)
def db_analytics(db: Session = Depends(get_db)) -> APIResponse[AnalyticsRead]:
    confirmed = db.query(SignalRecord).filter(SignalRecord.confirmed == True).all()

    trades      = [r for r in confirmed if r.result is not None]
    wins        = [r for r in trades if r.result == "WIN"]
    losses      = [r for r in trades if r.result == "LOSS"]
    breakevens  = [r for r in trades if r.result == "BREAKEVEN"]
    pending     = [r for r in confirmed if r.result is None]
    total_pips  = sum(r.pips or 0 for r in trades)

    rr_values   = [r.rr for r in confirmed if r.rr is not None]
    qs_values   = [r.quality_score for r in confirmed]

    return APIResponse(data=AnalyticsRead(
        total_confirmed=len(confirmed),
        total_trades=len(trades),
        pending=len(pending),
        wins=len(wins),
        losses=len(losses),
        breakevens=len(breakevens),
        win_rate=round(len(wins) / len(trades) * 100, 1) if trades else 0.0,
        total_pips=total_pips,
        avg_rr=round(sum(rr_values) / len(rr_values), 2) if rr_values else None,
        avg_quality_score=round(sum(qs_values) / len(qs_values), 1) if qs_values else None,
    ))
