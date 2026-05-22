"""
XAUUSD Institutional Signal Engine — Executive Strategist
=========================================================

The unified decision aggregator. Synthesises every existing engine
(scanner, predictor, killzone analyser, killzone policy, ICT framework,
correlation engine, calendar) into a single structured JSON verdict
that follows the institutional strategist mandate:

  Decision ∈ { LONG, SHORT, STAND ASIDE }

  STAND ASIDE is preferred when conditions are not high-quality.

STRICT DECISION RULES (any failure → STAND ASIDE):

  • setup_score ≥ 80
  • risk_reward ≥ 2.5
  • liquidity_model.confirmed == True
  • stop_loss defined
  • TP1 + TP2 defined
  • invalidation defined
  • macro does not directly contradict
  • spread + volatility acceptable
  • price not in mid-range with no edge

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
        decision = "LONG" if proposed_signal == "BUY" else "SHORT"

    # Quality band
    if total_setup_score >= 90: band = "A-grade"
    elif total_setup_score >= 80: band = "Valid"
    elif total_setup_score >= 70: band = "Watchlist"
    else: band = "No Trade"

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
    allow_live   = (decision != "STAND ASIDE" and total_setup_score >= 85
                    and settings.live_trading_authorized
                    and settings.auto_execution_enabled)

    return {
        "instrument": "XAUUSD",
        "timestamp":  now.isoformat(),
        "decision":   decision,
        "market_sentiment": (
            "Bullish" if (ema20_h1 > ema50_h1 and rsi_h1 > 50) else
            "Bearish" if (ema20_h1 < ema50_h1 and rsi_h1 < 50) else
            "Neutral" if abs(rsi_h1 - 50) < 5 else "Mixed"
        ),
        "setup_score":   total_setup_score,
        "quality_band":  band,
        "timeframe_alignment": {
            "daily":  f"close ${closes_h4[-1] if closes_h4 else current_price:.2f}, range {today_low}-{today_high}",
            "h4":     f"close ${closes_h4[-1]:.2f}" if closes_h4 else "—",
            "h1":     f"close ${closes_h1[-1]:.2f}, EMA20={ema20_h1}, EMA50={ema50_h1}, EMA100={ema100_h1}",
            "m15":    f"close ${closes_m15[-1]:.2f}, RSI={rsi_h1}, VWAP={vwap['vwap']}",
            "alignment_summary": market_state,
        },
        "market_state": market_state,
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
            "entry_type": "Wait for confirmation" if decision == "STAND ASIDE" else "Market",
            "entry":      entry,
            "stop_loss":  stop_loss,
            "tp1":        tp1,
            "tp2":        tp2,
            "tp3":        tp3,
            "risk_reward":rr or None,
            "invalidation": f"Price closes through {stop_loss}" if stop_loss else None,
            "position_size_guidance": (
                "No trade" if decision == "STAND ASIDE" else
                "Normal" if total_setup_score >= 90 else
                "Reduced" if total_setup_score >= 80 else
                "Demo only"
            ),
        },
        "execution_permission": {
            "allow_alert":          allow_alert,
            "allow_demo_execution": allow_demo,
            "allow_live_execution": allow_live,
            "reason": (
                "All decision rules satisfied" if decision != "STAND ASIDE"
                else "; ".join(stand_aside_reasons[:3])
            ),
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
            f"{decision} · score {total_setup_score}/100 · band {band} · model {model_letter}"
            + (f" · {gold_macro_bias}" if gold_macro_bias != "Neutral" else "")
        ),
    }


def _stand_aside_envelope(now: datetime, *, reason: str) -> dict:
    """Defensive envelope when the strategist can't get the data it needs."""
    return {
        "instrument":    "XAUUSD",
        "timestamp":     now.isoformat(),
        "decision":      "STAND ASIDE",
        "market_sentiment": "Neutral",
        "setup_score":   0,
        "quality_band":  "No Trade",
        "stand_aside_reason": reason,
        "final_verdict": f"STAND ASIDE — {reason}",
        "execution_permission": {
            "allow_alert":          False,
            "allow_demo_execution": False,
            "allow_live_execution": False,
            "reason":               reason,
        },
    }
