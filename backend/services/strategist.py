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
  C2  Killzone × direction has positive empirical expectancy
       (replaces v1's "liquidity sweep confirmed" check, which had ZERO
        predictive value in the 893-trade backtest. The empirical edge
        filter is what actually separates winning cells from losers.)
  C3  Structure / momentum confirms direction
  C4  Macro & session context does not conflict
  C5  Risk-reward + invalidation are acceptable

  5/5 → A-grade demo execution allowed   (~19% WR · +0.09R asymmetric)
  4/5 → Valid demo execution allowed     (~19% WR · +0.05R asymmetric)
  3/5 → Watchlist only, no execution
 ≤2/5 → STAND ASIDE

  Edge is R-multiple driven (2.5R reward / 1R risk), not high accuracy.
  Expect long losing streaks; expectancy is positive over many trades.

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


# Estimated expectancy strings — replaced the mandate's predicted WR bands
# with empirical values from the 893-trade backtest (2025-11 → 2026-05).
# The engine's edge is R-multiple driven (2.5R reward / 1R risk), not high
# accuracy — observed WR clusters around 18-22% regardless of N/5 band,
# but positive expectancy per trade. Honest framing matters for operator
# psychology (long losing streaks are NORMAL).
def _estimate_win_rate(passed: int) -> str:
    if passed >= 5: return "~22% WR · +0.17R (STRONG-TF · pullback zone · momentum-aligned)"
    if passed >= 4: return "~21% WR · +0.05R (positive-expectancy after pullback filter)"
    if passed >= 3: return "watchlist · no exec · no alert"
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


# Sessions where C4 must refuse regardless of what kz_posture says.
# Empirically derived: late_SELL n=3 WR=0% expectancy -1.00R (every late-session
# trade hit full SL during the 2026-05 / 2026-06 data sample). Low-volume
# windows have wider spreads and thin order books — stops get tagged easily.
_NEVER_TRADE_SESSIONS = {
    "Late-session low-quality liquidity",
    "Post-news disorder",
    "Asian range formation",      # Asian = directional moves usually faded by London
}


def _detect_cisd(direction: str, candles_m15, lookback: int = 8) -> tuple[bool, str]:
    """
    Change in State of Delivery detection (ICT sniper confirmation).

    Bullish CISD (BUY): find the most recent M15 bar that CLOSED bearish
    (close < open). Then check whether any subsequent bar within the
    lookback window CLOSED ABOVE that bar's HIGH. A body-close through
    the last down-candle's high = decisive break of bearish delivery =
    sniper confirmation to buy.

    Bearish CISD (SELL): mirror. Find the last bullish M15 (close > open),
    check for any subsequent CLOSE BELOW its LOW.

    Why this matters: our current sweep+reclaim check is loose — any
    close back inside prev-day range counts. CISD is tighter: it requires
    the confirming candle to CLOSE through opposing structure, not just
    wick or partially retrace. This is what disciplined ICT/SMC snipers
    (Nephew Sam, Michael Fanning) wait for before pulling the trigger on
    a reversal.

    Returns (confirmed, rationale).
    """
    if not candles_m15 or len(candles_m15) < 3:
        return (False, "insufficient M15 bars")

    recent = candles_m15[-lookback:]
    n = len(recent)

    if direction == "BUY":
        # Walk backwards, find most recent bearish close, then check if any
        # bar after it closed above its high.
        for i in range(n - 2, -1, -1):
            if recent[i].close < recent[i].open:
                pivot_high = recent[i].high
                for j in range(i + 1, n):
                    if recent[j].close > pivot_high:
                        return (True, f"CISD ✓ close {recent[j].close:.2f} > pivot high {pivot_high:.2f}")
                return (False, f"CISD ✗ no close above pivot high {pivot_high:.2f}")
        return (False, "CISD ✗ no recent bearish pivot in window")

    if direction == "SELL":
        for i in range(n - 2, -1, -1):
            if recent[i].close > recent[i].open:
                pivot_low = recent[i].low
                for j in range(i + 1, n):
                    if recent[j].close < pivot_low:
                        return (True, f"CISD ✓ close {recent[j].close:.2f} < pivot low {pivot_low:.2f}")
                return (False, f"CISD ✗ no close below pivot low {pivot_low:.2f}")
        return (False, "CISD ✗ no recent bullish pivot in window")

    return (False, "no direction")


