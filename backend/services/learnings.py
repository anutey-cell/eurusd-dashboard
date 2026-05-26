"""
Strategist learnings aggregator
===============================

Turns the append-only `strategist_verdicts` log (every verdict the engine
produced) into actionable lessons. Closed trades feed:

  • Overall WR, expectancy R, sample size
  • Breakdown by conditions_passed (3 / 4 / 5)  — validates the mandate's
    78-85% / 70-80% / 58-68% claims
  • Breakdown by killzone × direction
  • Breakdown by direction_source (scanner / predictor / strategist_htf)
  • Breakdown by sweep side (high / low / none)
  • Breakdown by macro alignment (aligned / neutral / conflicted)
  • Top 3 winning configurations
  • Top 3 losing configurations
  • Calibration notes (where reality diverges from theory)

Pure read function — never mutates state.
"""
from __future__ import annotations

import json
import logging
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from statistics import mean
from typing import Any

from sqlalchemy.orm import Session

from db_models import StrategistVerdict

log = logging.getLogger(__name__)

# Mandate's predicted WR ranges per conditions_passed band
_MANDATE_WR_PREDICTION = {
    5: (78, 85),
    4: (70, 80),
    3: (58, 68),
}


def _wr_pct(wins: int, total: int) -> float:
    return round(100.0 * wins / total, 1) if total else 0.0


def _r_multiple(row: StrategistVerdict) -> float | None:
    """Compute realised R-multiple from pips_outcome / |entry - SL|."""
    if (row.pips_outcome is None or row.entry is None
            or row.stop_loss is None):
        return None
    risk_pts = abs(row.entry - row.stop_loss)
    if risk_pts <= 0:
        return None
    return round(row.pips_outcome / risk_pts, 3)


def _bucket_stats(rows: list[StrategistVerdict]) -> dict:
    """Common per-bucket metrics: N, wins, WR%, expectancy R, avg MFE/MAE."""
    if not rows:
        return {"n": 0, "wins": 0, "losses": 0, "be": 0, "wr_pct": 0.0,
                "expectancy_r": 0.0, "avg_mfe": 0.0, "avg_mae": 0.0}
    wins   = sum(1 for r in rows if r.result == "WIN")
    losses = sum(1 for r in rows if r.result == "LOSS")
    be     = sum(1 for r in rows if r.result == "BREAKEVEN")
    rs     = [_r_multiple(r) for r in rows]
    rs     = [r for r in rs if r is not None]
    mfes   = [r.mfe_pts for r in rows if r.mfe_pts is not None]
    maes   = [r.mae_pts for r in rows if r.mae_pts is not None]
    return {
        "n":            len(rows),
        "wins":         wins,
        "losses":       losses,
        "be":           be,
        "wr_pct":       _wr_pct(wins, len(rows)),
        "expectancy_r": round(mean(rs), 3) if rs else 0.0,
        "avg_mfe":      round(mean(mfes), 2) if mfes else 0.0,
        "avg_mae":      round(mean(maes), 2) if maes else 0.0,
    }


def _extract_meta(row: StrategistVerdict) -> dict:
    """Pull diagnostic fields out of the persisted full_verdict_json."""
    try:
        v = json.loads(row.full_verdict_json or "{}")
    except Exception:
        v = {}
    diag = v.get("diagnostics", {}) or {}
    lm   = v.get("liquidity_model", {}) or {}
    return {
        "direction_source": diag.get("direction_source") or "scanner",
        "plan_source":      diag.get("plan_source") or "scanner",
        "sweep_side":       lm.get("sweep_side"),       # "high" | "low" | None
        "ict_score":        _extract_ict_score(v),
        "killzone":         _extract_killzone(v),
    }


def _extract_ict_score(v: dict) -> int | None:
    """Best-effort: ICT score lives in technical_confirmation or institutional_logic."""
    txt = v.get("institutional_logic", "") or ""
    # Look for "ict(72/100" pattern
    import re
    m = re.search(r"ict\((\d+)/100", txt)
    if m:
        try: return int(m.group(1))
        except ValueError: pass
    return None


def _extract_killzone(v: dict) -> str:
    """Map session_classification → killzone family."""
    s = (v.get("session_classification") or "").lower()
    if "london open" in s: return "london_open"
    if "london"       in s: return "london"
    if "new york open" in s or "ny open" in s: return "ny_open"
    if "overlap"      in s: return "overlap"
    if "asian"        in s: return "asian"
    if "late"         in s: return "late"
    return "other"


