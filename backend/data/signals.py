"""
Signal data layer.
- get_current_signal()   : mock static signal (Phase 1-3 fallback)
- run_signal_analysis()  : live engine result built from generated candles
- get_signal_history()   : historical mock trades
"""
from datetime import datetime, timezone

from models.signal import (
    CurrentSignal, SignalFactor, TradePlan, TradePlanTarget,
    SignalOutput, HistoricalTrade, SignalHistoryResponse,
    SignalAnalysisOutput, SignalModelDetail,
)
from data.candles import get_candles
from services.signal_engine import analyze_signal

# ── Current signal ────────────────────────────────────────────────────────────

_FACTORS = [
    SignalFactor(name="Trend Alignment",   value="Bullish (H4/D1)",     score=85, positive=True),
    SignalFactor(name="Market Structure",  value="Higher Highs / HLs",  score=80, positive=True),
    SignalFactor(name="RSI (H4)",          value="58.4 — Bullish Zone", score=68, positive=True),
    SignalFactor(name="MACD",              value="Bullish Cross",        score=72, positive=True),
    SignalFactor(name="Volume Delta",      value="Accumulation +2.8M",  score=70, positive=True),
    SignalFactor(name="DXY Correlation",   value="Weakening (bearish)", score=62, positive=True),
    SignalFactor(name="Spread",            value="1.2 pips (normal)",   score=90, positive=True),
    SignalFactor(name="Session Overlap",   value="London Open Active",  score=75, positive=True),
]

_CURRENT_SIGNAL = CurrentSignal(
    direction="BUY",
    strength=78,
    confidence=72,
    timestamp=datetime(2026, 5, 13, 8, 42, 0, tzinfo=timezone.utc),
    session="London",
    timeframe="H4",
    price=1.08432,
    change=0.00087,
    change_pct=0.08,
    factors=_FACTORS,
)

_TRADE_PLAN = TradePlan(
    entry=1.08450,
    stop_loss=1.08100,
    stop_loss_pips=35,
    targets=[
        TradePlanTarget(label="TP1", price=1.08800, rr="1.0",  pips=35,  partial="50%"),
        TradePlanTarget(label="TP2", price=1.09100, rr="1.86", pips=65,  partial="30%"),
        TradePlanTarget(label="TP3", price=1.09450, rr="2.86", pips=100, partial="20%"),
    ],
    risk_percent=1.5,
    position_size=0.75,
    account_size=10000.0,
    risk_amount=150.0,
    validity=datetime(2026, 5, 13, 20, 0, 0, tzinfo=timezone.utc),
    notes=(
        "Wait for London close H1 candle confirmation above 1.0840. "
        "Entry on pullback to the 1.0840–1.0845 demand zone. "
        "Invalidated on close below 1.0810."
    ),
)


def get_current_signal() -> SignalOutput:
    return SignalOutput(signal=_CURRENT_SIGNAL, trade_plan=_TRADE_PLAN)


def run_signal_analysis(macro_events: list[dict] | None = None) -> SignalAnalysisOutput:
    """Run the ICT engine on H4 candles and return the flat signal output."""
    candle_resp = get_candles(interval="H4", limit=200)
    r = analyze_signal(candle_resp.candles, macro_events or [])

    return SignalAnalysisOutput(
        signal=r.signal,                # type: ignore[arg-type]
        quality_score=r.quality_score,
        entry=r.entry,
        stop_loss=r.stop_loss,
        take_profit=r.take_profit,
        risk_pips=r.risk_pips,
        target_pips=r.target_pips,
        rr=r.rr,
        invalidation=r.invalidation,
        reason=r.reason,
        news_status=r.news_status,      # type: ignore[arg-type]
        model=SignalModelDetail(
            higher_timeframe_bias=r.model["higherTimeframeBias"],
            liquidity=r.model["liquidity"],
            structure=r.model["structure"],
            fvg=r.model["fvg"],
            session=r.model["session"],
        ),
    )


# ── Historical trades ─────────────────────────────────────────────────────────

