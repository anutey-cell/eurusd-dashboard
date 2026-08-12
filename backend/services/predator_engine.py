"""
Predator Engine v2 — outcome-first XAU/USD signals with M5-close detection.
==========================================================================

Empirically-driven changes 2026-08-12 (backend/scripts/predator_latency_audit.py):

  1. M5-CLOSE DETECTION (was M15-close). Restores ~87-99% of theoretical
     expectancy that M15-confirmation destroys. Evidence:
       ASIAN_BREAKDOWN  expct +2.38 → +12.98 pt/trade  (M15 → M5)
       PDL_BREAK        expct +20.59 → +26.35 pt/trade

  2. EXTENSION FILTER. Reject when too much of the move is already gone:
       ASIAN: pct_consumed > 30%   (OPTIMAL bucket is -4.77 expct — DELETE)
       PDL:   pct_consumed > 60%   (LATE bucket is -28.35 expct)

  3. ASIAN TPs 20/40 (was 30/50). Median remaining move is 27pt so 30pt TP1
     only hit 47.7%. 20pt hits 60.8%. PDL TPs unchanged (40/60 still valid).

  4. STATE MACHINE. Signal carries state = OBSERVE | ARMED | FIRE. ARMED
     emitted when price approaches level (pre-signal awareness). FIRE emitted
     only when M5 close breaches level + confluence + extension filter passes.

Archetypes (SELL only — no BUY edge validated on 5-month audit):

  ASIAN_BREAKDOWN  — SELL when M5 closes below Asian-session low
                     Prey: overnight buyers, range-bound stops below asian_low
  PDL_BREAK        — SELL when M5 closes below prev-day low (with acceptance)
                     Prey: yesterday's dip-buyers, retail "PDL = support" holders
  VOL_CONTINUATION — Direction-follow when volume >=1.3x mean + confluent primary
                     Prey: fade traders, mean-reversion algos

Safety:
  - Never places orders. Signals only, recorded to shadow_trades.
  - Regime-gated (services.regime_detector) — bullish regimes = no signals.
  - Telegram gated by predator_telegram_enabled (default False).
  - Fingerprint dedupe per M5 bar per level bucket.
"""
from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import text
from sqlalchemy.orm import Session

log = logging.getLogger(__name__)


# Empirical median remaining move from audit — used by extension filter to
# normalise pct_consumed calculation.
_EXPECTED_TOTAL_MOVE_PTS: dict[str, float] = {
    "ASIAN_BREAKDOWN": 40.0,   # median remaining 27.1pt + median lost 10.7pt ≈ 38pt total, rounded 40
    "PDL_BREAK":        75.0,   # median remaining 54.9pt + median lost 16.8pt ≈ 72pt total, rounded 75
}

# Per-archetype extension filter thresholds (fraction of expected total move)
_EXTENSION_LIMIT: dict[str, float] = {
    "ASIAN_BREAKDOWN": 0.30,   # Phase 7 audit: OPTIMAL (30-60%) is -4.77 expct
    "PDL_BREAK":        0.60,   # Phase 7 audit: LATE (60-100%) is -28.35 expct
}


# ─────────────────────────────────────────────────────────────────────────────
# Signal dataclass
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class PredatorSignal:
    archetype:      str
    direction:      str
    state:          str          # OBSERVE | ARMED | FIRE
    entry:          float
    stop_loss:      float
    tp1:            float
    tp2:            float
    rr:             float
    thesis:         str
    trigger:        str
    confidence:     str          # HIGH | MED | LOW
    counterparty:   str
    session:        str
    bar_time:       str          # ISO of the M5 bar that fired
    fingerprint:    str
    pct_consumed:   Optional[float] = None    # 0.0–1.0
    latency_pts:    Optional[float] = None
    m5_used:        bool = True

    def to_dict(self) -> dict:
        return asdict(self)


# ─────────────────────────────────────────────────────────────────────────────
# Data helpers
# ─────────────────────────────────────────────────────────────────────────────

