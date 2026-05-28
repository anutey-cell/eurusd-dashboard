"""
Consolidated Dashboard Summary
==============================

GET /api/v1/summary  (also aliased at /api/summary)

Returns the full dashboard state in a single JSON payload — designed for AI
consumption. An external analyst (e.g. Claude) can fetch this endpoint once
and immediately cross-reference our engine's signals against independent
analysis without juggling six different routes.

Pulls from the same caches the dashboard uses, so calling /api/summary every
30 s is cheap (no fresh strategist compute beyond the existing 60s cache).

Shape is documented in the response — flat-ish, clearly named, JSON-only.
"""
from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from config import settings
from database import get_db
from models.common import APIResponse
from rate_limit import limiter

router = APIRouter(prefix="/summary", tags=["summary"])
log = logging.getLogger(__name__)

# Tiny local cache — re-aggregates at most every 15s so even an aggressive
# AI poll doesn't trigger per-second strategist computations.
_cache: dict = {"payload": None, "cached_at": 0.0}
_CACHE_TTL_SEC = 15.0


@router.get(
    "",
    response_model=APIResponse[dict],
    summary="Consolidated dashboard state (verdict + market + macro + calendar + bridge + learnings)",
)
@limiter.limit("60/minute")
def dashboard_summary(
    request: Request,
    db: Session = Depends(get_db),
) -> APIResponse[dict]:
    age = time.time() - _cache.get("cached_at", 0)
    if _cache.get("payload") and age < _CACHE_TTL_SEC:
        return APIResponse(data=_cache["payload"], source="summary:cached")

    payload = _build_summary(db)
    _cache["payload"]   = payload
    _cache["cached_at"] = time.time()
    return APIResponse(data=payload, source="summary:fresh")


# ────────────────────────────────────────────────────────────────────────
# Builder
# ────────────────────────────────────────────────────────────────────────

def _build_summary(db: Session) -> dict:
    """
    Compose the consolidated payload. Every section is wrapped — a single
    sub-system failure should never break the whole summary; the field
    just becomes null or `{ "error": "..." }`.
    """
    now = datetime.now(timezone.utc)

    return {
        "generated_at":   now.isoformat(),
        "instrument":     "XAUUSD",
        "operating_mode": _operating_mode(),
        "market":         _safe(_market_section),
        "verdict":        _safe(lambda: _verdict_section(db)),
        "trade_plan":     _safe(lambda: _trade_plan_section(db)),
        "liquidity_model":_safe(lambda: _liquidity_section(db)),
        "macro":          _safe(lambda: _macro_section(db)),
        "calendar":       _safe(_calendar_section),
        "killzone":       _safe(lambda: _killzone_section(db)),
        "ict_alignment":  _safe(lambda: _ict_section(db)),
        "execution_permission": _safe(lambda: _execution_permission(db)),
        "bridge":         _safe(_bridge_section),
        "learnings_24h":  _safe(lambda: _learnings_section(db, 1)),
        "learnings_7d":   _safe(lambda: _learnings_section(db, 7)),
        "diagnostics":    _safe(lambda: _diagnostics_section(db)),
    }


def _safe(fn):
    """Run a section builder; on any failure return {"error": "..."} instead of bubbling."""
    try:
        return fn()
    except Exception as exc:
        log.warning("[summary] section failed: %s", exc)
        return {"error": f"{type(exc).__name__}: {exc}"}


# ── Section builders ────────────────────────────────────────────────────

def _operating_mode() -> dict:
    return {
        "data_mode":               settings.data_mode,
        "use_mandate_strategist":  settings.use_mandate_strategist,
        "allow_demo_trading":      settings.allow_demo_trading,
        "demo_auto_enqueue":       getattr(settings, "demo_auto_enqueue", False),
        "live_trading_authorized": settings.live_trading_authorized,
        "live_execution_allowed":  False,   # mandate: hard-disabled in code
        "mt5_bridge_enabled":      settings.mt5_bridge_enabled,
        "fixed_lot_size":          0.01,
    }


def _market_section() -> dict:
    """Latest M5 close + 24h move + intraday range from candle feed."""
    from data.candles import get_candles
    m5 = get_candles(interval="M5", limit=300, pair="xauusd")
    if not m5 or not m5.candles:
        return {"price": None}
    last  = m5.candles[-1]
    first = m5.candles[0]
    move      = last.close - first.close
    move_pct  = (move / first.close) * 100 if first.close else 0.0
    today_bars = [
        c for c in m5.candles
        if (c.time if c.time.tzinfo else c.time.replace(tzinfo=timezone.utc))
            .astimezone(timezone.utc).date() == datetime.now(timezone.utc).date()
    ]
    last_close_ts = last.time if hasattr(last, "time") else None
    age_s = None
    if last_close_ts:
        ts = last_close_ts if last_close_ts.tzinfo else last_close_ts.replace(tzinfo=timezone.utc)
        age_s = int((datetime.now(timezone.utc) - ts).total_seconds())
    return {
        "price":              round(last.close, 2),
        "price_source":       m5.source,
        "price_close_time":   last_close_ts.isoformat() if last_close_ts else None,
        "price_age_seconds":  age_s,
        "move_24h_pts":       round(move, 2),
        "move_24h_pct":       round(move_pct, 2),
        "session_high":       round(max((c.high for c in today_bars), default=last.close), 2),
        "session_low":        round(min((c.low  for c in today_bars), default=last.close), 2),
        "daily_open":         round(today_bars[0].open, 2) if today_bars else None,
    }


