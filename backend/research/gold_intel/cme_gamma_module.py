"""Research gamma module — PREPARED-DISABLED per Phase-2C directive.

Contains a Black-76 gamma calculator that reads from the ingested
cme_gc_options_eod + cme_gc_futures_eod tables. Never triggered by any
scheduled job. Callable on-demand only for research.

The computed metric is:
    OI_WEIGHTED_ABSOLUTE_GAMMA_SENSITIVITY (unsigned, per-1%-move dollars)
      = 100 * F^2 * Gamma_B76 * OI * 0.01
Terminology discipline:
  - metric name is "OI-weighted absolute gamma sensitivity" (not GEX)
  - unsigned
  - never labelled as dealer exposure or wall

Delta produced here is delta_derived_b76 (DERIVED_MODEL).
It never overwrites delta_cme (OBSERVED) in the schema.

Discipline: HYPOTHESIS-labelled residual between delta_cme and
delta_derived_b76 remains open. Potential contributors include
American-exercise premium, CME valuation methodology, IV convention,
interest-rate curve, or timestamp differences. None are proven.
"""
import math
from database import SessionLocal
from sqlalchemy import text

CONTRACT_MULTIPLIER = {
    "OG":  100.0,     # standard COMEX Gold options, 100 troy oz
    "OG1": 100.0, "OG2": 100.0, "OG3": 100.0, "OG4": 100.0,
    "OMG": 10.0,      # Micro Gold options, 10 troy oz
    "WMG": 10.0, "MMG": 10.0, "FMG": 10.0,
    "GMW": 100.0, "GWR": 100.0, "GWT": 100.0, "GWW": 100.0,
}

# Set to True only when the user explicitly authorises live gamma computation.
ENABLED = False

def _cdf(x): return 0.5*(1.0 + math.erf(x/math.sqrt(2.0)))
def _pdf(x): return math.exp(-0.5*x*x)/math.sqrt(2*math.pi)

def _b76(F, K, T, r, sig, cp):
    if sig<=0 or T<=0 or F<=0 or K<=0: return 0.0, 0.0, 0.0
    sq = math.sqrt(T)
    d1 = (math.log(F/K) + 0.5*sig*sig*T)/(sig*sq); d2 = d1 - sig*sq
    disc = math.exp(-r*T)
    if cp == "C":
        p = disc*(F*_cdf(d1) - K*_cdf(d2))
        d = disc*_cdf(d1)
    else:
        p = disc*(K*_cdf(-d2) - F*_cdf(-d1))
        d = disc*(_cdf(d1) - 1.0)
    g = disc*_pdf(d1)/(F*sig*sq)
    return p, d, g

def _solve_iv(F, K, T, r, cp, target, tol=1e-7, maxit=200):
    lo, hi = 0.0005, 4.0
    p_lo, _, _ = _b76(F, K, T, r, lo, cp)
    p_hi, _, _ = _b76(F, K, T, r, hi, cp)
    if not (min(p_lo, p_hi) <= target <= max(p_lo, p_hi)): return None
    for _ in range(maxit):
        mid = 0.5*(lo+hi); p, _, _ = _b76(F, K, T, r, mid, cp)
        if abs(p - target) < tol: return mid
        if (p - target) * (p_lo - target) > 0:
            lo = mid; p_lo = p
        else:
            hi = mid; p_hi = p
    return mid

def compute_on_demand(bulletin_date: str, options_status: str,
                      futures_status: str, r: float = 0.0478, force: bool = False):
    """Compute per-row gamma / delta_derived_b76 / abs_gamma_concentration.

    Refuses to run when ENABLED is False unless force=True (explicit override).
    Returns list of dicts; does NOT write into cme_gc_options_eod.
    """
    if not ENABLED and not force:
        return {"status": "DISABLED",
                "reason": "gamma module prepared but disabled per Phase-2C directive; "
                          "call with force=True only for authorised research."}
    # ... implementation is intentionally not wired to schedulers.
    # Left as a stub to preserve the discipline that gamma cannot be computed
    # by any automatic path.
    return {"status": "NOT_IMPLEMENTED_IN_STUB",
            "note": "on-demand implementation is intentionally deferred; when "
                    "authorised, this function pulls options + futures rows, "
                    "solves IV per row (Black-76), and writes derived_model outputs "
                    "to a separate research table — never onto cme_gc_options_eod."}