def _parse_ts(t):
    if isinstance(t, str):
        for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S"):
            try: return datetime.strptime(t.split("+")[0], fmt)
            except ValueError: continue
    return t


def _load_recent(db: Session, tf: str, n: int) -> list[tuple]:
    rows = db.execute(text(
        "SELECT candle_time, open, high, low, close, volume "
        "FROM historical_candles WHERE instrument='XAU/USD' AND timeframe=:tf "
        "ORDER BY candle_time DESC LIMIT :n"
    ), {"tf": tf, "n": n}).fetchall()
    out = []
    for r in rows:
        t = _parse_ts(r[0])
        if hasattr(t, "tzinfo") and t.tzinfo is not None:
            t = t.replace(tzinfo=None)
        out.append((t, float(r[1]), float(r[2]), float(r[3]),
                     float(r[4]), float(r[5] or 0)))
    return list(reversed(out))


def _last_h1_rsi(db: Session, n: int = 14) -> Optional[float]:
    rows = db.execute(text(
        "SELECT close FROM historical_candles "
        "WHERE instrument='XAU/USD' AND timeframe='H1' "
        "ORDER BY candle_time DESC LIMIT :n"
    ), {"n": n + 1}).fetchall()
    if len(rows) < n + 1:
        return None
    closes = [float(r[0]) for r in reversed(rows)]
    gains, losses = [], []
    for i in range(1, len(closes)):
        d = closes[i] - closes[i-1]
        (gains if d > 0 else losses).append(abs(d))
    if not gains and not losses: return 50.0
    avg_g = sum(gains) / n if gains else 0
    avg_l = sum(losses) / n if losses else 0
    if avg_l == 0: return 100.0
    return round(100 - 100 / (1 + avg_g / avg_l), 1)


def _prev_day_hl(m5_bars: list[tuple]) -> tuple[Optional[float], Optional[float]]:
    if not m5_bars: return None, None
    last_t = m5_bars[-1][0]
    today_start = last_t.replace(hour=0, minute=0, second=0, microsecond=0)
    prev_start = today_start - timedelta(days=1)
    highs, lows = [], []
    for t, o, h, l, c, v in m5_bars:
        if prev_start <= t < today_start:
            highs.append(h); lows.append(l)
    if not highs: return None, None
    return max(highs), min(lows)


def _asian_range(m5_bars: list[tuple]) -> tuple[Optional[float], Optional[float]]:
    if not m5_bars: return None, None
    last_t = m5_bars[-1][0]
    if 22 <= last_t.hour or last_t.hour < 6:
        session_start = last_t.replace(hour=22, minute=0, second=0, microsecond=0)
        if last_t.hour < 6: session_start -= timedelta(days=1)
    else:
        today_6 = last_t.replace(hour=6, minute=0, second=0, microsecond=0)
        session_start = today_6 - timedelta(hours=8)
    session_end = session_start + timedelta(hours=8)
    highs, lows = [], []
    for t, o, h, l, c, v in m5_bars:
        if session_start <= t < session_end:
            highs.append(h); lows.append(l)
    if not highs: return None, None
    return max(highs), min(lows)


def _vol_ratio_m5(m5_bars: list[tuple], window: int = 50) -> Optional[float]:
    if len(m5_bars) < window + 1: return None
    avg = sum(b[5] for b in m5_bars[-window-1:-1]) / window
    if avg <= 0: return None
    return round(m5_bars[-1][5] / avg, 2)


def _session_label(hour: int) -> str:
    if 22 <= hour or hour < 6:     return "ASIA"
    if 6 <= hour < 7:              return "PRE_LDN"
    if 7 <= hour < 10:             return "LDN_OPEN"
    if 10 <= hour < 12:            return "LDN_CONT"
    if 12 <= hour < 13:            return "LDN_LUNCH"
    if 13 <= hour < 16:            return "NY_OPEN"
    if 16 <= hour < 17:            return "LDN_NY_CLOSE"
    return "NY_LATE"


