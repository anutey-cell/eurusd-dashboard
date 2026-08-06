"""
Market Regime Engine — Phase 3
==============================

Independent classifier that runs BEFORE any entry strategy. It reads only
the canonical snapshot (Phase 2) — no strategy state, no trade plan. Its
job is to tell the operator *what the market is doing right now* even when
no compliant entry exists.

Output: RegimeAssessment with one of these classifications:
  STRONG_BULLISH_EXPANSION | BULLISH_CONTINUATION | BULLISH_PULLBACK |
  BULLISH_TRANSITION | BULLISH_ACCUMULATION |
  BALANCED_RANGE |
  BEARISH_ACCUMULATION | BEARISH_TRANSITION | BEARISH_PULLBACK |
  BEARISH_CONTINUATION | STRONG_BEARISH_EXPANSION |
  EXHAUSTION_OVEREXTENSION |
  HIGH_IMPACT_EVENT_RISK |
  INSUFFICIENT_DATA

Symmetric bull/bear logic — the same rules run mirror-imaged for shorts.

Behind `xauusd_market_regime_enabled`. Off by default until replay proves
value. Currently exposed only via /api/v1/diagnostics/market-regime for
shadow observation.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone, timedelta
from typing import Optional

log = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Constants — regime labels + directional buckets
# ─────────────────────────────────────────────────────────────────────────────

REGIME_STRONG_BULL         = "STRONG_BULLISH_EXPANSION"
REGIME_BULL_CONTINUATION   = "BULLISH_CONTINUATION"
REGIME_BULL_PULLBACK       = "BULLISH_PULLBACK"
REGIME_BULL_TRANSITION     = "BULLISH_TRANSITION"
REGIME_BULL_ACCUMULATION   = "BULLISH_ACCUMULATION"

REGIME_BALANCED_RANGE      = "BALANCED_RANGE"

REGIME_BEAR_ACCUMULATION   = "BEARISH_ACCUMULATION"
REGIME_BEAR_TRANSITION     = "BEARISH_TRANSITION"
REGIME_BEAR_PULLBACK       = "BEARISH_PULLBACK"
REGIME_BEAR_CONTINUATION   = "BEARISH_CONTINUATION"
REGIME_STRONG_BEAR         = "STRONG_BEARISH_EXPANSION"

REGIME_EXHAUSTION          = "EXHAUSTION_OVEREXTENSION"
REGIME_EVENT_RISK          = "HIGH_IMPACT_EVENT_RISK"
REGIME_INSUFFICIENT_DATA   = "INSUFFICIENT_DATA"

_BULLISH_REGIMES = {REGIME_STRONG_BULL, REGIME_BULL_CONTINUATION,
                     REGIME_BULL_PULLBACK, REGIME_BULL_TRANSITION,
                     REGIME_BULL_ACCUMULATION}
_BEARISH_REGIMES = {REGIME_STRONG_BEAR, REGIME_BEAR_CONTINUATION,
                     REGIME_BEAR_PULLBACK, REGIME_BEAR_TRANSITION,
                     REGIME_BEAR_ACCUMULATION}


# ─────────────────────────────────────────────────────────────────────────────
# Output dataclass
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class RegimeAssessment:
    regime:              str
    directional_bias:    str    # BULL | BEAR | NEUTRAL
    controller:          str    # BUYERS | SELLERS | BALANCED | UNCLEAR
    control_trend:       str    # STRENGTHENING | WEAKENING | STABLE
    transitioning:       bool
    accepting_above:     Optional[float]
    accepting_below:     Optional[float]
    move_maturity:       str    # EARLY | CONFIRMED | MATURE | EXTENDED | INVALIDATED | UNCLEAR
    liquidity_above:     list[float] = field(default_factory=list)
    liquidity_below:     list[float] = field(default_factory=list)
    invalidation_price:  Optional[float] = None
    confidence:          int = 0
    evidence:            list[str] = field(default_factory=list)
    warnings:            list[str] = field(default_factory=list)
    generated_at:        datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def to_dict(self) -> dict:
        return {**asdict(self),
                "generated_at": self.generated_at.isoformat()}


# ─────────────────────────────────────────────────────────────────────────────
# Small statistical helpers
# ─────────────────────────────────────────────────────────────────────────────

def _ema(vals: list[float], n: int) -> Optional[float]:
    if len(vals) < n or n <= 0:
        return None
    k = 2 / (n + 1)
    ema = sum(vals[:n]) / n
    for v in vals[n:]:
        ema = v * k + ema * (1 - k)
    return ema


def _atr_from_bars(bars: list, n: int = 14) -> Optional[float]:
    """Wilder ATR from a list of Bar-like objects (has .high .low .close)."""
    if len(bars) < n + 1:
        return None
    trs: list[float] = []
    for i in range(1, len(bars)):
        h, l, pc = bars[i].high, bars[i].low, bars[i-1].close
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    if len(trs) < n:
        return None
    atr = sum(trs[:n]) / n
    for tr in trs[n:]:
        atr = ((atr * (n - 1)) + tr) / n
    return atr


def _swing_high_low(bars: list, k: int = 3):
    """Find the last confirmed swing high + swing low using k-bar pivot rule."""
    if len(bars) < 2 * k + 1:
        return (None, None)
    last_high = None
    last_low = None
    for i in range(k, len(bars) - k):
        h, l = bars[i].high, bars[i].low
        if all(bars[j].high <= h for j in range(i - k, i + k + 1) if j != i):
            last_high = h
        if all(bars[j].low >= l for j in range(i - k, i + k + 1) if j != i):
            last_low = l
    return (last_high, last_low)


def _body_pct(bar) -> float:
    rng = bar.high - bar.low
    if rng <= 0:
        return 0.0
    return abs(bar.close - bar.open) / rng


# ─────────────────────────────────────────────────────────────────────────────
# Core classification helpers
# ─────────────────────────────────────────────────────────────────────────────

def _htf_bias(closes_h4: list[float]) -> str:
    """H4 EMA20 vs EMA50 relationship + slope."""
    if len(closes_h4) < 55:
        return "NEUTRAL"
    e20 = _ema(closes_h4, 20)
    e50 = _ema(closes_h4, 50)
    if e20 is None or e50 is None:
        return "NEUTRAL"
    # Slope proxy: current vs 10 bars ago
    prior_e20 = _ema(closes_h4[:-10], 20) if len(closes_h4) >= 30 else None
    slope_up = prior_e20 is not None and e20 > prior_e20
    slope_down = prior_e20 is not None and e20 < prior_e20
    if e20 > e50 and slope_up:
        return "BULL"
    if e20 < e50 and slope_down:
        return "BEAR"
    return "NEUTRAL"


def _htf_momentum_state(bars_h1: list) -> str:
    """H1 short-term momentum: STRONG_UP | UP | FLAT | DOWN | STRONG_DOWN."""
    if len(bars_h1) < 22:
        return "FLAT"
    closes = [b.close for b in bars_h1]
    e8  = _ema(closes, 8)
    e21 = _ema(closes, 21)
    if e8 is None or e21 is None:
        return "FLAT"
    prior_e8 = _ema(closes[:-3], 8) if len(closes) >= 11 else None
    diff = e8 - e21
    if diff > 0 and prior_e8 is not None and e8 > prior_e8:
        return "STRONG_UP" if diff / e21 > 0.001 else "UP"
    if diff < 0 and prior_e8 is not None and e8 < prior_e8:
        return "STRONG_DOWN" if abs(diff) / e21 > 0.001 else "DOWN"
    return "FLAT"


def _displacement_signal(bars_m15: list, atr_h1: Optional[float]) -> str:
    """
    Look at the last 6 M15 bars. Detect strong directional displacement:
      STRONG_UP   if ≥3 of last 6 are large-bodied greens
      STRONG_DOWN if ≥3 of last 6 are large-bodied reds
      FLAT        otherwise
    "Large-bodied" = body >= 0.6 × range AND body >= 0.35 × atr_h1
    """
    if len(bars_m15) < 6 or atr_h1 is None or atr_h1 <= 0:
        return "FLAT"
    tail = bars_m15[-6:]
    thr = 0.35 * atr_h1
    big_up = sum(1 for b in tail if b.close > b.open and _body_pct(b) >= 0.6
                 and (b.close - b.open) >= thr)
    big_dn = sum(1 for b in tail if b.close < b.open and _body_pct(b) >= 0.6
                 and (b.open - b.close) >= thr)
    if big_up >= 3 and big_up > big_dn:
        return "STRONG_UP"
    if big_dn >= 3 and big_dn > big_up:
        return "STRONG_DOWN"
    return "FLAT"


def _bos_direction(bars_m15: list, swing_hi: Optional[float],
                    swing_lo: Optional[float]) -> str:
    """Break of structure: did the last few M15 closes take out swing?"""
    if not bars_m15 or (swing_hi is None and swing_lo is None):
        return "NONE"
    last_close = bars_m15[-1].close
    if swing_hi is not None and last_close > swing_hi:
        return "UP"
    if swing_lo is not None and last_close < swing_lo:
        return "DOWN"
    return "NONE"


def _acceptance_above(bars_m15: list, level: float, min_bars: int = 2) -> bool:
    """Two+ M15 CLOSES above the level, and price didn't reclaim back below."""
    if not bars_m15 or level is None:
        return False
    tail = bars_m15[-6:]
    closes_above = sum(1 for b in tail if b.close > level)
    if closes_above < min_bars:
        return False
    # Did we dip back below and NOT reclaim?
    return bars_m15[-1].close > level


