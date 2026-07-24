"""Validate telegram_templates.py — every renderer + MarkdownV2 correctness."""
from __future__ import annotations

import re
import sys
from datetime import datetime, timezone, timedelta

# Path bootstrap so we can run from repo root or from /app
_here = None
try:
    import services.canonical_signal  # noqa: F401
except ImportError:
    import os
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from services.canonical_signal import (
    CanonicalSignal, signal_fingerprint, make_signal_id,
    STATE_MONITORING, STATE_ARMED, STATE_TRIGGERED, STATE_ACTIVE,
    STATE_TP1_HIT, STATE_TP2_HIT, STATE_TP3_HIT,
    STATE_BREAKEVEN, STATE_TRAILING, STATE_STOPPED,
    STATE_INVALIDATED, STATE_EXPIRED,
    DIRECTION_BUY, DIRECTION_SELL,
    STRATEGY_MANDATE, STRATEGY_VP_TRAP, STRATEGY_AGGREGATED,
)
from services.telegram_templates import (
    render, _esc,
    MODE_MINIMAL, MODE_STANDARD, MODE_DETAILED,
    TELEGRAM_MAX_BYTES,
)


def _hr(title: str) -> None:
    print("\n" + "─" * 78 + f"\n {title}\n" + "─" * 78)


def _make_signal(state: str = STATE_ARMED, direction: str = DIRECTION_BUY,
                 strategy: str = STRATEGY_MANDATE,
                 tp1: float = 4025.0, tp2: float = 4030.0, tp3: float = None,
                 confidence: int = 82) -> CanonicalSignal:
    now = datetime.now(timezone.utc)
    fp = signal_fingerprint(
        instrument="XAUUSD", direction=direction, strategy_id=strategy,
        entry_zone_low=4018.0, entry_zone_high=4020.0, stop_loss=4014.0,
        session="London KZ", created_at=now,
    )
    return CanonicalSignal(
        signal_id=make_signal_id(strategy, 1, "XAU", now),
        fingerprint=fp, strategy_id=strategy, strategy_name="Mandate 5-Gate",
        instrument="XAUUSD", direction=direction, confidence=confidence,
        entry_zone_low=4018.0, entry_zone_high=4020.0,
        stop_loss=4014.0, current_stop=4014.0,
        invalidation="Close M15 below 4012.5",
        tp1=tp1, tp2=tp2, tp3=tp3,
        tp1_label="Prev-day POC", tp2_label="Prior high",
        rr_tp1=1.5, rr_tp2=2.5, rr_tp3=3.5 if tp3 else None,
        no_chase_price=4022.0, session="London KZ",
        market_regime="Bullish continuation", htf_bias="Bull",
        trap_side="bear_trap",
        conditions_met=("C1: HTF bull", "C2: London KZ", "C3: CISD sniper", "C4: liquidity clear"),
        conditions_missing=("C5: momentum burst",),
        rationale="High-quality pullback into VAH after bear-trap sweep of PDL.",
        confluence=({"strategy_name": "VP Trap", "confidence": 78},
                    {"strategy_name": "KZ Magnet", "confidence": 71}),
        state=state, created_at=now,
        valid_until=now + timedelta(hours=2),
    )


# ── MarkdownV2 correctness checker ───────────────────────────────────────────

# Chars a backslash may precede (per MDV2 escape rules).
MDV2_ESCAPABLE = set("_*[]()~`>#+-=|{}.!\\")

# Specials that break MDV2 parsing when NOT escaped AND NOT used as formatting.
# `*` and `_` are legit formatting markers so we don't flag every occurrence —
# instead we check that they appear in matched pairs (bold/italic balance).
MDV2_SPECIAL_HARD = set("[]()~>#+-=|{}.!")


