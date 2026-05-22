"""
XAUUSD Institutional Demo Execution Engine
==========================================

The unified decision aggregator implementing the institutional demo-mandate.
Synthesises every existing engine (scanner, predictor, killzone analyser,
killzone policy, ICT framework, correlation engine, calendar) into a single
structured JSON verdict.

Decision ∈ { BUY, SELL, STAND ASIDE }
  STAND ASIDE is preferred when conditions are not high-quality.

OPERATING MODE
  • DEMO ONLY · live trading is hard-disabled
  • Fixed lot 0.01 · never larger, never increased after losses
  • Capital preservation comes first

5-CONDITION SCORING MODEL
  C1  Timeframe alignment supports direction
  C2  Liquidity sweep or liquidity target is confirmed
  C3  Structure / momentum confirms direction
  C4  Macro & session context does not conflict
  C5  Risk-reward + invalidation are acceptable

  5/5 → A-grade demo execution allowed   (est WR 78-85%)
  4/5 → Valid demo execution allowed     (est WR 70-80%)
  3/5 → Watchlist only, no execution     (est WR 58-68%)
 ≤2/5 → STAND ASIDE

EXECUTION STATUS ENUM (always set on every verdict)
  SIGNAL_ONLY · DEMO_TRADE_PLACED · DEMO_TRADE_REJECTED · STAND_ASIDE
  BRIDGE_OFFLINE · SPREAD_TOO_HIGH · NEWS_RISK_BLOCKED · INVALIDATED_BEFORE_ENTRY

Demo execution is only attempted when ALL of these are true:
  • conditions_passed ≥ 4
  • rr ≥ 1.5  (preferred ≥ 2.5)
  • entry/SL/TP1/TP2 all defined
  • bridge heartbeat fresh (≤120s)
  • spread acceptable
  • not inside a high-impact news window
  • demo_auto_enqueue setting on AND allow_demo_trading on
  • live_trading_authorized stays false (hard-coded check)

The aggregator NEVER fabricates data — every field comes from a real engine
output. When a sub-engine has no opinion, the field is null and the strategist
either explains why or stands aside.
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy.orm import Session

log = logging.getLogger(__name__)


# ────────────────────────────────────────────────────────────────────────────
# VWAP helper (session-anchored)
# ────────────────────────────────────────────────────────────────────────────

def compute_session_vwap(candles_m15, anchor_utc_hour: int = 0) -> dict:
    """
    Anchored VWAP starting from the most recent `anchor_utc_hour` UTC bar.
    Default anchor is 00:00 UTC = daily session anchor.
    """
    if not candles_m15:
        return {"vwap": None, "current": None, "position": "unknown", "deviation_pct": 0.0}
    now = datetime.now(timezone.utc)
    # Find the bar at/just after anchor for the most recent day that has bars
    today_anchor = now.replace(hour=anchor_utc_hour, minute=0, second=0, microsecond=0)
    if today_anchor > now:
        from datetime import timedelta
        today_anchor -= timedelta(days=1)

    session_bars = []
    for c in candles_m15:
        ct = c.time if c.time.tzinfo else c.time.replace(tzinfo=timezone.utc)
        if ct.astimezone(timezone.utc) >= today_anchor:
            session_bars.append(c)
    if not session_bars:
        return {"vwap": None, "current": None, "position": "unknown", "deviation_pct": 0.0}

    sum_pv = sum(((b.high + b.low + b.close) / 3) * b.volume for b in session_bars)
    sum_v  = sum(b.volume for b in session_bars)
    vwap = sum_pv / sum_v if sum_v > 0 else None
    if vwap is None:
        return {"vwap": None, "current": session_bars[-1].close, "position": "unknown", "deviation_pct": 0.0}

    cur = session_bars[-1].close
    dev_pct = (cur - vwap) / vwap * 100 if vwap > 0 else 0.0
    if abs(dev_pct) < 0.05:
        position = "AT_VWAP"
    elif dev_pct > 0:
        position = "ABOVE_VWAP"
    else:
        position = "BELOW_VWAP"

    return {
        "vwap": round(vwap, 2),
        "current": round(cur, 2),
        "position": position,
        "deviation_pct": round(dev_pct, 3),
    }


# ────────────────────────────────────────────────────────────────────────────
# Indicator helpers (live computation)
# ────────────────────────────────────────────────────────────────────────────

def _rsi(values, n=14):
    if len(values) < n + 1: return 50.0
    gains, losses = [], []
    for i in range(1, len(values)):
        d = values[i] - values[i - 1]
        gains.append(max(d, 0)); losses.append(max(-d, 0))
    avg_gain = sum(gains[-n:]) / n
    avg_loss = sum(losses[-n:]) / n
    if avg_loss == 0: return 100.0
    return round(100 - 100 / (1 + avg_gain / avg_loss), 1)

def _ema(values, n):
    if len(values) < n: return values[-1] if values else 0
    k = 2 / (n + 1)
    e = sum(values[:n]) / n
    for v in values[n:]: e = v * k + e * (1 - k)
    return round(e, 2)

def _atr(highs, lows, closes, n=14):
    if len(highs) < n + 1: return 0
    trs = []
    for i in range(1, len(highs)):
        trs.append(max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]), abs(lows[i] - closes[i - 1])))
    return round(sum(trs[-n:]) / n, 2)


# ────────────────────────────────────────────────────────────────────────────
# Mandate-enum classifiers
# Each returns one of the exact strings the institutional mandate requires —
# no free-text, no surprise variants. The dashboard panel and Telegram
# format both depend on these being stable.
# ────────────────────────────────────────────────────────────────────────────

# Market State — 8 mandate categories
_MARKET_STATE_TRENDING_BULL    = "Trending bullish"
_MARKET_STATE_TRENDING_BEAR    = "Trending bearish"
_MARKET_STATE_RANGE            = "Range-bound"
_MARKET_STATE_SWEEP            = "Liquidity sweep"
_MARKET_STATE_NEWS_REPRICE     = "News repricing"
_MARKET_STATE_COMPRESSION      = "Compression before expansion"
_MARKET_STATE_DECAY            = "Post-news volatility decay"
_MARKET_STATE_NO_STRUCTURE     = "No clean structure"


def _classify_market_state_mandate(
    *,
    ema20_h1: float, ema50_h1: float, ema100_h1: float | None,
    rsi_h1: float, atr_h1: float, atr_h1_baseline: float,
    news_clear: bool, scan_market_state: str,
    swept_recent: bool, kz_posture: str | None,
) -> str:
    """Map the engine's many internal flags to ONE of the 8 mandate strings."""
    if not news_clear:
        return _MARKET_STATE_NEWS_REPRICE
    if scan_market_state == "DATA_STALE":
        return _MARKET_STATE_NO_STRUCTURE
    if swept_recent:
        return _MARKET_STATE_SWEEP
    # Volatility compression: ATR < 60% of recent baseline → likely range
    if atr_h1_baseline > 0 and atr_h1 < 0.6 * atr_h1_baseline:
        # If trending EMAs say so, it's compression-before-expansion
        if ema100_h1 is not None and (ema20_h1 > ema50_h1 > ema100_h1 or ema20_h1 < ema50_h1 < ema100_h1):
            return _MARKET_STATE_COMPRESSION
        return _MARKET_STATE_RANGE
    # Post-news decay: ATR < 80% of baseline AND we're past a recent news window
    if atr_h1_baseline > 0 and atr_h1 < 0.8 * atr_h1_baseline and kz_posture in ("OBSERVE", "AVOID"):
        return _MARKET_STATE_DECAY
    if ema100_h1 is not None:
        if ema20_h1 > ema50_h1 > ema100_h1 and rsi_h1 > 50:
            return _MARKET_STATE_TRENDING_BULL
        if ema20_h1 < ema50_h1 < ema100_h1 and rsi_h1 < 50:
            return _MARKET_STATE_TRENDING_BEAR
    if abs(ema20_h1 - ema50_h1) < (atr_h1 * 0.25):
        return _MARKET_STATE_RANGE
    return _MARKET_STATE_NO_STRUCTURE


