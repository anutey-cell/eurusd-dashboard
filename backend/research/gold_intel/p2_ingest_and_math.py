"""Phase-2 core — gamma unit validation + CBOE GLD ingest + concentration.

DISCIPLINE
  - CME GC options: NOT_OBSERVED  (403 from droplet IP; recorded in options_source_audit)
  - CBOE GLD options: labelled research-only ETF proxy; NOT presented as GC data.
  - No trades; no production coupling.
"""
import gzip
import io
import json
import math
import urllib.request
from datetime import datetime, timezone, timedelta, date
from database import SessionLocal
from sqlalchemy import text

# ─── Section A. Source audit ─────────────────────────────────────────────
CME_ATTEMPTS = [
    ("CME quotes GC options JSON",         "https://www.cmegroup.com/CmeWS/mvc/Quotes/Option/437/G/OOF"),
    ("CME OOF settlement latest",          "https://www.cmegroup.com/CmeWS/mvc/Settlements/Options/Settlements/437/OOF"),
    ("CME ProductCalendar 437",            "https://www.cmegroup.com/CmeWS/mvc/ProductCalendar/Options/437"),
    ("CME MetalsBulletin (HTML)",          "https://www.cmegroup.com/markets/metals/precious/gold.settlements.html"),
    ("CME QuickStrike calendar",           "https://www.cmegroup.com/tools-information/quikstrike/options-calendar-gold.html"),
]
CBOE_URL = "https://cdn.cboe.com/api/global/delayed_quotes/options/GLD.json"

def http_get(url, timeout=25):
    req = urllib.request.Request(url, headers={
        "User-Agent":"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Accept":"application/json, text/html",
        "Accept-Encoding":"gzip",
        "Accept-Language":"en-US,en;q=0.9",
    })
    with urllib.request.urlopen(req, timeout=timeout) as r:
        body = r.read()
        if "gzip" in r.headers.get("Content-Encoding", ""):
            body = gzip.decompress(body)
        return body, r.status, r.headers.get("Content-Type","")

def audit_sources(db):
    for name, url in CME_ATTEMPTS:
        status = 0; reach = 0; notes = ""
        try:
            body, code, ctype = http_get(url, timeout=15)
            status = code; reach = 1; notes = f"len={len(body)} ctype={ctype}"
        except Exception as e:
            notes = f"{type(e).__name__}: {e}"
            try: status = int(str(e).split(":")[0].split()[-1])
            except Exception: status = 0
        db.execute(text("""
            INSERT INTO options_source_audit (source_name, url, access_method,
                reachable, http_status, notes)
            VALUES (:n, :u, 'HTTPS_JSON', :r, :s, :d)
        """), {"n": name, "u": url, "r": reach, "s": status, "d": notes})
    # CBOE probe
    try:
        body, code, ctype = http_get(CBOE_URL)
        db.execute(text("""
            INSERT INTO options_source_audit (source_name, url, access_method,
                reachable, http_status, notes)
            VALUES ('CBOE GLD delayed quotes', :u, 'HTTPS_JSON', 1, :s, :d)
        """), {"u": CBOE_URL, "s": code, "d": f"len={len(body)} ctype={ctype}"})
    except Exception as e:
        db.execute(text("""
            INSERT INTO options_source_audit (source_name, url, access_method,
                reachable, http_status, notes)
            VALUES ('CBOE GLD delayed quotes', :u, 'HTTPS_JSON', 0, 0, :d)
        """), {"u": CBOE_URL, "d": str(e)})
    db.commit()


# ─── Section B. Gamma unit validation (Black-76 for futures options) ─────
def norm_cdf(x):
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))

def norm_pdf(x):
    return math.exp(-0.5 * x * x) / math.sqrt(2 * math.pi)

