"""
High-Probability Setup Predictor for XAU/USD.

Combines five evidence layers into a single probability score (0-100)
with explicit factor breakdown — designed as a DECISION SUPPORT TOOL
for manual execution. The user reviews the prediction and chooses
whether to execute.

The five layers:
  1. TECHNICAL    — institutional scanner state (market_state, grade, confluence)
  2. FUNDAMENTAL  — DXY trend (inverse correlation) + computed real-yields proxy
  3. NEWS         — proximity to high-impact USD events, blackout windows
  4. VOLATILITY   — gold ATR regime (low/normal/high) as options-flow proxy
  5. SENTIMENT    — MyFxBook retail sentiment contrarian indicator

Each layer outputs:
  - score: 0-100
  - direction: BUY/SELL/NEUTRAL alignment
  - status: GREEN / YELLOW / RED
  - reasons: list of factors

Composite probability is a weighted sum of layer scores, blended with
the technical signal direction. Returns a structured decision card.

Safety: pure analysis. Does NOT place trades, does NOT confirm signals.
The user makes the call.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone

log = logging.getLogger(__name__)

# Layer weights (sum = 100)
LAYER_WEIGHTS = {
    "technical":   40,      # primary signal — biggest contributor
    "fundamental": 20,      # macro backdrop
    "news":        15,      # event risk avoidance
    "volatility":  15,      # vol regime alignment
    "sentiment":    10,     # contrarian retail positioning
}


@dataclass
class LayerVerdict:
    name:      str
    score:     int          # 0-100 within this layer
    direction: str          # BUY | SELL | NEUTRAL
    status:    str          # GREEN | YELLOW | RED
    reasons:   list[str] = field(default_factory=list)


@dataclass
class HighProbabilityPrediction:
    instrument:    str
    timestamp:     str
    probability:   int          # 0-100 composite
    direction:     str          # BUY | SELL | WAIT
    band:          str          # STRONG / MODERATE / WEAK / AVOID
    decision:      str          # human-readable recommendation
    layers:        list[LayerVerdict]
    trade_plan:    dict | None  # entry/SL/TP if technical engine produced one
    warnings:      list[str]
    aligned_count: int          # how many layers (of 5) align with the direction


# ═══════════════════════════════════════════════════════════════════════════════
# Layer evaluators
# ═══════════════════════════════════════════════════════════════════════════════

def _eval_technical(scan_result: dict) -> LayerVerdict:
    """Score the technical/ICT setup quality from scanner output."""
    state = scan_result.get("marketState", "NO_TRADE")
    signal = scan_result.get("signal", "WAIT")
    grade  = scan_result.get("grade", "D")
    conf   = scan_result.get("confidence", 0)

    # Grade-based scoring
    grade_score_map = {"A+": 100, "A": 90, "B": 75, "C": 55, "D": 30, "F": 0}
    base_score = grade_score_map.get(grade, 40)

    # Adjust for market state
    state_adj = {
        "SIGNAL_READY":        +20,
        "SETUP_FORMING":       +10,
        "WATCHLIST":           0,
        "NO_TRADE":            -15,
        "NEWS_BLOCKED":        -25,
        "DATA_STALE":          -30,
        "VOLATILITY_UNSTABLE": -20,
        "SPREAD_TOO_WIDE":     -15,
    }.get(state, -10)

    score = max(0, min(100, base_score + state_adj))
    direction = signal if signal in ("BUY", "SELL") else "NEUTRAL"

    if state == "SIGNAL_READY" and signal in ("BUY", "SELL"):
        status = "GREEN"
    elif state in ("SETUP_FORMING", "WATCHLIST"):
        status = "YELLOW"
    else:
        status = "RED"

    reasons = [
        f"Market state: {state}",
        f"Setup grade: {grade}",
        f"Scanner confidence: {conf}",
    ]
    # Add confluence factors if present
    confl = scan_result.get("confluence", {})
    if confl:
        aligned = confl.get("alignedCount", 0)
        total   = confl.get("totalFactors", 0)
        if total:
            reasons.append(f"Confluence: {aligned}/{total} factors aligned")

    return LayerVerdict("technical", score, direction, status, reasons)


def _eval_fundamental(scan_result: dict) -> LayerVerdict:
    """Score the macro backdrop from DXY trend + risk tone."""
    macro = scan_result.get("macro", {})
    dxy = (macro.get("dxyTrend") or "").lower()
    yields = (macro.get("yieldsTrend") or "").lower()
    tone = (macro.get("riskTone") or "").lower()

    direction = "NEUTRAL"
    score = 50  # neutral default
    status = "YELLOW"
    reasons = []

    # Gold = -0.8 with DXY: weakening DXY supports BUY, strengthening supports SELL
    if "weakening" in dxy:
        direction = "BUY"
        score = 70
        status = "GREEN"
        reasons.append("DXY weakening — bullish for gold")
    elif "strengthening" in dxy:
        direction = "SELL"
        score = 70
        status = "GREEN"
        reasons.append("DXY strengthening — bearish for gold")
    else:
        reasons.append(f"DXY trend: {dxy or 'neutral'}")

    # Yields: rising = bearish gold; falling = bullish gold
    if "falling" in yields and direction == "BUY":
        score = min(100, score + 10)
        reasons.append("Yields falling — supports BUY")
    elif "rising" in yields and direction == "SELL":
        score = min(100, score + 10)
        reasons.append("Yields rising — supports SELL")
    elif "rising" in yields and direction == "BUY":
        score = max(0, score - 10)
        reasons.append("Yields rising — opposes BUY")
        status = "YELLOW"
    elif "falling" in yields and direction == "SELL":
        score = max(0, score - 10)
        reasons.append("Yields falling — opposes SELL")
        status = "YELLOW"

    # Risk tone
    if "risk-on" in tone:
        reasons.append("Risk-on (USD/yields offered)")
    elif "risk-off" in tone:
        reasons.append("Risk-off (USD/yields bid)")
    elif "event risk" in tone:
        reasons.append(f"Active event risk: {tone}")
        status = "YELLOW"

    return LayerVerdict("fundamental", score, direction, status, reasons)


def _eval_news(scan_result: dict) -> LayerVerdict:
    """Score the news-event proximity layer."""
    news = scan_result.get("news", {})
    status_str = news.get("status", "")
    blocking = news.get("blockingEvent")
    minutes_to = news.get("minutesToEvent")
    next_event = news.get("nextEvent")

    reasons = []
    direction = "NEUTRAL"

    if status_str == "BLOCKED":
        return LayerVerdict("news", 0, "NEUTRAL", "RED",
                             [f"News BLOCKED: {blocking}",
                              "Do not trade until window expires"])

    # Clear, but check proximity to next event
    if minutes_to is not None and next_event:
        if minutes_to <= 60:
            return LayerVerdict("news", 25, "NEUTRAL", "RED",
                                 [f"Imminent event: {next_event} in {minutes_to} min",
                                  "Avoid entry — pre-news window"])
        elif minutes_to <= 240:
            return LayerVerdict("news", 50, "NEUTRAL", "YELLOW",
                                 [f"Event coming: {next_event} in {minutes_to} min",
                                  "Trade with caution — may not reach TP before event"])
        else:
            reasons.append(f"Next event {next_event} in {minutes_to} min (safe)")

    # No imminent events
    reasons.insert(0, "News window: CLEAR")
    return LayerVerdict("news", 85, "NEUTRAL", "GREEN", reasons)


def _eval_volatility(scan_result: dict) -> LayerVerdict:
    """Score volatility regime — gold trades best in NORMAL vol, badly in too-low or too-high."""
    risk = scan_result.get("risk", {})
    vol_status = risk.get("volatilityStatus", "UNKNOWN")
    vol_regime = risk.get("volatilityRegime", "STABLE")
    atr        = risk.get("atr", 0)

    if vol_status == "OK" and vol_regime in ("STABLE", "EXPANSION"):
        return LayerVerdict("volatility", 80, "NEUTRAL", "GREEN",
                             [f"Volatility OK (ATR {atr})",
                              f"Regime: {vol_regime}"])
    if vol_status == "LOW":
        return LayerVerdict("volatility", 35, "NEUTRAL", "YELLOW",
                             [f"Low volatility (ATR {atr}) — may struggle to reach target",
                              f"Regime: {vol_regime}"])
    if vol_status == "HIGH" or vol_regime == "BREAKOUT":
        return LayerVerdict("volatility", 40, "NEUTRAL", "YELLOW",
                             [f"Elevated volatility (ATR {atr}) — wider stops, slippage risk",
                              f"Regime: {vol_regime}"])
    return LayerVerdict("volatility", 50, "NEUTRAL", "YELLOW",
                         [f"Volatility status: {vol_status}", f"Regime: {vol_regime}"])


def _eval_sentiment(scan_result: dict) -> LayerVerdict:
    """Score retail sentiment as CONTRARIAN indicator."""
    # Pull from the engine model's sentiment field (if MyFxBook integration is active)
    em = scan_result.get("engineModel", {})
    sentiment = em.get("sentiment") or {}
    if not sentiment.get("available"):
        return LayerVerdict("sentiment", 50, "NEUTRAL", "YELLOW",
                             ["Sentiment data unavailable"])

    long_pct = sentiment.get("longPct", 50)
    interp   = sentiment.get("interpretation", "")
    adj      = sentiment.get("adjusted_score", 0)

    # Contrarian: extreme long retail = SELL bias; extreme short retail = BUY bias
    direction = "NEUTRAL"
    status = "YELLOW"
    score = 50

    if long_pct >= 70:
        direction = "SELL"
        status = "GREEN"
        score = 70
    elif long_pct <= 30:
        direction = "BUY"
        status = "GREEN"
        score = 70
    elif 45 <= long_pct <= 55:
        status = "YELLOW"

    return LayerVerdict("sentiment", score, direction, status,
                         [f"Retail long: {long_pct}%",
                          interp or "Contrarian view applied"])


# ═══════════════════════════════════════════════════════════════════════════════
# Composite scoring
# ═══════════════════════════════════════════════════════════════════════════════

def _classify_band(prob: int) -> str:
    if prob >= 80:  return "STRONG"
    if prob >= 65:  return "MODERATE"
    if prob >= 50:  return "WEAK"
    return "AVOID"


def _build_decision_text(direction: str, band: str, aligned: int,
                          trade_plan: dict | None, warnings: list[str]) -> str:
    if direction == "WAIT":
        return "WAIT — no actionable signal. Re-check scanner in 60s."
    if band == "STRONG":
        confluence_note = " Trade plan ready — review and execute manually if you agree." if trade_plan \
                          else " Confluence is strong but scanner has no entry plan yet — wait for SIGNAL_READY."
        return f"HIGH-PROBABILITY {direction}: {aligned}/5 evidence layers align.{confluence_note}"
    if band == "MODERATE":
        return (
            f"MODERATE {direction}: {aligned}/5 layers align. "
            f"Consider half-size or wait for full confluence."
        )
    if band == "WEAK":
        return (
            f"WEAK {direction}: only {aligned}/5 layers support. "
            f"Skip unless you have additional conviction outside the system."
        )
    return f"AVOID: probability too low ({aligned}/5 aligned). Stand aside."


# ═══════════════════════════════════════════════════════════════════════════════
# Main entry point
# ═══════════════════════════════════════════════════════════════════════════════

def predict_xauusd(db=None) -> HighProbabilityPrediction:
    """
    Run the full 5-layer high-probability evaluation against current state.
    Pulls live scanner data, evaluates each layer, returns composite verdict.
    """
    from services.institutional_scanner import scan_xauusd_market

    scan = scan_xauusd_market(force_refresh=False, db=db)

    # Evaluate each layer
    layers = [
        _eval_technical(scan),
        _eval_fundamental(scan),
        _eval_news(scan),
        _eval_volatility(scan),
        _eval_sentiment(scan),
    ]

    # Composite probability (weighted average of layer scores)
    weighted_sum = 0
    total_weight = 0
    for layer in layers:
        w = LAYER_WEIGHTS.get(layer.name, 0)
        weighted_sum += layer.score * w
        total_weight += w
    probability = int(round(weighted_sum / max(total_weight, 1)))

    # Direction = scanner signal if technical fired; else use dominant layer direction
    technical = layers[0]
    if technical.direction in ("BUY", "SELL"):
        direction = technical.direction
    else:
        # Vote across layers (excluding NEUTRAL)
        bull = sum(1 for L in layers if L.direction == "BUY")
        bear = sum(1 for L in layers if L.direction == "SELL")
        if bull > bear:    direction = "BUY"
        elif bear > bull:  direction = "SELL"
        else:              direction = "WAIT"

    # Count aligned layers (same direction as composite OR NEUTRAL+GREEN)
    aligned = 0
    if direction in ("BUY", "SELL"):
        for L in layers:
            if L.direction == direction:
                aligned += 1
            elif L.direction == "NEUTRAL" and L.status == "GREEN":
                aligned += 1   # NEUTRAL-GREEN doesn't oppose

    # Hard down-weighting if any RED layer
    red_count = sum(1 for L in layers if L.status == "RED")
    if red_count > 0:
        probability = min(probability, 60)        # cap at 60 if any RED
    if any(L.name == "news" and L.status == "RED" for L in layers):
        probability = min(probability, 35)        # very low if news blocking
        direction = "WAIT"

    if technical.status == "RED" and technical.direction == "NEUTRAL":
        direction = "WAIT"

    band = _classify_band(probability)

    # Trade plan (if scanner produced one)
    trade_plan = None
    rec_action = scan.get("recommendedAction", {})
    if rec_action.get("actionable") and rec_action.get("tradePlan"):
        trade_plan = rec_action["tradePlan"]

    # Warnings
    warnings = []
    for L in layers:
        if L.status == "RED":
            warnings.extend([f"[{L.name.upper()} RED] {r}" for r in L.reasons[:1]])

    decision = _build_decision_text(direction, band, aligned, trade_plan, warnings)

    return HighProbabilityPrediction(
        instrument="XAU/USD",
        timestamp=datetime.now(timezone.utc).isoformat(),
        probability=probability,
        direction=direction,
        band=band,
        decision=decision,
        layers=layers,
        trade_plan=trade_plan,
        warnings=warnings,
        aligned_count=aligned,
    )


def prediction_to_dict(pred: HighProbabilityPrediction) -> dict:
    return {
        "instrument":   pred.instrument,
        "timestamp":    pred.timestamp,
        "probability":  pred.probability,
        "direction":    pred.direction,
        "band":         pred.band,
        "decision":     pred.decision,
        "alignedCount": pred.aligned_count,
        "totalLayers":  len(pred.layers),
        "layers": [
            {
                "name":      L.name,
                "score":     L.score,
                "direction": L.direction,
                "status":    L.status,
                "reasons":   L.reasons,
                "weight":    LAYER_WEIGHTS.get(L.name, 0),
            }
            for L in pred.layers
        ],
        "tradePlan": pred.trade_plan,
        "warnings":  pred.warnings,
    }
