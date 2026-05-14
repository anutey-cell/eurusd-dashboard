"""
Institutional XAU/USD opportunity scanner.

Wraps the existing signal engine (which remains the final BUY/SELL gate) and
adds multi-timeframe context: D1/H4/H1 bias, M15 structure, M5 execution
readiness, ATR volatility, spread, data freshness, and a narrative market view.

Public API
----------
scan_xauusd_market(force_refresh, db) -> dict
    Returns a structured market view. Uses in-memory cache unless force_refresh.

get_scan_status() -> dict
    Returns cache / scanner health info.

Market states
-------------
NO_TRADE            No meaningful setup present
WATCHLIST           Directional bias without confirmed entry
SETUP_FORMING       Liquidity/FVG/structure forming but incomplete
SIGNAL_READY        All gates satisfied -- signal engine returned BUY/SELL
NEWS_BLOCKED        News risk prevents action
DATA_STALE          Candle data too old to trust
SPREAD_TOO_WIDE     Spread exceeds XAU/USD threshold
VOLATILITY_UNSTABLE ATR too low or too high for reliable entry

Scanner does NOT place trades, confirm signals, or bypass signal gates.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any

log = logging.getLogger(__name__)

# ── In-memory scan cache ──────────────────────────────────────────────────────
_cache: dict[str, Any] = {"result": None, "cached_at": None}
# Track last-persisted scan state to decide if DB write is needed
_last_persisted: dict[str, Any] = {
    "market_state": None,
    "signal": None,
    "confidence": None,
    "opportunity_status": None,
    "persisted_at": None,
}

HEARTBEAT_MINUTES = 15   # store auto-scan heartbeat every N minutes


def _cache_ttl() -> int:
    try:
        from config import settings
        return getattr(settings, "scan_cache_ttl_seconds", 45)
    except Exception:
        return 45


def _cache_fresh() -> bool:
    if not _cache["result"] or not _cache["cached_at"]:
        return False
    age = (datetime.now(timezone.utc) - _cache["cached_at"]).total_seconds()
    return age < _cache_ttl()


# ═══════════════════════════════════════════════════════════════════════════════
# Public API
# ═══════════════════════════════════════════════════════════════════════════════

def scan_xauusd_market(force_refresh: bool = False, db=None) -> dict:
    """
    Run or return cached XAU/USD institutional scan.

    force_refresh=True  → bypass cache, run fresh analysis.
    db                  → optional SQLAlchemy session for adaptive weights
                          and scan history persistence.
    """
    if not force_refresh and _cache_fresh():
        log.debug("[scanner] Serving cached scan result")
        return _cache["result"]

    log.info("[scanner] Running fresh XAU/USD scan  force=%s", force_refresh)
    scan_mode = "manual" if force_refresh else "auto"

    try:
        result = _run_scan(scan_mode=scan_mode, db=db)
    except Exception as exc:
        log.exception("[scanner] Scan failed: %s", exc)
        result = _error_result(str(exc))

    _cache["result"] = result
    _cache["cached_at"] = datetime.now(timezone.utc)

    # Persist to DB (best-effort, never blocks response)
    if db is not None:
        try:
            _maybe_persist(result, db, force=force_refresh)
        except Exception as _pe:
            log.debug("[scanner] DB persist failed (non-fatal): %s", _pe)

    # Fire Telegram alert for SIGNAL_READY (fire-and-forget)
    if result.get("marketState") == "SIGNAL_READY":
        try:
            _maybe_telegram_alert(result)
        except Exception as _te:
            log.debug("[scanner] Telegram alert (non-fatal): %s", _te)

    return result


def get_scan_status() -> dict:
    """Return scanner health / cache status."""
    from config import settings
    ttl = _cache_ttl()
    now = datetime.now(timezone.utc)
    cached_at = _cache["cached_at"]
    fresh = _cache_fresh()

    next_auto = None
    if cached_at:
        next_auto = (cached_at + timedelta(seconds=ttl)).isoformat()

    return {
        "enabled":                 getattr(settings, "auto_scan_enabled", True),
        "lastScanAt":              cached_at.isoformat() if cached_at else None,
        "nextAutoScanAt":          next_auto,
        "autoScanIntervalSeconds": getattr(settings, "scan_interval_seconds", 60),
        "cacheTtlSeconds":         ttl,
        "dataFresh":               fresh,
        "scannerHealthy":          True,
        "instrument":              "XAU/USD",
    }


# ═══════════════════════════════════════════════════════════════════════════════
# Core scan logic
# ═══════════════════════════════════════════════════════════════════════════════

def _run_scan(scan_mode: str = "auto", db=None) -> dict:
    """Execute the full multi-timeframe institutional scan."""
    from pair_config import get_pair_config
    from data.candles import get_candles
    from data.calendar import get_calendar
    from services.signal_engine import (
        detect_higher_timeframe_bias,
        detect_liquidity_sweep,
        detect_market_structure,
        detect_fair_value_gap,
        check_news_risk,
        detect_session,
    )
    from data.signals import run_signal_analysis

    cfg = get_pair_config("xauusd")
    pip_size          = cfg["pip_size"]          # 1.0 for XAU/USD
    atr_min           = cfg.get("atr_min", 15)
    atr_max           = cfg.get("atr_max", 300)
    max_spread        = cfg.get("max_spread", 5.0)
    strong_wick_pips  = cfg.get("strong_wick_pips", 8)
    fvg_min_pips      = cfg.get("fvg_min_pips", 5)

    now = datetime.now(timezone.utc)
    timestamp = now.isoformat()

    # ── 1. Fetch multi-timeframe candles ─────────────────────────────────────
    tf_limits = {"D1": 100, "H4": 200, "H1": 200, "M15": 200, "M5": 100}
    candles_by_tf: dict[str, list] = {}
    for tf, limit in tf_limits.items():
        try:
            resp = get_candles(interval=tf, limit=limit, pair="xauusd")
            candles_by_tf[tf] = resp.candles
        except Exception as e:
            log.debug("[scanner] %s candle fetch failed: %s", tf, e)
            candles_by_tf[tf] = []

    # ── 2. Data freshness guard ──────────────────────────────────────────────
    data_fresh, data_blocker = _check_data_freshness(candles_by_tf, now)
    if not data_fresh:
        result = _stale_result(timestamp, scan_mode, data_blocker)
        return result

    # ── 3. Macro calendar ────────────────────────────────────────────────────
    macro_events: list[dict] = []
    try:
        cal = get_calendar()
        macro_events = [
            {
                "time":     e.time.isoformat(),
                "currency": e.currency,
                "event":    e.event,
                "impact":   e.impact,
                "actual":   e.actual,
                "forecast": e.forecast,
                "previous": e.previous,
            }
            for e in cal.events
        ]
    except Exception as e:
        log.debug("[scanner] Calendar unavailable: %s", e)

    # ── 4. Session + news ────────────────────────────────────────────────────
    session = detect_session(at=now)
    news    = check_news_risk(macro_events, at=now, pair="xauusd")

    # ── 5. Multi-TF bias analysis ────────────────────────────────────────────
    d1c  = candles_by_tf.get("D1", [])
    h4c  = candles_by_tf.get("H4", [])
    h1c  = candles_by_tf.get("H1", [])
    m15c = candles_by_tf.get("M15", [])
    m5c  = candles_by_tf.get("M5", [])

    d1_htf  = detect_higher_timeframe_bias(d1c)  if len(d1c)  >= 51 else None
    h4_htf  = detect_higher_timeframe_bias(h4c)  if len(h4c)  >= 51 else None
    h1_htf  = detect_higher_timeframe_bias(h1c)  if len(h1c)  >= 51 else None

    htf_bullish = h4_htf.bullish if h4_htf else True  # default for structure detection

    h4_liq  = detect_liquidity_sweep(h4c,  pip_size=pip_size, strong_wick_pips=strong_wick_pips) if len(h4c)  >= 20 else None
    h4_ms   = detect_market_structure(h4c,  htf_bullish=htf_bullish) if len(h4c)  >= 15 else None
    h1_ms   = detect_market_structure(h1c,  htf_bullish=htf_bullish) if len(h1c)  >= 15 else None
    m15_ms  = detect_market_structure(m15c, htf_bullish=htf_bullish) if len(m15c) >= 15 else None
    h4_fvg  = detect_fair_value_gap(h4c,  pip_size=pip_size, fvg_min_pips=fvg_min_pips) if len(h4c) >= 5 else None

    # ── 6. ATR / volatility ──────────────────────────────────────────────────
    atr, vol_status = _calc_atr(h4c, pip_size, atr_min, atr_max)

    # ── 7. Run existing signal engine (H4 -- remains the BUY/SELL gate) ──────
    engine_result = None
    try:
        engine_result = run_signal_analysis(
            macro_events=macro_events, pair="xauusd", db=db
        )
    except Exception as e:
        log.warning("[scanner] Signal engine error: %s", e)

    signal        = engine_result.signal        if engine_result else "WAIT"
    quality_score = engine_result.quality_score if engine_result else 0

    # ── 8. Institutional bias (multi-TF majority vote) ───────────────────────
    inst_bias, bias_conf = _institutional_bias(d1_htf, h4_htf, h1_htf, h4_liq, h4_ms)

    # ── 9. Blocker codes ─────────────────────────────────────────────────────
    blocker_codes, blockers = _collect_blockers(
        news, h4_liq, h4_ms, h4_fvg, vol_status, quality_score,
        engine_result, cfg
    )

    # ── 10. Market state ─────────────────────────────────────────────────────
    market_state = _market_state(
        news=news,
        vol_status=vol_status,
        h4_liq=h4_liq,
        h4_ms=h4_ms,
        h4_fvg=h4_fvg,
        engine_signal=signal,
        inst_bias=inst_bias,
    )

    # ── 11. Confidence ────────────────────────────────────────────────────────
    confidence = _confidence(market_state, quality_score, bias_conf, h4_liq, h4_ms, h4_fvg)

    # ── 12. Key drivers ──────────────────────────────────────────────────────
    key_drivers = _key_drivers(d1_htf, h4_htf, h1_htf, h4_liq, h4_ms, h4_fvg, session, atr)

    # ── 13. Opportunity ──────────────────────────────────────────────────────
    opportunity = _opportunity(market_state, signal, quality_score, engine_result, h4_fvg, h4_liq, cfg)

    # ── 14. Liquidity view ───────────────────────────────────────────────────
    liq_view = _liquidity_view(h4c, h4_liq)

    # ── 15. FVG view ─────────────────────────────────────────────────────────
    fvg_view = _fvg_view(h4_fvg)

    # ── 16. Action + readiness ────────────────────────────────────────────────
    readiness = _readiness_label(market_state)
    action    = _action(market_state, signal, blockers)

    # ── 17. Narrative summary ─────────────────────────────────────────────────
    summary = _build_summary(
        market_state, signal, inst_bias, h4_liq, h4_ms, h4_fvg,
        news, session, vol_status, readiness, quality_score
    )

    # ── 18. Model view ────────────────────────────────────────────────────────
    model = {
        "d1Bias":       d1_htf.bias_text  if d1_htf  else "Insufficient D1 data",
        "h4Bias":       h4_htf.bias_text  if h4_htf  else "Insufficient H4 data",
        "h1Bias":       h1_htf.bias_text  if h1_htf  else "Insufficient H1 data",
        "m15Structure": m15_ms.structure_text if m15_ms else "Insufficient M15 data",
        "m5Execution":  _m5_execution(m5c, signal, market_state),
    }

    # ── 19. Risk view ─────────────────────────────────────────────────────────
    risk = {
        "spreadStatus":     "OK",    # TODO: live spread from MT5 tick when bridge online
        "volatilityStatus": vol_status,
        "targetRealism":    _target_realism(atr, cfg),
        "dataStatus":       "FRESH",
        "atr":              round(atr, 2),
        "session":          session.session_text,
    }

    # ── 20. News view ─────────────────────────────────────────────────────────
    next_ev = _next_event(macro_events, now)
    news_view = {
        "status":        news.status,
        "blockingEvent": news.blocking_event or None,
        "nextEvent":     next_ev.get("event") if next_ev else None,
        "nextEventTime": next_ev.get("time")  if next_ev else None,
        "minutesToEvent": _mins_to(next_ev, now) if next_ev else None,
    }

    return {
        "instrument":        "XAU/USD",
        "timestamp":         timestamp,
        "scanMode":          scan_mode,
        "marketState":       market_state,
        "signal":            signal,
        "institutionalBias": inst_bias,
        "confidence":        confidence,
        "readiness":         readiness,
        "summary":           summary,
        "keyDrivers":        key_drivers,
        "blockers":          blockers,
        "blockerCodes":      blocker_codes,
        "opportunity":       opportunity,
        "model":             model,
        "liquidity":         liq_view,
        "fvg":               fvg_view,
        "news":              news_view,
        "risk":              risk,
        "action":            action,
        "qualityScore":      quality_score,
    }


# ═══════════════════════════════════════════════════════════════════════════════
# Helper: data freshness
# ═══════════════════════════════════════════════════════════════════════════════

def _check_data_freshness(candles_by_tf: dict, now: datetime) -> tuple[bool, str]:
    """
    M15 candles must be < 45 min old.
    H4 candles must be < 8 h old.
    Returns (fresh, blocker_message).
    """
    for tf, max_age_min in [("M15", 45), ("H4", 480)]:
        bars = candles_by_tf.get(tf, [])
        if not bars:
            return False, f"No {tf} candles available"
        latest = bars[-1]
        latest_time = latest.time if hasattr(latest, "time") else None
        if latest_time is None:
            continue
        if not latest_time.tzinfo:
            latest_time = latest_time.replace(tzinfo=timezone.utc)
        age_min = (now - latest_time).total_seconds() / 60
        if age_min > max_age_min:
            return False, (
                f"{tf} candles are {age_min:.0f} min old "
                f"(max {max_age_min} min). Check data feed."
            )
    return True, ""


# ═══════════════════════════════════════════════════════════════════════════════
# Helper: ATR / volatility
# ═══════════════════════════════════════════════════════════════════════════════

def _calc_atr(candles: list, pip_size: float, atr_min: float, atr_max: float) -> tuple[float, str]:
    """Calculate 14-bar ATR in points. Returns (atr, status)."""
    if len(candles) < 14:
        return 50.0, "OK"  # default mid-range
    bars = candles[-14:]
    atr = sum((c.high - c.low) / pip_size for c in bars) / len(bars)
    if atr < atr_min:
        status = "LOW"
    elif atr > atr_max:
        status = "HIGH"
    else:
        status = "OK"
    return round(atr, 2), status


def _target_realism(atr: float, cfg: dict) -> str:
    target = cfg.get("target_pips", 50)
    if atr <= 0:
        return "UNKNOWN"
    ratio = target / atr
    if ratio <= 0.6:
        return "REALISTIC"
    if ratio <= 1.0:
        return "MARGINAL"
    return "AGGRESSIVE"


# ═══════════════════════════════════════════════════════════════════════════════
# Helper: institutional bias
# ═══════════════════════════════════════════════════════════════════════════════

def _institutional_bias(d1_htf, h4_htf, h1_htf, h4_liq, h4_ms) -> tuple[str, int]:
    """
    Multi-TF majority vote → institutional bias label + confidence (0-100).
    """
    votes_bull = 0
    votes_bear = 0
    total = 0

    for htf in (d1_htf, h4_htf, h1_htf):
        if htf is None:
            continue
        total += 1
        if htf.bullish:
            votes_bull += 1
        else:
            votes_bear += 1

    # Structure confirmation adds weight
    if h4_ms and h4_ms.shifted:
        if h4_ms.bullish:
            votes_bull += 1
        else:
            votes_bear += 1
        total += 1

    if total == 0:
        return "Neutral", 30

    majority_bull = votes_bull > votes_bear
    majority_bear = votes_bear > votes_bull

    # Determine bias label
    if votes_bull == votes_bear:
        bias = "Neutral"
        conf = 30
    elif majority_bull:
        bias = "Bullish"
        conf = int(60 + (votes_bull / total) * 30)
    else:
        bias = "Bearish"
        conf = int(60 + (votes_bear / total) * 30)

    # Refine label with additional context
    if h4_liq and h4_liq.swept:
        if h4_liq.bullish and majority_bear:
            bias = "Reversal forming"  # bearish overall but bullish sweep
            conf = max(50, conf - 10)
        elif not h4_liq.bullish and majority_bull:
            bias = "Reversal forming"  # bullish overall but bearish sweep
            conf = max(50, conf - 10)

    if h4_ms and not h4_ms.shifted and majority_bull == majority_bear:
        bias = "Compression"
        conf = 35

    return bias, min(conf, 95)


# ═══════════════════════════════════════════════════════════════════════════════
# Helper: market state
# ═══════════════════════════════════════════════════════════════════════════════

def _market_state(news, vol_status, h4_liq, h4_ms, h4_fvg, engine_signal, inst_bias) -> str:
    # Blocked states take priority
    if news and not news.clear:
        return "NEWS_BLOCKED"
    if vol_status == "HIGH":
        return "VOLATILITY_UNSTABLE"
    if vol_status == "LOW":
        return "VOLATILITY_UNSTABLE"

    # Signal engine is the final BUY/SELL gate
    if engine_signal in ("BUY", "SELL"):
        return "SIGNAL_READY"

    # Progressive setup analysis
    liq_ok = h4_liq and h4_liq.swept
    ms_ok  = h4_ms  and h4_ms.shifted
    fvg_ok = h4_fvg and h4_fvg.detected

    conditions_met = sum([bool(liq_ok), bool(ms_ok), bool(fvg_ok)])

    if conditions_met >= 2:
        return "SETUP_FORMING"
    if conditions_met >= 1 or inst_bias in ("Bullish", "Bearish", "Reversal forming"):
        return "WATCHLIST"
    return "NO_TRADE"


# ═══════════════════════════════════════════════════════════════════════════════
# Helper: blocker codes
# ═══════════════════════════════════════════════════════════════════════════════

def _collect_blockers(news, h4_liq, h4_ms, h4_fvg, vol_status, quality_score,
                      engine_result, cfg) -> tuple[list[str], list[str]]:
    codes: list[str] = []
    msgs:  list[str] = []

    if news and not news.clear:
        codes.append("NEWS_BLOCKED")
        msgs.append(f"News blackout active: {news.blocking_event}")

    if vol_status == "HIGH":
        codes.append("VOLATILITY_UNSTABLE")
        msgs.append("Volatility too high for reliable entry (ATR above threshold)")
    elif vol_status == "LOW":
        codes.append("VOLATILITY_UNSTABLE")
        msgs.append("Volatility too low (ATR below minimum threshold)")

    if not h4_liq or not h4_liq.swept:
        codes.append("NO_LIQUIDITY_SWEEP")
        msgs.append("No liquidity sweep detected on H4")

    if not h4_ms or not h4_ms.shifted:
        codes.append("NO_STRUCTURE_CONFIRMATION")
        msgs.append("No BOS or CHoCH confirmed on H4")

    if not h4_fvg or not h4_fvg.detected:
        codes.append("NO_FVG")
        msgs.append("No Fair Value Gap detected on H4")
    elif h4_fvg and h4_fvg.detected and h4_fvg.in_zone is False:
        # FVG exists but price not yet in zone
        if "not yet retested" in (h4_fvg.fvg_text or ""):
            codes.append("FVG_NOT_RETESTED")
            msgs.append("FVG detected but price has not yet entered the zone")

    min_score = cfg.get("min_score", 80)
    if quality_score < min_score:
        codes.append("SCORE_TOO_LOW")
        msgs.append(f"Quality score {quality_score}/100 below gate ({min_score})")

    if engine_result:
        rr = getattr(engine_result, "rr", None) or 0
        min_rr = cfg.get("min_rr", 2.5)
        if rr > 0 and rr < min_rr:
            codes.append("RR_TOO_LOW")
            msgs.append(f"Risk:Reward {rr:.2f} below minimum {min_rr}")

        inv = getattr(engine_result, "invalidation", None)
        if not inv and engine_result.signal in ("BUY", "SELL"):
            codes.append("INVALIDATION_MISSING")
            msgs.append("No invalidation level defined")

    return codes, msgs


# ═══════════════════════════════════════════════════════════════════════════════
# Helper: confidence score
# ═══════════════════════════════════════════════════════════════════════════════

def _confidence(market_state, quality_score, bias_conf, h4_liq, h4_ms, h4_fvg) -> int:
    if market_state == "SIGNAL_READY":
        return max(quality_score, 80)
    if market_state in ("DATA_STALE", "NEWS_BLOCKED", "VOLATILITY_UNSTABLE",
                         "SPREAD_TOO_WIDE", "NO_TRADE"):
        return min(quality_score, 35)

    # Weighted composite for WATCHLIST / SETUP_FORMING
    base = bias_conf
    if h4_liq and h4_liq.swept:
        base += 10
    if h4_ms and h4_ms.shifted:
        base += 10
    if h4_fvg and h4_fvg.detected:
        base += 8
    return min(int(base), 79)


# ═══════════════════════════════════════════════════════════════════════════════
# Helper: key drivers
# ═══════════════════════════════════════════════════════════════════════════════

def _key_drivers(d1_htf, h4_htf, h1_htf, h4_liq, h4_ms, h4_fvg, session, atr) -> list[str]:
    drivers: list[str] = []

    if d1_htf:
        drivers.append(f"D1: {d1_htf.bias_text}")
    if h4_htf:
        drivers.append(f"H4: {h4_htf.bias_text}")
    if h1_htf:
        drivers.append(f"H1: {h1_htf.bias_text}")
    if h4_liq and h4_liq.swept:
        side = "Sell-side" if h4_liq.bullish else "Buy-side"
        drivers.append(f"{side} liquidity swept on H4")
    if h4_ms and h4_ms.shifted:
        drivers.append(f"H4 structure: {h4_ms.structure_text}")
    if h4_fvg and h4_fvg.detected:
        fvg_side = "Bullish" if h4_fvg.bullish else "Bearish"
        drivers.append(f"{fvg_side} FVG detected on H4")
    if session:
        drivers.append(f"Session: {session.session_text}")
    if atr > 0:
        drivers.append(f"H4 ATR (14): {atr:.1f} points")

    return drivers[:8]  # cap at 8 drivers


# ═══════════════════════════════════════════════════════════════════════════════
# Helper: opportunity object
# ═══════════════════════════════════════════════════════════════════════════════

def _opportunity(market_state, signal, quality_score, engine_result, h4_fvg, h4_liq, cfg) -> dict:
    status_map = {
        "NO_TRADE":           "NO_TRADE",
        "WATCHLIST":          "WATCHLIST",
        "SETUP_FORMING":      "SETUP_FORMING",
        "SIGNAL_READY":       "SIGNAL_READY",
        "NEWS_BLOCKED":       "NEWS_BLOCKED",
        "DATA_STALE":         "DATA_STALE",
        "SPREAD_TOO_WIDE":    "SPREAD_TOO_WIDE",
        "VOLATILITY_UNSTABLE":"WATCHLIST",
    }

    opp_status = status_map.get(market_state, "NO_TRADE")

    # Derive likely side from liquidity sweep or signal
    if signal in ("BUY", "SELL"):
        side = signal
    elif h4_liq and h4_liq.swept:
        side = "BUY" if h4_liq.bullish else "SELL"
    else:
        side = "NONE"

    # Entry zone text
    if engine_result and signal in ("BUY", "SELL") and engine_result.entry:
        entry_zone = f"Near {engine_result.entry:.2f}"
    elif h4_fvg and h4_fvg.detected:
        entry_zone = f"FVG zone {h4_fvg.gap_low:.2f}–{h4_fvg.gap_high:.2f}"
    else:
        entry_zone = "Pending FVG / structure retest"

    # Invalidation text
    if engine_result and engine_result.invalidation:
        inv = engine_result.invalidation
        inv_text = f"Below {inv:.2f}" if signal == "BUY" else f"Above {inv:.2f}"
    elif h4_liq and h4_liq.swept:
        lv = h4_liq.swept_level
        inv_text = (f"Below swept low {lv:.2f}"
                    if h4_liq.bullish
                    else f"Above swept high {lv:.2f}")
    else:
        inv_text = "No invalidation defined -- no signal"

    # Target
    target_pts = cfg.get("target_pips", 50)
    if signal in ("BUY", "SELL"):
        target_text = f"{target_pts} points (confirmed)"
    else:
        target_text = f"{target_pts} points (if confirmed)"

    # RR status
    if engine_result and engine_result.rr:
        rr_text = f"1:{engine_result.rr:.2f}"
    else:
        rr_text = "Pending"

    return {
        "side":         side,
        "status":       opp_status,
        "qualityScore": quality_score,
        "entryZone":    entry_zone,
        "invalidation": inv_text,
        "target":       target_text,
        "rrStatus":     rr_text,
    }


# ═══════════════════════════════════════════════════════════════════════════════
# Helper: liquidity view
# ═══════════════════════════════════════════════════════════════════════════════

def _liquidity_view(h4c: list, h4_liq) -> dict:
    # Derive recent swing high/low from H4
    swing_high = max((c.high for c in h4c[-50:]), default=None) if h4c else None
    swing_low  = min((c.low  for c in h4c[-50:]), default=None) if h4c else None

    if h4_liq and h4_liq.swept:
        status = h4_liq.liq_text
        if h4_liq.bullish:
            next_target = (f"Previous session high {swing_high:.2f}"
                           if swing_high else "Higher swing target")
        else:
            next_target = (f"Previous session low {swing_low:.2f}"
                           if swing_low else "Lower swing target")
    else:
        status      = "No liquidity sweep detected"
        next_target = "Watching for sweep setup"

    return {
        "status":                status,
        "buySide":               f"{swing_high:.2f}" if swing_high else "--",
        "sellSide":              f"{swing_low:.2f}"  if swing_low  else "--",
        "swept":                 bool(h4_liq and h4_liq.swept),
        "sweepSide":             ("sell-side" if (h4_liq and h4_liq.swept and h4_liq.bullish)
                                  else "buy-side"  if (h4_liq and h4_liq.swept and not h4_liq.bullish)
                                  else "none"),
        "nearestLiquidityTarget": next_target,
    }


# ═══════════════════════════════════════════════════════════════════════════════
# Helper: FVG view
# ═══════════════════════════════════════════════════════════════════════════════

def _fvg_view(h4_fvg) -> dict:
    if not h4_fvg or not h4_fvg.detected:
        return {
            "status":    "No FVG detected on H4",
            "direction": None,
            "freshness": "N/A",
            "mitigated": False,
            "zone":      None,
        }

    # Freshness: if price is in zone → being mitigated; approaching → fresh; otherwise → pending
    if h4_fvg.in_zone:
        freshness = "Being mitigated"
        mitigated = False
    elif "approaching" in (h4_fvg.fvg_text or ""):
        freshness = "Fresh -- price approaching"
        mitigated = False
    else:
        freshness = "Fresh -- price not yet at zone"
        mitigated = False

    return {
        "status":    h4_fvg.fvg_text,
        "direction": "Bullish" if h4_fvg.bullish else "Bearish",
        "freshness": freshness,
        "mitigated": mitigated,
        "zone": {
            "high": round(h4_fvg.gap_high, 2),
            "low":  round(h4_fvg.gap_low,  2),
        },
    }


# ═══════════════════════════════════════════════════════════════════════════════
# Helper: M5 execution readiness
# ═══════════════════════════════════════════════════════════════════════════════

def _m5_execution(m5c: list, signal: str, market_state: str) -> str:
    if market_state == "SIGNAL_READY" and signal in ("BUY", "SELL"):
        return f"{signal} setup confirmed -- await M5 entry trigger"
    if market_state == "SETUP_FORMING":
        return "Monitoring M5 for entry trigger"
    if not m5c:
        return "No M5 data"
    return "Waiting"


# ═══════════════════════════════════════════════════════════════════════════════
# Helper: text builders
# ═══════════════════════════════════════════════════════════════════════════════

def _readiness_label(market_state: str) -> str:
    return {
        "SIGNAL_READY":       "Signal Ready",
        "SETUP_FORMING":      "Setup Forming",
        "WATCHLIST":          "Watchlist",
        "NO_TRADE":           "No Trade",
        "NEWS_BLOCKED":       "News Blocked",
        "DATA_STALE":         "Data Stale",
        "SPREAD_TOO_WIDE":    "Spread Too Wide",
        "VOLATILITY_UNSTABLE":"Volatility Unstable",
    }.get(market_state, "No Trade")


def _action(market_state: str, signal: str, blockers: list[str]) -> str:
    actions = {
        "SIGNAL_READY":        "CONFIRM_SIGNAL",
        "SETUP_FORMING":       "WAIT_FOR_CONFIRMATION",
        "WATCHLIST":           "MONITOR_CONDITIONS",
        "NO_TRADE":            "NO_ACTION",
        "NEWS_BLOCKED":        "WAIT_FOR_NEWS_CLEAR",
        "DATA_STALE":          "CHECK_DATA_FEED",
        "SPREAD_TOO_WIDE":     "WAIT_FOR_SPREAD_NORMAL",
        "VOLATILITY_UNSTABLE": "WAIT_FOR_STABLE_VOLATILITY",
    }
    return actions.get(market_state, "NO_ACTION")


def _build_summary(market_state, signal, inst_bias, h4_liq, h4_ms, h4_fvg,
                   news, session, vol_status, readiness, quality_score) -> str:
    """Build an institutional analyst-style narrative summary."""
    parts: list[str] = []

    # Bias opener
    bias_lower = inst_bias.lower()
    if inst_bias in ("Bullish", "Bearish"):
        parts.append(
            f"XAU/USD is trading with a {bias_lower} institutional bias."
        )
    elif inst_bias == "Reversal forming":
        parts.append(
            "XAU/USD is showing early reversal conditions across multiple timeframes."
        )
    else:
        parts.append(
            f"XAU/USD is currently in a {bias_lower} market condition."
        )

    # Liquidity
    if h4_liq and h4_liq.swept:
        liq_side = "sell-side" if h4_liq.bullish else "buy-side"
        parts.append(
            f"Price has swept {liq_side} liquidity ({h4_liq.liq_text})."
        )
    else:
        parts.append("No liquidity sweep has been detected on H4.")

    # Structure
    if h4_ms and h4_ms.shifted:
        parts.append(f"Market structure: {h4_ms.structure_text}.")
    else:
        wait_text = h4_ms.structure_text if h4_ms else "waiting for structure break"
        parts.append(f"Structure: {wait_text}.")

    # FVG
    if h4_fvg and h4_fvg.detected:
        parts.append(f"FVG: {h4_fvg.fvg_text}.")
    else:
        parts.append("No valid Fair Value Gap detected on H4.")

    # Risk conditions
    risk_parts: list[str] = []
    if news and news.clear:
        risk_parts.append("news risk is clear")
    else:
        risk_parts.append(f"news BLOCKED ({news.blocking_event if news else 'unknown event'})")
    if vol_status == "OK":
        risk_parts.append("volatility is within range")
    else:
        risk_parts.append(f"volatility is {vol_status.lower()}")
    if session:
        risk_parts.append(f"session: {session.session_text}")
    parts.append("Risk conditions: " + ", ".join(risk_parts) + ".")

    # State conclusion
    state_conclusions = {
        "SIGNAL_READY":
            f"Current status: SIGNAL READY. Score {quality_score}/100. "
            "Manual confirmation required -- no automatic execution.",
        "SETUP_FORMING":
            f"Current status: SETUP FORMING (score {quality_score}/100). "
            "Monitoring for full confluence before confirmation.",
        "WATCHLIST":
            f"Current status: WATCHLIST (score {quality_score}/100). "
            "Conditions are directional but entry criteria not yet met.",
        "NEWS_BLOCKED":
            "Current status: NEWS BLOCKED. "
            "All analysis is paused until the news window expires.",
        "VOLATILITY_UNSTABLE":
            "Current status: VOLATILITY UNSTABLE. "
            "Skipping entry until ATR normalises.",
        "DATA_STALE":
            "Current status: DATA STALE. "
            "Signals are suppressed until fresh candle data is confirmed.",
        "NO_TRADE":
            "Current status: NO TRADE. "
            "No meaningful setup conditions are present.",
    }
    parts.append(state_conclusions.get(market_state, f"Status: {readiness}."))

    return " ".join(parts)


# ═══════════════════════════════════════════════════════════════════════════════
# Helper: calendar utilities
# ═══════════════════════════════════════════════════════════════════════════════

def _next_event(macro_events: list[dict], now: datetime) -> dict | None:
    """Return the next upcoming USD high-impact event."""
    upcoming = []
    for ev in macro_events:
        if str(ev.get("currency", "")).upper() not in ("USD",):
            continue
        if str(ev.get("impact", "")).lower() != "high":
            continue
        raw = ev.get("time", "")
        try:
            ev_time = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
            if ev_time > now:
                upcoming.append((ev_time, ev))
        except Exception:
            continue
    if not upcoming:
        return None
    upcoming.sort(key=lambda x: x[0])
    return upcoming[0][1]


def _mins_to(event: dict | None, now: datetime) -> int | None:
    if not event:
        return None
    raw = event.get("time", "")
    try:
        ev_time = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        return max(0, int((ev_time - now).total_seconds() / 60))
    except Exception:
        return None


# ═══════════════════════════════════════════════════════════════════════════════
# Helper: Telegram alert (SIGNAL_READY only)
# ═══════════════════════════════════════════════════════════════════════════════

def _maybe_telegram_alert(scan_result: dict) -> None:
    """
    Fire a Telegram alert only when:
      - marketState == SIGNAL_READY
      - signal is BUY or SELL
      - score >= 80
      - news CLEAR
    Scanner alerts always include "Manual confirmation required."
    """
    try:
        from config import settings
        watchlist_alerts = getattr(settings, "telegram_watchlist_alerts", False)
    except Exception:
        watchlist_alerts = False

    signal = scan_result.get("signal", "")
    score  = scan_result.get("qualityScore", 0)
    news_s = scan_result.get("news", {}).get("status", "")

    # Hard gates
    if signal not in ("BUY", "SELL"):
        if not watchlist_alerts:
            return
    if score < 80:
        return
    if news_s != "CLEAR":
        return

    # Build alert payload compatible with existing send_signal_alert
    opp     = scan_result.get("opportunity", {})
    liq_v   = scan_result.get("liquidity", {})
    fvg_v   = scan_result.get("fvg", {})
    model_v = scan_result.get("model", {})
    risk_v  = scan_result.get("risk", {})

    # Try to extract price levels from opportunity / scan
    entry_zone = opp.get("entryZone", "")
    entry_price = None
    try:
        # Parse "Near 3285.40" → 3285.40
        entry_price = float(entry_zone.split()[-1]) if entry_zone and entry_zone[0].isalpha() else None
    except Exception:
        pass

    payload = {
        "pair":        "xauusd",
        "signal":      signal,
        "qualityScore": score,
        "entry":       entry_price,
        "stopLoss":    None,
        "takeProfit":  None,
        "riskPips":    None,
        "targetPips":  50,
        "rr":          None,
        "invalidation": None,
        "reason":      scan_result.get("summary", ""),
        "newsStatus":  news_s,
        "model": {
            "liquidity":  liq_v.get("status", ""),
            "structure":  model_v.get("h4Bias", ""),
            "fvg":        fvg_v.get("status", ""),
            "session":    risk_v.get("session", ""),
            "higherTimeframeBias": model_v.get("d1Bias", ""),
        },
    }

    from services.telegram_alert_service import send_signal_alert
    status = send_signal_alert(payload)
    log.info("[scanner] Telegram alert status=%s signal=%s score=%s", status, signal, score)


# ═══════════════════════════════════════════════════════════════════════════════
# Helper: DB persistence
# ═══════════════════════════════════════════════════════════════════════════════

def _maybe_persist(result: dict, db, force: bool = False) -> None:
    """
    Persist scan to DB when:
      - force=True (manual / forced scan)
      - marketState changed
      - signal changed
      - confidence changed >= 10
      - opportunity status changed
      - heartbeat: > 15 min since last persist
    """
    now = datetime.now(timezone.utc)
    ms   = result.get("marketState")
    sig  = result.get("signal")
    conf = result.get("confidence", 0)
    opp_status = result.get("opportunity", {}).get("status")

    should_store = force
    if not should_store:
        lp = _last_persisted
        changed = (
            ms != lp["market_state"]
            or sig != lp["signal"]
            or abs((conf or 0) - (lp["confidence"] or 0)) >= 10
            or opp_status != lp["opportunity_status"]
        )
        heartbeat = (
            lp["persisted_at"] is None
            or (now - lp["persisted_at"]).total_seconds() > HEARTBEAT_MINUTES * 60
        )
        should_store = changed or heartbeat

    if not should_store:
        return

    from db_models import InstitutionalScan

    record = InstitutionalScan(
        instrument       = result.get("instrument", "XAU/USD"),
        scan_mode        = result.get("scanMode", "auto"),
        market_state     = ms or "UNKNOWN",
        signal           = sig or "WAIT",
        institutional_bias = result.get("institutionalBias", ""),
        confidence       = conf,
        readiness        = result.get("readiness", ""),
        summary          = result.get("summary", ""),
        action           = result.get("action", ""),
        key_drivers_json   = json.dumps(result.get("keyDrivers", [])),
        blockers_json      = json.dumps(result.get("blockers", [])),
        opportunity_json   = json.dumps(result.get("opportunity", {})),
        model_json         = json.dumps(result.get("model", {})),
        liquidity_json     = json.dumps(result.get("liquidity", {})),
        fvg_json           = json.dumps(result.get("fvg", {})),
        news_json          = json.dumps(result.get("news", {})),
        risk_json          = json.dumps(result.get("risk", {})),
        raw_payload_json   = json.dumps(result),
    )
    db.add(record)
    db.commit()

    # Update tracker
    _last_persisted.update({
        "market_state":    ms,
        "signal":          sig,
        "confidence":      conf,
        "opportunity_status": opp_status,
        "persisted_at":    now,
    })
    log.info("[scanner] Scan persisted to DB market_state=%s signal=%s score=%s",
             ms, sig, conf)


# ═══════════════════════════════════════════════════════════════════════════════
# Fallback results
# ═══════════════════════════════════════════════════════════════════════════════

def _stale_result(timestamp: str, scan_mode: str, blocker_msg: str) -> dict:
    return {
        "instrument":        "XAU/USD",
        "timestamp":         timestamp,
        "scanMode":          scan_mode,
        "marketState":       "DATA_STALE",
        "signal":            "WAIT",
        "institutionalBias": "Neutral",
        "confidence":        0,
        "readiness":         "Data Stale",
        "summary": (
            f"XAU/USD scanner: data is stale -- {blocker_msg}. "
            "All signals suppressed until fresh candle data is confirmed. "
            "Status: DATA_STALE. Action: CHECK_DATA_FEED."
        ),
        "keyDrivers":  [f"Data stale: {blocker_msg}"],
        "blockers":    [blocker_msg],
        "blockerCodes": ["DATA_STALE"],
        "opportunity": {
            "side":         "NONE",
            "status":       "DATA_STALE",
            "qualityScore": 0,
            "entryZone":    "N/A",
            "invalidation": "N/A",
            "target":       "N/A",
            "rrStatus":     "N/A",
        },
        "model":   {"d1Bias": "Stale", "h4Bias": "Stale", "h1Bias": "Stale",
                    "m15Structure": "Stale", "m5Execution": "Data stale"},
        "liquidity": {"status": "Stale data", "swept": False},
        "fvg":       {"status": "Stale data", "detected": False},
        "news":      {"status": "UNKNOWN"},
        "risk":      {"dataStatus": "STALE", "spreadStatus": "UNKNOWN",
                      "volatilityStatus": "UNKNOWN"},
        "action":    "CHECK_DATA_FEED",
        "qualityScore": 0,
    }


def _error_result(error_msg: str) -> dict:
    ts = datetime.now(timezone.utc).isoformat()
    return _stale_result(ts, "auto", f"Scanner error: {error_msg}")
