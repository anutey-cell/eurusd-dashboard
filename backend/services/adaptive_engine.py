# -*- coding: utf-8 -*-
"""
Adaptive Learning Engine
========================
Analyses every logged trade outcome in the signals table to:

  1. Measure per-component prediction accuracy
     (does "Liquidity swept" actually precede wins? by how much?)

  2. Compute calibrated scoring weights
     Baseline weights: HTF=15, Liq=20, MS=20, FVG=20, News=15, Session=10
     After >= MIN_SAMPLES outcomes the weights drift toward accuracy-based values.

  3. Compute engine maturity score (0–100)
     Matures at MATURITY_TARGET outcomes (default 100).

  4. Store / load weights from the engine_weights DB table so they survive restarts.

Called by:
  - signal_engine.analyze_signal()   → loads weights before scoring
  - routers/engine.py                → exposes /engine/status endpoint
  - Scheduled background task (every LEARN_EVERY_MINUTES minutes)

Algorithm:
  For each completed signal (result = WIN | LOSS | BREAKEVEN):
    - Detect which ICT components were active (from text fields in signals table)
    - Treat BREAKEVEN as 0.5 win
    - Compute win_rate per component: wins / (wins + losses + 0.5*BEs)
    - Expected baseline win rate = BASELINE_WIN_RATE (default 0.55)
    - accuracy_multiplier = win_rate / BASELINE_WIN_RATE
    - Clamp multiplier: [MIN_MULT, MAX_MULT]
    - new_weight = round(base_weight * multiplier)
    - Apply only if n_samples >= MIN_SAMPLES (safety gate)

Component detection from DB text fields:
  HTF active:     macro_status NOT containing "Neutral" AND not empty
  Liq active:     liquidity_status containing "swept"
  MS active:      structure_status containing "BOS" or "CHoCH"
  FVG active:     fvg_status NOT containing "No Fair Value" AND not empty
  News clear:     news_status = "CLEAR"
  Session good:   session containing "kill zone" or "Overlap" or "London" or "New York"
"""
from __future__ import annotations

import json
import logging
import math
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy.orm import Session

from db_models import EngineWeightsRecord, SignalRecord

log = logging.getLogger(__name__)

# ── Constants ─────────────────────────────────────────────────────────────────

# Base weights (must sum to 100)
BASE_WEIGHTS = {
    "htf":     15,
    "liq":     20,
    "ms":      20,
    "fvg":     20,
    "news":    15,
    "session": 10,
}

MIN_SAMPLES        = 15      # minimum outcomes before adjusting a component's weight
MATURITY_TARGET    = 100     # outcomes needed for full maturity
BASELINE_WIN_RATE  = 0.55    # expected win rate at baseline (slightly better than random)
MIN_MULT           = 0.50    # maximum weight reduction factor
MAX_MULT           = 2.00    # maximum weight boost factor
LEARN_EVERY_MINUTES = 60     # background refresh cadence


# ── Data structures ───────────────────────────────────────────────────────────

@dataclass
class ComponentStats:
    name:          str
    base_weight:   int
    learned_weight: int
    n_samples:     int        # outcomes where component was active
    win_rate:      float      # 0.0–1.0 (BE = 0.5)
    accuracy_mult: float      # learned_weight / base_weight
    calibrated:    bool       # True if n_samples >= MIN_SAMPLES


@dataclass
class AdaptiveWeights:
    """
    Used by signal_engine.py to override base component scores.
    All fields match signal_engine component score variables.
    """
    htf:     int = BASE_WEIGHTS["htf"]
    liq:     int = BASE_WEIGHTS["liq"]
    ms:      int = BASE_WEIGHTS["ms"]
    fvg:     int = BASE_WEIGHTS["fvg"]
    news:    int = BASE_WEIGHTS["news"]
    session: int = BASE_WEIGHTS["session"]

    # Metadata (not used in scoring — surfaced in /engine/status)
    maturity_score:   int   = 0       # 0–100
    n_outcomes:       int   = 0       # total completed outcomes analysed
    overall_win_rate: float = 0.0
    calibrated:       bool  = False   # True when >= 1 component has been adjusted
    pair:             str   = "all"
    updated_at:       str   = ""

    def as_score_dict(self) -> dict:
        """Return only the scoring weights (no metadata)."""
        return {k: getattr(self, k) for k in BASE_WEIGHTS}


