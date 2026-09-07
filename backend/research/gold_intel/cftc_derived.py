"""CFTC derived table + historical distribution report.

For each cftc_cot_raw row, compute:
  - net positions per participant category
  - weekly deltas (Δlong/Δshort/Δnet vs previous report of same type)
  - MM net/OI ratios
  - rolling percentiles (1y=52 weeks, 3y=156, 5y=260) on MM_net
  - 3y z-score on MM_net

Then report the empirical distributions for the amended directive:
  |Δ MM long|, |Δ MM short|, |Δ MM net|, Δ OI, MM_net/OI.
"""
import statistics
from database import SessionLocal
from sqlalchemy import text

def pctile(sorted_vals, p):
    if not sorted_vals: return None
    if len(sorted_vals) == 1: return float(sorted_vals[0])
    k = (len(sorted_vals) - 1) * p
    lo = int(k)
    hi = min(lo + 1, len(sorted_vals) - 1)
    return sorted_vals[lo] + (sorted_vals[hi] - sorted_vals[lo]) * (k - lo)

def summarize(vals, label, abs_it=False):
    if abs_it:
        vals = [abs(v) for v in vals if v is not None]
    else:
        vals = [v for v in vals if v is not None]
    if not vals:
        return
    vs = sorted(vals)
    n = len(vs)
    print(f"\n  --- {label}  (n={n}) ---")
    print(f"    median: {statistics.median(vs):>14,.2f}")
    print(f"    p25:    {pctile(vs, 0.25):>14,.2f}")
    print(f"    p75:    {pctile(vs, 0.75):>14,.2f}")
    print(f"    p90:    {pctile(vs, 0.90):>14,.2f}")
    print(f"    p95:    {pctile(vs, 0.95):>14,.2f}")
    print(f"    p99:    {pctile(vs, 0.99):>14,.2f}")
    print(f"    mean:   {statistics.mean(vs):>14,.2f}")
    if n >= 2:
        print(f"    stdev:  {statistics.stdev(vs):>14,.2f}")
    print(f"    min:    {vs[0]:>14,.2f}")
    print(f"    max:    {vs[-1]:>14,.2f}")