def _check_mdv2(text: str) -> list[str]:
    """Return list of MDV2 escape errors in the rendered text.

    Rules:
      * Backticks toggle code-span mode; everything inside is literal, skipped.
      * A backslash must be followed by an escapable char.
      * `*` (bold) and `_` (italic) may appear unescaped as formatting; count
        them and flag if the total (outside code) is odd.
      * Every other MDV2 special MUST be preceded by a backslash.
    """
    errors = []
    in_code = False
    star_count = 0
    underscore_count = 0
    i = 0
    while i < len(text):
        ch = text[i]
        if ch == "`":
            in_code = not in_code
            i += 1
            continue
        if in_code:
            i += 1
            continue
        if ch == "\\":
            if i + 1 >= len(text) or text[i + 1] not in MDV2_ESCAPABLE:
                errors.append(f"dangling backslash at pos {i}: ...{text[max(0, i-8):i+4]!r}...")
                i += 1
            else:
                i += 2   # skip escape sequence
            continue
        if ch == "*":
            star_count += 1
        elif ch == "_":
            underscore_count += 1
        elif ch in MDV2_SPECIAL_HARD:
            errors.append(f"unescaped {ch!r} at pos {i}: ...{text[max(0, i-8):i+8]!r}...")
        i += 1
    if star_count % 2 != 0:
        errors.append(f"unbalanced bold markers ({star_count} × '*')")
    if underscore_count % 2 != 0:
        errors.append(f"unbalanced italic markers ({underscore_count} × '_')")
    return errors


# ── Individual test blocks ───────────────────────────────────────────────────

TOTAL = 0
PASSED = 0


def _run(name: str, fn) -> None:
    global TOTAL, PASSED
    TOTAL += 1
    try:
        fn()
        print(f"  ✓ {name}")
        PASSED += 1
    except AssertionError as exc:
        print(f"  ✗ {name}: {exc}")
    except Exception as exc:
        print(f"  ✗ {name}: unexpected {type(exc).__name__}: {exc}")


def test_esc_basic():
    assert _esc(None) == "—"
    assert _esc("hello") == "hello"
    assert _esc("4020.5") == "4020\\.5"
    assert _esc("MDT-XAU-20260724-001") == "MDT\\-XAU\\-20260724\\-001"
    # Every escapable char gets a backslash
    for ch in "_*[]()~`>#+-=|{}.!\\":
        out = _esc(ch)
        assert out == "\\" + ch, f"escape({ch!r}) = {out!r}"


def test_render_all_types():
    """Every message-type key produces a valid payload."""
    sig_map = {
        "monitoring":    _make_signal(state=STATE_MONITORING, confidence=68),
        "actionable":    _make_signal(state=STATE_ARMED, confidence=82),
        "entry_triggered": _make_signal(state=STATE_TRIGGERED, confidence=82),
        "tp1_hit":       _make_signal(state=STATE_TP1_HIT, confidence=82),
        "tp2_hit":       _make_signal(state=STATE_TP2_HIT, confidence=82),
        "final_target":  _make_signal(state=STATE_TP3_HIT, tp3=4040.0, confidence=82),
        "breakeven":     _make_signal(state=STATE_BREAKEVEN, confidence=82),
        "trailing":      _make_signal(state=STATE_TRAILING, confidence=82),
        "stop_hit":      _make_signal(state=STATE_STOPPED, confidence=82),
        "invalidated":   _make_signal(state=STATE_INVALIDATED, confidence=68),
        "expired":       _make_signal(state=STATE_EXPIRED, confidence=68),
        "high_confluence": _make_signal(state=STATE_ARMED, strategy=STRATEGY_AGGREGATED, confidence=91),
    }
    for mtype, sig in sig_map.items():
        extras = {
            "entry_triggered": {"fill_price": 4019.5, "mt5_ticket": "12345678"},
            "tp1_hit":         {"tp_price": 4025.0, "partial_closed_pct": 50, "moved_to_be": True},
            "tp2_hit":         {"tp_price": 4030.0, "partial_closed_pct": 30},
            "final_target":    {"tp_price": 4040.0, "total_r": 2.85},
            "trailing":        {"mfe": 45.2, "unrealized_r": 2.1},
            "stop_hit":        {"stop_price": 4014.0, "r_realized": -1.02, "mfe": 12.0, "stop_reason": "M15 bearish MSS"},
            "invalidated":     {"reason": "Close M15 below 4012.5", "trigger_price": 4011.8},
        }.get(mtype, {})

        payload = render(mtype, sig, extra=extras, mode=MODE_STANDARD)
        assert payload["parse_mode"] == "MarkdownV2"
        assert payload["message_type"] == mtype
        assert payload["message_fingerprint"] and len(payload["message_fingerprint"]) == 16
        assert 0 < payload["bytes"] <= TELEGRAM_MAX_BYTES
        errs = _check_mdv2(payload["text"])
        assert not errs, f"{mtype}: MDV2 errors: {errs[:3]}"


