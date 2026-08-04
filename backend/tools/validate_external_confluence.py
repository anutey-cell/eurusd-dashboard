"""Validate external_confluence — every scenario from the operator brief."""
from __future__ import annotations
import os, sys
from types import SimpleNamespace

try:
    import services.external_confluence  # noqa
except ImportError:
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from services import external_confluence as ec
from services.external_confluence import (
    get_external_confluence, status_for, invalidate_cache,
    EXEC_EXT_CONFIRMED, EXEC_EXT_NEUTRAL, EXEC_EXT_CONFLICT, EXEC_EXT_UNAVAILABLE,
)


TOTAL, PASSED = 0, 0
def _hr(t): print("\n" + "-"*78 + f"\n {t}\n" + "-"*78)
def _run(name, fn):
    global TOTAL, PASSED
    TOTAL += 1
    try: fn(); print(f"  OK   {name}"); PASSED += 1
    except AssertionError as e: print(f"  FAIL {name}: {e}")
    except Exception as e: print(f"  FAIL {name}: {type(e).__name__}: {e}")


def _settings(**overrides):
    s = SimpleNamespace(
        external_confluence_enabled=True,
        fastbull_confluence_enabled=True,
        cme_confluence_enabled=True,
        external_confluence_cache_seconds=600,
        external_confluence_http_timeout_s=3,
        external_confluence_max_upgrade=10,
        external_confluence_max_downgrade=15,
    )
    for k, v in overrides.items():
        setattr(s, k, v)
    return s


def _stub_providers(fb_return, cme_return, monkeypatch=None):
    """Monkey-patch the module-level imports so provider fetches are deterministic."""
    import services.fastbull_provider as fb
    import services.cme_provider as cme
    fb.fetch_fastbull_positioning = lambda timeout_s=3.0: fb_return
    cme.fetch_cme_context = lambda db=None, timeout_s=3.0, spot_price=None: cme_return


# ── Provider unavailable ────────────────────────────────────────────────────

def test_both_providers_unavailable_returns_neutral():
    invalidate_cache()
    _stub_providers(
        fb_return={"available": False, "long_short_bias": "unknown",
                    "interpretation": "network"},
        cme_return={"available": False, "futures_bias": "unknown",
                     "interpretation": "network"},
    )
    r = get_external_confluence(engine_direction="BUY", settings=_settings())
    assert r["confluence"]["bias"] == "unknown"
    assert r["confluence"]["score_adjustment"] == 0
    assert r["confluence"]["blocks_trade"] is False
    assert status_for(r, "BUY") == EXEC_EXT_UNAVAILABLE


def test_fastbull_unavailable_but_cme_bullish():
    invalidate_cache()
    _stub_providers(
        fb_return={"available": False, "long_short_bias": "unknown"},
        cme_return={"available": True, "futures_bias": "bullish",
                     "gc_price": 4050.0, "interpretation": "GC up 5"},
    )
    r = get_external_confluence(engine_direction="BUY", settings=_settings())
    assert r["confluence"]["bias"] == "bullish"
    assert r["confluence"]["score_adjustment"] == 5   # single-source confirm
    assert r["confluence"]["blocks_trade"] is False


# ── Confirmation cases ──────────────────────────────────────────────────────

def test_confluence_confirms_sell_both_sources():
    invalidate_cache()
    _stub_providers(
        fb_return={"available": True, "long_short_bias": "long_heavy",
                    "long_pct": 70, "short_pct": 30,
                    "interpretation": "crowd long-heavy"},
        cme_return={"available": True, "futures_bias": "bearish",
                     "gc_price": 4040.0, "interpretation": "GC down"},
    )
    r = get_external_confluence(engine_direction="SELL", settings=_settings())
    assert r["confluence"]["bias"] == "bearish"
    assert r["confluence"]["score_adjustment"] == 10   # both confirm
    assert r["confluence"]["blocks_trade"] is False
    assert status_for(r, "SELL") == EXEC_EXT_CONFIRMED


def test_confluence_confirms_buy_both_sources():
    invalidate_cache()
    _stub_providers(
        fb_return={"available": True, "long_short_bias": "short_heavy",
                    "long_pct": 30, "short_pct": 70,
                    "interpretation": "crowd short-heavy"},
        cme_return={"available": True, "futures_bias": "bullish",
                     "gc_price": 4055.0, "interpretation": "GC up"},
    )
    r = get_external_confluence(engine_direction="BUY", settings=_settings())
    assert r["confluence"]["bias"] == "bullish"
    assert r["confluence"]["score_adjustment"] == 10
    assert status_for(r, "BUY") == EXEC_EXT_CONFIRMED


