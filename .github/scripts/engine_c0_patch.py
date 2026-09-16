from __future__ import annotations

import pathlib
import re


def read(path: str) -> str:
    return pathlib.Path(path).read_text(encoding="utf-8")


def write(path: str, text: str) -> None:
    pathlib.Path(path).write_text(text, encoding="utf-8")


def replace_once(path: str, old: str, new: str) -> None:
    text = read(path)
    if old not in text:
        raise SystemExit(f"expected patch anchor not found: {path}: {old[:100]!r}")
    if text.count(old) != 1:
        raise SystemExit(f"patch anchor not unique: {path}: count={text.count(old)}")
    write(path, text.replace(old, new, 1))


# 1) Provider circuit breaker in cloud spot ingestion.
p = "backend/services/candle_ingestion.py"
text = read(p)
pattern = re.compile(
    r"def _fetch_with_fallback\(pair: str, interval: str,\n\s+lookback: int\) -> tuple\[list, str\]:.*?\n\ndef _field",
    re.S,
)
new_fn = '''def _fetch_with_fallback(pair: str, interval: str,
                          lookback: int) -> tuple[list, str]:
    """Fetch a fresh spot payload using a health-aware fallback chain.

    MT5 bars are pushed independently by the Windows bridge. The VPS cloud
    path uses Twelve Data then TradingView OANDA:XAUUSD. Provider health
    circuits prevent a known-broken credential/quota from being retried on
    every timeframe/cycle. Yahoo GC=F is never a spot fallback.
    """
    from services.tradingview_provider import invalidate_cache as _tv_invalidate
    from services.provider_health import should_attempt, note_success, note_failure

    failures: list[str] = []

    td_attempt, td_reason = should_attempt("twelvedata", interval)
    if td_attempt:
        try:
            candles = _fetch_twelvedata(pair, interval, lookback)
            ok, detail = _payload_fresh(candles, interval)
            if ok:
                note_success("twelvedata", interval)
                return candles, "twelvedata"
            failures.append(f"TwelveData rejected: {detail}")
            note_failure("twelvedata", interval, detail, category="stale_payload")
            log.warning("[candle_ingestion] %s %s TwelveData rejected: %s",
                        pair, interval, detail)
        except Exception as exc:
            category = note_failure("twelvedata", interval, exc)
            failures.append(f"TwelveData {type(exc).__name__}: {exc}")
            log.warning(
                "[candle_ingestion] %s %s TwelveData failed (%s): %s: %s",
                pair, interval, category, type(exc).__name__, exc,
            )
    else:
        failures.append(f"TwelveData skipped: {td_reason}")
        log.info("[candle_ingestion] %s %s TwelveData skipped: %s",
                 pair, interval, td_reason)

    tv_attempt, tv_reason = should_attempt("tradingview", interval)
    if tv_attempt:
        for attempt, backoff in enumerate([0.0] + _TV_RETRY_BACKOFFS_S, start=1):
            if backoff > 0:
                time.sleep(backoff)
                _tv_invalidate(pair)
            try:
                candles = _fetch_tradingview(pair, interval, lookback)
                ok, detail = _payload_fresh(candles, interval)
                if ok:
                    note_success("tradingview", interval)
                    if attempt > 1 or failures:
                        log.info(
                            "[candle_ingestion] %s %s using TradingView fallback "
                            "after primary/unhealthy source", pair, interval,
                        )
                    return candles, "tradingview"
                failures.append(f"TradingView attempt {attempt} rejected: {detail}")
                note_failure("tradingview", interval, detail, category="stale_payload")
                log.warning(
                    "[candle_ingestion] %s %s TradingView attempt %d rejected: %s",
                    pair, interval, attempt, detail,
                )
            except Exception as exc:
                category = note_failure("tradingview", interval, exc)
                failures.append(
                    f"TradingView attempt {attempt} {type(exc).__name__}: {exc}"
                )
                log.warning(
                    "[candle_ingestion] %s %s TradingView attempt %d failed (%s): %s: %s",
                    pair, interval, attempt, category, type(exc).__name__, exc,
                )
    else:
        failures.append(f"TradingView skipped: {tv_reason}")
        log.warning("[candle_ingestion] %s %s TradingView skipped: %s",
                    pair, interval, tv_reason)

    summary = " | ".join(failures[-8:])
    raise RuntimeError(
        f"No fresh XAU/USD spot provider for {pair} {interval}. {summary}"
    )


def _field'''
text2, n = pattern.subn(new_fn, text, count=1)
if n != 1:
    raise SystemExit(f"failed to replace _fetch_with_fallback, matches={n}")
