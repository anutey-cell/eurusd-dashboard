"""
Background scheduler for autonomous 2-week live testing.

Runs three loops independently inside the FastAPI process:

  1. Scanner loop (every 60s)
     - Fires dual_engine_runner (logs swing + trend_pullback observations)
     - Picks up SIGNAL_READY state changes
     - Existing institutional_scanner._maybe_telegram_alert fires Telegram

  2. High-probability prediction loop (every 5 min)
     - Calls high_probability_predictor.predict_xauusd()
     - When band=STRONG with actionable trade plan, fires a Telegram alert
     - Dedupes by fingerprint to avoid spam

  3. Drawdown alert loop (every 30 min)
     - Checks both swing + trend_pullback equity drawdown
     - Fires Telegram alert if > 10% drawdown breached

  4. Daily summary loop (once per day at 07:00 UTC)
     - Sends recap of yesterday's signals + observation tally

No external dependencies — uses pure asyncio. Survives task exceptions
(each loop wrapped in try/except). Logs every iteration.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
from datetime import datetime, timezone, timedelta
from typing import Optional

log = logging.getLogger(__name__)

# ── Tunables ──────────────────────────────────────────────────────────────────

SCANNER_INTERVAL_SEC      = 60
PREDICTION_INTERVAL_SEC   = 300        # 5 min — predictor uses cached scan
DRAWDOWN_INTERVAL_SEC     = 1800       # 30 min
DAILY_SUMMARY_HOUR_UTC    = 7          # 07:00 UTC

# Dedupe window for prediction alerts (don't spam)
PREDICTION_ALERT_COOLDOWN_MIN = 60
_last_prediction_alert: dict[str, datetime] = {}    # fingerprint -> sent_at


# ═══════════════════════════════════════════════════════════════════════════════
# Helper: send Telegram for STRONG prediction
# ═══════════════════════════════════════════════════════════════════════════════

def _format_prediction_alert(pred_dict: dict) -> str:
    """Build the Telegram alert body for a STRONG prediction."""
    direction = pred_dict.get("direction", "")
    prob      = pred_dict.get("probability", 0)
    aligned   = pred_dict.get("alignedCount", 0)
    total     = pred_dict.get("totalLayers", 0)
    plan      = pred_dict.get("tradePlan") or {}

    # Per-layer status lines
    layer_lines = []
    for L in pred_dict.get("layers", []):
        icon = {"GREEN": "🟢", "YELLOW": "🟡", "RED": "🔴"}.get(L.get("status"), "⚪")
        layer_lines.append(
            f"{icon} <b>{L['name']}</b> ({L['score']}) — {L['direction']}"
        )
    layer_block = "\n".join(layer_lines)

    msg = (
        f"⚡ <b>HIGH-PROBABILITY {direction} ALERT</b>\n"
        f"\n"
        f"<b>Probability:</b> {prob}%\n"
        f"<b>Aligned layers:</b> {aligned}/{total}\n"
        f"\n"
        f"<b>Layer breakdown:</b>\n"
        f"{layer_block}\n"
    )
    if plan and plan.get("entry"):
        rr_s = f"1:{plan.get('rr'):.2f}" if plan.get("rr") else "—"
        msg += (
            f"\n<b>Trade Plan:</b>\n"
            f"Entry: <b>{plan.get('entry'):.2f}</b>\n"
            f"Stop Loss: <b>{plan.get('stopLoss'):.2f}</b>\n"
            f"Take Profit: <b>{plan.get('takeProfit'):.2f}</b>\n"
            f"R:R: {rr_s}\n"
            f"Risk: {plan.get('riskPoints')} pts\n"
            f"Target: {plan.get('targetPoints')} pts\n"
            f"Quality: {plan.get('qualityScore')}/100\n"
        )
    msg += (
        f"\n⚠️ <i>Decision support only. Review all 7 layers and execute manually if you agree. "
        f"Broker execution remains disabled.</i>"
    )
    return msg


def _build_prediction_fingerprint(pred_dict: dict) -> str:
    """Stable fingerprint to dedupe same-trade-plan alerts."""
    plan = pred_dict.get("tradePlan") or {}
    raw = (
        f"{pred_dict.get('direction','')}:"
        f"{pred_dict.get('band','')}:"
        f"{float(plan.get('entry', 0)):.1f}:"
        f"{float(plan.get('stopLoss', 0)):.1f}:"
        f"{float(plan.get('takeProfit', 0)):.1f}"
    )
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def _maybe_alert_strong_prediction(pred_dict: dict) -> str:
    """Fire Telegram for STRONG predictions with trade plan. Returns status."""
    band = pred_dict.get("band")
    direction = pred_dict.get("direction")
    plan = pred_dict.get("tradePlan")

    if band != "STRONG":
        return "skipped: not STRONG"
    if direction not in ("BUY", "SELL"):
        return f"skipped: direction={direction}"
    if not plan or not plan.get("entry"):
        return "skipped: no trade plan"

    fp = _build_prediction_fingerprint(pred_dict)
    now = datetime.now(timezone.utc)
    last = _last_prediction_alert.get(fp)
    if last and (now - last).total_seconds() < PREDICTION_ALERT_COOLDOWN_MIN * 60:
        return f"skipped: cooldown ({(now-last).total_seconds()/60:.1f} min ago)"

    try:
        from services.telegram_alert_service import send_telegram_message, telegram_alerts_enabled
        if not telegram_alerts_enabled():
            return "skipped: telegram disabled"
        msg = _format_prediction_alert(pred_dict)
        ok = send_telegram_message(msg)
        if ok:
            _last_prediction_alert[fp] = now
            return "sent"
        return "failed"
    except Exception as exc:
        log.warning("[scheduler] Prediction alert send failed: %s", exc)
        return f"error: {exc}"


# ═══════════════════════════════════════════════════════════════════════════════
# Loops
# ═══════════════════════════════════════════════════════════════════════════════

async def _scanner_loop():
    """Auto-scan + dual-engine paper observation logging."""
    log.info("[scheduler] scanner loop started (every %ds)", SCANNER_INTERVAL_SEC)
    while True:
        try:
            await asyncio.sleep(SCANNER_INTERVAL_SEC)
            from database import SessionLocal
            from services.dual_engine_runner import run_dual_engines
            # Run blocking DB work in a thread to avoid blocking the event loop
            await asyncio.to_thread(_run_scanner_iteration)
        except asyncio.CancelledError:
            log.info("[scheduler] scanner loop cancelled")
            raise
        except Exception as exc:
            log.warning("[scheduler] scanner loop error: %s", exc)


def _run_scanner_iteration():
    """Single scanner-loop iteration (runs in thread)."""
    from database import SessionLocal
    from services.dual_engine_runner import run_dual_engines
    with SessionLocal() as db:
        result = run_dual_engines(db)
        log.debug("[scheduler] scan iteration: swing=%s tp=%s",
                  result.get("swing", {}).get("signal"),
                  result.get("trend_pullback", {}).get("signal"))


# ═══════════════════════════════════════════════════════════════════════════════
# Autonomous Executor loop — only fires when ALL gates pass
# ═══════════════════════════════════════════════════════════════════════════════

# Latest attempt snapshot exposed via /execution/autonomous/status
_last_auto_attempt: dict | None = None


def get_last_auto_attempt() -> dict | None:
    """Return the most recent ExecutionAttempt snapshot (None if none yet)."""
    return _last_auto_attempt


async def _auto_executor_loop():
    """
    LEGACY 5-gate auto-executor loop. Only spawned when
    settings.use_mandate_strategist is False (back-compat fallback).

    The mandate strategist (services.strategist_runner) is the authoritative
    decision engine — this older loop produces parallel orders with a
    different scoring model and lot policy. Keep it dormant in mandate mode.
    """
    from config import settings
    interval = getattr(settings, "auto_execution_interval_sec", 60)
    log.info("[scheduler] LEGACY auto-executor loop started (every %ds, enabled=%s)",
             interval, settings.auto_execution_enabled)
    while True:
        try:
            await asyncio.sleep(interval)
            await asyncio.to_thread(_run_auto_executor_iteration)
        except asyncio.CancelledError:
            log.info("[scheduler] auto-executor loop cancelled")
            raise
        except Exception as exc:
            log.warning("[scheduler] auto-executor loop error: %s", exc)


# ── Mandate-strategist loop ──────────────────────────────────────────────────

STRATEGIST_INTERVAL_SEC = 60      # one verdict per minute during active hours
_last_strategist_verdict: dict | None = None


def get_last_strategist_verdict() -> dict | None:
    """Most recent verdict the background loop produced (for /scheduler/status)."""
    return _last_strategist_verdict


async def _strategist_loop():
    """
    Runs the institutional demo-mandate strategist every 60s autonomously.

    On every tick:
      • make_decision(db)
      • Telegram alert (BUY/SELL with cooldown, STAND ASIDE if enabled)
      • PendingExecution enqueue at lot=0.01 if all gates pass
      • Append to strategist_verdicts (mandate signal log)

    The dashboard's /strategist/decision cache hits this same verdict — so
    one fresh compute per minute serves both autonomous operation AND the UI.
    """
    log.info("[scheduler] mandate-strategist loop started (every %ds)", STRATEGIST_INTERVAL_SEC)
    while True:
        try:
            await asyncio.sleep(STRATEGIST_INTERVAL_SEC)
            await asyncio.to_thread(_run_strategist_iteration)
        except asyncio.CancelledError:
            log.info("[scheduler] strategist loop cancelled")
            raise
        except Exception as exc:
            log.warning("[scheduler] strategist loop error: %s", exc)


def _run_strategist_iteration():
    """Single strategist iteration (runs in thread; opens its own DB session)."""
    global _last_strategist_verdict
    from database import SessionLocal
    from services.strategist_runner import run_once
    with SessionLocal() as db:
        v = run_once(db)
        _last_strategist_verdict = v
        log.info(
            "[strategist_loop] %s · %s/5 · %s · %s",
            v.get("decision"),
            v.get("conditions_passed"),
            v.get("execution_status"),
            v.get("final_verdict", "")[:100],
        )


# ── Daily market briefing loop ───────────────────────────────────────────────
#
# Fires once per day at 20:00 UTC (= 23:00 Africa/Nairobi / EAT). This is
# the close of the NY session + just before Asian open — natural reflection
# window for the operator. Weekends are skipped (Sat recap + Sun forecast
# handle that).

BRIEFING_CHECK_INTERVAL_SEC = 60     # cheap poll
DAILY_BRIEFING_HOUR_UTC     = 20     # 23:00 EAT


async def _daily_briefing_loop():
    """
    Sends the daily XAUUSD market briefing to Telegram once per day at
    20:00 UTC (23:00 EAT). The send_briefing_if_due() helper dedupes
    by calendar date — this loop can wake every minute cheaply and still
    fire exactly once per day.

    Opt-in via settings.telegram_hourly_briefing (env var name preserved
    for back-compat; same flag governs both the daily briefing + the
    weekend newsletters + the weekly digest).
    """
    log.info("[scheduler] daily briefing loop started "
             "(fires %02d:00 UTC = 23:00 EAT)", DAILY_BRIEFING_HOUR_UTC)
    while True:
        try:
            await asyncio.sleep(BRIEFING_CHECK_INTERVAL_SEC)
            await asyncio.to_thread(_run_briefing_iteration)
        except asyncio.CancelledError:
            log.info("[scheduler] daily briefing loop cancelled")
            raise
        except Exception as exc:
            log.warning("[scheduler] daily briefing loop error: %s", exc)


def _run_briefing_iteration():
    """
    Single briefing check (runs in thread). Only fires within the first
    2 minutes of 20:00 UTC so we don't accidentally late-send during a
    restart that lands at 20:15.
    """
    now = datetime.now(timezone.utc)
    if now.hour != DAILY_BRIEFING_HOUR_UTC or now.minute >= 2:
        return
    from database import SessionLocal
    from services.hourly_briefing import send_briefing_if_due
    with SessionLocal() as db:
        send_briefing_if_due(db)


# ── Weekly learning digest loop ──────────────────────────────────────────────

# Fire Sunday at 18:00 UTC (markets are closed worldwide → operator can read it)
DIGEST_WEEKDAY_UTC  = 6       # Mon=0 … Sun=6
DIGEST_HOUR_UTC     = 18
DIGEST_CHECK_INTERVAL_SEC = 5 * 60     # poll every 5 minutes
_last_digest_iso_week: str = ""        # de-dup key e.g. "2026-W21"


async def _weekly_digest_loop():
    """
    Sends a weekly learnings digest to Telegram once per ISO week,
    fired at Sunday 18:00 UTC. Aggregates the prior 7 days of closed
    strategist trades into WR / expectancy / top winners / losers /
    calibration notes — so the operator gets a structured progress
    report every Sunday evening.

    Opt-in via settings.telegram_hourly_briefing (same switch).
    """
    log.info("[scheduler] weekly digest loop started (Sun %02d:00 UTC)", DIGEST_HOUR_UTC)
    while True:
        try:
            await asyncio.sleep(DIGEST_CHECK_INTERVAL_SEC)
            await asyncio.to_thread(_run_digest_iteration)
        except asyncio.CancelledError:
            log.info("[scheduler] weekly digest loop cancelled")
            raise
        except Exception as exc:
            log.warning("[scheduler] weekly digest loop error: %s", exc)


def _run_digest_iteration():
    """Single weekly-digest check. Only fires Sunday 18:00 UTC, once/week."""
    global _last_digest_iso_week
    from config import settings
    if not getattr(settings, "telegram_hourly_briefing", False):
        return        # reuse the briefing opt-in
    now = datetime.now(timezone.utc)
    if now.weekday() != DIGEST_WEEKDAY_UTC or now.hour != DIGEST_HOUR_UTC:
        return
    iso = f"{now.isocalendar().year}-W{now.isocalendar().week:02d}"
    if iso == _last_digest_iso_week:
        return        # already sent for this week

    from database import SessionLocal
    from services.learnings import build_learnings, format_weekly_digest
    from services.strategist_runner import _send_plain
    with SessionLocal() as db:
        data = build_learnings(db, window_days=7)
        msg  = format_weekly_digest(data)
    try:
        _send_plain(msg)
        _last_digest_iso_week = iso
        log.info("[digest] sent weekly digest for %s (n=%d)", iso, data["sample_size"])
    except Exception as exc:
        log.warning("[digest] send failed: %s", exc)


# ── Weekend newsletter loops ─────────────────────────────────────────────────
#   Saturday 09:00 UTC → recap newsletter (week just closed)
#   Sunday   18:00 UTC → forecast newsletter (week ahead, before 22:00 reopen)

NEWSLETTER_CHECK_INTERVAL_SEC = 5 * 60     # poll every 5 minutes
SATURDAY_RECAP_HOUR_UTC       = 9
SUNDAY_FORECAST_HOUR_UTC      = 18
_last_recap_iso_week:    str = ""
_last_forecast_iso_week: str = ""


async def _weekend_newsletter_loop():
    """
    Fires the Saturday recap (Sat 09:00 UTC) and Sunday forecast (Sun 18:00 UTC).
    Both are one-per-week (deduped by ISO week). Gated by the same
    telegram_hourly_briefing opt-in setting.
    """
    log.info("[scheduler] weekend newsletter loop started "
             "(Sat %02d:00 recap, Sun %02d:00 forecast UTC)",
             SATURDAY_RECAP_HOUR_UTC, SUNDAY_FORECAST_HOUR_UTC)
    while True:
        try:
            await asyncio.sleep(NEWSLETTER_CHECK_INTERVAL_SEC)
            await asyncio.to_thread(_run_newsletter_iteration)
        except asyncio.CancelledError:
            log.info("[scheduler] weekend newsletter loop cancelled")
            raise
        except Exception as exc:
            log.warning("[scheduler] weekend newsletter loop error: %s", exc)


def _run_newsletter_iteration():
    """Single weekend newsletter check. Fires Sat 09:00 or Sun 18:00 UTC."""
    global _last_recap_iso_week, _last_forecast_iso_week
    from config import settings
    if not getattr(settings, "telegram_hourly_briefing", False):
        return

    now = datetime.now(timezone.utc)
    iso = f"{now.isocalendar().year}-W{now.isocalendar().week:02d}"

    from database import SessionLocal
    from services.strategist_runner import _send_plain
    from services.weekend_newsletters import build_saturday_recap, build_sunday_forecast

    # Saturday recap
    if (now.weekday() == 5 and now.hour == SATURDAY_RECAP_HOUR_UTC
            and iso != _last_recap_iso_week):
        with SessionLocal() as db:
            try:
                msg = build_saturday_recap(db)
                _send_plain(msg)
                _last_recap_iso_week = iso
                log.info("[newsletter] Saturday recap sent for %s", iso)
            except Exception as exc:
                log.warning("[newsletter] Saturday recap send failed: %s", exc)

    # Sunday forecast
    if (now.weekday() == 6 and now.hour == SUNDAY_FORECAST_HOUR_UTC
            and iso != _last_forecast_iso_week):
        with SessionLocal() as db:
            try:
                msg = build_sunday_forecast(db)
                _send_plain(msg)
                _last_forecast_iso_week = iso
                log.info("[newsletter] Sunday forecast sent for %s", iso)
            except Exception as exc:
                log.warning("[newsletter] Sunday forecast send failed: %s", exc)


# ── Pre-Phase-0: split candle top-up loops (fast M5/M15, slow HTF) ──────────
#
# TwelveData free tier: 8 req/min, 800/day. Budget:
#   fast : M5+M15 every 5 min  = 2 × 288 = 576/day
#   slow : H1+H4+D1 every 15 min = 3 × 96 = 288/day
#   total ≈ 864/day (paid tier if budget tightens; still ~1.2s peak p95)
#
# Rationale: M5 has a 10-min staleness threshold in the brief. A 15-min
# ingestion cadence guarantees M5 spends ~1/3 of every cycle "stale" —
# which drops data_quality_score and (post-Phase-2) forces the strategist
# to exclude M5. Fixing that requires the split cadence.

_CANDLE_INGESTION_FAST_S = 300    # 5 min — M5 + M15
_CANDLE_INGESTION_SLOW_S = 900    # 15 min — H1 + H4 + D1


async def _candle_ingestion_fast_loop():
    """Top-up M5 + M15 every 5 min to stay inside their freshness thresholds."""
    log.info("[scheduler] candle ingestion FAST loop (M5+M15) started (every %ds)",
             _CANDLE_INGESTION_FAST_S)
    while True:
        try:
            await asyncio.to_thread(_run_candle_ingestion, ("M5", "M15"))
        except asyncio.CancelledError:
            log.info("[scheduler] candle ingestion FAST loop cancelled")
            break
        except Exception as exc:
            log.warning("[scheduler] candle ingestion FAST loop error: %s", exc)
        await asyncio.sleep(_CANDLE_INGESTION_FAST_S)


async def _candle_ingestion_slow_loop():
    """Top-up H1 + H4 + D1 every 15 min (thresholds allow this cadence)."""
    log.info("[scheduler] candle ingestion SLOW loop (H1+H4+D1) started (every %ds)",
             _CANDLE_INGESTION_SLOW_S)
    while True:
        try:
            await asyncio.to_thread(_run_candle_ingestion, ("H1", "H4", "D1"))
        except asyncio.CancelledError:
            log.info("[scheduler] candle ingestion SLOW loop cancelled")
            break
        except Exception as exc:
            log.warning("[scheduler] candle ingestion SLOW loop error: %s", exc)
        await asyncio.sleep(_CANDLE_INGESTION_SLOW_S)


def _run_candle_ingestion(only_tf: Optional[tuple] = None):
    from database import SessionLocal
    from services.candle_ingestion import top_up_recent, ingest_gc_futures
    with SessionLocal() as db:
        r = top_up_recent(db, pair="xauusd", only_timeframes=only_tf)
        if r["totals"]["inserted"] or r["totals"]["errors"]:
            log.info("[candle_ingestion] top-up %s: inserted=%d skipped=%d errors=%d",
                     only_tf or "ALL",
                     r["totals"]["inserted"], r["totals"]["skipped"],
                     r["totals"]["errors"])
        # P189 — independent GC=F ingestion (never contaminates XAU/USD).
        # Only run on the fast loop (M5+M15) — enough granularity for basis work.
        if only_tf and "M5" in only_tf:
            try:
                gc_r = ingest_gc_futures(db, timeframes=("M5", "M15", "H1"))
                if gc_r["totals"]["inserted"]:
                    log.info("[gc_ingest] inserted=%d skipped=%d errors=%d",
                              gc_r["totals"]["inserted"], gc_r["totals"]["skipped"],
                              gc_r["totals"]["errors"])
            except Exception as exc:
                log.debug("[gc_ingest] skipped: %s", exc)


# ── P131: data-freshness sentinel loop ──────────────────────────────────────

_FRESHNESS_CHECK_INTERVAL_S = 600     # 10 min — faster detection of ingestion outages


async def _data_freshness_loop():
    """
    Every 30 min check historical_candles staleness across timeframes.
    Fires a Telegram alert once/day per stale timeframe.
    """
    log.info("[scheduler] data-freshness loop started (every %ds)",
             _FRESHNESS_CHECK_INTERVAL_S)
    # Small initial delay so we don't spam on startup before ingestion runs
    await asyncio.sleep(60)
    while True:
        try:
            await asyncio.to_thread(_run_freshness_check)
        except asyncio.CancelledError:
            log.info("[scheduler] data-freshness loop cancelled")
            break
        except Exception as exc:
            log.warning("[scheduler] data-freshness loop error: %s", exc)
        await asyncio.sleep(_FRESHNESS_CHECK_INTERVAL_S)


def _run_freshness_check():
    from database import SessionLocal
    from services.data_freshness import maybe_alert
    with SessionLocal() as db:
        r = maybe_alert(db) or {}
        if r.get("stale"):
            log.warning("[freshness] STALE timeframes: %s · details=%s",
                        r["stale"], {k: v for k, v in r.get("details", {}).items()
                                       if k in r["stale"]})


# ── P135: VP Trap measurement outcome loop ───────────────────────────────────

_VP_MEASUREMENT_INTERVAL_S = 60


async def _vp_trap_measurement_loop():
    """
    Every 60s, walk PENDING+TRIGGERED VP Trap measurement rows and
    advance them (TRIGGER, TP1_HIT, TP2_HIT, STOPPED, INVALIDATED, EXPIRED)
    against the current XAU/USD price. This is the outcome tracker for
    the 30-day protocol.
    """
    from config import settings
    log.info("[scheduler] vp-trap measurement loop started (every %ds)",
             _VP_MEASUREMENT_INTERVAL_S)
    # Skip loop entirely if measurement is disabled
    while True:
        try:
            if getattr(settings, "vp_trap_measurement_enabled", True):
                await asyncio.to_thread(_run_vp_measurement)
        except asyncio.CancelledError:
            log.info("[scheduler] vp-trap measurement loop cancelled")
            break
        except Exception as exc:
            log.warning("[scheduler] vp-trap measurement loop error: %s", exc)
        await asyncio.sleep(_VP_MEASUREMENT_INTERVAL_S)


def _run_vp_measurement():
    from database import SessionLocal
    from services.vp_trap_measurement import advance_outcomes
    with SessionLocal() as db:
        r = advance_outcomes(db) or {}
        # Log only when something actually changed
        interesting = {k: v for k, v in r.items() if isinstance(v, int) and v > 0}
        if interesting:
            log.info("[vp_measurement] outcomes advanced: %s", interesting)


# ── Shadow trade simulator outcome loop ────────────────────────────────────────
#
# Runs every N seconds (default 60) and walks every PENDING + TRIGGERED
# shadow_trades row, advancing state against live M5 candles. Fully independent
# of live execution — no orders, no alerts. Pure data pipeline.

async def _shadow_trade_advance_loop():
    from config import settings
    interval = getattr(settings, "shadow_trade_advance_interval_s", 60)
    log.info("[scheduler] shadow-trade advance loop started (every %ds)", interval)
    await asyncio.sleep(30)  # let ingest + freshness boot first
    while True:
        try:
            if getattr(settings, "shadow_trade_recording_enabled", True):
                await asyncio.to_thread(_run_shadow_trade_advance)
        except asyncio.CancelledError:
            log.info("[scheduler] shadow-trade advance loop cancelled")
            break
        except Exception as exc:
            log.warning("[scheduler] shadow-trade advance loop error: %s", exc)
        await asyncio.sleep(interval)


def _run_shadow_trade_advance():
    from config import settings
    from database import SessionLocal
    from services.shadow_trade_simulator import advance_outcomes
    with SessionLocal() as db:
        r = advance_outcomes(
            db,
            expiry_hours=getattr(settings, "shadow_trade_expiry_hours", 12),
        ) or {}
        interesting = {k: v for k, v in r.items()
                        if isinstance(v, int) and v > 0}
        if interesting:
            log.info("[shadow_trade] outcomes advanced: %s", interesting)


# ── Predator engine loop (2026-08-12) ────────────────────────────────────────
#
# Runs every N seconds. On each tick:
#   1. Evaluates 3 walk-forward-validated detectors
#   2. Dedupes by fingerprint (max 1 per hour per pattern per 5pt bucket)
#   3. Records each fresh signal to shadow_trades (grade=archetype)
#   4. Sends Telegram if predator_telegram_enabled is True
#
# No trade placement. Pure signal + measurement.

_PREDATOR_SEEN: dict[str, float] = {}   # fingerprint -> unix ts


async def _strategist_ledger_resolver_loop():
    """Runs every 60s: promotes governor READY (retry), resolves closed
    strategist BUYs, resolves SELL shadows, refreshes lifecycle canonicalization.
    Fail-open — a resolver failure never blocks trading."""
    log.info("[scheduler] strategist ledger resolver loop started (60s)")
    await asyncio.sleep(45)  # let other loops warm up first
    _lifecycle_pass = 0
    while True:
        try:
            # First: try to promote governor from NOT_READY once heartbeat arrives
            try:
                from services.portfolio_governor import retry_ready
                retry_ready()
            except Exception: pass

            from database import SessionLocal
            with SessionLocal() as db:
                try:
                    from services.strategist_buy_resolver import resolve_closed_buys
                    r = resolve_closed_buys(db, limit=100)
                    if r.get("resolved"):
                        log.info("[strategist_resolver] BUY resolved=%d scanned=%d",
                                  r["resolved"], r["scanned"])
                except Exception as _b: log.debug("[strategist_resolver] BUY: %s", _b)
                try:
                    from services.strategist_shadow_ledger import resolve_sell_shadows
                    r = resolve_sell_shadows(db, limit=200)
                    if r.get("resolved"):
                        log.info("[strategist_resolver] SELL shadow resolved=%d scanned=%d",
                                  r["resolved"], r["scanned"])
                except Exception as _s: log.debug("[strategist_resolver] SELL: %s", _s)
                # Lifecycle recanonicalization every 15 minutes (15 × 60s iterations)
                _lifecycle_pass += 1
                if _lifecycle_pass % 15 == 0:
                    try:
                        from services.lifecycle_canonicalization import recanonicalize_all
                        for eng in ("STRATEGIST_BUY", "STRATEGIST_SELL_SHADOW"):
                            r = recanonicalize_all(db, engine_filter=eng)
                            if r.get("lifecycle_ops"):
                                log.info("[lifecycle_canon] %s: verdicts=%d → lifecycle_ops=%d",
                                          eng, r["verdicts_scanned"], r["lifecycle_ops"])
                    except Exception as _l: log.debug("[lifecycle_canon]: %s", _l)
                # Forward coverage detection + classification every 15 min
                if _lifecycle_pass % 15 == 0:
                    try:
                        from services.forward_coverage import (
                            detect_and_seed_expansions, classify_pending_events,
                        )
                        r = detect_and_seed_expansions(db, since_days=2)
                        if r.get("events_created"):
                            log.info("[forward_coverage] detected %d new expansions",
                                      r["events_created"])
                        r = classify_pending_events(db, limit=500)
                        if r.get("classified"):
                            log.info("[forward_coverage] classified %d events: %s",
                                      r["classified"], r.get("per_class"))
                    except Exception as _c: log.debug("[forward_coverage]: %s", _c)
        except asyncio.CancelledError:
            log.info("[scheduler] strategist ledger resolver cancelled"); break
        except Exception as exc:
            log.warning("[scheduler] strategist ledger resolver error: %s", exc)
        await asyncio.sleep(60)


async def _predator_loop():
    from config import settings
    interval = getattr(settings, "predator_loop_interval_s", 60)
    log.info("[scheduler] predator engine loop started (every %ds)", interval)
    await asyncio.sleep(30)   # let data pipeline warm up first
    while True:
        try:
            if getattr(settings, "predator_enabled", True):
                await asyncio.to_thread(_run_predator_iteration)
        except asyncio.CancelledError:
            log.info("[scheduler] predator loop cancelled")
            break
        except Exception as exc:
            log.warning("[scheduler] predator loop error: %s", exc)
        await asyncio.sleep(interval)


# ARMED→INVALIDATED tracking. Keyed by archetype+5pt bucket so we know when
# a setup we alerted on has slipped away without firing.
_PREDATOR_ARMED_TRACKING: dict[str, dict] = {}
_PREDATOR_ARMED_STALE_S = 900   # 15 min — if ARMED >this long without FIRE, INVALIDATE

# Telegram FIRE-alert dedup: {opportunity_id: last_sent_unix_ts}
# Suppresses repeat FIRE Telegrams for the same canonical opportunity within
# the cooldown window. First FIRE goes out with full context; subsequent M5-close
# re-fires on the same setup stay silent (still logged to DB).
_PREDATOR_FIRE_TELEGRAM_LAST: dict[str, float] = {}
_PREDATOR_FIRE_TELEGRAM_COOLDOWN_S = 1800   # 30 min per opportunity_id

# Telegram ARMED-alert dedup: {armed_key: last_sent_unix_ts}
# Same rule as FIRE — only one ARMED Telegram per (archetype+dir+5pt bucket)
# within cooldown. Subsequent ARMED signals for the same bucket update the
# in-memory tracker silently. INVALIDATED sends are gated on this dict:
# if we suppressed the ARMED for a bucket, we also suppress its INVALIDATED,
# so the user only ever sees a paired alert → trigger (or alert → invalidated).
_PREDATOR_ARMED_TELEGRAM_LAST: dict[str, float] = {}
_PREDATOR_ARMED_TELEGRAM_COOLDOWN_S = 1800   # 30 min per armed_key


def _run_predator_iteration():
    import time as _time
    import hashlib as _hashlib
    from config import settings
    from database import SessionLocal
    from services.predator_engine import (
        evaluate, format_telegram_alert, format_telegram_invalidated,
        format_predator_execution_summary, _EXTENSION_LIMIT,
        _EXPECTED_TOTAL_MOVE_PTS,
    )
    from services.predator_position_sizer import (
        evaluate_volume_expansion, plan_deployment,
    )
    from services.predator_execution_manager import (
        create_batch, execute_batch_staged,
    )

    dedupe_s = int(getattr(settings, "predator_dedupe_cooldown_s", 3600))
    now = _time.time()
    tele_on = bool(getattr(settings, "predator_telegram_enabled", False))
    # HARDGUARD 2026-08-21 — Predator PRESS/expansion explicitly disabled at
    # code level regardless of PREDATOR_EXPANSION_ENABLED env flag. Global
    # portfolio governor (0.15 gross cap) makes 0.30 impossible in aggregate
    # anyway; this belt-and-braces removes even the theoretical path.
    expansion_allowed = False
    stage_delay = float(getattr(settings, "predator_stage_delay_s", 0.5))

    def _armed_key(sig):
        bucket = round(sig.entry / 5.0) * 5
        return f"{sig.archetype}|{sig.direction}|{bucket}"

    with SessionLocal() as db:
        # Fresh regime + reference-level context for Telegram formatting
        try:
            from services.regime_detector import classify_current_regime
            regime = classify_current_regime(db)
        except Exception:
            regime = None

        # Level lookups for ARMED message context
        try:
            from services.predator_engine import (
                _load_recent, _asian_range, _prev_day_hl,
            )
            _m5 = _load_recent(db, "M5", n=200)
            _a_h, _a_l = _asian_range(_m5) if _m5 else (None, None)
            _pdh, _pdl = _prev_day_hl(_m5) if _m5 else (None, None)
        except Exception:
            _a_l = _pdl = None
            _m5 = []

        signals = evaluate(db)
        seen_this_tick = set()

        for sig in signals:
            # dedupe by fingerprint (per M5 bar)
            last = _PREDATOR_SEEN.get(sig.fingerprint)
            if last and (now - last) < dedupe_s:
                continue
            _PREDATOR_SEEN[sig.fingerprint] = now

            armed_key = _armed_key(sig)
            seen_this_tick.add(armed_key)

            # Track ARMED entries for later INVALIDATED emission
            if sig.state == "ARMED":
                _PREDATOR_ARMED_TRACKING[armed_key] = {
                    "armed_at":  now,
                    "archetype": sig.archetype,
                    "direction": sig.direction,
                    "last_seen": now,
                }
                log.info("[predator] ARMED %s %s @ %.2f", sig.archetype, sig.direction, sig.entry)
            elif sig.state == "FIRE":
                # Promote — remove from ARMED tracking (setup succeeded)
                _PREDATOR_ARMED_TRACKING.pop(armed_key, None)
                log.info("[predator] FIRE %s %s @ %.2f RR=1:%.2f conf=%s",
                         sig.archetype, sig.direction, sig.entry, sig.rr, sig.confidence)

                # Record to shadow_trades ONLY for FIRE (ARMED has no plan)
                try:
                    from services.shadow_trade_simulator import record_shadow_trade
                    synthetic_verdict = {
                        "decision":   sig.direction,
                        "archetype":  sig.archetype,
                        "setup_score": 85 if sig.confidence == "HIGH" else 75 if sig.confidence == "MED" else 65,
                        "conditions_passed": 4,
                        "trade_plan": {
                            "entry": sig.entry, "stop_loss": sig.stop_loss,
                            "tp1": sig.tp1, "tp2": sig.tp2,
                            "tp1_rr": abs(sig.tp1 - sig.entry) / max(abs(sig.entry - sig.stop_loss), 0.1),
                            "tp2_rr": abs(sig.tp2 - sig.entry) / max(abs(sig.entry - sig.stop_loss), 0.1),
                            "invalidation": sig.stop_loss,
                            "risk_reward": sig.rr,
                        },
                    }
                    class _PredatorGrade:
                        grade = f"PRED_{sig.archetype}_{sig.confidence}"
                        reason = sig.thesis
                        composite_score = 85 if sig.confidence == "HIGH" else 75

                    record_shadow_trade(db, synthetic_verdict, grade_result=_PredatorGrade())
                except Exception as exc:
                    log.warning("[predator] shadow-record failed: %s", exc)

            # ── Sizing + execution planning (ONLY on FIRE) ──────────────
            deployment_plan = None
            batch = None
            exec_result = None
            if sig.state == "FIRE":
                try:
                    # Restart-safe dedupe — in-memory _PREDATOR_SEEN doesn't
                    # survive container restart, so double-check the DB before
                    # inserting to avoid UNIQUE-constraint failures.
                    from db_models import PredatorSignalBatch as _PSB
                    signal_id = f"{sig.fingerprint}"
                    existing = db.query(_PSB).filter(
                        _PSB.signal_id == signal_id
                    ).first()
                    if existing:
                        log.info("[predator] FIRE %s skipped — batch=%d already "
                                  "exists with signal_id=%s",
                                  sig.archetype, existing.id, signal_id)
                        continue

                    expansion = evaluate_volume_expansion(_m5, sig.archetype)
                    deployment_plan = plan_deployment(
                        archetype=sig.archetype,
                        direction=sig.direction,
                        entry=sig.entry,
                        stop_loss=sig.stop_loss,
                        tp1=sig.tp1,
                        tp2=sig.tp2,
                        expansion=expansion,
                        expansion_mode_allowed=expansion_allowed,
                    )
                    regime_dir = (regime or {}).get("direction")
                    regime_vol = (regime or {}).get("volatility")
                    # Freeze decision-journal context (observation only) — never
                    # reconstructed later. Fail-open on any lookup failure.
                    try:
                        from services.predator_observability import freeze_journal_context
                        _journal = freeze_journal_context(
                            db,
                            signal_direction=sig.direction,
                            signal_archetype=sig.archetype,
                            key_level=(_a_l if sig.archetype == "ASIAN_BREAKDOWN" else _pdl),
                            regime=regime,
                            m5=_m5,
                        )
                    except Exception as _jexc:
                        log.debug("[predator] journal freeze failed: %s", _jexc)
                        _journal = {}
                    batch = create_batch(
                        db,
                        signal_id=signal_id,
                        archetype=sig.archetype,
                        direction=sig.direction,
                        entry_price=sig.entry,
                        stop_loss=sig.stop_loss,
                        tp1=sig.tp1,
                        tp2=sig.tp2,
                        key_level=(_a_l if sig.archetype == "ASIAN_BREAKDOWN"
                                    else _pdl),
                        plan=deployment_plan,
                        regime_direction=regime_dir,
                        regime_volatility=regime_vol,
                        trend_context=_journal.get("trend_context"),
                        htf_disagreement=_journal.get("htf_disagreement"),
                        transition_state=_journal.get("transition_state"),
                        velocity_state=_journal.get("velocity_state"),
                        compression_state=_journal.get("compression_state"),
                        time_at_level_min=_journal.get("time_at_level_min"),
                        gc_context=_journal.get("gc_context"),
                        spread_at_fire=_journal.get("spread_at_fire"),
                    )

                    # Forward opportunity ledger (observation only)
                    try:
                        from services.predator_observability import (
                            record_forward_opportunity, current_predator_open_lots,
                        )
                        _open_lots = current_predator_open_lots(db)
                        _avail = max(0.0, 0.15 - _open_lots)
                        record_forward_opportunity(
                            db,
                            opportunity_id=batch.opportunity_id,
                            signal_id=signal_id,
                            archetype=sig.archetype,
                            direction=sig.direction,
                            model_decision="FIRE",
                            portfolio_decision="PENDING",
                            expected_entry=sig.entry,
                            sl=sig.stop_loss,
                            tp1=sig.tp1,
                            tp2=sig.tp2,
                            expected_tickets=len(deployment_plan.positions),
                            expected_lots=deployment_plan.max_exposure_lots,
                            actual_available_capacity=_avail,
                            actual_open_exposure=_open_lots,
                        )
                    except Exception as _fexc:
                        log.debug("[predator] forward ledger failed: %s", _fexc)
                except Exception as exc:
                    log.warning("[predator] sizing/plan failed: %s", exc)
                    try: db.rollback()
                    except Exception: pass

            # Telegram gated — signal integrity rules already enforced
            # in evaluate() (regime gate + extension filter). Pass level +
            # current price to formatter for ARMED context, plan for FIRE.
            if tele_on:
                # Per-state Telegram cooldown. Same canonical setup only pings
                # the user ONCE per 30 min — first ARMED goes out with full
                # context, first FIRE goes out with plan; subsequent M5-close
                # re-fires on the same setup stay silent (DB records still land).
                _skip_telegram = False
                if sig.state == "FIRE" and batch is not None and batch.opportunity_id:
                    _last = _PREDATOR_FIRE_TELEGRAM_LAST.get(batch.opportunity_id)
                    if _last and (now - _last) < _PREDATOR_FIRE_TELEGRAM_COOLDOWN_S:
                        _skip_telegram = True
                        log.info("[predator] FIRE Telegram suppressed — same "
                                  "opportunity_id=%s fired %.0fs ago",
                                  batch.opportunity_id, now - _last)
                elif sig.state == "ARMED":
                    _last = _PREDATOR_ARMED_TELEGRAM_LAST.get(armed_key)
                    if _last and (now - _last) < _PREDATOR_ARMED_TELEGRAM_COOLDOWN_S:
                        _skip_telegram = True
                        log.info("[predator] ARMED Telegram suppressed — same "
                                  "bucket=%s armed %.0fs ago",
                                  armed_key, now - _last)
                if not _skip_telegram:
                    try:
                        from services.strategist_runner import _send_plain
                        key_level = None
                        current_price = None
                        current_price_ts = None
                        if sig.archetype == "ASIAN_BREAKDOWN":
                            key_level = _a_l
                        elif sig.archetype == "PDL_BREAK":
                            key_level = _pdl
                        elif sig.archetype == "APPROACHING_LEVEL":
                            # Use whichever is closer for ARMED message
                            candidates = [x for x in (_a_l, _pdl) if x is not None]
                            if candidates:
                                key_level = max(candidates)   # nearest above current close (SELL setup)
                        if _m5:
                            current_price = _m5[-1][4]
                            current_price_ts = _m5[-1][0]
                        msg = format_telegram_alert(sig, regime=regime,
                                                        key_level=key_level,
                                                        current_price=current_price,
                                                        current_price_ts=current_price_ts,
                                                        deployment_plan=deployment_plan)
                        _send_plain(msg)
                        # Record send so cooldown starts.
                        if sig.state == "FIRE" and batch is not None and batch.opportunity_id:
                            _PREDATOR_FIRE_TELEGRAM_LAST[batch.opportunity_id] = now
                        elif sig.state == "ARMED":
                            _PREDATOR_ARMED_TELEGRAM_LAST[armed_key] = now
                    except Exception as exc:
                        log.warning("[predator] telegram send failed: %s", exc)

            # ── Staged execution (only if FIRE + batch planned + master flag on)
            if sig.state == "FIRE" and batch is not None:
                try:
                    # Per-ticket revalidation callback — enforces spec §7
                    # "Before EVERY additional 0.03 position is opened, recalculate".
                    max_pct = _EXTENSION_LIMIT.get(sig.archetype, 0.60)

                    def _revalidate(seq_no):
                        # Reload fresh M5 for real-time price + extension recheck
                        from services.predator_engine import _load_recent
                        fresh_m5 = _load_recent(db, "M5", n=200)
                        if not fresh_m5:
                            return False, "M5 unavailable for revalidation"
                        fresh_price = fresh_m5[-1][4]

                        # Direction-aware adverse-drift check. Only block if price
                        # moved AGAINST the trade — favorable follow-through means
                        # a valid setup getting better, not a reason to abort.
                        #   SELL: adverse if fresh_price > entry
                        #   BUY:  adverse if fresh_price < entry
                        if sig.direction == "SELL":
                            adverse = max(0.0, fresh_price - sig.entry)
                        else:
                            adverse = max(0.0, sig.entry - fresh_price)
                        if adverse > 5.0:
                            return False, (f"adverse drift {adverse:.1f}pt against "
                                            f"planned entry {sig.entry:.2f}")

                        # Extension recheck — how much of expected FAVORABLE move
                        # is already consumed? Too much = LATE/EXHAUSTED = abort.
                        expected_total = _EXPECTED_TOTAL_MOVE_PTS.get(sig.archetype, 40.0)
                        if sig.direction == "SELL":
                            consumed = max(0.0, (sig.entry - fresh_price) / expected_total)
                        else:
                            consumed = max(0.0, (fresh_price - sig.entry) / expected_total)
                        if consumed >= max_pct:
                            return False, (f"pct_consumed {consumed*100:.0f}% "
                                            f">= max {max_pct*100:.0f}%")
                        return True, "ok"

                    exec_result = execute_batch_staged(
                        db, batch,
                        revalidate_fn=_revalidate,
                        stage_delay_s=stage_delay,
                    )
                    # Option A: only send an Execution Summary Telegram when
                    # something actually opened. If the batch aborted at
                    # revalidation before any ticket landed, the FIRE alert
                    # already told the operator the plan — a second alert
                    # saying "0 opened" is pure noise.
                    _opened = int(exec_result.get("opened", 0) or 0)
                    if tele_on and _opened > 0:
                        try:
                            from services.strategist_runner import _send_plain
                            # Refresh batch state (execute_batch_staged commits)
                            db.refresh(batch)
                            summary = format_predator_execution_summary(
                                batch, exec_result.get("tickets") or [],
                                skipped_reason=(exec_result.get("reason")
                                                if batch.execution_status
                                                    == "SHADOW_ONLY" else None),
                            )
                            _send_plain(summary)
                        except Exception as exc:
                            log.warning("[predator] execution-summary send failed: %s", exc)
                    elif tele_on and _opened == 0:
                        log.info("[predator] execution-summary suppressed — "
                                  "batch=%d opened=0 (Option A noise mute)",
                                  batch.id if batch else -1)
                except Exception as exc:
                    log.error("[predator] staged execution raised: %s", exc)

        # ── Emit INVALIDATED for tracked ARMED entries that went stale ──
        stale_keys = []
        for key, info in _PREDATOR_ARMED_TRACKING.items():
            if key in seen_this_tick:
                info["last_seen"] = now
                continue
            age = now - info["last_seen"]
            if age >= _PREDATOR_ARMED_STALE_S:
                stale_keys.append(key)

        for key in stale_keys:
            info = _PREDATOR_ARMED_TRACKING.pop(key)
            reason = ("Trigger window elapsed without M5 close breaching level; "
                      "price moved away or regime shifted")
            log.info("[predator] INVALIDATED %s %s (armed %ds ago)",
                     info["archetype"], info["direction"],
                     int(now - info["armed_at"]))
            # Pair 1:1 with ARMED Telegram — only send INVALIDATED if we
            # actually sent an ARMED Telegram for this bucket. If the ARMED
            # was suppressed by cooldown, the user was never told, so the
            # INVALIDATED would be orphan noise.
            _armed_was_sent_at = _PREDATOR_ARMED_TELEGRAM_LAST.pop(key, None)
            if tele_on and _armed_was_sent_at is not None:
                try:
                    from services.strategist_runner import _send_plain
                    _send_plain(format_telegram_invalidated(
                        info["archetype"], info["direction"], reason,
                    ))
                except Exception as exc:
                    log.warning("[predator] invalidated-send failed: %s", exc)


# ── Phase 11: market-intelligence alert loop ──────────────────────────────
#
# Every 60s, run the full Phase 2-10 pipeline and let the intel engine
# decide whether any new alert candidates fire. Governed by two flags:
#   xauusd_market_intelligence_telegram_enabled — master switch
#   xauusd_market_intel_shadow_mode              — persist-only (no send)
# When flag is False, this loop still runs but all candidates are
# suppressed with reason "flag off" — so nothing sends, nothing is stored.

_MARKET_INTEL_INTERVAL_S = 60


async def _market_intel_loop():
    """Phase 11 detection + delivery loop. Fires every 60s."""
    from config import settings
    log.info("[scheduler] market-intel loop started (every %ds)",
             _MARKET_INTEL_INTERVAL_S)
    # Initial delay so freshness + candle ingestion have a chance to boot
    await asyncio.sleep(30)
    while True:
        try:
            # Only run pipeline if flag is on — else save the compute
            if getattr(settings, "xauusd_market_intelligence_telegram_enabled", False):
                await asyncio.to_thread(_run_market_intel_iteration)
        except asyncio.CancelledError:
            log.info("[scheduler] market-intel loop cancelled")
            break
        except Exception as exc:
            log.warning("[scheduler] market-intel loop error: %s", exc)
        await asyncio.sleep(_MARKET_INTEL_INTERVAL_S)


def _run_market_intel_iteration():
    """Full Phase 2-10 pipeline → Phase 11 detect/dedupe/persist."""
    from database import SessionLocal
    from services.canonical_market_data import get_canonical
    from services.htf_weighted_alignment import compute_htf_alignment
    from services.market_regime import classify_regime
    from services.directional_evidence import compute_directional_evidence
    from services.breakout_acceptance import scan_key_levels
    from services.opportunity_state import evaluate_and_transition
    from services.separated_verdicts import compute_separated_verdict
    from services.key_level_ranking import rank_key_levels
    from services.macro_interpretation import compute_macro_context
    from services.market_intelligence_alerts import fire_intel_alerts
    from config import settings

    cmd = get_canonical(cache_ttl_s=settings.xauusd_canonical_data_cache_ttl_s)
    with SessionLocal() as db:
        snap = cmd.snapshot(db)
        try:
            from services.calendar_provider import get_upcoming_events
            events = get_upcoming_events(hours=2) or []
        except Exception:
            events = []

        htf = compute_htf_alignment(snap)
        regime = classify_regime(snap, upcoming_events=events)
        evidence = compute_directional_evidence(
            snap, htf_alignment=htf, regime=regime, upcoming_events=events,
        )
        breakouts = scan_key_levels(snap, htf_alignment=htf)
        # PERSIST state transitions here so we accumulate history
        state_tr = evaluate_and_transition(
            db, snapshot=snap, regime=regime, htf_alignment=htf,
            evidence=evidence, breakouts=breakouts, persist=True,
        )
        verdict = compute_separated_verdict(
            snapshot=snap, htf_alignment=htf, regime=regime, evidence=evidence,
            breakouts=breakouts, state_transition=state_tr,
        )
        ranking = rank_key_levels(snap, breakouts=breakouts)

        # Macro — best-effort
        macro = None
        try:
            correlation_snapshot = yields_context = dxy_bars = None
            try:
                from services.correlation_engine import compute_intermarket_correlations
                correlation_snapshot = compute_intermarket_correlations(
                    timeframe="H1", n_bars=100)
            except Exception: pass
            try:
                from services.fred_provider import get_yields_context
                yields_context = get_yields_context()
            except Exception: pass
            try:
                from services.tradingview_provider import get_tv_candles
                dxy_bars = get_tv_candles("dxy", timeframe="H1", limit=40)
            except Exception: pass
            macro = compute_macro_context(
                snapshot=snap, tech_direction=htf.direction,
                upcoming_events=events, dxy_bars=dxy_bars,
                correlation_snapshot=correlation_snapshot,
                yields_context=yields_context,
            )
        except Exception as exc:
            log.debug("[market-intel] macro assembly skipped: %s", exc)

        outcomes = fire_intel_alerts(
            db, prev_state=state_tr.prev_state, new_state=state_tr.new_state,
            trigger_condition=state_tr.trigger_condition,
            trigger_price=state_tr.price,
            snapshot=snap, verdict=verdict, evidence=evidence, ranking=ranking,
            macro=macro, state_transition=state_tr, breakouts=breakouts,
        )
        # Log only if something happened
        interesting = [o for o in outcomes if o.result in ("sent", "shadow")]
        if interesting:
            log.info("[market-intel] fired: %s",
                      [(o.alert_type, o.result) for o in interesting])


def _run_auto_executor_iteration():
    """Single auto-executor iteration (runs in thread)."""
    global _last_auto_attempt
    from database import SessionLocal
    from services.auto_executor import evaluate_and_execute
    with SessionLocal() as db:
        att = evaluate_and_execute(db)
        _last_auto_attempt = att.to_dict()
        if att.fired:
            log.info("[auto_exec] order placed ticket=%s lot=%.2f signal=%s",
                     att.ticket, att.lot_size, att.signal)
        elif att.blocking_reason:
            log.debug("[auto_exec] blocked at %s: %s",
                      att.blocking_layer, att.blocking_reason)


async def _prediction_alert_loop():
    """Periodically check the high-probability predictor and alert on STRONG."""
    log.info("[scheduler] prediction alert loop started (every %ds)",
             PREDICTION_INTERVAL_SEC)
    while True:
        try:
            await asyncio.sleep(PREDICTION_INTERVAL_SEC)
            await asyncio.to_thread(_run_prediction_iteration)
        except asyncio.CancelledError:
            log.info("[scheduler] prediction loop cancelled")
            raise
        except Exception as exc:
            log.warning("[scheduler] prediction loop error: %s", exc)


def _run_prediction_iteration():
    from database import SessionLocal
    from services.high_probability_predictor import predict_xauusd, prediction_to_dict
    with SessionLocal() as db:
        pred = predict_xauusd(db=db)
        pred_dict = prediction_to_dict(pred)
        status = _maybe_alert_strong_prediction(pred_dict)
        log.info(
            "[scheduler] prediction iteration: %s prob=%s band=%s aligned=%s/%s → telegram=%s",
            pred_dict.get("direction"), pred_dict.get("probability"),
            pred_dict.get("band"), pred_dict.get("alignedCount"),
            pred_dict.get("totalLayers"), status,
        )


async def _drawdown_loop():
    """Periodically check drawdown for both engines."""
    log.info("[scheduler] drawdown loop started (every %ds)", DRAWDOWN_INTERVAL_SEC)
    while True:
        try:
            await asyncio.sleep(DRAWDOWN_INTERVAL_SEC)
            await asyncio.to_thread(_run_drawdown_iteration)
        except asyncio.CancelledError:
            log.info("[scheduler] drawdown loop cancelled")
            raise
        except Exception as exc:
            log.warning("[scheduler] drawdown loop error: %s", exc)


def _run_drawdown_iteration():
    from database import SessionLocal
    from services.equity_tracker import check_drawdown_alert
    with SessionLocal() as db:
        for engine in ("swing", "trend_pullback"):
            r = check_drawdown_alert(db, engine_id=engine)
            if r.get("alerted"):
                log.info("[scheduler] drawdown alert sent for %s (%.2f%%)",
                         engine, r.get("drawdownPct"))


# ── Daily summary loop ────────────────────────────────────────────────────────

_last_daily_summary: Optional[str] = None


async def _daily_summary_loop():
    """Once per day at 07:00 UTC, send a recap to Telegram."""
    log.info("[scheduler] daily summary loop started (fires at %02d:00 UTC)",
             DAILY_SUMMARY_HOUR_UTC)
    while True:
        try:
            # Check every 5 minutes whether it's time to fire
            await asyncio.sleep(300)
            now = datetime.now(timezone.utc)
            day_key = now.date().isoformat()
            global _last_daily_summary
            if now.hour == DAILY_SUMMARY_HOUR_UTC and day_key != _last_daily_summary:
                await asyncio.to_thread(_run_daily_summary)
                _last_daily_summary = day_key
        except asyncio.CancelledError:
            log.info("[scheduler] daily summary loop cancelled")
            raise
        except Exception as exc:
            log.warning("[scheduler] daily summary loop error: %s", exc)


def _run_daily_summary():
    """Compose + send the daily summary Telegram message."""
    from database import SessionLocal
    from services.paper_observation_tracker import get_observation_stats
    from services.equity_tracker import compute_equity_curve
    try:
        from services.telegram_alert_service import send_telegram_message, telegram_alerts_enabled
        if not telegram_alerts_enabled():
            log.info("[scheduler] daily summary: telegram disabled, skipping")
            return
        with SessionLocal() as db:
            sw_stats = get_observation_stats(db, engine_id="swing")
            tp_stats = get_observation_stats(db, engine_id="trend_pullback")
            sw_eq    = compute_equity_curve(db, engine_id="swing")
            tp_eq    = compute_equity_curve(db, engine_id="trend_pullback")

        msg = (
            f"📊 <b>Daily XAU/USD Paper Tracking Recap</b>\n"
            f"<i>{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}</i>\n"
            f"\n"
            f"<b>Swing Engine (ICT H1)</b>\n"
            f"  Observed: {sw_stats['total']}   Resolved: {sw_stats['resolved']}\n"
            f"  Win rate: {sw_stats['winRate']}%  Exp R: {sw_stats['expectancyR']:+.2f}\n"
            f"  Equity: ${sw_eq['finalEquity']:,.2f}  ({sw_eq['netReturnPct']:+.2f}%)\n"
            f"  Max DD: {sw_eq['maxDrawdownPct']}%  Current DD: {sw_eq['currentDrawdownPct']}%\n"
            f"\n"
            f"<b>Trend Pullback Engine (H1)</b>\n"
            f"  Observed: {tp_stats['total']}   Resolved: {tp_stats['resolved']}\n"
            f"  Win rate: {tp_stats['winRate']}%  Exp R: {tp_stats['expectancyR']:+.2f}\n"
            f"  Equity: ${tp_eq['finalEquity']:,.2f}  ({tp_eq['netReturnPct']:+.2f}%)\n"
            f"  Max DD: {tp_eq['maxDrawdownPct']}%  Current DD: {tp_eq['currentDrawdownPct']}%\n"
            f"\n"
            f"<i>Decision support only. No automatic execution.</i>"
        )
        send_telegram_message(msg)
        log.info("[scheduler] daily summary sent")
    except Exception as exc:
        log.warning("[scheduler] daily summary build failed: %s", exc)


# ═══════════════════════════════════════════════════════════════════════════════
# Public: start / stop
# ═══════════════════════════════════════════════════════════════════════════════

_tasks: list[asyncio.Task] = []


async def start_background_loops():
    """Spawn all background loops. Idempotent (safe to call multiple times)."""
    global _tasks
    if _tasks:
        log.info("[scheduler] loops already running")
        return
    # (Loop bodies defined above; add here if not yet imported)

    from config import settings
    use_mandate = getattr(settings, "use_mandate_strategist", True)

    _tasks = [
        asyncio.create_task(_scanner_loop(),               name="scanner_loop"),
        asyncio.create_task(_prediction_alert_loop(),      name="prediction_alert_loop"),
        asyncio.create_task(_drawdown_loop(),              name="drawdown_loop"),
        asyncio.create_task(_daily_summary_loop(),         name="daily_summary_loop"),
        asyncio.create_task(_daily_briefing_loop(),        name="daily_briefing_loop"),
        asyncio.create_task(_weekly_digest_loop(),         name="weekly_digest_loop"),
        asyncio.create_task(_weekend_newsletter_loop(),    name="weekend_newsletter_loop"),
        asyncio.create_task(_candle_ingestion_fast_loop(), name="candle_ingestion_fast_loop"),
        asyncio.create_task(_candle_ingestion_slow_loop(), name="candle_ingestion_slow_loop"),
        asyncio.create_task(_data_freshness_loop(),        name="data_freshness_loop"),
        asyncio.create_task(_vp_trap_measurement_loop(),   name="vp_trap_measurement_loop"),
        asyncio.create_task(_market_intel_loop(),          name="market_intel_loop"),
        asyncio.create_task(_shadow_trade_advance_loop(),  name="shadow_trade_advance_loop"),
        asyncio.create_task(_predator_loop(),              name="predator_loop"),
        asyncio.create_task(_strategist_ledger_resolver_loop(), name="strategist_ledger_resolver_loop"),
    ]

    # Pick exactly ONE execution authority — never both, or they'll fight.
    if use_mandate:
        _tasks.append(asyncio.create_task(_strategist_loop(), name="strategist_loop"))
        log.info("[scheduler] using MANDATE strategist (authoritative)")
    else:
        _tasks.append(asyncio.create_task(_auto_executor_loop(), name="auto_executor_loop"))
        log.info("[scheduler] using LEGACY auto-executor (mandate disabled)")

    log.info("[scheduler] %d background loops started", len(_tasks))


async def stop_background_loops():
    """Cancel all running background loops cleanly."""
    global _tasks
    if not _tasks:
        return
    for t in _tasks:
        t.cancel()
    await asyncio.gather(*_tasks, return_exceptions=True)
    _tasks = []
    log.info("[scheduler] all background loops stopped")
