"""Seed the gold_data_register + run Black-76 GEX numeric self-test.

Both are RESEARCH-ONLY. The Black-76 solver is a stand-alone test — it does
NOT ingest options data; it verifies the pricing/gamma math is correct so the
Phase 2 activation later has a validated primitive to build on.
"""
import math
from database import SessionLocal
from sqlalchemy import text

# ─────────────────────────────────────────────────────────────────────
# gold_data_register seed
# ─────────────────────────────────────────────────────────────────────

REGISTER = [
    # (dataset_name, category, provider, url, instrument, coverage,
    #  frequency, latency, historical_depth, observed_or_inferred, cost_tier,
    #  api_available, reliability, integration_complexity, expected_info_value,
    #  redundancy_flag, research_priority, status)
    ("CFTC Disaggregated Futures Only — GOLD",         "POSITIONING", "CFTC",
     "https://www.cftc.gov/files/dea/history/fut_disagg_txt_*.zip",
     "COMEX Gold 088691", "2006-06-13 → present", "Weekly (Tue as-of, Fri publish)",
     "T+3 typical", "20+ years", "OBSERVED", "FREE", 1,
     "Authoritative; annual archives verified 2006-2026",
     "LOW", "HIGH", "OI/positioning duplicated with COMEX OI reports", "P1", "PROMOTED"),

    ("CFTC Disaggregated F+O Combined — GOLD",         "POSITIONING", "CFTC",
     "https://www.cftc.gov/files/dea/history/com_disagg_txt_*.zip",
     "COMEX Gold 088691 (F+O combined)", "2006-06-13 → present", "Weekly",
     "T+3 typical", "20+ years", "OBSERVED", "FREE", 1,
     "Authoritative", "LOW", "MEDIUM (partial overlap with FUT_ONLY)",
     "Overlaps FUT_ONLY", "P1", "PROMOTED"),

    ("US Treasury Direct — Daily Yield Curve",         "MACRO", "US Treasury",
     "home.treasury.gov/resource-center/.../daily-treasury-rates.csv",
     "US Treasury Nominal Yields (1M-30Y)", "2019-01-02 → present", "Daily EOD",
     "Same-day close", "6+ years usable", "OBSERVED", "FREE", 1,
     "Authoritative; official Treasury", "LOW", "HIGH", "None", "P1", "PROMOTED"),

    ("US Treasury Direct — Daily Real Yield Curve",    "MACRO", "US Treasury",
     "home.treasury.gov/.../daily-treasury-real-yield-curve-rates.csv",
     "US Treasury TIPS Real Yields (5Y-30Y)", "2019-01-02 → present", "Daily EOD",
     "Same-day close", "6+ years usable", "OBSERVED", "FREE", 1,
     "Authoritative", "LOW", "HIGH", "None; complements nominal", "P1", "PROMOTED"),

    ("US 10Y Breakeven — DERIVED",                     "MACRO", "DERIVED",
     "computed:UST_10Y − UST_REAL10Y",
     "10Y Breakeven Inflation (proxy)", "2019-01-02 → present", "Daily EOD",
     "Same-day", "6+ years", "DERIVED", "FREE", 1,
     "Deterministic difference; matches FRED T10YIE within rounding",
     "LOW", "MEDIUM (deriv)", "None (identity)", "P1", "PROMOTED"),

    ("TradingView (anon) — DXY / VIX / MOVE / WTI / USDJPY / USDCNH / XAU D1",
     "MACRO", "TradingView (anonymous)",
     "tvDatafeed anonymous client",
     "spot/index daily bars", "~3 years accessible in anon mode", "Daily EOD",
     "Same-day close", "~800 D1 bars available", "OBSERVED", "FREE (rate-limited)", 1,
     "Anonymous mode returns limited history but authoritative TVC/CBOE data",
     "MEDIUM", "MEDIUM", "US10Y proxy overlaps Treasury direct nominal 10Y",
     "P1", "PROMOTED"),

    ("FRED — DFII10 / T10YIE / T5YIFR / DFF / DGS2 / DGS10",
     "MACRO", "FRED (Federal Reserve St. Louis)",
     "fred.stlouisfed.org/graph/fredgraph.csv",
     "US rates + inflation + policy series", "1962-present", "Daily EOD",
     "Same-day / T+1", "60+ years", "OBSERVED", "FREE (API key optional)", 1,
     "Authoritative but endpoint TIMES OUT from droplet (IP blocked/rate-limited)",
     "MEDIUM", "HIGH", "Overlaps Treasury Direct partially",
     "P1", "REJECTED (unreachable this build; retry with FRED API key)"),

    ("CFTC Bank Participation Report",                  "POSITIONING", "CFTC",
     "cftc.gov/dea/newcot/dea_bank_report.htm",
     "Bank participation in gold futures", "Monthly", "Monthly",
     "~T+5", "20+ years", "OBSERVED", "FREE", 0,
     "Small dealer-bank universe, complements COT", "MEDIUM", "MEDIUM",
     "Overlaps disaggregated Swap Dealer", "P2", "UNDER_REVIEW"),

    ("Gold ETF holdings — GLD, IAU (WGC)",              "FLOWS", "World Gold Council",
     "gold.org/goldhub/data/gold-etf-holdings-and-flows",
     "physical ETF holdings + AUM flows", "Daily", "Daily EOD",
     "T+1", "10+ years", "OBSERVED", "FREE (WGC)", 1,
     "Reputable; XLS/JSON access varies", "MEDIUM", "MEDIUM",
     "None (unique)", "P2", "UNDER_REVIEW"),

    ("Shanghai Gold Exchange — Au99.99 / Au(T+D)",      "PHYSICAL", "SGE",
     "en.sge.com.cn/dailyPriceQuotation.html",
     "Chinese physical gold prices + volume", "Daily", "Intraday during CN hours",
     "Real-time CN / T+1 UTC", "10+ years", "OBSERVED", "FREE (public HTML)", 0,
     "HTML scraping; Chinese calendar", "HIGH", "MEDIUM (Asia context)",
     "None (unique)", "P2", "UNDER_REVIEW"),

    ("Barchart GC Options snapshot (existing)",         "OPTIONS", "Barchart (owned files)",
     "local files: research/barchart_gc_options/",
     "GC options: strike / IV / delta / gamma", "One-off historical", "Snapshot",
     "n/a", "one-off", "OBSERVED", "FREE (already-owned)", 0,
     "Owned data but no OI/volume — insufficient for real GEX",
     "MEDIUM", "LOW (missing OI)", "Partial — no OI",
     "P1", "PROMOTED (as reference; not sufficient for GEX)"),

    ("CME Group Daily Bulletin — GC options EOD",       "OPTIONS", "CME Group",
     "cmegroup.com/tools-information/quikstrike/options-calendar-gold.html",
     "settlement + OI + volume by strike/expiry", "EOD", "EOD (Tues-Sat)",
     "~2h post-settle", "5+ years", "OBSERVED", "FREE (Daily Bulletin)", 1,
     "Authoritative for OI/vol; Greeks self-computed via Black-76", "MEDIUM",
     "HIGH — required for GEX-Lite", "None (unique)", "P1", "APPROVED_FOR_RESEARCH"),

    ("Databento GLBX.MDP3 — GC MBO/MBP",                "MICROSTRUCTURE", "Databento",
     "databento.com", "COMEX GC exchange trade/depth",
     "Live+history", "Live+historical", "ms-precision", "5+ years", "OBSERVED",
     "PAID (metered)", 1,
     "Authoritative exchange data; requires purchase",
     "HIGH", "HIGH (Track B)", "None (unique for exchange)",
     "P1 (Track B)", "REJECTED (paid — HOLD)"),

    ("Exness MT5 quote-level ticks — XAUUSD",          "MICROSTRUCTURE", "Exness (broker)",
     "MT5 copy_ticks_from — daemon POST", "Broker quote updates",
     "2026-09-03 → present", "Live intraday", "seconds",
     "days so far, growing", "OBSERVED_QUOTES_ONLY",
     "FREE (broker feed)", 1,
     "QUOTE-LEVEL only; NOT true order flow. Zero trade prints on Exness spot.",
     "LOW (already ingesting)", "RESEARCH_ONLY", "None (unique)",
     "P1 (Track A)", "PROMOTED"),

    ("ForexFactory calendar (existing)",                "EVENT_RISK", "ForexFactory",
     "internal cache",
     "Macro calendar", "Live", "~15min",
     "n/a", "n/a", "OBSERVED", "FREE", 1,
     "Already ingesting via macro_events", "LOW",
     "LOW (already there)", "None", "P3", "PROMOTED"),
]


