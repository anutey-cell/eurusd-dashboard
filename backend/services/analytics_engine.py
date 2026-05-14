"""
Performance analytics engine — pure functions, no DB or FastAPI imports.

All functions receive plain Python objects so they can be called with either
DB records or the mock HistoricalTrade list without any coupling to SQLAlchemy.

Duck-typing contract for a "trade" object:
  .signal       str   "BUY" | "SELL"
  .result       str   "WIN" | "LOSS" | "BREAKEVEN" | None
  .pips         int | None
  .rr           float | None
  .news_status  str   "CLEAR" | "BLOCKED"
  .session      str   (free text from the engine, e.g. "London kill zone")
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

# ── Constants ─────────────────────────────────────────────────────────────────

CANONICAL_SESSIONS = ("Asian", "London", "Overlap", "New York")

SampleSize = Literal["low", "developing", "useful", "strong"]


# ── Internal result types ─────────────────────────────────────────────────────

@dataclass
class SessionStat:
    session: str
    trades: int
    wins: int
    win_rate: float
    avg_pips: float


@dataclass
class EquityCurvePoint:
    trade: int
    pips: int
    cumulative_pips: int


@dataclass
class AnalyticsResult:
    sample_size: SampleSize
    completed_trades: int
    sample_message: str

    total_signals: int
    confirmed_trades: int

    win_rate: float
    loss_rate: float
    average_win_pips: float
    average_loss_pips: float
    average_rr: float | None
    average_pips: float
    expectancy: float
    profit_factor: float | None
    max_drawdown: int
    consecutive_wins: int
    consecutive_losses: int

    long_win_rate: float | None
    short_win_rate: float | None
    news_day_win_rate: float | None
    non_news_day_win_rate: float | None

    best_session: str | None
    worst_session: str | None
    session_breakdown: list[SessionStat]

    equity_curve: list[EquityCurvePoint]


# ── Session classification ────────────────────────────────────────────────────

def classify_session(session_text: str) -> str:
    """Map free-form session text → one of the four canonical names."""
    s = session_text.lower()
    if "overlap" in s:
        return "Overlap"
    if "london" in s:
        return "London"
    if "new york" in s or "ny" in s:
        return "New York"
    if "asian" in s or "tokyo" in s or "sydney" in s:
        return "Asian"
    return "Off-session"


# ── Sample confidence ─────────────────────────────────────────────────────────

def _classify_sample(n: int) -> tuple[SampleSize, str]:
    if n == 0:
        return "low", "No completed trades yet — log trade outcomes to build real analytics. All metrics are zero until real data exists."
    if n < 20:
        return "low", f"Low confidence — only {n} completed trade{'s' if n != 1 else ''}. Need 20+ for reliable statistics."
    if n < 50:
        return "developing", f"Developing sample — {n} completed trades. Results are directionally useful but not statistically robust."
    if n < 100:
        return "useful", f"Useful sample — {n} completed trades. Metrics are meaningful; continue building the record."
    return "strong", f"Strong sample — {n} completed trades. Statistics are reliable."


# ── Max drawdown ──────────────────────────────────────────────────────────────

def _max_drawdown(pips_series: list[int]) -> int:
    """Largest peak-to-trough decline on the cumulative pips curve."""
    if not pips_series:
        return 0
    cum, running = [], 0
    for p in pips_series:
        running += p
        cum.append(running)
    peak = cum[0]
    max_dd = 0
    for v in cum:
        if v > peak:
            peak = v
        dd = v - peak
        if dd < max_dd:
            max_dd = dd
    return int(max_dd)


# ── Consecutive run counter ───────────────────────────────────────────────────

def _consecutive(results: list[str]) -> tuple[int, int]:
    """Returns (max_consecutive_wins, max_consecutive_losses)."""
    max_w = max_l = cur_w = cur_l = 0
    for r in results:
        if r == "WIN":
            cur_w += 1; cur_l = 0
            if cur_w > max_w: max_w = cur_w
        elif r == "LOSS":
            cur_l += 1; cur_w = 0
            if cur_l > max_l: max_l = cur_l
        else:
            cur_w = cur_l = 0
    return max_w, max_l


# ── Session breakdown ─────────────────────────────────────────────────────────

def _session_breakdown(completed: list[Any]) -> tuple[list[SessionStat], str | None, str | None]:
    groups: dict[str, list[Any]] = {}
    for t in completed:
        key = classify_session(getattr(t, "session", "") or "")
        groups.setdefault(key, []).append(t)

    stats: list[SessionStat] = []
    for sess in CANONICAL_SESSIONS:
        ts = groups.get(sess, [])
        if not ts:
            continue
        wins = [t for t in ts if getattr(t, "result", None) == "WIN"]
        total_pips = sum(getattr(t, "pips", None) or 0 for t in ts)
        stats.append(SessionStat(
            session=sess,
            trades=len(ts),
            wins=len(wins),
            win_rate=round(len(wins) / len(ts) * 100, 1),
            avg_pips=round(total_pips / len(ts), 1),
        ))

    scored = [s for s in stats if s.trades >= 3]
    best   = max(scored, key=lambda s: s.win_rate).session if scored else None
    worst  = min(scored, key=lambda s: s.win_rate).session if scored else None
    return stats, best, worst


# ── Equity curve ──────────────────────────────────────────────────────────────

def _equity_curve(completed: list[Any]) -> list[EquityCurvePoint]:
    points, running = [], 0
    for i, t in enumerate(completed, 1):
        pips = int(getattr(t, "pips", None) or 0)
        running += pips
        points.append(EquityCurvePoint(trade=i, pips=pips, cumulative_pips=running))
    return points


# ── Zero-state result ─────────────────────────────────────────────────────────

def _zero_state(total_signals: int, confirmed: int) -> AnalyticsResult:
    size, msg = _classify_sample(0)
    return AnalyticsResult(
        sample_size=size, completed_trades=0, sample_message=msg,
        total_signals=total_signals, confirmed_trades=confirmed,
        win_rate=0.0, loss_rate=0.0,
        average_win_pips=0.0, average_loss_pips=0.0,
        average_rr=None, average_pips=0.0,
        expectancy=0.0, profit_factor=None,
        max_drawdown=0, consecutive_wins=0, consecutive_losses=0,
        long_win_rate=None, short_win_rate=None,
        news_day_win_rate=None, non_news_day_win_rate=None,
        best_session=None, worst_session=None,
        session_breakdown=[], equity_curve=[],
    )


# ── Master computation ────────────────────────────────────────────────────────

def compute_analytics(
    all_trades: list[Any],
    total_signals: int = 0,
    confirmed_trades: int = 0,
) -> AnalyticsResult:
    """
    Compute full analytics from a list of trade objects.

    Parameters
    ----------
    all_trades       All trades that have a non-null result (WIN / LOSS / BREAKEVEN).
    total_signals    Total signals generated (from DB count or mock total).
    confirmed_trades Total confirmed signals (regardless of result).
    """
    completed = [
        t for t in all_trades
        if getattr(t, "result", None) in ("WIN", "LOSS", "BREAKEVEN")
    ]
    n = len(completed)

    if n == 0:
        return _zero_state(total_signals, confirmed_trades)

    wins   = [t for t in completed if t.result == "WIN"]
    losses = [t for t in completed if t.result == "LOSS"]

    win_rate  = round(len(wins) / n * 100, 1)
    loss_rate = round(len(losses) / n * 100, 1)

    avg_win_pips  = round(sum(t.pips or 0 for t in wins)   / len(wins),  1) if wins   else 0.0
    avg_loss_pips = round(abs(sum(t.pips or 0 for t in losses)) / len(losses), 1) if losses else 0.0

    # Expectancy = (win_rate_decimal × avg_win) − (loss_rate_decimal × avg_loss)
    expectancy = round((win_rate / 100) * avg_win_pips - (loss_rate / 100) * avg_loss_pips, 1)

    # Profit factor = total winning pips / |total losing pips|
    total_win_pips  = sum(t.pips or 0 for t in wins)
    total_loss_pips = abs(sum(t.pips or 0 for t in losses))
    profit_factor   = round(total_win_pips / total_loss_pips, 2) if total_loss_pips > 0 else None

    all_pips  = [t.pips or 0 for t in completed]
    avg_pips  = round(sum(all_pips) / n, 1)
    max_dd    = _max_drawdown(all_pips)

    rr_vals  = [t.rr for t in completed if getattr(t, "rr", None) is not None]
    avg_rr   = round(sum(rr_vals) / len(rr_vals), 2) if rr_vals else None

    results         = [t.result for t in completed]
    consec_w, consec_l = _consecutive(results)

    # Directional — handles both DB (signal=) and mock (direction=) field names
    def _dir(t: Any) -> str:
        return getattr(t, "signal", None) or getattr(t, "direction", "")

    longs  = [t for t in completed if _dir(t) == "BUY"]
    shorts = [t for t in completed if _dir(t) == "SELL"]
    long_wr  = round(sum(1 for t in longs  if t.result == "WIN") / len(longs)  * 100, 1) if longs  else None
    short_wr = round(sum(1 for t in shorts if t.result == "WIN") / len(shorts) * 100, 1) if shorts else None

    # News-day performance
    news_t     = [t for t in completed if getattr(t, "news_status", "CLEAR") == "BLOCKED"]
    non_news_t = [t for t in completed if getattr(t, "news_status", "CLEAR") == "CLEAR"]
    news_wr     = round(sum(1 for t in news_t     if t.result == "WIN") / len(news_t)     * 100, 1) if news_t     else None
    non_news_wr = round(sum(1 for t in non_news_t if t.result == "WIN") / len(non_news_t) * 100, 1) if non_news_t else None

    session_stats, best_sess, worst_sess = _session_breakdown(completed)
    equity = _equity_curve(completed)
    size, msg = _classify_sample(n)

    return AnalyticsResult(
        sample_size=size, completed_trades=n, sample_message=msg,
        total_signals=total_signals, confirmed_trades=confirmed_trades,
        win_rate=win_rate, loss_rate=loss_rate,
        average_win_pips=avg_win_pips, average_loss_pips=avg_loss_pips,
        average_rr=avg_rr, average_pips=avg_pips,
        expectancy=expectancy, profit_factor=profit_factor,
        max_drawdown=max_dd, consecutive_wins=consec_w, consecutive_losses=consec_l,
        long_win_rate=long_wr, short_win_rate=short_wr,
        news_day_win_rate=news_wr, non_news_day_win_rate=non_news_wr,
        best_session=best_sess, worst_session=worst_sess,
        session_breakdown=session_stats, equity_curve=equity,
    )