# ── Conflict cases ──────────────────────────────────────────────────────────

def test_confluence_conflicts_with_engine_direction_soft():
    """One source opposes → -5, no block."""
    invalidate_cache()
    _stub_providers(
        fb_return={"available": True, "long_short_bias": "short_heavy",
                    "long_pct": 30, "short_pct": 70,
                    "interpretation": ""},
        cme_return={"available": False, "futures_bias": "unknown"},
    )
    r = get_external_confluence(engine_direction="SELL", settings=_settings())
    assert r["confluence"]["bias"] == "bullish"    # FB contrarian read
    assert r["confluence"]["score_adjustment"] == -5
    assert r["confluence"]["blocks_trade"] is False
    assert status_for(r, "SELL") == EXEC_EXT_CONFLICT


def test_confluence_conflicts_both_sources_blocks():
    """Both sources oppose engine → -15 AND blocks_trade=True."""
    invalidate_cache()
    _stub_providers(
        fb_return={"available": True, "long_short_bias": "short_heavy",
                    "long_pct": 30, "short_pct": 70,
                    "interpretation": ""},
        cme_return={"available": True, "futures_bias": "bullish",
                     "gc_price": 4050.0, "interpretation": ""},
    )
    r = get_external_confluence(engine_direction="SELL", settings=_settings())
    assert r["confluence"]["bias"] == "bullish"
    assert r["confluence"]["score_adjustment"] == -15
    assert r["confluence"]["blocks_trade"] is True
    assert status_for(r, "SELL") == EXEC_EXT_CONFLICT


def test_confluence_internally_conflicted_reduces_confidence():
    """FB bullish + CME bearish → conflicted, -5, no block."""
    invalidate_cache()
    _stub_providers(
        fb_return={"available": True, "long_short_bias": "short_heavy",
                    "long_pct": 30, "short_pct": 70, "interpretation": ""},
        cme_return={"available": True, "futures_bias": "bearish",
                     "gc_price": 4040.0, "interpretation": ""},
    )
    r = get_external_confluence(engine_direction="BUY", settings=_settings())
    assert r["confluence"]["bias"] == "conflicted"
    assert r["confluence"]["score_adjustment"] == -5
    assert r["confluence"]["blocks_trade"] is False


# ── Neutral cases ───────────────────────────────────────────────────────────

def test_confluence_neutral_when_both_balanced():
    invalidate_cache()
    _stub_providers(
        fb_return={"available": True, "long_short_bias": "balanced",
                    "long_pct": 50, "short_pct": 50, "interpretation": ""},
        cme_return={"available": True, "futures_bias": "neutral",
                     "gc_price": 4050.0, "interpretation": "GC flat"},
    )
    r = get_external_confluence(engine_direction="BUY", settings=_settings())
    assert r["confluence"]["bias"] == "neutral"
    assert r["confluence"]["score_adjustment"] == 0
    assert r["confluence"]["blocks_trade"] is False
    assert status_for(r, "BUY") == EXEC_EXT_NEUTRAL


# ── Anti-noise: engine standing aside ─────────────────────────────────────

def test_engine_stand_aside_external_bullish_no_upgrade():
    """External bias must NEVER turn a stand-aside into a trade."""
    invalidate_cache()
    _stub_providers(
        fb_return={"available": True, "long_short_bias": "short_heavy",
                    "long_pct": 30, "short_pct": 70, "interpretation": ""},
        cme_return={"available": True, "futures_bias": "bullish",
                     "gc_price": 4055.0, "interpretation": ""},
    )
    r = get_external_confluence(engine_direction="STAND_ASIDE", settings=_settings())
    # bias still computes internally...
    assert r["confluence"]["bias"] == "bullish"
    # ...but score adjustment is ZERO — engine ignores external when flat
    assert r["confluence"]["score_adjustment"] == 0
    assert r["confluence"]["blocks_trade"] is False


# ── Config gate ─────────────────────────────────────────────────────────────

