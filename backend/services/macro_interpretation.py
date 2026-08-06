"""
Enhanced Macro Interpretation — Phase 10
=========================================

The brief:
"Do not use only Bullish/Bearish/Conflicted for macro. Provide DXY direction,
 US 10Y yield direction, gold correlation state, whether macro supports or
 opposes technicals, whether correlation is active/weak/broken, current
 high-impact event risk, time until next major event, whether the move is
 macro-driven or technically driven.
 Macro should be supporting context, NOT an automatic veto."

Consumes existing services (correlation_engine, fred_provider, calendar)
and turns them into an 8-field MacroAssessment the strategist can attach
to any verdict.

Behind `xauusd_macro_interpretation_enabled`. Read-only diagnostic.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone, timedelta
from typing import Optional

log = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Output
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class MacroAssessment:
    # DXY
    dxy_direction:            str    # UP | DOWN | FLAT | UNKNOWN
    dxy_move_pct:             Optional[float] = None       # % change over lookback
    # Yields
    yield_10y_direction:      str    = "UNKNOWN"           # UP | DOWN | FLAT
    yield_10y_delta_bp:       Optional[float] = None       # basis points change
    real_yield_direction:     str    = "UNKNOWN"
    # Correlation
    gold_dxy_correlation:     Optional[float] = None       # -1 to +1 over 60 bars
    correlation_state:        str    = "UNKNOWN"           # ACTIVE_INVERSE | WEAK | BROKEN | ACTIVE_POSITIVE (rare)
    # Alignment with current technical direction
    macro_alignment:          str    = "NEUTRAL"           # SUPPORTIVE | OPPOSING | NEUTRAL | MIXED
    macro_alignment_reason:   str    = ""
    move_driver:              str    = "UNCLEAR"           # MACRO_DRIVEN | TECHNICAL_DRIVEN | HYBRID | UNCLEAR
    # Events
    next_high_impact_event:   Optional[dict] = None        # {name, time_utc, impact, currency}
    minutes_to_next_event:    Optional[int]  = None
    event_risk_level:         str    = "NONE"              # HIGH | ELEVATED | LOW | NONE
    # Meta
    warnings:                 list[str] = field(default_factory=list)
    generated_at:             datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def to_dict(self) -> dict:
        return {**asdict(self), "generated_at": self.generated_at.isoformat()}


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _ema(vals, n):
    if len(vals) < n:
        return None
    k = 2 / (n + 1)
    e = sum(vals[:n]) / n
    for v in vals[n:]:
        e = v * k + e * (1 - k)
    return e


def _slope_direction(vals, n=20, threshold_pct=0.05):
    """Return UP/DOWN/FLAT based on EMA(n) now vs 10 bars ago."""
    if len(vals) < n + 10:
        return ("UNKNOWN", None)
    now = _ema(vals, n)
    prior = _ema(vals[:-10], n)
    if now is None or prior is None:
        return ("UNKNOWN", None)
    pct = (now - prior) / prior * 100 if prior else 0
    if pct > threshold_pct:
        return ("UP", pct)
    if pct < -threshold_pct:
        return ("DOWN", pct)
    return ("FLAT", pct)


def _classify_correlation(corr: Optional[float]) -> str:
    """Gold's typical DXY correlation is around -0.6 to -0.8."""
    if corr is None:
        return "UNKNOWN"
    if corr <= -0.6:
        return "ACTIVE_INVERSE"
    if corr <= -0.3:
        return "WEAK"
    if -0.3 < corr < 0.3:
        return "BROKEN"
    return "ACTIVE_POSITIVE"           # unusual — gold + DXY moving together


