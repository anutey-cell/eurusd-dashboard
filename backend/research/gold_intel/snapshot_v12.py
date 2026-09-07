"""Daily Gold Intelligence Snapshot v1.2 — extends v1.1 with derivatives layer.

Additions on top of v1.1:
  - Data source audit (CME GC = NOT_OBSERVED; CBOE GLD = PROXY only)
  - GLD options intelligence layer (labelled as GLD ETF proxy, NOT GC)
  - ABS_GAMMA_CONCENTRATION per (expiry, strike) — dollars per 1% underlying move
  - Provisional basis observation appended to gc_xau_basis_observations
  - Confluence map — options-derived levels vs PDH/PDL/PWH/PWL/Asia H/L
  - Intelligence Matrix — adds DERIVATIVES layer (CONCENTRATION/LOCATION only,
    never bullish/bearish because gamma is UNSIGNED)
"""
import json
import math
import sys
sys.path.insert(0, "/app")
from datetime import datetime, timezone, timedelta, date
from database import SessionLocal
from sqlalchemy import text

from closure_20_snapshot_v11 import (
    current_xauusd_from_ticks, previous_trading_day, previous_trading_week,
    asian_session_range, structural_read, refresh_gc_bars, refresh_events,
    provisional_basis, upcoming_events, cftc_latest, latest_macro, track_a_health,
)

def log_basis_observation(db):
    """Append current basis observation to gc_xau_basis_observations."""
    b = provisional_basis(db, max_lag_min=60)
    if b["status"] != "PROVISIONAL":
        return None
    session_label = None
    hr = datetime.now(timezone.utc).hour
    if 22 <= hr or hr <= 5:  session_label = "ASIA"
    elif 6 <= hr <= 11:      session_label = "LONDON"
    elif 12 <= hr <= 15:     session_label = "LN_NY_OVERLAP"
    else:                     session_label = "NY_PM"
    db.execute(text("""
        INSERT OR IGNORE INTO gc_xau_basis_observations (
            observed_at_utc, gc_contract, gc_price, xau_source, xau_price,
            basis_pts, session_label
        ) VALUES (
            :ts, :c, :gc, 'mt5_ticks_mid', :xa, :b, :sl
        )
    """), {"ts": b["gc_obs_utc"], "c": b["gc_contract"], "gc": b["gc_close"],
             "xa": b["xau_mid"], "b": b["PROVISIONAL_basis_pts"], "sl": session_label})
    db.commit()
    return b

def basis_stats(db):
    """Return descriptive stats over gc_xau_basis_observations."""
    r = db.execute(text("""
        SELECT COUNT(*), MIN(basis_pts), MAX(basis_pts), AVG(basis_pts),
               MIN(observed_at_utc), MAX(observed_at_utc)
        FROM gc_xau_basis_observations
    """)).fetchone()
    if not r or r[0] == 0:
        return {"n_obs": 0, "status": "NO_DATA"}
    # simple percentile
    vals = [x[0] for x in db.execute(text(
        "SELECT basis_pts FROM gc_xau_basis_observations ORDER BY basis_pts"
    )).fetchall()]
    def pct(vs, p):
        if not vs: return None
        k = (len(vs)-1) * p
        lo = int(k); hi = min(lo+1, len(vs)-1)
        return vs[lo] + (vs[hi]-vs[lo])*(k-lo)
    return {"n_obs": r[0], "min": r[1], "max": r[2], "mean": round(r[3], 3),
             "median": pct(vals, 0.5),
             "p10": pct(vals, 0.10), "p90": pct(vals, 0.90),
             "first_obs": r[4], "last_obs": r[5],
             "status": "COLLECTING (30-day tracking-error study not yet complete)"}