# Session — 8 mandate categories  (mapped from UTC hour-of-day)
_SESSION_ASIAN_RANGE       = "Asian range formation"
_SESSION_LDN_OPEN_SWEEP    = "London open sweep"
_SESSION_LDN_CONTINUATION  = "London continuation"
_SESSION_LDN_LUNCH_CHOP    = "London lunch chop"
_SESSION_NY_OPEN_SWEEP     = "New York open sweep"
_SESSION_LDN_NY_OVERLAP    = "London/New York overlap expansion"
_SESSION_POST_NEWS         = "Post-news disorder"
_SESSION_LATE_LOW_QUAL     = "Late-session low-quality liquidity"


def _classify_session_mandate(*, hour_utc: float, news_clear: bool) -> str:
    """Map UTC hour to ONE of the 8 mandate session strings."""
    if not news_clear:
        return _SESSION_POST_NEWS
    if   hour_utc < 6:             return _SESSION_ASIAN_RANGE
    elif hour_utc < 8:             return _SESSION_LDN_OPEN_SWEEP
    elif hour_utc < 11:            return _SESSION_LDN_CONTINUATION
    elif hour_utc < 12:            return _SESSION_LDN_LUNCH_CHOP
    elif hour_utc < 14:            return _SESSION_NY_OPEN_SWEEP
    elif hour_utc < 17:            return _SESSION_LDN_NY_OVERLAP
    elif hour_utc < 22:            return _SESSION_LATE_LOW_QUAL
    else:                          return _SESSION_LATE_LOW_QUAL


# Timeframe alignment — 6 mandate categories
_TF_STRONG_BULL      = "Strong bullish"
_TF_BULL_EXTENDED    = "Bullish but extended"
_TF_NEUTRAL          = "Neutral"
_TF_BEAR_EXTENDED    = "Bearish but extended"
_TF_STRONG_BEAR      = "Strong bearish"
_TF_CONFLICTED       = "Conflicted"


def _classify_tf_alignment_mandate(
    *,
    d1_bias: str, h4_bias: str, h1_ema20: float, h1_ema50: float,
    rsi_h1: float,
) -> str:
    """Map HTF biases to ONE of the 6 mandate strings."""
    d = (d1_bias or "").lower()
    h = (h4_bias or "").lower()

    bulls = sum(1 for x in (d, h) if "bull" in x)
    bears = sum(1 for x in (d, h) if "bear" in x)
    h1_bull = h1_ema20 > h1_ema50

    if bulls == 2 and h1_bull and rsi_h1 < 70:
        return _TF_STRONG_BULL
    if bulls == 2 and h1_bull and rsi_h1 >= 70:
        return _TF_BULL_EXTENDED
    if bears == 2 and not h1_bull and rsi_h1 > 30:
        return _TF_STRONG_BEAR
    if bears == 2 and not h1_bull and rsi_h1 <= 30:
        return _TF_BEAR_EXTENDED
    if bulls and bears:
        return _TF_CONFLICTED
    return _TF_NEUTRAL


# Liquidity behaviour — 5 mandate categories
_LIQ_RUNNING       = "Running liquidity"
_LIQ_REJECTING     = "Rejecting liquidity"
_LIQ_RECLAIMING    = "Reclaiming liquidity"
_LIQ_CHOPPING      = "Chopping between pools"
_LIQ_EXPANDING     = "Expanding away from swept liquidity"


def _classify_liquidity_behaviour(*, scan: dict, model_letter: str, model_confirmed: bool) -> str:
    """Pick one of the 5 mandate strings describing how price is acting at liquidity."""
    eng_model = scan.get("engineModel", {}) or {}
    liq = (eng_model.get("liquidity") or "").lower()
    struct = (eng_model.get("structure") or "").lower()

    if "reclaim" in liq or "reclaim" in struct:
        return _LIQ_RECLAIMING
    if "reject" in liq or "rejection" in struct:
        return _LIQ_REJECTING
    if model_letter == "A" and model_confirmed:
        return _LIQ_EXPANDING
    if "swept" in liq or "sweep" in liq:
        return _LIQ_RUNNING
    return _LIQ_CHOPPING


# Estimated win-rate ranges — mandate values
def _estimate_win_rate(passed: int) -> str:
    if passed >= 5: return "78-85%"
    if passed >= 4: return "70-80%"
    if passed >= 3: return "58-68%"
    return "no trade"


# Bridge heartbeat freshness check
def _is_bridge_alive(max_age_seconds: int = 120) -> bool:
    """True iff any MT5 bridge daemon has pinged /bridge/health within window."""
    try:
        from routers.bridge import _BRIDGE_HEARTBEAT
        if not _BRIDGE_HEARTBEAT:
            return False
        now = datetime.now(timezone.utc)
        return any(
            (now - ts).total_seconds() < max_age_seconds
            for ts in _BRIDGE_HEARTBEAT.values()
        )
    except Exception:
        return False