_RAW_HISTORY = [
    (1,  "2026-05-12", "09:15", "BUY",  1.07820, 1.08210, +39,  1.95, "WIN",       +195, 74),
    (2,  "2026-05-12", "14:30", "SELL", 1.08350, 1.08100, +25,  1.25, "WIN",       +125, 65),
    (3,  "2026-05-11", "10:00", "BUY",  1.07640, 1.07410, -23, -1.0,  "LOSS",      -150, 58),
    (4,  "2026-05-09", "08:45", "BUY",  1.07300, 1.07820, +52,  2.60, "WIN",       +260, 82),
    (5,  "2026-05-08", "13:15", "SELL", 1.08100, 1.08100,   0,  0.0,  "BREAKEVEN",    0, 61),
    (6,  "2026-05-07", "09:00", "SELL", 1.08640, 1.08290, +35,  1.75, "WIN",       +175, 77),
    (7,  "2026-05-06", "10:30", "BUY",  1.07980, 1.07760, -22, -1.0,  "LOSS",      -150, 60),
    (8,  "2026-05-05", "08:15", "BUY",  1.07500, 1.08120, +62,  3.10, "WIN",       +310, 88),
    (9,  "2026-05-02", "14:00", "SELL", 1.08900, 1.08560, +34,  1.70, "WIN",       +170, 73),
    (10, "2026-05-01", "09:45", "BUY",  1.07150, 1.07420, +27,  1.35, "WIN",       +135, 69),
    (11, "2026-04-30", "11:00", "SELL", 1.08450, 1.08700, -25, -1.0,  "LOSS",      -150, 55),
    (12, "2026-04-29", "08:30", "BUY",  1.07640, 1.08190, +55,  2.75, "WIN",       +275, 85),
    (13, "2026-04-28", "13:30", "SELL", 1.08820, 1.08470, +35,  1.75, "WIN",       +175, 76),
    (14, "2026-04-25", "09:00", "BUY",  1.07200, 1.07000, -20, -1.0,  "LOSS",      -150, 52),
    (15, "2026-04-24", "10:15", "SELL", 1.08300, 1.07950, +35,  1.75, "WIN",       +175, 78),
    (16, "2026-04-23", "08:00", "BUY",  1.07050, 1.07580, +53,  2.65, "WIN",       +265, 81),
    (17, "2026-04-22", "14:45", "SELL", 1.08500, 1.08250, +25,  1.25, "WIN",       +125, 67),
    (18, "2026-04-17", "09:30", "BUY",  1.06800, 1.06560, -24, -1.0,  "LOSS",      -150, 57),
    (19, "2026-04-16", "11:30", "SELL", 1.08120, 1.07780, +34,  1.70, "WIN",       +170, 72),
    (20, "2026-04-15", "08:00", "BUY",  1.06950, 1.07480, +53,  2.65, "WIN",       +265, 83),
]

_HISTORY: list[HistoricalTrade] = [
    HistoricalTrade(
        id=r[0], date=r[1], time=r[2], direction=r[3],  # type: ignore[arg-type]
        entry=r[4], exit=r[5], pips=r[6], rr=r[7],
        result=r[8],  # type: ignore[arg-type]
        pnl=r[9], confidence=r[10],
    )
    for r in _RAW_HISTORY
]


def confirm_signal(signal_id: int) -> dict:
    # Phase 4: mark signal as acknowledged in the database
    return {"signal_id": signal_id, "confirmed": True, "message": "Signal confirmed"}


def update_signal_result(signal_id: int, result: str, exit_price: float, pips: int, pnl: float, notes: str | None) -> dict:
    # Phase 4: persist result to the database
    return {
        "signal_id": signal_id,
        "updated": True,
        "result": result,
        "exit_price": exit_price,
        "pips": pips,
        "pnl": pnl,
    }


def get_signal_history(
    page: int = 1,
    page_size: int = 20,
    direction: str | None = None,
    result: str | None = None,
) -> SignalHistoryResponse:
    trades = _HISTORY

    if direction:
        trades = [t for t in trades if t.direction == direction.upper()]
    if result:
        trades = [t for t in trades if t.result == result.upper()]

    total = len(trades)
    start = (page - 1) * page_size
    end = start + page_size
    return SignalHistoryResponse(
        total=total,
        page=page,
        page_size=page_size,
        trades=trades[start:end],
    )
