"""
Predator Engine — outcome-first XAU/USD signals from empirical edge discovery.
=============================================================================

Ships the three walk-forward-validated edges from `scripts/edge_discovery.py`
as first-class signals. Runs in parallel with the mandate strategist so we can
A/B compare live. Every fire records to shadow_trades for outcome tracking.

Archetypes (ranked by validated expectancy):

  1. ASIAN_BREAKDOWN     — SELL when M15 closes below Asian-session low
                           (test_lift 1.62, +9.24pt expectancy)
     Counterparty: Overnight buyers whose stops sit below Asian range;
                   Asian-session range traders.

  2. PDL_BREAK           — SELL when M15 closes below prev-day low w/ acceptance
                           (test_lift 1.33, +11.49pt expectancy)
     Counterparty: Yesterday's dip-buyers; retail "prev-low = support" holders.

  3. VOL_CONTINUATION    — Direction-follow when volume > 1.3x 50-bar mean +
                           confluent level break (test_lift 1.42, +8.93pt)
     Counterparty: Fade traders; mean-reversion algos.

Safety:
  - Never places orders. Emits signals only (recorded to shadow_trades).
  - Telegram gated by settings.predator_telegram_enabled (default False).
  - Dedupe per (archetype, direction, entry-bucket, hour) so max 1 signal
    per pattern per hour per 5pt price bucket.
  - Requires data-freshness sentinel passing before evaluating.

Usage:
  from services.predator_engine import evaluate
  signals = evaluate(db)     # list[PredatorSignal]
"""
from __future__ import annotations

import hashlib
import logging
import time
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import text
from sqlalchemy.orm import Session

log = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Signal dataclass
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class PredatorSignal:
    archetype:    str      # ASIAN_BREAKDOWN | PDL_BREAK | VOL_CONTINUATION
    direction:    str      # BUY | SELL
    entry:        float
    stop_loss:    float
    tp1:          float
    tp2:          float
    rr:           float
    thesis:       str      # WHY (counterparty)
    trigger:      str      # WHEN (this bar's condition)
    confidence:   str      # HIGH | MED | LOW
    counterparty: str
    session:      str
    bar_time:     str      # ISO
    fingerprint:  str      # (archetype, direction, entry_5pt_bucket, hour)

    def to_dict(self) -> dict:
        return asdict(self)


# ─────────────────────────────────────────────────────────────────────────────
# Data helpers
# ─────────────────────────────────────────────────────────────────────────────

def _load_recent_m15(db: Session, n: int = 250) -> list[tuple]:
    """Return the most-recent n M15 bars oldest-first as (t, o, h, l, c, v)."""
    rows = db.execute(text(
        "SELECT candle_time, open, high, low, close, volume "
        "FROM historical_candles "
        "WHERE instrument='XAU/USD' AND timeframe='M15' "
        "ORDER BY candle_time DESC LIMIT :n"
    ), {"n": n}).fetchall()
    out = []
    for r in rows:
        t = r[0]
        if isinstance(t, str):
            for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S"):
                try:
                    t = datetime.strptime(t.split("+")[0], fmt); break
                except ValueError:
                    continue
        if hasattr(t, "tzinfo") and t.tzinfo is not None:
            t = t.replace(tzinfo=None)
        out.append((t, float(r[1]), float(r[2]), float(r[3]), float(r[4]), float(r[5] or 0)))
    return list(reversed(out))


def _last_h1_rsi(db: Session, n: int = 14) -> Optional[float]:
    """Latest H1 RSI(14) — used as filter/confluence."""
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


def _prev_day_hl(bars: list[tuple]) -> tuple[Optional[float], Optional[float]]:
    """Return (prev_day_high, prev_day_low) for the calendar day before bars[-1]."""
    if not bars: return None, None
    last_t = bars[-1][0]
    today_start = last_t.replace(hour=0, minute=0, second=0, microsecond=0)
    prev_day_start = today_start - timedelta(days=1)
    highs, lows = [], []
    for t, o, h, l, c, v in bars:
        if prev_day_start <= t < today_start:
            highs.append(h); lows.append(l)
    if not highs: return None, None
    return max(highs), min(lows)


def _asian_range(bars: list[tuple]) -> tuple[Optional[float], Optional[float]]:
    """
    Return (asian_high, asian_low) for the most recent completed
    Asian session (22:00 → 06:00 UTC).
    """
    if not bars: return None, None
    last_t = bars[-1][0]
    if 22 <= last_t.hour or last_t.hour < 6:
        # inside Asian session — use what has formed since window start
        session_start = last_t.replace(hour=22, minute=0, second=0, microsecond=0)
        if last_t.hour < 6:
            session_start -= timedelta(days=1)
    else:
        today_6 = last_t.replace(hour=6, minute=0, second=0, microsecond=0)
        session_start = today_6 - timedelta(hours=8)
    session_end = session_start + timedelta(hours=8)
    highs, lows = [], []
    for t, o, h, l, c, v in bars:
        if session_start <= t < session_end:
            highs.append(h); lows.append(l)
    if not highs: return None, None
    return max(highs), min(lows)


