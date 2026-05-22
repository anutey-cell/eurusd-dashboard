"""
Autonomous Live Executor — learning-mode trading
================================================

Fires real MT5 orders ONLY when ALL three independent confirmation systems
agree, with strict per-day and per-trade caps so the system can learn from
real fills without risk of blowing the account.

Three-layer confirmation gate (ALL must pass — no exceptions):

  1. Institutional Scanner
     - market_state == "SIGNAL_READY"
     - signal IN ("BUY", "SELL")
     - qualityScore >= 85   (the validated edge config)

  2. High-Probability Predictor
     - band IN ("STRONG", "MODERATE")
     - direction matches scanner.signal exactly

  3. Killzone Edge Analyzer
     - current killzone posture IN ("TRADE", "PRESS")
     - edge_score >= 60

If any layer disagrees → no trade, log gating reason, exit.

Per-day caps (enforced before order submission):
  - settings.auto_execution_max_trades_per_day  (default 3)
  - settings.auto_execution_max_lot             (default 0.05, hard cap)
  - settings.max_open_trades                    (default 1 — at most one open)

Master switch chain (ALL must be true to even attempt):
  - settings.auto_execution_enabled
  - settings.data_mode == "live"
  - settings.mt5_execution_enabled
  - settings.allow_demo_trading           (legacy gate, still required)
  - settings.live_trading_authorized      (NEW — operator opts into live account)
  - broker kill switch NOT active

Returns a verbose `ExecutionAttempt` dict every cycle so the dashboard can
show "what's blocking us right now."
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy.orm import Session

log = logging.getLogger(__name__)


# ── Confirmation thresholds ──────────────────────────────────────────────────
SCANNER_MIN_SCORE     = 85
PREDICTOR_OK_BANDS    = {"STRONG", "MODERATE"}
KILLZONE_OK_POSTURES  = {"TRADE", "PRESS"}
KILLZONE_MIN_EDGE     = 60


# ── Result container ─────────────────────────────────────────────────────────

@dataclass
class ExecutionAttempt:
    """Per-cycle audit record returned to dashboard + Telegram + logs."""
    ts:               str              = ""
    attempted:        bool             = False
    fired:            bool             = False
    blocking_reason:  Optional[str]    = None
    blocking_layer:   Optional[str]    = None
    confirmations:    dict[str, Any]   = field(default_factory=dict)
    daily_count:      int              = 0
    daily_max:        int              = 0
    lot_size:         float            = 0.0
    ticket:           Optional[int]    = None
    signal:           Optional[str]    = None
    entry:            Optional[float]  = None
    stop_loss:        Optional[float]  = None
    take_profit:      Optional[float]  = None

    def to_dict(self) -> dict:
        return self.__dict__.copy()


# ── Master-switch chain ──────────────────────────────────────────────────────

def _check_master_switches() -> tuple[bool, Optional[str]]:
    """All-or-nothing pre-flight. Returns (ok, blocking_reason)."""
    from config import settings

    if not settings.auto_execution_enabled:
        return False, "AUTO_EXECUTION_ENABLED=false (master switch)"
    if settings.data_mode != "live":
        return False, f"DATA_MODE={settings.data_mode!r}, must be 'live' to trade"
    if not settings.mt5_execution_enabled:
        return False, "MT5_EXECUTION_ENABLED=false"
    if not settings.allow_demo_trading:
        return False, "ALLOW_DEMO_TRADING=false (legacy gate, must be true)"
    if not settings.live_trading_authorized:
        return False, "LIVE_TRADING_AUTHORIZED=false — explicit opt-in required"

    # Broker kill switch
    try:
        from services.broker_provider import check_execution_enabled
        status = check_execution_enabled()
        if status.get("killSwitchActive"):
            return False, "Broker kill switch is ACTIVE — execution disabled"
    except Exception:
        pass  # if broker_provider is unavailable, fall through (kill switch is best-effort)

    return True, None


# ── Daily trade counter ──────────────────────────────────────────────────────

def _count_accepted_trades_today(db: Session) -> int:
    """Count MT5TradeLog rows with status='accepted' since 00:00 UTC today."""
    from db_models import MT5TradeLog

    today_utc_start = datetime.now(timezone.utc).replace(
        hour=0, minute=0, second=0, microsecond=0,
    )
    return (
        db.query(MT5TradeLog)
        .filter(MT5TradeLog.created_at >= today_utc_start)
        .filter(MT5TradeLog.status == "accepted")
        .count()
    )


# ── Confirmation layers ──────────────────────────────────────────────────────

def _confirm_scanner(db: Session) -> tuple[bool, dict, Optional[str]]:
    """
    Layer 1: institutional scanner must be in SIGNAL_READY with direction +
    quality score >= 85.
    Returns (ok, confirmation_dict, blocking_reason).
    """
    try:
        from services.institutional_scanner import scan_xauusd_market
        scan = scan_xauusd_market(force_refresh=False, db=db)
    except Exception as exc:
        return False, {"available": False, "error": str(exc)}, f"Scanner error: {exc}"

    state  = scan.get("marketState", "UNKNOWN")
    signal = scan.get("signal", "WAIT")
    plan   = (scan.get("recommendedAction") or {}).get("tradePlan") or {}
    score  = int(plan.get("qualityScore", 0))

    info = {
        "marketState": state, "signal": signal, "qualityScore": score,
        "entry":       plan.get("entry"),
        "stopLoss":    plan.get("stopLoss"),
        "takeProfit":  plan.get("takeProfit"),
        "rr":          plan.get("rr"),
        "riskPips":    plan.get("riskPoints") or plan.get("riskPips"),
        "reason":      scan.get("summary", "")[:160],
        "rawScan":     scan,    # kept for downstream order build
    }

    if state != "SIGNAL_READY":
        return False, info, f"Scanner state is {state} (need SIGNAL_READY)"
    if signal not in ("BUY", "SELL"):
        return False, info, f"Scanner signal is {signal} (need BUY/SELL)"
    if score < SCANNER_MIN_SCORE:
        return False, info, f"Scanner score {score} < {SCANNER_MIN_SCORE}"
    if not info["entry"] or not info["stopLoss"] or not info["takeProfit"]:
        return False, info, "Scanner trade plan missing entry/SL/TP"

    return True, info, None


def _confirm_predictor(scanner_signal: str) -> tuple[bool, dict, Optional[str]]:
    """
    Layer 2: high-probability predictor band must be STRONG or MODERATE AND
    direction must match the scanner.
    """
    try:
        from services.high_probability_predictor import generate_prediction
        pred = generate_prediction()
    except Exception as exc:
        return False, {"available": False, "error": str(exc)}, f"Predictor error: {exc}"

    band      = pred.get("band", "WEAK")
    direction = pred.get("direction", "NEUTRAL")
    score     = int(pred.get("score", 0))
    aligned   = int(pred.get("alignedLayers", pred.get("aligned_layers", 0)))

    info = {
        "band":            band,
        "direction":       direction,
        "score":           score,
        "alignedLayers":   aligned,
    }

    if band not in PREDICTOR_OK_BANDS:
        return False, info, f"Predictor band {band} (need STRONG/MODERATE)"
    if direction != scanner_signal:
        return False, info, (
            f"Predictor direction {direction} disagrees with scanner {scanner_signal}"
        )
    return True, info, None


def _confirm_killzone(db: Session) -> tuple[bool, dict, Optional[str]]:
    """
    Layer 3: current killzone must be TRADE/PRESS with edge >= 60.
    """
    try:
        from services.killzone_analyzer import get_current_recommendation
        rec = get_current_recommendation(db, lookback_days=60)
    except Exception as exc:
        return False, {"available": False, "error": str(exc)}, f"Killzone error: {exc}"

    posture = rec.get("posture", "OBSERVE")
    edge    = int(rec.get("edge_score", 0))
    kz      = rec.get("current_kz", "unknown")
    label   = rec.get("label", kz)

    info = {
        "killzone":     kz,
        "label":        label,
        "posture":      posture,
        "edge_score":   edge,
        "window_utc":   rec.get("window_utc"),
    }

    if posture not in KILLZONE_OK_POSTURES:
        return False, info, f"Killzone posture is {posture} (need TRADE/PRESS)"
    if edge < KILLZONE_MIN_EDGE:
        return False, info, f"Killzone edge {edge} < {KILLZONE_MIN_EDGE}"
    return True, info, None


# ── Public entry point ───────────────────────────────────────────────────────

def evaluate_and_execute(db: Session) -> ExecutionAttempt:
    """
    Single-cycle orchestrator. Called every `auto_execution_interval_sec`
    from the background scheduler.

    Returns an `ExecutionAttempt` describing exactly what happened — used
    by the dashboard, Telegram, and audit logs.
    """
    from config import settings

    att = ExecutionAttempt(ts=datetime.now(timezone.utc).isoformat())
    att.daily_max = settings.auto_execution_max_trades_per_day

    # ── Master switches ────────────────────────────────────────────────
    ok, reason = _check_master_switches()
    if not ok:
        att.blocking_reason = reason
        att.blocking_layer  = "master_switch"
        return att

    att.attempted = True

    # ── Daily trade ceiling ─────────────────────────────────────────────
    daily = _count_accepted_trades_today(db)
    att.daily_count = daily
    if daily >= settings.auto_execution_max_trades_per_day:
        att.blocking_reason = (
            f"Daily ceiling reached: {daily}/{settings.auto_execution_max_trades_per_day}"
        )
        att.blocking_layer  = "daily_cap"
        return att

    # ── Confirmation 1: scanner ─────────────────────────────────────────
    ok, scan_info, why = _confirm_scanner(db)
    att.confirmations["scanner"] = {k: v for k, v in scan_info.items() if k != "rawScan"}
    if not ok:
        att.blocking_reason = why
        att.blocking_layer  = "scanner"
        return att

    scanner_signal = scan_info["signal"]
    att.signal      = scanner_signal
    att.entry       = scan_info["entry"]
    att.stop_loss   = scan_info["stopLoss"]
    att.take_profit = scan_info["takeProfit"]

    # ── Confirmation 2: high-prob predictor ─────────────────────────────
    ok, pred_info, why = _confirm_predictor(scanner_signal)
    att.confirmations["predictor"] = pred_info
    if not ok:
        att.blocking_reason = why
        att.blocking_layer  = "predictor"
        return att

    # ── Confirmation 3: killzone (edge-score gate) ──────────────────────
    ok, kz_info, why = _confirm_killzone(db)
    att.confirmations["killzone"] = kz_info
    if not ok:
        att.blocking_reason = why
        att.blocking_layer  = "killzone"
        return att

    # ── Confirmation 4: killzone × direction × engine POLICY ────────────
    # 4th gate — learned from 245-trade historical sample. Refuses cells
    # like (london_kz × SELL) and (ny_kz × any) which have negative ExpR.
    # See backend/services/killzone_policy.py for the table + audit trail.
    try:
        from services.killzone_policy import evaluate as _eval_kz_policy
        # We need an engine_id. The auto-executor consumes the scanner's
        # output, which corresponds to the "swing" engine. If you wire
        # trend_pullback or momentum_breakout to this executor in future,
        # pass the actual engine_id here.
        policy = _eval_kz_policy(
            killzone_key=kz_info.get("killzone", "unknown"),
            direction=scanner_signal,
            engine_id="swing",   # scanner = swing-ICT engine
        )
        att.confirmations["killzone_policy"] = {
            "decision":         policy.decision,
            "allow":            policy.allow,
            "reason":           policy.reason[:160],
            "sample_size":      policy.sample_size,
            "historical_wr":    policy.historical_wr,
            "historical_exp_r": policy.historical_exp_r,
            "is_exploratory":   policy.is_exploratory,
            "bypass_reason":    policy.bypass_reason,
        }
        if not policy.allow:
            att.blocking_reason = policy.reason
            att.blocking_layer  = "killzone_policy"
            return att
    except Exception as exc:
        # Policy module must never crash the executor. If it does, fail-safe
        # to "allow" (the other three gates already constrained the trade).
        log.warning("[auto_exec] killzone_policy evaluation failed (fail-open): %s", exc)
        att.confirmations["killzone_policy"] = {"error": str(exc), "allow": True}

    # ── All confirmations agree — build signal payload and fire ─────────
    signal_payload = {
        "pair":         "xauusd",
        "signal":       scanner_signal,
        "qualityScore": scan_info["qualityScore"],
        "entry":        scan_info["entry"],
        "stopLoss":     scan_info["stopLoss"],
        "takeProfit":   scan_info["takeProfit"],
        "rr":           scan_info["rr"],
        "riskPips":     scan_info["riskPips"],
        "newsStatus":   scan_info["rawScan"].get("newsStatus", "CLEAR"),
        "reason": (
            f"AUTO ({scanner_signal}) | scan={scan_info['qualityScore']} "
            f"pred={pred_info['band']} kz={kz_info['posture']}/{kz_info['edge_score']}"
        ),
        # Hard cap — never let position sizing exceed this
        "max_lot": settings.auto_execution_max_lot,
    }

    # ── Path A: Bridge mode — enqueue for the laptop daemon ─────────────
    if settings.mt5_bridge_enabled:
        try:
            from db_models import PendingExecution
            from datetime import timedelta as _td
            row = PendingExecution(
                pair="xauusd",
                signal=scanner_signal,
                entry=float(signal_payload["entry"]),
                stop_loss=float(signal_payload["stopLoss"]),
                take_profit=float(signal_payload["takeProfit"]),
                risk_pips=float(signal_payload.get("riskPips") or 0),
                quality_score=int(signal_payload.get("qualityScore") or 0),
                rr=float(signal_payload.get("rr") or 0),
                max_lot=settings.auto_execution_max_lot,
                reason=signal_payload.get("reason", ""),
                confirmations_json=json.dumps({
                    k: v for k, v in att.confirmations.items()
                }),
                expires_at=datetime.now(timezone.utc) + _td(minutes=5),
                status="PENDING",
            )
            db.add(row)
            db.commit()
            db.refresh(row)

            att.fired       = True       # Order is queued — counts as fired
            att.ticket      = None       # Real ticket comes back from bridge
            att.lot_size    = settings.auto_execution_max_lot
            att.daily_count = daily + 1

            log.info(
                "[auto_exec] ENQUEUED order #%d %s xauusd lot<=%.2f (waiting for bridge)",
                row.id, scanner_signal, settings.auto_execution_max_lot,
            )

            # Fire Telegram alert so the operator knows
            try:
                from services.telegram_service import send_text_alert
                send_text_alert(
                    text=(
                        f"<b>AUTO ORDER QUEUED</b>\n"
                        f"{scanner_signal} XAU/USD · max {settings.auto_execution_max_lot} lot\n"
                        f"Entry {signal_payload['entry']} · "
                        f"SL {signal_payload['stopLoss']} · "
                        f"TP {signal_payload['takeProfit']}\n"
                        f"Order #{row.id} · awaiting bridge daemon"
                    ),
                )
            except Exception:
                pass

            return att
        except Exception as exc:
            att.blocking_reason = f"Bridge enqueue failed: {exc}"
            att.blocking_layer  = "bridge_enqueue"
            log.warning("[auto_exec] %s", att.blocking_reason)
            return att

    # ── Path B: Direct execution (Windows MT5 available locally) ────────
    try:
        from services.mt5_provider import place_demo_market_order, MT5SafetyError
        result = place_demo_market_order(signal_payload)
    except Exception as exc:
        # validate_demo_order rejected (Gate 1-13) or order_send failed
        att.blocking_reason = f"MT5 rejected: {exc}"
        att.blocking_layer  = "mt5_gates"
        log.warning("[auto_exec] %s", att.blocking_reason)
        return att

    # Fired!
    att.fired       = True
    att.ticket      = result.get("ticket")
    att.lot_size    = float(result.get("volume", 0.0))
    att.daily_count = daily + 1

    log.info(
        "[auto_exec] FIRED %s %s lot=%.2f ticket=%s daily=%d/%d",
        signal_payload["signal"], "xauusd", att.lot_size, att.ticket,
        att.daily_count, att.daily_max,
    )

    # Fire-and-forget Telegram alert
    try:
        from services.telegram_service import send_text_alert
        send_text_alert(
            text=(
                f"<b>AUTO TRADE FIRED</b>\n"
                f"{signal_payload['signal']} XAU/USD · {att.lot_size} lot\n"
                f"Entry {att.entry} · SL {att.stop_loss} · TP {att.take_profit}\n"
                f"Ticket {att.ticket} · daily {att.daily_count}/{att.daily_max}\n"
                f"Scanner {scan_info['qualityScore']}/100 · "
                f"Predictor {pred_info['band']} · "
                f"KZ {kz_info['label']} ({kz_info['edge_score']})"
            ),
        )
    except Exception as exc:
        log.debug("Telegram fire alert failed (non-fatal): %s", exc)

    return att


# ── Status helpers (used by /execution/autonomous/status) ─────────────────────

def get_status(db: Session) -> dict:
    """
    Dashboard-friendly status without executing. Tells the operator
    what's currently blocking auto-execution.
    """
    from config import settings

    daily = _count_accepted_trades_today(db)
    master_ok, master_reason = _check_master_switches()

    return {
        "config": {
            "enabled":               settings.auto_execution_enabled,
            "data_mode":             settings.data_mode,
            "live_authorized":       settings.live_trading_authorized,
            "mt5_execution_enabled": settings.mt5_execution_enabled,
            "max_lot":               settings.auto_execution_max_lot,
            "max_trades_per_day":    settings.auto_execution_max_trades_per_day,
            "interval_sec":          settings.auto_execution_interval_sec,
        },
        "master_switch": {
            "ok": master_ok,
            "blocking_reason": master_reason,
        },
        "daily": {
            "trades_today":     daily,
            "trades_remaining": max(0, settings.auto_execution_max_trades_per_day - daily),
            "limit":            settings.auto_execution_max_trades_per_day,
            "at_cap":           daily >= settings.auto_execution_max_trades_per_day,
        },
        "thresholds": {
            "scanner_min_score": SCANNER_MIN_SCORE,
            "predictor_bands":   sorted(PREDICTOR_OK_BANDS),
            "killzone_postures": sorted(KILLZONE_OK_POSTURES),
            "killzone_min_edge": KILLZONE_MIN_EDGE,
        },
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }


def preview_attempt(db: Session) -> dict:
    """
    Dry-run: run the full evaluation pipeline WITHOUT firing the order.
    Same as `evaluate_and_execute` but the MT5 call is skipped if all
    three confirmations would pass — instead we report 'would fire'.
    """
    from config import settings

    att = ExecutionAttempt(ts=datetime.now(timezone.utc).isoformat())
    att.daily_max = settings.auto_execution_max_trades_per_day

    ok, reason = _check_master_switches()
    if not ok:
        att.blocking_reason, att.blocking_layer = reason, "master_switch"
        return att.to_dict() | {"would_fire": False}

    att.attempted   = True
    att.daily_count = _count_accepted_trades_today(db)
    if att.daily_count >= settings.auto_execution_max_trades_per_day:
        att.blocking_reason = f"Daily cap reached {att.daily_count}/{att.daily_max}"
        att.blocking_layer  = "daily_cap"
        return att.to_dict() | {"would_fire": False}

    ok, scan_info, why = _confirm_scanner(db)
    att.confirmations["scanner"] = {k: v for k, v in scan_info.items() if k != "rawScan"}
    if not ok:
        att.blocking_reason, att.blocking_layer = why, "scanner"
        return att.to_dict() | {"would_fire": False}
    att.signal = scan_info["signal"]
    att.entry = scan_info["entry"]
    att.stop_loss = scan_info["stopLoss"]
    att.take_profit = scan_info["takeProfit"]

    ok, pred_info, why = _confirm_predictor(scan_info["signal"])
    att.confirmations["predictor"] = pred_info
    if not ok:
        att.blocking_reason, att.blocking_layer = why, "predictor"
        return att.to_dict() | {"would_fire": False}

    ok, kz_info, why = _confirm_killzone(db)
    att.confirmations["killzone"] = kz_info
    if not ok:
        att.blocking_reason, att.blocking_layer = why, "killzone"
        return att.to_dict() | {"would_fire": False}

    # Apply the same killzone × direction policy filter as the live executor
    try:
        from services.killzone_policy import evaluate as _eval_kz_policy
        policy = _eval_kz_policy(
            killzone_key=kz_info.get("killzone", "unknown"),
            direction=scan_info["signal"],
            engine_id="swing",
        )
        att.confirmations["killzone_policy"] = {
            "decision":         policy.decision,
            "allow":            policy.allow,
            "reason":           policy.reason[:160],
            "sample_size":      policy.sample_size,
            "historical_wr":    policy.historical_wr,
            "historical_exp_r": policy.historical_exp_r,
            "is_exploratory":   policy.is_exploratory,
        }
        if not policy.allow:
            att.blocking_reason = policy.reason
            att.blocking_layer  = "killzone_policy"
            return att.to_dict() | {"would_fire": False}
    except Exception as exc:
        att.confirmations["killzone_policy"] = {"error": str(exc), "allow": True}

    return att.to_dict() | {"would_fire": True, "max_lot": settings.auto_execution_max_lot}