def b76(F, K, T, r, sigma, cp):
    """Black-76 for options on FUTURES. Returns (price, delta, gamma)."""
    if sigma <= 0 or T <= 0 or F <= 0 or K <= 0:
        return 0.0, 0.0, 0.0
    sq = math.sqrt(T)
    d1 = (math.log(F/K) + 0.5 * sigma * sigma * T) / (sigma * sq)
    d2 = d1 - sigma * sq
    disc = math.exp(-r * T)
    if cp.upper() == "C":
        price = disc * (F * norm_cdf(d1) - K * norm_cdf(d2))
        delta = disc * norm_cdf(d1)
    else:
        price = disc * (K * norm_cdf(-d2) - F * norm_cdf(-d1))
        delta = disc * (norm_cdf(d1) - 1.0)
    gamma = disc * norm_pdf(d1) / (F * sigma * sq)
    return price, delta, gamma

def bs76_equity(S, K, T, r, q, sigma, cp):
    """Black-Scholes for equity option paying continuous dividend q."""
    if sigma <= 0 or T <= 0 or S <= 0 or K <= 0:
        return 0.0, 0.0, 0.0
    sq = math.sqrt(T)
    d1 = (math.log(S/K) + (r - q + 0.5 * sigma * sigma) * T) / (sigma * sq)
    d2 = d1 - sigma * sq
    disc_r = math.exp(-r * T)
    disc_q = math.exp(-q * T)
    if cp.upper() == "C":
        price = S * disc_q * norm_cdf(d1) - K * disc_r * norm_cdf(d2)
        delta = disc_q * norm_cdf(d1)
    else:
        price = K * disc_r * norm_cdf(-d2) - S * disc_q * norm_cdf(-d1)
        delta = -disc_q * norm_cdf(-d1)
    gamma = disc_q * norm_pdf(d1) / (S * sigma * sq)
    return price, delta, gamma

def gamma_unit_validation():
    """One-contract worked example — analytical vs finite difference.

    Metric derivation (from first principles):
      ONE GC call, F=4400, K=4400, T=30d, r=4.5%, σ=20%.
      Contract multiplier m = 100 (troy oz).

      Analytical Γ (Black-76):
        Γ_an = e^(-rT) φ(d1) / (F σ √T)             units: 1/price
      Position gamma per contract in NOTIONAL terms:
        Γ_pos = m · Γ_an                            units: oz / price
                                                     (i.e. change in delta-in-oz
                                                     per $1 futures move)
      Dollar-gamma per contract (per $1 move):
        DG_$1 = m · F · Γ_an                        units: $ per $1 move
      Dollar-gamma per contract (per 1% move):
        DG_1pct = m · F² · Γ_an · 0.01              units: $ per 1% move
                = 0.01 × F × DG_$1

      Numerical check via central finite difference:
        Δ_an(F ± h) → recompute delta; Γ_num = (Δ(F+h) - Δ(F-h)) / (2h)
      For h=$1, Γ_num ≈ Γ_an to O(h²).
    """
    print("=" * 72)
    print("GAMMA UNIT VALIDATION — Black-76 on GC futures options")
    print("=" * 72)
    F, K, T, r, sig = 4400.0, 4400.0, 30/365.0, 0.045, 0.20
    m = 100    # GC contract multiplier (troy oz)

    price_c, delta_c, gamma_an = b76(F, K, T, r, sig, "C")
    print(f"F={F}   K={K}   T={T:.6f}y   r={r}   σ={sig}   m={m}")
    print(f"  Call price:                         {price_c:.6f}")
    print(f"  Call delta:                         {delta_c:.7f}   (dimensionless)")
    print(f"  Analytical Γ:                       {gamma_an:.9f}   (1/price)")
    print()

    # Position gamma
    gamma_oz = m * gamma_an
    dg_per_1 = m * F * gamma_an
    dg_per_pct = m * F * F * gamma_an * 0.01
    print(f"  Contract delta (oz):                {m * delta_c:.4f} oz")
    print(f"  Contract dollar delta:              ${m * F * delta_c:,.2f}")
    print(f"  Position Γ (oz per $1):             {gamma_oz:.6f} oz/$")
    print(f"  Position Γ dollar (per $1 move):    ${dg_per_1:,.4f}")
    print(f"  Position Γ dollar (per 1% move):    ${dg_per_pct:,.4f}")
    print()

    # Numerical Γ via central finite difference on delta at F±h
    for h in (0.5, 1.0, 5.0, 44.0):     # 44.0 ≈ 1% of F
        _, d_up, _ = b76(F + h, K, T, r, sig, "C")
        _, d_dn, _ = b76(F - h, K, T, r, sig, "C")
        gamma_num = (d_up - d_dn) / (2*h)
        err = gamma_num - gamma_an
        rel = err / gamma_an if gamma_an else 0
        print(f"  Numerical Γ (h=${h:>4.1f}): {gamma_num:.9f}   "
                f"err={err:+.3e}   rel_err={rel:+.5%}")
    print()

    # Change in delta from $1 move -- confirms dg_per_1 interpretation
    _, d_up, _ = b76(F + 1.0, K, T, r, sig, "C")
    _, d_dn, _ = b76(F - 1.0, K, T, r, sig, "C")
    d_delta = d_up - delta_c
    print(f"  Δdelta after +$1 futures move:      {d_delta:+.7f}")
    print(f"  ≈ analytical Γ × $1:                {gamma_an * 1.0:+.7f}   "
            f"|diff|={abs(d_delta - gamma_an):.3e}")

    # Same at 1% move
    _, d_up1p, _ = b76(F * 1.01, K, T, r, sig, "C")
    d_delta1p = d_up1p - delta_c
    print(f"  Δdelta after +1% futures move:      {d_delta1p:+.7f}")
    print(f"  ≈ Γ × 0.01·F  =  {gamma_an * 0.01 * F:+.7f}   "
            f"|diff|={abs(d_delta1p - gamma_an * 0.01 * F):.3e}")
    return {"analytical_gamma": gamma_an, "dg_per_1pct": dg_per_pct,
             "F": F, "K": K, "T": T}


