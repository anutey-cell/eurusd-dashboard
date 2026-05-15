"""
Backtesting and optimization endpoints.

XAU/USD strict backtest (PRIMARY):
  GET  /api/v1/backtest/xauusd              — full strict backtest
  POST /api/v1/backtest/import-csv          — upload historical candles
  GET  /api/v1/backtest/data-status         — check historical data
  GET  /api/v1/backtest/runs                — list saved runs
  GET  /api/v1/backtest/runs/{id}           — fetch full saved run
  DELETE /api/v1/backtest/runs/{id}         — delete a saved run

Legacy walk-forward:
  GET  /api/v1/backtest/run                 — legacy XAU/USD walk-forward
  GET  /api/v1/backtest/optimize            — parameter grid search

Rate limits:
  /xauusd        — 3 requests/minute per IP  (CPU-intensive strict scan)
  /run           — 5 requests/minute per IP
  /optimize      — 3 requests/minute per IP
  /import-csv    — 4 requests/minute per IP
"""
import json
import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, File, HTTPException, Query, Request, UploadFile
from sqlalchemy.orm import Session

from database import get_db
from db_models import BacktestRun
from data.candles import get_candles, INTERVAL_MINUTES
from exceptions import ProviderError
from models.common import APIResponse
from pair_config import SUPPORTED_PAIRS, DEFAULT_PAIR, DEFAULT_PAIR_CODE, validate_pair
from rate_limit import limiter
from services.historical_data_provider import (
    historical_data_available, import_csv as import_historical_csv,
    seed_historical_data, fetch_tradingview_history,
)
from services.xauusd_backtester import run_xauusd_backtest

router = APIRouter(prefix="/backtest", tags=["backtest"])
logger = logging.getLogger(__name__)

VALID_TIMEFRAMES = list(INTERVAL_MINUTES.keys())


def _fetch_candles(pair: str, timeframe: str, lookback: int):
    """Fetch candles for any supported pair (pair is already a normalised code)."""
    return get_candles(interval=timeframe, limit=lookback, pair=pair)


# ── /backtest/run ─────────────────────────────────────────────────────────────

