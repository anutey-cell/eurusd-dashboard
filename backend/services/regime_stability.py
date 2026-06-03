"""
Regime Stability Index
======================

Quantifies how stable the engine's current directional bias is, and how
close each of the four flip-drivers is to reversing. Output is consumed by
the daily briefing (23:00 EAT) to give the operator early warning of an
incoming regime shift before it actually fires.

Four flip-drivers tracked on 0-100 scale (higher = more stable):
  • D1 alignment     — D1 close gap to EMA20, normalized by ATR
  • H4 alignment     — H4 close gap to EMA20, normalized by ATR
  • Macro alignment  — DXY/yields/news consensus with current direction
  • ICT framework    — ICT alignment score (PO3 + Daily Open + P/D + Judas)

Stability label:
  ≥80 — rock solid
  60-79 — bias stable
  40-59 — wobbling
  <40 — approaching flip ⚠
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from sqlalchemy.orm import Session

log = logging.getLogger(__name__)


# ────────────────────────────────────────────────────────────────────────
# Public entry point
# ────────────────────────────────────────────────────────────────────────

def compute_regime_stability(db: Session) -> dict:
    """
    Returns the full regime-stability snapshot. Pure read — never mutates.
    """
    from services.strategist import make_decision
    from data.candles import get_candles

    try:
        verdict = make_decision(db)
    except Exception as exc:
        log.warning("[regime] verdict fetch failed: %s", exc)
        return _empty_payload(f"verdict_unavailable: {exc}")

    current = verdict.get("decision", "STAND ASIDE")
    # If we're STAND ASIDE, the "current bias" for stability purposes is
    # the LAST BUY/SELL signal seen (the bias persists through stand-asides).
    bias_for_stability = current if current in ("BUY", "SELL") else _last_directional_bias(db)

    # Duration of current regime (ticks + wall time)
    duration = _compute_regime_duration(db, bias_for_stability)

    # Pull candles for HTF stability scoring
    try:
        h4 = get_candles(interval="H4", limit=120, pair="xauusd")
        d1 = get_candles(interval="D1", limit=60,  pair="xauusd")
    except Exception as exc:
        log.warning("[regime] candle fetch failed: %s", exc)
        h4 = d1 = None

    drivers = {
        "d1_alignment":    _htf_stability(d1.candles if d1 else [], bias_for_stability),
        "h4_alignment":    _htf_stability(h4.candles if h4 else [], bias_for_stability),
        "macro":           _macro_stability(verdict, bias_for_stability),
        "ict_framework":   _ict_stability(verdict),
    }

    # Overall score = average of non-null drivers
    scores = [d["score"] for d in drivers.values() if d.get("score") is not None]
    overall = round(sum(scores) / len(scores), 0) if scores else None

    # Early-warning flags — drivers below 40
    weak = [name for name, d in drivers.items()
            if d.get("score") is not None and d["score"] < 40]

    return {
        "current_decision":     current,
        "bias_for_stability":   bias_for_stability,
        "duration_ticks":       duration["ticks"],
        "duration_human":       duration["human"],
        "duration_since":       duration["since_iso"],
        "drivers":              drivers,
        "overall_score":        overall,
        "overall_label":        _stability_label(overall) if overall is not None else "n/a",
        "approaching_flip":     bool(weak),
        "weak_drivers":         weak,
    }


def _empty_payload(reason: str) -> dict:
    return {
        "current_decision":  "UNKNOWN",
        "bias_for_stability": None,
        "duration_ticks":    0,
        "duration_human":    "n/a",
        "duration_since":    None,
        "drivers":           {},
        "overall_score":     None,
        "overall_label":     reason,
        "approaching_flip":  False,
        "weak_drivers":      [],
    }


# ────────────────────────────────────────────────────────────────────────
# Drivers
# ────────────────────────────────────────────────────────────────────────

def _htf_stability(candles, direction: Optional[str]) -> dict:
    """
    D1 / H4 alignment strength.

    score = 50 + 25 × min(gap/ATR, 2)   when aligned with direction
          = 50 - 25 × min(|gap|/ATR, 2)  when against direction
    so 100 = price ≥2 ATR in favored direction
       50 = price on EMA20
        0 = price ≥2 ATR in opposite direction
    """
    if not candles or len(candles) < 20 or direction not in ("BUY", "SELL"):
        return {"score": None, "label": "insufficient data", "detail": ""}

    closes = [c.close for c in candles]
    highs  = [c.high  for c in candles]
    lows   = [c.low   for c in candles]
    ema20 = _ema(closes, 20)
    atr   = _atr(highs, lows, closes, n=14)

    if atr <= 0:
        return {"score": None, "label": "no ATR", "detail": ""}

    last = closes[-1]
    # Signed gap, positive when aligned with current direction
    gap = (last - ema20) if direction == "BUY" else (ema20 - last)
    ratio = gap / atr

    # Map [-2, +2] → [0, 100]
    score = max(0, min(100, int(50 + 25 * max(-2, min(2, ratio)))))
    label = _stability_label(score)
    sign = "above" if direction == "BUY" else "below"
    detail = f"close ${last:.2f}  {sign} EMA20 ${ema20:.2f}  gap ${gap:+.2f}  ({ratio:+.2f}×ATR)"
    return {"score": score, "label": label, "detail": detail}


def _macro_stability(verdict: dict, direction: Optional[str]) -> dict:
    """Macro alignment with current direction."""
    mc = verdict.get("macro_context") or {}
    alignment = mc.get("macro_alignment", "Neutral")

    if direction not in ("BUY", "SELL"):
        return {"score": None, "label": "no bias", "detail": "—"}

    # Direct mapping from strategist's macro_alignment label
    score_map = {"Aligned": 95, "Neutral": 50, "Conflicted": 5}
    score = score_map.get(alignment, 50)
    label = _stability_label(score)
    bias = mc.get("gold_macro_bias", "—")
    news = mc.get("news_risk", "—")
    detail = f"{alignment} ({bias}) · news={news}"
    return {"score": score, "label": label, "detail": detail}


def _ict_stability(verdict: dict) -> dict:
    """ICT framework score → stability score."""
    import re
    text = verdict.get("institutional_logic", "") or ""
    # institutional_logic actually emits "ICT(72/100 ALIGNED)" — case-insensitive match
    m = re.search(r"ict\((\d+)/100", text, re.IGNORECASE)
    score = None
    if m:
        try:
            score = int(m.group(1))
        except ValueError:
            pass
    if score is None:
        return {"score": None, "label": "n/a", "detail": "ICT score not surfaced"}
    label = _stability_label(score)
    return {"score": score, "label": label, "detail": f"ICT alignment {score}/100"}


# ────────────────────────────────────────────────────────────────────────
# Regime duration — how long has this direction been stable?
# ────────────────────────────────────────────────────────────────────────

def _compute_regime_duration(db: Session, current_bias: Optional[str]) -> dict:
    """
    Walk strategist_verdicts backwards to find the last verdict that
    contradicts current_bias (e.g. the most recent BUY when current = SELL).
    Returns ticks + wall-time duration.
    """
    if current_bias not in ("BUY", "SELL"):
        return {"ticks": 0, "human": "no bias", "since_iso": None}

    from db_models import StrategistVerdict
    opposite = "SELL" if current_bias == "BUY" else "BUY"

    flip_row = (
        db.query(StrategistVerdict)
        .filter(StrategistVerdict.decision == opposite)
        .order_by(StrategistVerdict.created_at.desc())
        .first()
    )

    if flip_row is None:
        # Never flipped — find earliest verdict of any kind
        first_row = (
            db.query(StrategistVerdict)
            .order_by(StrategistVerdict.created_at.asc())
            .first()
        )
        since = first_row.created_at if first_row else datetime.now(timezone.utc)
    else:
        since = flip_row.created_at

    if since.tzinfo is None:
        since = since.replace(tzinfo=timezone.utc)
    delta = datetime.now(timezone.utc) - since
    ticks = int(delta.total_seconds() / 60)   # 60s per tick

    days = delta.days
    hours = delta.seconds // 3600
    minutes = (delta.seconds % 3600) // 60
    if days > 0:
        human = f"{days}d {hours}h"
    elif hours > 0:
        human = f"{hours}h {minutes}m"
    else:
        human = f"{minutes}m"

    return {"ticks": ticks, "human": human, "since_iso": since.isoformat()}


def _last_directional_bias(db: Session) -> Optional[str]:
    """Find the most recent BUY/SELL verdict so STAND-ASIDE pauses don't reset bias."""
    from db_models import StrategistVerdict
    row = (
        db.query(StrategistVerdict)
        .filter(StrategistVerdict.decision.in_(("BUY", "SELL")))
        .order_by(StrategistVerdict.created_at.desc())
        .first()
    )
    return row.decision if row else None