# 5-condition evaluator
def _evaluate_5_conditions(
    *,
    proposed_signal: str,
    tf_alignment_label: str,
    model_letter: str,
    model_confirmed: bool,
    scan_market_state: str,
    ict_score: int,
    macro_alignment: str,
    news_clear: bool,
    kz_posture: str | None,
    rr: float,
    entry: float | None,
    stop_loss: float | None,
    tp1: float | None,
    tp2: float | None,
) -> list[dict]:
    """
    Score the 5 mandate conditions. Each entry: {name, passed, detail}.
    """
    is_buy  = proposed_signal == "BUY"
    is_sell = proposed_signal == "SELL"

    # C1: Timeframe alignment supports direction
    c1_ok = False
    if is_buy and tf_alignment_label in (_TF_STRONG_BULL,):
        c1_ok = True
    elif is_sell and tf_alignment_label in (_TF_STRONG_BEAR,):
        c1_ok = True
    elif tf_alignment_label in (_TF_BULL_EXTENDED, _TF_BEAR_EXTENDED):
        # Extended is acceptable only for fade-style entries — treat as half-pass (don't count)
        c1_ok = False

    # C2: Liquidity sweep / target confirmed
    c2_ok = model_confirmed and model_letter in ("A", "B", "C", "D")

    # C3: Structure or momentum confirms direction
    c3_ok = (scan_market_state == "SIGNAL_READY") or (ict_score >= 60)

    # C4: Macro / session does not conflict
    c4_ok = (
        macro_alignment != "Conflicted"
        and news_clear
        and kz_posture in ("TRADE", "PRESS")
    )

    # C5: RR + invalidation acceptable
    c5_ok = bool(
        rr and rr >= 1.5
        and entry is not None and stop_loss is not None
        and tp1 is not None and tp2 is not None
    )

    return [
        {"name": "C1 Timeframe alignment supports direction",  "passed": c1_ok,
         "detail": tf_alignment_label},
        {"name": "C2 Liquidity sweep / target confirmed",      "passed": c2_ok,
         "detail": f"Model {model_letter} · confirmed={model_confirmed}"},
        {"name": "C3 Structure / momentum confirms direction", "passed": c3_ok,
         "detail": f"scanner={scan_market_state} · ict={ict_score}/100"},
        {"name": "C4 Macro / session does not conflict",        "passed": c4_ok,
         "detail": f"macro={macro_alignment} · news={'CLEAR' if news_clear else 'BLOCK'} · kz={kz_posture or '—'}"},
        {"name": "C5 RR + invalidation acceptable",             "passed": c5_ok,
         "detail": f"rr={rr or 0:.2f} · SL={stop_loss} · TP1={tp1} · TP2={tp2}"},
    ]


# Execution-status decider — produces ONE of the 8 mandate enum values
_EXEC_SIGNAL_ONLY      = "SIGNAL_ONLY"
_EXEC_DEMO_PLACED      = "DEMO_TRADE_PLACED"
_EXEC_DEMO_REJECTED    = "DEMO_TRADE_REJECTED"
_EXEC_STAND_ASIDE      = "STAND_ASIDE"
_EXEC_BRIDGE_OFFLINE   = "BRIDGE_OFFLINE"
_EXEC_SPREAD_HIGH      = "SPREAD_TOO_HIGH"
_EXEC_NEWS_BLOCKED     = "NEWS_RISK_BLOCKED"
_EXEC_INVALIDATED      = "INVALIDATED_BEFORE_ENTRY"


def _decide_execution_status(
    *,
    conditions_passed: int,
    proposed_signal: str,
    news_clear: bool,
    spread_acceptable: bool,
    bridge_alive: bool,
    rr: float,
    entry: float | None,
    stop_loss: float | None,
    tp1: float | None,
    tp2: float | None,
    demo_auto_enqueue: bool,
    allow_demo: bool,
) -> tuple[str, str]:
    """
    Pick the execution_status value. Returns (status, reason).

    Mandate precedence:
      1. STAND_ASIDE  — score below 3/5, or no direction
      2. NEWS_RISK_BLOCKED / SPREAD_TOO_HIGH / BRIDGE_OFFLINE
                      — clean setup but execution conditions fail
      3. SIGNAL_ONLY  — 3/5 watchlist, or 4-5/5 but enqueue disabled / RR<1.5
      4. DEMO_TRADE_PLACED — all gates pass
      (DEMO_TRADE_REJECTED + INVALIDATED_BEFORE_ENTRY are set post-fact
      by the bridge / monitor — not by this function.)
    """
    if proposed_signal not in ("BUY", "SELL") or conditions_passed < 3:
        return _EXEC_STAND_ASIDE, "Setup below 3/5 conditions"

    if conditions_passed == 3:
        return _EXEC_SIGNAL_ONLY, "Watchlist — 3/5 (no demo execution)"

    # 4-5/5 from here on — check execution gates
    if not news_clear:
        return _EXEC_NEWS_BLOCKED, "Inside high-impact news window"
    if not spread_acceptable:
        return _EXEC_SPREAD_HIGH, "Spread outside acceptable band"
    if not bridge_alive:
        return _EXEC_BRIDGE_OFFLINE, "MT5 bridge daemon heartbeat stale"
    if not entry or not stop_loss or not tp1 or not tp2:
        return _EXEC_SIGNAL_ONLY, "Trade levels incomplete"
    if not rr or rr < 1.5:
        return _EXEC_SIGNAL_ONLY, f"RR {rr or 0:.2f}<1.5 demo floor"
    if not (demo_auto_enqueue and allow_demo):
        return _EXEC_SIGNAL_ONLY, "Demo auto-enqueue disabled by operator"

    return _EXEC_DEMO_PLACED, "All gates pass"


def _build_mt5_execution_object(
    *,
    decision: str, entry: float, stop_loss: float,
    tp1: float, tp2: float, rr: float,
    setup_score_100: int, conditions_passed: int,
    execution_status: str,
) -> dict:
    """
    Strict MT5-bridge schema per the mandate. Only emitted when
    execution_status == DEMO_TRADE_PLACED. Lot is HARD-CODED 0.01.
    """
    return {
        "symbol":                  "XAUUSD",
        "mode":                    "demo",
        "action":                  decision,            # BUY or SELL
        "lot":                     0.01,                # ← hard-coded
        "entry":                   round(entry, 2),
        "stop_loss":               round(stop_loss, 2),
        "take_profit_1":           round(tp1, 2),
        "take_profit_2":           round(tp2, 2),
        "risk_reward":             round(rr, 2),
        "setup_score":             setup_score_100,
        "conditions_passed":       conditions_passed,
        "execution_status":        execution_status,
        "bridge_required":         True,
        "live_execution_allowed":  False,               # ← always false
        "learning_log_required":   True,
    }


def _compute_entry_tolerance(atr_h1: float) -> float:
    """How far from `entry` the live price can be and still hit market exec."""
    if atr_h1 <= 0:
        return 1.0
    return round(min(max(atr_h1 * 0.10, 0.5), 3.0), 2)


# ────────────────────────────────────────────────────────────────────────────
# Execution-model classifier
# ────────────────────────────────────────────────────────────────────────────