def _micro_momentum_aligned(direction: str, candles_m15, lookback: int = 3) -> tuple[bool, str]:
    """
    Bob Volman signal-bar rule: for a BUY, the majority of the last N M15
    bars must close ABOVE their open (real body up). For a SELL, majority
    close BELOW open. This blocks entries against short-term momentum —
    the immediate cause of most stop-outs.

    Returns (passed, detail_string).
    """
    if not candles_m15 or len(candles_m15) < lookback:
        return (False, f"insufficient M15 bars ({len(candles_m15) if candles_m15 else 0}<{lookback})")
    recent = candles_m15[-lookback:]
    up_bars   = sum(1 for c in recent if c.close > c.open)
    down_bars = sum(1 for c in recent if c.close < c.open)
    if direction == "BUY":
        ok = up_bars >= (lookback + 1) // 2 + 1 if lookback >= 3 else up_bars > down_bars
        # For lookback=3, need at least 2 of 3 up
        ok = up_bars >= 2 if lookback == 3 else up_bars > down_bars
        return (ok, f"last {lookback} M15: {up_bars}▲/{down_bars}▼ (need ≥2▲ for BUY)")
    if direction == "SELL":
        ok = down_bars >= 2 if lookback == 3 else down_bars > up_bars
        return (ok, f"last {lookback} M15: {up_bars}▲/{down_bars}▼ (need ≥2▼ for SELL)")
    return (False, "no direction")


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
    session_label: str | None,
    rr: float,
    entry: float | None,
    stop_loss: float | None,
    tp1: float | None,
    tp2: float | None,
    kz_policy: Any | None = None,    # PolicyVerdict from killzone_policy.evaluate()
    candles_m15: list | None = None, # for micro-momentum check in C3
    sweep: dict | None = None,       # NEW: sweep dict for CISD reversal gate in C3
) -> list[dict]:
    """
    Score the 5 mandate conditions. Each entry: {name, passed, detail}.
    """
    is_buy  = proposed_signal == "BUY"
    is_sell = proposed_signal == "SELL"

    # C1: Timeframe alignment supports direction.
    # STRONG aligned only. "Extended" was previously accepted as a pass
    # (with the rationalisation that C2/C4 would catch bad setups) — in
    # practice, "Bullish but extended" = D1+H4 bull AND H1 EMA20>50 AND
    # RSI≥70. That is a textbook overbought reading; entering long there
    # is chasing. Same for "Bearish but extended" (oversold). Empirical
    # noise-reduction pass: fewer 4/5 fires from stretched setups, and
    # the ones that remain are aligned trends, not late entries.
    c1_ok = False
    if is_buy  and tf_alignment_label == _TF_STRONG_BULL:
        c1_ok = True
    elif is_sell and tf_alignment_label == _TF_STRONG_BEAR:
        c1_ok = True

    # C2: Empirical (killzone × direction) edge filter
    # ────────────────────────────────────────────────
    # Replaces the legacy "model_letter in A/B/C/D" check which in the
    # 893-trade backtest had ZERO predictive value (5/5 and 4/5 produced
    # identical WR because model_confirmed never differentiates winners
    # from losers in this engine's structure).
    #
    # The new C2 asks: in this specific killzone, has this direction
    # produced positive expectancy historically? That's the question that
    # actually matters for whether the next firing is likely to print
    # green. ALLOW or EXPLORE pass; BLOCK fails.
    #
    # Fallback: if kz_policy is None (no current killzone or upstream
    # failure), keep the legacy model-letter check so we don't go dark.
    if kz_policy is not None:
        c2_ok = kz_policy.decision in ("ALLOW", "EXPLORE")
        c2_detail = (
            f"kz-policy {kz_policy.decision} · {kz_policy.killzone} "
            f"{kz_policy.direction} · n={kz_policy.sample_size} · "
            f"WR={kz_policy.historical_wr:.1f}% · "
            f"ExpR={kz_policy.historical_exp_r:+.2f}"
        )
    else:
        c2_ok = model_confirmed and model_letter in ("A", "B", "C", "D")
        c2_detail = f"Model {model_letter} · confirmed={model_confirmed} (fallback)"

    # C3: Structure confluence AND micro-momentum AND (CISD if reversal)
    # ────────────────────────────────────────────────────────────────
    # Three-layer check:
    #   1. structure_ok  — SIGNAL_READY OR ict>=60 (setup type recognized)
    #   2. momentum_ok   — Bob Volman: last 3 M15 majority in trade direction
    #   3. cisd_ok       — ICT sniper: if a sweep was reclaimed (reversal
    #                       setup), require CISD confirmation (body-close
    #                       through last opposing candle's structure).
    #                       Continuation setups (no sweep) skip this check.
    #
    # CISD gates ONLY reversal setups because that's what it was designed
    # for. Applying it to trend continuations would over-fire — there's
    # no reversal to confirm.
    structure_ok = (scan_market_state == "SIGNAL_READY") or (ict_score >= 60)
    if candles_m15 is not None:
        momentum_ok, momentum_detail = _micro_momentum_aligned(proposed_signal, candles_m15, lookback=3)
    else:
        momentum_ok, momentum_detail = (True, "M15 unavailable — skipped")

    is_reversal = bool(sweep and sweep.get("swept") and sweep.get("reclaimed"))
    if is_reversal and candles_m15 is not None:
        cisd_ok, cisd_detail = _detect_cisd(proposed_signal, candles_m15, lookback=8)
    else:
        cisd_ok, cisd_detail = (True, "continuation setup — CISD n/a" if not is_reversal else "M15 unavailable")

    c3_ok = structure_ok and momentum_ok and cisd_ok

    # C4: Macro / session does not conflict
    # Two-layer session check:
    #   1. kz_posture must be TRADE or PRESS (analyzer's recent-data view)
    #   2. session_label must NOT be in the empirically-bad session list
    # Either layer can veto. The session_label check catches cases where
    # the kz_analyzer hasn't yet learned a session is unprofitable —
    # e.g., the 3 late_SELL losses in our first 18-trade sample.
    session_ok = (session_label or "") not in _NEVER_TRADE_SESSIONS
    c4_ok = (
        macro_alignment != "Conflicted"
        and news_clear
        and kz_posture in ("TRADE", "PRESS")
        and session_ok
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
        {"name": "C2 Killzone × direction edge (empirical)",   "passed": c2_ok,
         "detail": c2_detail},
        {"name": "C3 Structure + momentum + CISD (reversal-gated)", "passed": c3_ok,
         "detail": f"scanner={scan_market_state} · ict={ict_score}/100 · {momentum_detail} · {cisd_detail}"},
        {"name": "C4 Macro / session does not conflict",        "passed": c4_ok,
         "detail": (
             f"macro={macro_alignment} · news={'CLEAR' if news_clear else 'BLOCK'} · "
             f"kz={kz_posture or '—'} · session={'OK' if session_ok else 'BLOCKED('+ (session_label or '')[:30] +')'}"
         )},
        {"name": "C5 RR + invalidation acceptable",             "passed": c5_ok,
         "detail": f"rr={rr or 0:.2f} · SL={stop_loss} · TP1={tp1} · TP2={tp2}"},
    ]


