"""
Weighted Volume Profile Pivot Points + Flux Directional Bias
=============================================================

A reusable indicator module that computes for any bar window (session,
killzone, arbitrary N-bar range):

  Weighted Volume Profile Pivot Points (VPPP)
  -------------------------------------------
  PP    session VWAP (volume-weighted mean price)
  R1    dVAH (upper 70% value-area boundary)
  R2    R1 + (dVAH - dVAL)      — 1-VA extension above
  S1    dVAL (lower 70% VA boundary)
  S2    S1 - (dVAH - dVAL)      — 1-VA extension below
  POC   Point of Control (highest-volume bin)

  Directional Flux
  ----------------
  For each bar we approximate buying vs selling volume using the
  Elder / Chaikin-style price-position weighting:

    buy_frac  = (close - low)  / (high - low)     [0..1]
    sell_frac = (high  - close) / (high - low)    [0..1]
    buy_vol   = volume × buy_frac
    sell_vol  = volume × sell_frac

  Then we build TWO volume profiles (buying vs selling) and locate:

    POC_buy   price where buying volume was heaviest (accumulation)
    POC_sell  price where selling volume was heaviest (distribution)

  flux_bias = (total_buy_vol - total_sell_vol) / total_vol   [-1..+1]

  Interpretation:
    flux_bias > +0.15  →  bullish accumulation dominates
    flux_bias < -0.15  →  bearish distribution dominates
    otherwise           →  neutral / balanced

  If POC_buy > POC_sell → buyers stacked ABOVE sellers = healthy uptrend
  If POC_buy < POC_sell → sellers stacked ABOVE buyers = distribution top

This module is DATA-SOURCE-AGNOSTIC. Any list of OHLCV bars works.
No side effects, no I/O. Pure functions the operator can drop into
any strategy that needs volume-flow context.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, asdict
from typing import Optional

log = logging.getLogger(__name__)


# ── Data classes ────────────────────────────────────────────────────────────

@dataclass
class VpppLevel:
    price: float
    label: str        # PP | R1 | R2 | S1 | S2 | POC | POC_BUY | POC_SELL
    role:  str        # support | resistance | pivot | accumulation | distribution
    kind:  str        # weighted | value_area | extension | flux


@dataclass
class VpppFluxResult:
    # Pivot levels
    pp:            float          # session VWAP
    r1:            float          # dVAH
    r2:            float          # R1 + VA width
    s1:            float          # dVAL
    s2:            float          # S1 - VA width
    poc:           float          # session Point of Control (all volume)
    # Flux components
    poc_buy:       Optional[float]  # price of heaviest buy volume
    poc_sell:      Optional[float]  # price of heaviest sell volume
    total_buy:     float
    total_sell:    float
    flux_bias:     float           # -1..+1
    flux_label:    str             # "bullish_accumulation" | "bearish_distribution" | "neutral"
    stacking:      str             # "buyers_above_sellers" | "sellers_above_buyers" | "aligned"
    # Meta
    bin_size:      float
    bar_count:     int
    total_volume:  float
    session_high:  float
    session_low:   float

    def to_dict(self) -> dict:
        return asdict(self)

    def as_levels(self) -> list[VpppLevel]:
        """Flat list of all levels for downstream consumers (dashboards, alerts)."""
        levels = [
            VpppLevel(price=self.pp,  label="PP",  role="pivot",       kind="weighted"),
            VpppLevel(price=self.r1,  label="R1",  role="resistance", kind="value_area"),
            VpppLevel(price=self.r2,  label="R2",  role="resistance", kind="extension"),
            VpppLevel(price=self.s1,  label="S1",  role="support",    kind="value_area"),
            VpppLevel(price=self.s2,  label="S2",  role="support",    kind="extension"),
            VpppLevel(price=self.poc, label="POC", role="pivot",       kind="value_area"),
        ]
        if self.poc_buy is not None:
            levels.append(VpppLevel(price=self.poc_buy,  label="POC_BUY",
                                    role="accumulation", kind="flux"))
        if self.poc_sell is not None:
            levels.append(VpppLevel(price=self.poc_sell, label="POC_SELL",
                                    role="distribution", kind="flux"))
        return levels


# ── Helpers ─────────────────────────────────────────────────────────────────

def _bin_size(high: float, low: float) -> float:
    rng = high - low
    return max(0.5, min(5.0, rng / 30.0))


def _histogram(bars: list, bin_size: float,
               volume_fn=None) -> dict[float, float]:
    """Distribute each bar's volume across price bins uniformly.

    volume_fn(bar) → volume-to-attribute. Default: bar.volume.
    Use a custom fn to build directional (buy/sell) histograms.
    """
    if not bars or bin_size <= 0:
        return {}
    hist: dict[float, float] = {}
    for b in bars:
        lo, hi = min(b.low, b.high), max(b.low, b.high)
        n_bins = max(1, int(round((hi - lo) / bin_size)))
        raw_vol = volume_fn(b) if volume_fn else (b.volume or 0)
        v_per = raw_vol / n_bins
        if v_per <= 0:
            continue
        start = round(lo / bin_size) * bin_size
        for i in range(n_bins):
            k = round(start + i * bin_size, 2)
            hist[k] = hist.get(k, 0.0) + v_per
    return hist


def _value_area(hist: dict[float, float], pct: float = 0.70
                ) -> tuple[float, float, float]:
    if not hist:
        return (0.0, 0.0, 0.0)
    total = sum(hist.values())
    if total <= 0:
        return (0.0, 0.0, 0.0)
    poc = max(hist.keys(), key=lambda k: hist[k])
    keys = sorted(hist.keys())
    idx = keys.index(poc)
    lo_i = hi_i = idx
    acc = hist[keys[idx]]
    target = total * pct
    while acc < target and (lo_i > 0 or hi_i < len(keys) - 1):
        below = hist[keys[lo_i - 1]] if lo_i > 0 else -1.0
        above = hist[keys[hi_i + 1]] if hi_i < len(keys) - 1 else -1.0
        if below < 0 and above < 0:
            break
        if below >= above:
            lo_i -= 1; acc += hist[keys[lo_i]]
        else:
            hi_i += 1; acc += hist[keys[hi_i]]
    return (keys[lo_i], keys[hi_i], poc)


def _vwap(bars: list) -> Optional[float]:
    if not bars:
        return None
    num, den = 0.0, 0.0
    for b in bars:
        tp = (b.high + b.low + b.close) / 3.0
        v = b.volume or 0
        num += tp * v
        den += v
    return round(num / den, 2) if den > 0 else None


# ── Directional volume split ────────────────────────────────────────────────

def _buy_vol(b) -> float:
    """Elder-style buying volume approximation."""
    rng = b.high - b.low
    if rng <= 0:
        return (b.volume or 0) / 2.0
    frac = (b.close - b.low) / rng
    return (b.volume or 0) * max(0.0, min(1.0, frac))


def _sell_vol(b) -> float:
    rng = b.high - b.low
    if rng <= 0:
        return (b.volume or 0) / 2.0
    frac = (b.high - b.close) / rng
    return (b.volume or 0) * max(0.0, min(1.0, frac))


def _flux_bias_and_label(total_buy: float, total_sell: float
                          ) -> tuple[float, str]:
    total = total_buy + total_sell
    if total <= 0:
        return (0.0, "neutral")
    bias = (total_buy - total_sell) / total
    bias = round(bias, 3)
    if bias >= 0.15:
        return (bias, "bullish_accumulation")
    if bias <= -0.15:
        return (bias, "bearish_distribution")
    return (bias, "neutral")


def _stacking_label(poc_buy: Optional[float], poc_sell: Optional[float]
                     ) -> str:
    if poc_buy is None or poc_sell is None:
        return "aligned"
    if abs(poc_buy - poc_sell) < 1.0:
        return "aligned"
    return "buyers_above_sellers" if poc_buy > poc_sell else "sellers_above_buyers"


# ── Public entry point ──────────────────────────────────────────────────────

def compute_vppp_flux(bars: list, value_area_pct: float = 0.70
                      ) -> Optional[VpppFluxResult]:
    """
    Compute Weighted Volume Profile Pivot Points + Flux for the given bars.

    Args:
        bars: list of OHLCV objects with .high, .low, .close, .open, .volume attrs
        value_area_pct: default 0.70 (standard TPO)

    Returns:
        VpppFluxResult or None if inputs are degenerate.
    """
    if not bars:
        return None
    hi = max(b.high for b in bars)
    lo = min(b.low  for b in bars)
    if hi <= lo:
        return None

    bin_size = _bin_size(hi, lo)

    # Full-volume histogram → POC + VA
    hist_all = _histogram(bars, bin_size)
    if not hist_all:
        return None
    val, vah, poc = _value_area(hist_all, value_area_pct)
    va_width = max(0.01, vah - val)

    # Weighted PP = VWAP
    pp = _vwap(bars) or round((hi + lo) / 2.0, 2)

    # Extensions
    r2 = round(vah + va_width, 2)
    s2 = round(val - va_width, 2)

    # Directional histograms
    hist_buy  = _histogram(bars, bin_size, volume_fn=_buy_vol)
    hist_sell = _histogram(bars, bin_size, volume_fn=_sell_vol)
    poc_buy  = max(hist_buy.keys(),  key=lambda k: hist_buy[k])  if hist_buy  else None
    poc_sell = max(hist_sell.keys(), key=lambda k: hist_sell[k]) if hist_sell else None

    total_buy  = sum(hist_buy.values())  if hist_buy  else 0.0
    total_sell = sum(hist_sell.values()) if hist_sell else 0.0
    flux_bias, flux_label = _flux_bias_and_label(total_buy, total_sell)
    stacking = _stacking_label(poc_buy, poc_sell)

    return VpppFluxResult(
        pp=pp, r1=round(vah, 2), r2=r2, s1=round(val, 2), s2=s2,
        poc=round(poc, 2),
        poc_buy=round(poc_buy, 2)  if poc_buy  is not None else None,
        poc_sell=round(poc_sell, 2) if poc_sell is not None else None,
        total_buy=round(total_buy, 2),
        total_sell=round(total_sell, 2),
        flux_bias=flux_bias, flux_label=flux_label,
        stacking=stacking,
        bin_size=round(bin_size, 2),
        bar_count=len(bars),
        total_volume=round(sum(hist_all.values()), 2),
        session_high=round(hi, 2),
        session_low=round(lo, 2),
    )


# ── Confluence helper for other strategies ─────────────────────────────────

def price_bias_from_vppp(current_price: float, vppp: VpppFluxResult,
                         tolerance_pts: float = 1.0) -> str:
    """
    Given current price and a VPPP result, return a one-word bias:
      "at_poc"        — within tolerance of POC (magnet zone)
      "above_r1"      — trading above VAH (extended long, watch for pullback)
      "above_pp"      — inside upper VA
      "below_s1"      — trading below VAL (extended short)
      "below_pp"      — inside lower VA
      "at_extreme_r"  — at or beyond R2
      "at_extreme_s"  — at or beyond S2

    Downstream strategies use this to decide whether the current price
    supports fade or continuation logic.
    """
    if abs(current_price - vppp.poc) <= tolerance_pts:
        return "at_poc"
    if current_price >= vppp.r2:
        return "at_extreme_r"
    if current_price >= vppp.r1:
        return "above_r1"
    if current_price >= vppp.pp:
        return "above_pp"
    if current_price <= vppp.s2:
        return "at_extreme_s"
    if current_price <= vppp.s1:
        return "below_s1"
    return "below_pp"


def flux_supports_direction(flux_bias: float, direction: str,
                             threshold: float = 0.10) -> bool:
    """
    True if the flux bias supports (or doesn't oppose) the given trade direction.

    For BUY: flux must be > -threshold (i.e. not strongly bearish)
    For SELL: flux must be < +threshold (i.e. not strongly bullish)

    This is a permissive check — flux only VETOES trades that fight
    strong directional bias. Neutral flux passes both directions.
    """
    if direction == "BUY":
        return flux_bias > -threshold
    if direction == "SELL":
        return flux_bias < threshold
    return True