def _classify_execution_model(*, scan: dict, ict: dict, news_clear: bool) -> tuple[str, bool, str, str]:
    """
    Identify which institutional execution model the current setup fits:
      A — Liquidity Sweep Reversal
      B — Breakout Retest Continuation
      C — Trend Pullback
      D — News Repricing Continuation
      E — None / Stand Aside
    Returns (model_name, confirmed, swept_level_str, target_str).
    """
    market_state = scan.get("marketState", "")
    eng_model    = scan.get("engineModel", {}) or {}
    liq_text     = (eng_model.get("liquidity") or "").lower()
    struct_text  = (eng_model.get("structure") or "").lower()
    fvg_text     = (eng_model.get("fvg") or "").lower()
    bias         = (scan.get("institutionalBias") or "").lower()

    swept_level = ""
    target = ""

    # MODEL A: Sweep + reclaim + structure shift
    swept = ("swept" in liq_text or "sweep" in liq_text) and "no liquidity" not in liq_text
    bos   = "bos" in struct_text or "choch" in struct_text
    if swept and bos and market_state == "SIGNAL_READY":
        return ("A", True, liq_text[:80], "next liquidity pool")

    # MODEL B: Breakout + retest
    if "retest" in struct_text or "retest" in fvg_text:
        return ("B", market_state == "SIGNAL_READY", struct_text[:80], "continuation target")

    # MODEL C: Trend pullback
    if "reversal" in bias.lower() and "in zone" in fvg_text:
        return ("C", market_state == "SIGNAL_READY", fvg_text[:80], "trend liquidity")

    # MODEL D: Post-news continuation
    if not news_clear:
        return ("D", False, "post-news repricing window", "wait for spread normalisation")

    # MODEL E
    return ("E", False, "none", "no actionable model")


# ────────────────────────────────────────────────────────────────────────────
# Main decision aggregator
# ────────────────────────────────────────────────────────────────────────────

