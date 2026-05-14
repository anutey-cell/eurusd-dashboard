"""
Pair Readiness Scoring — per-pair 0-100 readiness assessment.

Each pair is evaluated across seven independent dimensions.
A pair must score >= 85 before its operating mode can be promoted
above MONITOR_ONLY.

Scoring dimensions (max points):
  1. Data quality          20 pts — live/tradingview vs synthetic
  2. Sample size           20 pts — completed trades with outcomes
  3. Win-rate stability    15 pts — consistent above breakeven threshold
  4. Model accuracy        15 pts — quality_score distribution
  5. News filter coverage  10 pts — critical events mapped and tested
  6. Session coverage      10 pts — trades across at least 2 sessions
  7. Spread / volatility   10 pts — spread and ATR configs validated

Penalty deductions:
  -10  stale candles in last 48 h
  -10  >20% of signals classified as STALE_CANDLES or data errors
  -5   no completed trades in last 7 days (stagnant record)
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from pair_config import get_pair_config, SUPPORTED_PAIRS

log = logging.getLogger(__name__)


# ── Result types ──────────────────────────────────────────────────────────────

@dataclass
class ReadinessDimension:
    name: str
    score: int
    max_score: int
    status: str          # "pass" | "partial" | "fail"
    detail: str


@dataclass
class PairReadinessResult:
    pair_code:    str
    display:      str
    mode:         str
    total_score:  int           # 0-100
    gate_score:   int           # 85+ to promote above MONITOR_ONLY
    promotion_eligible: bool
    dimensions:   list[ReadinessDimension]
    blockers:     list[str]     # human-readable reasons preventing promotion
    warnings:     list[str]     # non-blocking advisories
    assessed_at:  str


# ── Scoring helpers ───────────────────────────────────────────────────────────

def _dim(name: str, score: int, max_score: int, detail: str) -> ReadinessDimension:
    if score >= max_score:
        status = "pass"
    elif score > 0:
        status = "partial"
    else:
        status = "fail"
    return ReadinessDimension(name=name, score=score, max_score=max_score,
                              status=status, detail=detail)


def _data_quality_score(db_records: list[Any], pair_code: str) -> tuple[ReadinessDimension, list[str], list[str]]:
    """Score data quality based on source tags in recent records."""
    blockers: list[str] = []
    warnings: list[str] = []

    if not db_records:
        return _dim("Data Quality", 0, 20, "No confirmed signals — cannot assess data source"), blockers, warnings

    # Count source types in confirmed records
    sources = [getattr(r, "data_source", "synthetic") or "synthetic" for r in db_records]
    stale_count   = sum(1 for s in sources if s == "stale")
    live_count    = sum(1 for s in sources if s in ("live", "mt5"))
    tv_count      = sum(1 for s in sources if s == "tradingview")
    synth_count   = sum(1 for s in sources if s == "synthetic")
    total         = len(sources)

    stale_pct = stale_count / total * 100 if total else 0
    if stale_pct > 20:
        blockers.append(f"Data Quality: {stale_pct:.0f}% of signals used stale candles (max 20%)")
        return _dim("Data Quality", 0, 20, f"{stale_pct:.0f}% stale signals — data feed unreliable"), blockers, warnings

    if live_count / total >= 0.5:
        score, detail = 20, f"Majority live MT5 data ({live_count}/{total} signals)"
    elif tv_count / total >= 0.5:
        score, detail = 15, f"Majority TradingView data ({tv_count}/{total} signals)"
    elif (live_count + tv_count) / total >= 0.3:
        score, detail = 10, f"Mixed real/synthetic ({live_count + tv_count} real, {synth_count} synthetic)"
        warnings.append("Data Quality: fewer than 50% of signals used real market data")
    else:
        score, detail = 5, f"Mostly synthetic data ({synth_count}/{total} signals)"
        warnings.append("Data Quality: majority synthetic — switch to real data source before promoting mode")

    return _dim("Data Quality", score, 20, detail), blockers, warnings


def _sample_size_score(completed: list[Any]) -> tuple[ReadinessDimension, list[str], list[str]]:
    blockers: list[str] = []
    warnings: list[str] = []
    n = len(completed)

    if n == 0:
        blockers.append("Sample Size: no completed trades with outcomes recorded")
        return _dim("Sample Size", 0, 20, "No completed trades"), blockers, warnings
    elif n < 10:
        score, detail = 5, f"{n} completed trades (need 10+ for partial, 30+ for full)"
        warnings.append(f"Sample Size: only {n} completed trades — log more results before promoting")
    elif n < 20:
        score, detail = 10, f"{n} completed trades (30+ recommended)"
        warnings.append(f"Sample Size: {n} trades — developing sample, not statistically robust")
    elif n < 30:
        score, detail = 15, f"{n} completed trades — approaching full score"
    else:
        score, detail = 20, f"{n} completed trades — sufficient sample"

    return _dim("Sample Size", score, 20, detail), blockers, warnings


def _win_rate_score(completed: list[Any]) -> tuple[ReadinessDimension, list[str], list[str]]:
    blockers: list[str] = []
    warnings: list[str] = []
    wins = [t for t in completed if getattr(t, "result", None) == "WIN"]
    n = len(completed)

    if n == 0:
        return _dim("Win-Rate Stability", 0, 15, "No completed trades"), blockers, warnings

    wr = len(wins) / n * 100

    if wr >= 55:
        score, detail = 15, f"{wr:.1f}% win rate — above 55% threshold"
    elif wr >= 48:
        score, detail = 10, f"{wr:.1f}% win rate — above breakeven; 55%+ ideal"
        warnings.append(f"Win Rate: {wr:.1f}% — monitor for at least 10 more trades before promotion")
    elif wr >= 40:
        score, detail = 5, f"{wr:.1f}% win rate — below ideal; continue observation"
        warnings.append(f"Win Rate: {wr:.1f}% — below 48% breakeven threshold")
    else:
        score, detail = 0, f"{wr:.1f}% win rate — below breakeven"
        blockers.append(f"Win Rate: {wr:.1f}% — must exceed 48% before promotion")

    return _dim("Win-Rate Stability", score, 15, detail), blockers, warnings


def _model_accuracy_score(confirmed: list[Any]) -> tuple[ReadinessDimension, list[str], list[str]]:
    """Score based on quality_score distribution across confirmed signals."""
    blockers: list[str] = []
    warnings: list[str] = []
    scores = [getattr(r, "quality_score", None) for r in confirmed if getattr(r, "quality_score", None) is not None]

    if not scores:
        return _dim("Model Accuracy", 0, 15, "No quality scores recorded"), blockers, warnings

    avg_qs = sum(scores) / len(scores)
    pct_above_80 = sum(1 for s in scores if s >= 80) / len(scores) * 100

    if avg_qs >= 82 and pct_above_80 >= 85:
        score, detail = 15, f"Avg quality {avg_qs:.1f}, {pct_above_80:.0f}% above gate"
    elif avg_qs >= 80 and pct_above_80 >= 70:
        score, detail = 10, f"Avg quality {avg_qs:.1f}, {pct_above_80:.0f}% above 80-pt gate"
        warnings.append(f"Model Accuracy: {100 - pct_above_80:.0f}% of confirmed signals are borderline — review gate")
    elif avg_qs >= 78:
        score, detail = 7, f"Avg quality {avg_qs:.1f} — marginally below ideal 82+"
        warnings.append(f"Model Accuracy: avg score {avg_qs:.1f} — review signal filter tightness")
    else:
        score, detail = 3, f"Avg quality {avg_qs:.1f} — consistently below 80-pt gate"
        blockers.append(f"Model Accuracy: avg quality score {avg_qs:.1f} — signals not consistently meeting quality gate")

    return _dim("Model Accuracy", score, 15, detail), blockers, warnings


def _news_filter_score(pair_code: str) -> tuple[ReadinessDimension, list[str], list[str]]:
    """Score news filter based on config completeness."""
    blockers: list[str] = []
    warnings: list[str] = []
    cfg = get_pair_config(pair_code)
    critical_events = cfg.get("critical_news_events", [])
    news_currencies = cfg.get("news_currencies", [])
    block_before    = cfg.get("news_block_minutes_before", 0)
    block_after     = cfg.get("news_block_minutes_after",  0)

    if not critical_events:
        blockers.append("News Filter: no critical_news_events configured for this pair")
        return _dim("News Filter Coverage", 0, 10, "No critical events configured"), blockers, warnings

    score = 0
    details = []

    if len(critical_events) >= 5:
        score += 4
        details.append(f"{len(critical_events)} critical events mapped")
    elif len(critical_events) >= 3:
        score += 2
        details.append(f"{len(critical_events)} critical events mapped (5+ recommended)")
        warnings.append("News Filter: add more critical events to strengthen news filter")

    if news_currencies:
        score += 3
        details.append(f"currencies: {', '.join(news_currencies)}")

    if block_before >= 30 and block_after >= 60:
        score += 3
        details.append(f"block window: -{block_before}m / +{block_after}m")
    elif block_before >= 15:
        score += 2
        details.append(f"block window: -{block_before}m / +{block_after}m (narrow)")
        warnings.append("News Filter: consider wider block window (30m before, 60m after)")

    return _dim("News Filter Coverage", min(score, 10), 10, "; ".join(details)), blockers, warnings


def _session_coverage_score(completed: list[Any]) -> tuple[ReadinessDimension, list[str], list[str]]:
    """Score based on how many sessions have recorded trades."""
    blockers: list[str] = []
    warnings: list[str] = []

    if not completed:
        return _dim("Session Coverage", 0, 10, "No completed trades"), blockers, warnings

    sessions = set()
    for t in completed:
        sess = (getattr(t, "session", "") or "").lower()
        if "london" in sess:     sessions.add("London")
        elif "overlap" in sess:  sessions.add("Overlap")
        elif "new york" in sess or " ny" in sess: sessions.add("New York")
        elif "asian" in sess or "tokyo" in sess:  sessions.add("Asian")

    n_sessions = len(sessions)
    if n_sessions >= 3:
        score, detail = 10, f"Active in {n_sessions} sessions: {', '.join(sorted(sessions))}"
    elif n_sessions == 2:
        score, detail = 7, f"Active in {n_sessions} sessions: {', '.join(sorted(sessions))}"
        warnings.append("Session Coverage: only 2 sessions covered — observe at least 3")
    elif n_sessions == 1:
        score, detail = 4, f"Only active in: {', '.join(sessions)}"
        warnings.append("Session Coverage: only 1 session recorded — need multi-session observation")
    else:
        score, detail = 0, "No session data in completed trades"

    return _dim("Session Coverage", score, 10, detail), blockers, warnings


def _spread_volatility_score(pair_code: str) -> tuple[ReadinessDimension, list[str], list[str]]:
    """Score based on whether spread and volatility filters are configured."""
    blockers: list[str] = []
    warnings: list[str] = []
    cfg = get_pair_config(pair_code)

    score = 0
    details = []

    max_spread = cfg.get("max_spread")
    if max_spread:
        score += 4
        details.append(f"max spread: {max_spread} {cfg.get('target_units', 'pips')}")

    if cfg.get("volatility_filter_enabled"):
        atr_min = cfg.get("atr_min", 0)
        atr_max = cfg.get("atr_max", 0)
        if atr_min > 0 and atr_max > 0:
            score += 6
            details.append(f"ATR filter: {atr_min}–{atr_max} {cfg.get('target_units', 'pips')}")
        else:
            score += 2
            details.append("volatility filter enabled but ATR limits not set")
            warnings.append("Spread/Volatility: set atr_min and atr_max for ATR filtering")
    else:
        warnings.append("Spread/Volatility: volatility filter disabled — enable for DEMO_ELIGIBLE mode")

    return _dim("Spread / Volatility", min(score, 10), 10, "; ".join(details) or "No spread/ATR config"), blockers, warnings


# ── Penalty checks ────────────────────────────────────────────────────────────

def _stale_candle_penalty(confirmed: list[Any]) -> tuple[int, list[str]]:
    """Return penalty points and warning messages for stale candle signals."""
    warnings: list[str] = []
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=48)

    stale_recent = [
        r for r in confirmed
        if getattr(r, "data_source", None) == "stale"
        and getattr(r, "created_at", None) is not None
        and (
            r.created_at.replace(tzinfo=timezone.utc) if r.created_at.tzinfo is None else r.created_at
        ) >= cutoff
    ]

    if stale_recent:
        warnings.append(f"Penalty: {len(stale_recent)} stale-candle signals in last 48h (-10 pts)")
        return 10, warnings
    return 0, warnings


def _stagnant_record_penalty(completed: list[Any]) -> tuple[int, list[str]]:
    """Penalty if no completed trades in last 7 days."""
    warnings: list[str] = []
    if not completed:
        return 0, warnings

    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=7)

    recent = [
        t for t in completed
        if getattr(t, "created_at", None) is not None
        and (
            t.created_at.replace(tzinfo=timezone.utc) if t.created_at.tzinfo is None else t.created_at
        ) >= cutoff
    ]

    if not recent and len(completed) > 0:
        warnings.append("Penalty: no trade outcomes recorded in last 7 days — stagnant record (-5 pts)")
        return 5, warnings
    return 0, warnings


# ── Master scorer ─────────────────────────────────────────────────────────────

def assess_pair_readiness(
    pair_code: str,
    confirmed_records: list[Any],   # all confirmed SignalRecord rows for this pair
) -> PairReadinessResult:
    """
    Compute the full readiness assessment for one pair.

    Parameters
    ----------
    pair_code          Short code, e.g. "eurusd"
    confirmed_records  SignalRecord rows where confirmed=True (DB objects or ducks)
    """
    cfg = get_pair_config(pair_code)
    completed = [r for r in confirmed_records if getattr(r, "result", None) in ("WIN", "LOSS", "BREAKEVEN")]

    all_blockers: list[str] = []
    all_warnings: list[str] = []
    dims: list[ReadinessDimension] = []
    total = 0

    # 1. Data quality
    d, bl, wa = _data_quality_score(confirmed_records, pair_code)
    dims.append(d); total += d.score; all_blockers += bl; all_warnings += wa

    # 2. Sample size
    d, bl, wa = _sample_size_score(completed)
    dims.append(d); total += d.score; all_blockers += bl; all_warnings += wa

    # 3. Win-rate stability
    d, bl, wa = _win_rate_score(completed)
    dims.append(d); total += d.score; all_blockers += bl; all_warnings += wa

    # 4. Model accuracy
    d, bl, wa = _model_accuracy_score(confirmed_records)
    dims.append(d); total += d.score; all_blockers += bl; all_warnings += wa

    # 5. News filter
    d, bl, wa = _news_filter_score(pair_code)
    dims.append(d); total += d.score; all_blockers += bl; all_warnings += wa

    # 6. Session coverage
    d, bl, wa = _session_coverage_score(completed)
    dims.append(d); total += d.score; all_blockers += bl; all_warnings += wa

    # 7. Spread / volatility
    d, bl, wa = _spread_volatility_score(pair_code)
    dims.append(d); total += d.score; all_blockers += bl; all_warnings += wa

    # Penalties
    pen1, wa1 = _stale_candle_penalty(confirmed_records)
    pen2, wa2 = _stagnant_record_penalty(completed)
    all_warnings += wa1 + wa2
    total = max(0, total - pen1 - pen2)

    promotion_eligible = (total >= 85) and not all_blockers

    return PairReadinessResult(
        pair_code=pair_code,
        display=cfg["display"],
        mode=cfg["mode"],
        total_score=total,
        gate_score=85,
        promotion_eligible=promotion_eligible,
        dimensions=dims,
        blockers=all_blockers,
        warnings=all_warnings,
        assessed_at=datetime.now(timezone.utc).isoformat(),
    )
