"""
Paper equity curve tracker + drawdown alerter.

Computes a running equity curve from resolved paper observations (per
engine_id). Detects when drawdown breaches a configurable threshold and
fires a Telegram alert.

Equity curve methodology:
  - Start equity = $10,000 (configurable)
  - Each resolved observation contributes: equity_delta = r_multiple * risk_amount
    where risk_amount = current_equity * risk_percent
  - This produces a path-dependent compounded curve

Drawdown alert:
  - If running drawdown_pct exceeds DRAWDOWN_ALERT_THRESHOLD (10% default)
    AND no alert has been sent in the past DRAWDOWN_COOLDOWN_HOURS (24h),
    fire a Telegram alert via existing telegram_alert_service.

Safe by design: only reads resolved paper observations, never places trades.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from db_models import PaperObservation

log = logging.getLogger(__name__)

INITIAL_EQUITY               = 10_000.0
DEFAULT_RISK_PERCENT         = 0.25
DRAWDOWN_ALERT_THRESHOLD_PCT = 10.0
DRAWDOWN_COOLDOWN_HOURS      = 24

# In-memory tracker for last alert per engine (avoid spam)
_last_alert_at: dict[str, datetime] = {}


@dataclass
class EquityPoint:
    observation_id: int
    observed_at:    str
    engine_id:      str
    equity:         float
    drawdown_pct:   float
    peak_equity:    float
    r_multiple:     float
    risk_amount:    float
    pnl_amount:     float
    result:         str


def compute_equity_curve(
    db: Session,
    engine_id: str | None = None,
    initial_equity: float = INITIAL_EQUITY,
    risk_percent:   float = DEFAULT_RISK_PERCENT,
) -> dict:
    """
    Walk through resolved paper observations in chronological order and
    compute the equity curve + peak + drawdown for each point.

    Returns:
        {
          engineId, initialEquity, finalEquity, peakEquity,
          maxDrawdownPct, currentDrawdownPct, totalTrades,
          totalWinTrades, totalLossTrades, points: [EquityPoint...]
        }
    """
    q = db.query(PaperObservation).filter(
        PaperObservation.result.in_(("WIN", "LOSS", "EXPIRED")),
    ).order_by(PaperObservation.observed_at.asc())
    if engine_id:
        q = q.filter(PaperObservation.engine_id == engine_id)

    rows = q.all()
    if not rows:
        return {
            "engineId":           engine_id or "all",
            "initialEquity":      initial_equity,
            "finalEquity":        initial_equity,
            "peakEquity":         initial_equity,
            "maxDrawdownPct":     0.0,
            "currentDrawdownPct": 0.0,
            "totalTrades":        0,
            "totalWinTrades":     0,
            "totalLossTrades":    0,
            "points":             [],
        }

    equity = initial_equity
    peak   = initial_equity
    max_dd_pct = 0.0
    points: list[dict] = []
    wins = losses = 0

    for obs in rows:
        rm = obs.r_multiple or 0.0
        risk_amount = equity * (risk_percent / 100.0)
        pnl = risk_amount * rm
        equity += pnl
        if equity > peak:
            peak = equity
        dd_pct = ((peak - equity) / peak * 100) if peak > 0 else 0.0
        max_dd_pct = max(max_dd_pct, dd_pct)

        if obs.result == "WIN":
            wins += 1
        elif obs.result == "LOSS":
            losses += 1

        points.append({
            "observationId": obs.id,
            "observedAt":    obs.observed_at.isoformat() if obs.observed_at else None,
            "engineId":      obs.engine_id,
            "equity":        round(equity, 2),
            "drawdownPct":   round(dd_pct, 2),
            "peakEquity":    round(peak, 2),
            "rMultiple":     round(rm, 3),
            "riskAmount":    round(risk_amount, 2),
            "pnlAmount":     round(pnl, 2),
            "result":        obs.result,
        })

    return {
        "engineId":           engine_id or "all",
        "initialEquity":      initial_equity,
        "finalEquity":        round(equity, 2),
        "peakEquity":         round(peak, 2),
        "maxDrawdownPct":     round(max_dd_pct, 2),
        "currentDrawdownPct": round(points[-1]["drawdownPct"], 2) if points else 0.0,
        "totalTrades":        len(rows),
        "totalWinTrades":     wins,
        "totalLossTrades":    losses,
        "netReturnPct":       round((equity - initial_equity) / initial_equity * 100, 2),
        "riskPercent":        risk_percent,
        "points":             points,
    }


def check_drawdown_alert(db: Session, engine_id: str) -> dict:
    """
    Check current drawdown for an engine and fire a Telegram alert if it
    exceeds the threshold AND the cooldown window has elapsed.

    Returns:
        {alerted: bool, drawdownPct: float, reason: str}
    """
    curve = compute_equity_curve(db, engine_id=engine_id)
    dd = curve["currentDrawdownPct"]

    if dd < DRAWDOWN_ALERT_THRESHOLD_PCT:
        return {
            "alerted": False,
            "drawdownPct": dd,
            "reason": f"Drawdown {dd:.2f}% below {DRAWDOWN_ALERT_THRESHOLD_PCT}% threshold",
        }

    # Cooldown check
    now = datetime.now(timezone.utc)
    last = _last_alert_at.get(engine_id)
    if last is not None:
        hours_since = (now - last).total_seconds() / 3600
        if hours_since < DRAWDOWN_COOLDOWN_HOURS:
            return {
                "alerted": False,
                "drawdownPct": dd,
                "reason": f"Cooldown active ({hours_since:.1f}h < {DRAWDOWN_COOLDOWN_HOURS}h)",
            }

    # Fire Telegram alert
    try:
        from services.telegram_alert_service import send_telegram_message, telegram_alerts_enabled
        if telegram_alerts_enabled():
            message = (
                f"⚠️ <b>Drawdown Alert</b>\n"
                f"\n"
                f"Engine: <b>{engine_id}</b>\n"
                f"Current drawdown: <b>{dd:.2f}%</b>\n"
                f"Peak equity: ${curve['peakEquity']:,.2f}\n"
                f"Current equity: ${curve['finalEquity']:,.2f}\n"
                f"Net return: {curve['netReturnPct']:+.2f}%\n"
                f"Trades: {curve['totalTrades']} (W:{curve['totalWinTrades']} L:{curve['totalLossTrades']})\n"
                f"\n"
                f"Paper-tracker drawdown exceeded {DRAWDOWN_ALERT_THRESHOLD_PCT}% threshold."
            )
            ok = send_telegram_message(message)
            if ok:
                _last_alert_at[engine_id] = now
                return {
                    "alerted": True,
                    "drawdownPct": dd,
                    "reason": "Telegram alert sent",
                }
            return {
                "alerted": False,
                "drawdownPct": dd,
                "reason": "Telegram send failed",
            }
        return {
            "alerted": False,
            "drawdownPct": dd,
            "reason": "Telegram alerts disabled",
        }
    except Exception as exc:
        log.warning("[equity_tracker] Drawdown alert send failed: %s", exc)
        return {
            "alerted": False,
            "drawdownPct": dd,
            "reason": f"Alert error: {exc}",
        }