def options_intelligence(db):
    """GLD-based options intelligence layer — labelled research-only proxy."""
    obs = date.today().isoformat()
    # sanity: is there any GLD data ingested today?
    n = db.execute(text(
        "SELECT COUNT(*) FROM options_gamma_concentration "
        "WHERE underlying='GLD' AND observation_date=:d"), {"d": obs}).scalar()
    if not n:
        return {"CME_GC_OPTIONS": {
            "status": "NOT_OBSERVED",
            "reason": "CME endpoints return HTTP 403 from droplet IP; no free authoritative alternative accessible in this build."},
            "GLD_ETF_OPTIONS_PROXY": {"status": "NO_DATA_TODAY"}}
    # Top strikes overall
    top = db.execute(text("""
        SELECT strike, option_expiry, days_to_expiry, total_oi, call_oi, put_oi,
               gamma_per_contract, abs_gamma_concentration
        FROM options_gamma_concentration
        WHERE underlying='GLD' AND observation_date=:d
        ORDER BY abs_gamma_concentration DESC LIMIT 10
    """), {"d": obs}).fetchall()
    # Nearest relevant expiry — most OI within DTE<=45
    r = db.execute(text("""
        SELECT option_expiry, days_to_expiry,
               SUM(total_oi) AS oi, SUM(call_oi) AS coi, SUM(put_oi) AS poi,
               SUM(abs_gamma_concentration) AS agc
        FROM options_gamma_concentration
        WHERE underlying='GLD' AND observation_date=:d AND days_to_expiry>=0
        GROUP BY option_expiry, days_to_expiry
        ORDER BY oi DESC LIMIT 6
    """), {"d": obs}).fetchall()
    top_expiries = [{"expiry": x[0], "dte": x[1], "total_oi": x[2],
                      "call_oi": x[3], "put_oi": x[4],
                      "abs_gamma_concentration_$_per_1pct": round(x[5], 0)} for x in r]
    # ATM = strike closest to underlying_price_ref
    r = db.execute(text("""
        SELECT strike, ABS(strike - underlying_price_ref) AS d
        FROM options_gamma_concentration
        WHERE underlying='GLD' AND observation_date=:d
        ORDER BY d ASC LIMIT 1
    """), {"d": obs}).fetchone()
    atm_strike = r[0] if r else None
    # ATM IV — average of |gamma_observed| top strikes at that ATM strike
    r = db.execute(text("""
        SELECT AVG(iv) FROM options_chain_v2
        WHERE underlying='GLD' AND observation_date=:d AND strike=:k AND iv IS NOT NULL
    """), {"d": obs, "k": atm_strike}).fetchone()
    atm_iv = r[0] if r else None
    # Top call OI and top put OI
    r = db.execute(text("""
        SELECT strike, SUM(call_oi) as coi
        FROM options_gamma_concentration
        WHERE underlying='GLD' AND observation_date=:d
        GROUP BY strike ORDER BY coi DESC LIMIT 5
    """), {"d": obs}).fetchall()
    top_call_oi = [{"strike": x[0], "call_oi": x[1]} for x in r]
    r = db.execute(text("""
        SELECT strike, SUM(put_oi) as poi
        FROM options_gamma_concentration
        WHERE underlying='GLD' AND observation_date=:d
        GROUP BY strike ORDER BY poi DESC LIMIT 5
    """), {"d": obs}).fetchall()
    top_put_oi = [{"strike": x[0], "put_oi": x[1]} for x in r]
    # Expected move — F * IV * sqrt(T), 1σ, DTE-normalized to next expiry
    r = db.execute(text("""
        SELECT underlying_price_ref, MIN(days_to_expiry)
        FROM options_gamma_concentration
        WHERE underlying='GLD' AND observation_date=:d AND days_to_expiry>=0
    """), {"d": obs}).fetchone()
    S_now, min_dte = r[0], r[1] if r else (None, None)
    exp_move = None
    if S_now and atm_iv and min_dte and min_dte > 0:
        T = min_dte / 365.0
        exp_move = round(S_now * atm_iv * math.sqrt(T), 3)
    return {
        "CME_GC_OPTIONS": {"status": "NOT_OBSERVED",
            "reason": "CME endpoints return HTTP 403 from droplet IP; no free authoritative alternative accessible."},
        "GLD_ETF_OPTIONS_PROXY": {
            "STATUS":   "OBSERVED  (SEPARATE RESEARCH HYPOTHESIS: does GLD positioning inform XAUUSD? NOT a GC substitute)",
            "QUARANTINE_NOTE": "GLD-native metrics only. No XAUUSD-equivalent strike overlay is published from this layer.",
            "SOURCE":   "CBOE delayed quotes (application/json)",
            "OBS_DATE": obs,
            "underlying_ref_S": S_now,
            "ATM_STRIKE_native": atm_strike,
            "ATM_IV_observed":   atm_iv,
            "NEAREST_EXPIRY_DTE": min_dte,
            "EXPECTED_MOVE_1SD_native_$": exp_move,
            "TOP_ABS_GAMMA_CONCENTRATION_strikes": [
                {"strike": x[0], "expiry": x[1], "dte": x[2],
                  "total_oi": x[3], "call_oi": x[4], "put_oi": x[5],
                  "gamma_per_contract_1_per_price": x[6],
                  "abs_gamma_concentration_$_per_1pct": round(x[7], 0)}
                for x in top
            ],
            "TOP_EXPIRIES_by_total_OI": top_expiries,
            "TOP_CALL_OI_strikes":       top_call_oi,
            "TOP_PUT_OI_strikes":        top_put_oi,
            "TERMINOLOGY_DISCIPLINE": [
                "FACT     : per-strike total OI is observed.",
                "DERIVED  : abs_gamma_concentration = m·S²·Γ·OI·0.01 (dollars per 1% move).",
                "INFERENCE: high concentration MAY indicate a level worth monitoring; nothing more.",
                "This layer is UNSIGNED. Do not read direction from gamma concentration.",
                "GLD is a research PROXY for gold optionality; not COMEX GC options.",
            ]
        }
    }