def seed_register(db):
    db.execute(text("DELETE FROM gold_data_register"))
    for row in REGISTER:
        (name, cat, provider, url, inst, cov, freq, lat, hist,
         obs, cost, api_ok, rel_note, integ, val, redund, prio, status) = row
        db.execute(text("""
            INSERT INTO gold_data_register (
                dataset_name, category, provider, official_source_url,
                instrument, coverage, frequency, latency, historical_depth,
                observed_or_inferred, cost_tier, api_available,
                reliability_note, integration_complexity, expected_info_value,
                redundancy_flag, research_priority, status
            ) VALUES (
                :n, :c, :p, :u, :i, :cov, :f, :lat, :h, :o, :ct, :api,
                :rn, :ic, :val, :red, :pr, :st
            )
        """), {"n": name, "c": cat, "p": provider, "u": url, "i": inst,
                "cov": cov, "f": freq, "lat": lat, "h": hist, "o": obs,
                "ct": cost, "api": api_ok, "rn": rel_note, "ic": integ,
                "val": val, "red": redund, "pr": prio, "st": status})
    db.commit()


# ─────────────────────────────────────────────────────────────────────
# Black-76 solver (options-on-futures)
# ─────────────────────────────────────────────────────────────────────
# Under Black-76 for GC futures options:
#   d1 = [ln(F/K) + 0.5 σ² T] / (σ √T)
#   d2 = d1 − σ √T
#   Call = e^(−rT) [F N(d1) − K N(d2)]
#   Put  = e^(−rT) [K N(−d2) − F N(−d1)]
#   Δ_call = e^(−rT) N(d1)
#   Δ_put  = e^(−rT) (N(d1) − 1)
#   Γ = e^(−rT) φ(d1) / (F σ √T)     — same for calls and puts under B-76
#
# GC contract spec (COMEX GC): 100 troy oz per contract, tick 0.10 = $10
# GAMMA EXPOSURE (per strike, per contract):
#   raw_gamma_per_contract  = Γ                                (dimensionless, per $1 of F change)
#   dollar_gamma_per_contract = 100 × F × Γ                    (∆Δ in oz × contract size)
#   position_gamma_notional = OI × dollar_gamma_per_contract   (dollars of ∆Δ per 1% underlying move)
#
# ABSOLUTE GEX by strike (absolute, undirected):
#   ABS_GEX(strike) = Σ_{expiries} [ OI_call(K,T) + OI_put(K,T) ] × 100 × F × Γ(K,T)
# TOTAL ABS_GEX = Σ_strikes ABS_GEX
#
# This is a GAMMA-CONCENTRATION metric. It tells us WHERE gamma sensitivity is
# concentrated across the strike surface. It does NOT tell us the direction
# of dealer inventory. Signed GEX requires a dealer inventory assumption.
# ─────────────────────────────────────────────────────────────────────