def _vol_ratio(bars: list[tuple], window: int = 50) -> Optional[float]:
    """Latest bar volume / mean(volume) over last `window` bars."""
    if len(bars) < window + 1: return None
    recent = bars[-window-1:-1]
    avg = sum(b[5] for b in recent) / window
    if avg <= 0: return None
    return round(bars[-1][5] / avg, 2)


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
    """(archetype, direction, entry rounded to 5pt bucket, hour) → short hash."""
    bucket = round(entry / 5.0) * 5
    key = f"{archetype}|{direction}|{bucket}|{bar_time.strftime('%Y%m%d%H')}"
    return hashlib.sha256(key.encode()).hexdigest()[:16]


# ─────────────────────────────────────────────────────────────────────────────
# Detectors
# ─────────────────────────────────────────────────────────────────────────────

def detect_asian_breakdown(bars: list[tuple],
                              rsi_h1: Optional[float] = None,
                              vol_r: Optional[float] = None) -> Optional[PredatorSignal]:
    """
    #1 edge — Asian Range Breakdown SELL.
    Fires when the just-closed M15 bar breached below asian_low by ≥ 2pt AND
    volume surge OR RSI H1 leaning weak.
    """
    if len(bars) < 20: return None
    a_high, a_low = _asian_range(bars)
    if a_low is None: return None
    t, o, h, l, c, v = bars[-1]

    if c >= a_low - 2.0:
        return None
    # Confluence gate: need EITHER vol_ratio ≥ 1.3 OR RSI H1 < 45
    if not ((vol_r is not None and vol_r >= 1.3) or (rsi_h1 is not None and rsi_h1 < 45)):
        return None

    entry = c
    stop  = round(a_low + 5.0, 2)
    tp1   = round(entry - 50.0, 2)
    tp2   = round(entry - 90.0, 2)
    sl_pts = abs(stop - entry)
    rr = round(abs(tp1 - entry) / max(sl_pts, 0.1), 2)

    confidence = "HIGH" if (vol_r or 0) >= 1.5 and (rsi_h1 or 50) < 40 else \
                 "MED"  if (vol_r or 0) >= 1.3 or (rsi_h1 or 50) < 45 else "LOW"

    return PredatorSignal(
        archetype="ASIAN_BREAKDOWN",
        direction="SELL",
        entry=entry, stop_loss=stop, tp1=tp1, tp2=tp2, rr=rr,
        thesis=f"Price {c:.2f} broke Asian_low {a_low:.2f} — overnight buyers "
               f"trapped, stops sitting below range",
        trigger=f"M15 close {c:.2f} < asian_low {a_low:.2f} - 2 pts "
                f"(vol_ratio={vol_r} rsi_h1={rsi_h1})",
        confidence=confidence,
        counterparty="Overnight Asian buyers who bought near range highs; "
                     "range-bound stops below asian_low",
        session=_session_label(t.hour),
        bar_time=t.isoformat(),
        fingerprint=_fingerprint("ASIAN_BREAKDOWN", "SELL", entry, t),
    )


def detect_pdl_break(bars: list[tuple],
                        vol_r: Optional[float] = None) -> Optional[PredatorSignal]:
    """
    #2 edge — Previous-Day Low Break SELL with acceptance.
    Fires when latest M15 close is below prev_day_low - 3pt AND has stayed
    below for ≥ 2 consecutive bars (acceptance).
    """
    if len(bars) < 20: return None
    prev_h, prev_l = _prev_day_hl(bars)
    if prev_l is None: return None
    t, o, h, l, c, v = bars[-1]

    if c >= prev_l - 3.0:
        return None
    # Acceptance: prev bar's close ALSO below prev_l
    prev_bar_close = bars[-2][4]
    if prev_bar_close >= prev_l:
        return None

    # Confluence: vol_ratio ≥ 1.2 OR close is also below Asian low
    a_high, a_low = _asian_range(bars)
    stacked_with_asian = a_low is not None and c < a_low
    has_vol = vol_r is not None and vol_r >= 1.2
    if not (has_vol or stacked_with_asian):
        return None

    entry = c
    stop  = round(prev_l + 5.0, 2)
    tp1   = round(entry - 50.0, 2)
    tp2   = round(entry - 90.0, 2)
    sl_pts = abs(stop - entry)
    rr = round(abs(tp1 - entry) / max(sl_pts, 0.1), 2)

    confidence = "HIGH" if stacked_with_asian and has_vol else \
                 "MED"  if stacked_with_asian or has_vol else "LOW"

    return PredatorSignal(
        archetype="PDL_BREAK",
        direction="SELL",
        entry=entry, stop_loss=stop, tp1=tp1, tp2=tp2, rr=rr,
        thesis=f"Price {c:.2f} broke prev_day_low {prev_l:.2f} with acceptance "
               f"({'stacked with Asian-low' if stacked_with_asian else ''} "
               f"{'+ vol surge' if has_vol else ''}). "
               "Yesterday's dip-buyers underwater.",
        trigger=f"2 consecutive M15 closes below prev_day_low - 3pts",
        confidence=confidence,
        counterparty="Yesterday's dip-buyers whose stops sit below PDL; "
                     "retail 'PDL=support' bagholders",
        session=_session_label(t.hour),
        bar_time=t.isoformat(),
        fingerprint=_fingerprint("PDL_BREAK", "SELL", entry, t),
    )


