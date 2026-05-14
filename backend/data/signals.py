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

# XAU/USD fallback mock signal (shown when live engine is unavailable)
_FACTORS = [
    SignalFactor(name="HTF Bias",         value="Waiting for D1 confirmation", score=5,  positive=False),
    SignalFactor(name="Market Structure", value="Waiting for CHoCH",            score=0,  positive=False),
    SignalFactor(name="Liquidity Sweep",  value="No sweep detected",            score=0,  positive=False),
    SignalFactor(name="Fair Value Gap",   value="No valid FVG yet",             score=0,  positive=False),
    SignalFactor(name="News Risk",        value="CLEAR",                        score=15, positive=True),
    SignalFactor(name="Session",          value="London kill zone",             score=10, positive=True),
    SignalFactor(name="Spread",           value="0.3 pts (normal)",             score=85, positive=True),
]

_CURRENT_SIGNAL = CurrentSignal(
    direction="NEUTRAL",
    strength=30,
    confidence=30,
    timestamp=datetime(2026, 5, 14, 9, 0, 0, tzinfo=timezone.utc),
    session="London",
    timeframe="H4",
    price=3285.00,
    change=-3.20,
    change_pct=-0.10,
    factors=_FACTORS,
)

_TRADE_PLAN = TradePlan(
    entry=None,
    stop_loss=None,
    stop_loss_pips=None,
    targets=[],
    risk_percent=None,
    position_size=None,
    account_size=None,
    risk_amount=None,
    validity=datetime(2026, 5, 14, 20, 0, 0, tzinfo=timezone.utc),
    notes=(
        "XAU/USD — waiting for all ICT gates to align. "
        "No active setup. Target: 50 points. Minimum RR: 2.5. "
        "Manual confirmation only — broker execution disabled."
    ),
)


def get_current_signal() -> SignalOutput:
    return SignalOutput(signal=_CURRENT_SIGNAL, trade_plan=_TRADE_PLAN)


def run_signal_analysis(
    macro_events: list[dict] | None = None,
    pair: str = "xauusd",
    db=None,            # SQLAlchemy Session — enables adaptive weights + live feed
) -> SignalAnalysisOutput:
    """
    Run the ICT engine on H4 candles and return the flat signal output.

    When `db` is provided:
      - Adaptive scoring weights are loaded from DB (learned from past outcomes)
      - Live MT5 bridge candles are tried before synthetic fallback
    """
    from pair_config import get_pair_config
    pair_cfg = get_pair_config(pair)

    # Engine handles both live-feed fetch and adaptive weights internally
    # when db is supplied. Pass empty candles so engine tries live feed first.
    r = analyze_signal(
        pair=pair,
        candles=[],                 # engine fills from live feed if bridge online
        macro_events=macro_events or [],
        db=db,
    )

    # If engine returned empty (bridge down, synthetic fallback needed), supply candles
    # NOTE: analyze_signal already handles this internally via synthetic fallback,
    # so we only hit this branch if the engine genuinely returned with no bars.

    return SignalAnalysisOutput(
        pair=pair,
        display_pair=pair_cfg.get("display", "XAU/USD"),
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
            sentiment=r.model.get("sentiment"),
            order_block=r.model.get("orderBlock"),
            ote_zone=r.model.get("oteZone"),
            dxy_alignment=r.model.get("dxyAlignment"),
            daily_open=r.model.get("dailyOpen"),
            london_fix=r.model.get("londonFix"),
            premium_gates_passed=r.model.get("premiumGatesPassed"),
        ),
        pair_mode=pair_cfg.get("mode", "MONITOR_ONLY"),
        data_source=getattr(r, "data_source", "synthetic"),
        weights_used=getattr(r, "weights_used", None),
        component_snapshot=getattr(r, "component_snapshot", "{}"),
    )


# ── Historical trades — XAU/USD mock fallback ─────────────────────────────────
# Prices in gold range (3200–3320). Points = price difference × 1 (pip_size=1.0).

_RAW_HISTORY = [
    # id  date         time   dir    entry    exit    pts    rr     result      pnl   score
    (1,  "2026-05-12", "09:15", "BUY",  3267.40, 3317.40, +50,  2.50, "WIN",      +250, 84),
    (2,  "2026-05-12", "14:30", "SELL", 3310.80, 3286.50, +24,  1.20, "WIN",      +120, 66),
    (3,  "2026-05-11", "10:00", "BUY",  3252.60, 3232.80, -20, -1.0,  "LOSS",     -100, 59),
    (4,  "2026-05-09", "08:45", "BUY",  3241.20, 3296.40, +55,  2.75, "WIN",      +275, 83),
    (5,  "2026-05-08", "13:15", "SELL", 3298.50, 3298.50,   0,  0.0,  "BREAKEVEN",   0, 62),
    (6,  "2026-05-07", "09:00", "SELL", 3314.70, 3279.70, +35,  1.75, "WIN",      +175, 77),
    (7,  "2026-05-06", "10:30", "BUY",  3259.80, 3239.80, -20, -1.0,  "LOSS",     -100, 61),
    (8,  "2026-05-05", "08:15", "BUY",  3238.40, 3313.60, +75,  3.75, "WIN",      +375, 90),
    (9,  "2026-05-02", "14:00", "SELL", 3325.10, 3290.10, +35,  1.75, "WIN",      +175, 74),
    (10, "2026-05-01", "09:45", "BUY",  3248.60, 3298.60, +50,  2.50, "WIN",      +250, 81),
    (11, "2026-04-30", "11:00", "SELL", 3307.90, 3328.10, -20, -1.0,  "LOSS",     -100, 56),
    (12, "2026-04-29", "08:30", "BUY",  3255.30, 3305.30, +50,  2.50, "WIN",      +250, 86),
    (13, "2026-04-28", "13:30", "SELL", 3318.50, 3283.50, +35,  1.75, "WIN",      +175, 78),
    (14, "2026-04-25", "09:00", "BUY",  3231.70, 3211.70, -20, -1.0,  "LOSS",     -100, 53),
    (15, "2026-04-24", "10:15", "SELL", 3304.20, 3269.20, +35,  1.75, "WIN",      +175, 79),
    (16, "2026-04-23", "08:00", "BUY",  3228.90, 3291.40, +63,  3.15, "WIN",      +315, 82),
    (17, "2026-04-22", "14:45", "SELL", 3312.00, 3287.00, +25,  1.25, "WIN",      +125, 68),
    (18, "2026-04-17", "09:30", "BUY",  3219.50, 3199.50, -20, -1.0,  "LOSS",     -100, 58),
    (19, "2026-04-16", "11:30", "SELL", 3297.80, 3262.80, +35,  1.75, "WIN",      +175, 73),
    (20, "2026-04-15", "08:00", "BUY",  3225.60, 3287.90, +62,  3.10, "WIN",      +310, 85),
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