def _fingerprint(archetype: str, direction: str, entry: float,
                    bar_time: datetime) -> str:
    """Include the M5 bar-time to the minute so we don't dedupe across bars."""
    bucket = round(entry / 5.0) * 5
    key = f"{archetype}|{direction}|{bucket}|{bar_time.strftime('%Y%m%d%H%M')}"
    return hashlib.sha256(key.encode()).hexdigest()[:16]


# ─────────────────────────────────────────────────────────────────────────────
# First-M5-close-below-level scanner — the CORE latency fix
# ─────────────────────────────────────────────────────────────────────────────

def _first_m5_close_below(m5_bars: list[tuple], level: float,
                             lookback_bars: int = 48) -> Optional[dict]:
    """
    Walk the last `lookback_bars` M5 bars (≈4h). Return the FIRST bar whose
    CLOSE is below `level`. This is the executable entry timestamp — much
    earlier than waiting for M15 close confirmation.
    """
    slice_bars = m5_bars[-lookback_bars:] if len(m5_bars) > lookback_bars else m5_bars
    for i, (t, o, h, l, c, v) in enumerate(slice_bars):
        if c < level:
            return {
                "time": t, "close": c, "high": h, "low": l,
                "idx_in_slice": i,
            }
    return None


def _apply_extension_filter(archetype: str, level: float,
                                first_break_price: float,
                                current_close: float) -> tuple[bool, float]:
    """
    Returns (passes, pct_consumed). pct_consumed = (level - current_close) /
    expected_total_move. Rejects when pct exceeds the archetype's threshold.
    """
    expected = _EXPECTED_TOTAL_MOVE_PTS.get(archetype, 50.0)
    limit    = _EXTENSION_LIMIT.get(archetype, 0.50)
    consumed_pts = abs(level - current_close)
    pct = consumed_pts / max(expected, 0.1)
    return (pct <= limit, min(pct, 2.0))


# ─────────────────────────────────────────────────────────────────────────────
# Detectors (M5-close based)
# ─────────────────────────────────────────────────────────────────────────────

def detect_asian_breakdown(m5_bars: list[tuple],
                              rsi_h1: Optional[float] = None,
                              vol_r: Optional[float] = None) -> Optional[PredatorSignal]:
    """
    #1 edge — Asian Range Breakdown SELL.
    Fires on the FIRST M5 close below asian_low - 2pt with confluence AND
    extension filter passes.
    """
    if len(m5_bars) < 20: return None
    a_high, a_low = _asian_range(m5_bars)
    if a_low is None: return None
    if not ((vol_r is not None and vol_r >= 1.3)
            or (rsi_h1 is not None and rsi_h1 < 45)):
        return None

    hit = _first_m5_close_below(m5_bars, a_low - 2.0)
    if hit is None: return None

    t = hit["time"]; c = hit["close"]

    # Extension filter — only fire if not already over-consumed
    passes, pct = _apply_extension_filter("ASIAN_BREAKDOWN", a_low, hit["close"], c)
    if not passes:
        log.debug("[predator/asian] extension filter rejected: pct_consumed=%.2f", pct)
        return None

    entry = c
    stop  = round(a_low + 5.0, 2)
    # TPs revised 2026-08-12 (median remaining=27pt, so 30pt only hit 47%)
    tp1   = round(entry - 20.0, 2)
    tp2   = round(entry - 40.0, 2)
    sl_pts = abs(stop - entry)
    rr = round(abs(tp1 - entry) / max(sl_pts, 0.1), 2)

    confidence = "HIGH" if (vol_r or 0) >= 1.5 and (rsi_h1 or 50) < 40 else \
                 "MED"  if (vol_r or 0) >= 1.3 or (rsi_h1 or 50) < 45 else "LOW"

    return PredatorSignal(
        archetype="ASIAN_BREAKDOWN",
        direction="SELL",
        state="FIRE",
        entry=entry, stop_loss=stop, tp1=tp1, tp2=tp2, rr=rr,
        thesis=(f"M5 close {c:.2f} broke Asian_low {a_low:.2f} — overnight "
                f"buyers trapped, stops sitting below range"),
        trigger=f"first M5 close below asian_low-2pt (vol_r={vol_r} rsi={rsi_h1})",
        confidence=confidence,
        counterparty="Overnight Asian buyers who bought near range highs; "
                     "range-bound stops below asian_low",
        session=_session_label(t.hour),
        bar_time=t.isoformat(),
        fingerprint=_fingerprint("ASIAN_BREAKDOWN", "SELL", entry, t),
        pct_consumed=round(pct, 3),
        latency_pts=round(abs(a_low - c), 1),
        m5_used=True,
    )


