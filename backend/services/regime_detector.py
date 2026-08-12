"""
Regime Detector — direction × volatility × session classification.

Feeds the Predator engine's regime gate. Cell-by-cell decisions come from
the empirical audit (backend/scripts/predator_audit.py Phase 3):

  PREDATOR FAVORABLE   (ENABLE):
    strong_bear × extreme  |  strong_bear × expanded  |  strong_bear × normal
    weak_bear   × extreme  |  weak_bear   × expanded
    range       × normal   (small-sample PDL_BREAK edge)
    range       × expanded (small-sample edge)

  PREDATOR HOSTILE     (DISABLE):
    range       × extreme  (PDL_BREAK loses -15.6pt here)
    weak_bear   × compressed
    weak_bear   × normal
    strong_bull × anything  (no BUY edge exists; SELL setups fade)
    weak_bull   × anything

All lookups use bars already in `historical_candles` — no external data.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import text
from sqlalchemy.orm import Session


# ─────────────────────────────────────────────────────────────────────────────
# Regime cell enable/disable table (from Phase 3 audit)
# ─────────────────────────────────────────────────────────────────────────────

# Format: (direction_regime, vol_regime) -> confidence multiplier
#   1.0 = full confidence, 0.5 = half, 0.0 = disable
_REGIME_CONFIDENCE: dict[tuple[str, str], float] = {
    # Strong bear — the empirical sweet spot
    ("strong_bear", "extreme"):    1.0,
    ("strong_bear", "expanded"):   1.0,
    ("strong_bear", "normal"):     0.8,
    ("strong_bear", "compressed"): 0.5,

    # Weak bear — mixed
    ("weak_bear", "extreme"):      0.8,
    ("weak_bear", "expanded"):     0.8,
    ("weak_bear", "normal"):       0.0,  # empirically loses money
    ("weak_bear", "compressed"):   0.0,  # empirically loses money

    # Range — mostly hostile, one cell profitable
    ("range", "expanded"):         0.6,
    ("range", "normal"):           0.6,  # PDL_BREAK only, high WR
    ("range", "extreme"):          0.0,  # PDL_BREAK loses -15.6pt
    ("range", "compressed"):       0.0,

    # Bull — Predator has ZERO BUY edge, all archetypes fail
    ("weak_bull", "extreme"):      0.0,
    ("weak_bull", "expanded"):     0.0,
    ("weak_bull", "normal"):       0.0,
    ("weak_bull", "compressed"):   0.0,
    ("strong_bull", "extreme"):    0.0,
    ("strong_bull", "expanded"):   0.0,
    ("strong_bull", "normal"):     0.0,
    ("strong_bull", "compressed"): 0.0,
}
_DEFAULT_UNKNOWN_MULT = 0.0    # fail safe: unknown regime = don't fire


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _parse_ts(t):
    if isinstance(t, str):
        for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S"):
            try: return datetime.strptime(t.split("+")[0], fmt)
            except ValueError: continue
    return t


def _load_m15_closes(db: Session, n: int = 300) -> list[tuple[datetime, float, float, float]]:
    """Return most-recent n M15 bars as (time, high, low, close) oldest-first."""
    rows = db.execute(text(
        "SELECT candle_time, high, low, close FROM historical_candles "
        "WHERE instrument='XAU/USD' AND timeframe='M15' "
        "ORDER BY candle_time DESC LIMIT :n"
    ), {"n": n}).fetchall()
    out = []
    for r in rows:
        t = _parse_ts(r[0])
        if hasattr(t, "tzinfo") and t.tzinfo is not None:
            t = t.replace(tzinfo=None)
        out.append((t, float(r[1]), float(r[2]), float(r[3])))
    return list(reversed(out))


def _ema(closes: list[float], n: int) -> Optional[float]:
    if len(closes) < n: return None
    k = 2 / (n + 1)
    ema = closes[-n]
    for c in closes[-n+1:]:
        ema = c * k + ema * (1 - k)
    return ema


def _atr_series(bars: list[tuple], n: int = 14, window: int = 200) -> list[float]:
    """Compute rolling ATR(n) over `window` bars, oldest first."""
    if len(bars) < n + window: return []
    out = []
    for i in range(len(bars) - window, len(bars)):
        if i < n: continue
        atrs = sum(bars[k][1] - bars[k][2] for k in range(i-n, i)) / n
        out.append(atrs)
    return out


# ─────────────────────────────────────────────────────────────────────────────
# Classifiers
# ─────────────────────────────────────────────────────────────────────────────

def classify_direction_regime(closes: list[float],
                                 ema_short: int = 20, ema_long: int = 50,
                                 lookback: int = 20) -> str:
    """5-way: strong_bull / weak_bull / range / weak_bear / strong_bear."""
    if len(closes) < ema_long + lookback:
        return "unknown"
    ema20 = _ema(closes, ema_short)
    ema50 = _ema(closes, ema_long)
    ema20_prev = _ema(closes[:-lookback], ema_short)
    if None in (ema20, ema50, ema20_prev):
        return "unknown"
    close = closes[-1]
    slope_pct = (ema20 - ema20_prev) / max(abs(ema20_prev), 0.01) * 100
    above_20 = close > ema20
    above_50 = ema20 > ema50

    if slope_pct > 0.3 and above_20 and above_50:            return "strong_bull"
    if slope_pct > 0.05 and above_20:                        return "weak_bull"
    if slope_pct < -0.3 and not above_20 and not above_50:   return "strong_bear"
    if slope_pct < -0.05 and not above_20:                   return "weak_bear"
    return "range"


def classify_vol_regime(bars: list[tuple], n: int = 14, window: int = 200) -> str:
    """4-way: compressed / normal / expanded / extreme (via ATR percentile)."""
    if len(bars) < n + window:
        return "unknown"
    atr_series = _atr_series(bars, n, window)
    if not atr_series:
        return "unknown"
    current_atr = atr_series[-1]
    ranked = sorted(atr_series)
    below = sum(1 for a in ranked if a < current_atr)
    pct = 100 * below / len(ranked)
    if pct < 20: return "compressed"
    if pct < 60: return "normal"
    if pct < 85: return "expanded"
    return "extreme"


def _session_label(hour: int) -> str:
    if 22 <= hour or hour < 6:     return "ASIA"
    if 6 <= hour < 7:              return "PRE_LDN"
    if 7 <= hour < 10:             return "LDN_OPEN"
    if 10 <= hour < 12:            return "LDN_CONT"
    if 12 <= hour < 13:            return "LDN_LUNCH"
    if 13 <= hour < 16:            return "NY_OPEN"
    if 16 <= hour < 17:            return "LDN_NY_CLOSE"
    return "NY_LATE"


# ─────────────────────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────────────────────

def classify_current_regime(db: Session) -> dict:
    """Return {direction, volatility, session, timestamp} for current market."""
    bars = _load_m15_closes(db, n=300)
    if not bars:
        return {"direction": "unknown", "volatility": "unknown",
                "session": "unknown", "timestamp": None}
    closes = [b[3] for b in bars]
    direction = classify_direction_regime(closes)
    volatility = classify_vol_regime(bars)
    session = _session_label(bars[-1][0].hour)
    return {
        "direction":  direction,
        "volatility": volatility,
        "session":    session,
        "timestamp":  bars[-1][0].isoformat(),
    }


def regime_confidence_multiplier(direction: str, volatility: str) -> float:
    """
    Look up the empirical enable/disable multiplier for a regime cell.
    0.0 = don't fire; 1.0 = full confidence; 0.5-0.8 = degraded.
    """
    return _REGIME_CONFIDENCE.get((direction, volatility), _DEFAULT_UNKNOWN_MULT)


def is_predator_favorable_regime(regime: dict, min_multiplier: float = 0.5) -> tuple[bool, str]:
    """
    Returns (allowed, reason). Predator fires only when multiplier >= threshold.
    """
    d = regime.get("direction", "unknown")
    v = regime.get("volatility", "unknown")
    mult = regime_confidence_multiplier(d, v)
    if mult >= min_multiplier:
        return True, f"regime ({d} × {v}) mult={mult}"
    return False, f"regime ({d} × {v}) mult={mult} < {min_multiplier} — hostile to SELL predator"


__all__ = [
    "classify_current_regime", "classify_direction_regime",
    "classify_vol_regime", "regime_confidence_multiplier",
    "is_predator_favorable_regime",
]