def main():
    with SessionLocal() as db:
        # Clear derived table for full recompute
        db.execute(text("DELETE FROM cftc_cot_derived"))
        db.commit()

        for report_type in ("FUT_ONLY", "COMBINED"):
            rows = db.execute(text("""
                SELECT id, report_date_yyyy_mm_dd, open_interest_all,
                       m_money_positions_long_all, m_money_positions_short_all,
                       m_money_positions_spread_all,
                       prod_merc_positions_long_all, prod_merc_positions_short_all,
                       swap_positions_long_all, swap_positions_short_all,
                       other_rept_positions_long_all, other_rept_positions_short_all
                FROM cftc_cot_raw WHERE report_type=:rt
                ORDER BY report_date_yyyy_mm_dd ASC
            """), {"rt": report_type}).fetchall()

            prev = None
            history_net = []
            history_oi  = []
            batch = []

            for r in rows:
                (rid, rd, oi, mm_l, mm_s, mm_sp, pr_l, pr_s, sw_l, sw_s, oth_l, oth_s) = r
                mm_net = (mm_l or 0) - (mm_s or 0)
                pr_net = (pr_l or 0) - (pr_s or 0)
                sw_net = (sw_l or 0) - (sw_s or 0)
                oth_net = (oth_l or 0) - (oth_s or 0)
                if prev is not None:
                    d_mm_l = (mm_l or 0) - (prev["mm_l"] or 0)
                    d_mm_s = (mm_s or 0) - (prev["mm_s"] or 0)
                    d_mm_net = mm_net - prev["mm_net"]
                    d_oi = (oi or 0) - (prev["oi"] or 0)
                else:
                    d_mm_l = d_mm_s = d_mm_net = d_oi = None

                mm_net_pct_oi = (mm_net / oi) if oi else None
                mm_long_pct_oi = ((mm_l or 0) / oi) if oi else None
                mm_short_pct_oi = ((mm_s or 0) / oi) if oi else None

                # Rolling percentiles/z-score on mm_net (against history so far)
                history_net.append(mm_net)
                def pctile_of_last_n(values, current, n):
                    if len(values) < 4: return None
                    tail = values[-n:] if len(values) > n else values[:]
                    # rank of current within tail (excluding itself is same value since appended)
                    sorted_tail = sorted(tail)
                    below = sum(1 for v in sorted_tail if v < current)
                    return below / max(len(sorted_tail) - 1, 1) if len(sorted_tail) > 1 else None
                pct_1y = pctile_of_last_n(history_net, mm_net, 52)
                pct_3y = pctile_of_last_n(history_net, mm_net, 156)
                pct_5y = pctile_of_last_n(history_net, mm_net, 260)
                # z-score over 3y
                z_3y = None
                if len(history_net) >= 30:
                    tail = history_net[-156:]
                    m = statistics.mean(tail)
                    s = statistics.stdev(tail) if len(tail) > 1 else None
                    if s and s > 0:
                        z_3y = (mm_net - m) / s

                batch.append({
                    "raw_id": rid, "report_type": report_type,
                    "report_date_yyyy_mm_dd": rd,
                    "oi_all": oi,
                    "m_money_long": mm_l, "m_money_short": mm_s, "m_money_spread": mm_sp,
                    "m_money_net": mm_net, "prod_merc_net": pr_net,
                    "swap_net": sw_net, "other_rept_net": oth_net,
                    "d_m_money_long_1w": d_mm_l, "d_m_money_short_1w": d_mm_s,
                    "d_m_money_net_1w":  d_mm_net, "d_oi_1w": d_oi,
                    "m_money_net_pct_oi":  mm_net_pct_oi,
                    "m_money_long_pct_oi": mm_long_pct_oi,
                    "m_money_short_pct_oi": mm_short_pct_oi,
                    "m_money_net_pctile_1y": pct_1y,
                    "m_money_net_pctile_3y": pct_3y,
                    "m_money_net_pctile_5y": pct_5y,
                    "m_money_net_z_3y": z_3y,
                })

                prev = {"mm_l": mm_l, "mm_s": mm_s, "mm_net": mm_net, "oi": oi}

            # Bulk insert
            for row in batch:
                db.execute(text("""
                    INSERT OR REPLACE INTO cftc_cot_derived (
                        raw_id, report_type, report_date_yyyy_mm_dd,
                        oi_all, m_money_long, m_money_short, m_money_spread,
                        m_money_net, prod_merc_net, swap_net, other_rept_net,
                        d_m_money_long_1w, d_m_money_short_1w, d_m_money_net_1w, d_oi_1w,
                        m_money_net_pct_oi, m_money_long_pct_oi, m_money_short_pct_oi,
                        m_money_net_pctile_1y, m_money_net_pctile_3y, m_money_net_pctile_5y,
                        m_money_net_z_3y
                    ) VALUES (
                        :raw_id, :report_type, :report_date_yyyy_mm_dd,
                        :oi_all, :m_money_long, :m_money_short, :m_money_spread,
                        :m_money_net, :prod_merc_net, :swap_net, :other_rept_net,
                        :d_m_money_long_1w, :d_m_money_short_1w, :d_m_money_net_1w, :d_oi_1w,
                        :m_money_net_pct_oi, :m_money_long_pct_oi, :m_money_short_pct_oi,
                        :m_money_net_pctile_1y, :m_money_net_pctile_3y, :m_money_net_pctile_5y,
                        :m_money_net_z_3y
                    )
                """), row)
            db.commit()
            print(f"  {report_type}: derived rows written: {len(batch)}")

        # ── Distribution analysis (FUT_ONLY only for now) ──
        print()
        print("=" * 72)
        print("HISTORICAL DISTRIBUTIONS — DISAGGREGATED FUT_ONLY  (2006-2026, gold)")
        print("=" * 72)

        r = db.execute(text("""
            SELECT d_m_money_long_1w, d_m_money_short_1w, d_m_money_net_1w,
                   d_oi_1w, m_money_net_pct_oi
            FROM cftc_cot_derived WHERE report_type='FUT_ONLY'
        """)).fetchall()
        dLong  = [x[0] for x in r]
        dShort = [x[1] for x in r]
        dNet   = [x[2] for x in r]
        dOI    = [x[3] for x in r]
        pctOI  = [x[4] for x in r]

        summarize(dLong,  "|Δ Managed Money Long|",  abs_it=True)
        summarize(dShort, "|Δ Managed Money Short|", abs_it=True)
        summarize(dNet,   "|Δ Managed Money Net|",   abs_it=True)
        summarize(dOI,    "|Δ Open Interest|",       abs_it=True)
        summarize(pctOI,  "Managed Money Net / OI (signed)", abs_it=False)

        # Latest observation location
        print()
        print("=" * 72)
        print("LATEST OBSERVATION — location within historical distribution")
        print("=" * 72)
        latest = db.execute(text("""
            SELECT * FROM cftc_cot_derived WHERE report_type='FUT_ONLY'
            ORDER BY report_date_yyyy_mm_dd DESC LIMIT 1
        """)).fetchone()
        if latest:
            m = dict(latest._mapping)
            print(f"  report_date:         {m['report_date_yyyy_mm_dd']}")
            print(f"  MM long:             {m['m_money_long']:,}")
            print(f"  MM short:            {m['m_money_short']:,}")
            print(f"  MM net:              {m['m_money_net']:,}")
            print(f"  ΔMM long 1W:         {m['d_m_money_long_1w']:,}")
            print(f"  ΔMM short 1W:        {m['d_m_money_short_1w']:,}")
            print(f"  ΔMM net 1W:          {m['d_m_money_net_1w']:,}")
            print(f"  MM net / OI:         {m['m_money_net_pct_oi']*100:.2f}%")
            print(f"  1y percentile MM net: {(m['m_money_net_pctile_1y'] or 0)*100:.1f}%")
            print(f"  3y percentile MM net: {(m['m_money_net_pctile_3y'] or 0)*100:.1f}%")
            print(f"  5y percentile MM net: {(m['m_money_net_pctile_5y'] or 0)*100:.1f}%")
            print(f"  3y z-score MM net:    {m['m_money_net_z_3y']:.2f}")

if __name__ == "__main__":
    main()
