"""
Signal Grading (A+ / A / B / C / STAND_ASIDE)
==============================================

Turns the strategist's raw verdict into a discrete quality grade used
by the alert gate and Telegram template.

Grade table:
  A+   score >= 90, RR >= 3.0, liquidity confirmed, cp >= 4
  A    score 80-89, RR >= 2.5, cp >= 4
  B    score 70-79, cp >= 3    → WATCHLIST ONLY (never fires alert
                                  unless watchlist mode explicitly on)
  C    score < 70              → suppress
  STAND_ASIDE  decision != BUY/SELL, missing SL/TP/invalidation,
               data stale, news within block window, spread over cap,
               or conflicted directional bias.

CME gold options context is attached to the verdict before grading. It is
read-only/unsigned context: it enriches the engine journal and Telegram card
but does NOT change the grade or direction until forward validation proves an
edge. This prevents OI concentration from being mislabelled as dealer GEX.

Signal-only mode: no code path here enables execution. Grading is
purely a filter on which alerts reach the user.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

log = logging.getLogger(__name__)

GRADE_APLUS  = "A+"
GRADE_A      = "A"
GRADE_B      = "B"      # watchlist only
GRADE_C      = "C"      # suppress
GRADE_ASIDE  = "STAND_ASIDE"

ALERT_GRADES        = {GRADE_APLUS, GRADE_A}
WATCHLIST_GRADES    = {GRADE_B}
SUPPRESS_GRADES     = {GRADE_C, GRADE_ASIDE}


@dataclass
class GradeResult:
    grade:            str
    reason:           str
    should_alert:     bool
    should_watchlist: bool
    should_suppress:  bool

    def to_dict(self) -> dict:
        return {
            "grade":            self.grade,
            "reason":           self.reason,
            "should_alert":     self.should_alert,
            "should_watchlist": self.should_watchlist,
            "should_suppress":  self.should_suppress,
        }


def _attach_cme_context(verdict: dict) -> None:
    """Attach one read-only CME context snapshot. Never blocks grading."""
    if "cme_options_context" in verdict:
        return
    try:
        from database import SessionLocal
        from services.cme_options_context import get_cme_options_context
        with SessionLocal() as db:
            verdict["cme_options_context"] = get_cme_options_context(db)
    except Exception as exc:
        verdict["cme_options_context"] = {
            "status": "UNAVAILABLE",
            "reason": f"{type(exc).__name__}: {exc}",
            "directional_bias": "UNSIGNED_NEUTRAL",
        }
        log.debug("[signal_grading] CME context unavailable: %s", exc)


def grade_verdict(verdict: dict, *,
                    min_score_a: int = 80,
                    min_score_aplus: int = 90,
                    min_rr_a: float = 2.5,
                    min_rr_aplus: float = 3.0,
                    watchlist_score: int = 70) -> GradeResult:
    """
    Compute the grade for a strategist verdict. Fails safe → STAND_ASIDE.
    """
    _attach_cme_context(verdict)

    decision = verdict.get("decision")
    if decision not in ("BUY", "SELL"):
        return GradeResult(GRADE_ASIDE,
                            f"decision={decision} (not BUY/SELL)",
                            False, False, True)

    cp = verdict.get("conditions_passed", 0) or 0
    setup_score = verdict.get("setup_score") or 0
    tp = verdict.get("trade_plan") or {}
    entry = tp.get("entry")
    stop_loss = tp.get("stop_loss")
    tp1 = tp.get("tp1")
    tp2 = tp.get("tp2")
    rr = tp.get("risk_reward") or 0.0
    invalidation = tp.get("invalidation") or verdict.get("invalidation_price") \
                    or stop_loss   # SL doubles as invalidation when no explicit level

    # Hard STAND_ASIDE — anything missing = no grade
    if entry is None or stop_loss is None or tp1 is None or tp2 is None:
        return GradeResult(GRADE_ASIDE,
                            "missing entry/SL/TP1/TP2",
                            False, False, True)
    if invalidation is None:
        return GradeResult(GRADE_ASIDE, "no invalidation level defined",
                            False, False, True)
    if rr <= 0:
        return GradeResult(GRADE_ASIDE, f"RR={rr} <= 0",
                            False, False, True)
    if cp < 3:
        return GradeResult(GRADE_ASIDE,
                            f"only {cp}/5 conditions passed",
                            False, False, True)

    # News-block / spread-cap / data-stale checks (via execution_status)
    es = verdict.get("execution_status") or ""
    if es in ("NEWS_BLOCKED", "SPREAD_HIGH", "DATA_STALE"):
        return GradeResult(GRADE_ASIDE,
                            f"execution_status={es}",
                            False, False, True)

    # Liquidity model confirmation (informational — enforced by cp>=4)
    lm = verdict.get("liquidity_model") or {}
    lm_confirmed = bool(lm.get("type") or lm.get("confirmed"))

    # A+ : top-tier
    if (cp >= 4 and setup_score >= min_score_aplus
            and rr >= min_rr_aplus and lm_confirmed):
        return GradeResult(GRADE_APLUS,
                            f"score={setup_score}>={min_score_aplus}, RR=1:{rr}>=1:{min_rr_aplus}, "
                            f"cp={cp}/5, liquidity confirmed",
                            True, False, False)

    # A : alertable
    if cp >= 4 and setup_score >= min_score_a and rr >= min_rr_a:
        return GradeResult(GRADE_A,
                            f"score={setup_score}>={min_score_a}, RR=1:{rr}>=1:{min_rr_a}, cp={cp}/5",
                            True, False, False)

    # B : watchlist
    if cp >= 3 and setup_score >= watchlist_score:
        return GradeResult(GRADE_B,
                            f"score={setup_score} in [{watchlist_score},{min_score_a}) — watchlist only",
                            False, True, False)

    # C : suppress
    return GradeResult(GRADE_C,
                        f"score={setup_score}<{watchlist_score} or RR=1:{rr}<1:{min_rr_a}",
                        False, False, True)


def _format_cme_context(verdict: dict) -> list[str]:
    ctx = verdict.get("cme_options_context") or {}
    if ctx.get("status") != "OBSERVED":
        return []

    lines = [
        "",
        "CME OPTIONS CONTEXT (unsigned)",
        f"Bulletin: {ctx.get('bulletin_date', '—')} {ctx.get('bulletin_status', '')}".rstrip(),
    ]
    basis = ctx.get("gc_xau_basis")
    if basis is not None:
        lines.append(f"GC-XAU basis: {basis:+.2f}")

    nearest = ctx.get("nearest_zones") or []
    if nearest:
        lines.append("Nearest mapped concentrations:")
        for z in nearest[:2]:
            xau = z.get("xau_equiv")
            gc = z.get("gc_strike")
            oi = z.get("total_oi")
            score = z.get("sensitivity_score")
            lines.append(f"  XAU {xau} <- GC {gc} | OI {oi} | sensitivity {score}")

    strongest = ctx.get("strongest_zones") or []
    if strongest:
        lines.append("Strongest mapped concentrations:")
        for z in strongest[:2]:
            lines.append(
                f"  XAU {z.get('xau_equiv')} <- GC {z.get('gc_strike')} | "
                f"OI {z.get('total_oi')} | dOI {z.get('oi_change'):+d}"
            )
    lines.append("Context only: no dealer-gamma sign inferred.")
    return lines


def format_signal_grade_body(verdict: dict, grade_result: GradeResult,
                                *, spread_pts: Optional[float] = None,
                                data_source: str = "Canonical XAUUSD + optional MT5 + CME options context"
                                ) -> str:
    """
    Render the standardized Telegram signal card with read-only CME context.
    Grade goes in the header; CME context never changes the trade grade here.
    """
    decision = verdict.get("decision", "STAND ASIDE")
    tp = verdict.get("trade_plan") or {}
    lm = verdict.get("liquidity_model") or {}
    tc = verdict.get("technical_confirmation") or {}

    bias = decision if decision in ("BUY", "SELL") else "NEUTRAL"
    setup = lm.get("type") or verdict.get("liquidity_behaviour") or "—"

    # Extract 4H manipulation context (Phase 6 layer if present)
    fhm = tc.get("four_hour_manipulation") or {}
    if fhm.get("classification"):
        setup = f"{setup} + 4H {fhm['classification']}"

    entry = tp.get("entry")
    entry_tol = tp.get("entry_tolerance", 0)
    sl = tp.get("stop_loss")
    tp1 = tp.get("tp1")
    tp2 = tp.get("tp2")
    tp3 = tp.get("tp3")
    if tp3 is None and tp1 is not None and tp2 is not None and entry is not None:
        try:
            direction_sign = 1 if tp2 > tp1 else -1
            tp3 = round(tp2 + direction_sign * 0.5 * abs(tp2 - tp1), 2)
        except Exception:
            tp3 = "—"

    rr = tp.get("risk_reward") or 0
    setup_score = verdict.get("setup_score") or 0
    session = verdict.get("session_classification") or "—"
    invalidation = tp.get("invalidation") or verdict.get("invalidation_price") or sl

    spread_str = "—"
    if spread_pts is not None:
        spread_str = f"{spread_pts:.1f} pts"

    grade = grade_result.grade
    header = f"XAUUSD SIGNAL — {grade}"

    lines = [
        header,
        "",
        f"Bias: {bias}",
        f"Setup: {setup}",
        f"Entry Zone: {entry} +/- {entry_tol}" if entry is not None else "Entry Zone: —",
        f"Stop Loss: {sl}" if sl is not None else "Stop Loss: —",
        f"Take Profit 1: {tp1}" if tp1 is not None else "Take Profit 1: —",
        f"Take Profit 2: {tp2}" if tp2 is not None else "Take Profit 2: —",
        f"Take Profit 3: {tp3}",
        f"Risk/Reward: 1:{rr}",
        f"Setup Score: {setup_score}",
        f"Session: {session}",
        f"Spread: {spread_str}",
        f"Data Source: {data_source}",
        f"Invalidation: {invalidation}" if invalidation is not None else "Invalidation: —",
        "",
        "Reason:",
        f"{grade_result.reason}",
    ]
    lines.extend(_format_cme_context(verdict))
    lines.extend([
        "",
        "(Signal-only mode — no automatic order placement.)",
        "",
        "ENGINE: LEGACY",
    ])
    return "\n".join(lines)


def format_stand_aside_body(verdict: dict, grade_result: GradeResult) -> str:
    """Compact stand-aside summary — no trade levels."""
    setup = (verdict.get("liquidity_model") or {}).get("type", "—")
    return "\n".join([
        f"XAUUSD SIGNAL — STAND ASIDE",
        "",
        f"Bias: NEUTRAL",
        f"Setup: {setup}",
        f"Session: {verdict.get('session_classification', '—')}",
        f"Setup Score: {verdict.get('setup_score', 0)}",
        f"Data Source: Canonical XAUUSD + optional MT5 + CME options context",
        "",
        "Reason:",
        f"{grade_result.reason}",
        "",
        "(No compliant entry. No trade sent.)",
        "",
        "ENGINE: LEGACY",
    ])


__all__ = [
    "grade_verdict", "format_signal_grade_body", "format_stand_aside_body",
    "GradeResult",
    "GRADE_APLUS", "GRADE_A", "GRADE_B", "GRADE_C", "GRADE_ASIDE",
    "ALERT_GRADES", "WATCHLIST_GRADES", "SUPPRESS_GRADES",
]
