"""
Directional Evidence & Contradiction Scoring — Phase 5
========================================================

Replaces the current additive confluence with 8 separate scores. The brief:
"Do not use only additive confluence. Contradictory evidence must reduce
confidence — but never automatically block a directional thesis."

Outputs:
  bull_evidence_score        0-100    weighted sum of bull evidence items
  bear_evidence_score        0-100    weighted sum of bear evidence items
  contradiction_score        0-100    penalty (higher = more contradictions)
  data_quality_score         0-100    from canonical snapshot
  event_risk_score           0-100    proximity to high-impact news
  extension_risk_score       0-100    how stretched from H1 EMA21 in ATR
  directional_confidence     0-100    net confidence in the dominant side
  entry_quality_confidence   0-100    directional × data_quality (0-100 scale)

Feature flag `xauusd_directional_intelligence_enabled`. Off until validated.
Read-only via /api/v1/diagnostics/directional-evidence — no strategy consumes
it yet (that comes in Phase 8).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone, timedelta
from typing import Optional

log = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Output shape
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class EvidenceItem:
    name:         str         # short slug, e.g. "H1_BOS_UP"
    weight:       int         # points contributed (positive)
    description:  str         # human-readable one-liner

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class EvidenceAssessment:
    dominant_direction:       str           # BULL | BEAR | NEUTRAL

    bull_evidence_score:      int
    bear_evidence_score:      int
    contradiction_score:      int
    data_quality_score:       int
    event_risk_score:         int
    extension_risk_score:     int
    directional_confidence:   int
    entry_quality_confidence: int

    bull_items:      list[EvidenceItem] = field(default_factory=list)
    bear_items:      list[EvidenceItem] = field(default_factory=list)
    contradictions:  list[EvidenceItem] = field(default_factory=list)
    warnings:        list[str] = field(default_factory=list)
    generated_at:    datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def to_dict(self) -> dict:
        return {
            "dominant_direction":       self.dominant_direction,
            "bull_evidence_score":      self.bull_evidence_score,
            "bear_evidence_score":      self.bear_evidence_score,
            "contradiction_score":      self.contradiction_score,
            "data_quality_score":       self.data_quality_score,
            "event_risk_score":         self.event_risk_score,
            "extension_risk_score":     self.extension_risk_score,
            "directional_confidence":   self.directional_confidence,
            "entry_quality_confidence": self.entry_quality_confidence,
            "bull_items":     [i.to_dict() for i in self.bull_items],
            "bear_items":     [i.to_dict() for i in self.bear_items],
            "contradictions": [i.to_dict() for i in self.contradictions],
            "warnings":       self.warnings,
            "generated_at":   self.generated_at.isoformat(),
        }


# ─────────────────────────────────────────────────────────────────────────────
# Weight table (all bull-side; bear mirrors exactly)
# ─────────────────────────────────────────────────────────────────────────────

_BULL_WEIGHTS = {
    "HH_HL_STRUCTURE":         12,      # higher highs + higher lows
    "H1_BOS_UP":               15,      # H1 broke swing high
    "M15_BOS_UP":              10,      # M15 broke swing high
    "SELLSIDE_SWEEP_RECLAIM":  10,      # swept PDL/AL then reclaimed
    "ASIAN_HIGH_BROKEN":        8,
    "PDH_BROKEN":              10,
    "BULLISH_DISPLACEMENT":    10,      # big-body M15 up bars
    "PRICE_ABOVE_DAILY_OPEN":   6,
    "PRICE_ABOVE_SESSION_VWAP": 5,
    "SHALLOW_PULLBACKS":        4,
    "PROTECTED_LOW_INTACT":     5,
    "BULLISH_ACCEPTANCE":       8,      # 2+ M15 closes past a key level
    "DXY_INVERSE_SUPPORTIVE":   4,
    "YIELDS_SUPPORTIVE":        3,
}

_BEAR_WEIGHTS = {
    "LH_LL_STRUCTURE":         12,
    "H1_BOS_DOWN":             15,
    "M15_BOS_DOWN":            10,
    "BUYSIDE_SWEEP_REJECT":    10,
    "ASIAN_LOW_BROKEN":         8,
    "PDL_BROKEN":              10,
    "BEARISH_DISPLACEMENT":    10,
    "PRICE_BELOW_DAILY_OPEN":   6,
    "PRICE_BELOW_SESSION_VWAP": 5,
    "SHALLOW_RALLIES":          4,
    "PROTECTED_HIGH_INTACT":    5,
    "BEARISH_ACCEPTANCE":       8,
    "DXY_STRONG_SUPPORTIVE":    4,
    "YIELDS_UP_SUPPORTIVE":     3,
}

_CONTRADICTION_WEIGHTS = {
    "UNDER_H4_SUPPLY":           12,     # bull setup near H4 resistance
    "ABOVE_H4_DEMAND":           12,     # bear setup near H4 support
    "IMMEDIATE_RETURN_TO_RANGE": 15,     # breakout candle reversed
    "DAILY_ATR_CONSUMED":        10,     # >75% of ATR spent
    "MOMENTUM_DIVERGENCE":       10,
    "DXY_OPPOSING":              8,
    "EXCESSIVE_SPREAD":           7,
    "NEWS_APPROACHING":          12,
    "FAILED_FOLLOWTHROUGH":      10,
    "STRUCTURE_CONFLICT":        10,     # HTF alignment score disagrees with regime
}


# ─────────────────────────────────────────────────────────────────────────────
# Small helpers
# ─────────────────────────────────────────────────────────────────────────────

def _clip(v, lo=0, hi=100):
    return max(lo, min(hi, int(round(v))))


def _atr_from_bars(bars, n=14):
    if len(bars) < n + 1:
        return None
    trs = []
    for i in range(1, len(bars)):
        h, l, pc = bars[i].high, bars[i].low, bars[i-1].close
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    if len(trs) < n:
        return None
    atr = sum(trs[:n]) / n
    for tr in trs[n:]:
        atr = ((atr * (n - 1)) + tr) / n
    return atr


def _ema(vals, n):
    if len(vals) < n:
        return None
    k = 2 / (n + 1)
    e = sum(vals[:n]) / n
    for v in vals[n:]:
        e = v * k + e * (1 - k)
    return e


def _swing_hi_lo(bars, k=3):
    if len(bars) < 2 * k + 1:
        return (None, None)
    hi = lo = None
    for i in range(k, len(bars) - k):
        h, l = bars[i].high, bars[i].low
        if all(bars[j].high <= h for j in range(i - k, i + k + 1) if j != i):
            hi = h
        if all(bars[j].low >= l for j in range(i - k, i + k + 1) if j != i):
            lo = l
    return (hi, lo)


def _has_ascending_hh_hl(bars, min_pivots=3):
    """Look at recent M15 swings — do highs go up AND lows go up?"""
    if len(bars) < 20:
        return False
    # Grab the last few local pivots by simple 2-bar rule
    highs, lows = [], []
    for i in range(2, len(bars) - 2):
        h, l = bars[i].high, bars[i].low
        if all(bars[j].high < h for j in (i-2, i-1, i+1, i+2)):
            highs.append((i, h))
        if all(bars[j].low > l for j in (i-2, i-1, i+1, i+2)):
            lows.append((i, l))
    if len(highs) < min_pivots or len(lows) < min_pivots:
        return False
    recent_h = [h for _, h in highs[-min_pivots:]]
    recent_l = [l for _, l in lows[-min_pivots:]]
    return all(recent_h[i] > recent_h[i-1] for i in range(1, min_pivots)) \
        and all(recent_l[i] > recent_l[i-1] for i in range(1, min_pivots))


def _has_descending_lh_ll(bars, min_pivots=3):
    if len(bars) < 20:
        return False
    highs, lows = [], []
    for i in range(2, len(bars) - 2):
        h, l = bars[i].high, bars[i].low
        if all(bars[j].high < h for j in (i-2, i-1, i+1, i+2)):
            highs.append((i, h))
        if all(bars[j].low > l for j in (i-2, i-1, i+1, i+2)):
            lows.append((i, l))
    if len(highs) < min_pivots or len(lows) < min_pivots:
        return False
    recent_h = [h for _, h in highs[-min_pivots:]]
    recent_l = [l for _, l in lows[-min_pivots:]]
    return all(recent_h[i] < recent_h[i-1] for i in range(1, min_pivots)) \
        and all(recent_l[i] < recent_l[i-1] for i in range(1, min_pivots))


def _bos_up(bars_m15, k=3):
    swing_hi, _ = _swing_hi_lo(bars_m15, k=k)
    return swing_hi is not None and bars_m15[-1].close > swing_hi


def _bos_down(bars_m15, k=3):
    _, swing_lo = _swing_hi_lo(bars_m15, k=k)
    return swing_lo is not None and bars_m15[-1].close < swing_lo


def _bos_up_h1(bars_h1, k=3):
    swing_hi, _ = _swing_hi_lo(bars_h1, k=k)
    return swing_hi is not None and bars_h1[-1].close > swing_hi


def _bos_down_h1(bars_h1, k=3):
    _, swing_lo = _swing_hi_lo(bars_h1, k=k)
    return swing_lo is not None and bars_h1[-1].close < swing_lo


def _sweep_and_reclaim_bull(bars_m15, level, lookback=6):
    """Recent M15 bar wicked BELOW level then closed back above."""
    if level is None or len(bars_m15) < lookback:
        return False
    for b in bars_m15[-lookback:]:
        if b.low < level and b.close > level:
            return True
    return False


def _sweep_and_reject_bear(bars_m15, level, lookback=6):
    """Recent M15 bar wicked ABOVE level then closed back below."""
    if level is None or len(bars_m15) < lookback:
        return False
    for b in bars_m15[-lookback:]:
        if b.high > level and b.close < level:
            return True
    return False


def _acceptance_above(bars_m15, level, min_bars=2):
    if level is None or len(bars_m15) < min_bars:
        return False
    tail = bars_m15[-6:]
    return sum(1 for b in tail if b.close > level) >= min_bars and bars_m15[-1].close > level


def _acceptance_below(bars_m15, level, min_bars=2):
    if level is None or len(bars_m15) < min_bars:
        return False
    tail = bars_m15[-6:]
    return sum(1 for b in tail if b.close < level) >= min_bars and bars_m15[-1].close < level


def _body_pct(b):
    rng = b.high - b.low
    return 0.0 if rng <= 0 else abs(b.close - b.open) / rng


def _displacement_up(bars_m15, atr_h1, tail=6, threshold=3):
    if atr_h1 is None or len(bars_m15) < tail:
        return False
    thr = 0.35 * atr_h1
    return sum(1 for b in bars_m15[-tail:]
                if b.close > b.open and _body_pct(b) >= 0.6 and (b.close - b.open) >= thr
               ) >= threshold


def _displacement_down(bars_m15, atr_h1, tail=6, threshold=3):
    if atr_h1 is None or len(bars_m15) < tail:
        return False
    thr = 0.35 * atr_h1
    return sum(1 for b in bars_m15[-tail:]
                if b.close < b.open and _body_pct(b) >= 0.6 and (b.open - b.close) >= thr
               ) >= threshold


def _shallow_pullbacks(bars_m15, atr_h1, direction, tail=12):
    """Recent counter-direction wicks are all < 0.5 × ATR."""
    if atr_h1 is None or len(bars_m15) < tail:
        return False
    thr = 0.5 * atr_h1
    if direction == "BULL":
        # Pullbacks = red bars; check max down-body against atr
        pullback_body = [abs(b.open - b.close) for b in bars_m15[-tail:]
                         if b.close < b.open]
        return bool(pullback_body) and max(pullback_body) < thr
    if direction == "BEAR":
        pullback_body = [abs(b.close - b.open) for b in bars_m15[-tail:]
                         if b.close > b.open]
        return bool(pullback_body) and max(pullback_body) < thr
    return False


def _session_vwap(bars_m15, session_open):
    """Cumulative typical-price × volume from session_open. Falls back to
    plain arithmetic mean when volume is 0."""
    if not bars_m15 or session_open is None:
        return None
    session_bars = [b for b in bars_m15 if b.time >= session_open]
    if not session_bars:
        return None
    typ = [(b.high + b.low + b.close) / 3 for b in session_bars]
    vols = [b.volume or 0 for b in session_bars]
    if sum(vols) > 0:
        return sum(t * v for t, v in zip(typ, vols)) / sum(vols)
    return sum(typ) / len(typ)


def _event_risk_within(events, minutes=30):
    if not events:
        return False
    now = datetime.now(timezone.utc)
    end = now + timedelta(minutes=minutes)
    for ev in events:
        try:
            ts = ev.get("time_utc") if isinstance(ev, dict) else getattr(ev, "time_utc", None)
            if isinstance(ts, str):
                ts = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            impact = str(ev.get("impact", "") if isinstance(ev, dict)
                          else getattr(ev, "impact", "")).lower()
            if ts and "high" in impact and now <= ts <= end:
                return True
        except Exception:
            continue
    return False


def _minutes_to_next_high_impact(events):
    if not events:
        return None
    now = datetime.now(timezone.utc)
    best = None
    for ev in events:
        try:
            ts = ev.get("time_utc") if isinstance(ev, dict) else getattr(ev, "time_utc", None)
            if isinstance(ts, str):
                ts = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            impact = str(ev.get("impact", "") if isinstance(ev, dict)
                          else getattr(ev, "impact", "")).lower()
            if ts and "high" in impact and ts >= now:
                m = (ts - now).total_seconds() / 60
                if best is None or m < best:
                    best = m
        except Exception:
            continue
    return best


# ─────────────────────────────────────────────────────────────────────────────
# Public entry point
# ─────────────────────────────────────────────────────────────────────────────

def compute_directional_evidence(
    snapshot,
    *,
    htf_alignment=None,
    regime=None,
    upcoming_events: Optional[list] = None,
    macro_context: Optional[dict] = None,
    spread_threshold: float = 5.0,
) -> EvidenceAssessment:
    """
    Given a CanonicalSnapshot (+ optional supporting artifacts), produce the
    8-score evidence assessment. Fails open — always returns an assessment.
    """
    warnings: list[str] = []
    bull_items: list[EvidenceItem] = []
    bear_items: list[EvidenceItem] = []
    contradictions: list[EvidenceItem] = []

    if snapshot is None:
        return EvidenceAssessment(
            dominant_direction="NEUTRAL",
            bull_evidence_score=0, bear_evidence_score=0,
            contradiction_score=0, data_quality_score=0,
            event_risk_score=0, extension_risk_score=0,
            directional_confidence=0, entry_quality_confidence=0,
            warnings=["snapshot is None"],
        )

    tfs = snapshot.timeframes or {}
    bars_h4  = tfs.get("H4",  None).candles if tfs.get("H4")  else []
    bars_h1  = tfs.get("H1",  None).candles if tfs.get("H1")  else []
    bars_m15 = tfs.get("M15", None).candles if tfs.get("M15") else []
    levels   = snapshot.levels

    if len(bars_h1) < 25 or len(bars_m15) < 20:
        warnings.append("insufficient bars for evidence scoring")
        return EvidenceAssessment(
            dominant_direction="NEUTRAL",
            bull_evidence_score=0, bear_evidence_score=0,
            contradiction_score=0,
            data_quality_score=snapshot.data_quality_score or 0,
            event_risk_score=0, extension_risk_score=0,
            directional_confidence=0, entry_quality_confidence=0,
            warnings=warnings,
        )

    atr_h1 = _atr_from_bars(bars_h1, 14)
    current_price = bars_m15[-1].close

    # ── BULL evidence ─────────────────────────────────────────────────────
    def _add_bull(key, desc):
        bull_items.append(EvidenceItem(key, _BULL_WEIGHTS[key], desc))

    if _has_ascending_hh_hl(bars_m15):
        _add_bull("HH_HL_STRUCTURE", "M15 higher-highs + higher-lows sequence intact")
    if _bos_up_h1(bars_h1):
        _add_bull("H1_BOS_UP", f"H1 close {bars_h1[-1].close:.2f} above recent swing high")
    if _bos_up(bars_m15):
        _add_bull("M15_BOS_UP", f"M15 close {bars_m15[-1].close:.2f} above swing high")
    if _sweep_and_reclaim_bull(bars_m15, levels.pdl):
        _add_bull("SELLSIDE_SWEEP_RECLAIM", f"swept PDL {levels.pdl} and reclaimed")
    elif _sweep_and_reclaim_bull(bars_m15, levels.asian_low):
        _add_bull("SELLSIDE_SWEEP_RECLAIM", f"swept Asian low {levels.asian_low} and reclaimed")
    if levels.asian_high and current_price > levels.asian_high:
        _add_bull("ASIAN_HIGH_BROKEN", f"price {current_price:.2f} above Asian high {levels.asian_high:.2f}")
    if levels.pdh and current_price > levels.pdh:
        _add_bull("PDH_BROKEN", f"price {current_price:.2f} above PDH {levels.pdh:.2f}")
    if _displacement_up(bars_m15, atr_h1):
        _add_bull("BULLISH_DISPLACEMENT", "3+ of last 6 M15 bars big-body greens")
    if levels.daily_open and current_price > levels.daily_open:
        _add_bull("PRICE_ABOVE_DAILY_OPEN", f"holding above daily open {levels.daily_open:.2f}")
    vwap = _session_vwap(bars_m15, snapshot.session.session_open) if snapshot.session else None
    if vwap and current_price > vwap:
        _add_bull("PRICE_ABOVE_SESSION_VWAP", f"above session VWAP {vwap:.2f}")
    if _shallow_pullbacks(bars_m15, atr_h1, "BULL"):
        _add_bull("SHALLOW_PULLBACKS", "recent counter-moves < 0.5 × ATR")
    # Protected low = swing low still intact
    _, swing_lo = _swing_hi_lo(bars_m15, k=3)
    if swing_lo and current_price > swing_lo and min(b.low for b in bars_m15[-6:]) >= swing_lo:
        _add_bull("PROTECTED_LOW_INTACT", f"swing low {swing_lo:.2f} not violated")
    if _acceptance_above(bars_m15, levels.pdh) or _acceptance_above(bars_m15, levels.asian_high):
        _add_bull("BULLISH_ACCEPTANCE", "≥2 M15 closes past PDH/Asian high")

    # ── BEAR evidence (mirror) ────────────────────────────────────────────
    def _add_bear(key, desc):
        bear_items.append(EvidenceItem(key, _BEAR_WEIGHTS[key], desc))

    if _has_descending_lh_ll(bars_m15):
        _add_bear("LH_LL_STRUCTURE", "M15 lower-highs + lower-lows sequence intact")
    if _bos_down_h1(bars_h1):
        _add_bear("H1_BOS_DOWN", f"H1 close {bars_h1[-1].close:.2f} below recent swing low")
    if _bos_down(bars_m15):
        _add_bear("M15_BOS_DOWN", f"M15 close {bars_m15[-1].close:.2f} below swing low")
    if _sweep_and_reject_bear(bars_m15, levels.pdh):
        _add_bear("BUYSIDE_SWEEP_REJECT", f"swept PDH {levels.pdh} and rejected")
    elif _sweep_and_reject_bear(bars_m15, levels.asian_high):
        _add_bear("BUYSIDE_SWEEP_REJECT", f"swept Asian high {levels.asian_high} and rejected")
    if levels.asian_low and current_price < levels.asian_low:
        _add_bear("ASIAN_LOW_BROKEN", f"price below Asian low {levels.asian_low:.2f}")
    if levels.pdl and current_price < levels.pdl:
        _add_bear("PDL_BROKEN", f"price below PDL {levels.pdl:.2f}")
    if _displacement_down(bars_m15, atr_h1):
        _add_bear("BEARISH_DISPLACEMENT", "3+ of last 6 M15 bars big-body reds")
    if levels.daily_open and current_price < levels.daily_open:
        _add_bear("PRICE_BELOW_DAILY_OPEN", f"below daily open {levels.daily_open:.2f}")
    if vwap and current_price < vwap:
        _add_bear("PRICE_BELOW_SESSION_VWAP", f"below session VWAP {vwap:.2f}")
    if _shallow_pullbacks(bars_m15, atr_h1, "BEAR"):
        _add_bear("SHALLOW_RALLIES", "recent counter-moves < 0.5 × ATR")
    swing_hi, _ = _swing_hi_lo(bars_m15, k=3)
    if swing_hi and current_price < swing_hi and max(b.high for b in bars_m15[-6:]) <= swing_hi:
        _add_bear("PROTECTED_HIGH_INTACT", f"swing high {swing_hi:.2f} not violated")
    if _acceptance_below(bars_m15, levels.pdl) or _acceptance_below(bars_m15, levels.asian_low):
        _add_bear("BEARISH_ACCEPTANCE", "≥2 M15 closes past PDL/Asian low")

    # ── Macro-derived evidence (optional) ─────────────────────────────────
    if macro_context and isinstance(macro_context, dict):
        dxy_dir = str(macro_context.get("dxy_direction", "")).upper()
        y10_dir = str(macro_context.get("yield_10y_direction", "")).upper()
        if dxy_dir == "DOWN":
            _add_bull("DXY_INVERSE_SUPPORTIVE", "DXY falling — supportive for gold")
        elif dxy_dir == "UP":
            _add_bear("DXY_STRONG_SUPPORTIVE", "DXY rising — supportive for gold weakness")
        if y10_dir == "DOWN":
            _add_bull("YIELDS_SUPPORTIVE", "10Y yield falling — supportive for gold")
        elif y10_dir == "UP":
            _add_bear("YIELDS_UP_SUPPORTIVE", "10Y yield rising — supportive for gold weakness")

    # ── Contradictions ─────────────────────────────────────────────────────
    # UNDER_H4_SUPPLY / ABOVE_H4_DEMAND: only a contradiction when price is
    # WITHIN 1×ATR of the level and hasn't yet cleared it. Once we're a full
    # ATR beyond, we've broken through and it's not a contradiction anymore.
    if bars_h4 and atr_h1:
        h4_sw_hi, h4_sw_lo = _swing_hi_lo(bars_h4, k=3)
        if (h4_sw_hi and bull_items
                and (h4_sw_hi - atr_h1) <= current_price < h4_sw_hi):
            contradictions.append(EvidenceItem(
                "UNDER_H4_SUPPLY", _CONTRADICTION_WEIGHTS["UNDER_H4_SUPPLY"],
                f"bull thesis directly under H4 supply {h4_sw_hi:.2f}"
            ))
        if (h4_sw_lo and bear_items
                and h4_sw_lo < current_price <= (h4_sw_lo + atr_h1)):
            contradictions.append(EvidenceItem(
                "ABOVE_H4_DEMAND", _CONTRADICTION_WEIGHTS["ABOVE_H4_DEMAND"],
                f"bear thesis directly above H4 demand {h4_sw_lo:.2f}"
            ))

    # IMMEDIATE_RETURN_TO_RANGE — last M15 bar big body but current close inside range
    if len(bars_m15) >= 3:
        prev = bars_m15[-2]
        prev_body = abs(prev.close - prev.open)
        if atr_h1 and prev_body >= 0.6 * atr_h1:
            # Check if current close returned back into prev bar's range
            if prev.low <= bars_m15[-1].close <= prev.high:
                contradictions.append(EvidenceItem(
                    "IMMEDIATE_RETURN_TO_RANGE",
                    _CONTRADICTION_WEIGHTS["IMMEDIATE_RETURN_TO_RANGE"],
                    "breakout bar's follow-through returned into prior bar's range",
                ))

    # DAILY_ATR_CONSUMED — today's high-low range vs D1 ATR
    if tfs.get("D1"):
        d1_bars = tfs["D1"].candles
        atr_d1 = _atr_from_bars(d1_bars, 14)
        if atr_d1 and len(d1_bars) > 0:
            today = d1_bars[-1]
            if (today.high - today.low) >= 0.75 * atr_d1:
                contradictions.append(EvidenceItem(
                    "DAILY_ATR_CONSUMED", _CONTRADICTION_WEIGHTS["DAILY_ATR_CONSUMED"],
                    f"today's range {(today.high - today.low):.2f} ≥ 75% of D1 ATR {atr_d1:.2f}",
                ))

    # EXCESSIVE_SPREAD
    if snapshot.spread and snapshot.spread > spread_threshold:
        contradictions.append(EvidenceItem(
            "EXCESSIVE_SPREAD", _CONTRADICTION_WEIGHTS["EXCESSIVE_SPREAD"],
            f"spread {snapshot.spread:.2f} > threshold {spread_threshold}",
        ))

    # NEWS_APPROACHING
    if _event_risk_within(upcoming_events or [], minutes=30):
        contradictions.append(EvidenceItem(
            "NEWS_APPROACHING", _CONTRADICTION_WEIGHTS["NEWS_APPROACHING"],
            "high-impact event within 30 minutes",
        ))

    # STRUCTURE_CONFLICT — regime direction vs HTF alignment direction disagree
    if htf_alignment is not None and regime is not None:
        htf_dir = getattr(htf_alignment, "direction", None)
        reg_bias = getattr(regime, "directional_bias", None)
        if (htf_dir == "BULL" and reg_bias == "BEAR") or (htf_dir == "BEAR" and reg_bias == "BULL"):
            contradictions.append(EvidenceItem(
                "STRUCTURE_CONFLICT", _CONTRADICTION_WEIGHTS["STRUCTURE_CONFLICT"],
                f"HTF alignment says {htf_dir} but regime says {reg_bias}",
            ))

    # DXY_OPPOSING
    if macro_context and isinstance(macro_context, dict):
        dxy_dir = str(macro_context.get("dxy_direction", "")).upper()
        # If our dominant evidence is bull but DXY is UP → opposing
        if sum(i.weight for i in bull_items) > sum(i.weight for i in bear_items):
            if dxy_dir == "UP":
                contradictions.append(EvidenceItem(
                    "DXY_OPPOSING", _CONTRADICTION_WEIGHTS["DXY_OPPOSING"],
                    "gold bullish thesis but DXY rising",
                ))
        elif sum(i.weight for i in bear_items) > sum(i.weight for i in bull_items):
            if dxy_dir == "DOWN":
                contradictions.append(EvidenceItem(
                    "DXY_OPPOSING", _CONTRADICTION_WEIGHTS["DXY_OPPOSING"],
                    "gold bearish thesis but DXY falling",
                ))

    # ── Aggregate scores ──────────────────────────────────────────────────
    bull_evidence_score = _clip(sum(i.weight for i in bull_items))
    bear_evidence_score = _clip(sum(i.weight for i in bear_items))
    contradiction_score = _clip(sum(i.weight for i in contradictions))
    data_quality_score  = _clip(snapshot.data_quality_score or 0)

    # Extension risk: distance from H1 EMA21 in ATR multiples, 0-100 curve
    h1_ema21 = _ema([b.close for b in bars_h1], 21)
    if h1_ema21 and atr_h1 and atr_h1 > 0:
        ext_mult = abs(current_price - h1_ema21) / atr_h1
        # 0× → 0, 2× → 40, 4× → 80, 5× → 100
        extension_risk_score = _clip(ext_mult * 20)
    else:
        extension_risk_score = 0

    # Event risk score: minutes to next high-impact
    next_hi = _minutes_to_next_high_impact(upcoming_events or [])
    if next_hi is None:
        event_risk_score = 0
    elif next_hi < 5:
        event_risk_score = 100
    elif next_hi < 15:
        event_risk_score = 80
    elif next_hi < 30:
        event_risk_score = 60
    elif next_hi < 60:
        event_risk_score = 30
    else:
        event_risk_score = 10

    # Dominant direction — favour the higher evidence side; tie = NEUTRAL
    if bull_evidence_score > bear_evidence_score + 5:
        dominant_direction = "BULL"
        base_conf = bull_evidence_score
    elif bear_evidence_score > bull_evidence_score + 5:
        dominant_direction = "BEAR"
        base_conf = bear_evidence_score
    else:
        dominant_direction = "NEUTRAL"
        base_conf = max(bull_evidence_score, bear_evidence_score)

    # Directional confidence: base minus contradiction/extension/event penalties
    directional_confidence = _clip(
        base_conf
        - 0.5 * contradiction_score
        - 0.3 * extension_risk_score
        - 0.4 * event_risk_score
    )

    # Entry-quality confidence: gated by data quality
    entry_quality_confidence = _clip(directional_confidence * (data_quality_score / 100.0))

    return EvidenceAssessment(
        dominant_direction=dominant_direction,
        bull_evidence_score=bull_evidence_score,
        bear_evidence_score=bear_evidence_score,
        contradiction_score=contradiction_score,
        data_quality_score=data_quality_score,
        event_risk_score=event_risk_score,
        extension_risk_score=extension_risk_score,
        directional_confidence=directional_confidence,
        entry_quality_confidence=entry_quality_confidence,
        bull_items=bull_items, bear_items=bear_items,
        contradictions=contradictions,
        warnings=warnings,
    )


__all__ = [
    "compute_directional_evidence", "EvidenceAssessment", "EvidenceItem",
    "_BULL_WEIGHTS", "_BEAR_WEIGHTS", "_CONTRADICTION_WEIGHTS",
]
