# -*- coding: utf-8 -*-
"""
ICT / Smart Money Concepts signal engine — EUR/USD and XAU/USD.

Output format (flat):
  signal          BUY | SELL | WAIT
  qualityScore    0-100
  entry           price or null
  stopLoss        price or null
  takeProfit      price or null
  riskPips        int or null
  targetPips      int (pair-specific)
  rr              float or null  (targetPips / riskPips)
  invalidation    same as stopLoss
  reason          specific gate failure (not a generic message)
  newsStatus      CLEAR | BLOCKED
  model           { higherTimeframeBias, liquidity, structure, fvg, session }

Scoring model (100 pts):
  HTF Bias            15 pts  -- EMA-21/50 alignment + HH/HL pivot structure
  Liquidity Sweep     20 pts  -- stop-hunt wick beyond swing that reverses (>=3 pip)
  Market Structure    20 pts  -- BOS / CHoCH via 3-bar pivots (3-bar lookback)
  Fair Value Gap      20 pts  -- 3-candle imbalance >= pair fvg_min_pips
  News Risk           15 pts  -- 0 if inside blackout window
  Session Timing      10 pts  -- London / NY / Overlap active; 0 Asian / Off

Gate sequence (ALL must pass for BUY or SELL):
  1. news_clear          -- no blackout window active
  2. score >= 80         -- minimum confluence
  3. liquidity_swept     -- stop hunt confirmed with score >= 15
  4. structure_shifted   -- BOS or CHoCH in signal direction
  5. fvg_detected        -- entry trigger present
  6. directional_agree   -- >= 3 of 4 direction votes agree
  7. rr >= min_rr        -- risk-reward viable

Liquidity labeling (ICT correct):
  Wick below swing low + close above  -> "Sell-side liquidity swept" (bullish)
  Wick above swing high + close below -> "Buy-side liquidity swept"  (bearish)

Trade geometry:
  target     = target_pips * pip_size
  BUY  SL    = swept_low  - sl_buffer
  SELL SL    = swept_high + sl_buffer
  BUY  TP    = entry + target
  SELL TP    = entry - target
  RR         = target_pips / riskPips  (must be >= min_rr)

News blackout:
  Major events (CPI, NFP, FOMC, ECB): 60 min before -> 30 min after
  Other high-impact EUR/USD events:    30 min before -> 15 min after
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

PIP            = 0.0001
TARGET_PIPS    = 40
TARGET         = TARGET_PIPS * PIP
SL_BUFFER_PIPS = 3
SL_BUFFER      = SL_BUFFER_PIPS * PIP
MIN_RR         = 2.5

# Minimum sweep wick size for a "strong" sweep (full 20 pts)
STRONG_WICK_PIPS = 3

# Major news keywords (extended blackout)
MAJOR_EVENT_KEYWORDS = frozenset({"CPI", "NFP", "FOMC", "ECB"})

# How many recent candles to scan as sweep candidates
SWEEP_CANDIDATE_BARS = 5
# Reference window for swing high/low (bars before candidates)
SWEEP_REF_BARS       = 50

# Structure break lookback (bars)
STRUCTURE_LOOKBACK   = 3


# ---------------------------------------------------------------------------
# Internal data structures
# ---------------------------------------------------------------------------

@dataclass
class Candle:
    time:   datetime
    open:   float
    high:   float
    low:    float
    close:  float
    volume: int


@dataclass
class HTFResult:
    bullish:    bool
    score:      int
    ema21:      float
    ema50:      float
    structure:  str       # "HH/HL" | "LH/LL" | "Mixed"
    bias_text:  str       # "Bullish (HH/HL)" | "Bearish (LH/LL)" etc.


@dataclass
class LiqResult:
    swept:        bool
    bullish:      bool    # True = sell-side swept (bullish reversal expected)
    score:        int
    swept_level:  float
    liq_text:     str


@dataclass
class MSResult:
    shifted:        bool
    bullish:        bool
    score:          int
    pattern:        str   # "BOS" | "CHoCH" | "None"
    structure_text: str


@dataclass
class FVGResult:
    detected:  bool
    bullish:   bool
    score:     int
    gap_low:   float
    gap_high:  float
    in_zone:   bool
    fvg_text:  str


@dataclass
class NewsResult:
    clear:          bool
    score:          int
    blocking_event: str
    status:         str   # "CLEAR" | "BLOCKED"


@dataclass
class SessionResult:
    score:        int
    session:      str
    in_kill_zone: bool
    session_text: str


@dataclass
class SignalResult:
    signal:       str
    quality_score: int
    entry:        float | None
    stop_loss:    float | None
    take_profit:  float | None
    risk_pips:    int   | None
    target_pips:  int
    rr:           float | None
    invalidation: float | None
    reason:       str
    news_status:  str
    model:        dict
    htf:  HTFResult
    liq:  LiqResult
    ms:   MSResult
    fvg:  FVGResult
    news: NewsResult
    sess: SessionResult
    pair:               str  = "eurusd"
    display_pair:       str  = "EUR/USD"
    component_snapshot: str  = "{}"    # JSON — which ICT gates were active
    weights_used:       dict = None    # adaptive weights applied (None = base weights)
    data_source:        str  = "synthetic"  # "live" | "synthetic"


# ---------------------------------------------------------------------------
# EMA helper
# ---------------------------------------------------------------------------

def _ema(values: list[float], period: int) -> list[float]:
    if len(values) < period:
        return [values[-1]] * len(values)
    k   = 2 / (period + 1)
    sma = sum(values[:period]) / period
    result = [sma]
    for v in values[period:]:
        result.append(v * k + result[-1] * (1 - k))
    return [result[0]] * (period - 1) + result


# ---------------------------------------------------------------------------
# Pivot helpers
# ---------------------------------------------------------------------------

def _pivot_highs(vals: list[float]) -> list[tuple[int, float]]:
    return [
        (i, vals[i]) for i in range(1, len(vals) - 1)
        if vals[i] > vals[i - 1] and vals[i] > vals[i + 1]
    ]


def _pivot_lows(vals: list[float]) -> list[tuple[int, float]]:
    return [
        (i, vals[i]) for i in range(1, len(vals) - 1)
        if vals[i] < vals[i - 1] and vals[i] < vals[i + 1]
    ]


# ---------------------------------------------------------------------------
# 1. Higher-timeframe bias  (0-15 pts)
# ---------------------------------------------------------------------------

def detect_higher_timeframe_bias(candles: list[Candle]) -> HTFResult:
    closes = [c.close for c in candles]

    if len(closes) < 51:
        mid = closes[-1]
        return HTFResult(bullish=True, score=5, ema21=mid, ema50=mid,
                         structure="Mixed", bias_text="Neutral (insufficient data)")

    ema21 = _ema(closes, 21)
    ema50 = _ema(closes, 50)
    bullish_ema = ema21[-1] > ema50[-1]
    # Confirm trend is stable (same side 4 bars ago)
    stable = (ema21[-4] > ema50[-4]) == bullish_ema

    highs = [c.high for c in candles[-20:]]
    lows  = [c.low  for c in candles[-20:]]
    ph = _pivot_highs(highs)
    pl = _pivot_lows(lows)

    hh_hl = (len(ph) >= 2 and ph[-1][1] > ph[-2][1]
              and len(pl) >= 2 and pl[-1][1] > pl[-2][1])
    lh_ll = (len(ph) >= 2 and ph[-1][1] < ph[-2][1]
              and len(pl) >= 2 and pl[-1][1] < pl[-2][1])
    structure = "HH/HL" if hh_hl else "LH/LL" if lh_ll else "Mixed"

    structural_confirm = (bullish_ema and hh_hl) or (not bullish_ema and lh_ll)
    score = 15 if (structural_confirm and stable) else (8 if bullish_ema else 5)
    bias_text = "Bullish" if bullish_ema else "Bearish"

    return HTFResult(
        bullish   = bullish_ema,
        score     = score,
        ema21     = round(ema21[-1], 5),
        ema50     = round(ema50[-1], 5),
        structure = structure,
        bias_text = f"{bias_text} ({structure})",
    )


# ---------------------------------------------------------------------------
# 2. Liquidity sweep  (0-20 pts)
#
# FIX #2  : Correct ICT labeling
#   Wick BELOW swing low, close ABOVE -> sell-side liquidity swept (bullish)
#   Wick ABOVE swing high, close BELOW -> buy-side liquidity swept (bearish)
#
# FIX #3  : Widen candidate window from last 2 bars to last SWEEP_CANDIDATE_BARS
# FIX #10 : Add strength gate -- partial wick (< 3 pips) scores 10, not 20
# ---------------------------------------------------------------------------

def detect_liquidity_sweep(candles: list[Candle], pip_size: float = PIP) -> LiqResult:
    _null = LiqResult(swept=False, bullish=True, score=0,
                      swept_level=0.0, liq_text="No liquidity sweep detected")
    if len(candles) < SWEEP_CANDIDATE_BARS + 10:
        return _null

    ref_end   = -(SWEEP_CANDIDATE_BARS)
    ref        = candles[:ref_end][-SWEEP_REF_BARS:]   # reference swing range
    candidates = candles[ref_end:]                      # most recent bars (FIX #3)

    if not ref:
        return _null

    swing_high = max(c.high for c in ref)
    swing_low  = min(c.low  for c in ref)

    for c in reversed(candidates):
        # --- Sell-side liquidity swept (bullish reversal) ---
        wick_lo = c.low < swing_low and c.close > swing_low
        if wick_lo:
            wick_pips = (c.close - c.low) / pip_size
            score = 20 if wick_pips >= STRONG_WICK_PIPS else 10
            return LiqResult(
                swept        = True,
                bullish      = True,
                score        = score,
                swept_level  = round(swing_low, 5),
                liq_text     = (
                    f"Sell-side liquidity swept below {swing_low:.5f}"
                    f" ({wick_pips:.1f} pip wick)"
                ),
            )

        # --- Buy-side liquidity swept (bearish reversal) ---
        wick_hi = c.high > swing_high and c.close < swing_high
        if wick_hi:
            wick_pips = (c.high - c.close) / pip_size
            score = 20 if wick_pips >= STRONG_WICK_PIPS else 10
            return LiqResult(
                swept        = True,
                bullish      = False,
                score        = score,
                swept_level  = round(swing_high, 5),
                liq_text     = (
                    f"Buy-side liquidity swept above {swing_high:.5f}"
                    f" ({wick_pips:.1f} pip wick)"
                ),
            )

    return _null


# ---------------------------------------------------------------------------
# 3. Market structure  (0-20 pts)
#
# FIX #4  : 3-bar lookback for structure break (was exact single-bar only)
# FIX #9  : "Waiting" text now reflects the HTF direction context
# ---------------------------------------------------------------------------

def detect_market_structure(
    candles: list[Candle],
    htf_bullish: bool = True,
) -> MSResult:
    _null = MSResult(shifted=False, bullish=True, score=0,
                     pattern="None", structure_text="No structural break detected")
    if len(candles) < 15:
        return _null

    bars   = candles[-40:]
    highs  = [c.high  for c in bars]
    lows   = [c.low   for c in bars]
    closes = [c.close for c in bars]

    ph = _pivot_highs(highs)
    pl = _pivot_lows(lows)

    # FIX #4: check whether any of the last STRUCTURE_LOOKBACK closes crossed the pivot
    def _crossed_above(pivot_price: float) -> bool:
        n = len(closes)
        for j in range(max(1, n - STRUCTURE_LOOKBACK), n):
            if closes[j] > pivot_price and closes[j - 1] <= pivot_price:
                return True
        return False

    def _crossed_below(pivot_price: float) -> bool:
        n = len(closes)
        for j in range(max(1, n - STRUCTURE_LOOKBACK), n):
            if closes[j] < pivot_price and closes[j - 1] >= pivot_price:
                return True
        return False

    if ph:
        _, last_ph = ph[-1]
        if _crossed_above(last_ph):
            is_bos = len(ph) >= 2 and last_ph > ph[-2][1]
            pattern = "BOS" if is_bos else "CHoCH"
            score   = 20 if is_bos else 15
            return MSResult(
                shifted        = True,
                bullish        = True,
                score          = score,
                pattern        = pattern,
                structure_text = f"Bullish {pattern} confirmed above {last_ph:.5f}",
            )

    if pl:
        _, last_pl = pl[-1]
        if _crossed_below(last_pl):
            is_bos = len(pl) >= 2 and last_pl < pl[-2][1]
            pattern = "BOS" if is_bos else "CHoCH"
            score   = 20 if is_bos else 15
            return MSResult(
                shifted        = True,
                bullish        = False,
                score          = score,
                pattern        = pattern,
                structure_text = f"Bearish {pattern} confirmed below {last_pl:.5f}",
            )

    # FIX #9: waiting text reflects HTF context so it is directionally meaningful
    if htf_bullish:
        waiting_text = "Waiting for bullish CHoCH / BOS confirmation"
    else:
        waiting_text = "Waiting for bearish CHoCH / BOS confirmation"

    return MSResult(shifted=False, bullish=htf_bullish, score=0,
                    pattern="None", structure_text=waiting_text)


# ---------------------------------------------------------------------------
# 4. Fair Value Gap  (0-20 pts)
#
# FIX #1  : Replace en-dash/em-dash Unicode chars with ASCII to avoid Docker
#            UTF-8 mojibake (â€“ artifacts in API responses)
# ---------------------------------------------------------------------------

def detect_fair_value_gap(
    candles:      list[Candle],
    pip_size:     float = PIP,
    fvg_min_pips: int   = 3,
) -> FVGResult:
    _null = FVGResult(detected=False, bullish=False, score=0,
                      gap_low=0.0, gap_high=0.0, in_zone=False,
                      fvg_text="No Fair Value Gap detected")
    if len(candles) < 5:
        return _null

    price = candles[-1].close
    scan  = candles[max(0, len(candles) - 25):]

    for i in range(len(scan) - 1, 1, -1):
        c1, c3 = scan[i - 2], scan[i]

        # Bullish FVG: gap between c1.high and c3.low
        if c3.low > c1.high:
            gl, gh = c1.high, c3.low
            size = (gh - gl) / pip_size
            if size >= fvg_min_pips:
                in_zone  = gl <= price <= gh
                approach = price < gl and (gl - price) / pip_size <= 10
                score    = 20 if in_zone else (15 if approach else 10)
                # FIX #1: ASCII dashes only -- no en/em-dash Unicode
                state = "price in zone" if in_zone else ("approaching" if approach else "not yet retested")
                return FVGResult(
                    detected  = True, bullish = True, score = score,
                    gap_low   = round(gl, 5), gap_high = round(gh, 5),
                    in_zone   = in_zone,
                    fvg_text  = f"Bullish FVG {gl:.5f} to {gh:.5f} ({size:.1f} pips) | {state}",
                )

        # Bearish FVG: gap between c3.high and c1.low
        if c1.low > c3.high:
            gl, gh = c3.high, c1.low
            size = (gh - gl) / pip_size
            if size >= fvg_min_pips:
                in_zone  = gl <= price <= gh
                approach = price > gh and (price - gh) / pip_size <= 10
                score    = 20 if in_zone else (15 if approach else 10)
                state = "price in zone" if in_zone else ("approaching" if approach else "not yet retested")
                return FVGResult(
                    detected  = True, bullish = False, score = score,
                    gap_low   = round(gl, 5), gap_high = round(gh, 5),
                    in_zone   = in_zone,
                    fvg_text  = f"Bearish FVG {gl:.5f} to {gh:.5f} ({size:.1f} pips) | {state}",
                )

    return _null


# ---------------------------------------------------------------------------
# 5. News risk  (0-15 pts)
# ---------------------------------------------------------------------------

def _is_major(event_name: str) -> bool:
    return any(kw in event_name.upper() for kw in MAJOR_EVENT_KEYWORDS)


def check_news_risk(
    macro_events: list[dict],
    at: datetime | None = None,
) -> NewsResult:
    now = at or datetime.now(timezone.utc)
    high_impact = [
        e for e in macro_events
        if str(e.get("impact", "")).lower() == "high"
        and str(e.get("currency", "")).upper() in ("EUR", "USD")
    ]

    for ev in high_impact:
        raw = ev.get("time", "")
        try:
            ev_time = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        except (ValueError, TypeError):
            continue

        name   = str(ev.get("event", "Unknown"))
        diff_s = (ev_time - now).total_seconds()
        before_s, after_s = (3600, 1800) if _is_major(name) else (1800, 900)

        if -after_s <= diff_s <= before_s:
            return NewsResult(clear=False, score=0, status="BLOCKED",
                              blocking_event=name)

    return NewsResult(clear=True, score=15, status="CLEAR", blocking_event="")


# ---------------------------------------------------------------------------
# 6. Session timing  (0-10 pts)
# ---------------------------------------------------------------------------

def detect_session(at: datetime | None = None) -> SessionResult:
    """
    UTC hour bands:
      00-07  Asian                0 pts
      07-12  London               5 pts  (10 in London kill zone 08-10)
      12-17  London/NY Overlap   10 pts  (NY kill zone 13-15:30 also 10 pts)
      17-21  New York             5 pts
      21-00  Off-session          0 pts
    """
    now = at or datetime.now(timezone.utc)
    t   = now.hour + now.minute / 60

    if 0 <= t < 7:
        return SessionResult(score=0, session="Asian",
                             in_kill_zone=False, session_text="Asian session")
    if 7 <= t < 12:
        kz    = 8 <= t < 10
        score = 10 if kz else 5
        label = "London kill zone" if kz else "London session"
        return SessionResult(score=score, session="London",
                             in_kill_zone=kz, session_text=label)
    if 12 <= t < 17:
        kz    = 13 <= t < 15.5
        label = "New York kill zone" if kz else "London/New York overlap"
        return SessionResult(score=10, session="Overlap",
                             in_kill_zone=kz, session_text=label)
    if 17 <= t < 21:
        return SessionResult(score=5, session="New York",
                             in_kill_zone=False, session_text="New York session")

    return SessionResult(score=0, session="Off-session",
                         in_kill_zone=False, session_text="Off-session")


# ---------------------------------------------------------------------------
# 7. Quality score  (adaptive-weight aware)
# ---------------------------------------------------------------------------

# Base weights — must match adaptive_engine.BASE_WEIGHTS
_BASE_W = {"htf": 15, "liq": 20, "ms": 20, "fvg": 20, "news": 15, "session": 10}


def calculate_quality_score(htf, liq, ms, fvg, news, sess,
                             adaptive_weights: Optional[dict] = None) -> int:
    """
    Compute quality score 0-100.

    If `adaptive_weights` is provided (dict with keys htf/liq/ms/fvg/news/session),
    each component's raw score is scaled proportionally to its learned weight vs base.

    Example: base liq=20, raw liq_score=10 (50% of max), learned liq_weight=24
             → adjusted = round(0.5 * 24) = 12  instead of 10

    Max possible score always stays 100 because adaptive_engine normalises weights to sum=100.
    """
    aw = adaptive_weights or {}

    def _adj(raw: int, key: str) -> int:
        base = _BASE_W[key]
        if not aw or base == 0:
            return raw
        learned = aw.get(key, base)
        pct = raw / base
        return round(pct * learned)

    return (
        _adj(htf.score,  "htf")
        + _adj(liq.score,  "liq")
        + _adj(ms.score,   "ms")
        + _adj(fvg.score,  "fvg")
        + _adj(news.score, "news")
        + _adj(sess.score, "session")
    )


# ---------------------------------------------------------------------------
# 8. Reason builder -- specific, gate-aware messages
#
# FIX #10 : Each gate failure now returns a distinct, actionable message
# ---------------------------------------------------------------------------

def _build_reason(
    news:       NewsResult,
    liq:        LiqResult,
    ms:         MSResult,
    fvg:        FVGResult,
    score:      int,
    bull_votes: int,
    bear_votes: int,
    n_votes:    int,
    rr_val:     float | None = None,
    sess:       SessionResult | None = None,
) -> str:
    if not news.clear:
        return f"News blackout: {news.blocking_event} — wait for window to expire"
    if score < 80:
        missing = 80 - score
        session_note = ""
        if sess and sess.score == 0:
            session_note = " (Asian/Off-session adds 0 pts — wait for London/NY open)"
        return (
            f"Score {score}/100 — need {missing} more pts to qualify{session_note}"
        )
    if not liq.swept:
        return "No liquidity sweep — waiting for stop-hunt wick beyond swing high or low"
    if liq.score < 15:
        return (
            f"Sweep wick too small ({liq.liq_text}) — "
            f"need >= {STRONG_WICK_PIPS} pip wick for full confirmation"
        )
    if not ms.shifted:
        return (
            "Liquidity swept but no structure break yet — "
            f"waiting for {ms.structure_text.lower()}"
        )
    if not fvg.detected:
        return "Structure break confirmed but no Fair Value Gap found for entry trigger"
    if bull_votes == bear_votes:
        return (
            f"Direction split {bull_votes}-{bear_votes} — "
            "HTF/liquidity/structure/FVG not aligned; no directional edge"
        )
    if rr_val is not None and rr_val < MIN_RR:
        return (
            f"RR {rr_val:.2f} below minimum 1:{MIN_RR} — "
            "stop loss too wide relative to target; wait for tighter entry"
        )
    return "All gates checked — setup conditions not fully satisfied"


# ---------------------------------------------------------------------------
# 9. Master analysis
# ---------------------------------------------------------------------------

def analyze_signal(
    pair:           str             = "eurusd",
    candles:        list[Any]       = None,
    macro_events:   list[dict]      = None,
    at:             datetime | None = None,
    pip_size:       float           = PIP,
    target_pips:    int             = TARGET_PIPS,
    sl_buffer_pips: int             = SL_BUFFER_PIPS,
    min_rr:         float           = MIN_RR,
    fvg_min_pips:   int             = 3,
    db=None,                        # SQLAlchemy Session — optional, enables adaptive weights
) -> SignalResult:
    """
    Run the full ICT/SMC pipeline.

    pair           -- "eurusd" | "xauusd"
    at             -- historical timestamp for backtesting (uses now() if None)
    pip_size       -- 0.0001 for EUR/USD; 1.0 for XAU/USD
    target_pips    -- fixed profit target in pips
    sl_buffer_pips -- buffer beyond swept level for stop loss
    min_rr         -- minimum risk:reward required to fire BUY/SELL
    fvg_min_pips   -- minimum FVG size in pips to be considered valid
    """
    if candles is None:
        candles = []
    if macro_events is None:
        macro_events = []

    # ── Live feed: try MT5 bridge, then TradingView, then synthetic ──────
    if not candles:
        try:
            from services.live_feed import get_live_candles
            live = get_live_candles(pair, timeframe="H4", limit=300)
            if live:
                candles = live
                log.info("[engine] Using live MT5 candles for %s (%d bars)", pair, len(candles))
        except Exception as _lf_exc:
            log.debug("[engine] Live feed unavailable (%s) — synthetic fallback", _lf_exc)

    # Synthetic fallback: generate candles when bridge is offline and none provided
    if not candles:
        try:
            from data.candles import get_candles as _get_synthetic
            _synth = _get_synthetic(interval="H4", limit=300, pair=pair)
            candles = _synth.candles
            log.debug("[engine] Using synthetic candles for %s (%d bars)", pair, len(candles))
        except Exception as _syn_exc:
            log.warning("[engine] Synthetic candle fallback failed: %s", _syn_exc)

    # ── Stale candle guard ────────────────────────────────────────────────
    # H4 candles older than 8 hours cannot safely produce BUY/SELL signals.
    # Force WAIT immediately so stale data can never trigger a trade.
    if candles:
        _latest_time_str = candles[-1].get("time", "") if hasattr(candles[-1], "get") else ""
        if _latest_time_str:
            try:
                _latest_ts = datetime.fromisoformat(_latest_time_str.replace("Z", "+00:00"))
                _age_hours = (datetime.now(timezone.utc) - _latest_ts).total_seconds() / 3600
                # H4 candle cycle = 4h; allow 4h candle duration + 4h tolerance = 8h max
                if _age_hours > 8:
                    log.warning("[engine] STALE CANDLES for %s — latest bar is %.1fh old (max 8h). Forcing WAIT.",
                                pair, _age_hours)
                    _stale_reason = f"STALE_CANDLES — data is {_age_hours:.1f}h old (max 8h). Check data feed."
                    return SignalResult(
                        signal="WAIT", quality_score=0,
                        entry=None, stop_loss=None, take_profit=None,
                        risk_pips=None, target_pips=target_pips, rr=None,
                        invalidation=None, reason=_stale_reason, news_status="CLEAR",
                        model={"higherTimeframeBias": "—", "liquidity": "—",
                               "structure": "—", "fvg": "—", "session": "—"},
                        htf=HTFResult(bullish=False, score=0, ema21=0.0, ema50=0.0,
                                      structure="—", bias_text="Stale data"),
                        liq=LiqResult(swept=False, bullish=False, score=0,
                                      swept_level=0.0, liq_text="Stale data"),
                        ms=MSResult(shifted=False, bullish=False, score=0,
                                    pattern="None", structure_text="Stale data"),
                        fvg=FVGResult(detected=False, bullish=False, score=0,
                                      gap_low=0.0, gap_high=0.0, in_zone=False,
                                      fvg_text="Stale data"),
                        news=NewsResult(clear=True, score=0, blocking_event="",
                                        status="CLEAR"),
                        sess=SessionResult(score=0, session="—", in_kill_zone=False,
                                           session_text="Stale data"),
                        pair=pair,
                        display_pair=pair.upper(),
                        component_snapshot="{}",
                        weights_used=None,
                        data_source="stale",
                    )
            except Exception as _ts_exc:
                log.debug("[engine] Could not parse candle timestamp for staleness check: %s", _ts_exc)

    # ── Adaptive weights: load from DB if session provided ────────────────
    _adaptive_w: dict | None = None
    if db is not None:
        try:
            from services.adaptive_engine import get_current_weights
            aw = get_current_weights(db, pair="all")
            _adaptive_w = aw.as_score_dict()
        except Exception as _aw_exc:
            log.debug("[engine] Adaptive weights unavailable (%s) — using base weights", _aw_exc)

    # Load pair-specific config (explicit params take precedence)
    from pair_config import get_pair_config
    try:
        _pcfg = get_pair_config(pair)
        if pip_size       == PIP:            pip_size       = _pcfg["pip_size"]
        if target_pips    == TARGET_PIPS:    target_pips    = _pcfg["target_pips"]
        if sl_buffer_pips == SL_BUFFER_PIPS: sl_buffer_pips = _pcfg["sl_buffer_pips"]
        if min_rr         == MIN_RR:         min_rr         = _pcfg["min_rr"]
        if fvg_min_pips   == 3:              fvg_min_pips   = _pcfg["fvg_min_pips"]
        _price_decimals = _pcfg["price_decimals"]
    except ValueError:
        _pcfg           = None
        _price_decimals = 5

    # Normalise input: accept dicts, Pydantic-like objects, or internal Candle
    def _to_candle(raw: Any) -> Candle:
        if isinstance(raw, Candle):
            return raw
        if hasattr(raw, "open"):
            return Candle(
                time   = getattr(raw, "time", datetime.now(timezone.utc)),
                open   = float(raw.open),
                high   = float(raw.high),
                low    = float(raw.low),
                close  = float(raw.close),
                volume = int(getattr(raw, "volume", 0)),
            )
        return Candle(
            time   = datetime.fromisoformat(
                str(raw.get("time", datetime.now(timezone.utc).isoformat()))
            ),
            open   = float(raw["open"]),
            high   = float(raw["high"]),
            low    = float(raw["low"]),
            close  = float(raw["close"]),
            volume = int(raw.get("volume", 0)),
        )

    bars = [_to_candle(c) for c in candles]

    # Run all detection modules
    htf  = detect_higher_timeframe_bias(bars)
    liq  = detect_liquidity_sweep(bars, pip_size=pip_size)
    ms   = detect_market_structure(bars, htf_bullish=htf.bullish)   # FIX #9
    fvg  = detect_fair_value_gap(bars, pip_size=pip_size, fvg_min_pips=fvg_min_pips)
    news = check_news_risk(macro_events, at=at)
    sess = detect_session(at=at)

    score = calculate_quality_score(htf, liq, ms, fvg, news, sess,
                                    adaptive_weights=_adaptive_w)
    price = round(bars[-1].close, _price_decimals) if bars else 1.08432

    # FIX #5: directional votes only count detected/active components
    _dir_votes = [
        htf.bullish,
        liq.bullish if liq.swept    else None,
        ms.bullish  if ms.shifted   else None,
        fvg.bullish if fvg.detected else None,
    ]
    _active = [v for v in _dir_votes if v is not None]
    bull_votes = sum(1 for v in _active if v)
    bear_votes = sum(1 for v in _active if not v)
    n_votes    = len(_active)

    # Defaults
    signal      = "WAIT"
    entry       = None
    stop_loss   = None
    take_profit = None
    risk_pips   = None
    rr          = None
    invalidation = None
    reason      = ""

    _sl_buffer = sl_buffer_pips * pip_size
    _target    = target_pips    * pip_size

    # All structural gates (before RR check)
    gates_ok = (
        news.clear
        and score >= 80
        and liq.swept
        and liq.score >= 15   # FIX: require strong sweep, not just any wick
        and ms.shifted
        and fvg.detected
    )

    if gates_ok:
        if bull_votes >= 3 and bull_votes > bear_votes:
            sl_level = liq.swept_level - _sl_buffer
            rp       = (price - sl_level) / pip_size
            rr_val   = target_pips / rp if rp > 0 else 0.0
            if rr_val >= min_rr:
                signal      = "BUY"
                entry       = price
                stop_loss   = round(sl_level, _price_decimals)
                take_profit = round(price + _target, _price_decimals)
                risk_pips   = round(rp)
                rr          = round(rr_val, 2)
                invalidation = stop_loss
            else:
                reason = _build_reason(news, liq, ms, fvg, score,
                                       bull_votes, bear_votes, n_votes, rr_val, sess)

        elif bear_votes >= 3 and bear_votes > bull_votes:
            sl_level = liq.swept_level + _sl_buffer
            rp       = (sl_level - price) / pip_size
            rr_val   = target_pips / rp if rp > 0 else 0.0
            if rr_val >= min_rr:
                signal      = "SELL"
                entry       = price
                stop_loss   = round(sl_level, _price_decimals)
                take_profit = round(price - _target, _price_decimals)
                risk_pips   = round(rp)
                rr          = round(rr_val, 2)
                invalidation = stop_loss
            else:
                reason = _build_reason(news, liq, ms, fvg, score,
                                       bull_votes, bear_votes, n_votes, rr_val, sess)
        else:
            reason = _build_reason(news, liq, ms, fvg, score,
                                   bull_votes, bear_votes, n_votes, sess=sess)
    else:
        reason = _build_reason(news, liq, ms, fvg, score,
                               bull_votes, bear_votes, n_votes, sess=sess)

    # ── MyFXBook sentiment (non-blocking, supplementary) ──────────────────
    _sentiment: dict = {}
    try:
        from services.myfxbook_provider import get_sentiment_for_signal
        _sentiment = get_sentiment_for_signal(pair, signal)
    except Exception:
        _sentiment = {"available": False, "interpretation": "Sentiment unavailable", "adjusted_score": 0}

    model_dict = {
        "higherTimeframeBias": htf.bias_text,
        "liquidity":           liq.liq_text,
        "structure":           ms.structure_text,
        "fvg":                 fvg.fvg_text,
        "session":             sess.session_text,
        "sentiment":           _sentiment,
    }

    # Component activity snapshot (stored in DB for learning)
    _component_snapshot = json.dumps({
        "htf":     bool(htf.score >= 8),
        "liq":     liq.swept,
        "ms":      ms.shifted,
        "fvg":     fvg.detected,
        "news":    news.clear,
        "session": sess.score >= 5,
    })

    # Weights used (for audit transparency)
    _weights_used = _adaptive_w or _BASE_W

    # Fire alert channel (non-blocking)
    if signal in ("BUY", "SELL"):
        try:
            from services.alert_service import fire_alert
            fire_alert(
                pair      = _pcfg["display"] if _pcfg else pair,
                signal    = signal,
                score     = score,
                entry     = entry,
                stop_loss = stop_loss,
                take_profit = take_profit,
                rr        = rr,
                reason    = reason or "All gates passed",
            )
        except Exception:
            pass

    # Determine data source from the candles that were actually used
    _data_src = "synthetic"
    if candles and hasattr(candles[0], "get"):
        _src = candles[0].get("source", "")
        if _src == "mt5":
            _data_src = "live"
        elif _src == "tradingview":
            _data_src = "tradingview"

    return SignalResult(
        signal        = signal,
        quality_score = score,
        entry         = entry,
        stop_loss     = stop_loss,
        take_profit   = take_profit,
        risk_pips     = risk_pips,
        target_pips   = target_pips,
        rr            = rr,
        invalidation  = invalidation,
        reason        = reason,
        news_status   = news.status,
        model         = model_dict,
        htf=htf, liq=liq, ms=ms, fvg=fvg, news=news, sess=sess,
        pair               = pair,
        display_pair       = _pcfg["display"] if _pcfg else "EUR/USD",
        component_snapshot = _component_snapshot,
        weights_used       = _weights_used,
        data_source        = _data_src,
    )