# ── Component presence detector ───────────────────────────────────────────────

def _detect_components(rec: SignalRecord) -> dict[str, bool]:
    """
    Infer which ICT components were active from text fields stored at signal time.
    Returns dict: component_name → True/False (active / not-active)
    """
    liq  = (rec.liquidity_status  or "").lower()
    ms   = (rec.structure_status  or "").lower()
    fvg  = (rec.fvg_status        or "").lower()
    htf  = (rec.macro_status      or "").lower()
    sess = (rec.session           or "").lower()
    news = (rec.news_status       or "CLEAR").upper()

    return {
        "htf":     bool(htf) and "neutral" not in htf,
        "liq":     "swept" in liq,
        "ms":      "bos" in ms or "choch" in ms,
        "fvg":     bool(fvg) and "no fair value" not in fvg,
        "news":    news == "CLEAR",
        "session": any(k in sess for k in ("kill zone", "overlap", "london", "new york")),
    }


def _outcome_score(result: str) -> float:
    """Convert result string to numeric score: WIN=1, LOSS=0, BREAKEVEN=0.5."""
    if result == "WIN":    return 1.0
    if result == "LOSS":   return 0.0
    if result == "BREAKEVEN": return 0.5
    return 0.5   # unknown → neutral


# ── Core learning function ────────────────────────────────────────────────────

def _compute_weights(
    outcomes: list[SignalRecord],
    pair: str = "all",
) -> AdaptiveWeights:
    """
    Analyse a list of completed SignalRecords and return calibrated AdaptiveWeights.
    """
    if not outcomes:
        return AdaptiveWeights(pair=pair, updated_at=_now())

    # Accumulators per component: (total_score, total_seen)
    acc: dict[str, list[float]] = {k: [] for k in BASE_WEIGHTS}
    all_scores: list[float] = []

    for rec in outcomes:
        score = _outcome_score(rec.result)
        all_scores.append(score)
        components = _detect_components(rec)
        for comp, active in components.items():
            if active:
                acc[comp].append(score)

    n_total          = len(all_scores)
    overall_win_rate = sum(all_scores) / n_total if n_total else 0.0
    maturity_score   = min(100, round(n_total / MATURITY_TARGET * 100))

    weights  = {}
    any_calibrated = False

    for comp, base in BASE_WEIGHTS.items():
        samples = acc[comp]
        n       = len(samples)
        if n >= MIN_SAMPLES:
            wr   = sum(samples) / n
            mult = wr / BASELINE_WIN_RATE
            mult = max(MIN_MULT, min(MAX_MULT, mult))
            w    = max(1, round(base * mult))
            any_calibrated = True
        else:
            w = base
        weights[comp] = w

    # Re-scale so weights still sum to 100 (keeps scoring model valid)
    total = sum(weights.values())
    if total != 100 and total > 0:
        factor = 100 / total
        weights = {k: max(1, round(v * factor)) for k, v in weights.items()}
        # Fix rounding residual on the largest component
        diff = 100 - sum(weights.values())
        largest = max(weights, key=weights.get)
        weights[largest] += diff

    return AdaptiveWeights(
        htf=weights["htf"],
        liq=weights["liq"],
        ms=weights["ms"],
        fvg=weights["fvg"],
        news=weights["news"],
        session=weights["session"],
        maturity_score=maturity_score,
        n_outcomes=n_total,
        overall_win_rate=round(overall_win_rate, 4),
        calibrated=any_calibrated,
        pair=pair,
        updated_at=_now(),
    )


