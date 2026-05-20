"""
Trading Psychology Knowledge Base
=================================

Encoded principles from the canonical books of trading psychology + risk.
The engine evaluates itself against each principle and surfaces:

  - Which principles are FULLY EMBODIED in current system behavior
  - Which are PARTIALLY honored (i.e. half-built or has caveats)
  - Which are MISSING and represent the next learning step

This turns the dashboard into an evolving study tool — the engine "knows"
which trader-development milestones it has crossed and which remain ahead.

Books referenced (in canonical order most cited by professional desks):

  - Mark Douglas — "Trading in the Zone" (2000)
  - Mark Douglas — "The Disciplined Trader" (1990)
  - Van K. Tharp — "Trade Your Way to Financial Freedom" (1998, rev. 2007)
  - Van K. Tharp — "Definitive Guide to Position Sizing" (2008)
  - Brett N. Steenbarger — "The Psychology of Trading" (2002)
  - Brett N. Steenbarger — "The Daily Trading Coach" (2009)
  - Jack D. Schwager — "Market Wizards" series (1989, 1992, 2001, 2012)
  - Curtis M. Faith — "Way of the Turtle" (2007)
  - Alexander Elder — "Trading for a Living" (1993)
  - John J. Murphy — "Technical Analysis of the Financial Markets" (1999)
  - Daniel Kahneman — "Thinking, Fast and Slow" (2011)
  - Nassim N. Taleb — "Fooled by Randomness" (2001)
  - Nassim N. Taleb — "The Black Swan" (2007)
  - Michael W. Covel — "Trend Following" (2004)
  - Richard L. Peterson — "Inside the Investor's Brain" (2007)

Each principle below has:
  - id, name, book, author
  - text  : the verbatim or paraphrased principle
  - engine_implication : what an algorithmic trading system must do to honor it
  - check_fn : returns "FULL" | "PARTIAL" | "MISSING" based on system state
"""
from __future__ import annotations

# ──────────────────────────────────────────────────────────────────────────────
# Principle catalogue
# ──────────────────────────────────────────────────────────────────────────────
# Order matters — first ten are foundational; later ones are advanced.