def build_learnings(db: Session, *, window_days: int = 7) -> dict:
    """
    Aggregate closed strategist trades into the structured lesson dict.
    Returns a JSON-safe payload ready for both the API endpoint and the
    weekly Telegram digest.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(days=window_days)

    # Only closed-outcome rows count for learning (PENDING / null don't)
    closed = (
        db.query(StrategistVerdict)
          .filter(StrategistVerdict.created_at >= cutoff)
          .filter(StrategistVerdict.result.in_(("WIN", "LOSS", "BREAKEVEN")))
          .all()
    )

    if not closed:
        return {
            "window_days": window_days,
            "sample_size": 0,
            "headline":    "No closed trades in window yet — keep collecting data.",
            "overall":     _bucket_stats([]),
            "by_conditions_passed": {},
            "by_kz_direction":      {},
            "by_direction_source":  {},
            "by_sweep_side":        {},
            "by_macro_alignment":   {},
            "top_winners":          [],
            "top_losers":           [],
            "calibration_notes":    [],
        }

    overall = _bucket_stats(closed)

    # ── Bucket: by conditions_passed (the key mandate validator) ────────
    by_cp = defaultdict(list)
    for r in closed:
        by_cp[r.conditions_passed].append(r)
    by_conditions = {
        f"{cp}/5": {**_bucket_stats(rs),
                    "mandate_predicted_wr": (
                        f"{_MANDATE_WR_PREDICTION[cp][0]}-{_MANDATE_WR_PREDICTION[cp][1]}%"
                        if cp in _MANDATE_WR_PREDICTION else "n/a"
                    )}
        for cp, rs in sorted(by_cp.items(), reverse=True)
    }

    # ── Bucket: killzone × direction ────────────────────────────────────
    by_kz = defaultdict(list)
    for r in closed:
        meta = _extract_meta(r)
        key = f"{meta['killzone']}_{r.decision}"
        by_kz[key].append(r)
    by_kz_direction = {k: _bucket_stats(rs)
                       for k, rs in sorted(by_kz.items(),
                                           key=lambda x: -len(x[1]))}

    # ── Bucket: direction_source ────────────────────────────────────────
    by_dir_src = defaultdict(list)
    for r in closed:
        by_dir_src[_extract_meta(r)["direction_source"]].append(r)
    by_direction_source = {k: _bucket_stats(rs)
                           for k, rs in sorted(by_dir_src.items())}

    # ── Bucket: sweep side ──────────────────────────────────────────────
    by_sweep = defaultdict(list)
    for r in closed:
        s = _extract_meta(r)["sweep_side"]
        by_sweep[s or "none"].append(r)
    by_sweep_side = {k: _bucket_stats(rs)
                     for k, rs in sorted(by_sweep.items())}

    # ── Bucket: macro alignment ─────────────────────────────────────────
    # macro_alignment lives in full_verdict_json.macro_context.macro_alignment
    by_macro = defaultdict(list)
    for r in closed:
        try:
            v = json.loads(r.full_verdict_json or "{}")
            ma = (v.get("macro_context") or {}).get("macro_alignment") or "Neutral"
        except Exception:
            ma = "Unknown"
        by_macro[ma].append(r)
    by_macro_alignment = {k: _bucket_stats(rs) for k, rs in sorted(by_macro.items())}

    # ── Top winners + losers (by R-multiple) ────────────────────────────
    ranked = [(_r_multiple(r), r) for r in closed]
    ranked = [(r_mul, r) for r_mul, r in ranked if r_mul is not None]
    ranked.sort(key=lambda x: x[0])

    def _summarise(r: StrategistVerdict) -> dict:
        return {
            "id":               r.id,
            "createdAt":        r.created_at.isoformat() if r.created_at else None,
            "decision":         r.decision,
            "conditionsPassed": r.conditions_passed,
            "result":           r.result,
            "rMultiple":        _r_multiple(r),
            "pipsOutcome":      r.pips_outcome,
            "mfePts":           r.mfe_pts,
            "maePts":           r.mae_pts,
            "session":          r.session_classification,
            "tfAlignment":      r.tf_alignment_label,
            "improvementNote":  r.improvement_note,
        }
    top_losers  = [_summarise(r) for _, r in ranked[:3]]
    top_winners = [_summarise(r) for _, r in reversed(ranked[-3:])]

    # ── Calibration notes — where reality diverges from theory ─────────
    notes: list[str] = []
    for cp_band, stats in by_conditions.items():
        cp_int = int(cp_band.split("/")[0])
        if stats["n"] < 5:
            notes.append(f"{cp_band}: only {stats['n']} closed trades — sample too small to draw conclusions.")
            continue
        actual_wr = stats["wr_pct"]
        if cp_int in _MANDATE_WR_PREDICTION:
            lo, hi = _MANDATE_WR_PREDICTION[cp_int]
            if actual_wr < lo - 10:
                notes.append(
                    f"{cp_band}: actual WR {actual_wr}% is significantly below mandate's "
                    f"{lo}-{hi}% prediction. Consider tightening confluence."
                )
            elif actual_wr > hi + 5:
                notes.append(
                    f"{cp_band}: actual WR {actual_wr}% exceeds mandate's "
                    f"{lo}-{hi}% prediction. The gate may be too strict — could loosen."
                )
            else:
                notes.append(
                    f"{cp_band}: actual WR {actual_wr}% within mandate band {lo}-{hi}%. Gate calibrated."
                )

    # Killzone-edge notes
    for key, stats in by_kz_direction.items():
        if stats["n"] >= 5 and stats["expectancy_r"] < -0.3:
            notes.append(
                f"{key}: {stats['n']} trades, WR {stats['wr_pct']}%, "
                f"expectancy {stats['expectancy_r']:+.2f}R — negative edge. Block in policy."
            )

    # ── Headline ────────────────────────────────────────────────────────
    headline = (
        f"{overall['n']} closed trades · WR {overall['wr_pct']}% · "
        f"expectancy {overall['expectancy_r']:+.2f}R"
    )

    return {
        "window_days":          window_days,
        "sample_size":          overall["n"],
        "headline":             headline,
        "overall":              overall,
        "by_conditions_passed": by_conditions,
        "by_kz_direction":      by_kz_direction,
        "by_direction_source":  by_direction_source,
        "by_sweep_side":        by_sweep_side,
        "by_macro_alignment":   by_macro_alignment,
        "top_winners":          top_winners,
        "top_losers":           top_losers,
        "calibration_notes":    notes,
    }


# ────────────────────────────────────────────────────────────────────────
# Telegram weekly-digest formatter
# ────────────────────────────────────────────────────────────────────────

def format_weekly_digest(learnings: dict) -> str:
    """
    Compose the structured weekly Telegram digest. Plain text + emojis to
    match the mandate signal/briefing style. Skips empty buckets so the
    message stays readable when sample sizes are small.
    """
    n = learnings["sample_size"]
    overall = learnings["overall"]
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M GMT")

    if n == 0:
        return (
            f"📊 XAUUSD WEEKLY LEARNINGS\n"
            f"━━━━━━━━━━━━━━━━━━━━━\n"
            f"📅 {ts}\n"
            f"No closed trades in last {learnings['window_days']} days.\n"
            f"Keep the engine running — verdicts are still being logged.\n"
            f"━━━━━━━━━━━━━━━━━━━━━"
        )

    lines = [
        f"📊 XAUUSD WEEKLY LEARNINGS",
        f"━━━━━━━━━━━━━━━━━━━━━",
        f"📅 {ts}  ·  last {learnings['window_days']} days",
        f"",
        f"🎯 OVERALL",
        f"  Trades:     {overall['n']}  ({overall['wins']}W / {overall['losses']}L / {overall['be']}BE)",
        f"  Win rate:   {overall['wr_pct']}%",
        f"  Expectancy: {overall['expectancy_r']:+.2f}R",
        f"  Avg MFE:    +{overall['avg_mfe']} pts   Avg MAE: -{overall['avg_mae']} pts",
    ]

    # By conditions passed
    bc = learnings.get("by_conditions_passed") or {}
    if bc:
        lines += ["", "📐 BY CONDITIONS PASSED"]
        for band, st in bc.items():
            pred = st.get("mandate_predicted_wr", "n/a")
            lines.append(
                f"  {band}: {st['n']:>3} trades · WR {st['wr_pct']:>5.1f}% "
                f"(mandate: {pred}) · exp {st['expectancy_r']:+.2f}R"
            )

    # Killzone × direction
    bkz = learnings.get("by_kz_direction") or {}
    if bkz:
        lines += ["", "🕐 BY KILLZONE × DIRECTION"]
        for key, st in list(bkz.items())[:6]:
            lines.append(
                f"  {key:<22} {st['n']:>2}t · WR {st['wr_pct']:>5.1f}% · exp {st['expectancy_r']:+.2f}R"
            )

    # Top winners
    winners = learnings.get("top_winners") or []
    if winners:
        lines += ["", "🏆 TOP WINNERS"]
        for w in winners:
            lines.append(
                f"  #{w['id']} {w['decision']:<4} {w['conditionsPassed']}/5 → {w['result']:<3} "
                f"{w['rMultiple']:+.2f}R  ({w['session']})"
            )

    # Top losers
    losers = learnings.get("top_losers") or []
    if losers:
        lines += ["", "📉 TOP LOSERS"]
        for l in losers:
            lines.append(
                f"  #{l['id']} {l['decision']:<4} {l['conditionsPassed']}/5 → {l['result']:<3} "
                f"{l['rMultiple']:+.2f}R  ({l['session']})"
            )

    # Calibration notes
    notes = learnings.get("calibration_notes") or []
    if notes:
        lines += ["", "🔍 CALIBRATION NOTES"]
        for note in notes:
            lines.append(f"  • {note}")

    lines += ["", "━━━━━━━━━━━━━━━━━━━━━",
              "Capital preservation > revenge. Demo only · 0.01 lot."]
    return "\n".join(lines)