def norm_cdf(x):
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))

def norm_pdf(x):
    return math.exp(-0.5 * x * x) / math.sqrt(2 * math.pi)

def b76_price_and_greeks(F, K, T, r, sigma, cp):
    """Returns (price, delta, gamma) under Black-76."""
    if sigma <= 0 or T <= 0 or F <= 0 or K <= 0:
        return None, None, None
    sqrtT = math.sqrt(T)
    d1 = (math.log(F/K) + 0.5 * sigma * sigma * T) / (sigma * sqrtT)
    d2 = d1 - sigma * sqrtT
    disc = math.exp(-r * T)
    if cp == "C":
        price = disc * (F * norm_cdf(d1) - K * norm_cdf(d2))
        delta = disc * norm_cdf(d1)
    else:
        price = disc * (K * norm_cdf(-d2) - F * norm_cdf(-d1))
        delta = disc * (norm_cdf(d1) - 1.0)
    gamma = disc * norm_pdf(d1) / (F * sigma * sqrtT)
    return price, delta, gamma

def absolute_gex_per_contract(F, gamma_per_contract, contract_size=100):
    """Dollar gamma per contract:  100 × F × Γ."""
    return contract_size * F * gamma_per_contract

# ── Verification test ──
def run_selftest():
    print("=" * 68)
    print("BLACK-76 SELF-TEST — GC CONTRACT SPEC")
    print("=" * 68)
    # Reference case
    F = 4400.0     # GC futures price (near current)
    K = 4400.0     # ATM
    T = 30/365.0   # 30 days to expiry
    r = 0.045      # 4.5% risk-free (roughly matches UST_10Y)
    sig = 0.20     # 20% annualised IV
    # Call
    pC, dC, gC = b76_price_and_greeks(F, K, T, r, sig, "C")
    # Put
    pP, dP, gP = b76_price_and_greeks(F, K, T, r, sig, "P")
    # Put-call parity check under Black-76:
    #   Call - Put = e^(-rT) * (F - K)  → for F=K, Call = Put
    pcp_lhs = pC - pP
    pcp_rhs = math.exp(-r*T) * (F - K)
    print(f"F={F}, K={K}, T={T:.4f}y, r={r}, σ={sig}")
    print(f"  Call: price={pC:.4f}  Δ={dC:.5f}  Γ={gC:.7f}")
    print(f"  Put:  price={pP:.4f}  Δ={dP:.5f}  Γ={gP:.7f}")
    print(f"  Put-Call Parity check: LHS={pcp_lhs:.6f}  RHS={pcp_rhs:.6f}  |diff|={abs(pcp_lhs-pcp_rhs):.2e}")
    # Γ should be equal for calls and puts under Black-76
    print(f"  Γ symmetry (Call vs Put): |ΔΓ|={abs(gC-gP):.2e}")
    # Dollar gamma per contract
    dg = absolute_gex_per_contract(F, gC, contract_size=100)
    print(f"  Absolute dollar gamma per contract at ATM: ${dg:,.2f}")

    # Now scan a strike ladder (research prototype)
    print()
    print("=== ABSOLUTE GEX PROTOTYPE (strike ladder, single expiry) ===")
    strikes  = [4300, 4350, 4400, 4450, 4500]
    call_OI  = [ 1200, 3400, 5200, 4100, 2400]
    put_OI   = [  900, 2600, 5400, 2600, 1200]
    total_abs_gex = 0.0
    for K, cOI, pOI in zip(strikes, call_OI, put_OI):
        _, _, gc = b76_price_and_greeks(F, K, T, r, sig, "C")
        _, _, gp = b76_price_and_greeks(F, K, T, r, sig, "P")
        # Sanity: Γ same → dollar gamma same
        dg_per_c = absolute_gex_per_contract(F, gc)
        abs_gex_at_K = (cOI + pOI) * dg_per_c
        total_abs_gex += abs_gex_at_K
        print(f"  K={K}: call_OI={cOI}  put_OI={pOI}  Γ={gc:.6f}  "
              f"$Γ/contract=${dg_per_c:,.2f}  ABS_GEX_K=${abs_gex_at_K:,.0f}")
    print(f"  TOTAL ABS_GEX (ladder) = ${total_abs_gex:,.0f}")
    print()
    print("What this DOES tell us:")
    print("  · Where gamma sensitivity is concentrated across strikes.")
    print("  · Which strikes carry the largest dollar-gamma exposure for the aggregate OI.")
    print()
    print("What this does NOT tell us:")
    print("  · Whether dealers are LONG or SHORT gamma. That needs a dealer-inventory model,")
    print("    which we have explicitly DEFERRED per the amended directive.")
    print("  · Whether price is likely to be pinned or repelled by a strike.")
    print("  · Anything about the OTC/dealer book that isn't reflected in COMEX OI.")


def main():
    with SessionLocal() as db:
        seed_register(db)
        r = db.execute(text("SELECT COUNT(*) FROM gold_data_register")).scalar()
        print(f"gold_data_register rows: {r}")
        r = db.execute(text(
            "SELECT dataset_name, category, cost_tier, research_priority, status "
            "FROM gold_data_register ORDER BY research_priority, dataset_name"
        )).fetchall()
        for x in r:
            print(f"  {x[3]:<3} {x[4]:<26} {x[2]:<20} {x[1]:<16} {x[0]}")
        print()
    run_selftest()

if __name__ == "__main__":
    main()
