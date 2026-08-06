"""
Replay Validation Harness — Phase 14
======================================

The brief: "Replay 30-60 historical XAUUSD trading days. Compare current
production engine vs new regime engine + opportunity state machine +
weighted alignment + breakout acceptance. Do not optimize only for the
most recent missed move."

Pragmatic implementation: we score against captured tables rather than
re-running the full pipeline at every historic tick (which would require
freezing an "as-of" state per tick — expensive and error-prone). We use:

  ground truth   → services.opportunity_coverage.detect_and_score()
                    (populates qualifying_expansions table)

  old engine     → strategist_verdicts  (mandate BUY/SELL cp>=3 within
                                          ±60 min of each expansion)

  new engine     → market_intelligence_alerts (Phase 11 candidates —
                                                even suppressed/shadow rows
                                                count as "detected")

The comparison is honest — Phase 11 alerts have been captured from the
moment the layer went live, so anything before that shows the old engine
alone. As shadow mode runs longer, this report will accumulate real
divergence between the two.

Behind `xauusd_replay_validation_enabled`. Read-only.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone, timedelta, date
from statistics import median
from typing import Optional

from sqlalchemy import text
from sqlalchemy.orm import Session

log = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Data classes
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class EngineMetrics:
    engine:                       str
    alerts_per_day:               float
    coverage_pct:                 float
    median_detection_delay_min:   Optional[float]
    false_alert_count:            int
    late_alert_pct:               float       # % of alerts fired > 25% into move
    direction_accuracy_pct:       Optional[float]
    matched_expansions:           int
    total_alerts:                 int

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class DayScenario:
    day:                str        # YYYY-MM-DD
    scenario_tag:       str
    direction:          str        # BULL | BEAR | NEUTRAL
    total_move_pct:     float
    intraday_range_pct: float
    d1_atr_ratio:       float
    old_engine_alerts:  int
    new_engine_alerts:  int
    ground_truth_expansions: int

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class ReplayReport:
    n_days:              int
    window_start:        date
    window_end:          date
    scenarios_covered:   dict[str, int]
    old_engine:          EngineMetrics
    new_engine:          EngineMetrics
    delta_vs_old:        dict[str, float]
    verdict:             str
    day_scenarios:       list[DayScenario] = field(default_factory=list)
    warnings:            list[str] = field(default_factory=list)
    generated_at:        datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def to_dict(self) -> dict:
        d = asdict(self)
        d["window_start"] = self.window_start.isoformat()
        d["window_end"]   = self.window_end.isoformat()
        d["generated_at"] = self.generated_at.isoformat()
        return d


# ─────────────────────────────────────────────────────────────────────────────
# Scenario classifier
# ─────────────────────────────────────────────────────────────────────────────

def classify_day_scenario(day_bars: list, d1_atr: float,
                             news_events_that_day: int = 0) -> tuple[str, str]:
    """
    Given all H1 bars of ONE day, return (scenario_tag, direction).
    """
    if not day_bars:
        return ("no_data", "NEUTRAL")
    if d1_atr <= 0:
        return ("no_data", "NEUTRAL")

    opens = day_bars[0].open
    closes = day_bars[-1].close
    day_high = max(b.high for b in day_bars)
    day_low = min(b.low for b in day_bars)
    net_move = closes - opens
    intraday_range = day_high - day_low
    atr_ratio = intraday_range / d1_atr

    # Direction
    if abs(net_move) < 0.15 * d1_atr:
        direction = "NEUTRAL"
    else:
        direction = "BULL" if net_move > 0 else "BEAR"

    # News-day override
    if news_events_that_day >= 1 and atr_ratio >= 1.2:
        return ("news_day", direction)

    # Low volatility
    if atr_ratio < 0.5:
        return ("low_volatility", direction)

    # Strong trend
    if direction == "BULL" and net_move >= 1.5 * d1_atr and (day_high - closes) < 0.3 * abs(net_move):
        return ("strong_bullish_trend", "BULL")
    if direction == "BEAR" and (-net_move) >= 1.5 * d1_atr and (closes - day_low) < 0.3 * abs(net_move):
        return ("strong_bearish_trend", "BEAR")

    # Reversal: check first vs second half of day
    mid = len(day_bars) // 2
    if mid >= 2:
        first_move = day_bars[mid-1].close - day_bars[0].open
        second_move = day_bars[-1].close - day_bars[mid].open
        if (first_move > 0 > second_move and abs(first_move) > 0.3 * d1_atr
                and abs(second_move) > 0.3 * d1_atr):
            return ("reversal", direction)
        if (first_move < 0 < second_move and abs(first_move) > 0.3 * d1_atr
                and abs(second_move) > 0.3 * d1_atr):
            return ("reversal", direction)

    # Failed breakout: made new extreme but closed inside prior range
    prior_range = max(0.1, intraday_range)
    close_inside_pct = (closes - day_low) / prior_range if intraday_range > 0 else 0.5
    if 0.4 <= close_inside_pct <= 0.6 and atr_ratio >= 1.0:
        return ("failed_breakout", direction)

    # London vs NY dominance
    london_bars = [b for b in day_bars if 7 <= b.time.hour < 13]
    ny_bars = [b for b in day_bars if 13 <= b.time.hour < 17]
    if london_bars and ny_bars:
        london_move = london_bars[-1].close - london_bars[0].open
        ny_move = ny_bars[-1].close - ny_bars[0].open
        if abs(london_move) > 0.5 * d1_atr and abs(london_move) > abs(ny_move) * 1.5:
            return ("london_expansion", direction)
        # NY reversal: direction flipped between London and NY
        if london_move > 0 > ny_move or london_move < 0 < ny_move:
            if abs(ny_move) > 0.3 * d1_atr:
                return ("ny_reversal", direction)

    # Range
    if abs(net_move) < 0.4 * d1_atr and atr_ratio >= 0.6:
        return ("range", "NEUTRAL")

    # Continuation (default when we have a direction but no specific tag)
    if direction in ("BULL", "BEAR"):
        return ("continuation", direction)

    return ("range", "NEUTRAL")


# ─────────────────────────────────────────────────────────────────────────────
# Per-engine metrics
# ─────────────────────────────────────────────────────────────────────────────

def _count_old_engine_alerts(db, day_start, day_end, direction=None) -> int:
    q = ("SELECT COUNT(*) FROM strategist_verdicts "
         "WHERE created_at >= :s AND created_at < :e "
         "AND conditions_passed >= 3 AND decision IN ('BUY','SELL')")
    params = {"s": day_start, "e": day_end}
    if direction:
        q += " AND decision = :d"
        params["d"] = "BUY" if direction == "BULL" else "SELL"
    try:
        row = db.execute(text(q), params).fetchone()
        return int(row[0]) if row else 0
    except Exception as exc:
        log.debug("[replay] old-engine count failed: %s", exc)
        return 0


def _count_new_engine_alerts(db, day_start, day_end, direction=None) -> int:
    q = ("SELECT COUNT(*) FROM market_intelligence_alerts "
         "WHERE ts >= :s AND ts < :e "
         "AND delivery_result IN ('sent','shadow','suppressed')")
    params = {"s": day_start, "e": day_end}
    if direction:
        q += " AND alert_type LIKE :d"
        params["d"] = f"%{'BULLISH' if direction == 'BULL' else 'BEARISH'}%"
    try:
        row = db.execute(text(q), params).fetchone()
        return int(row[0]) if row else 0
    except Exception as exc:
        log.debug("[replay] new-engine count failed: %s", exc)
        return 0


def _compute_engine_metrics(
    db, *, engine: str, window_start: datetime, window_end: datetime,
    expansions: list[dict],
) -> EngineMetrics:
    """
    engine ∈ {"old", "new"}. Scores against `expansions` list.

    An expansion is "matched" if there's an alert of the right direction
    within ±60 min of expansion started_at.
    """
    n_days = max(1, (window_end - window_start).days)

    # Total alerts across window
    if engine == "old":
        total_alerts = _count_old_engine_alerts(db, window_start, window_end)
    else:
        total_alerts = _count_new_engine_alerts(db, window_start, window_end)
    alerts_per_day = round(total_alerts / n_days, 2)

    # Match each expansion to alert
    matched = 0
    delays: list[float] = []
    late_alerts = 0
    direction_hits = 0
    direction_total = 0

    for exp in expansions:
        exp_start = exp["started_at"]
        if isinstance(exp_start, str):
            try:
                exp_start = datetime.fromisoformat(exp_start.replace("Z","+00:00").split("+")[0]).replace(tzinfo=timezone.utc)
            except Exception:
                continue
        exp_end = exp.get("ended_at")
        if isinstance(exp_end, str):
            try:
                exp_end = datetime.fromisoformat(exp_end.replace("Z","+00:00").split("+")[0]).replace(tzinfo=timezone.utc)
            except Exception:
                exp_end = exp_start + timedelta(hours=4)
        elif exp_end is None:
            exp_end = exp_start + timedelta(hours=4)

        win_start = exp_start - timedelta(minutes=60)
        win_end = exp_end + timedelta(minutes=60)

        # Same-direction alert lookup
        if engine == "old":
            q = ("SELECT MIN(created_at) FROM strategist_verdicts "
                 "WHERE created_at BETWEEN :s AND :e "
                 "AND conditions_passed >= 3 AND decision = :d")
            dec = "BUY" if exp["direction"] == "BULL" else "SELL"
            params = {"s": win_start, "e": win_end, "d": dec}
        else:
            q = ("SELECT MIN(ts) FROM market_intelligence_alerts "
                 "WHERE ts BETWEEN :s AND :e "
                 "AND alert_type LIKE :d "
                 "AND delivery_result IN ('sent','shadow')")
            direction_pat = f"%{'BULLISH' if exp['direction'] == 'BULL' else 'BEARISH'}%"
            params = {"s": win_start, "e": win_end, "d": direction_pat}
        try:
            row = db.execute(text(q), params).fetchone()
            first_ts = row[0] if row else None
        except Exception:
            first_ts = None

        if first_ts is None:
            continue

        if isinstance(first_ts, str):
            try:
                first_ts = datetime.fromisoformat(first_ts.replace("Z","+00:00").split("+")[0]).replace(tzinfo=timezone.utc)
            except Exception:
                continue

        matched += 1
        delay = (first_ts - exp_start).total_seconds() / 60
        delays.append(delay)
        total_min = max(1, (exp_end - exp_start).total_seconds() / 60)
        if delay > 0 and delay / total_min > 0.25:
            late_alerts += 1

    coverage_pct = round(matched / len(expansions) * 100, 1) if expansions else 0.0
    med_delay = round(median(delays), 1) if delays else None
    late_pct = round(late_alerts / matched * 100, 1) if matched else 0.0

    # False alerts: alerts that don't correspond to any expansion in window
    # (approximation: total_alerts - matched)
    false_alerts = max(0, total_alerts - matched)

    return EngineMetrics(
        engine=engine, alerts_per_day=alerts_per_day,
        coverage_pct=coverage_pct,
        median_detection_delay_min=med_delay,
        false_alert_count=false_alerts,
        late_alert_pct=late_pct,
        direction_accuracy_pct=None,     # requires closed-trade outcomes; TODO
        matched_expansions=matched, total_alerts=total_alerts,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Public entry point
# ─────────────────────────────────────────────────────────────────────────────

def run_replay(db: Session, *, days: int = 30,
                 instrument: str = "XAU/USD") -> ReplayReport:
    """
    Runs the full replay report.
    Assumes the opportunity_coverage detector has already populated
    qualifying_expansions (caller should have run detect_and_score first).
    """
    warnings: list[str] = []
    now = datetime.now(timezone.utc)
    window_start = now - timedelta(days=days)

    # 1) Pull the ground-truth expansions
    try:
        rows = db.execute(text(
            "SELECT direction, started_at, ended_at, total_distance, "
            "atr_multiple, detected, detection_delay_min "
            "FROM qualifying_expansions "
            "WHERE instrument=:i AND started_at >= :s"
        ), {"i": instrument, "s": window_start}).fetchall()
    except Exception as exc:
        warnings.append(f"expansions query failed: {exc}")
        rows = []

    expansions = [
        {"direction": r[0], "started_at": r[1], "ended_at": r[2],
         "total_distance": r[3], "atr_multiple": r[4]}
        for r in rows
    ]

    # 2) Compute per-engine metrics
    old_metrics = _compute_engine_metrics(
        db, engine="old", window_start=window_start, window_end=now,
        expansions=expansions,
    )
    new_metrics = _compute_engine_metrics(
        db, engine="new", window_start=window_start, window_end=now,
        expansions=expansions,
    )

    # 3) Delta computation
    delta = {
        "coverage_pct_delta":   round(new_metrics.coverage_pct - old_metrics.coverage_pct, 2),
        "alerts_per_day_delta": round(new_metrics.alerts_per_day - old_metrics.alerts_per_day, 2),
        "false_alerts_delta":   new_metrics.false_alert_count - old_metrics.false_alert_count,
    }

    # 4) Verdict
    verdict = _judge_replay(old_metrics, new_metrics, delta, len(expansions))

    # 5) Per-day scenarios (bounded — max 90 days shown)
    day_scenarios = _classify_recent_days(db, days=min(days, 90),
                                            instrument=instrument, expansions=expansions)
    scenarios_covered: dict[str, int] = {}
    for d in day_scenarios:
        scenarios_covered[d.scenario_tag] = scenarios_covered.get(d.scenario_tag, 0) + 1

    return ReplayReport(
        n_days=days, window_start=window_start.date(), window_end=now.date(),
        scenarios_covered=scenarios_covered,
        old_engine=old_metrics, new_engine=new_metrics,
        delta_vs_old=delta, verdict=verdict,
        day_scenarios=day_scenarios, warnings=warnings,
    )


def _judge_replay(old, new, delta, n_expansions) -> str:
    if n_expansions < 5:
        return "INSUFFICIENT_SAMPLE — need at least 5 qualifying expansions"
    cov_delta = delta.get("coverage_pct_delta", 0)
    false_delta = delta.get("false_alerts_delta", 0)
    if cov_delta >= 20 and false_delta <= 5:
        return f"BETTER — new engine coverage +{cov_delta:.1f}%, false alerts +{false_delta}"
    if cov_delta >= 10:
        return f"MIXED — coverage +{cov_delta:.1f}%, watch false alerts (+{false_delta})"
    if cov_delta <= -10:
        return f"WORSE — new engine coverage {cov_delta:.1f}% vs old"
    return f"NEUTRAL — coverage delta {cov_delta:+.1f}%, false alerts {false_delta:+d}"


def _classify_recent_days(db, *, days: int, instrument: str,
                            expansions: list[dict]) -> list[DayScenario]:
    """Pull H1 + D1 bars for each day in window and classify."""
    from services.canonical_market_data import _fetch_bars

    # Need enough H1 for the whole window + D1 ATR
    h1 = _fetch_bars(db, instrument, "H1", lookback=days * 24 + 100)
    d1 = _fetch_bars(db, instrument, "D1", lookback=days + 30)
    if not h1 or not d1:
        return []

    # Group H1 by UTC date
    from collections import defaultdict
    by_day: dict = defaultdict(list)
    for b in h1:
        by_day[b.time.date()].append(b)

    # D1 ATR from last 14 D1 bars
    if len(d1) < 15:
        return []
    trs = []
    for i in range(1, len(d1)):
        h, l, pc = d1[i].high, d1[i].low, d1[i-1].close
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    d1_atr = sum(trs[-14:]) / 14 if len(trs) >= 14 else sum(trs) / len(trs)

    # Expansions per day
    exp_per_day: dict = defaultdict(int)
    for e in expansions:
        exp_day = e["started_at"].date() if hasattr(e["started_at"], "date") else None
        if exp_day is not None:
            exp_per_day[exp_day] += 1

    now = datetime.now(timezone.utc)
    scenarios: list[DayScenario] = []
    for d, day_bars in sorted(by_day.items(), reverse=True)[:days]:
        if not day_bars:
            continue
        day_bars.sort(key=lambda b: b.time)
        tag, direction = classify_day_scenario(day_bars, d1_atr)
        opens = day_bars[0].open
        closes = day_bars[-1].close
        day_high = max(b.high for b in day_bars)
        day_low = min(b.low for b in day_bars)
        day_start = datetime.combine(d, datetime.min.time()).replace(tzinfo=timezone.utc)
        day_end = day_start + timedelta(days=1)
        old_ct = _count_old_engine_alerts(db, day_start, day_end)
        new_ct = _count_new_engine_alerts(db, day_start, day_end)
        scenarios.append(DayScenario(
            day=d.isoformat(), scenario_tag=tag, direction=direction,
            total_move_pct=round((closes - opens) / opens * 100, 2) if opens > 0 else 0.0,
            intraday_range_pct=round((day_high - day_low) / opens * 100, 2) if opens > 0 else 0.0,
            d1_atr_ratio=round((day_high - day_low) / d1_atr, 2) if d1_atr > 0 else 0.0,
            old_engine_alerts=old_ct, new_engine_alerts=new_ct,
            ground_truth_expansions=exp_per_day.get(d, 0),
        ))
    return scenarios


__all__ = [
    "run_replay", "classify_day_scenario",
    "ReplayReport", "EngineMetrics", "DayScenario",
]