# Execution-status decider — produces ONE of the mandate enum values
_EXEC_SIGNAL_ONLY      = "SIGNAL_ONLY"
_EXEC_DEMO_PLACED      = "DEMO_TRADE_PLACED"
_EXEC_DEMO_REJECTED    = "DEMO_TRADE_REJECTED"
_EXEC_STAND_ASIDE      = "STAND_ASIDE"
_EXEC_BRIDGE_OFFLINE   = "BRIDGE_OFFLINE"
_EXEC_SPREAD_HIGH      = "SPREAD_TOO_HIGH"
_EXEC_NEWS_BLOCKED     = "NEWS_RISK_BLOCKED"
_EXEC_INVALIDATED      = "INVALIDATED_BEFORE_ENTRY"
_EXEC_POSITION_CAP     = "POSITION_CAP_REACHED"   # ← risk gate: too many open trades
_EXEC_MONDAY_OBSERVE   = "MONDAY_OBSERVE"         # ← Monday is observation-only per operator risk plan


def _volume_confirms_continuation(h1_candles, window: int = 20, recent: int = 3,
                                  ratio_threshold: float = 1.2) -> tuple[bool, float]:
    """
    True if the average volume of the last `recent` H1 bars is at least
    `ratio_threshold` × the median volume of the prior `window` bars.

    Volume above norm = institutional participation = trend continuation
    is more likely than a retail-driven retrace.

    Returns (passes, observed_ratio). observed_ratio is the actual recent-avg
    over prior-median so the verdict can surface it for transparency.
    """
    if not h1_candles or len(h1_candles) < window + recent:
        return (False, 0.0)
    recent_vols = [c.volume for c in h1_candles[-recent:]]
    prior_vols  = sorted(c.volume for c in h1_candles[-(window + recent):-recent])
    if not recent_vols or not prior_vols:
        return (False, 0.0)
    prior_median = prior_vols[len(prior_vols) // 2]
    if prior_median <= 0:
        return (False, 0.0)
    recent_avg = sum(recent_vols) / len(recent_vols)
    ratio = recent_avg / prior_median
    return (ratio >= ratio_threshold, round(ratio, 2))


def _compute_dynamic_cap(
    *,
    base_cap: int,
    extended_cap: int,
    floating_pnl: float,
    profit_threshold: float,
    tf_alignment_label: str,
    market_state: str,
    volume_passes: bool,
) -> tuple[int, bool, list[str]]:
    """
    Decide today's effective position cap (5 normally, up to 10 on confirmed
    trend continuation past the profit threshold).

    Returns (effective_cap, extended_active, reasons_failed).
    `reasons_failed` is empty when extended is active; otherwise lists each
    condition that didn't pass — surfaced in the verdict for transparency.
    """
    reasons: list[str] = []

    if floating_pnl < profit_threshold:
        reasons.append(f"floating P&L ${floating_pnl:+.0f} < ${profit_threshold:.0f} threshold")

    trend_strong = tf_alignment_label in (_TF_STRONG_BULL, _TF_STRONG_BEAR)
    if not trend_strong:
        reasons.append(f"TF alignment '{tf_alignment_label}' is not Strong bullish/bearish")

    trending_state = market_state in (_MARKET_STATE_TRENDING_BULL, _MARKET_STATE_TRENDING_BEAR)
    if not trending_state:
        reasons.append(f"market state '{market_state}' is not trending")

    if not volume_passes:
        reasons.append("recent H1 volume below 1.2× prior-20 median")

    if reasons:
        return (base_cap, False, reasons)
    return (extended_cap, True, [])


def _get_open_positions_snapshot() -> dict:
    """
    Read the MT5 open-position count + ticket list from the bridge heartbeat.
    Counts only fresh daemons (≤120s) so a stale daemon's stale count can't
    falsely block legitimate signals.

    Returns:
      count   — int (0 if no fresh daemon)
      tickets — list[int]
      floating_pnl — float (sum of unrealized P&L across all positions)
      source_daemon — daemon id whose snapshot we used (None if no data)
    """
    try:
        from routers.bridge import _BRIDGE_HEARTBEAT, _MT5_TERMINAL_STATE
        now = datetime.now(timezone.utc)
        # Pick the freshest fresh daemon (multiple shouldn't happen in practice)
        best = None
        best_age = None
        for did, ts in _BRIDGE_HEARTBEAT.items():
            age = (now - ts).total_seconds()
            if age > 120: continue
            if best_age is None or age < best_age:
                best, best_age = did, age
        if not best:
            return {"count": 0, "tickets": [], "floating_pnl": 0.0, "source_daemon": None}
        state = _MT5_TERMINAL_STATE.get(best, {}) or {}
        return {
            "count":         state.get("open_positions_count") or 0,
            "tickets":       state.get("open_position_tickets") or [],
            "floating_pnl":  state.get("floating_pnl") or 0.0,
            "source_daemon": best,
        }
    except Exception:
        return {"count": 0, "tickets": [], "floating_pnl": 0.0, "source_daemon": None}


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
    open_positions_count: int = 0,
    max_concurrent_positions: int = 5,
    kz_policy: Any | None = None,    # PolicyVerdict — informational only (see note)
) -> tuple[str, str]:
    """
    Pick the execution_status value. Returns (status, reason).

    Mandate precedence:
      1. STAND_ASIDE         — score below 3/5, or no direction
      2. POSITION_CAP_REACHED — at the hard concurrent-position ceiling
      3. NEWS_RISK_BLOCKED / SPREAD_TOO_HIGH / BRIDGE_OFFLINE
                             — clean setup but execution conditions fail
      4. SIGNAL_ONLY         — 3/5 watchlist, or 4-5/5 but enqueue disabled / RR<1.5
      5. DEMO_TRADE_PLACED   — all gates pass
      (DEMO_TRADE_REJECTED + INVALIDATED_BEFORE_ENTRY are set post-fact
      by the bridge / monitor — not by this function.)

    NOTE on kz_policy: kept as a kwarg for forward-compat / future
    recalibration. NOT used as a hard execution gate at this time —
    the current POLICY_TABLE was learned from data that pre-dates the
    _NEVER_TRADE_SESSIONS blacklist, so its BLOCK/EXPLORE labels
    over-fire on the post-blacklist trade pool (validated: hard-gating
    on it dropped ExpR from +0.018R to -0.007R in re-run backtest).
    C2 uses kz_policy as a soft signal for transparency; the actual
    execution decision relies on the other 4 conditions + external gates.
    Re-enable as a hard gate once the table is re-learned from post-
    blacklist live data.
    """
    if proposed_signal not in ("BUY", "SELL") or conditions_passed < 3:
        return _EXEC_STAND_ASIDE, "Setup below 3/5 conditions"

    if conditions_passed == 3:
        return _EXEC_SIGNAL_ONLY, "Watchlist — 3/5 (no demo execution)"

    # 4-5/5 from here on — check execution gates

    # MONDAY OBSERVATION GATE — checked FIRST so it's the clearest signal in
    # the verdict. Signal alert still fires (decision = BUY/SELL); just no
    # MT5 enqueue. Operator studies Monday for weekly direction; trades from
    # Tuesday onward.
    from services.strategist_runner import is_monday_observation
    if is_monday_observation():
        return _EXEC_MONDAY_OBSERVE, (
            "Monday observation only — execution resumes Tuesday 00:00 UTC"
        )

    # POSITION-CAP GATE — risk management before anything else for 4/5+
    # When at the ceiling, the operator must review/close existing trades
    # before new ones can enter. Prevents overtrading during trending periods.
    if open_positions_count >= max_concurrent_positions:
        return _EXEC_POSITION_CAP, (
            f"At hard cap of {max_concurrent_positions} open positions — "
            f"review/close existing trades before new entry"
        )

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
# Self-sufficient helpers — strategist no longer depends on scanner.SIGNAL_READY
# ────────────────────────────────────────────────────────────────────────────