def test_all_three_modes_render():
    sig = _make_signal(state=STATE_ARMED, confidence=82)
    for m in (MODE_MINIMAL, MODE_STANDARD, MODE_DETAILED):
        p = render("actionable", sig, mode=m)
        assert p["bytes"] > 50, f"{m}: text too short: {p['text']!r}"
        errs = _check_mdv2(p["text"])
        assert not errs, f"{m}: MDV2 errors: {errs[:3]}"
    # Minimal < standard < detailed (roughly)
    b_min = render("actionable", sig, mode=MODE_MINIMAL)["bytes"]
    b_std = render("actionable", sig, mode=MODE_STANDARD)["bytes"]
    b_det = render("actionable", sig, mode=MODE_DETAILED)["bytes"]
    assert b_min <= b_std <= b_det, f"mode sizes not monotone: min={b_min} std={b_std} det={b_det}"


def test_fingerprint_idempotency():
    """Rendering same (signal, state) twice yields the same message_fingerprint."""
    sig = _make_signal(state=STATE_TP1_HIT, confidence=82)
    a = render("tp1_hit", sig, extra={"tp_price": 4025.0, "moved_to_be": True})
    b = render("tp1_hit", sig, extra={"tp_price": 4025.0, "moved_to_be": True})
    assert a["message_fingerprint"] == b["message_fingerprint"]


def test_fingerprint_price_drift_bucketed():
    """Small drift on TP price (< 5pt) should NOT change fingerprint."""
    sig = _make_signal(state=STATE_TP1_HIT)
    a = render("tp1_hit", sig, extra={"tp_price": 4025.0, "moved_to_be": True})
    b = render("tp1_hit", sig, extra={"tp_price": 4026.5, "moved_to_be": True})
    assert a["message_fingerprint"] == b["message_fingerprint"], (
        f"1.5pt drift changed fingerprint: {a['message_fingerprint']} vs {b['message_fingerprint']}"
    )


def test_dangerous_chars_in_content_escaped():
    """A signal whose invalidation text has MDV2 chars must not break the render."""
    now = datetime.now(timezone.utc)
    fp = signal_fingerprint(
        instrument="XAUUSD", direction=DIRECTION_SELL, strategy_id=STRATEGY_VP_TRAP,
        entry_zone_low=4020.0, entry_zone_high=4022.0, stop_loss=4026.0,
        session="NY KZ", created_at=now,
    )
    sig = CanonicalSignal(
        signal_id="VPT-XAU-20260724-099",
        fingerprint=fp, strategy_id=STRATEGY_VP_TRAP, strategy_name="VP Trap Reversal",
        instrument="XAUUSD", direction=DIRECTION_SELL, confidence=79,
        entry_zone_low=4020.0, entry_zone_high=4022.0,
        stop_loss=4026.0, current_stop=4026.0,
        invalidation="Close M15 > 4028.5 (dPOC 4023.2)",  # parens + dot + gt-sign
        session="NY KZ", state=STATE_ARMED,
        rationale="Bear trap at VAH — POC = 4023.2 · target [PDL] · price *rejected*",  # brackets/asterisks
        conditions_met=("HTF bearish [confirmed]", "M15 MSS"),
        created_at=now,
    )
    p = render("actionable", sig, mode=MODE_DETAILED)
    errs = _check_mdv2(p["text"])
    assert not errs, f"MDV2 errors with dangerous content: {errs}"
    # Ensure the content survived through escaping (should find escaped `>` and `[`)
    assert "\\>" in p["text"], "expected escaped `>` in output"
    assert "\\[" in p["text"], "expected escaped `[` in output"