def _now() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


# ── DB persistence ────────────────────────────────────────────────────────────

def _save_weights(db: Session, weights: AdaptiveWeights) -> None:
    rec = db.query(EngineWeightsRecord).filter(
        EngineWeightsRecord.pair == weights.pair
    ).first()
    data = asdict(weights)
    if rec is None:
        rec = EngineWeightsRecord(**{
            "pair":             weights.pair,
            "htf_weight":       weights.htf,
            "liq_weight":       weights.liq,
            "ms_weight":        weights.ms,
            "fvg_weight":       weights.fvg,
            "news_weight":      weights.news,
            "session_weight":   weights.session,
            "n_samples":        weights.n_outcomes,
            "overall_win_rate": weights.overall_win_rate,
            "maturity_score":   weights.maturity_score,
            "calibrated":       weights.calibrated,
        })
        db.add(rec)
    else:
        rec.htf_weight       = weights.htf
        rec.liq_weight       = weights.liq
        rec.ms_weight        = weights.ms
        rec.fvg_weight       = weights.fvg
        rec.news_weight      = weights.news
        rec.session_weight   = weights.session
        rec.n_samples        = weights.n_outcomes
        rec.overall_win_rate = weights.overall_win_rate
        rec.maturity_score   = weights.maturity_score
        rec.calibrated       = weights.calibrated
        rec.updated_at       = datetime.now(tz=timezone.utc)
    db.commit()
    log.info("[adaptive] Saved weights for pair=%s maturity=%d%% outcomes=%d",
             weights.pair, weights.maturity_score, weights.n_outcomes)


def _load_weights(db: Session, pair: str = "all") -> AdaptiveWeights | None:
    rec = db.query(EngineWeightsRecord).filter(
        EngineWeightsRecord.pair == pair
    ).first()
    if rec is None:
        return None
    return AdaptiveWeights(
        htf=rec.htf_weight,
        liq=rec.liq_weight,
        ms=rec.ms_weight,
        fvg=rec.fvg_weight,
        news=rec.news_weight,
        session=rec.session_weight,
        maturity_score=rec.maturity_score,
        n_outcomes=rec.n_samples,
        overall_win_rate=rec.overall_win_rate,
        calibrated=rec.calibrated,
        pair=rec.pair,
        updated_at=rec.updated_at.isoformat() if rec.updated_at else "",
    )


# ── Public interface ──────────────────────────────────────────────────────────

def run_learning_cycle(db: Session, pair: str = "all") -> AdaptiveWeights:
    """
    Main entry point — query DB, compute weights, persist, return.
    Called by the engine router's POST /engine/learn and the background scheduler.
    """
    query = db.query(SignalRecord).filter(SignalRecord.result.isnot(None))
    if pair != "all":
        # Normalise: "EUR/USD" → "eurusd" for DB match
        pair_filter = pair.lower().replace("/", "")
        query = query.filter(SignalRecord.pair.ilike(f"%{pair_filter.upper()[:3]}%"))
    outcomes = query.all()
    weights = _compute_weights(outcomes, pair=pair)
    _save_weights(db, weights)
    return weights


def get_current_weights(db: Session, pair: str = "all") -> AdaptiveWeights:
    """
    Return cached weights from DB if available, else compute fresh.
    Used by signal_engine.py on every analysis call.
    """
    cached = _load_weights(db, pair=pair)
    if cached is not None:
        return cached
    # First time — compute and save
    return run_learning_cycle(db, pair=pair)