write(p, text2)

# 2) Canonical source attribution.
p = "backend/services/canonical_market_data.py"
replace_once(
    p,
    '    tick_source:         str = "unknown"      # mt5 | twelvedata-derived | none\n',
    '    tick_source:         str = "unknown"      # mt5 | <db-provider>-close-proxy | none\n',
)
replace_once(
    p,
    '    return bars\n\n\ndef _live_tick(instrument: str, m5_bars: Sequence[Bar]) -> tuple[Optional[float], Optional[float], Optional[float], Optional[datetime], str, Optional[float]]:\n',
    '''    return bars


def _latest_bar_source(db: Session, instrument: str, tf: str) -> str:
    """Return the provider that supplied the newest persisted candle."""
    try:
        row = db.execute(text(
            "SELECT source FROM historical_candles "
            "WHERE instrument=:i AND timeframe=:t "
            "ORDER BY candle_time DESC LIMIT 1"
        ), {"i": instrument, "t": tf}).fetchone()
        if row and row[0]:
            return str(row[0]).strip().lower()
    except Exception:
        pass
    return "historical-candles"


def _live_tick(instrument: str, m5_bars: Sequence[Bar],
               m5_source: str = "historical-candles") -> tuple[Optional[float], Optional[float], Optional[float], Optional[datetime], str, Optional[float]]:
''',
)
replace_once(
    p,
    '                last.close + half_spread,\n                2 * half_spread,\n                last.time, "twelvedata-derived", 0.0)\n',
    '                last.close + half_spread,\n                2 * half_spread,\n                last.time, f"{m5_source}-close-proxy", 0.0)\n',
)
replace_once(
    p,
    '            tf_slices[tf] = TimeframeSlice(\n                tf=tf, candles=bars,\n                latest_closed=latest_closed,\n                age_min=age_min, threshold_min=threshold,\n                status=status,\n            )\n',
    '            tf_slices[tf] = TimeframeSlice(\n                tf=tf, candles=bars,\n                latest_closed=latest_closed,\n                age_min=age_min, threshold_min=threshold,\n                status=status,\n                source=_latest_bar_source(db, instrument, tf),\n            )\n',
)
replace_once(
    p,
    '        bid, ask, spread, tick_ts, tick_src, tick_lat = _live_tick(instrument, m5_bars)\n',
    '        m5_source = tf_slices.get("M5", TimeframeSlice("M5")).source\n        bid, ask, spread, tick_ts, tick_src, tick_lat = _live_tick(instrument, m5_bars, m5_source)\n',
)

# 3) Expose provider circuits in health.
p = "backend/services/market_data_health.py"
replace_once(
    p,
    '    return {\n        "status": state,\n',
    '    from services.provider_health import provider_health_snapshot\n\n    return {\n        "status": state,\n',
)
replace_once(
    p,
    '        "last_ingest_error": get_last_ingest_error(),\n        "tradingview_enabled": tradingview_enabled,\n',
    '        "last_ingest_error": get_last_ingest_error(),\n        "provider_health": provider_health_snapshot(),\n        "tradingview_enabled": tradingview_enabled,\n',
)

# 4) Cloud-first freshness operator guidance.
p = "backend/services/data_freshness.py"
old = '''    lines.append("Action:")
    if "mt5" in provider_by_tf.values():
        lines.append("  - MT5 daemon push is active. If persistently stale, check laptop daemon logs")
        lines.append("    and MT5 terminal (open a chart for the stale TF to force server subscription).")
    else:
        lines.append("  - MT5 daemon not pushing. Restart mt5_bridge_daemon.py on laptop.")
    lines.append("  - Yahoo GC=F backup engages when TV empties.")
    lines.append("  - Transient drop (recovers in 1-2 cycles): no action needed.")
    lines.append("Engine will refuse to trade on stale data until it clears.")
'''
new = '''    lines.append("Action:")
    lines.append("  - Cloud XAU/USD continuity is primary: check provider_health and last_ingest_error.")
    lines.append("  - TradingView OANDA:XAUUSD is the independent spot fallback when Twelve Data is unavailable.")
    if "mt5" in provider_by_tf.values():
        lines.append("  - HOME MT5 is online and adds broker bars/ticks; it is not required for signal continuity.")
    else:
        lines.append("  - HOME MT5 is offline/absent; cloud signal generation should continue if spot data is fresh.")
    lines.append("  - Yahoo GC=F is futures context only and never satisfies XAU/USD spot freshness.")
    lines.append("  - Transient provider drops normally recover through the fallback chain.")
    lines.append("Actionable signals fail closed only when core XAU/USD M5/M15/H1 data is stale.")
'''
replace_once(p, old, new)

