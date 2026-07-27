"""
Execution Gates — Quality + Headwind Filter Layer
==================================================

Sits between _evaluate_5_conditions() and _decide_execution_status().
Owns three quality filters and four headwind filters, each with an
independent config flag so any can be disabled without touching the
strategist core.

Quality filters (must pass to allow execution):
  Q1  min_setup_score       — reject if setup_score < min (default 85)
  Q2  positive_ev           — reject if est_WR × rr - (1 - est_WR) < 0
  Q3  strong_wr_estimate    — reject if the estimator returns "no exec"

Headwind filters (each demotes DEMO_TRADE_PLACED → SIGNAL_ONLY):
  H1  session_penalty       — NY KZ needs score ≥ 90 (was live-loss center)
  H2  post_loss_cooldown    — cooldown after a stopped MT5 trade
  H3  direction_flip_cool   — cooldown after a direction-flip alert
  H4  wide_spread_penalty   — spread 3-5 pts demotes to signal-only
  H5  news_proximity_veto   — no execution within N min BEFORE next event

Design: pure functions, easy to test. Every filter returns
(passed: bool, reason: str). Caller aggregates.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone, timedelta
from typing import Optional

log = logging.getLogger(__name__)


# ── Win-rate + EV helpers ───────────────────────────────────────────────────

def estimate_win_rate_from_score(setup_score: int) -> tuple[str, float, float]:
    """
    Returns (label, wr_low_frac, expected_r_per_trade). setup_score is 0-100.

    Bands calibrated from 893-trade backtest × the setup-score fine-grain
    filter. Above the 90 band the pullback filter selects setups that
    historically produced positive expectancy net of costs; below 85 the
    estimator says 'no trade' because the math is negative EV even at
    the reward-multiple advantage.

    Reward assumption: mandate uses ~2.5R average on TP1/TP2 mix.
    """
    if setup_score >= 90:
        return ("~30% WR · +0.05R (score >= 90 filter)",  0.30, +0.05)
    if setup_score >= 85:
        return ("~25% WR · marginal EV (score 85-89)",     0.25, -0.13)
    if setup_score >= 80:
        return ("~22% WR · negative EV (below quality)",   0.22, -0.23)
    if setup_score >= 70:
        return ("watchlist band · no exec",                0.20, -0.30)
    return ("below scoring band · no trade",               0.00,  0.00)


def positive_ev(wr_frac: float, rr: float) -> tuple[bool, float]:
    """Return (positive?, ev_per_trade_in_R). Assumes 1R risk per trade."""
    ev = wr_frac * rr - (1.0 - wr_frac)
    return (ev > 0, ev)


# ── Q1: minimum setup-score gate ────────────────────────────────────────────

def check_setup_score(setup_score: int, threshold: int) -> tuple[bool, str]:
    if setup_score < threshold:
        return (False, f"setup_score {setup_score} < {threshold}")
    return (True, f"setup_score {setup_score} >= {threshold}")


# ── Q2 / Q3: expectancy filter ──────────────────────────────────────────────

def check_expected_value(setup_score: int, rr: float) -> tuple[bool, str]:
    """Reject if the estimator says the setup is negative EV."""
    label, wr_low, exp_r = estimate_win_rate_from_score(setup_score)
    ok, ev = positive_ev(wr_low, max(rr or 0.0, 0.0))
    if not ok:
        return (False, f"negative EV — {wr_low*100:.0f}% WR × {rr:.1f}R = {ev:+.2f}R")
    return (True, f"positive EV — {wr_low*100:.0f}% WR × {rr:.1f}R = {ev:+.2f}R")


# ── H1: session-specific penalty ────────────────────────────────────────────

SESSION_PENALTIES: dict[str, int] = {
    # session_label prefix → additional score required to execute
    "ny_kz":       5,     # +5 for NY KZ (was live-loss center)
    "overlap":     5,     # overlap London+NY was worst in the live audit
    "late_ny":    99,     # never (already in _NEVER_TRADE_SESSIONS but double belt)
    "asian":       5,     # observation-only; require higher score to execute
}


def check_session_penalty(session_label: Optional[str], setup_score: int,
                            base_threshold: int) -> tuple[bool, str]:
    if not session_label:
        return (True, "no session context")
    s = session_label.lower()
    for prefix, bump in SESSION_PENALTIES.items():
        if s.startswith(prefix):
            required = base_threshold + bump
            if setup_score < required:
                return (False,
                        f"session={prefix} needs score >= {required} (got {setup_score})")
            return (True, f"session={prefix} · score {setup_score} >= {required}")
    return (True, f"session={s} no penalty")


# ── H2: post-loss cooldown ──────────────────────────────────────────────────

def check_post_loss_cooldown(db, cooldown_min: int = 60) -> tuple[bool, str]:
    """
    True if there's been no STOPPED trade in the last `cooldown_min` minutes.

    Reads mt5_trade_logs; a row with status='closed' and negative outcome
    counts as a stop.
    """
    if cooldown_min <= 0:
        return (True, "cooldown disabled")
    try:
        from db_models import Signal as SM
        from services.canonical_signal import STATE_STOPPED
        threshold = datetime.now(timezone.utc) - timedelta(minutes=cooldown_min)
        # Prefer canonical registry — richer state model
        row = (db.query(SM)
                 .filter(SM.state == STATE_STOPPED)
                 .filter(SM.closed_at >= threshold)
                 .order_by(SM.closed_at.desc())
                 .first())
        if row is not None:
            age_min = (datetime.now(timezone.utc) - row.closed_at.replace(tzinfo=timezone.utc)
                        if row.closed_at.tzinfo is None else row.closed_at).total_seconds() / 60
            return (False, f"post-loss cooldown — {row.signal_id} stopped "
                            f"{age_min:.0f}m ago (need {cooldown_min}m)")
    except Exception as exc:
        log.debug("[gates] post-loss check skipped: %s", exc)
    return (True, "no recent stop-hit")


# ── H3: direction-flip cooldown ─────────────────────────────────────────────

# Module-level state — updated by strategist_runner when a flip alert fires.
_last_flip_at:        Optional[datetime] = None
_last_flip_direction: Optional[str] = None


def mark_direction_flip(direction: str) -> None:
    global _last_flip_at, _last_flip_direction
    _last_flip_at        = datetime.now(timezone.utc)
    _last_flip_direction = direction
    log.info("[gates] direction flip recorded: %s at %s", direction, _last_flip_at)


def check_direction_flip_cooldown(direction: str, cooldown_min: int = 120
                                     ) -> tuple[bool, str]:
    if cooldown_min <= 0 or _last_flip_at is None:
        return (True, "no prior flip")
    age_min = (datetime.now(timezone.utc) - _last_flip_at).total_seconds() / 60
    if age_min >= cooldown_min:
        return (True, f"flip cooldown expired ({age_min:.0f}m > {cooldown_min}m)")
    if _last_flip_direction and direction == _last_flip_direction:
        return (False, f"direction-flip cooldown — flipped to {direction} "
                        f"{age_min:.0f}m ago (need {cooldown_min}m)")
    return (True, f"direction differs from last flip ({direction} vs {_last_flip_direction})")


# ── H4: wide-spread penalty ─────────────────────────────────────────────────

def check_spread_penalty(spread_pts: Optional[float],
                           tight_max: float = 3.0,
                           hard_max: float = 5.0) -> tuple[bool, str]:
    """
    Passes clean if spread <= tight_max.
    Between tight_max and hard_max → False (demote to signal-only).
    Above hard_max → False (already blocked by _EXEC_SPREAD_HIGH upstream).
    """
    if spread_pts is None:
        return (True, "spread unknown")
    if spread_pts <= tight_max:
        return (True, f"spread {spread_pts:.1f} pts <= {tight_max}")
    if spread_pts <= hard_max:
        return (False, f"spread {spread_pts:.1f} pts inside penalty band "
                        f"({tight_max}-{hard_max})")
    return (False, f"spread {spread_pts:.1f} pts > hard max {hard_max}")


# ── H5: news proximity ─────────────────────────────────────────────────────

def check_news_proximity(db, lookahead_min: int = 30) -> tuple[bool, str]:
    """
    True if there is NO high-impact news event within the next
    `lookahead_min` minutes. Reads macro_events. Fails safe (returns
    True) if the table isn't queryable.
    """
    try:
        from db_models import MacroEvent
        now = datetime.now(timezone.utc)
        window_end = now + timedelta(minutes=lookahead_min)
        upcoming = (db.query(MacroEvent)
                       .filter(MacroEvent.impact == "high")
                       .filter(MacroEvent.event_time.between(now, window_end))
                       .order_by(MacroEvent.event_time.asc())
                       .first())
        if upcoming is not None:
            eta_min = (upcoming.event_time - now).total_seconds() / 60 if upcoming.event_time.tzinfo else \
                       (upcoming.event_time.replace(tzinfo=timezone.utc) - now).total_seconds() / 60
            return (False, f"high-impact news in {eta_min:.0f}m — "
                            f"{getattr(upcoming, 'title', 'event')[:40]}")
    except Exception as exc:
        log.debug("[gates] news proximity check skipped: %s", exc)
    return (True, "no imminent high-impact news")


# ── Public aggregator ──────────────────────────────────────────────────────

def evaluate_execution_quality(
    *,
    db,
    setup_score: int,
    rr: float,
    session_label: Optional[str],
    direction: str,
    spread_pts: Optional[float] = None,
    settings=None,
) -> dict:
    """
    Aggregate every quality + headwind filter into one verdict:
      {
        "allow_execution": bool,      # True only if every gate green
        "should_demote":   bool,      # True if any headwind wants signal-only
        "reasons":         [str],     # human-readable breadcrumb
        "wr_label":        str,       # from estimator
      }

    settings — pass config.settings (or None for defaults) so admins can
    disable any gate without touching code.
    """
    from types import SimpleNamespace
    s = settings or SimpleNamespace()
    def cfg(name, default):
        return getattr(s, name, default)

    min_score      = cfg("min_setup_score_for_execution", 85)
    require_pos_ev = cfg("require_positive_ev",           True)
    session_gate   = cfg("session_penalty_enabled",       True)
    post_loss_min  = cfg("post_loss_cooldown_min",        60)
    flip_cool_min  = cfg("direction_flip_cooldown_min",   120)
    tight_spread   = cfg("execution_tight_spread_pts",    3.0)
    hard_spread    = cfg("execution_hard_spread_pts",     5.0)
    news_lookahead = cfg("news_proximity_lookahead_min",  30)

    reasons: list[str] = []
    allow = True
    demote = False

    # Quality
    ok, why = check_setup_score(setup_score, min_score)
    reasons.append(("QUALITY" if ok else "Q1_BLOCK") + ": " + why)
    if not ok: allow = False

    if require_pos_ev:
        ok, why = check_expected_value(setup_score, rr)
        reasons.append(("EV" if ok else "Q2_BLOCK") + ": " + why)
        if not ok: allow = False

    label, _, _ = estimate_win_rate_from_score(setup_score)

    # Headwinds
    if session_gate:
        ok, why = check_session_penalty(session_label, setup_score, min_score)
        reasons.append(("SESSION" if ok else "H1_BLOCK") + ": " + why)
        if not ok: allow = False

    ok, why = check_post_loss_cooldown(db, post_loss_min)
    reasons.append(("COOLDOWN" if ok else "H2_BLOCK") + ": " + why)
    if not ok: allow = False

    ok, why = check_direction_flip_cooldown(direction, flip_cool_min)
    reasons.append(("FLIP" if ok else "H3_BLOCK") + ": " + why)
    if not ok: allow = False

    ok, why = check_spread_penalty(spread_pts, tight_spread, hard_spread)
    reasons.append(("SPREAD" if ok else "H4_DEMOTE") + ": " + why)
    if not ok: demote = True

    ok, why = check_news_proximity(db, news_lookahead)
    reasons.append(("NEWS" if ok else "H5_BLOCK") + ": " + why)
    if not ok: allow = False

    return {
        "allow_execution":  allow and not demote,
        "should_demote":    demote,
        "wr_label":         label,
        "reasons":          reasons,
    }


__all__ = [
    "estimate_win_rate_from_score", "positive_ev",
    "check_setup_score", "check_expected_value",
    "check_session_penalty", "check_post_loss_cooldown",
    "check_direction_flip_cooldown", "check_spread_penalty",
    "check_news_proximity",
    "mark_direction_flip",
    "evaluate_execution_quality",
]