def detect_pdl_break(m5_bars: list[tuple],
                        vol_r: Optional[float] = None) -> Optional[PredatorSignal]:
    """
    #2 edge — Prev-Day Low Break SELL.
    Fires on the FIRST M5 close below prev_day_low - 3pt with acceptance
    (prev M5 bar also closed below) + confluence + extension filter passes.
    """
    if len(m5_bars) < 20: return None
    prev_h, prev_l = _prev_day_hl(m5_bars)
    if prev_l is None: return None

    hit = _first_m5_close_below(m5_bars, prev_l - 3.0)
    if hit is None: return None

    # Acceptance: preceding M5 bar's close ALSO below the level
    slice_start = len(m5_bars) - min(len(m5_bars), 48)
    hit_idx_absolute = slice_start + hit["idx_in_slice"]
    if hit_idx_absolute < 1: return None
    if m5_bars[hit_idx_absolute - 1][4] >= prev_l:
        return None   # no acceptance — single-bar breach is fragile

    t = hit["time"]; c = hit["close"]

    a_high, a_low = _asian_range(m5_bars)
    stacked = a_low is not None and c < a_l if (a_l := a_low) is not None else False
    has_vol = vol_r is not None and vol_r >= 1.2
    if not (has_vol or stacked):
        return None

    passes, pct = _apply_extension_filter("PDL_BREAK", prev_l, c, c)
    if not passes:
        log.debug("[predator/pdl] extension filter rejected: pct=%.2f", pct)
        return None

    entry = c
    stop  = round(prev_l + 5.0, 2)
    tp1   = round(entry - 40.0, 2)
    tp2   = round(entry - 60.0, 2)
    sl_pts = abs(stop - entry)
    rr = round(abs(tp1 - entry) / max(sl_pts, 0.1), 2)

    confidence = "HIGH" if stacked and has_vol else \
                 "MED"  if stacked or has_vol else "LOW"

    return PredatorSignal(
        archetype="PDL_BREAK",
        direction="SELL",
        state="FIRE",
        entry=entry, stop_loss=stop, tp1=tp1, tp2=tp2, rr=rr,
        thesis=(f"M5 acceptance below prev_day_low {prev_l:.2f}. "
                f"Yesterday's dip-buyers underwater. "
                f"{'Stacked with Asian-low.' if stacked else ''} "
                f"{'Vol surge.' if has_vol else ''}").strip(),
        trigger=f"2 consecutive M5 closes below PDL-3pt",
        confidence=confidence,
        counterparty="Yesterday's dip-buyers whose stops sit below PDL; "
                     "retail 'PDL=support' bagholders",
        session=_session_label(t.hour),
        bar_time=t.isoformat(),
        fingerprint=_fingerprint("PDL_BREAK", "SELL", entry, t),
        pct_consumed=round(pct, 3),
        latency_pts=round(abs(prev_l - c), 1),
        m5_used=True,
    )


