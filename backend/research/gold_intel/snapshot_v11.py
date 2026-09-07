"""Phase-1 closure — Daily Intelligence V1.1

Fixes:
  - Current XAUUSD from mt5_ticks (last bid), not stale M5 close
  - PDH/PDL: previous trading day derived from actual trading-day presence
  - PWH/PWL: previous completed trading week (Mon–Fri boundary)
  - Asian session H/L: explicit 22:00 UTC prev day → 06:00 UTC current day
  - Structural read-in (regime + liquidity_map + vp_trap_zones — READ ONLY)
  - GC futures feed via TradingView COMEX:GC1! (front-month continuous)
  - ForexFactory calendar → gold_intel_events (research-only, does NOT touch macro_events)
  - Provisional GC-XAU basis (only if both timestamps within tolerance)
  - Confidence gating: BULLISH/BEARISH/NEUTRAL/CONFLICTED/NO_EDGE/INCOMPLETE/STAND_ASIDE
"""
import hashlib
import json
import os
import urllib.request
from datetime import datetime, timezone, timedelta, date
from database import SessionLocal
from sqlalchemy import text

# ═══════════════════════════════════════════════════════════════════════
# SECTION A — Data source pullers  (research-only)
# ═══════════════════════════════════════════════════════════════════════

def refresh_gc_bars(db, contracts=(("GC1!", "COMEX"), ("GCZ2026", "COMEX"))):
    """Pull GC futures bars from TradingView anonymous into gold_intel_gc_bars."""
    from tvDatafeed import TvDatafeed, Interval
    tv = TvDatafeed()
    tf_map = {"D1": Interval.in_daily, "H1": Interval.in_1_hour,
              "M15": Interval.in_15_minute, "M5": Interval.in_5_minute}
    written = 0
    for sym, exch in contracts:
        for tf_label, tf_iv in tf_map.items():
            try:
                df = tv.get_hist(symbol=sym, exchange=exch, interval=tf_iv, n_bars=300)
            except Exception as e:
                print(f"  [ERR] GC {sym}@{exch} {tf_label}: {e}")
                continue
            if df is None or df.empty: continue
            for ts, row in df.iterrows():
                bt = ts.strftime("%Y-%m-%d %H:%M:%S")
                db.execute(text("""
                    INSERT OR IGNORE INTO gold_intel_gc_bars (
                        contract, timeframe, bar_time_utc,
                        open, high, low, close, volume,
                        provider, exchange
                    ) VALUES (
                        :c, :tf, :bt, :o, :h, :l, :cl, :v,
                        'tradingview_anon', :exch
                    )
                """), {"c": sym, "tf": tf_label, "bt": bt,
                        "o": float(row["open"]), "h": float(row["high"]),
                        "l": float(row["low"]), "cl": float(row["close"]),
                        "v": int(row.get("volume", 0) or 0), "exch": exch})
                written += 1
            db.commit()
    return written