def _verdict_section(db: Session) -> dict:
    """Pull the cached strategist verdict via the router cache (no fresh recompute here)."""
    v = _cached_verdict(db)
    return {
        "decision":                 v.get("decision"),
        "conditions_passed":        v.get("conditions_passed"),
        "estimated_win_rate_range": v.get("estimated_win_rate_range"),
        "execution_status":         v.get("execution_status"),
        "execution_status_reason":  v.get("execution_status_reason"),
        "improvement_note":         v.get("improvement_note"),
        "market_state":             v.get("market_state"),
        "session_classification":   v.get("session_classification"),
        "tf_alignment_label":       v.get("tf_alignment_label"),
        "liquidity_behaviour":      v.get("liquidity_behaviour"),
        "market_sentiment":         v.get("market_sentiment"),
        "setup_score_legacy":       v.get("setup_score"),
        "quality_band":             v.get("quality_band"),
        "final_verdict":            v.get("final_verdict"),
        "conditions":               v.get("conditions", []),
    }


def _trade_plan_section(db: Session) -> dict:
    v = _cached_verdict(db)
    tp = v.get("trade_plan") or {}
    return {
        "entry":           tp.get("entry"),
        "entry_tolerance": tp.get("entry_tolerance"),
        "stop_loss":       tp.get("stop_loss"),
        "tp1":             tp.get("tp1"),
        "tp2":             tp.get("tp2"),
        "tp3":             tp.get("tp3"),
        "risk_reward":     tp.get("risk_reward"),
        "lot_size":        tp.get("lot_size", 0.01),
        "entry_type":      tp.get("entry_type"),
        "invalidation":    tp.get("invalidation"),
        "mt5_execution_object": v.get("mt5_execution_object"),
    }


def _liquidity_section(db: Session) -> dict:
    v = _cached_verdict(db)
    lm = v.get("liquidity_model") or {}
    return {
        "type":             lm.get("type"),
        "confirmed":        lm.get("confirmed"),
        "swept_level_text": lm.get("swept_level"),
        "target_liquidity": lm.get("target_liquidity"),
        "sweep_detected":   lm.get("sweep_detected", False),
        "sweep_side":       lm.get("sweep_side"),
        "sweep_level":      lm.get("sweep_level"),
        "sweep_reclaimed":  lm.get("sweep_reclaimed", False),
        "key_zones":        v.get("key_zones", {}),
    }


def _macro_section(db: Session) -> dict:
    v  = _cached_verdict(db)
    mc = v.get("macro_context") or {}
    return {
        "dxy_bias":         mc.get("dxy_bias"),
        "yields_bias":      mc.get("yields_bias"),
        "gold_macro_bias":  mc.get("gold_macro_bias"),
        "macro_alignment":  mc.get("macro_alignment"),
        "news_risk":        mc.get("news_risk"),
    }


def _calendar_section() -> dict:
    """Today's USD events + next high-impact USD event ahead."""
    from data.calendar import get_calendar
    today = get_calendar()
    today_events = [e.model_dump() for e in (today.events or [])]
    today_high = [e for e in today_events if (e.get("impact") or "").lower() == "high"]

    # Look ahead through this week's events for next high-impact USD
    next_high = None
    now = datetime.now(timezone.utc)
    for delta in range(8):
        from datetime import timedelta as _td
        d_str = (now + _td(days=delta)).strftime("%Y-%m-%d")
        try:
            day = get_calendar(date=d_str, high_impact_only=True)
        except Exception:
            continue
        for e in (day.events or []):
            ev_t = e.time
            if not isinstance(ev_t, datetime):
                continue
            ev_t = ev_t if ev_t.tzinfo else ev_t.replace(tzinfo=timezone.utc)
            if ev_t > now:
                next_high = {
                    "time":          ev_t.isoformat(),
                    "event":         e.event,
                    "currency":      e.currency,
                    "impact":        e.impact,
                    "minutes_until": int((ev_t - now).total_seconds() / 60),
                }
                break
        if next_high:
            break

    return {
        "today_events_count":      len(today_events),
        "today_high_impact_count": len(today_high),
        "today_events": [
            {
                "time": (e.get("time").isoformat() if isinstance(e.get("time"), datetime) else e.get("time")),
                "event": e.get("event"),
                "currency": e.get("currency"),
                "impact": e.get("impact"),
            } for e in today_events
        ],
        "next_high_impact": next_high,
        "provider": settings.calendar_provider,
    }