def _htf_bias_label(closes: list[float], lookback: int = 50) -> str:
    """
    "Bullish (HH/HL)" / "Bearish (LH/LL)" / "Neutral" — derived directly from
    EMAs, no scanner dependency. Mirrors the scanner's engineModel.* labels
    so downstream code that searches for "bull"/"bear" substrings still works.
    """
    if not closes or len(closes) < lookback:
        return "Insufficient data"
    ema20 = _ema(closes, 20)
    ema50 = _ema(closes, min(lookback, 50))
    last  = closes[-1]
    if last > ema20 > ema50:    return "Bullish (HH/HL)"
    if last < ema20 < ema50:    return "Bearish (LH/LL)"
    if last > ema20:            return "Bullish (mixed)"
    if last < ema20:            return "Bearish (mixed)"
    return "Neutral"


def _derive_direction_from_htf(
    *, d1_bias: str, h4_bias: str, h1_ema20: float, h1_ema50: float,
) -> tuple[str, str]:
    """
    Propose BUY/SELL/WAIT from HTF EMA alignment alone. Used as the fallback
    when both the scanner and the predictor return WAIT — the strategist
    must still have the courage to identify a directional thesis.

    Voting rule: need at least 2 of 3 timeframes aligned with the same side.
    """
    d = (d1_bias or "").lower()
    h = (h4_bias or "").lower()
    h1_bull = h1_ema20 is not None and h1_ema50 is not None and h1_ema20 > h1_ema50
    h1_bear = h1_ema20 is not None and h1_ema50 is not None and h1_ema20 < h1_ema50

    bull_votes = ("bull" in d) + ("bull" in h) + bool(h1_bull)
    bear_votes = ("bear" in d) + ("bear" in h) + bool(h1_bear)

    if bull_votes >= 2 and bull_votes > bear_votes:
        return ("BUY",  f"HTF aligned bullish (D1={d or '—'}, H4={h or '—'}, H1={'bull' if h1_bull else 'mixed'})")
    if bear_votes >= 2 and bear_votes > bull_votes:
        return ("SELL", f"HTF aligned bearish (D1={d or '—'}, H4={h or '—'}, H1={'bear' if h1_bear else 'mixed'})")
    return ("WAIT", f"HTF conflicted (bull={bull_votes}, bear={bear_votes})")