def _acceptance_below(bars_m15: list, level: float, min_bars: int = 2) -> bool:
    if not bars_m15 or level is None:
        return False
    tail = bars_m15[-6:]
    closes_below = sum(1 for b in tail if b.close < level)
    if closes_below < min_bars:
        return False
    return bars_m15[-1].close < level


def _extension_atr_multiple(price: float, ref_price: float,
                              atr_h1: Optional[float]) -> Optional[float]:
    if atr_h1 is None or atr_h1 <= 0 or ref_price is None:
        return None
    return abs(price - ref_price) / atr_h1


def _range_is_tight(bars_m15: list, atr_h1: Optional[float],
                     tight_multiple: float = 1.0) -> bool:
    """Recent M15 range < tight_multiple × H1 ATR = coiling / accumulation."""
    if len(bars_m15) < 12 or atr_h1 is None or atr_h1 <= 0:
        return False
    recent = bars_m15[-12:]
    span = max(b.high for b in recent) - min(b.low for b in recent)
    return span < tight_multiple * atr_h1


def _event_risk_within(events: list, minutes: int = 15) -> bool:
    """Any high-impact calendar event within `minutes` of now."""
    if not events:
        return False
    now = datetime.now(timezone.utc)
    window_end = now + timedelta(minutes=minutes)
    for ev in events:
        try:
            ts = ev.get("time_utc") if isinstance(ev, dict) else getattr(ev, "time_utc", None)
            if isinstance(ts, str):
                ts = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            impact = ev.get("impact") if isinstance(ev, dict) else getattr(ev, "impact", "")
            if ts and impact and "high" in str(impact).lower() and now <= ts <= window_end:
                return True
        except Exception:
            continue
    return False