def refresh_events(db):
    """Pull ForexFactory this-week calendar into gold_intel_events."""
    url = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            rows = json.loads(r.read())
    except Exception as e:
        return {"status": "DEGRADED", "reason": str(e), "written": 0}
    if not isinstance(rows, list):
        return {"status": "DEGRADED", "reason": "non-list payload", "written": 0}

    # Classify gold relevance
    CORE = {"CPI","NFP","FOMC","Federal Funds","Rate Decision","PCE","Powell",
            "Non-Farm","Core CPI","Core PCE","GDP","PPI","Retail Sales"}
    def relevance(country, title):
        t = (title or "").lower()
        if country in ("USD",):
            for kw in CORE:
                if kw.lower() in t: return "CORE"
            if any(k in t for k in ("jobless","claims","ism","fed","treasury","auction")):
                return "CONTEXT"
            return "LOW"
        if country in ("EUR","JPY","GBP","CNY") and any(kw.lower() in t for kw in ("cpi","gdp","rate")):
            return "CONTEXT"
        return "LOW"

    written = 0
    for r_ in rows:
        title = r_.get("title") or ""
        country = r_.get("country") or ""
        date_str = r_.get("date") or ""
        if not (title and date_str): continue
        # date_str is ISO 8601 with tz. Normalise to UTC.
        try:
            dt = datetime.fromisoformat(date_str)
            dt_utc = dt.astimezone(timezone.utc)
        except Exception:
            continue
        impact = (r_.get("impact") or "").lower()
        forecast = r_.get("forecast") or ""
        prev = r_.get("previous") or ""
        actual = r_.get("actual") or ""
        rel = relevance(country, title)
        row_hash = hashlib.md5(f"{country}|{title}|{dt_utc.isoformat()}|{forecast}|{prev}|{actual}".encode()).hexdigest()[:16]
        db.execute(text("""
            INSERT OR REPLACE INTO gold_intel_events (
                event_time_utc, currency, event_title, impact,
                forecast, previous_value, actual_value,
                source, source_row_hash, gold_relevance, last_updated_utc
            ) VALUES (
                :t, :c, :title, :imp, :f, :p, :a,
                'forexfactory', :h, :rel, :now
            )
        """), {"t": dt_utc.strftime("%Y-%m-%d %H:%M:%S"), "c": country, "title": title,
                "imp": impact, "f": forecast, "p": prev, "a": actual,
                "h": row_hash, "rel": rel,
                "now": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")})
        written += 1
    db.commit()
    return {"status": "OK", "written": written}


# ═══════════════════════════════════════════════════════════════════════
# SECTION B — Trading-day / session utilities
# ═══════════════════════════════════════════════════════════════════════

def current_xauusd_from_ticks(db, freshness_threshold_s=300):
    """Live XAUUSD from mt5_ticks (Track A). Returns dict with source + age."""
    r = db.execute(text(
        "SELECT tick_time_utc, bid, ask FROM mt5_ticks ORDER BY tick_time_msc DESC LIMIT 1"
    )).fetchone()
    if not r:
        return {"status": "NO_DATA", "source": "mt5_ticks"}
    ts, bid, ask = r
    try:
        dt = datetime.fromisoformat(str(ts))
        if dt.tzinfo is None: dt = dt.replace(tzinfo=timezone.utc)
    except Exception:
        dt = None
    age_s = (datetime.now(timezone.utc) - dt).total_seconds() if dt else None
    fresh = age_s is not None and age_s <= freshness_threshold_s
    mid = (bid + ask) / 2.0
    return {
        "status": "LIVE" if fresh else "STALE",
        "source": "mt5_ticks (Exness bid/ask)",
        "tick_time_utc": str(ts),
        "bid": bid, "ask": ask, "mid": round(mid, 3),
        "spread_pts": round(ask - bid, 3),
        "age_seconds": round(age_s, 1) if age_s is not None else None,
        "freshness_threshold_s": freshness_threshold_s,
    }


def _weekday_of(day_iso):
    """Return weekday (0=Mon...6=Sun) for a YYYY-MM-DD string."""
    return datetime.strptime(day_iso, "%Y-%m-%d").weekday()


def previous_trading_day(db, ref_date=None):
    """Return the previous trading day for XAU/USD.

    Definition:  the most recent UTC date strictly < ref_date on which the
    historical_candles table has ≥ 6 H1 bars (a session-day cutoff that
    naturally excludes Saturdays; Sundays with the ~22:00 UTC open have
    only 2 bars and are also skipped).
    """
    ref = ref_date or date.today().isoformat()
    r = db.execute(text("""
        SELECT DATE(candle_time) AS d, COUNT(*) AS n
        FROM historical_candles
        WHERE instrument='XAU/USD' AND timeframe='H1'
          AND DATE(candle_time) < :ref
        GROUP BY d
        HAVING n >= 6
        ORDER BY d DESC
        LIMIT 1
    """), {"ref": ref}).fetchone()
    if not r:
        return {"status": "NO_DATA"}
    d, n = r
    ext = db.execute(text("""
        SELECT MAX(high), MIN(low), MIN(candle_time), MAX(candle_time)
        FROM historical_candles
        WHERE instrument='XAU/USD' AND timeframe='H1' AND DATE(candle_time) = :d
    """), {"d": d}).fetchone()
    return {"status": "OK",
            "session_date_utc": d,
            "PDH": ext[0], "PDL": ext[1],
            "session_start_utc": str(ext[2]),
            "session_end_utc": str(ext[3]),
            "n_h1_bars": n,
            "convention": "trading-day = UTC date with ≥ 6 H1 bars, excluding Sat + Sun-open"}


def previous_trading_week(db, ref_date=None):
    """Previous completed trading week (Mon–Fri).

    Given `today` = ref_date, the previous trading week is:
      - The Mon..Fri UTC block whose Friday is the last full Friday
        strictly BEFORE the current ISO week's Monday.
    We use historical_candles H1 bars restricted to Mon..Fri UTC.
    """
    ref = datetime.fromisoformat(ref_date + "T00:00:00").date() if ref_date else date.today()
    # Monday of current ISO week (Python: Mon=0)
    days_back = ref.weekday()          # 0..6, Mon..Sun
    monday_this_week = ref - timedelta(days=days_back)
    friday_prev_week = monday_this_week - timedelta(days=3)   # Mon → Fri
    monday_prev_week = friday_prev_week - timedelta(days=4)
    r = db.execute(text("""
        SELECT MAX(high), MIN(low), MIN(candle_time), MAX(candle_time), COUNT(*)
        FROM historical_candles
        WHERE instrument='XAU/USD' AND timeframe='H1'
          AND DATE(candle_time) BETWEEN :mon AND :fri
    """), {"mon": monday_prev_week.isoformat(),
            "fri": friday_prev_week.isoformat()}).fetchone()
    if not r or r[0] is None:
        return {"status": "NO_DATA",
                "week_boundary": f"{monday_prev_week} → {friday_prev_week}"}
    return {"status": "OK",
            "PWH": r[0], "PWL": r[1],
            "week_start_utc": str(r[2]),
            "week_end_utc":   str(r[3]),
            "n_h1_bars":      r[4],
            "week_boundary":  f"{monday_prev_week} → {friday_prev_week}",
            "convention":     "PWH/PWL = max/min of H1 across Mon..Fri UTC of last completed week"}


def asian_session_range(db, ref_date=None, from_ticks=True):
    """Asian session — 22:00 UTC prev calendar day → 06:00 UTC current day.

    Uses mt5_ticks if present within the window; falls back to H1 bars.
    """
    ref = date.fromisoformat(ref_date) if ref_date else datetime.now(timezone.utc).date()
    start = datetime.combine(ref - timedelta(days=1), datetime.min.time()).replace(hour=22, tzinfo=timezone.utc)
    end   = datetime.combine(ref,                       datetime.min.time()).replace(hour=6,  tzinfo=timezone.utc)
    # From ticks (preferred)
    if from_ticks:
        r = db.execute(text("""
            SELECT MAX(bid), MIN(bid), MIN(tick_time_utc), MAX(tick_time_utc), COUNT(*)
            FROM mt5_ticks
            WHERE tick_time_utc BETWEEN :s AND :e
        """), {"s": start.strftime("%Y-%m-%d %H:%M:%S"),
                "e": end.strftime("%Y-%m-%d %H:%M:%S")}).fetchone()
        if r and r[0] and r[4] and r[4] >= 60:
            hi = r[0]; lo = r[1]
            return {
                "status": "OK",
                "source": "mt5_ticks",
                "session_start_utc": start.isoformat(),
                "session_end_utc":   end.isoformat(),
                "FACT_high":     hi,
                "FACT_low":      lo,
                "DERIVED_range": round(hi - lo, 3),
                "DERIVED_mid":   round((hi + lo)/2.0, 3),
                "n_ticks":       r[4],
                "first_tick":    str(r[2]),
                "last_tick":     str(r[3]),
                "INFERENCE_note":"Liquidity commentary intentionally omitted at v1.1",
            }
    # Fallback: H1
    r = db.execute(text("""
        SELECT MAX(high), MIN(low), MIN(candle_time), MAX(candle_time), COUNT(*)
        FROM historical_candles
        WHERE instrument='XAU/USD' AND timeframe='H1'
          AND candle_time BETWEEN :s AND :e
    """), {"s": start.strftime("%Y-%m-%d %H:%M:%S"),
            "e": end.strftime("%Y-%m-%d %H:%M:%S")}).fetchone()
    if not r or r[0] is None:
        return {"status": "NOT_AVAILABLE",
                "reason": "no ticks or H1 bars inside 22:00-06:00 UTC window"}
    hi, lo = r[0], r[1]
    return {"status": "OK", "source": "historical_candles/H1",
            "session_start_utc": start.isoformat(),
            "session_end_utc":   end.isoformat(),
            "FACT_high": hi, "FACT_low": lo,
            "DERIVED_range": round(hi - lo, 3),
            "DERIVED_mid":   round((hi+lo)/2.0, 3),
            "n_h1_bars": r[4]}


# ═══════════════════════════════════════════════════════════════════════
# SECTION C — Structural read-in (READ ONLY, no coupling)
# ═══════════════════════════════════════════════════════════════════════

def structural_read(db, freshness_thresh_min=90):
    """Read (do not modify) recent structural outputs already produced by
    the running strategist / vp_trap / regime pipelines.

    We deliberately do NOT recompute them.
    """
    out = {}
    # Regime + HTF alignment as observed by strategist_verdicts
    r = db.execute(text("""
        SELECT tf_alignment_label, market_state, session_classification, created_at
        FROM strategist_verdicts ORDER BY id DESC LIMIT 1
    """)).fetchone()
    if r:
        try:
            ts = datetime.fromisoformat(str(r[3]))
            if ts.tzinfo is None: ts = ts.replace(tzinfo=timezone.utc)
            age_min = (datetime.now(timezone.utc) - ts).total_seconds()/60.0
        except Exception:
            age_min = None
        out["strategist_pulse"] = {
            "FACT_tf_alignment_label":   r[0],
            "FACT_market_state":         r[1],
            "FACT_session_classification": r[2],
            "observed_at_utc":           str(r[3]),
            "age_minutes":               round(age_min, 1) if age_min else None,
            "status": "STALE" if (age_min and age_min > freshness_thresh_min) else "OK",
        }
    else:
        out["strategist_pulse"] = {"status": "NO_DATA"}

    # VP Trap zones (read-only)
    r = db.execute(text("""
        SELECT level_type, level_side, reference_price, state,
               state_reason, created_at
        FROM vp_trap_zones ORDER BY id DESC LIMIT 6
    """)).fetchall()
    zones = []
    for x in r:
        zones.append({"level_type": x[0], "side": x[1], "reference_price": x[2],
                       "state": x[3], "state_reason": x[4],
                       "created_at": str(x[5])})
    out["vp_trap_zones_recent"] = {"status": "OK" if zones else "NO_DATA",
                                     "zones": zones[:6]}

    # market_intelligence_alerts (read-only)
    r = db.execute(text("""
        SELECT alert_type, directional_assessment,
               directional_confidence, opportunity_status, ts
        FROM market_intelligence_alerts ORDER BY id DESC LIMIT 3
    """)).fetchall()
    alerts = []
    for x in r:
        alerts.append({"type": x[0], "directional_assessment": x[1],
                        "directional_confidence": x[2],
                        "opportunity_status": x[3], "ts": str(x[4])})
    out["market_intelligence_alerts_recent"] = {"status": "OK" if alerts else "NO_DATA",
                                                  "alerts": alerts}
    return out


# ═══════════════════════════════════════════════════════════════════════
# SECTION D — GC / XAU basis (provisional)
# ═══════════════════════════════════════════════════════════════════════

def provisional_basis(db, max_lag_min=5):
    """PROVISIONAL basis = latest GC front-month H1 close − nearest XAU spot mid.

    Only publishes if the two observations are within `max_lag_min` minutes.
    """
    # Latest GC H1 close
    gc = db.execute(text("""
        SELECT bar_time_utc, close FROM gold_intel_gc_bars
        WHERE contract='GC1!' AND timeframe='H1'
        ORDER BY bar_time_utc DESC LIMIT 1
    """)).fetchone()
    if not gc:
        return {"status": "NOT_AVAILABLE", "reason": "no GC bars ingested"}
    gc_ts, gc_close = gc
    try:
        gc_dt = datetime.fromisoformat(str(gc_ts))
        if gc_dt.tzinfo is None: gc_dt = gc_dt.replace(tzinfo=timezone.utc)
    except Exception:
        return {"status": "NOT_AVAILABLE", "reason": "GC ts parse"}

    # Latest MT5 tick around gc_dt (±max_lag_min minutes)
    r = db.execute(text("""
        SELECT bid, ask, tick_time_utc FROM mt5_ticks
        WHERE tick_time_utc BETWEEN :s AND :e
        ORDER BY ABS(strftime('%s', tick_time_utc) - strftime('%s', :ref)) ASC
        LIMIT 1
    """), {"s": (gc_dt - timedelta(minutes=max_lag_min)).strftime("%Y-%m-%d %H:%M:%S"),
            "e": (gc_dt + timedelta(minutes=max_lag_min)).strftime("%Y-%m-%d %H:%M:%S"),
            "ref": gc_dt.strftime("%Y-%m-%d %H:%M:%S")}).fetchone()
    if not r:
        return {"status": "NOT_AVAILABLE",
                "reason": f"no MT5 tick within ±{max_lag_min} min of GC obs {gc_ts}"}
    xau_mid = (r[0] + r[1]) / 2.0
    basis = gc_close - xau_mid
    return {"status": "PROVISIONAL",
            "gc_contract":   "GC1! (front-month continuous)",
            "gc_obs_utc":    str(gc_ts),
            "gc_close":      gc_close,
            "xau_ref_source":"mt5_ticks nearest tick",
            "xau_obs_utc":   str(r[2]),
            "xau_mid":       round(xau_mid, 3),
            "PROVISIONAL_basis_pts": round(basis, 3),
            "match_window_min": max_lag_min,
            "note": "PROVISIONAL — mapped-strike overlay NOT published; further validation required"}


# ═══════════════════════════════════════════════════════════════════════
# SECTION E — Snapshot composer + classification governance
# ═══════════════════════════════════════════════════════════════════════

def latest_macro(db, sid):
    r = db.execute(text(
        "SELECT obs_date, value, provider FROM macro_series_raw WHERE series_id=:s "
        "ORDER BY obs_date DESC LIMIT 1"), {"s": sid}).fetchone()
    return dict(r._mapping) if r else None


def cftc_latest(db):
    r = db.execute(text("""
        SELECT d.*, r.market_and_exchange_names
        FROM cftc_cot_derived d JOIN cftc_cot_raw r ON r.id = d.raw_id
        WHERE d.report_type='FUT_ONLY'
        ORDER BY d.report_date_yyyy_mm_dd DESC LIMIT 1
    """)).fetchone()
    return dict(r._mapping) if r else None


def upcoming_events(db, days=7):
    now = datetime.now(timezone.utc)
    horizon = (now + timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
    r = db.execute(text("""
        SELECT event_time_utc, currency, event_title, impact, forecast, previous_value, gold_relevance
        FROM gold_intel_events
        WHERE event_time_utc >= :now AND event_time_utc <= :hz
          AND (gold_relevance='CORE' OR (gold_relevance='CONTEXT' AND impact='high'))
        ORDER BY event_time_utc ASC
    """), {"now": now.strftime("%Y-%m-%d %H:%M:%S"), "hz": horizon}).fetchall()
    return [{"time_utc": x[0], "currency": x[1], "event": x[2],
              "impact": x[3], "forecast": x[4], "previous": x[5], "gold_relevance": x[6]}
             for x in r]


def track_a_health(db):
    r = db.execute(text(
        "SELECT COUNT(*), MAX(tick_time_utc), MAX(tick_time_msc) FROM mt5_ticks"
    )).fetchone()
    if not r or not r[0]:
        return {"status": "NO_DATA"}
    now_ms = datetime.now(timezone.utc).timestamp() * 1000
    lag_s = (now_ms - (r[2] or 0)) / 1000 if r[2] else None
    return {"total_ticks": r[0], "last_ts": str(r[1]),
            "lag_seconds": round(lag_s, 1) if lag_s else None,
            "status": "HEALTHY" if lag_s is not None and lag_s < 300 else "DEGRADED"}


# ── classification governance ──
def classify(snapshot):
    """
    Confidence-gated classification.
    Returns one of: BULLISH, BEARISH, NEUTRAL, CONFLICTED, NO_EDGE,
                    INCOMPLETE, STAND_ASIDE.
    """
    obs = []          # layers with usable, fresh data
    stale = []
    missing = []
    critical_missing = []

    def rate(name, ok, stale_flag=False, critical=False):
        if ok and not stale_flag:
            obs.append(name)
        elif stale_flag:
            stale.append(name)
            if critical: critical_missing.append(name)
        else:
            missing.append(name)
            if critical: critical_missing.append(name)

    xau = snapshot["data_health"]["XAUUSD_feed"]
    rate("XAUUSD_CURRENT", xau["status"] in ("LIVE",),
         stale_flag=(xau["status"] == "STALE"),
         critical=True)

    struct = snapshot["market_structure"]
    struct_ok = (
        struct.get("PDH", {}).get("status") == "OK" and
        struct.get("PWH", {}).get("status") == "OK" and
        struct.get("strategist_pulse", {}).get("status") == "OK"
    )
    struct_stale = (
        struct.get("strategist_pulse", {}).get("status") == "STALE"
    )
    rate("STRUCTURE", struct_ok, stale_flag=struct_stale, critical=True)

    macro = snapshot["macro"]
    macro_ok = any(macro.get(k, {}).get("FACT_value") is not None
                    for k in ("DXY", "UST_10Y", "UST_REAL10Y"))
    rate("MACRO", macro_ok)

    pos_ok = snapshot["positioning"].get("FACT_position_date") is not None
    rate("POSITIONING", pos_ok)

    ev = snapshot["event_risk"]["status"]
    rate("EVENTS", ev == "OK", stale_flag=(ev == "DEGRADED"))

    rate("OPTIONS", False)     # not observed
    rate("PHYSICAL", False)
    rate("GC_MICROSTRUCTURE", False)

    # Governance:
    if critical_missing:
        return {"CLASSIFICATION": "INCOMPLETE",
                "reason": f"Critical layer(s) missing/stale: {critical_missing}",
                "observed_layers": obs, "missing_layers": missing,
                "stale_layers": stale, "critical_missing_inputs": critical_missing}

    if len(obs) < 3:
        return {"CLASSIFICATION": "INCOMPLETE",
                "reason": f"Fewer than 3 independent observable layers ({len(obs)})",
                "observed_layers": obs, "missing_layers": missing,
                "stale_layers": stale, "critical_missing_inputs": critical_missing}

    # Now attempt a substantive read only if structure is OK.
    tf_lab = (struct.get("strategist_pulse", {}).get("FACT_tf_alignment_label") or "").lower()
    real10 = macro.get("UST_REAL10Y", {})
    dxy    = macro.get("DXY", {})
    struct_bias = ("BULL" if "bull" in tf_lab else
                    "BEAR" if "bear" in tf_lab else
                    "CONFLICTED" if "conflict" in tf_lab else "NEUTRAL")
    macro_bias = (
        "BEAR" if (real10.get("DERIVED_daily_change") or 0) > 0.02 and (dxy.get("DERIVED_daily_change") or 0) > 0.15
        else "BULL" if (real10.get("DERIVED_daily_change") or 0) < -0.02 and (dxy.get("DERIVED_daily_change") or 0) < -0.15
        else "MIXED"
    )

    # Positioning caution flag
    p5 = snapshot["positioning"].get("DERIVED_MM_net_pctile_5y")
    pos_caution = None
    if p5 is not None:
        pos_caution = "CROWDED_LONG" if p5 >= 0.90 else ("CROWDED_SHORT" if p5 <= 0.10 else "NORMAL")

    if struct_bias == "CONFLICTED" or (struct_bias == "BULL" and macro_bias == "BEAR") or \
       (struct_bias == "BEAR" and macro_bias == "BULL"):
        return {"CLASSIFICATION": "CONFLICTED",
                "reason": f"structure={struct_bias} × macro={macro_bias}",
                "observed_layers": obs, "missing_layers": missing,
                "stale_layers": stale, "critical_missing_inputs": critical_missing}

    if struct_bias == "BULL" and macro_bias in ("BULL","MIXED"):
        # Only BULLISH if positioning not crowded long
        if pos_caution == "CROWDED_LONG":
            return {"CLASSIFICATION": "CONFLICTED",
                    "reason": "macro+structure supportive but positioning crowded long",
                    "observed_layers": obs, "missing_layers": missing,
                    "stale_layers": stale, "critical_missing_inputs": critical_missing}
        return {"CLASSIFICATION": "BULLISH", "reason": f"structure=BULL, macro={macro_bias}, positioning={pos_caution}",
                "observed_layers": obs, "missing_layers": missing,
                "stale_layers": stale, "critical_missing_inputs": critical_missing}

    if struct_bias == "BEAR" and macro_bias in ("BEAR","MIXED"):
        if pos_caution == "CROWDED_SHORT":
            return {"CLASSIFICATION": "CONFLICTED",
                    "reason": "macro+structure bearish but positioning crowded short",
                    "observed_layers": obs, "missing_layers": missing,
                    "stale_layers": stale, "critical_missing_inputs": critical_missing}
        return {"CLASSIFICATION": "BEARISH", "reason": f"structure=BEAR, macro={macro_bias}, positioning={pos_caution}",
                "observed_layers": obs, "missing_layers": missing,
                "stale_layers": stale, "critical_missing_inputs": critical_missing}

    return {"CLASSIFICATION": "NO_EDGE",
            "reason": f"structure={struct_bias}, macro={macro_bias}, no clear asymmetry",
            "observed_layers": obs, "missing_layers": missing,
            "stale_layers": stale, "critical_missing_inputs": critical_missing}


# ── main ──
def main():
    with SessionLocal() as db:
        print("=" * 78)
        print("PHASE-1 CLOSURE — Refreshing GC + Events + composing V1.1 snapshot")
        print("=" * 78)

        # A. Pulls
        run_ids = {}
        started = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        gc_written = refresh_gc_bars(db)
        db.execute(text("INSERT INTO research_pipeline_runs "
            "(pipeline_name, started_at_utc, finished_at_utc, status, detail, rows_written) "
            "VALUES ('gc_pull', :s, :f, 'OK', 'TV COMEX GC1! + GCZ2026', :n)"),
            {"s": started, "f": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
             "n": gc_written})
        db.commit()
        print(f"  GC bars pulled: {gc_written}")

        started2 = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        ev = refresh_events(db)
        db.execute(text("INSERT INTO research_pipeline_runs "
            "(pipeline_name, started_at_utc, finished_at_utc, status, detail, rows_written) "
            "VALUES ('events_pull', :s, :f, :st, :d, :n)"),
            {"s": started2, "f": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
             "st": ev["status"], "d": ev.get("reason", ""),
             "n": ev.get("written", 0)})
        db.commit()
        print(f"  Events pull: {ev}")

        # B. Compose snapshot
        snap = {
            "snapshot_ts_utc": datetime.now(timezone.utc).isoformat(),
            "version": "GOLD_INTEL_V1.1",
        }

        # DATA HEALTH
        xau_now = current_xauusd_from_ticks(db)
        gc_latest = db.execute(text(
            "SELECT bar_time_utc, close FROM gold_intel_gc_bars "
            "WHERE contract='GC1!' AND timeframe='H1' ORDER BY bar_time_utc DESC LIMIT 1"
        )).fetchone()
        gc_age_hr = None
        if gc_latest:
            try:
                gc_dt = datetime.fromisoformat(str(gc_latest[0]))
                if gc_dt.tzinfo is None: gc_dt = gc_dt.replace(tzinfo=timezone.utc)
                gc_age_hr = (datetime.now(timezone.utc) - gc_dt).total_seconds()/3600
            except Exception: pass

        cot = cftc_latest(db)
        cot_age = None
        if cot and cot.get("report_date_yyyy_mm_dd"):
            try:
                cot_age = (date.today() - datetime.strptime(str(cot["report_date_yyyy_mm_dd"]), "%Y-%m-%d").date()).days
            except Exception: pass

        last_ev_pull = db.execute(text(
            "SELECT MAX(finished_at_utc) FROM research_pipeline_runs "
            "WHERE pipeline_name='events_pull' AND status='OK'"
        )).scalar()

        snap["data_health"] = {
            "XAUUSD_feed":    xau_now,
            "GC_feed":        {"status": "LIVE" if gc_age_hr is not None and gc_age_hr < 3 else "STALE",
                                "latest_bar_utc": str(gc_latest[0]) if gc_latest else None,
                                "close": gc_latest[1] if gc_latest else None,
                                "age_hours": round(gc_age_hr, 2) if gc_age_hr else None,
                                "source": "TradingView COMEX:GC1! (front-month continuous)"},
            "Macro_pipeline": {"status": "OK — Treasury Direct + TradingView(anon)"},
            "CFTC_pipeline":  {"status": "OK", "position_date": str(cot["report_date_yyyy_mm_dd"]) if cot else None,
                                "age_days": cot_age},
            "Event_calendar": {"status": ev.get("status", "UNKNOWN"),
                                "last_refresh_utc": last_ev_pull,
                                "gold_relevant_upcoming": None},   # filled below
            "Track_A":        track_a_health(db),
        }

        # MARKET STRUCTURE
        pdh = previous_trading_day(db)
        pwh = previous_trading_week(db)
        asia = asian_session_range(db)
        struct = structural_read(db)
        snap["market_structure"] = {
            "current_price":            xau_now,
            "PDH":                      pdh,
            "PWH":                      pwh,
            "Asian_session_range":      asia,
            "strategist_pulse":         struct["strategist_pulse"],
            "vp_trap_zones_recent":     struct["vp_trap_zones_recent"],
            "market_intelligence_alerts_recent": struct["market_intelligence_alerts_recent"],
        }

        # MACRO
        macro_out = {}
        for sid, label in [
            ("DXY","US Dollar Index"),
            ("UST_2Y","US 2Y Treasury Nominal"),
            ("UST_10Y","US 10Y Treasury Nominal"),
            ("UST_REAL10Y","US 10Y Real Yield (TIPS)"),
            ("US_BE_10Y_DERIVED","US 10Y Breakeven (DERIVED)"),
            ("WTI","WTI Crude Oil"),
            ("VIX","CBOE VIX"),
            ("MOVE","ICE BofA MOVE (Treasury vol)"),
            ("USDCNH","USD/CNH offshore yuan"),
            ("USDJPY","USD/JPY"),
        ]:
            cur = latest_macro(db, sid)
            # 1-obs-back for daily change
            back = db.execute(text(
                "SELECT obs_date, value FROM macro_series_raw WHERE series_id=:s "
                "ORDER BY obs_date DESC LIMIT 2 OFFSET 1"), {"s": sid}).fetchone()
            change = None
            if cur and back:
                change = cur["value"] - back[1]
            age = None
            if cur and isinstance(cur["obs_date"], str):
                age = (date.today() - datetime.strptime(cur["obs_date"], "%Y-%m-%d").date()).days
            macro_out[sid] = {
                "label": label,
                "FACT_value": cur["value"] if cur else None,
                "obs_date": cur["obs_date"] if cur else None,
                "provider": cur["provider"] if cur else None,
                "age_days": age,
                "DERIVED_daily_change": change,
            }
        macro_out["FED_FUNDS_EXPECT"] = {"status": "NOT_AVAILABLE",
            "reason": "CME/paid — Phase 1 gap kept explicit"}
        macro_out["SOFR_CURVE"] = {"status": "NOT_AVAILABLE",
            "reason": "CME/paid"}
        macro_out["FRED_direct_breakevens"] = {"status": "NOT_AVAILABLE",
            "reason": "FRED endpoint unreachable from droplet (optional enhancement)"}
        snap["macro"] = macro_out

        # POSITIONING
        if cot:
            snap["positioning"] = {
                "FACT_position_date": str(cot["report_date_yyyy_mm_dd"]),
                "FACT_open_interest_all": cot["oi_all"],
                "FACT_MM_long":    cot["m_money_long"],
                "FACT_MM_short":   cot["m_money_short"],
                "DERIVED_MM_net":  cot["m_money_net"],
                "DERIVED_ΔMM_long_1W":  cot["d_m_money_long_1w"],
                "DERIVED_ΔMM_short_1W": cot["d_m_money_short_1w"],
                "DERIVED_ΔMM_net_1W":   cot["d_m_money_net_1w"],
                "DERIVED_MM_net_pct_OI":cot["m_money_net_pct_oi"],
                "DERIVED_MM_net_pctile_1y": cot["m_money_net_pctile_1y"],
                "DERIVED_MM_net_pctile_5y": cot["m_money_net_pctile_5y"],
                "DERIVED_Prod_Merc_net":   cot["prod_merc_net"],
                "DERIVED_Swap_Dealer_net": cot["swap_net"],
                "classification_state": "NOT_APPLIED_v1: pending threshold review",
            }
        else:
            snap["positioning"] = {"status": "NO_DATA"}

        # OPTIONS / PHYSICAL / GC MICRO / TRACK A
        snap["options"]  = {"status": "NOT_OBSERVED", "note": "Phase 2 inactive"}
        snap["physical"] = {"status": "NOT_OBSERVED"}
        snap["gc_microstructure"] = {"status": "NOT_OBSERVED — Track B HOLD"}
        snap["exness_quote_microstructure"] = {"status": "CAPTURING (observation-mode)",
            **track_a_health(db)}

        # EVENTS
        upcoming = upcoming_events(db, days=7)
        snap["data_health"]["Event_calendar"]["gold_relevant_upcoming"] = len(upcoming)
        snap["event_risk"] = {"status": ev.get("status", "UNKNOWN"),
                                "last_refresh_utc": last_ev_pull,
                                "upcoming_gold_relevant_7d": upcoming[:8]}

        # PROVISIONAL BASIS
        snap["provisional_basis"] = provisional_basis(db, max_lag_min=60)

        # CLASSIFICATION — governed
        cls = classify(snap)
        snap["classification"] = cls

        # Scenarios only when structure OK
        if cls["CLASSIFICATION"] in ("BULLISH", "BEARISH", "NEUTRAL", "CONFLICTED", "NO_EDGE"):
            # Build simple scenarios from PDH/PDL/PWH/PWL levels
            pdh_v = snap["market_structure"]["PDH"].get("PDH")
            pdl_v = snap["market_structure"]["PDH"].get("PDL")
            pwh_v = snap["market_structure"]["PWH"].get("PWH")
            pwl_v = snap["market_structure"]["PWH"].get("PWL")
            asia_h = snap["market_structure"]["Asian_session_range"].get("FACT_high")
            asia_l = snap["market_structure"]["Asian_session_range"].get("FACT_low")
            snap["scenarios"] = {
                "BULLISH": {"trigger": f"Close above prior-day high {pdh_v} on H1",
                            "next_liquidity": f"PWH {pwh_v}",
                            "invalidation": f"Rejection back below Asian low {asia_l}"},
                "BEARISH": {"trigger": f"Close below prior-day low {pdl_v} on H1",
                            "next_liquidity": f"PWL {pwl_v}",
                            "invalidation": f"Reclaim above Asian high {asia_h}"},
                "STAND_ASIDE": "High-impact event within 30 min; large spread; missing structure",
            }
        else:
            snap["scenarios"] = {"status": "NOT_GENERATED — structure not healthy"}

        # Persist
        db.execute(text("""
            INSERT INTO gold_daily_intel_snapshots
              (snapshot_ts_utc, cover_date, conclusion, payload_json, version)
            VALUES (:ts, :cd, :con, :js, :ver)
        """), {"ts": snap["snapshot_ts_utc"],
                "cd": str(date.today()),
                "con": cls["CLASSIFICATION"],
                "js": json.dumps(snap, indent=2, default=str),
                "ver": snap["version"]})
        db.commit()

        # Print compact human view
        print()
        print("─" * 80)
        print(f"CLASSIFICATION: {cls['CLASSIFICATION']}  ({cls['reason']})")
        print("─" * 80)
        print(f"observed: {cls['observed_layers']}")
        print(f"stale:    {cls['stale_layers']}")
        print(f"missing:  {cls['missing_layers']}")
        print(f"critical: {cls['critical_missing_inputs']}")
        print()
        print("--- DATA HEALTH ---")
        for k, v in snap["data_health"].items():
            print(f"  {k}: {v}")
        print("--- MARKET STRUCTURE (compact) ---")
        print(f"  current_price: {xau_now}")
        print(f"  PDH: {pdh}")
        print(f"  PWH: {pwh}")
        print(f"  Asian_session: {asia}")
        print(f"  strategist_pulse: {snap['market_structure']['strategist_pulse']}")
        print("--- PROVISIONAL BASIS ---")
        print(f"  {snap['provisional_basis']}")
        print("--- UPCOMING EVENTS (7d, gold-relevant) ---")
        for e in upcoming[:8]:
            print(f"  {e['time_utc']} {e['currency']:<4} impact={e['impact']:<6} rel={e['gold_relevance']:<7}  {e['event'][:70]}")

if __name__ == "__main__":
    main()
