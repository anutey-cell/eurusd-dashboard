from __future__ import annotations

from dataclasses import dataclass

from services.opportunity_arbiter import (
    build_arbiter_context,
    get_latest_arbiter_context,
    publish_arbiter_context,
    reset_arbiter_context,
)


@dataclass
class Obj:
    pass


class Breakout:
    def __init__(self, classification, direction="UP", confidence=70,
                 level_name="PDH", level=4350.0):
        self.classification = classification
        self.direction = direction
        self.confidence = confidence
        self.level_name = level_name
        self.level = level
        self.followthrough_bars = 0
        self.time_outside_min = 0
        self.returned_to_range = classification in ("FAILED_BREAKOUT", "BREAKOUT_INVALIDATED")
        self.distance_from_level_atr = 0.2


class Verdict:
    directional_assessment = "Bullish"
    opportunity_status = "Direction developing"
    entry_status = "No compliant entry"
    confidence = 72


class Transition:
    new_state = "BULLISH_TRANSITION"


class HTF:
    direction = "BULL"
    strength = "MEDIUM"
    score = 58


class Regime:
    regime = "BULLISH_TRANSITION"
    directional_bias = "BULL"
    invalidation_price = 4300.0


class Evidence:
    dominant_direction = "BULL"
    bull_evidence_score = 62
    bear_evidence_score = 18
    directional_confidence = 72
    entry_quality_confidence = 55
    extension_risk_score = 20
    contradiction_score = 10
    contradictions = []


def setup_function():
    reset_arbiter_context()


def teardown_function():
    reset_arbiter_context()


def test_liquidity_probe_is_candidate_not_claimed_intent():
    ctx = build_arbiter_context(
        htf_alignment=HTF(), regime=Regime(), evidence=Evidence(),
        breakouts=[Breakout("LIQUIDITY_PROBE", direction="UP", confidence=80)],
        state_transition=Transition(), separated_verdict=Verdict(),
        cme={"status": "OBSERVED", "directional_bias": "UNSIGNED_NEUTRAL"},
        actionability={"actionable": True, "status": "ACTIONABLE", "reason": "ok"},
    )

    assert ctx["structure"]["state"] == "LIQUIDITY_PROBE"
    assert ctx["structure"]["manipulation_candidate"] is True
    assert ctx["structure"]["liquidity_side"] == "BUY_SIDE_SWEEP_CANDIDATE"
    assert "intent is not inferred" in ctx["structure"]["reason"]
    assert ctx["cme"]["is_signal_gate"] is False
    assert ctx["policy"]["arbiter_is_execution_authority"] is False


def test_failed_breakout_outranks_developing_breakout():
    ctx = build_arbiter_context(
        breakouts=[
            Breakout("BREAKOUT_DEVELOPING", direction="UP", confidence=90),
            Breakout("FAILED_BREAKOUT", direction="DOWN", confidence=65, level_name="PDL"),
        ],
        state_transition=Transition(), separated_verdict=Verdict(),
        actionability={"actionable": True, "status": "ACTIONABLE", "reason": "ok"},
    )
    assert ctx["structure"]["state"] == "FAILED_BREAKOUT"
    assert ctx["structure"]["direction"] == "DOWN"
    assert ctx["structure"]["liquidity_side"] == "SELL_SIDE_SWEEP_CANDIDATE"


def test_publish_and_read_latest_context():
    ctx = build_arbiter_context(
        breakouts=[], state_transition=Transition(), separated_verdict=Verdict(),
        actionability={"actionable": True, "status": "ACTIONABLE", "reason": "ok"},
    )
    publish_arbiter_context(ctx)
    latest = get_latest_arbiter_context(max_age_s=180)
    assert latest["status"] == "OBSERVED"
    assert latest["stale"] is False
    assert latest["structure"]["state"] == "NO_ACTIVE_BREAKOUT"
