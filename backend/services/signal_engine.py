"""
ICT / Smart Money Concepts signal engine for EUR/USD.

Output format (flat):
  signal          BUY | SELL | WAIT
  qualityScore    0–100
  entry           price or null
  stopLoss        price or null
  takeProfit      price or null (fixed 40-pip target)
  riskPips        int or null
  targetPips      40 (constant)
  rr              float or null  (targetPips / riskPips)
  invalidation    same as stopLoss or null
  reason          human-readable gate failure
  newsStatus      CLEAR | BLOCKED
  model           { higherTimeframeBias, liquidity, structure, fvg, session }

Scoring model (100 pts):
  HTF Bias            15 pts  – EMA-21 vs EMA-50 + HH/HL pivot structure
  Liquidity Sweep     20 pts  – stop-hunt wick beyond swing level that reverses
  Market Structure    20 pts  – BOS / CHoCH via 3-bar pivots
  Fair Value Gap      20 pts  – 3-candle imbalance ≥ 3 pips
  News Risk           15 pts  – 0 if inside blackout window
  Session Timing      10 pts  – London / NY / Overlap active; 0 for Asian / Off

Gate sequence (ALL must pass for BUY or SELL):
  1. news_clear          – no blackout window active
  2. score >= 80         – minimum confluence
  3. liquidity_swept     – stop hunt confirmed
  4. structure_shifted   – BOS or CHoCH in signal direction
  5. fvg_detected        – entry trigger present
  6. directional_agree   – 3 of 4 indicators agree (htf / liq / ms / fvg)
  7. rr >= 2.5           – risk-reward viable with 40-pip fixed target

Trade geometry:
  pip value  = 0.0001
  target     = 40 pips = 0.0040
  BUY  SL    = swept_low  - 3 pips buffer
  SELL SL    = swept_high + 3 pips buffer
  BUY  TP    = entry + 0.0040
  SELL TP    = entry - 0.0040
  RR         = 40 / riskPips   (must be >= 2.5)

News blackout windows:
  Major events (CPI, NFP, FOMC, ECB rate decision):
    60 min before → 30 min after
  All other high-impact EUR/USD events:
    30 min before → 15 min after
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

# ── Constants ─────────────────────────────────────────────────────────────────

PIP            = 0.0001
TARGET_PIPS    = 40
TARGET         = TARGET_PIPS * PIP        # 0.0040
SL_BUFFER_PIPS = 3
SL_BUFFER      = SL_BUFFER_PIPS * PIP
MIN_RR         = 2.5

# Extended blackout: 60 min before / 30 min after
MAJOR_EVENT_KEYWORDS = frozenset({"CPI", "NFP", "FOMC", "ECB"})


# ── Internal data structures ──────────────────────────────────────────────────

@dataclass
class Candle:
    time: datetime
    open: float
    high: float
    low: float
    close: float
    volume: int


@dataclass
class HTFResult:
    bullish: bool
    score: int
    ema21: float
    ema50: float
    structure: str    # "HH/HL" | "LH/LL" | "Mixed"
    bias_text: str    # "Bullish" | "Bearish" | "Neutral"


@dataclass
class LiqResult:
    swept: bool
    bullish: bool     # True = buy-side sweep (wick below lows, close above)
    score: int
    swept_level: float
    liq_text: str


@dataclass
class MSResult:
    shifted: bool
    bullish: bool
    score: int
    pattern: str      # "BOS" | "CHoCH" | "None"
    structure_text: str


@dataclass
class FVGResult:
    detected: bool
    bullish: bool
    score: int
    gap_low: float
    gap_high: float
    in_zone: bool
    fvg_text: str


@dataclass
class NewsResult:
    clear: bool
    score: int
    blocking_event: str
    status: str       # "CLEAR" | "BLOCKED"


@dataclass
class SessionResult:
    score: int
    session: str      # "Asian" | "London" | "New York" | "Overlap" | "Off-session"
    in_kill_zone: bool
    session_text: str


@dataclass
class SignalResult:
    signal: str       # "BUY" | "SELL" | "WAIT"
    quality_score: int
    entry: float | None
    stop_loss: float | None
    take_profit: float | None
    risk_pips: int | None
    target_pips: int
    rr: float | None
    invalidation: float | None
    reason: str
    news_status: str  # "CLEAR" | "BLOCKED"
    model: dict       # { higherTimeframeBias, liquidity, structure, fvg, session }
    # Sub-results kept for potential bridge use
    htf: HTFResult
    liq: LiqResult
    ms: MSResult
    fvg: FVGResult
    news: NewsResult
    sess: SessionResult


# ── EMA helper ────────────────────────────────────────────────────────────────

def _ema(values: list[float], period: int) -> list[float]:
    if len(values) < period:
        return [values[-1]] * len(values)
    k = 2 / (period + 1)
    sma = sum(values[:period]) / period
    result = [sma]
    for v in values[period:]:
        result.append(v * k + result[-1] * (1 - k))
    return [result[0]] * (period - 1) + result


# ── 3-bar pivot helpers ───────────────────────────────────────────────────────

def _pivot_highs(vals: list[float]) -> list[float]:
    return [
        vals[i] for i in range(1, len(vals) - 1)
        if vals[i] > vals[i - 1] and vals[i] > vals[i + 1]
    ]


def _pivot_lows(vals: list[float]) -> list[float]:
    return [
        vals[i] for i in range(1, len(vals) - 1)
        if vals[i] < vals[i - 1] and vals[i] < vals[i + 1]
    ]


# ── 1. Higher-timeframe bias  (0–15 pts) ──────────────────────────────────────

def detect_higher_timeframe_bias(candles: list[Candle]) -> HTFResult:
    closes = [c.close for c in candles]

    if len(closes) < 51:
        mid = closes[-1]
        return HTFResult(bullish=True, score=5, ema21=mid, ema50=mid,
                         structure="Mixed", bias_text="Neutral")

    ema21 = _ema(closes, 21)
    ema50 = _ema(closes, 50)
    bullish_ema = ema21[-1] > ema50[-1]
    stable = (ema21[-4] > ema50[-4]) == bullish_ema  # same side 4 bars ago

    highs = [c.high for c in candles[-20:]]
    lows  = [c.low  for c in candles[-20:]]
    ph = _pivot_highs(highs)
    pl = _pivot_lows(lows)

    hh_hl = len(ph) >= 2 and ph[-1] > ph[-2] and len(pl) >= 2 and pl[-1] > pl[-2]
    lh_ll = len(ph) >= 2 and ph[-1] < ph[-2] and len(pl) >= 2 and pl[-1] < pl[-2]
    structure = "HH/HL" if hh_hl else "LH/LL" if lh_ll else "Mixed"

    structural_confirm = (bullish_ema and hh_hl) or (not bullish_ema and lh_ll)
    score = 15 if (structural_confirm and stable) else (8 if bullish_ema else 5)
    bias_text = "Bullish" if bullish_ema else "Bearish"

    return HTFResult(
        bullish=bullish_ema,
        score=score,
        ema21=round(ema21[-1], 5),
        ema50=round(ema50[-1], 5),
        structure=structure,
        bias_text=f"{bias_text} ({structure})",
    )


# ── 2. Liquidity sweep  (0–20 pts) ────────────────────────────────────────────

def detect_liquidity_sweep(candles: list[Candle], pip_size: float = PIP) -> LiqResult:
    _null = LiqResult(swept=False, bullish=True, score=0,
                      swept_level=0.0, liq_text="No sweep detected")
    if len(candles) < 10:
        return _null

    ref        = candles[-32:-2]
    candidates = candles[-2:]
    if not ref:
        return _null

    swing_high = max(c.high for c in ref)
    swing_low  = min(c.low  for c in ref)

    for c in reversed(candidates):
        wick_lo = c.low  < swing_low  and c.close > swing_low
        wick_hi = c.high > swing_high and c.close < swing_high

        if wick_lo:
            wick_pips = (c.close - c.low) / pip_size
            score = 20 if wick_pips >= 3 else 10
            return LiqResult(swept=True, bullish=True, score=score,
                             swept_level=round(swing_low, 5),
                             liq_text=f"Buy-side liquidity swept below {swing_low:.5f}"
                                      f" ({wick_pips:.1f} pip wick)")

        if wick_hi:
            wick_pips = (c.high - c.close) / pip_size
            score = 20 if wick_pips >= 3 else 10
            return LiqResult(swept=True, bullish=False, score=score,
                             swept_level=round(swing_high, 5),
                             liq_text=f"Sell-side liquidity swept above {swing_high:.5f}"
                                      f" ({wick_pips:.1f} pip wick)")

    return _null


# ── 3. Market structure  (0–20 pts) ───────────────────────────────────────────

def detect_market_structure(candles: list[Candle]) -> MSResult:
    _null = MSResult(shifted=False, bullish=True, score=0,
                     pattern="None", structure_text="No structural break detected")
    if len(candles) < 15:
        return _null

    bars   = candles[-40:]
    highs  = [c.high  for c in bars]
    lows   = [c.low   for c in bars]
    closes = [c.close for c in bars]

    ph = [(i, h) for i in range(1, len(highs) - 1)
          if highs[i] > highs[i - 1] and highs[i] > highs[i + 1]
          for h in (highs[i],)]
    pl = [(i, l) for i in range(1, len(lows)  - 1)
          if lows[i]  < lows[i - 1]  and lows[i]  < lows[i + 1]
          for l in (lows[i],)]

    last_c  = closes[-1]
    prev_c  = closes[-2]

    if ph:
        _, last_ph = ph[-1]
        if last_c > last_ph and prev_c <= last_ph:
            is_bos = len(ph) >= 2 and last_ph > ph[-2][1]
            pattern = "BOS" if is_bos else "CHoCH"
            score   = 20 if is_bos else 15
            return MSResult(shifted=True, bullish=True, score=score, pattern=pattern,
                            structure_text=f"Bullish {pattern} confirmed above {last_ph:.5f}")

    if pl:
        _, last_pl = pl[-1]
        if last_c < last_pl and prev_c >= last_pl:
            is_bos = len(pl) >= 2 and last_pl < pl[-2][1]
            pattern = "BOS" if is_bos else "CHoCH"
            score   = 20 if is_bos else 15
            return MSResult(shifted=True, bullish=False, score=score, pattern=pattern,
                            structure_text=f"Bearish {pattern} confirmed below {last_pl:.5f}")

    # Describe what we're waiting for based on HTF (approximate from last pivot)
    waiting = "Waiting for bullish CHoCH / BOS" if (ph and pl and ph[-1][0] > pl[-1][0]) \
              else "Waiting for bearish CHoCH / BOS"
    return MSResult(shifted=False, bullish=True, score=0, pattern="None",
                    structure_text=waiting)


# ── 4. Fair Value Gap  (0–20 pts) ─────────────────────────────────────────────

def detect_fair_value_gap(
    candles: list[Candle],
    pip_size: float = PIP,
    fvg_min_pips: int = 3,
) -> FVGResult:
    _null = FVGResult(detected=False, bullish=True, score=0,
                      gap_low=0.0, gap_high=0.0, in_zone=False,
                      fvg_text="No Fair Value Gap detected")
    if len(candles) < 5:
        return _null

    price = candles[-1].close
    scan  = candles[max(0, len(candles) - 25):]

    for i in range(len(scan) - 1, 1, -1):
        c1, c3 = scan[i - 2], scan[i]

        # Bullish FVG: c3.low > c1.high
        if c3.low > c1.high:
            gl, gh = c1.high, c3.low
            size = (gh - gl) / pip_size
            if size >= fvg_min_pips:
                in_zone    = gl <= price <= gh
                approach   = price < gl and (gl - price) / pip_size <= 10
                score      = 20 if in_zone else (15 if approach else 10)
                state      = "price in zone" if in_zone else ("approaching" if approach else "detected but not retested")
                return FVGResult(detected=True, bullish=True, score=score,
                                 gap_low=round(gl, 5), gap_high=round(gh, 5), in_zone=in_zone,
                                 fvg_text=f"Bullish FVG {gl:.5f}–{gh:.5f} ({size:.1f} pips) — {state}")

        # Bearish FVG: c1.low > c3.high
        if c1.low > c3.high:
            gl, gh = c3.high, c1.low
            size = (gh - gl) / pip_size
            if size >= fvg_min_pips:
                in_zone    = gl <= price <= gh
                approach   = price > gh and (price - gh) / pip_size <= 10
                score      = 20 if in_zone else (15 if approach else 10)
                state      = "price in zone" if in_zone else ("approaching" if approach else "detected but not retested")
                return FVGResult(detected=True, bullish=False, score=score,
                                 gap_low=round(gl, 5), gap_high=round(gh, 5), in_zone=in_zone,
                                 fvg_text=f"Bearish FVG {gl:.5f}–{gh:.5f} ({size:.1f} pips) — {state}")

    return _null


# ── 5. News risk  (0–15 pts) ──────────────────────────────────────────────────

def _is_major(event_name: str) -> bool:
    name_upper = event_name.upper()
    return any(kw in name_upper for kw in MAJOR_EVENT_KEYWORDS)


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

        if _is_major(name):
            before_s, after_s = 3600, 1800
        else:
            before_s, after_s = 1800, 900

        if -after_s <= diff_s <= before_s:
            return NewsResult(
                clear=False, score=0, status="BLOCKED",
                blocking_event=name,
            )

    return NewsResult(clear=True, score=15, status="CLEAR", blocking_event="")


# ── 6. Session timing  (0–10 pts) ─────────────────────────────────────────────

def detect_session(at: datetime | None = None) -> SessionResult:
    """
    UTC hour bands:
      00–07  Asian              0 pts
      07–12  London             5 pts  (10 in London kill zone 08–10)
      12–17  London/NY Overlap  10 pts (NY kill zone 13–15:30 also 10 pts)
      17–21  New York           5 pts
      21–00  Off-session        0 pts

    Pass `at` to evaluate a historical timestamp instead of now.
    """
    now = at or datetime.now(timezone.utc)
    t   = now.hour + now.minute / 60

    london_kz  = 8 <= t < 10
    ny_kz      = 13 <= t < 15.5

    if 0 <= t < 7:
        return SessionResult(score=0, session="Asian",
                             in_kill_zone=False, session_text="Asian session")
    if 7 <= t < 12:
        score = 10 if london_kz else 5
        label = "London kill zone" if london_kz else "London session"
        return SessionResult(score=score, session="London",
                             in_kill_zone=london_kz, session_text=label)
    if 12 <= t < 17:
        score = 10
        if ny_kz:
            label = "New York kill zone"
        else:
            label = "London/New York overlap"
        return SessionResult(score=score, session="Overlap",
                             in_kill_zone=ny_kz, session_text=label)
    if 17 <= t < 21:
        return SessionResult(score=5, session="New York",
                             in_kill_zone=False, session_text="New York session")

    return SessionResult(score=0, session="Off-session",
                         in_kill_zone=False, session_text="Off-session")


# ── 7. Quality score ──────────────────────────────────────────────────────────

def calculate_quality_score(htf, liq, ms, fvg, news, sess) -> int:
    return htf.score + liq.score + ms.score + fvg.score + news.score + sess.score


# ── 8. Reason builder ─────────────────────────────────────────────────────────

def _build_reason(
    news: NewsResult,
    liq: LiqResult,
    ms: MSResult,
    fvg: FVGResult,
    score: int,
    bull_votes: int,
    rr_val: float | None = None,
) -> str:
    if not news.clear:
        return f"News blackout active: {news.blocking_event}"
    if score < 80:
        return f"Quality score {score}/100 below minimum threshold of 80"
    if not liq.swept:
        return "Waiting for liquidity sweep (stop hunt beyond swing high or low)"
    if not ms.shifted:
        return "Liquidity sweep detected but market structure has not confirmed."
    if not fvg.detected:
        return "Structure break confirmed but no Fair Value Gap found for entry"
    if bull_votes == 2:
        return "Mixed directional signals — indicators split 2–2 (no edge)"
    if rr_val is not None and rr_val < MIN_RR:
        return f"Risk-reward {rr_val:.2f} below minimum 1:{MIN_RR} (stop loss too wide)"
    return "Setup conditions not fully met"


# ── 9. Master analysis  ───────────────────────────────────────────────────────

def analyze_signal(
    candles: list[Any],
    macro_events: list[dict] | None = None,
    at: datetime | None = None,
    pip_size: float = PIP,
    target_pips: int = TARGET_PIPS,
    sl_buffer_pips: int = SL_BUFFER_PIPS,
    min_rr: float = MIN_RR,
    fvg_min_pips: int = 3,
) -> SignalResult:
    """
    Run the full ICT/SMC pipeline.

    at            – historical timestamp for backtesting (uses now() if None)
    pip_size      – pair-specific pip (0.0001 EUR/USD, 0.01 JPY, 0.1 XAU)
    target_pips   – fixed profit-target in pips for this pair
    sl_buffer_pips – structural SL buffer added beyond the swept level
    min_rr        – minimum risk-reward ratio required to fire BUY/SELL
    fvg_min_pips  – minimum FVG size in pips to be considered valid
    """
    if macro_events is None:
        macro_events = []

    # Normalise input: accept dicts, Pydantic-like objects, or internal Candle
    def _to_candle(raw: Any) -> Candle:
        if isinstance(raw, Candle):
            return raw
        if hasattr(raw, "open"):
            return Candle(
                time=getattr(raw, "time", datetime.now(timezone.utc)),
                open=float(raw.open), high=float(raw.high),
                low=float(raw.low),   close=float(raw.close),
                volume=int(getattr(raw, "volume", 0)),
            )
        return Candle(
            time=datetime.fromisoformat(
                str(raw.get("time", datetime.now(timezone.utc).isoformat()))
            ),
            open=float(raw["open"]), high=float(raw["high"]),
            low=float(raw["low"]),   close=float(raw["close"]),
            volume=int(raw.get("volume", 0)),
        )

    bars = [_to_candle(c) for c in candles]

    htf  = detect_higher_timeframe_bias(bars)
    liq  = detect_liquidity_sweep(bars, pip_size=pip_size)
    ms   = detect_market_structure(bars)
    fvg  = detect_fair_value_gap(bars, pip_size=pip_size, fvg_min_pips=fvg_min_pips)
    news = check_news_risk(macro_events, at=at)
    sess = detect_session(at=at)

    score      = calculate_quality_score(htf, liq, ms, fvg, news, sess)
    price      = round(bars[-1].close, 5) if bars else 1.08432
    bull_votes = sum([htf.bullish, liq.bullish, ms.bullish, fvg.bullish])
    bear_votes = 4 - bull_votes

    # Default outputs
    signal     = "WAIT"
    entry      = None
    stop_loss  = None
    take_profit = None
    risk_pips  = None
    rr         = None
    invalidation = None
    reason     = ""

    # All structural gates (before RR check)
    gates_ok = (
        news.clear
        and score >= 80
        and liq.swept
        and ms.shifted
        and fvg.detected
    )

    _sl_buffer = sl_buffer_pips * pip_size
    _target    = target_pips    * pip_size

    if gates_ok:
        # Determine candidate direction
        if bull_votes >= 3:
            sl_level = liq.swept_level - _sl_buffer
            rp       = (price - sl_level) / pip_size
            rr_val   = target_pips / rp if rp > 0 else 0.0
            if rr_val >= min_rr:
                signal      = "BUY"
                entry       = price
                stop_loss   = round(sl_level, 5)
                take_profit = round(price + _target, 5)
                risk_pips   = round(rp)
                rr          = round(rr_val, 2)
                invalidation = stop_loss
            else:
                reason = _build_reason(news, liq, ms, fvg, score, bull_votes, rr_val)

        elif bear_votes >= 3:
            sl_level = liq.swept_level + _sl_buffer
            rp       = (sl_level - price) / pip_size
            rr_val   = target_pips / rp if rp > 0 else 0.0
            if rr_val >= min_rr:
                signal      = "SELL"
                entry       = price
                stop_loss   = round(sl_level, 5)
                take_profit = round(price - _target, 5)
                risk_pips   = round(rp)
                rr          = round(rr_val, 2)
                invalidation = stop_loss
            else:
                reason = _build_reason(news, liq, ms, fvg, score, bear_votes, rr_val)

        else:
            reason = _build_reason(news, liq, ms, fvg, score, 2)
    else:
        reason = _build_reason(news, liq, ms, fvg, score, bull_votes)

    model_dict = {
        "higherTimeframeBias": htf.bias_text,
        "liquidity":           liq.liq_text,
        "structure":           ms.structure_text,
        "fvg":                 fvg.fvg_text,
        "session":             sess.session_text,
    }

    return SignalResult(
        signal=signal,
        quality_score=score,
        entry=entry,
        stop_loss=stop_loss,
        take_profit=take_profit,
        risk_pips=risk_pips,
        target_pips=target_pips,
        rr=rr,
        invalidation=invalidation,
        reason=reason,
        news_status=news.status,
        model=model_dict,
        htf=htf, liq=liq, ms=ms, fvg=fvg, news=news, sess=sess,
    )