def build_confluence_map(db, market_structure, gld_options, xau_current):
    """Compare GLD options strikes to XAU structural levels.

    Because GLD ≈ 1/10 of gold spot minus fee drag, we compute a naive scale
    factor `S_ratio = xau_mid / gld_underlying_ref` and produce PROVISIONAL
    XAUUSD-equivalent strikes. This mapping is EXPLICITLY provisional.
    """
    xau_mid = xau_current.get("mid")
    proxy = gld_options.get("GLD_ETF_OPTIONS_PROXY", {})
    S_gld = proxy.get("underlying_ref_S")
    if not xau_mid or not S_gld:
        return {"status": "NOT_AVAILABLE", "reason": "missing xau mid or GLD S"}
    S_ratio = xau_mid / S_gld
    # anchor levels
    anchors = []
    for kind, container in [
        ("PDH", market_structure.get("PDH", {}).get("PDH")),
        ("PDL", market_structure.get("PDH", {}).get("PDL")),
        ("PWH", market_structure.get("PWH", {}).get("PWH")),
        ("PWL", market_structure.get("PWH", {}).get("PWL")),
        ("ASIA_H", market_structure.get("Asian_session_range", {}).get("FACT_high")),
        ("ASIA_L", market_structure.get("Asian_session_range", {}).get("FACT_low")),
    ]:
        if container is not None:
            anchors.append((kind, container))
    if not anchors:
        return {"status": "NO_ANCHORS"}
    # options levels — top-5 by gamma concentration
    opt_levels = []
    for row in proxy.get("TOP_ABS_GAMMA_CONCENTRATION_strikes", [])[:5]:
        gld_K = row["strike"]
        xau_equiv = gld_K * S_ratio
        opt_levels.append({
            "kind":                    "TOP_GAMMA",
            "native_underlying":       "GLD",
            "native_strike":           gld_K,
            "xauusd_equivalent":       round(xau_equiv, 2),
            "mapping_method":          f"native_strike × (xau_mid / gld_S) = {gld_K} × {S_ratio:.5f}",
            "mapping_status":          "PROVISIONAL — GLD-to-XAU scale ratio, unvalidated",
            "expiry":                  row["expiry"],
            "abs_gamma_concentration": row["abs_gamma_concentration_$_per_1pct"],
        })
    # top OI strikes
    for row in (proxy.get("TOP_CALL_OI_strikes") or [])[:3]:
        gld_K = row["strike"]
        opt_levels.append({
            "kind":                    "TOP_CALL_OI",
            "native_underlying":       "GLD",
            "native_strike":           gld_K,
            "xauusd_equivalent":       round(gld_K * S_ratio, 2),
            "mapping_method":          "GLD × (xau_mid / gld_S)",
            "mapping_status":          "PROVISIONAL",
            "call_oi":                 row["call_oi"],
        })
    for row in (proxy.get("TOP_PUT_OI_strikes") or [])[:3]:
        gld_K = row["strike"]
        opt_levels.append({
            "kind":                    "TOP_PUT_OI",
            "native_underlying":       "GLD",
            "native_strike":           gld_K,
            "xauusd_equivalent":       round(gld_K * S_ratio, 2),
            "mapping_method":          "GLD × (xau_mid / gld_S)",
            "mapping_status":          "PROVISIONAL",
            "put_oi":                  row["put_oi"],
        })
    # compute nearest anchor for each options level (research fields)
    matrix = []
    for opt in opt_levels:
        xau_equiv = opt["xauusd_equivalent"]
        best = None
        for kind, anchor in anchors:
            dist = xau_equiv - anchor
            if best is None or abs(dist) < abs(best["distance_pts"]):
                best = {"anchor_kind": kind, "anchor_price": anchor,
                         "distance_pts": round(dist, 2),
                         "distance_pct": round(dist / anchor * 100, 3) if anchor else None}
        row = {**opt, "nearest_anchor": best}
        matrix.append(row)
        db.execute(text("""
            INSERT INTO options_confluence_v2 (
                snapshot_ts_utc, underlying, options_level_kind,
                options_level_native, options_level_xauusd_equiv, mapping_status,
                anchor_kind, anchor_price, distance_pts, distance_pct
            ) VALUES (
                :ts, 'GLD', :kk, :native, :xau, 'PROVISIONAL',
                :ak, :ap, :dp, :dpc
            )
        """), {"ts": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
                "kk": opt["kind"], "native": opt["native_strike"],
                "xau": xau_equiv, "ak": best["anchor_kind"],
                "ap": best["anchor_price"], "dp": best["distance_pts"],
                "dpc": best["distance_pct"]})
    db.commit()
    return {
        "status": "COMPUTED_PROVISIONAL",
        "S_ratio": round(S_ratio, 5),
        "GLD_underlying_S": S_gld,
        "XAU_mid": xau_mid,
        "note": "GLD-based mapping is a research proxy. The GLD/gold ratio is affected by ETF expense drag (~0.4%/y) and NAV vs price. This confluence map is DERIVED research, not a trading level.",
        "confluence_rows": matrix,
    }