# ────────────────────────────────────────────────────────────────────────
# Formatter — block to embed in the daily briefing
# ────────────────────────────────────────────────────────────────────────

def format_regime_stability_block(rs: dict) -> str:
    """Compose the regime stability section for the daily briefing message."""
    bias = rs.get("bias_for_stability") or "—"
    bias_emoji = "🟢" if bias == "BUY" else "🔴" if bias == "SELL" else "⚪"
    overall = rs.get("overall_score")
    overall_str = f"{overall:.0f}%" if overall is not None else "—"
    overall_label = rs.get("overall_label", "—")
    duration = rs.get("duration_human", "n/a")
    ticks = rs.get("duration_ticks", 0)

    lines = [
        f"🧭 REGIME STABILITY INDEX",
        f"  Current bias:    {bias_emoji} {bias}  ({duration}, {ticks} ticks)",
        f"  Overall:         {overall_str}  ({overall_label})",
        f"",
        f"  Driver pressure (lower = closer to flip):",
    ]

    driver_labels = {
        "d1_alignment":  "D1 alignment",
        "h4_alignment":  "H4 alignment",
        "macro":         "Macro",
        "ict_framework": "ICT framework",
    }
    drivers = rs.get("drivers", {})
    for key, label in driver_labels.items():
        d = drivers.get(key, {})
        score = d.get("score")
        if score is None:
            lines.append(f"    {label:<14} —")
            continue
        bar = _bar(score)
        warn = " ⚠ approaching flip" if score < 40 else ""
        lines.append(f"    {label:<14} {bar} {score:>3.0f}%  {d.get('label', '')}{warn}")
        detail = d.get("detail")
        if detail and detail != "—":
            lines.append(f"                                 {detail[:80]}")

    if rs.get("approaching_flip"):
        weak = rs.get("weak_drivers") or []
        names = ", ".join(weak)
        lines += [
            f"",
            f"  ⚠️ Early flip warning — {len(weak)} driver(s) below 40%: {names}",
            f"     Watch for ICT score drop OR macro shift in the next session.",
        ]
    return "\n".join(lines)


# ────────────────────────────────────────────────────────────────────────
# Helpers
# ────────────────────────────────────────────────────────────────────────

def _stability_label(score: Optional[int]) -> str:
    if score is None:        return "n/a"
    if score >= 80:          return "rock solid"
    if score >= 60:          return "bias stable"
    if score >= 40:          return "wobbling"
    return "approaching flip"


def _bar(score: int, width: int = 10) -> str:
    """ASCII bar — 10 chars wide, █ filled / ░ empty."""
    fill = int(round((score / 100) * width))
    return "█" * fill + "░" * (width - fill)


def _ema(values, n):
    if len(values) < n: return values[-1] if values else 0.0
    k = 2 / (n + 1)
    e = sum(values[:n]) / n
    for v in values[n:]:
        e = v * k + e * (1 - k)
    return round(e, 2)


def _atr(highs, lows, closes, n=14):
    if len(highs) < n + 1: return 0.0
    trs = []
    for i in range(1, len(highs)):
        trs.append(max(
            highs[i] - lows[i],
            abs(highs[i] - closes[i - 1]),
            abs(lows[i]  - closes[i - 1]),
        ))
    return round(sum(trs[-n:]) / n, 2)
