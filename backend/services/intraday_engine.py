"""
Intraday Signal Engine for XAU/USD M15.

This is a SEPARATE engine variant from the main signal_engine.py.
The main engine was designed for H4 swing trading. This one is tuned
for genuine intraday trading on M15 with much smaller thresholds and
session-specific setups.

Key differences vs the swing engine:

  Thresholds (tuned for M15 gold volatility):
    fvg_min_pips:     5  -> 1.5    (M15 FVGs are smaller)
    strong_wick_pips: 8  -> 2.5    (M15 sweep wicks are smaller)
    sl_buffer_pips:   5  -> 1.5
    atr_min:          15 -> 1.0    (M15 ATR is ~3-8 pts on gold)
    atr_max:          300-> 12.0
    min_rr:           2.5-> 2.0    (faster targets)

  New PRIMARY setup: Asian range break-retest
    1. Compute today's Asian session (00:00-07:00 UTC) high/low
    2. After 07:00, watch for London/NY breaking the range
    3. Entry: when price RETRACES into the violated extreme
    4. SL beyond the breakout swing, TP at 50 points (configurable)
    This is THE classic intraday gold setup and the most-used by
    funded prop traders.

  Killzone hard gate:
    Only fires inside London KZ (07:00-10:00 UTC) or NY KZ (13:00-16:00 UTC).
    Outside these windows -> WAIT.

  M15-tuned ICT fallback:
    When Asian range setup isn't present, falls back to the standard
    ICT trifecta (liquidity + structure + FVG) but with M15-scaled
    thresholds — fires more often than the swing engine on M15.

The returned SignalResult is shape-compatible with the swing engine's
SignalResult so the backtester can consume both transparently.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional

from services.signal_engine import (
    Candle, HTFResult, LiqResult, MSResult, FVGResult, NewsResult, SessionResult,
    SignalResult,
    detect_higher_timeframe_bias,
    detect_liquidity_sweep,
    detect_market_structure,
    detect_fair_value_gap,
    check_news_risk,
    detect_session,
    classify_setup_type,
)

log = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════════════════════
# Intraday tuning defaults (for M15 XAU/USD)
# ═══════════════════════════════════════════════════════════════════════════════

INTRADAY_DEFAULTS = {
    "fvg_min_pips":     1.0,
    "strong_wick_pips": 2.0,
    "sl_buffer_pips":   1.5,
    "atr_min":          0.8,
    "atr_max":         15.0,
    "min_rr":           1.8,
    "target_pips":     50,        # user's stated target
    "min_score":       55,        # lower bar than swing engine (which uses 80)
    # Killzones (UTC hours, decimal) — widened to capture the open swings
    "london_kz":       (6.5, 11.0),
    "ny_kz":          (12.5, 17.0),
    # Asian range
    "asian_start_h":    0.0,
    "asian_end_h":      7.0,
    "asian_range_min":  2.5,      # skip if range too tight (noisy)
    "asian_range_max": 50.0,      # skip if range too wide (no edge)
    "breakout_min":     1.0,      # required pts beyond Asian extreme (loosened)
    "retest_tolerance": 5.0,      # how close to extreme price must retrace (loosened)
}


# ═══════════════════════════════════════════════════════════════════════════════
# Asian range break-retest detector
# ═══════════════════════════════════════════════════════════════════════════════

@dataclass
class AsianRangeSetup:
    direction:   str      # "BUY" | "SELL"
    asian_high:  float
    asian_low:   float
    breakout_extreme: float    # the London/NY extreme that violated the range
    entry:       float
    stop_loss:   float
    take_profit: float
    risk_points: float
    reward_points: float
    rr:          float
    reason:      str


def _hour_of(t: datetime) -> float:
    return t.hour + t.minute / 60


def check_asian_range_setup(
    candles:        list[Candle],
    at:             datetime,
    pip_size:       float = 1.0,
    target_pips:    int   = 50,
    sl_buffer_pips: float = 1.5,
    asian_range_min: float = 3.0,
    asian_range_max: float = 40.0,
    breakout_min:    float = 2.0,
    retest_tolerance: float = 3.0,
    min_rr:          float = 2.0,
) -> Optional[AsianRangeSetup]:
    """
    Detect the Asian range break-retest pattern. Returns None if no setup.

    Logic:
      1. Find today's Asian session candles (00:00-07:00 UTC)
      2. Compute Asian H + L
      3. After 07:00 UTC, check if any London/NY candle has broken the range
         by >= breakout_min points beyond H (bearish trap) or below L (bullish trap)
      4. If yes, and current price has retraced back to within retest_tolerance
         of the violated extreme, fire the setup
      5. SL goes beyond the breakout extreme, TP at +/- target_pips
    """
    if not at:
        at = datetime.now(timezone.utc)
    if at.tzinfo is None:
        at = at.replace(tzinfo=timezone.utc)

    today_start = at.replace(hour=0, minute=0, second=0, microsecond=0)
    asian_end   = today_start.replace(hour=7)

    if at <= asian_end:
        return None    # Asian session still active

    asian_candles = []
    london_candles = []
    for c in candles:
        ct = c.time if c.time.tzinfo else c.time.replace(tzinfo=timezone.utc)
        if today_start <= ct < asian_end:
            asian_candles.append(c)
        elif ct >= asian_end and ct <= at:
            london_candles.append(c)

    if len(asian_candles) < 4 or len(london_candles) < 1:
        return None

    asian_h = max(c.high for c in asian_candles)
    asian_l = min(c.low  for c in asian_candles)
    asian_range = (asian_h - asian_l) / pip_size

    if asian_range < asian_range_min or asian_range > asian_range_max:
        return None

    london_h = max(c.high for c in london_candles)
    london_l = min(c.low  for c in london_candles)
    current  = candles[-1]
    cur_price = current.close

    # ── BEARISH setup: London broke ABOVE Asian H, retracing back ──────────
    bull_breakout_dist = (london_h - asian_h) / pip_size
    if bull_breakout_dist >= breakout_min:
        # Has price retraced back to within tolerance of Asian H?
        dist_to_asian_h = (cur_price - asian_h) / pip_size   # could be negative
        if -retest_tolerance <= dist_to_asian_h <= retest_tolerance:
            entry = round(asian_h, 2)
            sl    = round(london_h + sl_buffer_pips * pip_size, 2)
            tp    = round(entry - target_pips * pip_size, 2)
            risk  = (sl - entry) / pip_size
            reward = (entry - tp) / pip_size
            if risk > 0 and reward / risk >= min_rr:
                return AsianRangeSetup(
                    direction="SELL",
                    asian_high=asian_h, asian_low=asian_l,
                    breakout_extreme=london_h,
                    entry=entry, stop_loss=sl, take_profit=tp,
                    risk_points=risk, reward_points=reward,
                    rr=round(reward / risk, 2),
                    reason=(
                        f"Asian H {asian_h:.2f} broken to {london_h:.2f} "
                        f"(+{bull_breakout_dist:.1f}pt), retracing — SELL the retest"
                    ),
                )

    # ── BULLISH setup: London broke BELOW Asian L, retracing back ──────────
    bear_breakout_dist = (asian_l - london_l) / pip_size
    if bear_breakout_dist >= breakout_min:
        dist_to_asian_l = (asian_l - cur_price) / pip_size
        if -retest_tolerance <= dist_to_asian_l <= retest_tolerance:
            entry = round(asian_l, 2)
            sl    = round(london_l - sl_buffer_pips * pip_size, 2)
            tp    = round(entry + target_pips * pip_size, 2)
            risk  = (entry - sl) / pip_size
            reward = (tp - entry) / pip_size
            if risk > 0 and reward / risk >= min_rr:
                return AsianRangeSetup(
                    direction="BUY",
                    asian_high=asian_h, asian_low=asian_l,
                    breakout_extreme=london_l,
                    entry=entry, stop_loss=sl, take_profit=tp,
                    risk_points=risk, reward_points=reward,
                    rr=round(reward / risk, 2),
                    reason=(
                        f"Asian L {asian_l:.2f} broken to {london_l:.2f} "
                        f"(-{bear_breakout_dist:.1f}pt), retracing — BUY the retest"
                    ),
                )

    return None


# ═══════════════════════════════════════════════════════════════════════════════
# Killzone gate
# ═══════════════════════════════════════════════════════════════════════════════

def in_killzone(at: datetime,
                london_kz: tuple = (7.0, 10.5),
                ny_kz:     tuple = (13.0, 16.5)) -> tuple[bool, str]:
    h = _hour_of(at)
    if london_kz[0] <= h <= london_kz[1]:
        return True, "London KZ"
    if ny_kz[0] <= h <= ny_kz[1]:
        return True, "NY KZ"
    return False, f"Off-killzone (h={h:.1f})"


# ═══════════════════════════════════════════════════════════════════════════════
# ATR computation (for vol filter)
# ═══════════════════════════════════════════════════════════════════════════════

def compute_atr(candles: list[Candle], period: int = 14, pip_size: float = 1.0) -> float:
    if len(candles) < period:
        return 0.0
    recent = candles[-period:]
    return sum((c.high - c.low) / pip_size for c in recent) / period


# ═══════════════════════════════════════════════════════════════════════════════
# Main intraday analysis
# ═══════════════════════════════════════════════════════════════════════════════

def analyze_intraday(
    pair:                str = "xauusd",
    candles:             list = None,
    macro_events:        list[dict] = None,
    at:                  datetime | None = None,
    pip_size:            float = 1.0,
    target_pips:         int   = 50,
    sl_buffer_pips:      float = 1.5,
    min_rr:              float = 2.0,
    fvg_min_pips:        float = 1.5,
    strong_wick_pips:    float = 2.5,
    atr_min:             float = 1.0,
    atr_max:             float = 12.0,
    min_score:           int   = 60,
    enable_killzone:     bool  = True,
    enable_asian_range:  bool  = True,
    enable_news_filter:  bool  = True,
    db=None,
) -> SignalResult:
    """
    Intraday M15 XAU/USD signal analysis.

    Returns a SignalResult compatible with the swing-engine signature so
    the backtester can consume either engine variant transparently.
    """
    if candles is None:
        candles = []
    if macro_events is None:
        macro_events = []

    bars = candles
    now = at or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)

    # Defaults for the empty-WAIT path
    def _wait(reason: str, model_extras: dict | None = None) -> SignalResult:
        return SignalResult(
            signal="WAIT", quality_score=0,
            entry=None, stop_loss=None, take_profit=None,
            risk_pips=None, target_pips=target_pips, rr=None,
            invalidation=None, reason=reason, news_status="CLEAR",
            model={
                "higherTimeframeBias": "—", "liquidity": "—",
                "structure": "—", "fvg": "—", "session": "—",
                "engineVariant": "intraday",
                **(model_extras or {}),
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
            sess=detect_session(at=now),
            pair=pair, display_pair="XAU/USD",
            data_source="intraday-engine",
            setup_type="no_signal",
        )

    if len(bars) < 30:
        return _wait("Insufficient candles (<30) for intraday analysis")

    # ── Stale data guard ──────────────────────────────────────────────────
    latest_t = bars[-1].time if bars[-1].time.tzinfo else bars[-1].time.replace(tzinfo=timezone.utc)
    age_min = (now - latest_t).total_seconds() / 60
    if age_min > 30:
        return _wait(f"Stale data (latest bar {age_min:.0f} min old)")

    # ── Killzone gate (hard) ──────────────────────────────────────────────
    if enable_killzone:
        in_kz, kz_label = in_killzone(now)
        if not in_kz:
            return _wait(f"{kz_label} — intraday engine only fires inside killzones")

    # ── News filter ───────────────────────────────────────────────────────
    news = check_news_risk(macro_events, at=now, pair=pair)
    if enable_news_filter and not news.clear:
        return _wait(f"News blackout: {news.blocking_event}", {"newsStatus": "BLOCKED"})

    # ── Volatility filter ─────────────────────────────────────────────────
    atr = compute_atr(bars, period=14, pip_size=pip_size)
    if atr < atr_min or atr > atr_max:
        return _wait(f"ATR {atr:.1f} outside intraday range [{atr_min}-{atr_max}]")

    # ── Session for context ───────────────────────────────────────────────
    sess = detect_session(at=now)

    # ═══════════════════════════════════════════════════════════════════════
    # PRIMARY SETUP: Asian range break-retest
    # ═══════════════════════════════════════════════════════════════════════
    if enable_asian_range:
        ar = check_asian_range_setup(
            candles=bars, at=now, pip_size=pip_size,
            target_pips=target_pips, sl_buffer_pips=sl_buffer_pips,
            min_rr=min_rr,
        )
        if ar is not None:
            # Score the setup: clean Asian-range break-retest is high quality
            score = 80
            # Bonus if liquidity sweep aligns with direction
            liq = detect_liquidity_sweep(bars, pip_size=pip_size,
                                          strong_wick_pips=strong_wick_pips)
            if liq.swept and ((ar.direction == "BUY" and liq.bullish) or
                              (ar.direction == "SELL" and not liq.bullish)):
                score += 10
            # Bonus if FVG present in retrace zone
            fvg = detect_fair_value_gap(bars, pip_size=pip_size,
                                         fvg_min_pips=fvg_min_pips)
            if fvg.detected:
                score += 5

            return SignalResult(
                signal=ar.direction, quality_score=min(score, 100),
                entry=ar.entry, stop_loss=ar.stop_loss, take_profit=ar.take_profit,
                risk_pips=round(ar.risk_points), target_pips=target_pips,
                rr=ar.rr, invalidation=ar.stop_loss,
                reason=ar.reason, news_status=news.status,
                model={
                    "higherTimeframeBias": f"Asian H/L: {ar.asian_high:.2f}/{ar.asian_low:.2f}",
                    "liquidity":  liq.liq_text if 'liq' in dir() else "—",
                    "structure":  f"Asian range {(ar.asian_high - ar.asian_low):.1f}pt break-retest",
                    "fvg":        fvg.fvg_text if 'fvg' in dir() else "—",
                    "session":    sess.session_text,
                    "engineVariant": "intraday",
                    "setupType":     "asian_range_break_retest",
                    "asianHigh":     ar.asian_high,
                    "asianLow":      ar.asian_low,
                    "breakoutExtreme": ar.breakout_extreme,
                },
                htf=HTFResult(bullish=(ar.direction=="BUY"), score=10,
                               ema21=0, ema50=0, structure="Asian range",
                               bias_text=f"{ar.direction} retest"),
                liq=liq if 'liq' in dir() else
                     LiqResult(swept=False, bullish=False, score=0,
                               swept_level=0, liq_text="—"),
                ms=MSResult(shifted=True, bullish=(ar.direction=="BUY"),
                             score=20, pattern="ASIAN_BREAK",
                             structure_text=f"Asian range break-retest"),
                fvg=fvg if 'fvg' in dir() else
                     FVGResult(detected=False, bullish=False, score=0,
                                gap_low=0, gap_high=0, in_zone=False, fvg_text="—"),
                news=news, sess=sess,
                pair=pair, display_pair="XAU/USD",
                data_source="intraday-engine",
                setup_type="asian_range_break_retest",
            )

    # ═══════════════════════════════════════════════════════════════════════
    # FALLBACK: M15-tuned ICT trifecta
    # ═══════════════════════════════════════════════════════════════════════
    htf = detect_higher_timeframe_bias(bars)
    liq = detect_liquidity_sweep(bars, pip_size=pip_size,
                                  strong_wick_pips=strong_wick_pips)
    ms  = detect_market_structure(bars, htf_bullish=htf.bullish)
    fvg = detect_fair_value_gap(bars, pip_size=pip_size, fvg_min_pips=fvg_min_pips)

    # Re-weighted score for intraday (less weight on HTF since M15 noise is high)
    score = (
        int(htf.score * 0.5)    # HTF less critical on M15
        + liq.score
        + ms.score
        + fvg.score
        + news.score
        + sess.score
    )

    # ── Gate: 2-of-3 ICT components (looser than swing engine) ─────────────
    # Intraday M15 doesn't always produce all three. Accept setups where at
    # least 2 of {liquidity, structure, FVG} are present + score passes.
    components_present = sum([liq.swept, ms.shifted, fvg.detected])
    if components_present < 2:
        missing = []
        if not liq.swept:    missing.append("liquidity_sweep")
        if not ms.shifted:   missing.append("structure_shift")
        if not fvg.detected: missing.append("FVG")
        return _wait(f"ICT confluence too weak ({components_present}/3): missing {missing}",
                     {"engineVariant": "intraday",
                      "fallbackScore": score,
                      "componentsPresent": components_present})

    if score < min_score:
        return _wait(f"Score {score} < {min_score} required",
                     {"engineVariant": "intraday",
                      "fallbackScore": score})

    # Direction vote
    bull = sum(1 for x in (htf.bullish, liq.bullish, ms.bullish, fvg.bullish) if x)
    bear = 4 - bull
    if bull >= 3:
        direction = "BUY"
    elif bear >= 3:
        direction = "SELL"
    else:
        return _wait(f"Direction split {bull}-{bear}", {"engineVariant": "intraday"})

    price = bars[-1].close
    if direction == "BUY":
        sl = round(liq.swept_level - sl_buffer_pips * pip_size, 2)
        tp = round(price + target_pips * pip_size, 2)
        risk = (price - sl) / pip_size
    else:
        sl = round(liq.swept_level + sl_buffer_pips * pip_size, 2)
        tp = round(price - target_pips * pip_size, 2)
        risk = (sl - price) / pip_size

    if risk <= 0:
        return _wait("Invalid risk geometry")

    rr = round(target_pips / risk, 2)
    if rr < min_rr:
        return _wait(f"RR {rr} < {min_rr} required")

    setup_type = classify_setup_type(
        htf=htf, liq=liq, ms=ms, fvg=fvg, ob=None,
        sess=sess, news=news, signal=direction,
        quality_score=score, at=now, macro_events=macro_events,
    )

    return SignalResult(
        signal=direction, quality_score=score,
        entry=round(price, 2), stop_loss=sl, take_profit=tp,
        risk_pips=round(risk), target_pips=target_pips,
        rr=rr, invalidation=sl,
        reason=f"Intraday ICT trifecta: {liq.liq_text[:40]}, {ms.pattern}, FVG",
        news_status=news.status,
        model={
            "higherTimeframeBias": htf.bias_text,
            "liquidity":  liq.liq_text,
            "structure":  ms.structure_text,
            "fvg":        fvg.fvg_text,
            "session":    sess.session_text,
            "engineVariant": "intraday",
            "setupType":     setup_type,
        },
        htf=htf, liq=liq, ms=ms, fvg=fvg, news=news, sess=sess,
        pair=pair, display_pair="XAU/USD",
        data_source="intraday-engine",
        setup_type=setup_type,
    )
