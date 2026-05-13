"""
Historical backtester for the EUR/USD ICT/SMC signal engine.

Walk-forward design — no look-ahead bias:
  - The engine only sees candles[:i+1] for bar i.
  - After an entry is taken, future bars are scanned for TP/SL resolution;
    this is permitted (it represents what actually happened after the entry).
  - Session scoring and news-blackout windows use the candle timestamp, not now().

Spread and slippage are applied at entry:
  BUY  entry = close + (spread + slippage) * PIP
  SELL entry = close - (spread + slippage) * PIP

Config (set in .env):
  BACKTEST_SPREAD_PIPS   = 1.0
  BACKTEST_SLIPPAGE_PIPS = 0.5
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from config import settings
from pair_config import PAIRS, DEFAULT_PAIR, get_pair
from services.signal_engine import analyze_signal, PIP, TARGET_PIPS

logger = logging.getLogger(__name__)

# ── Config ────────────────────────────────────────────────────────────────────

SPREAD_PIPS   = settings.backtest_spread_pips
SLIPPAGE_PIPS = settings.backtest_slippage_pips

MIN_CANDLES   = 60    # warm-up bars before first signal (EMA-50 needs 50)
SIGNAL_WINDOW = 150   # default sliding window; overridable per run
MAX_BARS_FWD  = 300   # max bars forward to scan for TP/SL; trade expires if unresolved


# ── Internal types ────────────────────────────────────────────────────────────

@dataclass
class _Trade:
    time: str
    signal: str
    entry: float
    stop_loss: float
    take_profit: float
    result: str       # WIN | LOSS
    pips: int
    rr: float
    session: str
    quality_score: int
    reason: str


# ── Helpers ───────────────────────────────────────────────────────────────────

def _session_label(ts: datetime) -> str:
    t = ts.hour + ts.minute / 60
    if 7 <= t < 12:
        return "London"
    if 12 <= t < 17:
        return "Overlap"
    if 17 <= t < 21:
        return "New York"
    if 0 <= t < 7:
        return "Asian"
    return "Off-session"


def _max_drawdown(cum_pips: list[int]) -> int:
    if not cum_pips:
        return 0
    peak  = cum_pips[0]
    worst = 0
    for p in cum_pips:
        if p > peak:
            peak = p
        dd = p - peak
        if dd < worst:
            worst = dd
    return worst


def _brief_reason(model: dict, fallback: str) -> str:
    parts = []
    for key in ("liquidity", "structure", "fvg"):
        txt = model.get(key, "")
        if txt and "No" not in txt and "Waiting" not in txt and "None" not in txt:
            clean = txt.split("(")[0].split("—")[0].strip().rstrip(";,.")
            if clean:
                parts.append(clean)
    return "; ".join(parts) if parts else (fallback or "ICT setup")


def _to_engine_candle(raw: Any):
    from services.signal_engine import Candle as EC
    if isinstance(raw, EC):
        return raw
    return EC(
        time=getattr(raw, "time", datetime.now(timezone.utc)),
        open=float(raw.open),
        high=float(raw.high),
        low=float(raw.low),
        close=float(raw.close),
        volume=int(getattr(raw, "volume", 0)),
    )


# ── Core walk-forward loop ────────────────────────────────────────────────────

def run_backtest(
    candles_raw: list[Any],
    macro_events: list[dict] | None = None,
    pair: str = DEFAULT_PAIR,
    signal_window: int = SIGNAL_WINDOW,
    min_rr_override: float | None = None,
) -> dict:
    """
    Run the ICT signal engine across historical candles and return performance stats.

    candles_raw    - ordered oldest-first list of candle objects
    macro_events   - optional historical calendar events for news-gate filtering
    pair           - trading pair; selects pip size, target, SL buffer from PAIR_CONFIG
    signal_window  - how many past bars to feed into the engine each bar
    min_rr_override - override the pair's default min RR (used by optimizer)
    """
    macro_events = macro_events or []
    cfg = get_pair(pair)
    pip_size       = cfg["pip"]
    target_pips    = cfg["target_pips"]
    sl_buffer_pips = cfg["sl_buffer_pips"]
    fvg_min_pips   = cfg["fvg_min_pips"]
    pair_min_rr    = min_rr_override if min_rr_override is not None else cfg["min_rr"]

    cost = (SPREAD_PIPS + SLIPPAGE_PIPS) * pip_size

    bars = [_to_engine_candle(c) for c in candles_raw]

    trades: list[_Trade] = []
    skip_until = -1  # bar index after which we resume scanning

    for i in range(MIN_CANDLES, len(bars)):
        if i <= skip_until:
            continue

        # Feed only past data — strictly no look-ahead
        window    = bars[max(0, i - signal_window): i + 1]
        candle_ts = bars[i].time

        result = analyze_signal(
            window, macro_events, at=candle_ts,
            pip_size=pip_size, target_pips=target_pips,
            sl_buffer_pips=sl_buffer_pips, min_rr=pair_min_rr,
            fvg_min_pips=fvg_min_pips,
        )

        if result.signal not in ("BUY", "SELL"):
            continue

        direction = result.signal
        if result.stop_loss is None:
            continue

        # Apply spread + slippage at entry
        if direction == "BUY":
            entry       = round(bars[i].close + cost, 5)
            stop_loss   = result.stop_loss
            take_profit = round(entry + target_pips * pip_size, 5)
        else:
            entry       = round(bars[i].close - cost, 5)
            stop_loss   = result.stop_loss
            take_profit = round(entry - target_pips * pip_size, 5)

        risk_pips = abs(entry - stop_loss) / pip_size
        if risk_pips <= 0:
            continue
        rr = round(TARGET_PIPS / risk_pips, 2)

        # Scan forward for first TP or SL hit.
        # This is not look-ahead because we are checking what would have
        # happened AFTER the entry was already taken.
        trade_result = None
        pips_result  = 0
        resolved_bar = i

        for j in range(i + 1, min(i + 1 + MAX_BARS_FWD, len(bars))):
            f = bars[j]
            if direction == "BUY":
                tp_hit = f.high >= take_profit
                sl_hit = f.low  <= stop_loss
            else:  # SELL
                tp_hit = f.low  <= take_profit
                sl_hit = f.high >= stop_loss

            if tp_hit and sl_hit:
                # Both in same candle: worst-case, assume SL hit first
                trade_result = "LOSS"
                pips_result  = -round(risk_pips)
            elif tp_hit:
                trade_result = "WIN"
                pips_result  = TARGET_PIPS
            elif sl_hit:
                trade_result = "LOSS"
                pips_result  = -round(risk_pips)

            if trade_result is not None:
                resolved_bar = j
                break

        if trade_result is None:
            # Trade expired without resolution — skip and advance past it
            skip_until = i + MAX_BARS_FWD
            continue

        trades.append(_Trade(
            time=candle_ts.isoformat(),
            signal=direction,
            entry=entry,
            stop_loss=round(stop_loss, 5),
            take_profit=round(take_profit, 5),
            result=trade_result,
            pips=pips_result,
            rr=rr,
            session=_session_label(candle_ts),
            quality_score=result.quality_score,
            reason=_brief_reason(result.model, result.reason),
        ))

        skip_until = resolved_bar  # no new entries until this trade resolves

    # ── Statistics ────────────────────────────────────────────────────────────

    n      = len(trades)
    wins   = [t for t in trades if t.result == "WIN"]
    losses = [t for t in trades if t.result == "LOSS"]
    nw, nl = len(wins), len(losses)

    win_rate = round(nw / n * 100, 2) if n else 0.0

    total_win_pips  = sum(t.pips for t in wins)
    total_loss_pips = abs(sum(t.pips for t in losses))
    avg_win_pips    = round(total_win_pips  / nw, 1) if nw else 0.0
    avg_loss_pips   = round(total_loss_pips / nl, 1) if nl else 0.0

    wr            = win_rate / 100
    lr            = 1 - wr
    expectancy    = round(wr * avg_win_pips - lr * avg_loss_pips, 1) if n else 0.0
    profit_factor = round(total_win_pips / total_loss_pips, 2) if total_loss_pips > 0 else None

    avg_rr   = round(sum(t.rr   for t in trades) / n, 2) if n else 0.0
    avg_pips = round(sum(t.pips for t in trades) / n, 1) if n else 0.0

    # Cumulative equity curve
    cum          = 0
    equity_curve: list[dict] = []
    for idx, t in enumerate(trades):
        cum += t.pips
        equity_curve.append({"trade": idx + 1, "pips": cum})

    max_dd     = _max_drawdown([p["pips"] for p in equity_curve])
    total_pips = cum

    # Session breakdown
    sess_map: dict[str, dict] = {}
    for t in trades:
        s = t.session
        if s not in sess_map:
            sess_map[s] = {"trades": 0, "wins": 0}
        sess_map[s]["trades"] += 1
        if t.result == "WIN":
            sess_map[s]["wins"] += 1

    session_breakdown = sorted(
        [
            {
                "session": s,
                "trades":  v["trades"],
                "wins":    v["wins"],
                "winRate": round(v["wins"] / v["trades"] * 100, 1) if v["trades"] else 0.0,
            }
            for s, v in sess_map.items()
        ],
        key=lambda x: -x["winRate"],
    )

    best_session  = session_breakdown[0]["session"]  if session_breakdown else "N/A"
    worst_session = session_breakdown[-1]["session"] if session_breakdown else "N/A"

    # Directional stats
    buys      = [t for t in trades if t.signal == "BUY"]
    sells     = [t for t in trades if t.signal == "SELL"]
    buy_wins  = [t for t in buys  if t.result == "WIN"]
    sell_wins = [t for t in sells if t.result == "WIN"]
    buy_win_rate  = round(len(buy_wins)  / len(buys)  * 100, 1) if buys  else 0.0
    sell_win_rate = round(len(sell_wins) / len(sells) * 100, 1) if sells else 0.0

    return {
        "totalTrades":      n,
        "wins":             nw,
        "losses":           nl,
        "winRate":          win_rate,
        "averageRR":        avg_rr,
        "averagePips":      avg_pips,
        "expectancy":       expectancy,
        "profitFactor":     profit_factor,
        "maxDrawdown":      max_dd,
        "totalPips":        total_pips,
        "bestSession":      best_session,
        "worstSession":     worst_session,
        "buyWinRate":       buy_win_rate,
        "sellWinRate":      sell_win_rate,
        "pair":             pair,
        "spreadPips":       SPREAD_PIPS,
        "slippagePips":     SLIPPAGE_PIPS,
        "sessionBreakdown": session_breakdown,
        "equityCurve":      equity_curve,
        "trades": [
            {
                "time":         t.time,
                "signal":       t.signal,
                "entry":        t.entry,
                "stopLoss":     t.stop_loss,
                "takeProfit":   t.take_profit,
                "result":       t.result,
                "pips":         t.pips,
                "rr":           t.rr,
                "session":      t.session,
                "qualityScore": t.quality_score,
                "reason":       t.reason,
            }
            for t in trades
        ],
    }
