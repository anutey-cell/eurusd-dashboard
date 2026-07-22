"""
Killzone Magnet Strategy
========================

Trades price toward the PRIOR killzone's dPOC as a magnet, filtered by
Flux directional bias (from volume_pivot_flux).

Backtest evidence (see tools/backtest_killzone_liquidity.py) shows
these touch rates for prior-KZ POC magnets:

  london_kz  → overlap    85.5%     ← strongest
  london_pre → london_kz  80.0%
  overlap    → ny_kz      76.4%
  ny_kz      → ny_pm      64.8%
  london_kz  → ny_kz      60.0%

The strategy fires at the START of the target killzone if:

  1. Prior KZ has a well-formed profile (>=8 bars, VA width < threshold)
  2. Current price is >= min_distance_atr × ATR from the prior KZ POC
  3. Flux bias from the prior KZ does not STRONGLY oppose the magnet play
     (a strongly bullish flux says "don't fight the buying — no SELL back")
  4. News clear, weekend gate respected

Trade plan:
  Entry: current price
  SL:    beyond the prior KZ's session extreme on the side away from POC
         + 0.15 × ATR buffer
  TP1:   prior KZ POC (the magnet)
  TP2:   VWAP (secondary magnet, computed from prior KZ)

Independent module. Does NOT touch mandate, momentum, VP Trap, or any
other engine. Config-toggled off by default until validation.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from typing import Optional

log = logging.getLogger(__name__)


# ── Chain of KZ transitions we trade (from backtest edge-ranking) ──────────
# (prior_kz, target_kz, prior_hour_lo, prior_hour_hi, target_hour_lo, target_hour_hi,
#  expected_touch_rate)
MAGNET_CHAINS = [
    ("london_kz",  "overlap",     7, 10, 10, 13, 85.5),
    ("london_pre", "london_kz",   6,  7,  7, 10, 80.0),
    ("overlap",    "ny_kz",      10, 13, 13, 16, 76.4),
    ("ny_kz",      "ny_pm",      13, 16, 16, 22, 64.8),
    ("london_kz",  "ny_kz",       7, 10, 13, 16, 60.0),
]

# Only trigger in the FIRST N minutes of the target KZ — otherwise the
# opportunity has passed as price moves further
TARGET_KZ_TRIGGER_WINDOW_MIN = 45


# ── Data classes ────────────────────────────────────────────────────────────

@dataclass
class MagnetSetup:
    prior_kz:       str
    target_kz:      str
    prior_poc:      float
    prior_vwap:     Optional[float]
    prior_session_high: float
    prior_session_low:  float
    current_price:  float
    distance_pts:   float
    distance_atr:   float
    direction:      str            # BUY | SELL
    entry:          float
    sl:             float
    tp1:            float          # POC
    tp2:            Optional[float] # VWAP if available
    risk_pts:       float
    rr_tp1:         float
    rr_tp2:         float
    flux_bias:      float
    flux_label:     str
    flux_supports:  bool
    expected_touch: float          # from backtest
    trigger_time:   datetime
    reason:         str

    def to_dict(self) -> dict:
        d = asdict(self)
        d["trigger_time"] = self.trigger_time.isoformat() if self.trigger_time else None
        return d


# ── KZ bar filtering ────────────────────────────────────────────────────────

def _bars_in_utc_window(bars: list, day_utc,
                        hour_lo: int, hour_hi: int) -> list:
    """M15 bars within [hour_lo, hour_hi) UTC on day_utc."""
    if not bars:
        return []
    start = datetime(day_utc.year, day_utc.month, day_utc.day,
                     hour_lo, tzinfo=timezone.utc)
    end   = datetime(day_utc.year, day_utc.month, day_utc.day,
                     hour_hi, tzinfo=timezone.utc)
    out = []
    for c in bars:
        t = c.time if c.time.tzinfo else c.time.replace(tzinfo=timezone.utc)
        t = t.astimezone(timezone.utc)
        if start <= t < end:
            out.append(c)
    return out


def _current_kz_bars(candles_m15: list, now: datetime,
                     hour_lo: int, hour_hi: int) -> list:
    """Bars in the current KZ up to `now`."""
    today = now.date()
    return [c for c in _bars_in_utc_window(candles_m15, today, hour_lo, hour_hi)
            if (c.time if c.time.tzinfo else c.time.replace(tzinfo=timezone.utc)) <= now]


# ── Main detector ───────────────────────────────────────────────────────────

def detect_kz_magnet(
    candles_m15:      list,
    now:              Optional[datetime] = None,
    atr_h1:           float = 20.0,
    min_distance_atr: float = 0.6,        # ≥ 0.6×ATR from POC to trigger
    max_va_width_atr: float = 2.0,        # skip if prior KZ VA too wide (unreliable POC)
    news_clear:       bool  = True,
) -> Optional[MagnetSetup]:
    """
    Scan for an active magnet setup RIGHT NOW.

    Called on every strategist tick. Returns a MagnetSetup only when all
    filters pass; otherwise None. Deterministic + pure — same inputs give
    same output.
    """
    from services.volume_pivot_flux import (
        compute_vppp_flux, flux_supports_direction,
    )

    if not candles_m15 or not news_clear:
        return None
    if now is None:
        now = datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)

    current_hour = now.hour
    today = now.date()

    # Find the chain where we're INSIDE the target KZ's first trigger window
    for prior_name, target_name, p_lo, p_hi, t_lo, t_hi, expected_touch in MAGNET_CHAINS:
        if not (t_lo <= current_hour < t_hi):
            continue
        # Compute minutes-into-target-KZ
        target_start = datetime(today.year, today.month, today.day,
                                 t_lo, tzinfo=timezone.utc)
        minutes_in = (now - target_start).total_seconds() / 60.0
        if minutes_in > TARGET_KZ_TRIGGER_WINDOW_MIN:
            continue

        # Get prior-KZ bars for the SAME day (or previous if target opens at 0)
        prior_bars = _bars_in_utc_window(candles_m15, today, p_lo, p_hi)
        if len(prior_bars) < 8:
            continue

        # Compute prior-KZ profile + flux
        vppp = compute_vppp_flux(prior_bars)
        if vppp is None:
            continue

        va_width = vppp.r1 - vppp.s1
        if va_width > max_va_width_atr * atr_h1:
            # Prior VA too wide — POC is unreliable as magnet
            continue

        # Current price = last available M15 close (should be inside target KZ)
        current_bars = _current_kz_bars(candles_m15, now, t_lo, t_hi)
        if not current_bars:
            continue
        current_price = current_bars[-1].close

        distance_pts = current_price - vppp.poc
        distance_atr = abs(distance_pts) / max(1.0, atr_h1)
        if distance_atr < min_distance_atr:
            # Too close to POC — magnet already spent
            continue

        # Direction: mean-revert toward POC
        direction = "SELL" if distance_pts > 0 else "BUY"
        entry = current_price
        buffer = atr_h1 * 0.15

        if direction == "SELL":
            sl = round(vppp.session_high + buffer, 2)
        else:
            sl = round(vppp.session_low - buffer, 2)

        risk = abs(entry - sl)
        if risk <= 0:
            continue

        # TP1 = POC magnet; TP2 = VWAP if it exists and is further along the path
        tp1 = round(vppp.poc, 2)
        tp2 = None
        if vppp.pp:
            # Only use VWAP as TP2 if it's on the same side as POC relative to entry
            if direction == "SELL" and vppp.pp < entry and vppp.pp <= tp1:
                tp2 = round(vppp.pp, 2)
            elif direction == "BUY" and vppp.pp > entry and vppp.pp >= tp1:
                tp2 = round(vppp.pp, 2)

        rr_tp1 = round(abs(tp1 - entry) / risk, 2)
        rr_tp2 = round(abs(tp2 - entry) / risk, 2) if tp2 else 0.0

        # Flux check — permissive (doesn't require flux to actively support,
        # only requires flux to not STRONGLY oppose)
        flux_ok = flux_supports_direction(vppp.flux_bias, direction, threshold=0.20)

        if not flux_ok:
            # Flux strongly opposes the magnet play — skip
            continue

        # Minimum RR gate
        if rr_tp1 < 1.0:
            continue

        return MagnetSetup(
            prior_kz=prior_name,
            target_kz=target_name,
            prior_poc=vppp.poc,
            prior_vwap=vppp.pp,
            prior_session_high=vppp.session_high,
            prior_session_low=vppp.session_low,
            current_price=round(current_price, 2),
            distance_pts=round(distance_pts, 2),
            distance_atr=round(distance_atr, 2),
            direction=direction,
            entry=round(entry, 2),
            sl=sl,
            tp1=tp1,
            tp2=tp2,
            risk_pts=round(risk, 2),
            rr_tp1=rr_tp1,
            rr_tp2=rr_tp2,
            flux_bias=vppp.flux_bias,
            flux_label=vppp.flux_label,
            flux_supports=flux_ok,
            expected_touch=expected_touch,
            trigger_time=now,
            reason=(
                f"{prior_name} POC ${vppp.poc:.2f} · price ${current_price:.2f} "
                f"({distance_atr:.1f}× ATR away) · "
                f"flux {vppp.flux_label} ({vppp.flux_bias:+.2f}) · "
                f"expected magnet {expected_touch:.0f}%"
            ),
        )

    return None


# ── Convenience wrapper for live use ────────────────────────────────────────

def scan_for_magnet(atr_h1: float = 20.0,
                    news_clear: bool = True) -> Optional[MagnetSetup]:
    """Fetch fresh M15 candles and run the detector. For runner use."""
    try:
        from data.candles import get_candles
        resp = get_candles(interval="M15", limit=200, pair="xauusd")
        candles = resp.candles if resp and resp.candles else []
        if not candles:
            return None
        return detect_kz_magnet(candles, atr_h1=atr_h1, news_clear=news_clear)
    except Exception as exc:
        log.warning("[kz_magnet] scan failed: %s", exc)
        return None