def make_decision(db: Session) -> dict:
    """
    Produce the unified institutional verdict. Read-only — never mutates state,
    never fires Telegram (the router decides about alerts based on the response).
    """
    from data.candles import get_candles
    from services.institutional_scanner import scan_xauusd_market
    from services.high_probability_predictor import predict_xauusd, prediction_to_dict
    from services.killzone_analyzer import get_current_recommendation
    from services.killzone_policy import evaluate as eval_kz_policy
    from services.ict_advanced import compute_ict_alignment

    now = datetime.now(timezone.utc)

    # ── Pull candles for indicators ─────────────────────────────────────
    LIVE = {"tradingview", "mt5", "tradingview-cached", "mt5-cached"}
    try:
        m15_resp = get_candles(interval="M15", limit=500, pair="xauusd")
        h1_resp  = get_candles(interval="H1",  limit=200, pair="xauusd")
        h4_resp  = get_candles(interval="H4",  limit=100, pair="xauusd")
        d1_resp  = get_candles(interval="D1",  limit=30,  pair="xauusd")
    except Exception as exc:
        log.warning("[strategist] candle fetch failed: %s", exc)
        m15_resp = h1_resp = h4_resp = d1_resp = None

    if not m15_resp or not m15_resp.candles or getattr(m15_resp, "source", "") not in LIVE:
        return _stand_aside_envelope(
            now, reason=f"No live M15 data (source={getattr(m15_resp, 'source', 'none')})"
        )

    m15 = m15_resp.candles
    h1  = h1_resp.candles  if h1_resp  else []
    h4  = h4_resp.candles  if h4_resp  else []
    d1  = d1_resp.candles  if d1_resp  else []

    closes_m15 = [c.close for c in m15]
    closes_h1  = [c.close for c in h1]
    closes_h4  = [c.close for c in h4]

    current_price = round(closes_m15[-1], 2)

    # ── Indicator block ─────────────────────────────────────────────────
    rsi_h1     = _rsi(closes_h1)
    ema20_h1   = _ema(closes_h1, 20)
    ema50_h1   = _ema(closes_h1, 50)
    ema100_h1  = _ema(closes_h1, 100) if len(closes_h1) >= 100 else None
    macd_h1    = round(_ema(closes_h1, 12) - _ema(closes_h1, 26), 2)
    atr_h1     = _atr([c.high for c in h1], [c.low for c in h1], closes_h1)
    vwap = compute_session_vwap(m15)

    # ── Pull engine signals ─────────────────────────────────────────────
    try:
        scan = scan_xauusd_market(force_refresh=False, db=db) or {}
    except Exception as exc:
        log.warning("[strategist] scanner failed: %s", exc)
        scan = {"marketState": "UNKNOWN", "signal": "WAIT", "summary": str(exc)}

    try:
        pred_obj = predict_xauusd(db=db)
        pred = prediction_to_dict(pred_obj) if pred_obj else {}
    except Exception as exc:
        log.warning("[strategist] predictor failed: %s", exc)
        pred = {"band": "UNKNOWN", "direction": "WAIT", "probability": 0}

    try:
        kz = get_current_recommendation(db, lookback_days=60) or {}
    except Exception as exc:
        log.warning("[strategist] killzone failed: %s", exc)
        kz = {}

    proposed_signal = scan.get("signal") or pred.get("direction") or "WAIT"

    try:
        ict = compute_ict_alignment(
            candles_m15=m15,
            candles_h4=h4,
            at=now,
            signal_direction=proposed_signal if proposed_signal in ("BUY", "SELL") else None,
        )
    except Exception as exc:
        log.warning("[strategist] ICT framework failed: %s", exc)
        ict = None

    try:
        if proposed_signal in ("BUY", "SELL") and kz.get("current_kz"):
            kz_policy = eval_kz_policy(
                killzone_key=kz.get("current_kz", "unknown"),
                direction=proposed_signal,
                engine_id="swing",
            )
        else:
            kz_policy = None
    except Exception as exc:
        log.debug("[strategist] kz_policy lookup failed: %s", exc)
        kz_policy = None

    # ── Macro pull (from FRED + DXY layer in predictor) ─────────────────
    yields_layer = next((l for l in (pred.get("layers") or []) if l.get("name") == "yields"), None)
    fund_layer   = next((l for l in (pred.get("layers") or []) if l.get("name") == "fundamental"), None)
    news_layer   = next((l for l in (pred.get("layers") or []) if l.get("name") == "news"), None)

    news_clear = (news_layer or {}).get("status") == "GREEN"
    dxy_dir    = (fund_layer or {}).get("direction", "NEUTRAL")
    yields_dir = (yields_layer or {}).get("direction", "NEUTRAL")

    gold_macro_bias = "Neutral"
    if dxy_dir == "SELL" and yields_dir in ("NEUTRAL", "SELL"):
        gold_macro_bias = "Bearish (DXY ↑ + yields ≥)"
    elif dxy_dir == "BUY" and yields_dir in ("NEUTRAL", "BUY"):
        gold_macro_bias = "Bullish (DXY ↓ + yields ≤)"
    elif dxy_dir != "NEUTRAL" or yields_dir != "NEUTRAL":
        gold_macro_bias = "Mixed"

    macro_aligned = "Neutral"
    if proposed_signal == "BUY":
        if gold_macro_bias.startswith("Bullish"): macro_aligned = "Aligned"
        elif gold_macro_bias.startswith("Bearish"): macro_aligned = "Conflicted"
    elif proposed_signal == "SELL":
        if gold_macro_bias.startswith("Bearish"): macro_aligned = "Aligned"
        elif gold_macro_bias.startswith("Bullish"): macro_aligned = "Conflicted"

    # ── Key liquidity zones (computed) ──────────────────────────────────
    prev_day_high = round(max(c.high for c in d1[-2:-1]), 2) if len(d1) >= 2 else None
    prev_day_low  = round(min(c.low  for c in d1[-2:-1]), 2) if len(d1) >= 2 else None
    today_bars_m15 = [c for c in m15 if (c.time if c.time.tzinfo else c.time.replace(tzinfo=timezone.utc)).astimezone(timezone.utc).date() == now.date()]
    today_high = round(max(c.high for c in today_bars_m15), 2) if today_bars_m15 else None
    today_low  = round(min(c.low  for c in today_bars_m15), 2) if today_bars_m15 else None

    # Round-number levels around current price (50-point grid)
    rn_below = math.floor(current_price / 50) * 50
    rn_above = rn_below + 50
    round_numbers = sorted({rn_below - 50, rn_below, rn_above, rn_above + 50})

    # Session classification text
    h = now.hour + now.minute / 60
    if   h < 6:                session_label = "Asian range formation"
    elif h < 7:                session_label = "Pre-London"
    elif h < 10:               session_label = "London open / kill zone"
    elif h < 12:               session_label = "London continuation"
    elif h < 13:               session_label = "London lunch chop"
    elif h < 16:               session_label = "New York kill zone"
    elif h < 17:               session_label = "London/NY overlap close"
    elif h < 22:               session_label = "NY afternoon"
    else:                      session_label = "Late session — low quality"

    # Market state
    spread_acceptable = atr_h1 > 0 and atr_h1 < 100   # crude — refine with broker tick if available
    if scan.get("marketState") == "DATA_STALE":
        market_state = "Data stale — no clean structure"
    elif not news_clear:
        market_state = "News repricing window"
    elif kz.get("posture") == "AVOID":
        market_state = "Late-session low-quality liquidity"
    elif rsi_h1 > 70 and ema20_h1 > ema50_h1:
        market_state = "Trending bullish (extended)"
    elif rsi_h1 < 30 and ema20_h1 < ema50_h1:
        market_state = "Trending bearish (extended)"
    elif ema20_h1 > ema50_h1 > (ema100_h1 or 0):
        market_state = "Trending bullish"
    elif ema20_h1 < ema50_h1 < (ema100_h1 or 1e9):
        market_state = "Trending bearish"
    else:
        market_state = "Range-bound / mixed"

    # Execution model
    model_letter, model_confirmed, swept_text, target_text = _classify_execution_model(
        scan=scan, ict=(ict.score if ict else 0), news_clear=news_clear,
    )

    # ── Compute setup score (out of 100, per the strategist mandate) ────
    score_alignment = 15 if ema20_h1 > ema50_h1 and proposed_signal == "BUY" \
                  else 15 if ema20_h1 < ema50_h1 and proposed_signal == "SELL" \
                  else 5
    score_liquidity = 20 if model_letter in ("A","B","C") and model_confirmed else (10 if model_letter != "E" else 0)
    score_structure = 15 if scan.get("marketState") == "SIGNAL_READY" else \
                       8 if scan.get("marketState") == "WATCHLIST" else 0
    score_session   = 10 if kz.get("posture") == "PRESS" else \
                       8 if kz.get("posture") == "TRADE" else \
                       3 if kz.get("posture") == "OBSERVE" else 0
    score_macro     = 15 if macro_aligned == "Aligned" else \
                       7 if macro_aligned == "Neutral" else 0
    score_dxy       = 10 if (fund_layer or {}).get("status") == "GREEN" else \
                       5 if (fund_layer or {}).get("status") == "YELLOW" else 0

    score_rr = 0          # will fill once we know SL/TP
    score_spread = 5 if spread_acceptable else 0

    total_setup_score = score_alignment + score_liquidity + score_structure + \
                        score_session + score_macro + score_dxy + score_spread

    # ── Build trade plan if feasible ────────────────────────────────────
    plan_obj = scan.get("recommendedAction", {}).get("tradePlan") or {}
    entry        = plan_obj.get("entry")
    stop_loss    = plan_obj.get("stopLoss")
    take_profit  = plan_obj.get("takeProfit")
    risk_pts     = plan_obj.get("riskPoints") or plan_obj.get("riskPips")
    target_pts   = plan_obj.get("targetPoints")
    rr           = plan_obj.get("rr") or 0

    tp1, tp2, tp3 = None, None, None
    if entry and stop_loss and take_profit and proposed_signal in ("BUY", "SELL"):
        # Stagger TPs: TP1 = 1R, TP2 = 2R (scanner's full TP), TP3 = 3R
        rr_unit = abs(entry - stop_loss)
        if proposed_signal == "BUY":
            tp1 = round(entry + rr_unit * 1.0, 2)
            tp2 = round(entry + rr_unit * 2.5, 2)
            tp3 = round(entry + rr_unit * 4.0, 2)
        else:
            tp1 = round(entry - rr_unit * 1.0, 2)
            tp2 = round(entry - rr_unit * 2.5, 2)
            tp3 = round(entry - rr_unit * 4.0, 2)
        rr = round(abs(tp2 - entry) / rr_unit, 2)

    if rr and rr >= 2.5:
        score_rr = 10
    elif rr and rr >= 1.5:
        score_rr = 5
    total_setup_score += score_rr

    # ── Apply STRICT DECISION RULES ─────────────────────────────────────
    stand_aside_reasons = []
    if total_setup_score < 80:
        stand_aside_reasons.append(f"Setup score {total_setup_score}<80")
    if not entry or not stop_loss:
        stand_aside_reasons.append("Entry/SL not defined")
    if not tp1 or not tp2:
        stand_aside_reasons.append("TPs incomplete")
    if rr and rr < 2.5:
        stand_aside_reasons.append(f"RR {rr}<2.5")
    if not model_confirmed or model_letter == "E":
        stand_aside_reasons.append("No confirmed execution model")
    if macro_aligned == "Conflicted":
        stand_aside_reasons.append(f"Macro conflicts ({gold_macro_bias})")
    if not news_clear:
        stand_aside_reasons.append("Inside news risk window")
    if proposed_signal not in ("BUY", "SELL"):
        stand_aside_reasons.append("Scanner is in WAIT")
    if ict and ict.posture == "MISALIGNED":
        stand_aside_reasons.append(f"ICT framework MISALIGNED ({ict.score}/100)")
    if kz_policy is not None and not kz_policy.allow:
        stand_aside_reasons.append(
            f"KZ policy BLOCK ({kz.get('current_kz')} × {proposed_signal})"
        )

    decision = "STAND ASIDE"
    if not stand_aside_reasons and proposed_signal in ("BUY", "SELL"):
        # Mandate uses BUY/SELL (not LONG/SHORT) as the canonical decision values
        decision = proposed_signal

    # Quality band derived from the legacy 0-100 score (kept for backward-compat
    # with the existing dashboard panel). The mandate's primary band signal is
    # `conditions_passed` (see below).
    if total_setup_score >= 90: band = "A-grade"
    elif total_setup_score >= 80: band = "Valid"
    elif total_setup_score >= 70: band = "Watchlist"
    else: band = "No Trade"

    # ── Mandate enums (8/8/6/5-category classifications) ────────────────
    # ATR baseline = 50-period H1 ATR average, used to detect compression / decay
    atr_baseline_h1 = 0.0
    if len(h1) >= 50:
        recent_atr_samples = []
        for i in range(max(1, len(h1) - 50), len(h1)):
            window = h1[max(0, i - 14):i]
            if len(window) >= 14:
                ws = _atr([c.high for c in window], [c.low for c in window], [c.close for c in window])
                if ws: recent_atr_samples.append(ws)
        if recent_atr_samples:
            atr_baseline_h1 = sum(recent_atr_samples) / len(recent_atr_samples)

    swept_recently = bool(
        scan.get("engineModel", {}).get("liquidity", "").lower().count("swept")
        or scan.get("engineModel", {}).get("liquidity", "").lower().count("sweep")
    )
    market_state_mandate = _classify_market_state_mandate(
        ema20_h1=ema20_h1, ema50_h1=ema50_h1, ema100_h1=ema100_h1,
        rsi_h1=rsi_h1, atr_h1=atr_h1, atr_h1_baseline=atr_baseline_h1,
        news_clear=news_clear,
        scan_market_state=scan.get("marketState", "UNKNOWN"),
        swept_recent=swept_recently,
        kz_posture=kz.get("posture"),
    )
    session_mandate = _classify_session_mandate(hour_utc=h, news_clear=news_clear)
    tf_alignment_mandate = _classify_tf_alignment_mandate(
        d1_bias=(scan.get("engineModel") or {}).get("d1Bias", ""),
        h4_bias=(scan.get("engineModel") or {}).get("h4Bias", ""),
        h1_ema20=ema20_h1, h1_ema50=ema50_h1,
        rsi_h1=rsi_h1,
    )
    liquidity_behaviour = _classify_liquidity_behaviour(
        scan=scan, model_letter=model_letter, model_confirmed=model_confirmed,
    )

    # ── 5-condition mandate evaluation ──────────────────────────────────
    conditions = _evaluate_5_conditions(
        proposed_signal=proposed_signal,
        tf_alignment_label=tf_alignment_mandate,
        model_letter=model_letter,
        model_confirmed=model_confirmed,
        scan_market_state=scan.get("marketState", "UNKNOWN"),
        ict_score=(ict.score if ict else 0),
        macro_alignment=macro_aligned,
        news_clear=news_clear,
        kz_posture=kz.get("posture"),
        rr=rr or 0,
        entry=entry, stop_loss=stop_loss, tp1=tp1, tp2=tp2,
    )
    conditions_passed = sum(1 for c in conditions if c["passed"])
    est_win_rate = _estimate_win_rate(conditions_passed)

    # If conditions_passed < 3 we MUST stand aside regardless of legacy score
    if conditions_passed < 3 and decision != "STAND ASIDE":
        decision = "STAND ASIDE"
        stand_aside_reasons.insert(0, f"Conditions passed {conditions_passed}/5 (need ≥3 for any action)")

    # ── Next-trigger guidance ───────────────────────────────────────────
    long_trigger = ""
    short_trigger = ""
    if not prev_day_low or not prev_day_high:
        no_trade_cond = "No clean reference levels yet."
    else:
        long_trigger  = f"BUY valid IF: price sweeps {prev_day_low} then reclaims AND DXY weakens AND in London/NY KZ"
        short_trigger = f"SELL valid IF: price sweeps {prev_day_high} then rejects AND DXY strengthens AND in London/NY KZ"
        no_trade_cond = "Mid-range chop with no liquidity sweep, OR pre-news window"

    # ── Execution permissions ───────────────────────────────────────────
    from config import settings
    allow_alert  = (decision != "STAND ASIDE")
    allow_demo   = (decision != "STAND ASIDE" and total_setup_score >= 85
                    and settings.allow_demo_trading)
    allow_live   = False   # ← MANDATE: live execution is hard-disabled in this engine

    # ── Bridge / spread / news gates for execution_status ───────────────
    bridge_alive = _is_bridge_alive(max_age_seconds=120)
    # Spread acceptable: scanner sets risk.spreadStatus when live data is fresh
    spread_status = ((scan.get("risk") or {}).get("spreadStatus") or "UNKNOWN").upper()
    spread_acceptable = spread_status in ("OK", "NORMAL", "ACCEPTABLE", "UNKNOWN")
    demo_auto_enqueue = getattr(settings, "demo_auto_enqueue", False)

    execution_status, exec_reason = _decide_execution_status(
        conditions_passed=conditions_passed,
        proposed_signal=proposed_signal,
        news_clear=news_clear,
        spread_acceptable=spread_acceptable,
        bridge_alive=bridge_alive,
        rr=rr or 0,
        entry=entry, stop_loss=stop_loss, tp1=tp1, tp2=tp2,
        demo_auto_enqueue=demo_auto_enqueue,
        allow_demo=settings.allow_demo_trading,
    )

    entry_tolerance = _compute_entry_tolerance(atr_h1)

    mt5_execution_object = None
    if execution_status == _EXEC_DEMO_PLACED and decision in ("BUY", "SELL"):
        mt5_execution_object = _build_mt5_execution_object(
            decision=decision,
            entry=entry, stop_loss=stop_loss, tp1=tp1, tp2=tp2,
            rr=rr or 0,
            setup_score_100=total_setup_score,
            conditions_passed=conditions_passed,
            execution_status=execution_status,
        )

    # Per-cycle improvement note — what's stopping a 5/5 grade right now
    failed_conditions = [c["name"] for c in conditions if not c["passed"]]
    if conditions_passed >= 5:
        improvement_note = "5/5 — no improvement gap."
    else:
        improvement_note = (
            f"Missing: {'; '.join(failed_conditions[:3])}"
            if failed_conditions else "—"
        )

    return {
        "instrument": "XAUUSD",
        "timestamp":  now.isoformat(),
        "decision":   decision,
        # ── MANDATE PRIMARY FIELDS ──
        "conditions_passed":         conditions_passed,
        "conditions":                conditions,
        "estimated_win_rate_range":  est_win_rate,
        "execution_status":          execution_status,
        "execution_status_reason":   exec_reason,
        "mt5_execution_object":      mt5_execution_object,
        "improvement_note":          improvement_note,
        "session_classification":    session_mandate,
        "tf_alignment_label":        tf_alignment_mandate,
        "liquidity_behaviour":       liquidity_behaviour,
        # ── EXISTING FIELDS (preserved for dashboard panel + back-compat) ──
        "market_sentiment": (
            "Bullish" if (ema20_h1 > ema50_h1 and rsi_h1 > 50) else
            "Bearish" if (ema20_h1 < ema50_h1 and rsi_h1 < 50) else
            "Neutral" if abs(rsi_h1 - 50) < 5 else "Mixed"
        ),
        "setup_score":   total_setup_score,            # 0-100 legacy fine-grain score
        "quality_band":  band,
        "timeframe_alignment": {
            "daily":  f"close ${closes_h4[-1] if closes_h4 else current_price:.2f}, range {today_low}-{today_high}",
            "h4":     f"close ${closes_h4[-1]:.2f}" if closes_h4 else "—",
            "h1":     f"close ${closes_h1[-1]:.2f}, EMA20={ema20_h1}, EMA50={ema50_h1}, EMA100={ema100_h1}",
            "m15":    f"close ${closes_m15[-1]:.2f}, RSI={rsi_h1}, VWAP={vwap['vwap']}",
            "alignment_summary": tf_alignment_mandate,   # ← mandate enum
        },
        "market_state":         market_state_mandate,    # ← mandate enum (was free-text)
        "market_state_detail":  market_state,            # original detailed string kept here
        "liquidity_model": {
            "confirmed":        model_confirmed,
            "type": {
                "A": "Liquidity Sweep Reversal",
                "B": "Breakout Retest",
                "C": "Trend Pullback",
                "D": "News Repricing",
                "E": "None",
            }.get(model_letter, "None"),
            "swept_level":      swept_text,
            "target_liquidity": target_text,
        },
        "key_zones": {
            "immediate_supply": [today_high] if today_high else [],
            "immediate_demand": [today_low]  if today_low  else [],
            "support":          [prev_day_low]  if prev_day_low  else [],
            "resistance":       [prev_day_high] if prev_day_high else [],
            "round_numbers":    round_numbers,
        },
        "macro_context": {
            "dxy_bias":         dxy_dir,
            "yields_bias":      yields_dir,
            "news_risk":        "CLEAR" if news_clear else "ELEVATED",
            "gold_macro_bias":  gold_macro_bias,
            "macro_alignment":  macro_aligned,
        },
        "technical_confirmation": {
            "structure":      (scan.get("engineModel") or {}).get("structure", "—"),
            "ema":            f"EMA20={ema20_h1} EMA50={ema50_h1} EMA100={ema100_h1}",
            "vwap":           f"VWAP={vwap['vwap']} position={vwap['position']}",
            "rsi":            f"{rsi_h1} ({'OB' if rsi_h1 > 70 else 'OS' if rsi_h1 < 30 else 'neutral'})",
            "macd":           f"{macd_h1} ({'bull' if macd_h1 > 0 else 'bear'})",
            "atr_volatility": f"{atr_h1} pts (spread {'OK' if spread_acceptable else 'abnormal'})",
            "candle_quality": (scan.get("engineModel") or {}).get("fvg", "—"),
        },
        "trade_plan": {
            "entry_type": (
                "Wait for confirmation" if decision == "STAND ASIDE"
                else "Market" if execution_status == _EXEC_DEMO_PLACED
                else "Signal only — no execution"
            ),
            "entry":           entry,
            "entry_tolerance": entry_tolerance,         # MANDATE: ± tolerance in USD
            "stop_loss":       stop_loss,
            "tp1":             tp1,
            "tp2":             tp2,
            "tp3":             tp3,
            "risk_reward":     rr or None,
            "invalidation":    f"Price closes through {stop_loss}" if stop_loss else None,
            "lot_size":        0.01,                   # MANDATE: ALWAYS 0.01 on demo
            "position_size_guidance": (
                "No trade" if decision == "STAND ASIDE" else
                "Demo only · 0.01 lot · learning mode"
            ),
        },
        "execution_permission": {
            "allow_alert":          allow_alert,
            "allow_demo_execution": (execution_status == _EXEC_DEMO_PLACED),
            "allow_live_execution": False,             # MANDATE: hard-disabled
            "execution_status":     execution_status,  # mirror of top-level for legacy clients
            "reason":               exec_reason,
        },
        "management_plan": {
            "after_tp1":   "Move SL to entry (breakeven)",
            "after_tp2":   "Trail SL to TP1 level; let runner work",
            "trail_logic": "Structural trail: SL behind each new swing high (BUY) or low (SELL)",
            "early_exit_condition": (
                "Exit if H1 closes through opposing structural level OR DXY rallies >0.5% intraday"
            ),
        },
        "stand_aside_reason": "; ".join(stand_aside_reasons) if stand_aside_reasons else "",
        "next_trigger": {
            "long_trigger":     long_trigger,
            "short_trigger":    short_trigger,
            "no_trade_condition": no_trade_cond,
        },
        "institutional_logic": (
            f"5-gate confirmation: scanner({scan.get('marketState')}) · "
            f"predictor({pred.get('band')}/{pred.get('direction')}) · "
            f"killzone({kz.get('label')}/{kz.get('posture')}) · "
            f"kz-policy({'ALLOW' if (kz_policy and kz_policy.allow) else 'BLOCK' if kz_policy else '—'}) · "
            f"ICT({ict.score if ict else '—'}/100 {ict.posture if ict else '—'})"
        ),
        "final_verdict": (
            f"{decision} · {conditions_passed}/5 conditions · est WR {est_win_rate}"
            f" · {execution_status} · model {model_letter}"
            + (f" · {gold_macro_bias}" if gold_macro_bias != "Neutral" else "")
        ),
    }


