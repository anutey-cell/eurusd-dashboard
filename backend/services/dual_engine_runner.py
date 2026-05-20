"""
Dual-engine paper observation runner.

Runs BOTH the swing-H1 ICT scanner AND the trend-pullback H1 strategy
on every scan trigger, logging each as a paper observation tagged with
its engine_id. This lets the dashboard track both candidates in parallel
for the 6-month observation comparison.

Engines tracked:
  - "swing"           — institutional scanner (ICT swing H1 with score>=85)
  - "trend_pullback"  — EMA21/50 pullback strategy on H1

Each engine that returns BUY/SELL gets its own row in paper_observations.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from sqlalchemy.orm import Session

log = logging.getLogger(__name__)

# Minimum quality thresholds for paper observation logging
SWING_MIN_SCORE         = 85         # the validated edge config (run #45)
TREND_PULLBACK_MIN_RR   = 2.0
MOMENTUM_MIN_SCORE      = 65         # momentum engine fires more frequently


def run_dual_engines(db: Session) -> dict:
    """
    Run both engines on current H1 candles and log any qualifying signals
    to paper_observations with engine_id tagging.

    Returns:
        dict with per-engine results:
        {
          "swing":          {logged: bool, observation_id: int|None, signal, reason},
          "trend_pullback": {logged: bool, observation_id: int|None, signal, reason},
        }
    """
    results = {
        "swing":             {"logged": False, "observation_id": None,
                               "signal": "WAIT", "reason": ""},
        "trend_pullback":    {"logged": False, "observation_id": None,
                               "signal": "WAIT", "reason": ""},
        "momentum_breakout": {"logged": False, "observation_id": None,
                               "signal": "WAIT", "reason": ""},
    }

    # ── Engine 1: Swing H1 ICT (scanner-based) ────────────────────────────
    try:
        from services.institutional_scanner import scan_xauusd_market
        from services.paper_observation_tracker import log_observation
        scan = scan_xauusd_market(force_refresh=False, db=db)
        results["swing"]["signal"] = scan.get("signal", "WAIT")
        results["swing"]["reason"] = scan.get("summary", "")[:100]
        # Scanner already auto-logs but we tag explicitly
        if scan.get("marketState") == "SIGNAL_READY":
            score = scan.get("recommendedAction", {}).get("tradePlan", {}).get("qualityScore", 0)
            if score >= SWING_MIN_SCORE:
                obs_id = log_observation(db, scan, engine_id="swing")
                if obs_id:
                    results["swing"]["logged"] = True
                    results["swing"]["observation_id"] = obs_id
    except Exception as e:
        log.warning("[dual_runner] Swing engine failed: %s", e)
        results["swing"]["reason"] = f"engine error: {e}"

    # ── Engine 2: Trend Pullback H1 ───────────────────────────────────────
    try:
        from services.intraday_strategies import analyze_trend_pullback
        from services.historical_data_provider import load_historical_macro_events
        from data.candles import get_candles
        from services.paper_observation_tracker import _build_fingerprint, PaperObservation
        from datetime import timedelta
        import json as _json

        # Pull recent H1 candles. In live mode this routes through TradingView
        # for fresh data; we REFUSE to feed synthetic data into the learning
        # engine because that would pollute the paper-observation dataset.
        from config import settings
        LIVE_SOURCES = {"tradingview", "mt5", "tradingview-cached", "mt5-cached"}
        try:
            resp = get_candles(interval="H1", limit=500, pair="xauusd")
            src = getattr(resp, "source", "unknown")
            if settings.data_mode == "live" and src not in LIVE_SOURCES:
                results["trend_pullback"]["reason"] = (
                    f"Refusing synthetic candles (source={src}) in live mode."
                )
                return results
            candles = resp.candles
        except Exception as _e:
            log.debug("[dual_runner] live candles unavailable (%s)", _e)
            if settings.data_mode == "live":
                results["trend_pullback"]["reason"] = f"Live candle fetch failed: {_e}"
                return results
            # Demo mode only: it's acceptable to pull from DB / synthetic
            from services.historical_data_provider import get_historical_candles
            candles, _src = get_historical_candles(
                db=db, timeframe="H1", lookback=500, allow_synthetic_fallback=True,
            )

        if not candles or len(candles) < 60:
            results["trend_pullback"]["reason"] = "Insufficient H1 candles"
            return results

        now = datetime.now(timezone.utc)
        first_t = candles[0].time if candles[0].time.tzinfo else candles[0].time.replace(tzinfo=timezone.utc)
        macro = load_historical_macro_events(db=db, start=first_t, end=now + timedelta(hours=1))

        tp_result = analyze_trend_pullback(
            candles=candles, at=now, macro_events=macro,
            pip_size=1.0, target_pips=50, max_sl_pips=15, min_rr=TREND_PULLBACK_MIN_RR,
            enable_killzone=True, enable_news_filter=True,
        )
        results["trend_pullback"]["signal"] = tp_result.signal
        results["trend_pullback"]["reason"] = tp_result.reason[:100]

        # Log it as a paper observation if BUY/SELL
        if tp_result.signal in ("BUY", "SELL") and tp_result.entry is not None:
            # Build synthetic scan-result-like dict for log_observation
            synthetic_scan = {
                "instrument":     "XAU/USD",
                "marketState":    "SIGNAL_READY",
                "signal":         tp_result.signal,
                "confidence":     tp_result.quality_score,
                "grade":          "B",
                "engineModel":    {"setupType": tp_result.setup_type or "trend_pullback"},
                "recommendedAction": {
                    "actionable": True,
                    "tradePlan": {
                        "direction":    tp_result.signal,
                        "entry":        tp_result.entry,
                        "stopLoss":     tp_result.stop_loss,
                        "takeProfit":   tp_result.take_profit,
                        "riskPoints":   tp_result.risk_pips,
                        "targetPoints": tp_result.target_pips,
                        "rr":           tp_result.rr,
                        "qualityScore": tp_result.quality_score,
                        "session":      tp_result.sess.session_text if tp_result.sess else "",
                    },
                },
            }
            obs_id = log_observation(db, synthetic_scan, engine_id="trend_pullback")
            if obs_id:
                results["trend_pullback"]["logged"] = True
                results["trend_pullback"]["observation_id"] = obs_id

    except Exception as e:
        log.warning("[dual_runner] Trend pullback engine failed: %s", e)
        results["trend_pullback"]["reason"] = f"engine error: {e}"

    # ── Engine 3: Momentum Breakout M15 ──────────────────────────────────
    # Catches the strong NY-open / news-spike moves that ICT misses because
    # they don't go through the sweep -> MSS -> FVG choreography.
    try:
        from services.intraday_strategies import analyze_momentum_breakout
        from data.candles import get_candles
        from config import settings as _settings
        LIVE = {"tradingview", "mt5", "tradingview-cached", "mt5-cached"}
        m15_resp = get_candles(interval="M15", limit=200, pair="xauusd")
        src = getattr(m15_resp, "source", "unknown")
        if _settings.data_mode == "live" and src not in LIVE:
            results["momentum_breakout"]["reason"] = (
                f"Refusing synthetic M15 candles (source={src}) in live mode."
            )
        elif not m15_resp.candles or len(m15_resp.candles) < 22:
            results["momentum_breakout"]["reason"] = "Insufficient M15 candles (need >=22)"
        else:
            now = datetime.now(timezone.utc)
            first_t = m15_resp.candles[0].time
            if not first_t.tzinfo:
                first_t = first_t.replace(tzinfo=timezone.utc)
            try:
                from services.historical_data_provider import load_historical_macro_events
                from datetime import timedelta as _td
                m_events = load_historical_macro_events(
                    db=db, start=first_t, end=now + _td(hours=1),
                )
            except Exception:
                m_events = []

            mb_result = analyze_momentum_breakout(
                candles=m15_resp.candles, at=now, macro_events=m_events,
                pip_size=1.0, target_rr=2.5,
                min_body_atr_mult=2.0, min_volume_mult=1.5,
                min_close_pct=0.80, max_sl_pts=25.0,
                enable_killzone=True, enable_news_filter=True,
            )
            results["momentum_breakout"]["signal"] = mb_result.signal
            results["momentum_breakout"]["reason"] = mb_result.reason[:120]

            if (mb_result.signal in ("BUY", "SELL")
                    and mb_result.entry is not None
                    and mb_result.quality_score >= MOMENTUM_MIN_SCORE):
                synthetic_scan = {
                    "instrument":   "XAU/USD",
                    "marketState":  "SIGNAL_READY",
                    "signal":       mb_result.signal,
                    "confidence":   mb_result.quality_score,
                    "grade":        "B",
                    "engineModel":  {"setupType": "momentum_breakout"},
                    "recommendedAction": {
                        "actionable": True,
                        "tradePlan": {
                            "direction":    mb_result.signal,
                            "entry":        mb_result.entry,
                            "stopLoss":     mb_result.stop_loss,
                            "takeProfit":   mb_result.take_profit,
                            "riskPoints":   mb_result.risk_pips,
                            "targetPoints": mb_result.target_pips,
                            "rr":           mb_result.rr,
                            "qualityScore": mb_result.quality_score,
                            "session":      mb_result.sess.session_text if mb_result.sess else "",
                        },
                    },
                }
                obs_id = log_observation(db, synthetic_scan, engine_id="momentum_breakout")
                if obs_id:
                    results["momentum_breakout"]["logged"] = True
                    results["momentum_breakout"]["observation_id"] = obs_id
    except Exception as e:
        log.warning("[dual_runner] Momentum breakout engine failed: %s", e)
        results["momentum_breakout"]["reason"] = f"engine error: {e}"

    log.info(
        "[dual_runner] swing=%s/%s trend_pullback=%s/%s momentum=%s/%s",
        results["swing"]["signal"],
        "logged" if results["swing"]["logged"] else "skipped",
        results["trend_pullback"]["signal"],
        "logged" if results["trend_pullback"]["logged"] else "skipped",
        results["momentum_breakout"]["signal"],
        "logged" if results["momentum_breakout"]["logged"] else "skipped",
    )
    return results