@router.get(
    "/run",
    summary="Backtest the ICT signal engine on historical candles",
    description=(
        "Walk-forward backtest with no look-ahead bias. "
        "Rate-limited to 5 requests/minute per IP."
    ),
)
@limiter.limit("5/minute")
def backtest_run(
    request: Request,
    pair: str = Query(
        default=DEFAULT_PAIR,
        description=f"Trading pair. Supported: {', '.join(SUPPORTED_PAIRS)}",
    ),
    timeframe: str = Query(
        default="H4",
        description=f"Candle timeframe. Valid: {', '.join(VALID_TIMEFRAMES)}",
    ),
    lookback: int = Query(
        default=500,
        ge=100,
        le=5000,
        description="Number of historical candles (100–5000)",
    ),
) -> dict:
    tf = timeframe.upper()
    if tf not in INTERVAL_MINUTES:
        raise HTTPException(
            status_code=422,
            detail=f"Unknown timeframe '{timeframe}'. Valid: {VALID_TIMEFRAMES}",
        )
    try:
        pair = validate_pair(pair)   # normalise to code ("eurusd" | "xauusd")
    except ValueError:
        raise HTTPException(
            status_code=422,
            detail=f"Unsupported pair '{pair}'. Valid codes: {list(SUPPORTED_PAIRS)}",
        )

    logger.info(
        "Backtest requested pair=%s timeframe=%s lookback=%d ip=%s",
        pair, tf, lookback, request.client.host if request.client else "?",
    )

    try:
        candle_resp = _fetch_candles(pair, tf, lookback)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except ProviderError as exc:
        logger.warning("Provider error in backtest pair=%s: %s", pair, exc)
        raise HTTPException(status_code=exc.http_code, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Candle data error: {exc}") from exc

    from services.backtester import run_backtest
    result = run_backtest(candle_resp.candles, pair=pair)
    logger.info(
        "Backtest complete pair=%s trades=%s winRate=%s expectancy=%s",
        pair, result.get("totalTrades"), result.get("winRate"), result.get("expectancy"),
    )
    return result


# ── /backtest/optimize ────────────────────────────────────────────────────────

@router.get(
    "/optimize",
    summary="Walk-forward parameter optimization",
    description=(
        "Grid search over signal_window × min_rr on IS slice (70%), "
        "validates best params on OOS (30%). "
        "Rate-limited to 3 requests/minute per IP."
    ),
)
@limiter.limit("3/minute")
def backtest_optimize(
    request: Request,
    pair: str = Query(
        default=DEFAULT_PAIR,
        description=f"Trading pair. Supported: {', '.join(SUPPORTED_PAIRS)}",
    ),
    timeframe: str = Query(
        default="H4",
        description=f"Candle timeframe. Valid: {', '.join(VALID_TIMEFRAMES)}",
    ),
    lookback: int = Query(
        default=1000,
        ge=200,
        le=5000,
        description="Number of historical candles for IS+OOS split (200–5000)",
    ),
) -> dict:
    tf = timeframe.upper()
    if tf not in INTERVAL_MINUTES:
        raise HTTPException(
            status_code=422,
            detail=f"Unknown timeframe '{timeframe}'. Valid: {VALID_TIMEFRAMES}",
        )
    try:
        pair = validate_pair(pair)   # normalise to code
    except ValueError:
        raise HTTPException(
            status_code=422,
            detail=f"Unsupported pair '{pair}'. Valid codes: {list(SUPPORTED_PAIRS)}",
        )

    logger.info(
        "Optimizer requested pair=%s timeframe=%s lookback=%d ip=%s",
        pair, tf, lookback, request.client.host if request.client else "?",
    )

    try:
        candle_resp = _fetch_candles(pair, tf, lookback)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except ProviderError as exc:
        logger.warning("Provider error in optimize pair=%s: %s", pair, exc)
        raise HTTPException(status_code=exc.http_code, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Candle data error: {exc}") from exc

    from services.optimizer import optimize
    result = optimize(candle_resp.candles, pair=pair)

    if "error" in result:
        raise HTTPException(status_code=422, detail=result)

    logger.info(
        "Optimizer complete pair=%s bestParams=%s overfitWarning=%s",
        pair, result.get("bestParams"), result.get("overfitWarning"),
    )
    return result


# ═══════════════════════════════════════════════════════════════════════════════
# Strict XAU/USD backtest endpoints
# ═══════════════════════════════════════════════════════════════════════════════

@router.get(
    "/xauusd",
    response_model=APIResponse[dict],
    summary="Strict XAU/USD backtest — full diagnostic report",
    description=(
        "Runs the existing signal engine across historical XAU/USD candles "
        "with strict look-ahead protection, conservative TP/SL collision, "
        "spread + slippage applied, and full reliability rating. "
        "Returns summary metrics, equity curve, trade list, skipped trades, "
        "breakdowns, and warnings. Rate-limited to 3 requests/minute."
    ),
)
@limiter.limit("3/minute")
def xauusd_backtest(
    request: Request,
    start_date:          str | None = Query(default=None, description="ISO date YYYY-MM-DD"),
    end_date:            str | None = Query(default=None, description="ISO date YYYY-MM-DD"),
    timeframe:           str = Query(default="M15", description="M5 | M15 | M30 | H1 | H4 | D1"),
    lookback:            int = Query(default=5000, ge=200, le=20000),
    initial_balance:     float = Query(default=10000.0, gt=0),
    risk_percent:        float = Query(default=0.25, gt=0, le=5),
    spread_points:       float = Query(default=1.5,  ge=0, le=20),
    slippage_points:     float = Query(default=0.5,  ge=0, le=20),
    commission_per_trade: float = Query(default=0.0, ge=0, le=100),
    include_news_filter: bool = Query(default=True),
    session_filter:      str | None = Query(default=None),
    min_score:           int = Query(default=80, ge=50, le=100),
    min_rr:              float = Query(default=2.5, ge=1.0, le=10),
    max_trades:          int | None = Query(default=None, ge=1, le=10000),
    allow_overlap:       bool = Query(default=False, description="Allow concurrent trades"),
    auto_seed:           bool = Query(default=True, description="Auto-seed 365 days if DB empty"),
    enable_premium_gates: bool = Query(default=True, description="Enforce 5 premium ICT gates (OB+OTE+DXY+DailyOpen+LondonFix)"),
    walk_forward_segments: int = Query(default=4, ge=2, le=10, description="Phase 2a — walk-forward segments"),
    monte_carlo_runs:      int = Query(default=500, ge=0, le=5000, description="Phase 2a — Monte Carlo runs (0 = disabled)"),
    classify_regimes:      bool = Query(default=True, description="Phase 2a — classify each trade's market regime"),
    risk_sensitivity_flag: bool = Query(default=True, alias="risk_sensitivity", description="Phase 2a — project equity at 0.25/0.5/1.0 risk levels"),
    analyze_skipped:       bool = Query(default=True, description="Phase 2b — simulate hypothetical outcomes for skipped trades"),
    engine_variant:        str  = Query(default="swing", description="Engine variant: swing (H4 default) | intraday (M15 with Asian range)"),
    anti_cluster_hours:    float = Query(default=0.0, ge=0, le=72, description="Reject same-direction signals within N hours (0 = disabled)"),
    target_pips_override:  int | None = Query(default=None, ge=5, le=200, description="Override pair-config target_pips (default = 50 for XAU/USD)"),
    save:                bool = Query(default=True, description="Save run to backtest_runs table"),
    db: Session = Depends(get_db),
) -> APIResponse[dict]:
    """Run a strict XAU/USD backtest. Returns the full diagnostic report."""
    def _parse_date(d: str | None) -> datetime | None:
        if not d:
            return None
        try:
            dt = datetime.fromisoformat(d.replace("Z", "+00:00"))
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        except Exception as e:
            raise HTTPException(status_code=400,
                                detail=f"Invalid date '{d}': {e}") from e

    sd = _parse_date(start_date)
    ed = _parse_date(end_date)

    logger.info(
        "[backtest] XAU/USD backtest requested timeframe=%s lookback=%d "
        "balance=%.2f risk=%.2f spread=%.1f slippage=%.1f minScore=%d minRR=%.2f",
        timeframe, lookback, initial_balance, risk_percent,
        spread_points, slippage_points, min_score, min_rr,
    )

    try:
        result = run_xauusd_backtest(
            db=db,
            start_date=sd,
            end_date=ed,
            timeframe=timeframe,
            lookback=lookback,
            initial_balance=initial_balance,
            risk_percent=risk_percent,
            spread_points=spread_points,
            slippage_points=slippage_points,
            commission_per_trade=commission_per_trade,
            include_news_filter=include_news_filter,
            session_filter=session_filter,
            min_score=min_score,
            min_rr=min_rr,
            max_trades=max_trades,
            allow_overlap=allow_overlap,
            auto_seed=auto_seed,
            enable_premium_gates=enable_premium_gates,
            walk_forward_segments=walk_forward_segments,
            monte_carlo_runs=monte_carlo_runs,
            classify_regimes=classify_regimes,
            risk_sensitivity=risk_sensitivity_flag,
            analyze_skipped=analyze_skipped,
            engine_variant=engine_variant,
            anti_cluster_hours=anti_cluster_hours,
            target_pips_override=target_pips_override,
        )
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception("[backtest] XAU/USD backtest failed")
        raise HTTPException(status_code=502, detail=f"Backtest error: {exc}") from exc

    # Persist if requested
    saved_id = None
    if save:
        try:
            saved_id = _persist_run(db, result, sd, ed, timeframe)
            result["savedRunId"] = saved_id
        except Exception as e:
            logger.warning("[backtest] Save failed (non-fatal): %s", e)

    s = result.get("summary", {})
    logger.info(
        "[backtest] XAU/USD complete trades=%d winRate=%s exp=%s PF=%s rating=%s savedId=%s",
        s.get("validTrades"), s.get("winRate"), s.get("expectancyPoints"),
        s.get("profitFactor"), result.get("reliability", {}).get("score"), saved_id,
    )
    return APIResponse(data=result)


@router.post(
    "/import-csv",
    response_model=APIResponse[dict],
    summary="Import historical XAU/USD candles from CSV",
    description=(
        "Upload a CSV with columns timestamp,open,high,low,close[,volume]. "
        "Rows are validated (OHLC sanity, ISO timestamp), duplicates are skipped. "
        "Returns counts and any per-row error messages. "
        "Rate-limited to 4 requests/minute."
    ),
)
@limiter.limit("4/minute")
async def import_csv_route(
    request: Request,
    file: UploadFile = File(..., description="CSV file with timestamp,open,high,low,close,volume"),
    timeframe: str = Query(default="M15"),
    db: Session = Depends(get_db),
) -> APIResponse[dict]:
    try:
        raw = await file.read()
        try:
            text = raw.decode("utf-8-sig")
        except UnicodeDecodeError:
            text = raw.decode("latin-1")
        result = import_historical_csv(db=db, csv_text=text, timeframe=timeframe)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception("[backtest] CSV import failed")
        raise HTTPException(status_code=502, detail=f"CSV import error: {exc}") from exc

    logger.info(
        "[backtest] CSV import filename=%s timeframe=%s imported=%d skipped=%d",
        file.filename, result.timeframe, result.imported, result.skipped,
    )
    return APIResponse(data={
        "imported":   result.imported,
        "skipped":    result.skipped,
        "duplicates": result.duplicates,
        "invalid":    result.invalid,
        "timeframe":  result.timeframe,
        "instrument": result.instrument,
        "errors":     result.errors,
        "filename":   file.filename,
    })


@router.post(
    "/seed-historical",
    response_model=APIResponse[dict],
    summary="Seed deterministic historical XAU/USD data for backtesting",
    description=(
        "Generates a deterministic 365-day dataset (default) of XAU/USD candles "
        "with realistic session-weighted volatility, regime shifts, and "
        "liquidity sweeps. Also populates macro_events with a realistic "
        "USD macro calendar (NFP, CPI, FOMC, GDP, Retail Sales, ISM, "
        "Unemployment Claims). Idempotent: skips if data already exists "
        "unless force=true. Rate-limited to 2 requests/minute."
    ),
)
@limiter.limit("2/minute")
def seed_historical(
    request: Request,
    days:      int  = Query(default=365, ge=30, le=1825),
    timeframe: str  = Query(default="M15"),
    seed:      int  = Query(default=42),
    force:     bool = Query(default=False, description="Overwrite if data exists"),
    db: Session = Depends(get_db),
) -> APIResponse[dict]:
    try:
        result = seed_historical_data(
            db=db, days=days, timeframe=timeframe, seed=seed, force=force,
        )
        return APIResponse(data=result)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception("[backtest] Seed failed")
        raise HTTPException(status_code=502, detail=f"Seed error: {exc}") from exc


@router.post(
    "/fetch-tradingview",
    response_model=APIResponse[dict],
    summary="Bulk import real XAU/USD historical candles from TradingView",
    description=(
        "Uses the existing authenticated tradingview_provider to fetch real "
        "OANDA:XAUUSD historical bars and store them locally in historical_candles. "
        "This is a one-time data import — subsequent backtests read from the local DB. "
        "Pass force=true to refresh existing rows. Rate-limited to 2/minute."
    ),
)
@limiter.limit("2/minute")
def fetch_tradingview(
    request: Request,
    timeframe: str  = Query(default="H4"),
    n_bars:    int  = Query(default=5000, ge=500, le=20000),
    force:     bool = Query(default=False),
    db:        Session = Depends(get_db),
) -> APIResponse[dict]:
    try:
        result = fetch_tradingview_history(
            db=db, timeframe=timeframe, n_bars=n_bars, force=force,
        )
        if not result.get("success"):
            raise HTTPException(status_code=502,
                                 detail=result.get("error", "TradingView fetch failed"))
        return APIResponse(data=result)
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("[backtest] TradingView import failed")
        raise HTTPException(status_code=502, detail=f"Import error: {exc}") from exc


@router.get(
    "/data-status",
    response_model=APIResponse[dict],
    summary="Historical candle availability for backtesting",
)
@limiter.limit("30/minute")
def data_status(
    request: Request,
    timeframe: str = Query(default="M15"),
    db: Session = Depends(get_db),
) -> APIResponse[dict]:
    return APIResponse(data=historical_data_available(db=db, timeframe=timeframe))


@router.get(
    "/runs",
    response_model=APIResponse[dict],
    summary="List saved backtest runs (newest first)",
)
@limiter.limit("30/minute")
def list_runs(
    request: Request,
    limit: int = Query(default=20, ge=1, le=100),
    db:    Session = Depends(get_db),
) -> APIResponse[dict]:
    rows = (
        db.query(BacktestRun)
          .order_by(BacktestRun.created_at.desc())
          .limit(limit)
          .all()
    )
    return APIResponse(data={
        "total": len(rows),
        "runs": [_run_to_summary(r) for r in rows],
    })


@router.get(
    "/runs/{run_id}",
    response_model=APIResponse[dict],
    summary="Fetch a single saved backtest run (full payload)",
)
@limiter.limit("30/minute")
def get_run(
    request: Request,
    run_id: int,
    db: Session = Depends(get_db),
) -> APIResponse[dict]:
    row = db.get(BacktestRun, run_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"Backtest run {run_id} not found")

    return APIResponse(data={
        **_run_to_summary(row),
        "settings":    _safe_json(row.settings_json,   {}),
        "summary":     _safe_json(row.summary_json,    {}),
        "breakdowns":  _safe_json(row.breakdowns_json, {}),
        "equityCurve": _safe_json(row.equity_curve_json, []),
        "trades":      _safe_json(row.trades_json,     []),
        "skipped":     _safe_json(row.skipped_json,    []),
    })


@router.get(
    "/compare-runs",
    response_model=APIResponse[dict],
    summary="Compare the last N saved backtest runs side-by-side",
)
@limiter.limit("10/minute")
def compare_runs(
    request: Request,
    limit: int = Query(default=5, ge=2, le=20),
    db:    Session = Depends(get_db),
) -> APIResponse[dict]:
    rows = (
        db.query(BacktestRun)
          .order_by(BacktestRun.created_at.desc())
          .limit(limit)
          .all()
    )
    if not rows:
        return APIResponse(data={"runs": [], "comparison": []})

    comparison = []
    for r in rows:
        comparison.append({
            "id":                  r.id,
            "createdAt":           r.created_at.isoformat() if r.created_at else None,
            "timeframe":           r.timeframe,
            "validTrades":         r.valid_trades,
            "winRate":             r.win_rate,
            "expectancyR":         r.expectancy_r,
            "profitFactor":        r.profit_factor,
            "maxDrawdownPercent":  r.max_drawdown_percent,
            "netReturnPercent":    r.net_return_percent,
            "qualityScore":        r.reliability_rating,
            "band":                r.reliability_band,
        })
    return APIResponse(data={"runs": [_run_to_summary(r) for r in rows], "comparison": comparison})


@router.delete(
    "/runs/{run_id}",
    response_model=APIResponse[dict],
    summary="Delete a saved backtest run (requires explicit ID)",
)
@limiter.limit("10/minute")
def delete_run(
    request: Request,
    run_id: int,
    db: Session = Depends(get_db),
) -> APIResponse[dict]:
    row = db.get(BacktestRun, run_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"Backtest run {run_id} not found")
    db.delete(row)
    db.commit()
    return APIResponse(data={"deleted": True, "id": run_id})


# ── Helpers for run persistence ───────────────────────────────────────────────

def _persist_run(db: Session, result: dict, sd, ed, timeframe: str) -> int:
    s = result.get("summary", {})
    settings = result.get("settings", {})
    rating   = result.get("reliability", {})

    row = BacktestRun(
        instrument="XAU/USD",
        timeframe=timeframe,
        start_date=sd,
        end_date=ed,
        initial_balance=settings.get("initialBalance", 10000.0),
        risk_percent=settings.get("riskPercent", 0.25),
        spread_points=settings.get("spreadPoints", 1.5),
        slippage_points=settings.get("slippagePoints", 0.5),
        min_score=settings.get("minScore", 80),
        min_rr=settings.get("minRR", 2.5),
        total_signals_scanned=s.get("totalSignalsScanned", 0),
        valid_trades=s.get("validTrades", 0),
        win_rate=s.get("winRate", 0.0),
        expectancy_points=s.get("expectancyPoints", 0.0),
        expectancy_r=s.get("expectancyR", 0.0),
        profit_factor=s.get("profitFactor"),
        max_drawdown_percent=s.get("maxDrawdownPercent", 0.0),
        final_balance=s.get("finalBalance", 0.0),
        net_return_percent=s.get("netReturnPercent", 0.0),
        reliability_rating=rating.get("score", 0),
        reliability_band=rating.get("band"),
        settings_json=json.dumps(settings),
        summary_json=json.dumps(s),
        trades_json=json.dumps(result.get("trades", [])),
        skipped_json=json.dumps(result.get("skipped", [])),
        breakdowns_json=json.dumps(result.get("breakdowns", {})),
        equity_curve_json=json.dumps(result.get("equityCurve", [])),
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row.id


def _run_to_summary(r: BacktestRun) -> dict:
    return {
        "id":                  r.id,
        "createdAt":           r.created_at.isoformat() if r.created_at else None,
        "instrument":          r.instrument,
        "timeframe":           r.timeframe,
        "startDate":           r.start_date.isoformat() if r.start_date else None,
        "endDate":             r.end_date.isoformat()   if r.end_date   else None,
        "initialBalance":      r.initial_balance,
        "riskPercent":         r.risk_percent,
        "spreadPoints":        r.spread_points,
        "slippagePoints":      r.slippage_points,
        "minScore":            r.min_score,
        "minRR":               r.min_rr,
        "totalSignalsScanned": r.total_signals_scanned,
        "validTrades":         r.valid_trades,
        "winRate":             r.win_rate,
        "expectancyPoints":    r.expectancy_points,
        "expectancyR":         r.expectancy_r,
        "profitFactor":        r.profit_factor,
        "maxDrawdownPercent":  r.max_drawdown_percent,
        "finalBalance":        r.final_balance,
        "netReturnPercent":    r.net_return_percent,
        "reliabilityRating":   r.reliability_rating,
        "reliabilityBand":     r.reliability_band,
    }


def _safe_json(raw: str | None, default):
    if not raw:
        return default
    try:
        return json.loads(raw)
    except Exception:
        return default


# ── /backtest/eurusd — explicitly rejected ─────────────────────────────────────

@router.get(
    "/eurusd",
    summary="EUR/USD not supported",
    include_in_schema=False,
)
def backtest_eurusd_rejected() -> None:
    raise HTTPException(
        status_code=400,
        detail="EUR/USD is not supported. This dashboard is XAU/USD only. Use /backtest/run?pair=XAU/USD."
    )