def _detect_liquidity_sweep(
    *, candles_m15: list, candles_d1: list, lookback_m15_bars: int = 16,
) -> dict:
    """
    Detect a sweep-and-reclaim of the previous calendar day's high or low
    using recent M15 bars. The reclaim half is what the mandate's Model A
    actually needs to confirm — a sweep alone isn't enough.

    Returns:
      swept     — bool: did price wick through prev-day H/L within lookback
      side      — "high" | "low" | None
      level     — the swept price level
      reclaimed — bool: did price close back inside the prev-day range
      rationale — human-readable summary
    """
    if (not candles_m15 or len(candles_m15) < lookback_m15_bars
            or not candles_d1 or len(candles_d1) < 2):
        return {"swept": False, "side": None, "level": None,
                "reclaimed": False, "rationale": "Insufficient data"}

    prev_d1   = candles_d1[-2]
    prev_high = prev_d1.high
    prev_low  = prev_d1.low

    recent  = candles_m15[-lookback_m15_bars:]
    cur     = candles_m15[-1].close

    # Wick-only sweep (high wick must exceed prev_high; body close optional)
    high_swept = any(c.high > prev_high for c in recent)
    low_swept  = any(c.low  < prev_low  for c in recent)

    # Reclaim = current price is back inside the prev-day range AFTER the sweep
    high_reclaimed = high_swept and cur < prev_high
    low_reclaimed  = low_swept  and cur > prev_low

    if high_reclaimed:
        return {"swept": True, "side": "high", "level": round(prev_high, 2),
                "reclaimed": True,
                "rationale": f"Swept prev-day high ${prev_high:.2f} → reclaimed below (current ${cur:.2f})"}
    if low_reclaimed:
        return {"swept": True, "side": "low", "level": round(prev_low, 2),
                "reclaimed": True,
                "rationale": f"Swept prev-day low ${prev_low:.2f} → reclaimed above (current ${cur:.2f})"}
    if high_swept:
        return {"swept": True, "side": "high", "level": round(prev_high, 2),
                "reclaimed": False,
                "rationale": f"Swept prev-day high ${prev_high:.2f}, no reclaim yet"}
    if low_swept:
        return {"swept": True, "side": "low", "level": round(prev_low, 2),
                "reclaimed": False,
                "rationale": f"Swept prev-day low ${prev_low:.2f}, no reclaim yet"}
    return {"swept": False, "side": None, "level": None,
            "reclaimed": False, "rationale": "No prev-day H/L sweep in recent bars"}


def _generate_trade_plan(
    *, direction: str, current_price: float, atr_h1: float,
    candles_m15: list,
    h1_ema20: float | None = None,   # NEW: pullback-zone anchor
    sl_atr_mult: float = 1.5,
    tp_r_multiples: tuple = (1.5, 2.5, 4.0),
    swing_lookback: int = 12,
    pullback_atr_max: float = 0.6,   # NEW: reject entries > N*ATR from EMA20
) -> dict:
    """
    Generate a self-contained ATR-based trade plan when the scanner doesn't
    provide one. SL anchored to recent swing high/low (whichever is the
    invalidation side) capped to ATR×mult so we don't over-stretch on quiet days.

    PULLBACK GATE: if h1_ema20 is provided and the current price is more than
    `pullback_atr_max × atr_h1` beyond EMA20 in the trade direction, return
    entry=None. Rationale: Al Brooks / Bob Volman / every pro trader — in a
    strong trend, wait for the pullback into the 20 EMA. Chasing an extended
    move is the #1 cause of stop-outs. This surgical filter attacks the 42%
    LOSS rate in the backtest directly.

    Returns dict with entry / stop_loss / tp1 / tp2 / tp3 / rr / risk_pts.
    """
    if direction not in ("BUY", "SELL") or not current_price or atr_h1 <= 0:
        return {"entry": None, "stop_loss": None, "tp1": None, "tp2": None,
                "tp3": None, "rr": 0, "risk_pts": 0, "source": "none"}

    # Pullback-zone gate: reject chase entries
    if h1_ema20 is not None and h1_ema20 > 0:
        distance_atr = (current_price - h1_ema20) / atr_h1
        if direction == "BUY" and distance_atr > pullback_atr_max:
            # Price is far ABOVE EMA20 — buying here is chasing an uptrend
            return {"entry": None, "stop_loss": None, "tp1": None, "tp2": None,
                    "tp3": None, "rr": 0, "risk_pts": 0, "source": "chase_rejected",
                    "rejection": f"BUY chased: {distance_atr:.2f}×ATR above EMA20 "
                                 f"(max {pullback_atr_max}) — wait for pullback"}
        if direction == "SELL" and distance_atr < -pullback_atr_max:
            # Price is far BELOW EMA20 — selling here is chasing a downtrend
            return {"entry": None, "stop_loss": None, "tp1": None, "tp2": None,
                    "tp3": None, "rr": 0, "risk_pts": 0, "source": "chase_rejected",
                    "rejection": f"SELL chased: {abs(distance_atr):.2f}×ATR below EMA20 "
                                 f"(max {pullback_atr_max}) — wait for pullback"}

    # ATR bounds — never risk less than 15pts (noise floor), never more than 80pts
    sl_dist_atr = max(min(atr_h1 * sl_atr_mult, 80.0), 15.0)

    # Recent swing anchor
    recent = candles_m15[-swing_lookback:] if candles_m15 and len(candles_m15) >= swing_lookback else (candles_m15 or [])
    swing_low  = min((c.low  for c in recent), default=None) if recent else None
    swing_high = max((c.high for c in recent), default=None) if recent else None

    if direction == "BUY":
        # SL = MAX of (recent swing low - buffer) and (entry - ATR distance)
        #   i.e. take the wider of the two so we don't get knocked out by noise
        sl_swing = (swing_low - 1.0) if swing_low else (current_price - sl_dist_atr)
        sl_atr   = current_price - sl_dist_atr
        sl       = min(sl_swing, sl_atr)         # wider of the two = lower price
        rr_unit  = current_price - sl
        if rr_unit < 5.0:                         # SL too tight, expand
            rr_unit = sl_dist_atr
            sl      = current_price - rr_unit
        tp1 = round(current_price + rr_unit * tp_r_multiples[0], 2)
        tp2 = round(current_price + rr_unit * tp_r_multiples[1], 2)
        tp3 = round(current_price + rr_unit * tp_r_multiples[2], 2)
    else:    # SELL
        sl_swing = (swing_high + 1.0) if swing_high else (current_price + sl_dist_atr)
        sl_atr   = current_price + sl_dist_atr
        sl       = max(sl_swing, sl_atr)
        rr_unit  = sl - current_price
        if rr_unit < 5.0:
            rr_unit = sl_dist_atr
            sl      = current_price + rr_unit
        tp1 = round(current_price - rr_unit * tp_r_multiples[0], 2)
        tp2 = round(current_price - rr_unit * tp_r_multiples[1], 2)
        tp3 = round(current_price - rr_unit * tp_r_multiples[2], 2)

    return {
        "entry":     round(current_price, 2),
        "stop_loss": round(sl, 2),
        "tp1":       tp1,
        "tp2":       tp2,
        "tp3":       tp3,
        "rr":        round(tp_r_multiples[1], 2),   # primary RR = to TP2
        "risk_pts":  round(rr_unit, 2),
        "source":    "strategist_atr",
    }