# 5) Attach core-data actionability before strategist side effects.
p = "backend/services/strategist_runner.py"
replace_once(
    p,
    '    verdict = make_decision(db)\n\n    # Pre-compute signal grade',
    '''    verdict = make_decision(db)

    # Universal core-data gate. Optional context (CME/CFTC/macro/MT5)
    # never blocks a signal, but stale M5/M15/H1 spot perception does.
    try:
        from services.actionability_gate import attach_actionability
        attach_actionability(verdict, db)
    except Exception as exc:
        log.warning("[strategist_runner] actionability gate failed closed: %s", exc)
        verdict["data_actionability"] = {
            "actionable": False,
            "status": "DATA_STALE",
            "reason": f"gate_error:{type(exc).__name__}",
            "optional_context_is_gate": False,
        }
        if verdict.get("decision") in ("BUY", "SELL"):
            verdict["execution_status"] = "DATA_STALE"
            verdict["execution_status_reason"] = "Core market-data gate unavailable"
            permission = verdict.get("execution_permission") or {}
            permission["allow_alert"] = False
            permission["allow_execute"] = False
            verdict["execution_permission"] = permission

    # Pre-compute signal grade''',
)

# The grader consumes the attached gate but stays deterministic/replay-safe.
p = "backend/services/signal_grading.py"
replace_once(
    p,
    '    _attach_cme_context(verdict)\n\n    decision = verdict.get("decision")\n',
    '''    _attach_cme_context(verdict)

    data_gate = verdict.get("data_actionability")
    if isinstance(data_gate, dict) and not data_gate.get("actionable", True):
        return GradeResult(
            GRADE_ASIDE,
            f"core market data not actionable: {data_gate.get('reason', 'unknown')}",
            False, False, True,
        )

    decision = verdict.get("decision")
''',
)

# 6) Reset provider state between continuity tests.
p = "backend/tests/test_candle_ingestion_continuity.py"
replace_once(
    p,
    'from services import candle_ingestion as ci\n\n\ndef _bar',
    '''from services import candle_ingestion as ci
from services.provider_health import reset_provider_health


@pytest.fixture(autouse=True)
def _reset_provider_state():
    reset_provider_health()
    yield
    reset_provider_health()


def _bar''',
)

# 7) Extend continuity CI.
p = ".github/workflows/data-continuity-tests.yml"
text = read(p)
anchor = "      - 'backend/services/market_data_health.py'\n"
addition = (
    "      - 'backend/services/provider_health.py'\n"
    "      - 'backend/services/actionability_gate.py'\n"
    "      - 'backend/services/canonical_market_data.py'\n"
    "      - 'backend/services/data_freshness.py'\n"
    "      - 'backend/services/strategist_runner.py'\n"
)
if text.count(anchor) != 2:
    raise SystemExit(f"expected two CI service anchors, got {text.count(anchor)}")
text = text.replace(anchor, anchor + addition)

test_anchor = "      - 'backend/tests/test_market_data_health_endpoint.py'\n"
test_addition = (
    "      - 'backend/tests/test_provider_health.py'\n"
    "      - 'backend/tests/test_actionability_gate.py'\n"
)
if text.count(test_anchor) != 2:
    raise SystemExit(f"expected two CI test anchors, got {text.count(test_anchor)}")
text = text.replace(test_anchor, test_anchor + test_addition)

compile_anchor = "          python -m py_compile services/market_data_health.py\n"
if text.count(compile_anchor) != 1:
    raise SystemExit("compile anchor missing/non-unique")
text = text.replace(
    compile_anchor,
    compile_anchor
    + "          python -m py_compile services/provider_health.py\n"
    + "          python -m py_compile services/actionability_gate.py\n"
    + "          python -m py_compile services/canonical_market_data.py\n"
    + "          python -m py_compile services/data_freshness.py\n"
    + "          python -m py_compile services/strategist_runner.py\n",
    1,
)

pytest_anchor = "            tests/test_market_data_health_endpoint.py \\\n"
if text.count(pytest_anchor) != 1:
    raise SystemExit("pytest anchor missing/non-unique")
text = text.replace(
    pytest_anchor,
    pytest_anchor
    + "            tests/test_provider_health.py \\\n"
    + "            tests/test_actionability_gate.py \\\n",
    1,
)
write(p, text)

print("C0 remediation patches applied")