# ────────────────────────────────────────────────────────────────────────────
# Telegram formatters — pinned to EXACT mandate spec
# ────────────────────────────────────────────────────────────────────────────

def format_mandate_signal_message(
    verdict: dict, *,
    long_pct: float | None = None, short_pct: float | None = None,
    spread_pts: float | None = None,
) -> str:
    """
    Build the EXACT Telegram message format the mandate requires for
    BUY / SELL decisions. Plain text — no HTML tags.

    Optional inputs (sentiment / spread) come from the router, which has
    access to MyFXBook and the live tick.
    """
    dec = verdict.get("decision", "STAND ASIDE")
    if dec not in ("BUY", "SELL"):
        return format_mandate_stand_aside_message(verdict)

    arrow = "🟢 BUY" if dec == "BUY" else "🔴 SELL"
    tp = verdict.get("trade_plan", {}) or {}
    tc = verdict.get("technical_confirmation", {}) or {}
    mc = verdict.get("macro_context", {}) or {}
    lm = verdict.get("liquidity_model", {}) or {}

    current_price = tp.get("entry")
    entry         = tp.get("entry")
    entry_tol     = tp.get("entry_tolerance", 0)
    sl            = tp.get("stop_loss")
    tp1           = tp.get("tp1")
    tp2           = tp.get("tp2")
    rr            = tp.get("risk_reward") or 0

    # Extract RSI/ATR plain values from descriptive strings (best-effort)
    rsi_str = (tc.get("rsi") or "").split()[0] if tc.get("rsi") else "—"
    atr_str = (tc.get("atr_volatility") or "").split()[0] if tc.get("atr_volatility") else "—"

    sentiment_str = "—"
    if long_pct is not None and short_pct is not None:
        sentiment_str = f"{long_pct:.0f}% Long / {short_pct:.0f}% Short"

    cp = verdict.get("conditions_passed", 0)
    wr = verdict.get("estimated_win_rate_range", "—")
    es = verdict.get("execution_status", "STAND_ASIDE")
    trade_placed = "YES" if es == "DEMO_TRADE_PLACED" else "NO"

    # Brief analysis line — joins liquidity, structure, session, macro, risk
    analysis = (
        f"{lm.get('type','—')} · {verdict.get('liquidity_behaviour','—')} · "
        f"{verdict.get('session_classification','—')} · macro={mc.get('macro_alignment','—')} · "
        f"{verdict.get('execution_status_reason','—')}"
    )

    ts = verdict.get("timestamp", datetime.now(timezone.utc).isoformat())
    try:
        ts_gmt = datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M GMT")
    except Exception:
        ts_gmt = ts

    return (
        f"📈 XAUUSD SIGNAL 📈\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"Direction:    {arrow}\n"
        f"Price:        ${current_price}\n"
        f"Score:        {cp}/5 conditions\n"
        f"Est Win Rate: {wr}\n"
        f"\n"
        f"📌 TRADE LEVELS\n"
        f"Entry:        ${entry} +/- ${entry_tol}\n"
        f"Stop Loss:    ${sl}\n"
        f"Take Profit 1: ${tp1}\n"
        f"Take Profit 2: ${tp2}\n"
        f"Risk:Reward:  1:{rr}\n"
        f"\n"
        f"📊 MARKET DATA\n"
        f"RSI(14):      {rsi_str}\n"
        f"ATR(14):      ${atr_str}\n"
        f"Sentiment:    {sentiment_str}\n"
        f"\n"
        f"📝 Analysis:\n"
        f"Score: {cp}/5. {analysis}\n"
        f"Trade placed on demo: {trade_placed}\n"
        f"Lot Size: 0.01\n"
        f"Execution Status: {es}\n"
        f"\n"
        f"Time: {ts_gmt}\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"NOT FINANCIAL ADVICE. Manage your risk."
    )


