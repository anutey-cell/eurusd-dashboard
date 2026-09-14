"""Read-only Predator notification shadow-vs-legacy parity audit.

Compares the gateway's projected ACTIONABLE decisions in shadow mode against
legacy Telegram send attempts using the persistent notification event ledger.
Adds setup-registry attribution so divergence can be separated by archetype,
trading date and observation timing.

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


def _iso(v):
    d = _dt(v)
    return d.isoformat() if d else (str(v) if v is not None else None)


def _nested_counter(events, *keys):
    out = Counter()
    for e in events:
        out[tuple(str(e.get(k) or "none") for k in keys)] += 1
    return {" | ".join(k): v for k, v in sorted(out.items())}


def main():
    cutoff = datetime.now(timezone.utc) - timedelta(days=LOOKBACK_DAYS)
    with SessionLocal() as db:
        rows = db.execute(text(
            "SELECT e.created_at, e.setup_id, e.architecture_mode, e.decision, e.reason, "
            "e.msg_type, e.internal_state, e.notification_state, "
            "s.archetype, s.direction, s.session, s.trading_date, "
            "s.first_seen_at, s.last_seen_at, s.last_evaluated_bar, "
            "s.latest_price, s.latest_confidence, s.latest_regime, "
            "s.shadow_notification_state, s.notification_state "
            "FROM predator_notification_events e "
            "LEFT JOIN predator_setups s ON s.setup_id = e.setup_id "
            "WHERE e.created_at >= :cutoff ORDER BY e.created_at"
        ), {"cutoff": cutoff}).fetchall()

    events = []
    for r in rows:
        events.append({
            "created_at": _iso(r[0]),
            "setup_id": r[1],
            "mode": r[2],
            "decision": r[3],
            "reason": r[4],
            "msg_type": r[5],
            "internal_state": r[6],
            "event_notification_state": r[7],
            "archetype": r[8] or "UNKNOWN",
            "direction": r[9],
            "session": r[10],
            "trading_date": r[11],
            "first_seen_at": _iso(r[12]),
            "last_seen_at": _iso(r[13]),
            "last_evaluated_bar": _iso(r[14]),
            "latest_price": r[15],
            "latest_confidence": r[16],
            "latest_regime": r[17],
            "setup_shadow_state": r[18],
            "setup_real_state": r[19],
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

    suppressed = [
        e for e in events
        if e["mode"] == "shadow" and e["decision"] == "WOULD_SUPPRESS"
    ]
    suppressed_fire = [e for e in suppressed if e["internal_state"] == "FIRE"]
    legacy_events = [
        e for e in events
        if e["mode"] == "legacy" and e["decision"] == "LEGACY_SEND_ATTEMPT"
    ]
    legacy_fire_events = [e for e in legacy_events if e["internal_state"] == "FIRE"]

    reasons = Counter(e["reason"] or "none" for e in suppressed)
    decisions = Counter((e["mode"], e["decision"]) for e in events)

    # Representative row per setup helps diagnose whether divergences are old
    # pre-fix hypotheses, stale repeated observations, or a current archetype.
    representative = {}
    for e in events:
        representative[e["setup_id"]] = e

    dangerous_details = []
    for sid in sorted(dangerous_legacy):
        e = representative.get(sid, {})
        reasons_for_sid = sorted({
            x.get("reason") or "none" for x in suppressed_fire
            if x.get("setup_id") == sid
        })
        dangerous_details.append({
            "setup_id": sid,
            "archetype": e.get("archetype"),
            "session": e.get("session"),
            "trading_date": e.get("trading_date"),
            "first_seen_at": e.get("first_seen_at"),
            "last_seen_at": e.get("last_seen_at"),
            "last_evaluated_bar": e.get("last_evaluated_bar"),
            "latest_confidence": e.get("latest_confidence"),
            "latest_regime": e.get("latest_regime"),
            "shadow_suppression_reasons": reasons_for_sid,
        })

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
        "shadow_suppressions_by_reason_and_archetype": _nested_counter(
            suppressed, "reason", "archetype"
        ),
        "shadow_fire_suppressions_by_reason_archetype_session": _nested_counter(
            suppressed_fire, "reason", "archetype", "session"
        ),
        "legacy_fire_attempts_by_archetype_session": _nested_counter(
            legacy_fire_events, "archetype", "session"
        ),
        "dangerous_divergence_by_archetype": _nested_counter(
            [e for e in suppressed_fire if e["setup_id"] in dangerous_legacy],
            "archetype", "reason"
        ),
        "dangerous_setup_details": dangerous_details,
        "decision_counts": {f"{k[0]}:{k[1]}": v for k, v in decisions.items()},
        "promotion_criteria": {
            "minimum_shadow_would_send_sample": MIN_PROMOTION_SAMPLE,
            "minimum_overlap_rate": MIN_OVERLAP_RATE,
            "maximum_legacy_only_rate": MAX_DIVERGENCE_RATE,
            "dangerous_divergence_must_be_zero": True,
        },
        "gateway_ready_for_sole_sender": promote,
    }
    print(json.dumps(report, indent=2, sort_keys=True, default=str))


if __name__ == "__main__":
    main()