def test_truncation():
    """Rendering with obscenely long rationale must truncate to <= 4096 bytes."""
    sig = _make_signal(state=STATE_ARMED)
    long_ratio = "The market is very complicated. " * 500   # ~16000 chars
    from dataclasses import replace
    sig = replace(sig, rationale=long_ratio)
    p = render("actionable", sig, mode=MODE_DETAILED)
    assert p["bytes"] <= TELEGRAM_MAX_BYTES, f"exceeded max: {p['bytes']}"
    assert p["text"].endswith("…"), "expected ellipsis suffix on truncated text"


def test_eat_conversion():
    """Header time must reflect UTC + 3."""
    # Signal at UTC 12:00 → EAT 15:00
    fixed_utc = datetime(2026, 7, 24, 12, 0, 0, tzinfo=timezone.utc)
    sig = _make_signal(state=STATE_ARMED)
    from dataclasses import replace
    sig = replace(sig, created_at=fixed_utc)
    p = render("actionable", sig, mode=MODE_STANDARD, now=fixed_utc)
    # Colon is not an MDV2 special, so no escape needed.
    assert "15:00 EAT" in p["text"], f"expected 15:00 EAT in text; got: {p['text'][:300]}"


def test_none_optional_targets_render_ok():
    """A signal missing TP2/TP3 must still render cleanly (no crash)."""
    sig = _make_signal(state=STATE_ARMED, tp2=None, tp3=None)
    p = render("actionable", sig, mode=MODE_STANDARD)
    assert p["bytes"] > 0
    errs = _check_mdv2(p["text"])
    assert not errs, f"MDV2 errors: {errs}"


def test_sample_actionable_readable():
    """Print an actual rendered message so we can eyeball it."""
    sig = _make_signal(state=STATE_ARMED, confidence=82, tp3=4040.0)
    p = render("actionable", sig, mode=MODE_STANDARD)
    print("\n  sample ACTIONABLE (standard mode):")
    for line in p["text"].splitlines():
        print("  │ " + line)
    print(f"  · bytes={p['bytes']} fp={p['message_fingerprint']}")


def test_sample_high_confluence():
    sig = _make_signal(state=STATE_ARMED, strategy=STRATEGY_AGGREGATED, confidence=91, tp3=4040.0)
    from dataclasses import replace
    sig = replace(sig, strategy_name="Aggregated (3 strategies)")
    p = render("high_confluence", sig, mode=MODE_DETAILED)
    print("\n  sample HIGH_CONFLUENCE (detailed mode):")
    for line in p["text"].splitlines():
        print("  │ " + line)


# ── Main ─────────────────────────────────────────────────────────────────────

def main() -> int:
    print("=" * 78)
    print(" TELEGRAM NOTIFICATION P2 — TEMPLATE VALIDATION")
    print("=" * 78)

    _hr("1. MarkdownV2 escape primitives")
    _run("_esc handles None + escapable chars", test_esc_basic)

    _hr("2. All 12 renderers produce valid MDV2")
    _run("every message type renders with correct escape", test_render_all_types)

    _hr("3. All 3 modes render")
    _run("minimal / standard / detailed all valid", test_all_three_modes_render)

    _hr("4. Fingerprint idempotency + bucketing")
    _run("same input → same fingerprint", test_fingerprint_idempotency)
    _run("1.5pt drift ignored by bucketing", test_fingerprint_price_drift_bucketed)

    _hr("5. Content injection safety")
    _run("dangerous chars in signal content are escaped", test_dangerous_chars_in_content_escaped)

    _hr("6. Truncation to 4096 bytes")
    _run("long rationale → truncated with ellipsis", test_truncation)

    _hr("7. EAT conversion (UTC+3)")
    _run("12:00 UTC renders as 15:00 EAT", test_eat_conversion)

    _hr("8. Optional TP handling")
    _run("missing TP2/TP3 renders cleanly", test_none_optional_targets_render_ok)

    _hr("9. Eyeball samples")
    _run("actionable readable", test_sample_actionable_readable)
    _run("high_confluence readable", test_sample_high_confluence)

    print("\n" + "=" * 78)
    if PASSED == TOTAL:
        print(f" ALL PASS · {PASSED}/{TOTAL} · Templates ready for P3 (mandate adapter)")
        return 0
    print(f" FAIL · {PASSED}/{TOTAL}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