# ─────────────────────────────────────────────────────────────────────────────
# Public: classify_regime(snapshot, upcoming_events)
# ─────────────────────────────────────────────────────────────────────────────

def classify_regime(snapshot, upcoming_events: Optional[list] = None) -> RegimeAssessment:
    """
    Take a CanonicalSnapshot and return the 14-way regime classification.

    Fails open — always returns a RegimeAssessment, never raises. Missing/
    stale data collapses to INSUFFICIENT_DATA with warnings.
    """
    warnings: list[str] = []
    evidence: list[str] = []

    # ── 0) Guard: insufficient data ──────────────────────────────────────────
    if snapshot is None:
        return RegimeAssessment(
            regime=REGIME_INSUFFICIENT_DATA, directional_bias="NEUTRAL",
            controller="UNCLEAR", control_trend="STABLE",
            transitioning=False, accepting_above=None, accepting_below=None,
            move_maturity="UNCLEAR", warnings=["snapshot is None"],
        )
    if (snapshot.data_quality_score or 0) < 50:
        warnings.append(f"data_quality_score={snapshot.data_quality_score}<50")
        return RegimeAssessment(
            regime=REGIME_INSUFFICIENT_DATA, directional_bias="NEUTRAL",
            controller="UNCLEAR", control_trend="STABLE",
            transitioning=False, accepting_above=None, accepting_below=None,
            move_maturity="UNCLEAR", warnings=warnings, confidence=0,
        )

    tfs = snapshot.timeframes or {}
    bars_h4  = tfs.get("H4",  None).candles if tfs.get("H4")  else []
    bars_h1  = tfs.get("H1",  None).candles if tfs.get("H1")  else []
    bars_m15 = tfs.get("M15", None).candles if tfs.get("M15") else []
    if len(bars_h4) < 30 or len(bars_h1) < 30 or len(bars_m15) < 20:
        warnings.append("insufficient bars (H4<30 or H1<30 or M15<20)")
        return RegimeAssessment(
            regime=REGIME_INSUFFICIENT_DATA, directional_bias="NEUTRAL",
            controller="UNCLEAR", control_trend="STABLE",
            transitioning=False, accepting_above=None, accepting_below=None,
            move_maturity="UNCLEAR", warnings=warnings, confidence=0,
        )

    # ── 1) Event risk shortcut ───────────────────────────────────────────────
    if _event_risk_within(upcoming_events or [], minutes=15):
        evidence.append("high-impact event within 15 minutes")
        return RegimeAssessment(
            regime=REGIME_EVENT_RISK, directional_bias="NEUTRAL",
            controller="UNCLEAR", control_trend="STABLE",
            transitioning=False, accepting_above=None, accepting_below=None,
            move_maturity="UNCLEAR", warnings=warnings, evidence=evidence,
            confidence=90,
        )

    # ── 2) Compute inputs ────────────────────────────────────────────────────
    closes_h4 = [b.close for b in bars_h4]
    htf_bias = _htf_bias(closes_h4)
    momo     = _htf_momentum_state(bars_h1)
    atr_h1   = _atr_from_bars(bars_h1, 14)
    disp     = _displacement_signal(bars_m15, atr_h1)
    swing_hi, swing_lo = _swing_high_low(bars_m15, k=3)
    bos      = _bos_direction(bars_m15, swing_hi, swing_lo)
    tight_range = _range_is_tight(bars_m15, atr_h1, tight_multiple=1.0)

    levels = snapshot.levels
    current_price = bars_m15[-1].close if bars_m15 else None
    pdh_broken = levels.pdh is not None and current_price is not None and current_price > levels.pdh
    pdl_broken = levels.pdl is not None and current_price is not None and current_price < levels.pdl
    ah_broken  = levels.asian_high is not None and current_price is not None and current_price > levels.asian_high
    al_broken  = levels.asian_low  is not None and current_price is not None and current_price < levels.asian_low

    accept_above_pdh = _acceptance_above(bars_m15, levels.pdh) if levels.pdh else False
    accept_above_ah  = _acceptance_above(bars_m15, levels.asian_high) if levels.asian_high else False
    accept_below_pdl = _acceptance_below(bars_m15, levels.pdl) if levels.pdl else False
    accept_below_al  = _acceptance_below(bars_m15, levels.asian_low) if levels.asian_low else False

    # Extension check: distance from H1 EMA21 in ATR multiples
    h1_ema21 = _ema([b.close for b in bars_h1], 21)
    ext_mult = _extension_atr_multiple(current_price, h1_ema21, atr_h1)

    evidence.append(f"HTF bias={htf_bias}")
    evidence.append(f"H1 momentum={momo}")
    evidence.append(f"M15 displacement={disp}")
    evidence.append(f"BOS={bos}  (swing_hi={swing_hi} swing_lo={swing_lo})")
    if atr_h1: evidence.append(f"ATR_H1={atr_h1:.2f}")
    if ext_mult is not None: evidence.append(f"extension_atr_mult={ext_mult:.2f}")
    if tight_range: evidence.append("recent 12×M15 range is tight (<1×ATR)")

    # ── 3) Regime decision tree ──────────────────────────────────────────────
    regime = REGIME_BALANCED_RANGE
    directional_bias = "NEUTRAL"
    controller = "BALANCED"
    control_trend = "STABLE"
    transitioning = False
    move_maturity = "UNCLEAR"
    accepting_above = None
    accepting_below = None
    invalidation_price = None
    confidence = 50

    # 3a) EXHAUSTION overrides only when the move is TRULY stretched AND
    # displacement has already stalled. A move that is 5× extended but
    # STILL displacing hard is still a strong expansion — not exhausted.
    # We call exhaustion only when:
    #   ext_mult >= 4.0 (very stretched from H1 EMA21)
    #   AND recent displacement is FLAT or against direction
    #   AND HTF bias is directional
    disp_still_with_trend = (
        (htf_bias == "BULL" and disp == "STRONG_UP") or
        (htf_bias == "BEAR" and disp == "STRONG_DOWN")
    )
    if (ext_mult is not None and ext_mult >= 4.0
            and htf_bias in ("BULL", "BEAR")
            and not disp_still_with_trend):
        regime = REGIME_EXHAUSTION
        directional_bias = "BULL" if htf_bias == "BULL" else "BEAR"
        controller = "BUYERS" if directional_bias == "BULL" else "SELLERS"
        control_trend = "WEAKENING"
        move_maturity = "EXTENDED"
        confidence = 75
        evidence.append(f"extended {ext_mult:.1f}× ATR from H1 EMA21 with stalled displacement → EXHAUSTION")
        invalidation_price = swing_lo if directional_bias == "BULL" else swing_hi
        return _finalize(regime, directional_bias, controller, control_trend,
                         transitioning, accepting_above, accepting_below,
                         move_maturity, levels, invalidation_price,
                         confidence, evidence, warnings)

    # 3b) BULLISH branch
    if htf_bias == "BULL":
        directional_bias = "BULL"
        controller = "BUYERS"
        # Strong expansion doesn't require formal M15 pivot BOS — displacement
        # + level acceptance is the market-structure signal. The recent
        # big-body bars ARE the BOS.
        if disp == "STRONG_UP" and (accept_above_pdh or accept_above_ah or bos == "UP"):
            regime = REGIME_STRONG_BULL
            control_trend = "STRENGTHENING"
            move_maturity = "CONFIRMED"
            confidence = 85
            accepting_above = (levels.pdh if accept_above_pdh
                                else (levels.asian_high if accept_above_ah else None))
            evidence.append("HTF bull + M15 displacement + level acceptance / BOS up")
        elif bos == "UP":
            regime = REGIME_BULL_CONTINUATION
            control_trend = "STABLE"
            move_maturity = "CONFIRMED"
            confidence = 70
            evidence.append("HTF bull + BOS up (continuation)")
        elif tight_range and pdh_broken:
            regime = REGIME_BULL_ACCUMULATION
            control_trend = "STABLE"
            move_maturity = "EARLY"
            confidence = 60
            evidence.append("HTF bull + coiling near/above PDH (accumulation)")
        elif momo in ("UP", "STRONG_UP") and disp != "STRONG_DOWN":
            regime = REGIME_BULL_PULLBACK
            control_trend = "STABLE"
            move_maturity = "CONFIRMED"
            confidence = 65
            evidence.append("HTF bull + shallow pullback in progress")
            invalidation_price = swing_lo
        else:
            regime = REGIME_BULL_ACCUMULATION
            move_maturity = "EARLY"
            confidence = 55
            evidence.append("HTF bull with unclear near-term action")
        invalidation_price = invalidation_price or swing_lo

    # 3c) BEARISH branch (mirror)
    elif htf_bias == "BEAR":
        directional_bias = "BEAR"
        controller = "SELLERS"
        if disp == "STRONG_DOWN" and (accept_below_pdl or accept_below_al or bos == "DOWN"):
            regime = REGIME_STRONG_BEAR
            control_trend = "STRENGTHENING"
            move_maturity = "CONFIRMED"
            confidence = 85
            accepting_below = (levels.pdl if accept_below_pdl
                                else (levels.asian_low if accept_below_al else None))
            evidence.append("HTF bear + M15 displacement + level acceptance / BOS down")
        elif bos == "DOWN":
            regime = REGIME_BEAR_CONTINUATION
            control_trend = "STABLE"
            move_maturity = "CONFIRMED"
            confidence = 70
            evidence.append("HTF bear + BOS down (continuation)")
        elif tight_range and pdl_broken:
            regime = REGIME_BEAR_ACCUMULATION
            control_trend = "STABLE"
            move_maturity = "EARLY"
            confidence = 60
            evidence.append("HTF bear + coiling near/below PDL (accumulation)")
        elif momo in ("DOWN", "STRONG_DOWN") and disp != "STRONG_UP":
            regime = REGIME_BEAR_PULLBACK
            control_trend = "STABLE"
            move_maturity = "CONFIRMED"
            confidence = 65
            evidence.append("HTF bear + shallow pullback higher")
            invalidation_price = swing_hi
        else:
            regime = REGIME_BEAR_ACCUMULATION
            move_maturity = "EARLY"
            confidence = 55
            evidence.append("HTF bear with unclear near-term action")
        invalidation_price = invalidation_price or swing_hi

    # 3d) NEUTRAL branch — transition detection
    else:
        # BULLISH_TRANSITION: HTF neutral + H1 BOS up + M15 displacement + accepting above key level
        if bos == "UP" and disp == "STRONG_UP" and (ah_broken or pdh_broken):
            regime = REGIME_BULL_TRANSITION
            directional_bias = "BULL"
            controller = "BUYERS"
            control_trend = "STRENGTHENING"
            transitioning = True
            move_maturity = "EARLY"
            confidence = 70
            accepting_above = levels.pdh if ah_broken and pdh_broken else levels.asian_high
            evidence.append("HTF neutral + BOS up + displacement + broke Asian/PD high → transition")
            invalidation_price = swing_lo
        # BEARISH_TRANSITION: mirror
        elif bos == "DOWN" and disp == "STRONG_DOWN" and (al_broken or pdl_broken):
            regime = REGIME_BEAR_TRANSITION
            directional_bias = "BEAR"
            controller = "SELLERS"
            control_trend = "STRENGTHENING"
            transitioning = True
            move_maturity = "EARLY"
            confidence = 70
            accepting_below = levels.pdl if al_broken and pdl_broken else levels.asian_low
            evidence.append("HTF neutral + BOS down + displacement + broke Asian/PD low → transition")
            invalidation_price = swing_hi
        # BALANCED_RANGE default
        else:
            regime = REGIME_BALANCED_RANGE
            controller = "BALANCED"
            move_maturity = "UNCLEAR"
            confidence = 55
            evidence.append("no directional edge — balanced range")

    return _finalize(regime, directional_bias, controller, control_trend,
                      transitioning, accepting_above, accepting_below,
                      move_maturity, levels, invalidation_price,
                      confidence, evidence, warnings)


