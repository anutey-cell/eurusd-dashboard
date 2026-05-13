from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from models.analytics import AnalyticsResponse, FullAnalyticsResponse
from models.analytics import SampleInfo, SessionBreakdown, EquityCurvePoint
from models.common import APIResponse
from database import get_db
from db_models import SignalRecord
from data.analytics import get_analytics
from services.analytics_engine import compute_analytics

router = APIRouter(prefix="/analytics", tags=["analytics"])


# ── Legacy mock endpoint (Phase 1–3 shape) ────────────────────────────────────

@router.get(
    "/mock",
    response_model=APIResponse[AnalyticsResponse],
    summary="Mock analytics (legacy KPI + chart format)",
    description="Returns Phase 2 mock analytics. Preserved for frontend fallback compatibility.",
)
def mock_analytics() -> APIResponse[AnalyticsResponse]:
    return APIResponse(data=get_analytics())


# ── Phase 7 DB-backed full analytics ─────────────────────────────────────────

@router.get(
    "",
    response_model=APIResponse[FullAnalyticsResponse],
    summary="Full performance analytics from stored signal history",
    description=(
        "Computes comprehensive analytics from the SQLite signal database. "
        "Falls back to the mock 20-trade history when the DB has fewer than 5 "
        "completed trades. Confidence level is indicated in sampleInfo."
    ),
)
def full_analytics(db: Session = Depends(get_db)) -> APIResponse[FullAnalyticsResponse]:
    confirmed_records = (
        db.query(SignalRecord)
          .filter(SignalRecord.confirmed == True)
          .all()
    )

    completed_records = [r for r in confirmed_records if r.result in ("WIN", "LOSS", "BREAKEVEN")]

    # Fall back to mock trade history when DB has insufficient data
    if len(completed_records) < 5:
        from data.signals import _HISTORY as mock_history
        trades_source = mock_history
        total_signals    = len(mock_history)
        confirmed_count  = len(mock_history)
    else:
        trades_source   = completed_records
        total_signals   = len(confirmed_records)
        confirmed_count = len(confirmed_records)

    result = compute_analytics(
        all_trades=trades_source,
        total_signals=total_signals,
        confirmed_trades=confirmed_count,
    )

    return APIResponse(data=FullAnalyticsResponse(
        sample_info=SampleInfo(
            size=result.sample_size,
            completed_trades=result.completed_trades,
            message=result.sample_message,
        ),
        total_signals=result.total_signals,
        confirmed_trades=result.confirmed_trades,
        completed_trades=result.completed_trades,
        win_rate=result.win_rate,
        loss_rate=result.loss_rate,
        average_win_pips=result.average_win_pips,
        average_loss_pips=result.average_loss_pips,
        average_rr=result.average_rr,
        average_pips=result.average_pips,
        expectancy=result.expectancy,
        profit_factor=result.profit_factor,
        max_drawdown=result.max_drawdown,
        consecutive_wins=result.consecutive_wins,
        consecutive_losses=result.consecutive_losses,
        long_win_rate=result.long_win_rate,
        short_win_rate=result.short_win_rate,
        news_day_win_rate=result.news_day_win_rate,
        non_news_day_win_rate=result.non_news_day_win_rate,
        best_session=result.best_session,
        worst_session=result.worst_session,
        session_breakdown=[
            SessionBreakdown(
                session=s.session, trades=s.trades, wins=s.wins,
                win_rate=s.win_rate, avg_pips=s.avg_pips,
            )
            for s in result.session_breakdown
        ],
        equity_curve=[
            EquityCurvePoint(trade=p.trade, pips=p.pips, cumulative_pips=p.cumulative_pips)
            for p in result.equity_curve
        ],
    ))