PRINCIPLES: list[dict] = [

    # ── FOUNDATION (Douglas, Tharp) ─────────────────────────────────────────
    {
        "id": "probabilistic_thinking",
        "name": "Think In Probabilities, Not Predictions",
        "book": "Trading in the Zone", "author": "Mark Douglas",
        "category": "mindset",
        "text": (
            "Anything can happen. You don't need to know what is going to happen "
            "next to make money. There's a random distribution between wins and "
            "losses for any given set of variables that defines an edge. Edge is "
            "nothing more than an indication of a higher probability of one thing "
            "happening over another."
        ),
        "engine_implication": (
            "System must size positions based on EDGE, not certainty. Every signal "
            "carries explicit probability + R-multiple, never 'this WILL win.'"
        ),
        "check_key": "probabilistic_sizing",
    },
    {
        "id": "edge_must_be_measured",
        "name": "An Edge Must Be Defined And Measured",
        "book": "Trade Your Way to Financial Freedom", "author": "Van K. Tharp",
        "category": "edge",
        "text": (
            "An edge is a statistical advantage based upon market behavior that "
            "is likely to continue. Without a measured edge, you do not have a "
            "trading system — you have hope."
        ),
        "engine_implication": (
            "Backtester must produce {win_rate, expectancyR, profitFactor} with "
            "n>=30 trades on real data before any setup is approved for live trading."
        ),
        "check_key": "measured_edge",
    },
    {
        "id": "r_multiples",
        "name": "Think In R-Multiples, Not Dollar Amounts",
        "book": "Trade Your Way to Financial Freedom", "author": "Van K. Tharp",
        "category": "risk",
        "text": (
            "Define your initial risk on every trade as 1R. Express all subsequent "
            "results in multiples of R. A 3R winner means you made 3x what you risked. "
            "This removes account size as a variable and makes performance comparable."
        ),
        "engine_implication": (
            "Every trade is logged with r_multiple field. Equity curve uses R-multiples "
            "not dollars. Position sizing computes the 1R distance from entry to SL."
        ),
        "check_key": "r_multiple_tracking",
    },
    {
        "id": "position_sizing_first",
        "name": "Position Sizing Is The Single Most Important Element",
        "book": "Definitive Guide to Position Sizing", "author": "Van K. Tharp",
        "category": "risk",
        "text": (
            "Position sizing — answering 'how much?' — has a bigger impact on your "
            "results than entries, exits, or even edge selection. It is the only "
            "variable you can change that directly controls drawdown and ruin."
        ),
        "engine_implication": (
            "Fixed-fractional risk per trade (e.g. 0.25-1% of equity). Lot size "
            "computed from (equity * risk_pct) / (SL_distance * pip_value), then "
            "hard-capped by max_lot."
        ),
        "check_key": "fixed_fractional_sizing",
    },
    {
        "id": "cut_losses_short",
        "name": "Cut Your Losses Short, Let Winners Run",
        "book": "Trading for a Living", "author": "Alexander Elder",
        "category": "execution",
        "text": (
            "The cardinal rule. Most traders do the opposite — they take small "
            "gains quickly out of fear and let losses run out of hope. A hard "
            "stop loss honored without negotiation is the only protection."
        ),
        "engine_implication": (
            "Every trade has a STRUCTURAL stop loss that cannot be widened. "
            "Take-profit at 2R+ to ensure winners ARE bigger than losers. "
            "No martingale or averaging-down logic anywhere."
        ),
        "check_key": "hard_stops_no_avg_down",
    },
    {
        "id": "rule_based_execution",
        "name": "Mechanical Rules Eliminate Emotional Override",
        "book": "Way of the Turtle", "author": "Curtis M. Faith",
        "category": "discipline",
        "text": (
            "The Turtles were taught: 'If you can write down your rules and follow "
            "them exactly, the rules don't have to be perfect — only consistent.' "
            "Most traders fail because they cannot execute even a great system."
        ),
        "engine_implication": (
            "System auto-executes on rule satisfaction, removing the human from "
            "decision moment. Manual overrides require explicit log + reason. "
            "Kill switch exists for emergencies but is non-default."
        ),
        "check_key": "auto_execution_with_caps",
    },

    # ── MARKET REALITY (Schwager, Murphy) ───────────────────────────────────
    {
        "id": "trade_with_trend",
        "name": "Trade In The Direction Of The Major Trend",
        "book": "Technical Analysis of the Financial Markets", "author": "John J. Murphy",
        "category": "structure",
        "text": (
            "The trend is your friend. Identify the dominant trend on a higher "
            "timeframe and only take setups on lower timeframes that align with it. "
            "Counter-trend trades are lower-probability and require tighter risk."
        ),
        "engine_implication": (
            "HTF trend filter (H4/D1 EMA alignment) is a gate, not advisory. "
            "Counter-trend signals receive a score penalty or are blocked entirely."
        ),
        "check_key": "htf_trend_filter",
    },
    {
        "id": "no_certainty",
        "name": "Markets Are Inherently Uncertain — Plan For Random Sequences",
        "book": "Trading in the Zone", "author": "Mark Douglas",
        "category": "mindset",
        "text": (
            "Even a 70% win-rate system will produce streaks of 5+ losses by chance. "
            "Your job is not to predict the next outcome but to follow the rules "
            "across the entire distribution. Variance is feature, not bug."
        ),
        "engine_implication": (
            "Walk-forward / Monte Carlo backtests included. Drawdown sized to "
            "survive a 10-loss streak at the system's losing-streak quantile. "
            "Daily-loss circuit breaker prevents tilt."
        ),
        "check_key": "drawdown_survival_check",
    },
    {
        "id": "expectancy_over_winrate",
        "name": "Optimise For Expectancy, Not Win Rate",
        "book": "Trade Your Way to Financial Freedom", "author": "Van K. Tharp",
        "category": "edge",
        "text": (
            "A 30% win rate at 4R can outperform a 60% win rate at 1R. Expectancy "
            "(avg_win * win_rate - avg_loss * loss_rate) is the metric that matters. "
            "Selling high win-rate, low-R systems is the most common trap."
        ),
        "engine_implication": (
            "Backtest reports lead with expectancyR, not winRate. Engine comparison "
            "ranks by expectancy. Minimum 1:2.5 RR is gate, not advisory."
        ),
        "check_key": "expectancy_first_metric",
    },

    # ── BIAS & FAILURE MODES (Kahneman, Taleb, Steenbarger) ────────────────
    {
        "id": "recency_bias_immunity",
        "name": "Beware Recency Bias",
        "book": "Thinking, Fast and Slow", "author": "Daniel Kahneman",
        "category": "cognitive",
        "text": (
            "Humans overweight what happened most recently. Three losses in a row "
            "feel like the system 'isn't working.' Statistical thinking requires "
            "judging the full distribution, not the tail you just lived through."
        ),
        "engine_implication": (
            "Sample-size gates: do not adjust system weights based on <30 trades. "
            "Surface n in every stat. Reject 'curve-fitting on the last 5 trades'."
        ),
        "check_key": "sample_size_gates",
    },
    {
        "id": "survivorship_bias",
        "name": "Survivorship Bias — Most 'Pro' Stories Are Lucky",
        "book": "Fooled by Randomness", "author": "Nassim N. Taleb",
        "category": "cognitive",
        "text": (
            "The traders you see on Twitter survived. The thousands who blew up are "
            "silent. Do not copy a setup because it 'works for' someone — you have "
            "no idea how many silently failed with the same setup."
        ),
        "engine_implication": (
            "Backtest on real data only — never synthetic. Tag data_source on every "
            "result. Refuse to draw conclusions from cherry-picked windows."
        ),
        "check_key": "real_data_only",
    },
    {
        "id": "black_swan_protection",
        "name": "Survive The Black Swan",
        "book": "The Black Swan", "author": "Nassim N. Taleb",
        "category": "risk",
        "text": (
            "Rare, high-impact events occur. Survival requires capping per-trade and "
            "per-day risk so that no single event ends the account. Stops can slip; "
            "expect 2-3x normal loss on flash events."
        ),
        "engine_implication": (
            "Daily loss limit, max open positions, max lot per trade. News blackout "
            "filters around tier-1 events. Spread gate refuses trades when liquidity "
            "is abnormal."
        ),
        "check_key": "circuit_breakers_present",
    },
    {
        "id": "post_trade_review",
        "name": "Every Trade Becomes A Lesson — Or A Repeated Mistake",
        "book": "The Daily Trading Coach", "author": "Brett N. Steenbarger",
        "category": "learning",
        "text": (
            "Without structured review, traders repeat the same errors for years. "
            "Each trade must be logged with: entry reason, exit reason, mistake "
            "category, and whether outcome confirmed or contradicted the setup."
        ),
        "engine_implication": (
            "Paper observation records: signal, entry/SL/TP, score, session, "
            "setup_type, outcome, r_multiple. Aggregations by setup_type/session "
            "drive future weight adjustments."
        ),
        "check_key": "comprehensive_journaling",
    },
    {
        "id": "patience_over_action",
        "name": "Best Trades Are Rare — Patience Beats Action",
        "book": "Market Wizards", "author": "Jack D. Schwager",
        "category": "discipline",
        "text": (
            "Top traders interviewed across 30 years agree: more money is lost from "
            "force-trading marginal setups than from missing opportunities. The "
            "default action is INACTION. 'I get paid to wait,' said Druckenmiller."
        ),
        "engine_implication": (
            "Default decision is STAND ASIDE. Setup score, RR, killzone posture, "
            "and confluence layers must ALL pass before firing. System trades 1-3 "
            "times per day max, not 10-20."
        ),
        "check_key": "stand_aside_default",
    },

    # ── ADVANCED (Steenbarger, Covel, Peterson) ────────────────────────────
    {
        "id": "market_regime_awareness",
        "name": "Know Which Market Regime You Are In",
        "book": "The Psychology of Trading", "author": "Brett N. Steenbarger",
        "category": "structure",
        "text": (
            "A trend-following system loses in ranges; a mean-reversion system "
            "loses in trends. The same setup with different regime context gives "
            "different results. The first question is always: what is the market "
            "doing right now?"
        ),
        "engine_implication": (
            "Intermarket correlation engine detects regime shifts (e.g. gold + DXY "
            "both rising = safe-haven flight, not normal). Killzone analyzer scores "
            "edge per session."
        ),
        "check_key": "regime_detection",
    },
    {
        "id": "follow_the_trend",
        "name": "Trend Following Edge Comes From Letting Winners Run Far",
        "book": "Trend Following", "author": "Michael W. Covel",
        "category": "edge",
        "text": (
            "Trend systems have win rates as low as 30-40% but make money because "
            "the winners are 5-10x the size of losers. Most retail traders cannot "
            "psychologically hold a winning trade through normal pullbacks."
        ),
        "engine_implication": (
            "TP1/TP2/TP3 + runner structure. Trailing stops based on structure not "
            "fixed points. Breakeven move only after meaningful progress."
        ),
        "check_key": "multi_tp_with_runner",
    },
    {
        "id": "loss_aversion_awareness",
        "name": "Loss Aversion Distorts Decision-Making",
        "book": "Thinking, Fast and Slow", "author": "Daniel Kahneman",
        "category": "cognitive",
        "text": (
            "Humans feel losses ~2x more strongly than gains. This causes premature "
            "profit-taking and stop-loss procrastination. Mechanical execution "
            "removes this asymmetry."
        ),
        "engine_implication": (
            "Auto-executor places SL at order_send time, not after — no chance to "
            "second-guess. Take-profit ladder fires automatically, no manual exit."
        ),
        "check_key": "auto_sl_on_entry",
    },
    {
        "id": "scenario_planning",
        "name": "Plan Every Trade Before Entering — Including The Exit",
        "book": "Market Wizards", "author": "Jack D. Schwager",
        "category": "discipline",
        "text": (
            "'I am always thinking about losing money as opposed to making money.' "
            "— Paul Tudor Jones. Before entering, define: entry, SL, TP1/TP2/TP3, "
            "what invalidates the thesis, what would make you exit early."
        ),
        "engine_implication": (
            "Every signal record contains: entry, stopLoss, takeProfit, rr, "
            "invalidation, and the engineModel evidence. No 'figure it out later.'"
        ),
        "check_key": "complete_trade_plan",
    },
    {
        "id": "emotion_neutrality",
        "name": "Trade The System, Not Your Feelings",
        "book": "Inside the Investor's Brain", "author": "Richard L. Peterson",
        "category": "mindset",
        "text": (
            "Greed after wins and fear after losses cause sizing errors and rule "
            "violations. The only antidote: pre-commit to rules and remove the "
            "discretion that emotion can hijack."
        ),
        "engine_implication": (
            "Fixed-fractional sizing irrespective of recent results. Cooldown "
            "between trades. Daily-loss circuit breaker. No 'one more revenge "
            "trade' path in code."
        ),
        "check_key": "no_discretionary_sizing",
    },
]