def _minutes_to_next_high_impact_event(events: list) -> tuple[Optional[int], Optional[dict]]:
    """Return (minutes_to_next, event_dict) — event dict is the raw event or None."""
    if not events:
        return (None, None)
    now = datetime.now(timezone.utc)
    best_min = None
    best_ev = None
    for ev in events:
        try:
            ts = ev.get("time_utc") if isinstance(ev, dict) else getattr(ev, "time_utc", None)
            if isinstance(ts, str):
                ts = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            impact = str(ev.get("impact", "") if isinstance(ev, dict)
                          else getattr(ev, "impact", "")).lower()
            if ts and "high" in impact and ts >= now:
                m = int((ts - now).total_seconds() / 60)
                if best_min is None or m < best_min:
                    best_min = m
                    best_ev = ev if isinstance(ev, dict) else {
                        "name": getattr(ev, "name", "?"),
                        "time_utc": ts.isoformat(),
                        "impact": impact,
                    }
        except Exception:
            continue
    return (best_min, best_ev)


def _event_risk_level(minutes_to_next: Optional[int]) -> str:
    if minutes_to_next is None:
        return "NONE"
    if minutes_to_next < 15:
        return "HIGH"
    if minutes_to_next < 60:
        return "ELEVATED"
    if minutes_to_next < 240:
        return "LOW"
    return "NONE"


def _macro_alignment(
    tech_direction: str,
    dxy_dir: str,
    yield_dir: str,
) -> tuple[str, str]:
    """
    Compare technical direction with macro signals.
    - Gold is inversely correlated to DXY (rising DXY = bearish gold)
    - Gold is inversely correlated to real yields (rising yields = bearish gold)
    Returns (label, reason).
    """
    if tech_direction not in ("BULL", "BEAR"):
        return ("NEUTRAL", "no technical direction to align against")

    if tech_direction == "BULL":
        dxy_ok = dxy_dir == "DOWN"
        yld_ok = yield_dir == "DOWN"
    else:
        dxy_ok = dxy_dir == "UP"
        yld_ok = yield_dir == "UP"

    if dxy_ok and yld_ok:
        return ("SUPPORTIVE", f"technical {tech_direction} + DXY {dxy_dir} + yields {yield_dir}")
    if dxy_ok or yld_ok:
        return ("MIXED", f"partial macro support ({'DXY' if dxy_ok else 'yields'})")
    if dxy_dir in ("UP", "DOWN") or yield_dir in ("UP", "DOWN"):
        return ("OPPOSING", f"technical {tech_direction} but DXY {dxy_dir}, yields {yield_dir}")
    return ("NEUTRAL", "macro flat")


def _move_driver(
    tech_direction: str,
    gold_move_pct: Optional[float],
    dxy_move_pct: Optional[float],
    correlation_state: str,
) -> str:
    """Was the gold move driven by macro or by technicals?"""
    if tech_direction not in ("BULL", "BEAR"):
        return "UNCLEAR"
    if gold_move_pct is None or dxy_move_pct is None:
        return "UNCLEAR"
    if correlation_state == "BROKEN":
        return "TECHNICAL_DRIVEN"
    # Gold went bull + DXY strongly down = macro-driven
    if tech_direction == "BULL" and dxy_move_pct <= -0.3:
        return "MACRO_DRIVEN"
    if tech_direction == "BEAR" and dxy_move_pct >= 0.3:
        return "MACRO_DRIVEN"
    # Gold moved > 0.5% but DXY nearly flat → technicals
    if abs(gold_move_pct) >= 0.5 and abs(dxy_move_pct) < 0.15:
        return "TECHNICAL_DRIVEN"
    return "HYBRID"


# ─────────────────────────────────────────────────────────────────────────────
# Public entry point
# ─────────────────────────────────────────────────────────────────────────────

