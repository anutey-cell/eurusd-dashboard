"""
Non-ICT intraday strategies for XAU/USD M15.

Built after the ICT-based intraday engine (intraday_engine.py) was shown
to have no edge across 2 years of real Exness M15 data. This module
explores fundamentally different strategy archetypes:

  1. trend_pullback  — EMA21/50 trend follow with pullback entries
  2. bb_reversion    — Bollinger Band mean reversion (fade extremes)
  3. opening_range   — London/NY first-hour opening range breakout
  4. asian_fade      — Fade the FIRST touch of Asian session H/L during London

Each strategy:
  - Returns a SignalResult shape-compatible with the swing/intraday engines
  - Has its own internal target_pips / SL logic tuned to the setup type
  - Respects the standard news filter + killzone constraints

Safety: pure analysis. No trades placed. No Telegram alerts. Read-only.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

from services.signal_engine import (
    SignalResult, HTFResult, LiqResult, MSResult, FVGResult,
    NewsResult, SessionResult,
    detect_session, check_news_risk,
)

log = logging.getLogger(__name__)


# ─── Math helpers ─────────────────────────────────────────────────────────────

def _ema(values: list[float], period: int) -> list[float]:
    if len(values) < period:
        return [values[-1]] * len(values)
    k = 2 / (period + 1)
    sma = sum(values[:period]) / period
    out = [sma]
    for v in values[period:]:
        out.append(v * k + out[-1] * (1 - k))
    return [out[0]] * (period - 1) + out


def _bollinger(closes: list[float], period: int = 20, std_mult: float = 2.0) -> tuple[float, float, float]:
    if len(closes) < period:
        c = closes[-1]
        return c, c, c
    recent = closes[-period:]
    mid = sum(recent) / period
    var = sum((v - mid) ** 2 for v in recent) / period
    sd = var ** 0.5
    return mid - std_mult * sd, mid, mid + std_mult * sd


def _atr(candles, period: int = 14, pip_size: float = 1.0) -> float:
    if len(candles) < period:
        return 0.0
    bars = candles[-period:]
    return sum((c.high - c.low) / pip_size for c in bars) / period


def _candle_time(c) -> datetime:
    t = c.time
    return t if t.tzinfo else t.replace(tzinfo=timezone.utc)


def _in_killzone(at: datetime) -> bool:
    h = at.hour + at.minute / 60
    return (6.5 <= h <= 11.0) or (12.5 <= h <= 17.0)


# ─── Result builders ──────────────────────────────────────────────────────────

def _wait(reason: str, at: datetime, setup: str = "no_signal") -> SignalResult:
    sess = detect_session(at=at)
    return SignalResult(
        signal="WAIT", quality_score=0,
        entry=None, stop_loss=None, take_profit=None,
        risk_pips=None, target_pips=0, rr=None,
        invalidation=None, reason=reason, news_status="CLEAR",
        model={
            "higherTimeframeBias": "—", "liquidity": "—",
            "structure": "—", "fvg": "—", "session": sess.session_text,
            "engineVariant": setup,
        },
        htf=HTFResult(bullish=False, score=0, ema21=0, ema50=0,
                       structure="—", bias_text="N/A"),
        liq=LiqResult(swept=False, bullish=False, score=0,
                       swept_level=0, liq_text="N/A"),
        ms=MSResult(shifted=False, bullish=False, score=0,
                     pattern="None", structure_text="N/A"),
        fvg=FVGResult(detected=False, bullish=False, score=0,
                       gap_low=0, gap_high=0, in_zone=False, fvg_text="N/A"),
        news=NewsResult(clear=True, score=0, blocking_event="", status="CLEAR"),
        sess=sess,
        pair="xauusd", display_pair="XAU/USD",
        data_source="non-ict",
        setup_type=setup,
    )


def _fire(direction: str, entry: float, sl: float, tp: float,
           risk_pts: float, target_pts: float, rr: float,
           reason: str, at: datetime, news: NewsResult,
           setup: str, score: int = 75) -> SignalResult:
    sess = detect_session(at=at)
    return SignalResult(
        signal=direction, quality_score=score,
        entry=round(entry, 2), stop_loss=round(sl, 2), take_profit=round(tp, 2),
        risk_pips=max(1, int(round(risk_pts))), target_pips=max(1, int(round(target_pts))),
        rr=round(rr, 2), invalidation=round(sl, 2),
        reason=reason, news_status=news.status,
        model={
            "higherTimeframeBias": "—", "liquidity": "—",
            "structure": setup, "fvg": "—", "session": sess.session_text,
            "engineVariant": setup,
        },
        htf=HTFResult(bullish=(direction == "BUY"), score=0,
                       ema21=0, ema50=0, structure="—", bias_text="N/A"),
        liq=LiqResult(swept=False, bullish=False, score=0,
                       swept_level=0, liq_text="N/A"),
        ms=MSResult(shifted=True, bullish=(direction == "BUY"), score=0,
                     pattern=setup, structure_text=reason),
        fvg=FVGResult(detected=False, bullish=False, score=0,
                       gap_low=0, gap_high=0, in_zone=False, fvg_text="N/A"),
        news=news, sess=sess,
        pair="xauusd", display_pair="XAU/USD",
        data_source="non-ict",
        setup_type=setup,
    )


# ═══════════════════════════════════════════════════════════════════════════════
# 1. TREND PULLBACK — EMA21/50 trend with pullback entries
# ═══════════════════════════════════════════════════════════════════════════════

def analyze_trend_pullback(
    candles, at: datetime, macro_events: list[dict] | None = None,
    pip_size: float = 1.0,
    target_pips: int = 30,           # achievable on M15
    max_sl_pips: float = 15,         # reject if SL would exceed this
    min_rr: float = 2.0,
    enable_killzone: bool = True,
    enable_news_filter: bool = True,
    **_,
) -> SignalResult:
    """
    Classic trend-pullback intraday setup:
      1. EMA21 > EMA50 (uptrend) AND trend stable over last 3 bars
      2. Recent 5 bars pulled back to TOUCH EMA21 from above
      3. Current bar closes back above EMA21 AND above prev bar's high
      4. Entry: current close. SL: 1pt below pullback low. TP: target_pips above.
    """
    if not candles or len(candles) < 60:
        return _wait("Need >= 60 bars for EMA50", at, "trend_pullback")

    now = at or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    if enable_killzone and not _in_killzone(now):
        return _wait("Off-killzone", now, "trend_pullback")

    news = check_news_risk(macro_events or [], at=now, pair="xauusd")
    if enable_news_filter and not news.clear:
        return _wait(f"News blocked: {news.blocking_event}", now, "trend_pullback")

    closes = [c.close for c in candles]
    ema21 = _ema(closes, 21)
    ema50 = _ema(closes, 50)

    last = candles[-1]
    prev = candles[-2]

    # Trend filter: EMA21 > EMA50 for at least last 3 bars
    uptrend = (ema21[-1] > ema50[-1] and ema21[-2] > ema50[-2] and ema21[-3] > ema50[-3])
    downtrend = (ema21[-1] < ema50[-1] and ema21[-2] < ema50[-2] and ema21[-3] < ema50[-3])

    if not (uptrend or downtrend):
        return _wait("No stable EMA trend", now, "trend_pullback")

    e21 = ema21[-1]

    if uptrend:
        # Pullback: at least one of last 5 bars had LOW <= ema21 (touched/undercut)
        pullback_lows = [c.low for c in candles[-5:]]
        recent_low = min(pullback_lows)
        if recent_low > e21 * 1.002:
            return _wait("No pullback to EMA21 (uptrend)", now, "trend_pullback")
        # Reversal: current closes ABOVE ema21 AND above prev high
        if last.close > e21 and last.close > prev.high:
            entry = last.close
            sl    = recent_low - 1.0 * pip_size
            tp    = entry + target_pips * pip_size
            risk  = (entry - sl) / pip_size
            if risk <= 0 or risk > max_sl_pips:
                return _wait(f"SL too wide ({risk:.1f}pt > {max_sl_pips})",
                              now, "trend_pullback")
            rr = target_pips / risk
            if rr < min_rr:
                return _wait(f"RR {rr:.2f} < {min_rr}", now, "trend_pullback")
            return _fire(
                "BUY", entry, sl, tp, risk, target_pips, rr,
                f"Trend pullback BUY: EMA21 {e21:.2f}, recovered above prev high {prev.high:.2f}",
                now, news, "trend_pullback", score=75,
            )

    if downtrend:
        pullback_highs = [c.high for c in candles[-5:]]
        recent_high = max(pullback_highs)
        if recent_high < e21 * 0.998:
            return _wait("No pullback to EMA21 (downtrend)", now, "trend_pullback")
        if last.close < e21 and last.close < prev.low:
            entry = last.close
            sl    = recent_high + 1.0 * pip_size
            tp    = entry - target_pips * pip_size
            risk  = (sl - entry) / pip_size
            if risk <= 0 or risk > max_sl_pips:
                return _wait(f"SL too wide", now, "trend_pullback")
            rr = target_pips / risk
            if rr < min_rr:
                return _wait(f"RR {rr:.2f} < {min_rr}", now, "trend_pullback")
            return _fire(
                "SELL", entry, sl, tp, risk, target_pips, rr,
                f"Trend pullback SELL: EMA21 {e21:.2f}, broke below prev low {prev.low:.2f}",
                now, news, "trend_pullback", score=75,
            )

    return _wait("No reversal candle", now, "trend_pullback")


# ═══════════════════════════════════════════════════════════════════════════════
# 2. BOLLINGER BAND MEAN REVERSION — fade extremes back to mid
# ═══════════════════════════════════════════════════════════════════════════════

def analyze_bb_reversion(
    candles, at: datetime, macro_events: list[dict] | None = None,
    pip_size: float = 1.0,
    target_pips: int = 15,           # small intraday target
    max_sl_pips: float = 8,
    min_rr: float = 1.8,
    bb_period: int = 20,
    bb_std: float = 2.0,
    enable_killzone: bool = True,
    enable_news_filter: bool = True,
    **_,
) -> SignalResult:
    """
    Bollinger Band mean reversion:
      1. Previous candle CLOSED outside the band (price exhaustion)
      2. Current candle CLOSES back inside the band (reversal confirmed)
      3. Entry: current close. SL: 1pt beyond prev extreme. TP: middle band.
    Counter-trend by design — works best in range-bound conditions.
    """
    if not candles or len(candles) < bb_period + 5:
        return _wait("Need more bars for BB", at, "bb_reversion")

    now = at or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    if enable_killzone and not _in_killzone(now):
        return _wait("Off-killzone", now, "bb_reversion")

    news = check_news_risk(macro_events or [], at=now, pair="xauusd")
    if enable_news_filter and not news.clear:
        return _wait("News blocked", now, "bb_reversion")

    closes = [c.close for c in candles]
    bb_low, bb_mid, bb_high = _bollinger(closes, bb_period, bb_std)
    band_width = bb_high - bb_low
    if band_width < 4.0 * pip_size:
        return _wait("BB too tight — no edge", now, "bb_reversion")

    last = candles[-1]
    prev = candles[-2]

    # SELL setup: prev closed above upper, current closed back below
    if prev.close > bb_high and last.close < bb_high:
        entry = last.close
        sl    = prev.high + 1.0 * pip_size
        tp    = bb_mid
        # Use target_pips as fallback if mid is too close
        if (entry - tp) < target_pips * pip_size * 0.5:
            tp = entry - target_pips * pip_size
        risk = (sl - entry) / pip_size
        reward = (entry - tp) / pip_size
        if risk <= 0 or reward <= 0:
            return _wait("Invalid geometry", now, "bb_reversion")
        if risk > max_sl_pips:
            return _wait(f"SL too wide ({risk:.1f}pt)", now, "bb_reversion")
        rr = reward / risk
        if rr < min_rr:
            return _wait(f"RR {rr:.2f} < {min_rr}", now, "bb_reversion")
        return _fire(
            "SELL", entry, sl, tp, risk, reward, rr,
            f"BB upper rejection: prev close {prev.close:.2f} > {bb_high:.2f}, current back inside",
            now, news, "bb_reversion", score=70,
        )

    # BUY setup: prev closed below lower, current closed back above
    if prev.close < bb_low and last.close > bb_low:
        entry = last.close
        sl    = prev.low - 1.0 * pip_size
        tp    = bb_mid
        if (tp - entry) < target_pips * pip_size * 0.5:
            tp = entry + target_pips * pip_size
        risk = (entry - sl) / pip_size
        reward = (tp - entry) / pip_size
        if risk <= 0 or reward <= 0:
            return _wait("Invalid geometry", now, "bb_reversion")
        if risk > max_sl_pips:
            return _wait(f"SL too wide", now, "bb_reversion")
        rr = reward / risk
        if rr < min_rr:
            return _wait(f"RR {rr:.2f} < {min_rr}", now, "bb_reversion")
        return _fire(
            "BUY", entry, sl, tp, risk, reward, rr,
            f"BB lower rejection: prev close {prev.close:.2f} < {bb_low:.2f}, current back inside",
            now, news, "bb_reversion", score=70,
        )

    return _wait("No BB rejection", now, "bb_reversion")


# ═══════════════════════════════════════════════════════════════════════════════
# 3. OPENING RANGE BREAKOUT — London/NY first-hour break
# ═══════════════════════════════════════════════════════════════════════════════

def analyze_opening_range_breakout(
    candles, at: datetime, macro_events: list[dict] | None = None,
    pip_size: float = 1.0,
    target_pips: int = 40,
    max_sl_pips: float = 20,
    min_rr: float = 1.8,
    or_minutes: int = 60,
    london_open_h: float = 7.0,
    ny_open_h: float = 12.0,
    enable_news_filter: bool = True,
    **_,
) -> SignalResult:
    """
    Opening Range Breakout (ORB):
      1. Define OR = first hour of London (07-08 UTC) OR NY (12-13 UTC)
      2. After OR ends, watch for first candle to CLOSE beyond OR high/low
      3. Entry on breakout close. SL = opposite end of OR. TP = target_pips.
      4. Only valid for a few hours after OR ends.
    """
    if not candles or len(candles) < 30:
        return _wait("Insufficient bars", at, "opening_range")

    now = at or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    h = now.hour + now.minute / 60

    london_or_end  = london_open_h + or_minutes / 60      # 08:00
    london_trade_end = 11.0
    ny_or_end      = ny_open_h + or_minutes / 60          # 13:00
    ny_trade_end   = 17.0

    today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    or_high = or_low = None
    or_label = None

    # Which OR window are we in?
    if london_or_end <= h <= london_trade_end:
        or_start_t = today.replace(hour=int(london_open_h))
        or_end_t   = today.replace(hour=int(london_or_end))
        or_label = "London"
    elif ny_or_end <= h <= ny_trade_end:
        or_start_t = today.replace(hour=int(ny_open_h))
        or_end_t   = today.replace(hour=int(ny_or_end))
        or_label = "NY"
    else:
        return _wait(f"Outside OR trade window (h={h:.1f})", now, "opening_range")

    or_bars = [c for c in candles
                if or_start_t <= _candle_time(c) < or_end_t]
    if not or_bars:
        return _wait(f"No {or_label} OR bars in window", now, "opening_range")

    or_high = max(c.high for c in or_bars)
    or_low  = min(c.low  for c in or_bars)
    or_size = (or_high - or_low) / pip_size
    if or_size < 5.0 or or_size > 50.0:
        return _wait(f"OR size {or_size:.1f}pt outside [5, 50]", now, "opening_range")

    news = check_news_risk(macro_events or [], at=now, pair="xauusd")
    if enable_news_filter and not news.clear:
        return _wait("News blocked", now, "opening_range")

    last = candles[-1]
    prev = candles[-2]

    # First breakout above OR high
    if last.close > or_high and prev.close <= or_high:
        entry = last.close
        sl    = or_low - 1.0 * pip_size      # opposite side of OR
        tp    = entry + target_pips * pip_size
        risk  = (entry - sl) / pip_size
        if risk <= 0 or risk > max_sl_pips:
            return _wait(f"SL too wide ({risk:.1f}pt)", now, "opening_range")
        rr = target_pips / risk
        if rr < min_rr:
            return _wait(f"RR {rr:.2f} < {min_rr}", now, "opening_range")
        return _fire(
            "BUY", entry, sl, tp, risk, target_pips, rr,
            f"{or_label} ORB above {or_high:.2f} (OR size {or_size:.1f}pt)",
            now, news, "opening_range", score=72,
        )

    # First breakdown below OR low
    if last.close < or_low and prev.close >= or_low:
        entry = last.close
        sl    = or_high + 1.0 * pip_size
        tp    = entry - target_pips * pip_size
        risk  = (sl - entry) / pip_size
        if risk <= 0 or risk > max_sl_pips:
            return _wait(f"SL too wide", now, "opening_range")
        rr = target_pips / risk
        if rr < min_rr:
            return _wait(f"RR {rr:.2f} < {min_rr}", now, "opening_range")
        return _fire(
            "SELL", entry, sl, tp, risk, target_pips, rr,
            f"{or_label} ORB below {or_low:.2f} (OR size {or_size:.1f}pt)",
            now, news, "opening_range", score=72,
        )

    return _wait("No first-touch breakout", now, "opening_range")


# ═══════════════════════════════════════════════════════════════════════════════
# 4. ASIAN RANGE FADE — fade FIRST touch of Asian H/L during London
# ═══════════════════════════════════════════════════════════════════════════════
# Different from the ICT "break and retest" — this FADES the first touch,
# betting on mean reversion back to Asian mid-range. Pure counter-trend.

def analyze_asian_range_fade(
    candles, at: datetime, macro_events: list[dict] | None = None,
    pip_size: float = 1.0,
    target_pips: int = 20,
    max_sl_pips: float = 10,
    min_rr: float = 1.8,
    asian_start_h: float = 0.0,
    asian_end_h:   float = 7.0,
    fade_window_end_h: float = 11.0,
    enable_news_filter: bool = True,
    **_,
) -> SignalResult:
    """
    Fade Asian-session extremes during London.

    Logic:
      1. Compute today's Asian H/L (00:00-07:00 UTC)
      2. Wait inside London window (07:00-11:00 UTC)
      3. When current candle WICKS to/beyond Asian H but CLOSES BELOW it,
         enter SELL (fade the failed breakout)
      4. Mirror for Asian L → BUY
      5. SL: 1pt beyond the wick extreme. TP: Asian mid-range.
    """
    if not candles or len(candles) < 30:
        return _wait("Insufficient bars", at, "asian_fade")

    now = at or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    h = now.hour + now.minute / 60

    if not (asian_end_h <= h <= fade_window_end_h):
        return _wait(f"Outside fade window ({asian_end_h:.0f}-{fade_window_end_h:.0f} UTC)",
                      now, "asian_fade")

    news = check_news_risk(macro_events or [], at=now, pair="xauusd")
    if enable_news_filter and not news.clear:
        return _wait("News blocked", now, "asian_fade")

    today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    asian_start_t = today.replace(hour=int(asian_start_h))
    asian_end_t   = today.replace(hour=int(asian_end_h))

    asian_bars = [c for c in candles
                   if asian_start_t <= _candle_time(c) < asian_end_t]
    if len(asian_bars) < 4:
        return _wait("Insufficient Asian data", now, "asian_fade")

    asian_h   = max(c.high for c in asian_bars)
    asian_l   = min(c.low  for c in asian_bars)
    asian_mid = (asian_h + asian_l) / 2
    asian_range = (asian_h - asian_l) / pip_size

    if asian_range < 3.0 or asian_range > 35.0:
        return _wait(f"Asian range {asian_range:.1f}pt outside [3, 35]",
                      now, "asian_fade")

    last = candles[-1]
    prev = candles[-2]

    # FADE Asian H: current candle wicks above asian_h, closes below it,
    # AND prev candle was still below asian_h (so this is the first touch)
    if last.high >= asian_h and last.close < asian_h and prev.close < asian_h - 0.5 * pip_size:
        entry = last.close
        sl    = last.high + 1.0 * pip_size
        tp    = asian_mid
        if (entry - tp) < target_pips * pip_size * 0.6:
            tp = entry - target_pips * pip_size
        risk   = (sl - entry) / pip_size
        reward = (entry - tp) / pip_size
        if risk <= 0 or reward <= 0:
            return _wait("Invalid geometry", now, "asian_fade")
        if risk > max_sl_pips:
            return _wait(f"SL too wide ({risk:.1f}pt)", now, "asian_fade")
        rr = reward / risk
        if rr < min_rr:
            return _wait(f"RR {rr:.2f} < {min_rr}", now, "asian_fade")
        return _fire(
            "SELL", entry, sl, tp, risk, reward, rr,
            f"Fade Asian H {asian_h:.2f} first-touch wick (range {asian_range:.1f}pt)",
            now, news, "asian_fade", score=72,
        )

    # FADE Asian L
    if last.low <= asian_l and last.close > asian_l and prev.close > asian_l + 0.5 * pip_size:
        entry = last.close
        sl    = last.low - 1.0 * pip_size
        tp    = asian_mid
        if (tp - entry) < target_pips * pip_size * 0.6:
            tp = entry + target_pips * pip_size
        risk   = (entry - sl) / pip_size
        reward = (tp - entry) / pip_size
        if risk <= 0 or reward <= 0:
            return _wait("Invalid geometry", now, "asian_fade")
        if risk > max_sl_pips:
            return _wait(f"SL too wide", now, "asian_fade")
        rr = reward / risk
        if rr < min_rr:
            return _wait(f"RR {rr:.2f} < {min_rr}", now, "asian_fade")
        return _fire(
            "BUY", entry, sl, tp, risk, reward, rr,
            f"Fade Asian L {asian_l:.2f} first-touch wick (range {asian_range:.1f}pt)",
            now, news, "asian_fade", score=72,
        )

    return _wait("No Asian extreme rejection yet", now, "asian_fade")


# ═══════════════════════════════════════════════════════════════════════════════
# Dispatcher
# ═══════════════════════════════════════════════════════════════════════════════

STRATEGY_FUNCS = {
    "trend_pullback":  analyze_trend_pullback,
    "bb_reversion":    analyze_bb_reversion,
    "opening_range":   analyze_opening_range_breakout,
    "asian_fade":      analyze_asian_range_fade,
}


def analyze_non_ict_strategy(variant: str, **kwargs) -> SignalResult:
    """Dispatch to one of the 4 non-ICT strategies by name."""
    fn = STRATEGY_FUNCS.get(variant)
    if fn is None:
        raise ValueError(f"Unknown non-ICT strategy: {variant}. "
                         f"Available: {list(STRATEGY_FUNCS.keys())}")
    return fn(**kwargs)