# ─── Section C. Ingest CBOE GLD options ──────────────────────────────────
GLD_SYMBOL_RX_HELP = """
GLD OCC option symbol format:
  Root(1..6 chars) + YY(2) + MM(2) + DD(2) + Call/Put(1) + Strike(8 = int of price*1000, left-padded)
Examples:
  GLD250815C00250000  → GLD, expiry 2025-08-15, C, strike = 250000 / 1000 = 250.0
"""
def parse_occ(sym):
    """Return (root, expiry_date, cp, strike)."""
    if not sym: return None
    s = sym.strip()
    # Root ends before 6-digit YYMMDD -- find last block of DDMMYY + C/P + 8 digits
    # Robust parse:
    if len(s) < 15: return None
    tail = s[-15:]      # YYMMDDCstrikeXXXXX  (15 chars)
    yy = tail[0:2]; mm = tail[2:4]; dd = tail[4:6]
    cp = tail[6]
    k_str = tail[7:15]
    root = s[:-15]
    try:
        yr = 2000 + int(yy)
        expiry = date(yr, int(mm), int(dd))
        strike = int(k_str) / 1000.0
        return (root, expiry, cp, strike)
    except Exception:
        return None

def ingest_cboe_gld(db):
    body, _, _ = http_get(CBOE_URL, timeout=30)
    d = json.loads(body)
    ts = d.get("timestamp")   # observation timestamp string in exchange local time
    obs_data = d.get("data", {})
    opts = obs_data.get("options", [])
    S = obs_data.get("current_price") or obs_data.get("last_trade_price")
    print(f"  CBOE GLD chain: {len(opts)} rows   S={S}   obs_ts={ts}")
    obs_date = date.today().isoformat()
    written = 0
    for o in opts:
        parsed = parse_occ(o.get("option"))
        if not parsed: continue
        root, expiry, cp, strike = parsed
        if root not in ("GLD",):    # keep only vanilla GLD
            continue
        dte = (expiry - date.today()).days
        row = {
            "exchange": "CBOE", "underlying": "GLD",
            "underlying_kind": "EQUITY_OPTION",
            "observation_date": obs_date,
            "retrieval_ts_utc": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
            "underlying_contract": "GLD",
            "underlying_expiry":   None,
            "option_expiry":       expiry.isoformat(),
            "days_to_expiry":      dte,
            "strike":              strike,
            "option_type":         cp,
            "open_interest":       int(o.get("open_interest") or 0),
            "change_in_open_interest": None,
            "volume":              int(o.get("volume") or 0),
            "settlement":          None,
            "last_price":          o.get("last_trade_price"),
            "bid":                 o.get("bid"),
            "ask":                 o.get("ask"),
            "iv":                  o.get("iv"),
            "iv_source":           "OBSERVED" if o.get("iv") is not None else None,
            "delta_observed":      o.get("delta"),
            "gamma_observed":      o.get("gamma"),
            "vega_observed":       o.get("vega"),
            "theta_observed":      o.get("theta"),
            "delta_derived":       None,
            "gamma_derived":       None,
            "contract_multiplier": 100.0,
            "greeks_status":       "OBSERVED" if o.get("gamma") is not None else "NONE",
            "underlying_price_ref": S,
            "underlying_price_ts":  ts,
            "source":              "cboe_delayed",
            "source_url":          CBOE_URL,
        }
        db.execute(text("""
            INSERT OR REPLACE INTO options_chain_v2 (
                exchange, underlying, underlying_kind,
                observation_date, retrieval_ts_utc,
                underlying_contract, underlying_expiry,
                option_expiry, days_to_expiry, strike, option_type,
                open_interest, change_in_open_interest, volume,
                settlement, last_price, bid, ask, iv, iv_source,
                delta_observed, gamma_observed, vega_observed, theta_observed,
                delta_derived, gamma_derived, contract_multiplier,
                greeks_status, underlying_price_ref, underlying_price_ts,
                source, source_url
            ) VALUES (
                :exchange, :underlying, :underlying_kind,
                :observation_date, :retrieval_ts_utc,
                :underlying_contract, :underlying_expiry,
                :option_expiry, :days_to_expiry, :strike, :option_type,
                :open_interest, :change_in_open_interest, :volume,
                :settlement, :last_price, :bid, :ask, :iv, :iv_source,
                :delta_observed, :gamma_observed, :vega_observed, :theta_observed,
                :delta_derived, :gamma_derived, :contract_multiplier,
                :greeks_status, :underlying_price_ref, :underlying_price_ts,
                :source, :source_url
            )
        """), row)
        written += 1
    db.commit()
    print(f"  rows written to options_chain_v2 (GLD): {written}")
    return written, S, ts