# ────────────────────────────────────────────────────────────────────────────
# Execution-model classifier
# ────────────────────────────────────────────────────────────────────────────

def _classify_execution_model(
    *, scan: dict, ict: dict, news_clear: bool,
    sweep: dict | None = None, ict_score: int = 0,
) -> tuple[str, bool, str, str]:
    """
    Identify which institutional execution model the current setup fits:
      A — Liquidity Sweep Reversal
      B — Breakout Retest Continuation
      C — Trend Pullback
      D — News Repricing Continuation
      E — None / Stand Aside
    Returns (model_name, confirmed, swept_level_str, target_str).

    Mandate refactor: Model A is now confirmed by the strategist's OWN
    sweep detection (services.strategist._detect_liquidity_sweep) when the
    scanner is in WATCHLIST / not SIGNAL_READY. Previously gated only on
    scanner.SIGNAL_READY which almost never fires on real markets.
    """
    market_state = scan.get("marketState", "")
    eng_model    = scan.get("engineModel", {}) or {}
    liq_text     = (eng_model.get("liquidity") or "").lower()
    struct_text  = (eng_model.get("structure") or "").lower()
    fvg_text     = (eng_model.get("fvg") or "").lower()
    bias         = (scan.get("institutionalBias") or "").lower()
    sweep        = sweep or {}

    # MODEL A: Sweep + reclaim — independent of scanner state.
    # Confirmation tiers:
    #   - strategist sweep+reclaim AND (scanner BOS/CHoCH OR ICT score ≥ 60) → CONFIRMED
    #   - scanner-reported sweep + BOS + SIGNAL_READY                        → CONFIRMED (legacy path)
    #   - sweep alone (no reclaim, no structure)                             → unconfirmed
    bos   = "bos" in struct_text or "choch" in struct_text
    if sweep.get("swept") and sweep.get("reclaimed"):
        if bos or ict_score >= 60:
            return ("A", True, sweep.get("rationale", "Sweep + reclaim"), "back inside prev-day range")
        return ("A", False, sweep.get("rationale", "Sweep + reclaim"), "awaiting structure confirmation")
    # Legacy: scanner-driven confirmation
    legacy_swept = ("swept" in liq_text or "sweep" in liq_text) and "no liquidity" not in liq_text
    if legacy_swept and bos and market_state == "SIGNAL_READY":
        return ("A", True, liq_text[:80], "next liquidity pool")

    # MODEL B: Breakout + retest (still requires scanner signal — relies on FVG zone detection)
    if "retest" in struct_text or "retest" in fvg_text:
        confirmed = market_state == "SIGNAL_READY" or ict_score >= 70
        return ("B", confirmed, struct_text[:80] or fvg_text[:80], "continuation target")

    # MODEL C: Trend pullback (FVG-in-zone retest)
    if "reversal" in bias and "in zone" in fvg_text:
        confirmed = market_state == "SIGNAL_READY" or ict_score >= 70
        return ("C", confirmed, fvg_text[:80], "trend liquidity")

    # MODEL D: Post-news continuation — never confirmed during the window
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

    # ── Independent timeframe biases from candle EMAs (not scanner) ─────
    # The scanner has been returning "Insufficient D1 data" for D1 bias in
    # production. Compute our own from raw candles so the strategist never
    # depends on the scanner's HTF read.
    d1_closes = [c.close for c in d1] if d1 else []
    h4_closes = [c.close for c in h4] if h4 else []
    d1_bias_local = _htf_bias_label(d1_closes, lookback=20)
    h4_bias_local = _htf_bias_label(h4_closes, lookback=50)

    # ── Detect liquidity sweep directly from candles (not scanner) ──────
    sweep = _detect_liquidity_sweep(candles_m15=m15, candles_d1=d1)
    log.debug("[strategist] sweep detection: %s", sweep.get("rationale"))

    # ── Direction proposal — scanner > predictor. NO HTF FALLBACK. ──────
    # Previously, when both scanner and predictor abstained (WAIT), the
    # strategist derived a direction from raw HTF EMA gaps. That was a
    # major noise source: it fired signals in low-conviction chop where
    # neither upstream engine had a view. Now we respect the abstention —
    # no confluence, no proposal, no alert.
    proposed_signal = scan.get("signal") or pred.get("direction") or "WAIT"
    direction_source = ("scanner"   if scan.get("signal")   in ("BUY", "SELL")
                        else "predictor" if pred.get("direction") in ("BUY", "SELL")
                        else None)

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
            # IMPORTANT: engine_id was "swing" which auto-bypasses the policy
            # table — making kz_policy.allow always True. Switch to
            # "trend_pullback" so the actual learned (killzone × direction)
            # row is consulted. This is the empirical edge filter that
            # backed C2's replacement in the 5-condition gate.
            kz_policy = eval_kz_policy(
                killzone_key=kz.get("current_kz", "unknown"),
                direction=proposed_signal,
                engine_id="trend_pullback",
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
    # Sanitize D1 first so a single bad-tick bar can't poison the prev-day
    # reference levels. MAD-based filter rejects bars whose H or L deviates
    # >4× median absolute deviation from the median close.
    try:
        from services.weekend_newsletters import _sanitize_d1_bars
        clean_d1 = _sanitize_d1_bars(d1) if d1 else []
    except Exception:
        clean_d1 = d1 or []
    prev_day_high = round(max(c.high for c in clean_d1[-2:-1]), 2) if len(clean_d1) >= 2 else None
    prev_day_low  = round(min(c.low  for c in clean_d1[-2:-1]), 2) if len(clean_d1) >= 2 else None
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

    # Execution model — now receives our own sweep detection + ICT score so
    # Model A can confirm independent of scanner.SIGNAL_READY
    model_letter, model_confirmed, swept_text, target_text = _classify_execution_model(
        scan=scan, ict=(ict.score if ict else 0), news_clear=news_clear,
        sweep=sweep, ict_score=(ict.score if ict else 0),
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

    # ── Build trade plan ────────────────────────────────────────────────
    # Preference order:
    #   1. Scanner's recommendedAction.tradePlan (when SIGNAL_READY)
    #   2. Strategist's own ATR-based plan when scanner abstains BUT we
    #      have a direction (either from predictor or HTF-derived)
    plan_obj  = scan.get("recommendedAction", {}).get("tradePlan") or {}
    entry        = plan_obj.get("entry")
    stop_loss    = plan_obj.get("stopLoss")
    take_profit  = plan_obj.get("takeProfit")
    rr           = plan_obj.get("rr") or 0
    plan_source  = "scanner" if entry and stop_loss else None

    tp1, tp2, tp3 = None, None, None
    if entry and stop_loss and take_profit and proposed_signal in ("BUY", "SELL"):
        # Scanner-provided plan — stagger to TP1/TP2/TP3
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

    elif proposed_signal in ("BUY", "SELL") and current_price and atr_h1 > 0:
        # Scanner abstained → strategist generates its own ATR plan
        gen = _generate_trade_plan(
            direction=proposed_signal,
            current_price=current_price,
            atr_h1=atr_h1,
            candles_m15=m15,
            h1_ema20=ema20_h1,               # NEW: pullback-zone gate
        )
        if gen.get("entry") is not None:
            entry, stop_loss = gen["entry"], gen["stop_loss"]
            tp1, tp2, tp3    = gen["tp1"], gen["tp2"], gen["tp3"]
            rr               = gen["rr"]
            plan_source      = "strategist_atr"
            log.info(
                "[strategist] generated ATR trade plan %s entry=%s SL=%s TP1=%s TP2=%s rr=%s",
                proposed_signal, entry, stop_loss, tp1, tp2, rr,
            )
        elif gen.get("source") == "chase_rejected":
            log.info("[strategist] pullback gate rejected entry: %s", gen.get("rejection"))
            plan_source = "chase_rejected"

    if rr and rr >= 2.5:
        score_rr = 10
    elif rr and rr >= 1.5:
        score_rr = 5
    total_setup_score += score_rr

    # ── Diagnostic reasons (for reporting, NOT decision gating) ─────────
    # These were used to force STAND ASIDE in the legacy 80/100 model. The
    # 5-condition mandate model now governs the decision; these reasons are
    # surfaced as `stand_aside_reason` text only when the decision is in fact
    # STAND ASIDE (conditions < 3 OR no direction).
    stand_aside_reasons = []
    if not entry or not stop_loss:
        stand_aside_reasons.append("Entry/SL not defined")
    if not tp1 or not tp2:
        stand_aside_reasons.append("TPs incomplete")
    if rr and rr < 1.5:
        stand_aside_reasons.append(f"RR {rr}<1.5 (demo floor)")
    if not model_confirmed or model_letter == "E":
        stand_aside_reasons.append("No confirmed execution model")
    if macro_aligned == "Conflicted":
        stand_aside_reasons.append(f"Macro conflicts ({gold_macro_bias})")
    if not news_clear:
        stand_aside_reasons.append("Inside news risk window")
    if proposed_signal not in ("BUY", "SELL"):
        stand_aside_reasons.append("No direction (scanner/predictor/HTF all WAIT)")
    if ict and ict.posture == "MISALIGNED":
        stand_aside_reasons.append(f"ICT framework MISALIGNED ({ict.score}/100)")
    if kz_policy is not None and not kz_policy.allow:
        stand_aside_reasons.append(
            f"KZ policy BLOCK ({kz.get('current_kz')} × {proposed_signal})"
        )

    # ── Mandate enums (8/8/6/5-category classifications) ────────────────
    # Computed FIRST so the decision block below has everything it needs.
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

    swept_recently = bool(sweep.get("swept"))
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
        d1_bias=d1_bias_local if "Insufficient" not in d1_bias_local
                else (scan.get("engineModel") or {}).get("d1Bias", ""),
        h4_bias=h4_bias_local if "Insufficient" not in h4_bias_local
                else (scan.get("engineModel") or {}).get("h4Bias", ""),
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
        session_label=session_mandate,         # ← catches bad-session windows
        rr=rr or 0,
        entry=entry, stop_loss=stop_loss, tp1=tp1, tp2=tp2,
        kz_policy=kz_policy,                    # ← empirical edge as C2
        candles_m15=m15,                        # ← micro-momentum in C3
        sweep=sweep,                            # ← NEW: CISD reversal gate in C3
    )
    conditions_passed = sum(1 for c in conditions if c["passed"])
    est_win_rate = _estimate_win_rate(conditions_passed)

    # CISD status for verdict readout (recomputed cheaply; matches C3 logic)
    _is_reversal = bool(sweep and sweep.get("swept") and sweep.get("reclaimed"))
    if _is_reversal and proposed_signal in ("BUY", "SELL"):
        _cisd_ok, _cisd_detail = _detect_cisd(proposed_signal, m15, lookback=8)
        _verdict_cisd_status = {"confirmed": _cisd_ok, "detail": _cisd_detail,
                                "is_reversal": True}
    else:
        _verdict_cisd_status = {"confirmed": None,
                                "detail": "continuation setup — CISD n/a",
                                "is_reversal": False}

    # ── MANDATE DECISION ────────────────────────────────────────────────
    # Per the mandate:
    #   5/5 → A-grade demo execution allowed
    #   4/5 → valid demo execution allowed
    #   3/5 → watchlist (signal but no execution)
    #  ≤2/5 → STAND ASIDE
    decision = "STAND ASIDE"
    if conditions_passed >= 3 and proposed_signal in ("BUY", "SELL"):
        decision = proposed_signal

    # Quality band derived from the legacy 0-100 score (kept for the dashboard
    # panel). The mandate's primary signal is conditions_passed.
    if total_setup_score >= 90: band = "A-grade"
    elif total_setup_score >= 80: band = "Valid"
    elif total_setup_score >= 70: band = "Watchlist"
    else: band = "No Trade"

    if decision == "STAND ASIDE" and conditions_passed < 3 and proposed_signal in ("BUY", "SELL"):
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
    # allow_alert now requires cp>=4. The mandate says 3/5 is "Watchlist
    # only — no execution"; Telegram alerts on 3/5 have been noise-heavy
    # for the operator (setup on the cusp, not actionable). 3/5 still
    # appears on the dashboard and in the strategist_verdicts log; it
    # just doesn't ping the phone.
    allow_alert  = (decision != "STAND ASIDE" and conditions_passed >= 4)
    allow_demo   = (decision != "STAND ASIDE" and total_setup_score >= 85
                    and settings.allow_demo_trading)
    allow_live   = False   # ← MANDATE: live execution is hard-disabled in this engine

    # ── Bridge / spread / news / position-cap gates ─────────────────────
    bridge_alive = _is_bridge_alive(max_age_seconds=120)
    spread_status = ((scan.get("risk") or {}).get("spreadStatus") or "UNKNOWN").upper()
    spread_acceptable = spread_status in ("OK", "NORMAL", "ACCEPTABLE", "UNKNOWN")
    demo_auto_enqueue = getattr(settings, "demo_auto_enqueue", False)

    # Position-cap snapshot from bridge heartbeat — drives the risk gate
    pos_snap = _get_open_positions_snapshot()
    base_cap = getattr(settings, "max_concurrent_positions", 5)
    extended_cap = getattr(settings, "max_positions_extended", 10)
    profit_threshold = getattr(settings, "extended_cap_profit_usd", 300.0)
    vol_ratio_threshold = getattr(settings, "extended_cap_volume_ratio", 1.2)

    # Volume continuation check — used by the dynamic-cap unlock
    volume_passes, volume_ratio = _volume_confirms_continuation(
        h1, ratio_threshold=vol_ratio_threshold,
    )

    # Dynamic cap — base 5, extends to 10 when trend + profit + volume all confirm
    effective_cap, extended_active, cap_block_reasons = _compute_dynamic_cap(
        base_cap=base_cap,
        extended_cap=extended_cap,
        floating_pnl=pos_snap["floating_pnl"],
        profit_threshold=profit_threshold,
        tf_alignment_label=tf_alignment_mandate,
        market_state=market_state_mandate,
        volume_passes=volume_passes,
    )

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
        open_positions_count=pos_snap["count"],
        max_concurrent_positions=effective_cap,    # ← dynamic, not static
        kz_policy=kz_policy,                        # ← NEW: hard cell-edge gate
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
            # Strategist's own sweep detection — visible alongside scanner state
            "sweep_detected":   sweep.get("swept", False),
            "sweep_side":       sweep.get("side"),
            "sweep_level":      sweep.get("level"),
            "sweep_reclaimed":  sweep.get("reclaimed", False),
            # CISD (Change in State of Delivery) — sniper confirmation for reversals.
            # Only meaningful when a reversal setup is in play (sweep + reclaim).
            # For continuation setups (no sweep), cisd_confirmed is reported as
            # None (n/a) so downstream consumers can distinguish.
            "cisd_confirmed":   _verdict_cisd_status["confirmed"],
            "cisd_detail":      _verdict_cisd_status["detail"],
            "is_reversal_setup": _verdict_cisd_status["is_reversal"],
        },
        "diagnostics": {
            "direction_source":     direction_source,
            "plan_source":          plan_source,
            "d1_bias_local":        d1_bias_local,
            "h4_bias_local":        h4_bias_local,
            "sweep_rationale":      sweep.get("rationale"),
            "scanner_state":        scan.get("marketState"),
            "scanner_score":        scan.get("qualityScore"),
            # Position-cap visibility (dynamic pyramid)
            "open_positions":       pos_snap["count"],
            "open_position_tickets": pos_snap["tickets"],
            "floating_pnl":         pos_snap["floating_pnl"],
            "max_concurrent_positions": effective_cap,    # dynamic — actually used
            "cap_base":             base_cap,
            "cap_extended":         extended_cap,
            "cap_extended_active":  extended_active,
            "cap_profit_threshold": profit_threshold,
            "cap_volume_ratio":     volume_ratio,
            "cap_volume_required":  vol_ratio_threshold,
            "cap_block_reasons":    cap_block_reasons,
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