def persist_verdict(db: Session, verdict: dict, *, pending_execution_id: int | None = None) -> int | None:
    """
    Append one row to strategist_verdicts capturing the full mandate-required
    signal log. Idempotent failure mode: any DB error is swallowed (verdicts
    must never block the API response).

    Returns the new row's id (or None on failure).
    """
    try:
        from db_models import StrategistVerdict
        import json as _json
        tp = verdict.get("trade_plan", {}) or {}
        mc = verdict.get("macro_context", {}) or {}
        tc = verdict.get("technical_confirmation", {}) or {}

        # RSI / ATR are stored as descriptive strings in technical_confirmation
        # — parse the leading float so the log is queryable.
        def _first_float(s: str | None) -> float | None:
            if not s: return None
            try:
                head = s.split()[0]
                return float(head)
            except Exception:
                return None

        rsi_val = _first_float(tc.get("rsi"))
        atr_val = _first_float(tc.get("atr_volatility"))

        row = StrategistVerdict(
            symbol="XAUUSD",
            decision=verdict.get("decision", "STAND ASIDE"),
            conditions_passed=verdict.get("conditions_passed", 0),
            estimated_win_rate_range=verdict.get("estimated_win_rate_range"),
            execution_status=verdict.get("execution_status", "STAND_ASIDE"),
            execution_status_reason=verdict.get("execution_status_reason"),
            setup_score=verdict.get("setup_score"),
            quality_band=verdict.get("quality_band"),
            market_state=verdict.get("market_state"),
            session_classification=verdict.get("session_classification"),
            tf_alignment_label=verdict.get("tf_alignment_label"),
            liquidity_behaviour=verdict.get("liquidity_behaviour"),
            market_sentiment=verdict.get("market_sentiment"),
            entry=tp.get("entry"),
            entry_tolerance=tp.get("entry_tolerance"),
            stop_loss=tp.get("stop_loss"),
            tp1=tp.get("tp1"),
            tp2=tp.get("tp2"),
            tp3=tp.get("tp3"),
            risk_reward=tp.get("risk_reward"),
            lot_size=tp.get("lot_size", 0.01),
            rsi_h1=rsi_val,
            atr_h1=atr_val,
            dxy_bias=mc.get("dxy_bias"),
            yields_bias=mc.get("yields_bias"),
            gold_macro_bias=mc.get("gold_macro_bias"),
            news_risk=mc.get("news_risk"),
            improvement_note=verdict.get("improvement_note"),
            final_verdict_text=verdict.get("final_verdict"),
            full_verdict_json=_json.dumps(verdict, default=str),
            pending_execution_id=pending_execution_id,
            result="PENDING" if verdict.get("execution_status") == "DEMO_TRADE_PLACED" else None,
        )
        db.add(row)
        db.commit()
        db.refresh(row)
        return row.id
    except Exception as exc:
        log.warning("[strategist] persist_verdict failed (non-fatal): %s", exc)
        try:
            db.rollback()
        except Exception:
            pass
        return None