# ──────────────────────────────────────────────────────────────────────────────
# Check functions — evaluate the current engine against each principle
# ──────────────────────────────────────────────────────────────────────────────

def _check_probabilistic_sizing(ctx: dict) -> str:
    # Probabilistic if every trade has explicit RR + score
    return "FULL" if ctx.get("has_rr") and ctx.get("has_scores") else "PARTIAL"

def _check_measured_edge(ctx: dict) -> str:
    n = ctx.get("resolved_observations", 0)
    exp = ctx.get("expectancy_r", 0)
    if n >= 30 and exp > 0.1: return "FULL"
    if n >= 30:               return "PARTIAL"   # measured but negative
    if n >= 10:               return "PARTIAL"
    return "MISSING"

def _check_r_multiple_tracking(ctx: dict) -> str:
    return "FULL" if ctx.get("r_multiples_logged") else "MISSING"

def _check_fixed_fractional(ctx: dict) -> str:
    if ctx.get("fixed_fractional") and ctx.get("max_lot_cap"):
        return "FULL"
    if ctx.get("fixed_fractional"):
        return "PARTIAL"
    return "MISSING"

def _check_hard_stops(ctx: dict) -> str:
    if ctx.get("sl_required") and not ctx.get("avg_down_anywhere"):
        return "FULL"
    return "PARTIAL"