def _killzone_section(db: Session) -> dict:
    """Current killzone label, posture, edge score."""
    try:
        from services.killzone_analyzer import get_current_recommendation
        kz = get_current_recommendation(db, lookback_days=60) or {}
    except Exception as exc:
        return {"error": str(exc)}
    return {
        "current_kz":   kz.get("current_kz"),
        "label":        kz.get("label"),
        "posture":      kz.get("posture"),
        "edge_score":   kz.get("edge_score"),
        "sample_size":  kz.get("sample_size"),
        "win_rate":     kz.get("win_rate"),
        "rationale":    kz.get("rationale"),
    }


def _ict_section(db: Session) -> dict:
    """ICT framework alignment — PO3, Daily Open, Premium/Discount, Judas, total score."""
    try:
        from services.ict_advanced import compute_ict_alignment
        from data.candles import get_candles
        m15 = get_candles(interval="M15", limit=200, pair="xauusd")
        h4  = get_candles(interval="H4",  limit=50,  pair="xauusd")
        v   = _cached_verdict(db)
        direction = v.get("decision") if v.get("decision") in ("BUY", "SELL") else None
        ict = compute_ict_alignment(
            candles_m15=(m15.candles if m15 else []),
            candles_h4 =(h4.candles  if h4  else []),
            at=datetime.now(timezone.utc),
            signal_direction=direction,
        )
        if not ict:
            return {"score": None}
        return {
            "score":             ict.score,
            "posture":           ict.posture,
            "po3_phase":         ict.po3_phase,
            "daily_open_bias":   ict.daily_open_bias,
            "pd_zone":           ict.pd_zone,
            "judas_detected":    ict.judas_detected,
            "blocking_factors":  ict.blocking_factors,
        }
    except Exception as exc:
        return {"error": str(exc)}


def _execution_permission(db: Session) -> dict:
    v = _cached_verdict(db)
    ep = v.get("execution_permission") or {}
    return {
        "allow_alert":          ep.get("allow_alert"),
        "allow_demo_execution": ep.get("allow_demo_execution"),
        "allow_live_execution": ep.get("allow_live_execution"),
        "execution_status":     ep.get("execution_status"),
        "reason":               ep.get("reason"),
    }


def _bridge_section() -> dict:
    """Bridge daemon heartbeat + MT5 queue counts."""
    try:
        from routers.bridge import _BRIDGE_HEARTBEAT
        from database import SessionLocal
        from db_models import PendingExecution
        now = datetime.now(timezone.utc)
        daemons = []
        for did, ts in _BRIDGE_HEARTBEAT.items():
            age = int((now - ts).total_seconds())
            daemons.append({
                "id":           did,
                "last_seen":    ts.isoformat(),
                "age_seconds":  age,
                "is_fresh":     age < 120,
            })
        with SessionLocal() as _db:
            queue = {}
            for st in ("PENDING", "EXECUTING", "ACCEPTED", "REJECTED", "FAILED", "EXPIRED", "CLOSED"):
                queue[st] = _db.query(PendingExecution).filter(PendingExecution.status == st).count()
        return {
            "any_daemon_fresh": any(d["is_fresh"] for d in daemons) if daemons else False,
            "daemons":          daemons,
            "queue":            queue,
        }
    except Exception as exc:
        return {"error": str(exc)}


def _learnings_section(db: Session, window_days: int) -> dict:
    """Closed-trade aggregates over the window."""
    from services.learnings import build_learnings
    data = build_learnings(db, window_days=window_days)
    # Trim to the high-signal fields — the full structure is at /strategist/learnings
    return {
        "window_days":          data["window_days"],
        "sample_size":          data["sample_size"],
        "headline":             data["headline"],
        "overall":              data["overall"],
        "by_conditions_passed": data["by_conditions_passed"],
        "by_kz_direction":      data["by_kz_direction"],
        "calibration_notes":    data["calibration_notes"],
    }


def _diagnostics_section(db: Session) -> dict:
    v  = _cached_verdict(db)
    d  = v.get("diagnostics") or {}
    return {
        "direction_source":     d.get("direction_source"),
        "plan_source":          d.get("plan_source"),
        "d1_bias_local":        d.get("d1_bias_local"),
        "h4_bias_local":        d.get("h4_bias_local"),
        "sweep_rationale":      d.get("sweep_rationale"),
        "scanner_state":        d.get("scanner_state"),
        "scanner_score":        d.get("scanner_score"),
        "fx_provider":          settings.active_fx_provider,
        "calendar_provider":    settings.calendar_provider,
    }


# ── Verdict access (uses the strategist router's own 60s cache) ─────────

def _cached_verdict(db: Session) -> dict:
    """
    Fetch the current strategist verdict from the strategist router's module
    cache when fresh; otherwise compute one. Avoids triggering side effects
    (Telegram alerts, MT5 enqueue, persistence) — those only fire when the
    strategist router or background loop calls run_once directly.
    """
    try:
        from routers import strategist as st_router
        cache = getattr(st_router, "_cache", None)
        if cache and cache.get("verdict") and (time.time() - cache.get("cached_at", 0)) < 60:
            return cache["verdict"]
    except Exception:
        pass
    # Cache miss — read-only fallback (no side effects)
    try:
        from services.strategist import make_decision
        return make_decision(db)
    except Exception as exc:
        log.warning("[summary] verdict fallback failed: %s", exc)
        return {}