def get_component_stats(db: Session, pair: str = "all") -> list[ComponentStats]:
    """
    Detailed per-component breakdown — used by the /engine/status endpoint.
    """
    query = db.query(SignalRecord).filter(SignalRecord.result.isnot(None))
    if pair != "all":
        pair_filter = pair.lower().replace("/", "")
        query = query.filter(SignalRecord.pair.ilike(f"%{pair_filter.upper()[:3]}%"))
    outcomes = query.all()

    # Load current learned weights
    current = _load_weights(db, pair) or AdaptiveWeights(pair=pair)

    acc: dict[str, list[float]] = {k: [] for k in BASE_WEIGHTS}
    for rec in outcomes:
        score = _outcome_score(rec.result)
        for comp, active in _detect_components(rec).items():
            if active:
                acc[comp].append(score)

    learned = {
        "htf":     current.htf,
        "liq":     current.liq,
        "ms":      current.ms,
        "fvg":     current.fvg,
        "news":    current.news,
        "session": current.session,
    }
    comp_names = {
        "htf": "HTF Bias", "liq": "Liquidity Sweep",
        "ms": "Market Structure", "fvg": "Fair Value Gap",
        "news": "News Risk", "session": "Session Timing",
    }

    stats = []
    for comp, base in BASE_WEIGHTS.items():
        samples = acc[comp]
        n = len(samples)
        wr = sum(samples) / n if n else 0.0
        lw = learned[comp]
        stats.append(ComponentStats(
            name=comp_names[comp],
            base_weight=base,
            learned_weight=lw,
            n_samples=n,
            win_rate=round(wr, 4),
            accuracy_mult=round(lw / base, 3),
            calibrated=(n >= MIN_SAMPLES),
        ))
    return stats


def full_engine_status(db: Session, pair: str = "all") -> dict:
    """
    Single dict that the /engine/status endpoint returns.
    Aggregates maturity, weights, per-component stats, and bridge status.
    """
    from services.live_feed import bridge_status          # local import — avoid circular
    from services.tradingview_provider import tv_status
    from services.myfxbook_provider import myfxbook_status

    weights = get_current_weights(db, pair=pair)
    stats   = get_component_stats(db, pair=pair)

    # Per-pair breakdown
    pair_stats = {}
    for p_code in ("eurusd", "xauusd"):
        query = db.query(SignalRecord).filter(
            SignalRecord.result.isnot(None),
            SignalRecord.pair.ilike(f"%{p_code.upper()[:3]}%"),
        )
        rows = query.all()
        if rows:
            scores = [_outcome_score(r.result) for r in rows]
            pair_stats[p_code] = {
                "n_outcomes": len(rows),
                "win_rate":   round(sum(scores) / len(scores), 4),
            }
        else:
            pair_stats[p_code] = {"n_outcomes": 0, "win_rate": 0.0}

    return {
        "maturity": {
            "score":           weights.maturity_score,
            "level":           _maturity_label(weights.maturity_score),
            "n_outcomes":      weights.n_outcomes,
            "target":          MATURITY_TARGET,
            "overall_win_rate": weights.overall_win_rate,
            "calibrated":      weights.calibrated,
        },
        "weights": {
            "current":  weights.as_score_dict(),
            "baseline": BASE_WEIGHTS,
            "pair":     weights.pair,
            "updated_at": weights.updated_at,
        },
        "components": [
            {
                "name":           s.name,
                "base_weight":    s.base_weight,
                "learned_weight": s.learned_weight,
                "n_samples":      s.n_samples,
                "win_rate":       s.win_rate,
                "accuracy_mult":  s.accuracy_mult,
                "calibrated":     s.calibrated,
            }
            for s in stats
        ],
        "pair_breakdown": pair_stats,
        "bridge":      bridge_status(),
        "tradingview": tv_status(),
        "myfxbook":    myfxbook_status(),
    }


def _maturity_label(score: int) -> str:
    if score < 10:  return "Initialising"
    if score < 25:  return "Early Learning"
    if score < 50:  return "Developing"
    if score < 75:  return "Maturing"
    if score < 100: return "Near-Mature"
    return "Fully Mature"