def _check_auto_exec(ctx: dict) -> str:
    if ctx.get("auto_execution") and ctx.get("max_trades_per_day"):
        return "FULL"
    if ctx.get("auto_execution"):
        return "PARTIAL"
    return "MISSING"

def _check_htf_trend(ctx: dict) -> str:
    return "FULL" if ctx.get("htf_trend_filter_enabled") else "PARTIAL"

def _check_drawdown_survival(ctx: dict) -> str:
    n = ctx.get("resolved_observations", 0)
    dd = ctx.get("max_dd_pct", 0)
    if n >= 30 and dd < 10: return "FULL"
    if n >= 10 and dd < 15: return "PARTIAL"
    return "MISSING"

def _check_expectancy_first(ctx: dict) -> str:
    return "FULL" if ctx.get("min_rr_gate") and ctx.get("min_rr_gate") >= 2.0 else "PARTIAL"

def _check_sample_size_gates(ctx: dict) -> str:
    return "FULL" if ctx.get("sample_size_in_metrics") else "PARTIAL"

def _check_real_data_only(ctx: dict) -> str:
    if ctx.get("synthetic_data_count", 0) == 0 and ctx.get("data_source") == "real":
        return "FULL"
    return "PARTIAL"

def _check_circuit_breakers(ctx: dict) -> str:
    if ctx.get("daily_loss_limit") and ctx.get("news_blackout") and ctx.get("max_open_trades"):
        return "FULL"
    return "PARTIAL"