def _finalize(regime, dbias, controller, ctrend, transitioning,
              accepting_above, accepting_below, move_maturity, levels,
              invalidation_price, confidence, evidence, warnings) -> RegimeAssessment:
    """Bundle level pointers into liquidity_above / liquidity_below."""
    liquidity_above = [x for x in (levels.pdh, levels.pwh, levels.asian_high) if x is not None]
    liquidity_below = [x for x in (levels.pdl, levels.pwl, levels.asian_low) if x is not None]
    return RegimeAssessment(
        regime=regime, directional_bias=dbias,
        controller=controller, control_trend=ctrend,
        transitioning=transitioning,
        accepting_above=accepting_above, accepting_below=accepting_below,
        move_maturity=move_maturity,
        liquidity_above=sorted(set(liquidity_above)),
        liquidity_below=sorted(set(liquidity_below), reverse=True),
        invalidation_price=invalidation_price,
        confidence=confidence, evidence=evidence, warnings=warnings,
    )


__all__ = [
    "classify_regime", "RegimeAssessment",
    "REGIME_STRONG_BULL", "REGIME_BULL_CONTINUATION", "REGIME_BULL_PULLBACK",
    "REGIME_BULL_TRANSITION", "REGIME_BULL_ACCUMULATION",
    "REGIME_BALANCED_RANGE",
    "REGIME_BEAR_ACCUMULATION", "REGIME_BEAR_TRANSITION",
    "REGIME_BEAR_PULLBACK", "REGIME_BEAR_CONTINUATION", "REGIME_STRONG_BEAR",
    "REGIME_EXHAUSTION", "REGIME_EVENT_RISK", "REGIME_INSUFFICIENT_DATA",
]