def detect_vol_continuation(m5_bars: list[tuple],
                                vol_r: Optional[float] = None,
                                other_signals: list[PredatorSignal] | None = None
                              ) -> Optional[PredatorSignal]:
    """
    #3 edge — High-Vol Continuation. Only fires when a primary
    (asian_breakdown or pdl_break) also fired. Same plan, distinct label.
    """
    if len(m5_bars) < 60: return None
    if vol_r is None or vol_r < 1.3: return None
    if not other_signals: return None
    primary = other_signals[0]
    t = _parse_ts(primary.bar_time)
    return PredatorSignal(
        archetype="VOL_CONTINUATION",
        direction=primary.direction,
        state="FIRE",
        entry=primary.entry, stop_loss=primary.stop_loss,
        tp1=primary.tp1, tp2=primary.tp2, rr=primary.rr,
        thesis=(f"Volume surge (ratio {vol_r:.2f}× 50-bar avg) confirms "
                f"{primary.archetype}. Continuation likely."),
        trigger=f"vol_ratio={vol_r:.2f} + confluent {primary.archetype}",
        confidence="HIGH" if vol_r >= 2.0 else "MED",
        counterparty="Fade traders and mean-reversion algos betting the "
                     "vol spike is exhaustion",
        session=primary.session,
        bar_time=primary.bar_time,
        fingerprint=_fingerprint("VOL_CONTINUATION", primary.direction,
                                    primary.entry, t),
        pct_consumed=primary.pct_consumed,
        latency_pts=primary.latency_pts,
        m5_used=True,
    )


# ─────────────────────────────────────────────────────────────────────────────
# ARMED-state helpers (pre-signal awareness)
# ─────────────────────────────────────────────────────────────────────────────