def _check_comprehensive_journal(ctx: dict) -> str:
    if ctx.get("observation_count", 0) >= 100: return "FULL"
    if ctx.get("observation_count", 0) >= 30:  return "PARTIAL"
    return "MISSING"

def _check_stand_aside_default(ctx: dict) -> str:
    if ctx.get("multi_layer_confirmation") and ctx.get("max_trades_per_day", 99) <= 5:
        return "FULL"
    return "PARTIAL"

def _check_regime_detection(ctx: dict) -> str:
    if ctx.get("correlation_engine") and ctx.get("killzone_analyzer"):
        return "FULL"
    if ctx.get("killzone_analyzer"):
        return "PARTIAL"
    return "MISSING"

def _check_multi_tp(ctx: dict) -> str:
    return "PARTIAL"   # We have single TP currently; runner logic not yet impl

def _check_auto_sl_on_entry(ctx: dict) -> str:
    return "FULL" if ctx.get("sl_in_order_send") else "PARTIAL"

def _check_complete_trade_plan(ctx: dict) -> str:
    return "FULL" if ctx.get("plan_completeness") else "PARTIAL"

def _check_no_discretionary_sizing(ctx: dict) -> str:
    if ctx.get("fixed_fractional") and not ctx.get("discretionary_overrides"):
        return "FULL"
    return "PARTIAL"


CHECKS: dict[str, callable] = {
    "probabilistic_sizing":     _check_probabilistic_sizing,
    "measured_edge":            _check_measured_edge,
    "r_multiple_tracking":      _check_r_multiple_tracking,
    "fixed_fractional_sizing":  _check_fixed_fractional,
    "hard_stops_no_avg_down":   _check_hard_stops,
    "auto_execution_with_caps": _check_auto_exec,
    "htf_trend_filter":         _check_htf_trend,
    "drawdown_survival_check":  _check_drawdown_survival,
    "expectancy_first_metric":  _check_expectancy_first,
    "sample_size_gates":        _check_sample_size_gates,
    "real_data_only":           _check_real_data_only,
    "circuit_breakers_present": _check_circuit_breakers,
    "comprehensive_journaling": _check_comprehensive_journal,
    "stand_aside_default":      _check_stand_aside_default,
    "regime_detection":         _check_regime_detection,
    "multi_tp_with_runner":     _check_multi_tp,
    "auto_sl_on_entry":         _check_auto_sl_on_entry,
    "complete_trade_plan":      _check_complete_trade_plan,
    "no_discretionary_sizing":  _check_no_discretionary_sizing,
}


def evaluate_principles(ctx: dict) -> list[dict]:
    """
    Given a `ctx` (snapshot of current system state), return each principle
    with its evaluation: FULL / PARTIAL / MISSING.
    """
    out = []
    for p in PRINCIPLES:
        check_key = p.get("check_key", "")
        fn = CHECKS.get(check_key)
        status = fn(ctx) if fn else "PARTIAL"
        out.append({**p, "status": status})
    return out