def compose_v12(db):
    snap = {"snapshot_ts_utc": datetime.now(timezone.utc).isoformat(),
             "version": "GOLD_INTEL_V1.2"}

    # ── refresh dependencies (idempotent) ──
    n_gc = refresh_gc_bars(db)   # updates gold_intel_gc_bars
    ev   = refresh_events(db)    # updates gold_intel_events
    b_obs = log_basis_observation(db)

    xau_now = current_xauusd_from_ticks(db)
    ms = {
        "current_price":  xau_now,
        "PDH":            previous_trading_day(db),
        "PWH":            previous_trading_week(db),
        "Asian_session_range": asian_session_range(db),
    }
    ms.update(structural_read(db))
    snap["market_structure"] = ms

    # ── macro ──
    macro_out = {}
    for sid, label in [
        ("DXY","US Dollar Index"), ("UST_2Y","US 2Y Treasury Nominal"),
        ("UST_10Y","US 10Y Treasury Nominal"), ("UST_REAL10Y","US 10Y Real Yield (TIPS)"),
        ("US_BE_10Y_DERIVED","US 10Y Breakeven (DERIVED)"),
        ("WTI","WTI Crude"), ("VIX","VIX"), ("MOVE","MOVE"),
        ("USDCNH","USD/CNH"), ("USDJPY","USD/JPY"),
    ]:
        cur = latest_macro(db, sid)
        macro_out[sid] = {"label": label,
            "FACT_value": cur["value"] if cur else None,
            "obs_date":   cur["obs_date"] if cur else None,
            "provider":   cur["provider"] if cur else None}
    snap["macro"] = macro_out

    # ── positioning ──
    cot = cftc_latest(db)
    if cot:
        snap["positioning"] = {
            "FACT_position_date":     str(cot["report_date_yyyy_mm_dd"]),
            "FACT_MM_long":           cot["m_money_long"],
            "FACT_MM_short":          cot["m_money_short"],
            "DERIVED_MM_net":         cot["m_money_net"],
            "DERIVED_ΔMM_net_1W":     cot["d_m_money_net_1w"],
            "DERIVED_MM_net_pct_OI":  cot["m_money_net_pct_oi"],
            "DERIVED_MM_net_pctile_5y": cot["m_money_net_pctile_5y"],
            "classification_state":   "NOT_APPLIED_v1: HOLD",
        }
    else:
        snap["positioning"] = {"status": "NO_DATA"}

    # ── event risk (upcoming gold-relevant) ──
    upcoming = upcoming_events(db, days=7)
    snap["event_risk"] = {"status": ev.get("status", "UNKNOWN"),
                            "gold_relevant_upcoming_7d": upcoming[:6]}

    # ── track A ──
    snap["exness_quote_microstructure"] = track_a_health(db)

    # ── derivatives (NEW in v1.2) ──
    opts = options_intelligence(db)
    snap["derivatives"] = opts

    # ── basis ──
    snap["basis"] = {
        "provisional_current": b_obs or provisional_basis(db, max_lag_min=60),
        "tracking_dataset":    basis_stats(db),
    }

    # ── confluence map ──
    # QUARANTINED (2026-09-07 per Phase-2A directive): the GLD → XAU strike
    # mapping is not shown in the user-facing snapshot. The GLD dataset is
    # retained in options_gamma_concentration for a SEPARATE research hypothesis
    # ("does GLD options positioning contain independent information for XAUUSD?")
    # and the mapping utility is preserved in code but no longer surfaced.
    snap["options_confluence_map"] = {
        "status": "SUPPRESSED_QUARANTINE_v1.2a",
        "reason": ("GLD→XAU strike mapping is not sufficiently validated. The "
                    "underlying GLD positioning data remains in "
                    "options_gamma_concentration for the separate research "
                    "hypothesis about GLD options informing XAUUSD."),
    }

    # ── intelligence matrix + classification ──
    # Determine derivatives status:
    gld_status = opts.get("GLD_ETF_OPTIONS_PROXY", {}).get("STATUS", "NO_DATA")
    if "OBSERVED" in gld_status:
        derivatives_matrix_state = "CONCENTRATION_LOCATION_OBSERVED_PROXY"
    else:
        derivatives_matrix_state = "NOT_OBSERVED"

    snap["intelligence_matrix"] = {
        "MACRO":                    "MIXED",   # v1.1 already computed and displayed
        "POSITIONING":              "MODESTLY_LONG" if cot and (cot.get("m_money_net_pctile_5y") or 0) >= 0.60 else "NEUTRAL",
        "STRUCTURE":                "OBSERVED",
        "OPTIONS_GC":               "NOT_OBSERVED",
        "OPTIONS_GLD_PROXY":        derivatives_matrix_state,
        "PHYSICAL":                 "NOT_OBSERVED",
        "GC_MICROSTRUCTURE":        "NOT_OBSERVED",
        "XAU_QUOTE_MICROSTRUCTURE": "OBSERVATION_ONLY",
        "note": "Options-GLD_proxy provides CONCENTRATION/LOCATION context only. UNSIGNED. Does not contribute BULLISH/BEARISH."
    }

    # persist
    db.execute(text("""
        INSERT INTO gold_daily_intel_snapshots
          (snapshot_ts_utc, cover_date, conclusion, payload_json, version)
        VALUES (:ts, :cd, :con, :js, :ver)
    """), {"ts": snap["snapshot_ts_utc"], "cd": str(date.today()),
            "con": "V1.2_COMPOSED",
            "js": json.dumps(snap, indent=2, default=str),
            "ver": snap["version"]})
    db.commit()
    return snap