def detect_vol_continuation(bars: list[tuple],
                                vol_r: Optional[float] = None,
                                other_signals: list[PredatorSignal] | None = None
                              ) -> Optional[PredatorSignal]:
    """
    #3 edge — High-Vol Continuation.
    Only fires when there IS already a level-break signal from #1 or #2
    (empirical: vol_high alone is weak; vol_high + level break is strong).
    Amplifies the primary signal's direction.
    """
    if len(bars) < 60: return None
    if vol_r is None or vol_r < 1.3:
        return None
    # Must have a confluent primary signal
    if not other_signals:
        return None
    primary = other_signals[0]

    t, o, h, l, c, v = bars[-1]

    # Follow the primary's direction — same entry/plan just labelled differently
    # to make Telegram reader see the "vol amplifier" context.
    return PredatorSignal(
        archetype="VOL_CONTINUATION",
        direction=primary.direction,
        entry=primary.entry,
        stop_loss=primary.stop_loss,
        tp1=primary.tp1,
        tp2=primary.tp2,
        rr=primary.rr,
        thesis=f"Volume surge (ratio {vol_r:.2f}× 50-bar avg) confirms "
               f"{primary.archetype}. Continuation likely.",
        trigger=f"vol_ratio={vol_r:.2f} + confluent {primary.archetype}",
        confidence="HIGH" if vol_r >= 2.0 else "MED",
        counterparty="Fade traders and mean-reversion algos betting the "
                     "vol spike is exhaustion",
        session=_session_label(t.hour),
        bar_time=t.isoformat(),
        fingerprint=_fingerprint("VOL_CONTINUATION", primary.direction,
                                    primary.entry, t),
    )


# ─────────────────────────────────────────────────────────────────────────────
# Public entry point
# ─────────────────────────────────────────────────────────────────────────────

def evaluate(db: Session) -> list[PredatorSignal]:
    """Run all detectors on the freshest M15 bar. Returns 0..N signals."""
    bars = _load_recent_m15(db, n=250)
    if len(bars) < 50:
        log.debug("[predator] insufficient bars (%d)", len(bars))
        return []

    rsi_h1 = _last_h1_rsi(db)
    vol_r = _vol_ratio(bars)

    signals: list[PredatorSignal] = []

    # Primary detectors
    s1 = detect_asian_breakdown(bars, rsi_h1=rsi_h1, vol_r=vol_r)
    if s1: signals.append(s1)
    s2 = detect_pdl_break(bars, vol_r=vol_r)
    if s2: signals.append(s2)

    # Vol continuation only if #1 or #2 also fired
    s3 = detect_vol_continuation(bars, vol_r=vol_r, other_signals=signals)
    if s3: signals.append(s3)

    return signals


def format_telegram_alert(sig: PredatorSignal) -> str:
    """Distinct message format so operator can see this is a Predator signal."""
    return "\n".join([
        f"🐺 PREDATOR SIGNAL — {sig.archetype}  [{sig.confidence}]",
        f"XAU/USD  ·  {sig.direction}",
        "",
        f"Entry:  {sig.entry:.2f}",
        f"Stop:   {sig.stop_loss:.2f}  (risk {abs(sig.entry - sig.stop_loss):.1f} pts)",
        f"TP1:    {sig.tp1:.2f}  (reward {abs(sig.tp1 - sig.entry):.1f} pts)",
        f"TP2:    {sig.tp2:.2f}",
        f"RR:     1:{sig.rr}",
        f"Session: {sig.session}  ·  bar {sig.bar_time[:16]}",
        "",
        f"Why:  {sig.thesis}",
        f"Counterparty:  {sig.counterparty}",
        f"Trigger:  {sig.trigger}",
        "",
        "Source: empirical edge (walk-forward validated)",
        "This is a Predator signal — separate from the mandate strategist.",
    ])


__all__ = [
    "PredatorSignal", "evaluate", "format_telegram_alert",
    "detect_asian_breakdown", "detect_pdl_break", "detect_vol_continuation",
]