# ─── Section D. Absolute gamma concentration (unsigned) ──────────────────
# ABS_GAMMA_CONCENTRATION(K, expiry) =
#     m · S² · Γ_obs · (OI_call + OI_put) · 0.01
# Units:  dollars per 1% underlying move.
# Interpretation: "dollar-equivalent aggregate delta change if the
# underlying moves by 1%", attributed to open interest at that strike.
def compute_gld_concentration(db):
    obs_date = date.today().isoformat()
    r = db.execute(text("""
        SELECT strike, option_expiry, days_to_expiry, gamma_observed,
               open_interest, option_type, underlying_price_ref,
               contract_multiplier, iv
        FROM options_chain_v2
        WHERE underlying='GLD' AND observation_date=:d
    """), {"d": obs_date}).fetchall()
    print(f"  scanning {len(r)} GLD option rows")

    # Aggregate per (expiry, strike)
    agg = {}       # key = (expiry, strike) -> dict
    S_ref = None
    for (K, expiry, dte, gamma, oi, cp, S, m, iv) in r:
        if S_ref is None: S_ref = S
        key = (expiry, K)
        d = agg.setdefault(key, {"call_oi": 0, "put_oi": 0, "gamma": None,
                                   "dte": dte, "m": m, "iv": iv})
        if cp == "C": d["call_oi"] += (oi or 0)
        else:         d["put_oi"]  += (oi or 0)
        # store the max nonzero gamma seen at that strike/expiry (calls and puts
        # share Γ under B-76; under BS-equity they also match to O(rounding))
        if gamma is not None and gamma > (d["gamma"] or 0):
            d["gamma"] = gamma

    ins = 0
    for (expiry, K), d in agg.items():
        total_oi = d["call_oi"] + d["put_oi"]
        if total_oi == 0 or not d["gamma"] or not S_ref:
            continue
        # ABS_GAMMA_CONCENTRATION (per 1% underlying move, dollars)
        agc = d["m"] * S_ref * S_ref * d["gamma"] * total_oi * 0.01
        db.execute(text("""
            INSERT OR REPLACE INTO options_gamma_concentration (
                observation_date, exchange, underlying, underlying_price_ref,
                option_expiry, days_to_expiry, strike,
                total_oi, call_oi, put_oi, total_volume,
                gamma_per_contract, contract_multiplier,
                abs_gamma_concentration, iv_atm_snapshot
            ) VALUES (
                :od, 'CBOE', 'GLD', :s, :e, :dte, :k,
                :toi, :coi, :poi, 0, :g, :m, :agc, :iv
            )
        """), {"od": obs_date, "s": S_ref, "e": expiry,
                "dte": d["dte"], "k": K, "toi": total_oi,
                "coi": d["call_oi"], "poi": d["put_oi"],
                "g": d["gamma"], "m": d["m"], "agc": agc, "iv": d["iv"]})
        ins += 1
    db.commit()
    print(f"  wrote {ins} rows to options_gamma_concentration (GLD)")

    # Report top concentrations
    r = db.execute(text("""
        SELECT strike, option_expiry, days_to_expiry, total_oi,
               call_oi, put_oi, gamma_per_contract, abs_gamma_concentration
        FROM options_gamma_concentration
        WHERE underlying='GLD' AND observation_date=:d
        ORDER BY abs_gamma_concentration DESC LIMIT 10
    """), {"d": obs_date}).fetchall()
    print("\n  === TOP-10 GLD strikes by ABS_GAMMA_CONCENTRATION (per 1% move, $) ===")
    for x in r:
        print(f"    K={x[0]:>7.2f}  expiry={x[1]}  DTE={x[2]:>3}  "
                f"OI={x[3]:>7,}  Γ={x[6]:.5f}  ABS_GC=${x[7]:,.0f}")
    return ins


def main():
    with SessionLocal() as db:
        print("=" * 72)
        print("PHASE 2 — CME source audit + Gamma validation + GLD ingest + AGC")
        print("=" * 72)
        # A. audit
        audit_sources(db)
        rows = db.execute(text("""
            SELECT reachable, source_name, http_status, notes
            FROM options_source_audit ORDER BY id DESC LIMIT 6
        """)).fetchall()
        print("\n=== options_source_audit (latest 6) ===")
        for x in rows:
            tag = "[OK ]" if x[0] else "[BLK]"
            print(f"  {tag} status={x[2] or '-':<5} {x[1][:32]:<32}  {x[3][:80]}")
        # B. gamma unit validation
        print()
        gamma_ref = gamma_unit_validation()
        # C. GLD ingest
        print()
        print("=" * 72)
        print("CBOE GLD OPTIONS INGEST  (RESEARCH-ONLY ETF-PROXY, NOT GC)")
        print("=" * 72)
        n_written, S_ref, obs_ts = ingest_cboe_gld(db)
        # D. compute concentration
        print()
        print("=" * 72)
        print("GLD ABS_GAMMA_CONCENTRATION")
        print("=" * 72)
        compute_gld_concentration(db)

if __name__ == "__main__":
    main()
