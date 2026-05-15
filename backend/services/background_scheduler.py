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
    _tasks = [
        asyncio.create_task(_scanner_loop(),           name="scanner_loop"),
        asyncio.create_task(_prediction_alert_loop(),  name="prediction_alert_loop"),
        asyncio.create_task(_drawdown_loop(),          name="drawdown_loop"),
        asyncio.create_task(_daily_summary_loop(),     name="daily_summary_loop"),
    ]
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