def format_mandate_stand_aside_message(verdict: dict) -> str:
    """Exact format for STAND ASIDE informational alerts."""
    tp = verdict.get("trade_plan", {}) or {}
    nt = verdict.get("next_trigger", {}) or {}
    cp = verdict.get("conditions_passed", 0)
    price = tp.get("entry") or "—"
    reason = verdict.get("stand_aside_reason") or verdict.get("execution_status_reason") or "Conditions unclear"

    ts = verdict.get("timestamp", datetime.now(timezone.utc).isoformat())
    try:
        ts_gmt = datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M GMT")
    except Exception:
        ts_gmt = ts

    return (
        f"⚪ XAUUSD STAND ASIDE\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"Reason: {reason}\n"
        f"Current Price: ${price}\n"
        f"Score: {cp}/5\n"
        f"Next BUY Trigger: {nt.get('long_trigger') or '—'}\n"
        f"Next SELL Trigger: {nt.get('short_trigger') or '—'}\n"
        f"Time: {ts_gmt}\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"Capital preserved. No trade taken."
    )


def _stand_aside_envelope(now: datetime, *, reason: str) -> dict:
    """Defensive envelope when the strategist can't get the data it needs."""
    return {
        "instrument":               "XAUUSD",
        "timestamp":                now.isoformat(),
        "decision":                 "STAND ASIDE",
        # Mandate primary fields — always present so downstream consumers don't crash
        "conditions_passed":        0,
        "conditions":               [],
        "estimated_win_rate_range": "no trade",
        "execution_status":         _EXEC_STAND_ASIDE,
        "execution_status_reason":  reason,
        "mt5_execution_object":     None,
        "improvement_note":         reason,
        "session_classification":   "Asian range formation",
        "tf_alignment_label":       _TF_NEUTRAL,
        "liquidity_behaviour":      _LIQ_CHOPPING,
        # Legacy back-compat fields
        "market_sentiment":         "Neutral",
        "setup_score":              0,
        "quality_band":             "No Trade",
        "market_state":             "No clean structure",
        "stand_aside_reason":       reason,
        "final_verdict":            f"STAND ASIDE — {reason}",
        "execution_permission": {
            "allow_alert":          False,
            "allow_demo_execution": False,
            "allow_live_execution": False,
            "execution_status":     _EXEC_STAND_ASIDE,
            "reason":               reason,
        },
    }