def test_disabled_via_settings():
    invalidate_cache()
    _stub_providers(
        fb_return={"available": True, "long_short_bias": "long_heavy"},
        cme_return={"available": True, "futures_bias": "bearish"},
    )
    r = get_external_confluence(
        engine_direction="BUY",
        settings=_settings(external_confluence_enabled=False),
    )
    assert r["enabled"] is False
    assert r["confluence"]["score_adjustment"] == 0
    assert r["confluence"]["blocks_trade"] is False
    assert status_for(r, "BUY") == EXEC_EXT_UNAVAILABLE


# ── Cache ───────────────────────────────────────────────────────────────────

def test_cache_hit_reuses_provider_data():
    invalidate_cache()
    calls = {"fb": 0, "cme": 0}
    import services.fastbull_provider as fbmod
    import services.cme_provider as cmemod
    def _fb(**kw): calls["fb"] += 1; return {"available": True,
                                              "long_short_bias": "balanced",
                                              "long_pct": 50, "short_pct": 50}
    def _cme(**kw): calls["cme"] += 1; return {"available": True,
                                                "futures_bias": "neutral",
                                                "gc_price": 4050.0}
    fbmod.fetch_fastbull_positioning = lambda timeout_s=3.0: _fb()
    cmemod.fetch_cme_context = lambda db=None, timeout_s=3.0, spot_price=None: _cme()

    get_external_confluence(engine_direction="BUY", settings=_settings())
    get_external_confluence(engine_direction="SELL", settings=_settings())   # 2nd call
    assert calls["fb"] == 1, f"expected 1 fetch, got {calls['fb']}"
    assert calls["cme"] == 1, f"expected 1 fetch, got {calls['cme']}"


# ── Robustness ──────────────────────────────────────────────────────────────

def test_provider_raises_is_swallowed():
    """If a provider stub RAISES, the aggregator returns error-shape, not crash."""
    invalidate_cache()
    import services.fastbull_provider as fbmod
    def _boom(**kw): raise RuntimeError("boom")
    fbmod.fetch_fastbull_positioning = lambda timeout_s=3.0: _boom()
    import services.cme_provider as cmemod
    cmemod.fetch_cme_context = lambda db=None, timeout_s=3.0, spot_price=None: {
        "available": False, "futures_bias": "unknown"}

    r = get_external_confluence(engine_direction="BUY", settings=_settings())
    # Should return SOMETHING sensible — no exception propagates
    assert r["confluence"]["blocks_trade"] is False
    assert r["confluence"]["score_adjustment"] == 0


# ── Main ────────────────────────────────────────────────────────────────────

def main():
    print("=" * 78)
    print(" EXTERNAL-CONFLUENCE VALIDATION (P134)")
    print("=" * 78)

    _hr("1. Providers unavailable")
    _run("both unavailable → neutral/unknown", test_both_providers_unavailable_returns_neutral)
    _run("FB unavailable + CME bullish → +5", test_fastbull_unavailable_but_cme_bullish)

    _hr("2. Confirmation")
    _run("both confirm SELL → +10 CONFIRMED", test_confluence_confirms_sell_both_sources)
    _run("both confirm BUY → +10 CONFIRMED", test_confluence_confirms_buy_both_sources)

    _hr("3. Conflict")
    _run("one source opposes → -5 CONFLICT (no block)",
         test_confluence_conflicts_with_engine_direction_soft)
    _run("both oppose → -15 CONFLICT + blocks_trade=True",
         test_confluence_conflicts_both_sources_blocks)
    _run("FB vs CME conflict → conflicted, -5",
         test_confluence_internally_conflicted_reduces_confidence)

    _hr("4. Neutral")
    _run("both balanced → NEUTRAL, 0",
         test_confluence_neutral_when_both_balanced)

    _hr("5. Anti-noise (critical)")
    _run("engine stand-aside → external CANNOT upgrade to trade",
         test_engine_stand_aside_external_bullish_no_upgrade)

    _hr("6. Config gate")
    _run("disabled → UNAVAILABLE, adj=0", test_disabled_via_settings)

    _hr("7. Cache")
    _run("2nd call within TTL reuses provider payloads",
         test_cache_hit_reuses_provider_data)

    _hr("8. Robustness")
    _run("provider raise is swallowed", test_provider_raises_is_swallowed)

    print("\n" + "=" * 78)
    if PASSED == TOTAL:
        print(f" ALL PASS {PASSED}/{TOTAL} — external confluence ready")
        return 0
    print(f" FAIL {PASSED}/{TOTAL}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
