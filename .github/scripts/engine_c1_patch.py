from __future__ import annotations

import pathlib


def read(path: str) -> str:
    return pathlib.Path(path).read_text(encoding="utf-8")


def write(path: str, text: str) -> None:
    pathlib.Path(path).write_text(text, encoding="utf-8")


def replace_once(path: str, old: str, new: str) -> None:
    text = read(path)
    if text.count(old) != 1:
        raise SystemExit(f"patch anchor missing/non-unique: {path}: count={text.count(old)}")
    write(path, text.replace(old, new, 1))


# 1) Separate always-on intelligence computation from Telegram delivery.
p = "backend/config.py"
replace_once(
    p,
    '''    xauusd_macro_interpretation_enabled: bool = False
    xauusd_market_intelligence_telegram_enabled: bool = False
    xauusd_market_intel_shadow_mode: bool = True     # canonical shadow while true
''',
    '''    xauusd_macro_interpretation_enabled: bool = False
    # Always-on computation is independent of Telegram delivery. This lets the
    # opportunity state / breakout acceptance stack continuously inform other
    # engines without turning on a separate alert stream.
    xauusd_market_intelligence_compute_enabled: bool = True
    xauusd_market_intelligence_telegram_enabled: bool = False
    xauusd_market_intel_shadow_mode: bool = True     # delivery dry-run while true
''',
)

# 2) Scheduler computes the intelligence pipeline independent of delivery.
p = "backend/services/background_scheduler.py"
replace_once(
    p,
    '''# Every 60s, run the full Phase 2-10 pipeline and let the intel engine
# decide whether any new alert candidates fire. Governed by two flags:
#   xauusd_market_intelligence_telegram_enabled — master switch
#   xauusd_market_intel_shadow_mode              — persist-only (no send)
# When flag is False, this loop still runs but all candidates are
# suppressed with reason "flag off" — so nothing sends, nothing is stored.
''',
    '''# Every 60s, run the full Phase 2-10 pipeline and publish one canonical
# opportunity-arbiter context. Computation and Telegram delivery are separate:
#   xauusd_market_intelligence_compute_enabled  — keeps the market brain alive
#   xauusd_market_intelligence_telegram_enabled — optional intelligence alerts
#   xauusd_market_intel_shadow_mode             — delivery dry-run when enabled
# The arbiter is context only; it is never an execution authority.
''',
)
replace_once(
    p,
    '''        try:
            # Only run pipeline if flag is on — else save the compute
            if getattr(settings, "xauusd_market_intelligence_telegram_enabled", False):
                await asyncio.to_thread(_run_market_intel_iteration)
''',
    '''        try:
            if getattr(settings, "xauusd_market_intelligence_compute_enabled", True):
                await asyncio.to_thread(_run_market_intel_iteration)
''',
)
replace_once(
    p,
    '''    from services.market_intelligence_alerts import fire_intel_alerts
    from config import settings
''',
    '''    from services.market_intelligence_alerts import fire_intel_alerts
    from services.actionability_gate import evaluate_db_actionability
    from services.opportunity_arbiter import build_arbiter_context, publish_arbiter_context
    from config import settings
''',
)
old = '''        outcomes = fire_intel_alerts(
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
'''
new = '''        # Publish one canonical opportunity-arbiter context every cycle.
        # Optional layers are best-effort and never become hard signal gates.
        actionability = evaluate_db_actionability(db)
        cme = None
        try:
            from services.cme_options_context import get_cme_options_context
            current_xau = None
            if snap.bid is not None and snap.ask is not None:
                current_xau = (float(snap.bid) + float(snap.ask)) / 2.0
            cme = get_cme_options_context(db, current_xau=current_xau, top_n=5)
        except Exception as exc:
            log.debug("[market-intel] CME context unavailable: %s", exc)

        arbiter = build_arbiter_context(
            snapshot=snap, htf_alignment=htf, regime=regime, evidence=evidence,
            breakouts=breakouts, state_transition=state_tr,
            separated_verdict=verdict, ranking=ranking, macro=macro,
            cme=cme, actionability=actionability,
        )
        publish_arbiter_context(arbiter)

        structure = arbiter.get("structure") or {}
        if structure.get("manipulation_candidate"):
            log.info(
                "[market-intel] liquidity-event candidate %s %s %.2f class=%s conf=%s",
                structure.get("liquidity_side"), structure.get("level_name"),
                float(structure.get("level") or 0.0), structure.get("classification"),
                structure.get("confidence"),
            )

        # Delivery remains independently gated. With Telegram off, the engine
        # still computes/persists market state but emits no extra alert stream.
        outcomes = []
        if getattr(settings, "xauusd_market_intelligence_telegram_enabled", False):
            outcomes = fire_intel_alerts(
                db, prev_state=state_tr.prev_state, new_state=state_tr.new_state,
                trigger_condition=state_tr.trigger_condition,
                trigger_price=state_tr.price,
                snapshot=snap, verdict=verdict, evidence=evidence, ranking=ranking,
                macro=macro, state_transition=state_tr, breakouts=breakouts,
            )
        interesting = [o for o in outcomes if o.result in ("sent", "shadow")]
        if interesting:
            log.info("[market-intel] fired: %s",
                     [(o.alert_type, o.result) for o in interesting])
'''
replace_once(p, old, new)

# 3) Every mandate verdict carries the latest arbiter context, non-blocking.
p = "backend/services/strategist_runner.py"
replace_once(
    p,
    '''    # Pre-compute signal grade so downstream side-effects (enqueue + alert)
''',
    '''    # Attach the always-on market-intelligence/structure context. This is
    # observational reinforcement only; it does not allow/deny a Strategist signal.
    try:
        from services.opportunity_arbiter import get_latest_arbiter_context
        verdict["opportunity_arbiter"] = get_latest_arbiter_context(max_age_s=180)
    except Exception as exc:
        log.debug("[strategist_runner] arbiter context unavailable: %s", exc)
        verdict["opportunity_arbiter"] = {
            "status": "UNAVAILABLE", "stale": True,
            "policy": {"arbiter_is_execution_authority": False},
        }

    # Pre-compute signal grade so downstream side-effects (enqueue + alert)
''',
)

print("C1 arbiter wiring applied")
