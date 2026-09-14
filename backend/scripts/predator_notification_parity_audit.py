"""Read-only Predator notification shadow-vs-legacy parity audit.

Compares the gateway's projected ACTIONABLE decisions in shadow mode against
legacy Telegram send attempts using the persistent notification event ledger.
No sends and no database writes.
"""
from __future__ import annotations

import json
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone

from sqlalchemy import text

from database import SessionLocal

LOOKBACK_DAYS = 30
MIN_PROMOTION_SAMPLE = 20
MIN_OVERLAP_RATE = 0.95
MAX_DIVERGENCE_RATE = 0.05


def _dt(v):
    if v is None:
        return None
    if isinstance(v, datetime):
        return v
    try:
        return datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    except Exception:
        return None


def main():
    cutoff = datetime.now(timezone.utc) - timedelta(days=LOOKBACK_DAYS)
    with SessionLocal() as db:
        rows = db.execute(text(
            "SELECT created_at, setup_id, architecture_mode, decision, reason, "
            "msg_type, internal_state, notification_state "
            "FROM predator_notification_events "
            "WHERE created_at >= :cutoff ORDER BY created_at"
        ), {"cutoff": cutoff}).fetchall()

    events = []
    for r in rows:
        events.append({
            "created_at": _dt(r[0]).isoformat() if _dt(r[0]) else str(r[0]),
            "setup_id": r[1], "mode": r[2], "decision": r[3],
            "reason": r[4], "msg_type": r[5], "internal_state": r[6],
            "notification_state": r[7],
        })

    shadow_would = {
        e["setup_id"] for e in events
        if e["mode"] == "shadow" and e["decision"] == "WOULD_SEND"
        and e["internal_state"] == "FIRE"
    }
    legacy_fire = {
        e["setup_id"] for e in events
        if e["mode"] == "legacy" and e["decision"] == "LEGACY_SEND_ATTEMPT"
        and e["internal_state"] == "FIRE"
    }
    shadow_suppressed_fire = {
        e["setup_id"] for e in events
        if e["mode"] == "shadow" and e["decision"] == "WOULD_SUPPRESS"
        and e["internal_state"] == "FIRE"
    }

    overlap = shadow_would & legacy_fire
    shadow_only = shadow_would - legacy_fire
    legacy_only = legacy_fire - shadow_would
    dangerous_legacy = legacy_fire & shadow_suppressed_fire

    n_shadow = len(shadow_would)
    overlap_rate = len(overlap) / n_shadow if n_shadow else 0.0
    legacy_div_rate = len(legacy_only) / max(len(legacy_fire), 1)
    dangerous_rate = len(dangerous_legacy) / max(len(legacy_fire), 1)

    reasons = Counter(
        e["reason"] or "none" for e in events
        if e["mode"] == "shadow" and e["decision"] == "WOULD_SUPPRESS"
    )
    decisions = Counter((e["mode"], e["decision"]) for e in events)

    promote = (
        n_shadow >= MIN_PROMOTION_SAMPLE
        and overlap_rate >= MIN_OVERLAP_RATE
        and legacy_div_rate <= MAX_DIVERGENCE_RATE
        and dangerous_rate == 0.0
    )

    report = {
        "mode": "READ_ONLY_NOTIFICATION_PARITY_AUDIT",
        "lookback_days": LOOKBACK_DAYS,
        "event_count": len(events),
        "shadow_would_send_fire_setups": n_shadow,
        "legacy_fire_send_attempt_setups": len(legacy_fire),
        "overlap_setups": len(overlap),
        "overlap_rate_vs_shadow": round(overlap_rate, 4),
        "shadow_only_setups": sorted(shadow_only),
        "legacy_only_setups": sorted(legacy_only),
        "legacy_only_rate": round(legacy_div_rate, 4),
        "legacy_sent_despite_shadow_suppression": sorted(dangerous_legacy),
        "dangerous_divergence_rate": round(dangerous_rate, 4),
        "shadow_suppression_reasons": dict(reasons),
        "decision_counts": {f"{k[0]}:{k[1]}": v for k, v in decisions.items()},
        "promotion_criteria": {
            "minimum_shadow_would_send_sample": MIN_PROMOTION_SAMPLE,
            "minimum_overlap_rate": MIN_OVERLAP_RATE,
            "maximum_legacy_only_rate": MAX_DIVERGENCE_RATE,
            "dangerous_divergence_must_be_zero": True,
        },
        "gateway_ready_for_sole_sender": promote,
    }
    print(json.dumps(report, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