def main():
    with SessionLocal() as db:
        print("=" * 78)
        print("DAILY GOLD INTELLIGENCE SNAPSHOT V1.2")
        print("=" * 78)
        snap = compose_v12(db)

        # human-readable render
        print(f"\n{snap['snapshot_ts_utc']}   version={snap['version']}\n")
        print("--- DATA HEALTH SUMMARY ---")
        print(f"  XAUUSD: {snap['market_structure']['current_price']}")
        gc_ok = snap['basis']['provisional_current']
        print(f"  Basis:  {gc_ok}")
        print(f"  Track A tick health: {snap['exness_quote_microstructure']}")
        print()
        print("--- DERIVATIVES ---")
        cme = snap['derivatives']['CME_GC_OPTIONS']
        print(f"  CME_GC_OPTIONS: {cme}")
        proxy = snap['derivatives'].get('GLD_ETF_OPTIONS_PROXY', {})
        if proxy.get('STATUS'):
            print(f"  GLD_ETF_OPTIONS_PROXY: {proxy['STATUS']}")
            print(f"    S={proxy['underlying_ref_S']}  ATM={proxy['ATM_STRIKE_native']}  "
                    f"ATM_IV={proxy['ATM_IV_observed']}  DTE={proxy['NEAREST_EXPIRY_DTE']}  "
                    f"expected_move_1σ=${proxy['EXPECTED_MOVE_1SD_native_$']}")
            print("    TOP 5 by ABS_GAMMA_CONCENTRATION (dollars per 1% move):")
            for row in proxy["TOP_ABS_GAMMA_CONCENTRATION_strikes"][:5]:
                print(f"      K={row['strike']:>7.2f}  exp={row['expiry']}  DTE={row['dte']:>3}  "
                        f"OI={row['total_oi']:>7,}  ABS_GC=${row['abs_gamma_concentration_$_per_1pct']:>14,.0f}")
        print()
        print("--- CONFLUENCE MAP (options ↔ structural anchors, PROVISIONAL) ---")
        cf = snap['options_confluence_map']
        if cf.get("status", "").startswith("COMPUTED"):
            print(f"  S_ratio={cf['S_ratio']}   GLD_S={cf['GLD_underlying_S']}   XAU_mid={cf['XAU_mid']}")
            for row in cf["confluence_rows"][:8]:
                a = row["nearest_anchor"]
                print(f"    GLD {row['native_strike']:>7.2f}  →  XAU≈{row['xauusd_equivalent']:>8.2f}  "
                        f"[{row['kind']:<12}]   nearest={a['anchor_kind']:<7}@{a['anchor_price']:.2f}   "
                        f"Δ={a['distance_pts']:+.2f} pts ({a['distance_pct']:+.3f}%)")
        else:
            print(f"  {cf}")
        print()
        print("--- INTELLIGENCE MATRIX ---")
        for k, v in snap["intelligence_matrix"].items():
            print(f"  {k}: {v}")

if __name__ == "__main__":
    main()