def _armed_status(m5_bars: list[tuple], rsi_h1: Optional[float]) -> Optional[dict]:
    """
    Emit an ARMED signal when price is APPROACHING a validated level (within
    5pt) but hasn't broken yet. Gives operator early awareness.
    Returns None if no proximity to level.
    """
    if len(m5_bars) < 20: return None
    a_high, a_low = _asian_range(m5_bars)
    prev_h, prev_l = _prev_day_hl(m5_bars)
    if not m5_bars: return None
    close = m5_bars[-1][4]

    # Distance from each relevant level (SELL: below asian_low or PDL)
    armed_reasons = []
    if a_low is not None and 0 < close - a_low <= 5.0:
        armed_reasons.append(f"within 5pt of asian_low {a_low:.2f}")
    if prev_l is not None and 0 < close - prev_l <= 5.0:
        armed_reasons.append(f"within 5pt of prev_day_low {prev_l:.2f}")
    if rsi_h1 is not None and rsi_h1 < 45:
        armed_reasons.append(f"RSI H1 {rsi_h1:.0f} weak")

    if not armed_reasons:
        return None
    return {
        "reasons":     armed_reasons,
        "distance_to_asian_low": (close - a_low) if a_low else None,
        "distance_to_pdl":       (close - prev_l) if prev_l else None,
        "close":       close,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Public entry
# ─────────────────────────────────────────────────────────────────────────────

def evaluate(db: Session) -> list[PredatorSignal]:
    """
    M5-close detection, regime-gated, extension-filtered, ARMED-aware.
    ENGINE MODE: SELL-PREDATOR / BUY-OBSERVATION.
    """
    m5 = _load_recent(db, "M5", n=300)      # ≥250 for lookback + 50 vol window
    if len(m5) < 80:
        log.debug("[predator] insufficient M5 bars (%d)", len(m5))
        return []

    # Regime gate
    try:
        from services.regime_detector import (
            classify_current_regime, is_predator_favorable_regime,
            regime_confidence_multiplier,
        )
        regime = classify_current_regime(db)
        allowed, reason = is_predator_favorable_regime(regime)
        if not allowed:
            log.debug("[predator] suppressed by regime gate: %s", reason)
            return []
        regime_mult = regime_confidence_multiplier(
            regime.get("direction"), regime.get("volatility"),
        )
    except Exception as exc:
        log.warning("[predator] regime gate errored: %s", exc)
        return []

    rsi_h1 = _last_h1_rsi(db)
    vol_r = _vol_ratio_m5(m5)

    signals: list[PredatorSignal] = []

    s1 = detect_asian_breakdown(m5, rsi_h1=rsi_h1, vol_r=vol_r)
    if s1: signals.append(s1)
    s2 = detect_pdl_break(m5, vol_r=vol_r)
    if s2: signals.append(s2)
    s3 = detect_vol_continuation(m5, vol_r=vol_r, other_signals=signals)
    if s3: signals.append(s3)

    # If no FIRE, emit an ARMED "watching" signal if price is near level
    if not signals:
        armed = _armed_status(m5, rsi_h1)
        if armed:
            t = m5[-1][0]
            signals.append(PredatorSignal(
                archetype="APPROACHING_LEVEL",
                direction="SELL",
                state="ARMED",
                entry=armed["close"], stop_loss=0.0, tp1=0.0, tp2=0.0, rr=0.0,
                thesis="Approaching validated level — no fire yet",
                trigger="; ".join(armed["reasons"]),
                confidence="—",
                counterparty="—",
                session=_session_label(t.hour),
                bar_time=t.isoformat(),
                fingerprint=_fingerprint("ARMED", "SELL", armed["close"], t),
                pct_consumed=None, latency_pts=None, m5_used=True,
            ))

    # Regime downgrade of FIRE confidence
    if regime_mult < 1.0:
        for sig in signals:
            if sig.state != "FIRE": continue
            if sig.confidence == "HIGH" and regime_mult < 0.8:
                sig.confidence = "MED"
            elif sig.confidence == "MED" and regime_mult < 0.6:
                sig.confidence = "LOW"

    return signals


def format_telegram_alert(sig: PredatorSignal, regime: Optional[dict] = None) -> str:
    if sig.state == "ARMED":
        lines = [
            f"👁  PREDATOR ARMED — approaching level",
            f"XAU/USD  ·  potential {sig.direction}",
        ]
        if regime:
            lines.append(f"Regime: {regime.get('direction')} × {regime.get('volatility')}")
        lines += [
            "",
            f"Current: {sig.entry:.2f}",
            f"Trigger conditions: {sig.trigger}",
            "",
            "State: ARMED (no entry yet — waiting for M5 close below level)",
        ]
        return "\n".join(lines)
    # FIRE format
    lines = [
        f"🐺 PREDATOR FIRE — {sig.archetype}  [{sig.confidence}]",
        f"MODE: SELL-PREDATOR / BUY-OBSERVATION",
        f"XAU/USD  ·  {sig.direction}  (M5-close detection)",
    ]
    if regime:
        lines.append(f"Regime: {regime.get('direction')} × {regime.get('volatility')} × {regime.get('session')}")
    lines += [
        "",
        f"Entry:  {sig.entry:.2f}",
        f"Stop:   {sig.stop_loss:.2f}   (risk {abs(sig.entry - sig.stop_loss):.1f} pts)",
        f"TP1:    {sig.tp1:.2f}",
        f"TP2:    {sig.tp2:.2f}",
        f"RR:     1:{sig.rr}",
        f"Session: {sig.session}  ·  M5 bar {sig.bar_time[:16]}",
        f"Extension: {(sig.pct_consumed or 0)*100:.0f}% of expected move consumed  (filter: OK)",
        "",
        f"Why:  {sig.thesis}",
        f"Counterparty:  {sig.counterparty}",
        f"Trigger:  {sig.trigger}",
        "",
        "Source: outcome-first empirical edge · M5 confirmation · regime-gated",
        "SELL-only engine — no BUY archetype survived audit.",
    ]
    return "\n".join(lines)


__all__ = [
    "PredatorSignal", "evaluate", "format_telegram_alert",
    "detect_asian_breakdown", "detect_pdl_break", "detect_vol_continuation",
]