def compute_macro_context(
    *,
    snapshot=None,
    tech_direction: str = "NEUTRAL",
    upcoming_events: Optional[list] = None,
    dxy_bars: Optional[list] = None,
    correlation_snapshot: Optional[dict] = None,
    yields_context: Optional[dict] = None,
) -> MacroAssessment:
    """
    Compose the full macro assessment. Fails open — always returns an
    assessment. Any missing input just leaves that field as UNKNOWN.

    Inputs are all optional — the caller passes what's cheap to fetch.
    """
    warnings: list[str] = []

    # ── DXY direction ─────────────────────────────────────────────────────
    dxy_direction = "UNKNOWN"
    dxy_move_pct = None
    if dxy_bars and len(dxy_bars) >= 30:
        closes = []
        for b in dxy_bars:
            c = None
            if hasattr(b, "close"):
                c = b.close
            elif isinstance(b, dict):
                c = b.get("close") or b.get("Close") or b.get("c")
            else:
                try: c = float(b)
                except (TypeError, ValueError): c = None
            if c is not None:
                closes.append(float(c))
        if len(closes) >= 30:
            dxy_direction, dxy_move_pct = _slope_direction(closes)
    elif correlation_snapshot:
        # Best-effort: look for DXY row in pairs
        for p in correlation_snapshot.get("pairs", []):
            if p.get("code", "").lower() in ("dxy", "usdx"):
                # We can't infer direction from correlation alone; leave unknown
                pass

    # ── Yields ────────────────────────────────────────────────────────────
    yield_direction = "UNKNOWN"
    yield_delta_bp = None
    real_yield_direction = "UNKNOWN"
    if yields_context and yields_context.get("available"):
        y_trend = yields_context.get("yieldsTrend", "unknown").lower()
        yield_direction = {
            "rising": "UP", "falling": "DOWN", "flat": "FLAT"
        }.get(y_trend, "UNKNOWN")
        # Nominal 10Y delta in bp (values from FRED are in %)
        dgs10_delta = yields_context.get("dgs10Delta")
        if dgs10_delta is not None:
            yield_delta_bp = float(dgs10_delta) * 100
        # Real yield trend inferred from realYieldDelta
        real_delta = yields_context.get("realYieldDelta")
        if real_delta is not None:
            if real_delta > 0.05:
                real_yield_direction = "UP"
            elif real_delta < -0.05:
                real_yield_direction = "DOWN"
            else:
                real_yield_direction = "FLAT"
    else:
        warnings.append("yields context unavailable")

    # ── Gold-DXY correlation ──────────────────────────────────────────────
    gold_dxy_corr = None
    if correlation_snapshot:
        for p in correlation_snapshot.get("pairs", []):
            code = p.get("code", "").lower()
            if code in ("dxy", "usdx"):
                gold_dxy_corr = p.get("current_corr") or p.get("corr_60")
                break
    correlation_state = _classify_correlation(gold_dxy_corr)

    # ── Gold move over lookback (needs snapshot with H1) ──────────────────
    gold_move_pct = None
    if snapshot and snapshot.timeframes and snapshot.timeframes.get("H1"):
        h1_bars = snapshot.timeframes["H1"].candles
        if h1_bars and len(h1_bars) >= 20:
            first_close = h1_bars[-20].close
            last_close = h1_bars[-1].close
            if first_close > 0:
                gold_move_pct = (last_close - first_close) / first_close * 100

    # ── Alignment + move driver ───────────────────────────────────────────
    alignment, alignment_reason = _macro_alignment(
        tech_direction, dxy_direction, real_yield_direction or yield_direction,
    )
    driver = _move_driver(tech_direction, gold_move_pct, dxy_move_pct, correlation_state)

    # ── Event risk ────────────────────────────────────────────────────────
    minutes_to_next, next_event = _minutes_to_next_high_impact_event(
        upcoming_events or [])
    event_risk = _event_risk_level(minutes_to_next)

    return MacroAssessment(
        dxy_direction=dxy_direction,
        dxy_move_pct=round(dxy_move_pct, 3) if dxy_move_pct is not None else None,
        yield_10y_direction=yield_direction,
        yield_10y_delta_bp=round(yield_delta_bp, 2) if yield_delta_bp is not None else None,
        real_yield_direction=real_yield_direction,
        gold_dxy_correlation=round(gold_dxy_corr, 3) if gold_dxy_corr is not None else None,
        correlation_state=correlation_state,
        macro_alignment=alignment, macro_alignment_reason=alignment_reason,
        move_driver=driver,
        next_high_impact_event=next_event,
        minutes_to_next_event=minutes_to_next,
        event_risk_level=event_risk,
        warnings=warnings,
    )


__all__ = ["compute_macro_context", "MacroAssessment"]
