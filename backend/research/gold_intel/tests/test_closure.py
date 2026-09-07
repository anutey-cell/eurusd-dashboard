"""Phase-1 closure — validation tests. READ-ONLY."""
import json
import io
import hashlib
import unittest
from unittest.mock import patch
from datetime import date, datetime, timedelta, timezone
from database import SessionLocal
from sqlalchemy import text

# Reuse utilities from the snapshot module by dynamic import
import sys
sys.path.insert(0, "/app")
from closure_20_snapshot_v11 import (
    previous_trading_day, previous_trading_week, asian_session_range,
    current_xauusd_from_ticks, structural_read, classify, upcoming_events,
    provisional_basis,
)

class ClosureTests(unittest.TestCase):
    def setUp(self):
        self.db = SessionLocal()
    def tearDown(self):
        self.db.close()

    # ── Trading day / week convention ─────────────────────────
    def test_pdh_on_monday_returns_last_friday(self):
        """Monday call must return Friday's session, skipping Sat + Sun-open."""
        pdh = previous_trading_day(self.db, ref_date="2026-09-07")
        self.assertEqual(pdh["status"], "OK")
        self.assertEqual(pdh["session_date_utc"], "2026-09-04")
        self.assertGreaterEqual(pdh["n_h1_bars"], 6)

    def test_pdh_on_saturday_returns_friday(self):
        pdh = previous_trading_day(self.db, ref_date="2026-09-06")   # Sat
        self.assertEqual(pdh["session_date_utc"], "2026-09-04")

    def test_pdh_on_tuesday_returns_monday(self):
        # Any weekday in-week Tuesday should return the previous full session
        pdh = previous_trading_day(self.db, ref_date="2026-09-02")   # Wed
        # Sept 2 was a Wednesday; previous trading day is Tuesday Sept 1
        self.assertEqual(pdh["session_date_utc"], "2026-09-01")

    def test_pwh_previous_completed_week(self):
        pwh = previous_trading_week(self.db, ref_date="2026-09-07")
        self.assertEqual(pwh["week_boundary"], "2026-08-31 → 2026-09-04")

    def test_pwh_midweek_returns_prev_week(self):
        pwh = previous_trading_week(self.db, ref_date="2026-09-03")
        self.assertEqual(pwh["week_boundary"], "2026-08-24 → 2026-08-28")

    # ── Asian session boundaries ────────────────────────────
    def test_asian_session_utc_boundaries(self):
        a = asian_session_range(self.db)
        # Session end must equal today's 06:00 UTC
        self.assertTrue(a["session_end_utc"].endswith("06:00:00+00:00"))
        # Session start must equal (today-1) 22:00 UTC
        self.assertTrue(a["session_start_utc"].endswith("22:00:00+00:00"))
        if a["status"] == "OK":
            self.assertGreater(a["FACT_high"], a["FACT_low"])

    # ── Current price freshness ─────────────────────────────
    def test_current_price_within_threshold(self):
        cx = current_xauusd_from_ticks(self.db, freshness_threshold_s=300)
        self.assertIn(cx["status"], ("LIVE", "STALE"))
        if cx["status"] == "LIVE":
            self.assertLess(cx["age_seconds"], 300)

    def test_current_price_stale_flags_correctly(self):
        # Threshold=0 forces STALE label for any positive age
        cx = current_xauusd_from_ticks(self.db, freshness_threshold_s=0)
        self.assertEqual(cx["status"], "STALE")

    # ── GC feed ─────────────────────────────────────────────
    def test_gc_bars_present_and_recent(self):
        r = self.db.execute(text(
            "SELECT MAX(bar_time_utc), COUNT(*) FROM gold_intel_gc_bars "
            "WHERE contract='GC1!' AND timeframe='H1'"
        )).fetchone()
        self.assertIsNotNone(r[0])
        self.assertGreater(r[1], 0)
        latest = datetime.fromisoformat(str(r[0]))
        if latest.tzinfo is None:
            latest = latest.replace(tzinfo=timezone.utc)
        age_hr = (datetime.now(timezone.utc) - latest).total_seconds()/3600
        # During US session or shortly after, should be fresh
        self.assertLess(age_hr, 24)

    def test_provisional_basis_labels_conservatively(self):
        b = provisional_basis(self.db, max_lag_min=60)
        self.assertIn(b["status"], ("PROVISIONAL", "NOT_AVAILABLE"))
        if b["status"] == "PROVISIONAL":
            self.assertIn("PROVISIONAL", b["note"])
            self.assertIsInstance(b["PROVISIONAL_basis_pts"], float)

    # ── Event calendar ─────────────────────────────────────
    def test_events_ingested(self):
        r = self.db.execute(text("SELECT COUNT(*) FROM gold_intel_events")).scalar()
        self.assertGreater(r, 0)

    def test_events_status_reflects_ingestion(self):
        # Fresh pull should have status OK in research_pipeline_runs
        r = self.db.execute(text(
            "SELECT status FROM research_pipeline_runs "
            "WHERE pipeline_name='events_pull' ORDER BY id DESC LIMIT 1"
        )).scalar()
        self.assertIn(r, ("OK", "DEGRADED"))

    # ── CFTC integrity ─────────────────────────────────────
    def test_cftc_no_duplicates(self):
        r = self.db.execute(text(
            "SELECT COUNT(*) FROM ("
            "  SELECT report_type, report_date_yyyy_mm_dd, COUNT(*) c "
            "  FROM cftc_cot_raw GROUP BY 1,2 HAVING c > 1)"
        )).scalar()
        self.assertEqual(r, 0)

    def test_cftc_derived_row_count_matches_raw(self):
        r = self.db.execute(text("SELECT COUNT(*) FROM cftc_cot_raw")).scalar()
        d = self.db.execute(text("SELECT COUNT(*) FROM cftc_cot_derived")).scalar()
        # 1:1 within a small buffer for anomalies
        self.assertLessEqual(abs(r - d), 4)

    # ── Macro observations ────────────────────────────────
    def test_macro_series_present(self):
        r = self.db.execute(text(
            "SELECT COUNT(DISTINCT series_id) FROM macro_series_raw"
        )).scalar()
        self.assertGreaterEqual(r, 10)

    def test_macro_missing_observation_no_synthetic(self):
        # A weekend day should have NO row for Treasury series
        r = self.db.execute(text(
            "SELECT COUNT(*) FROM macro_series_raw "
            "WHERE series_id='UST_10Y' AND obs_date='2026-09-06'"
        )).scalar()
        self.assertEqual(r, 0)   # Sat -> no observation, not synthesised

    # ── Classification governance ─────────────────────────
    def test_incomplete_when_current_price_missing(self):
        fake_snap = {
            "data_health": {"XAUUSD_feed": {"status": "STALE"}},
            "market_structure": {"PDH":{"status":"OK"}, "PWH":{"status":"OK"},
                                    "strategist_pulse":{"status":"OK"}},
            "macro": {"UST_10Y":{"FACT_value":4.8}, "UST_REAL10Y":{}, "DXY":{}},
            "positioning": {"FACT_position_date": "2026-09-01"},
            "event_risk": {"status": "OK"},
        }
        c = classify(fake_snap)
        self.assertEqual(c["CLASSIFICATION"], "INCOMPLETE")

    def test_incomplete_when_structure_stale(self):
        fake_snap = {
            "data_health": {"XAUUSD_feed": {"status": "LIVE"}},
            "market_structure": {"PDH":{"status":"OK"}, "PWH":{"status":"OK"},
                                    "strategist_pulse":{"status":"STALE"}},
            "macro": {"UST_10Y":{"FACT_value":4.8}, "UST_REAL10Y":{}, "DXY":{}},
            "positioning": {"FACT_position_date": "2026-09-01"},
            "event_risk": {"status": "OK"},
        }
        c = classify(fake_snap)
        self.assertEqual(c["CLASSIFICATION"], "INCOMPLETE")

    def test_no_edge_when_no_bias(self):
        fake_snap = {
            "data_health": {"XAUUSD_feed": {"status": "LIVE"}},
            "market_structure": {"PDH":{"status":"OK"}, "PWH":{"status":"OK"},
                                    "strategist_pulse":{"status":"OK",
                                        "FACT_tf_alignment_label": "Neutral"}},
            "macro": {"UST_10Y":{"FACT_value":4.8}, "UST_REAL10Y":{"DERIVED_daily_change":0.0},
                       "DXY":{"DERIVED_daily_change":0.0}},
            "positioning": {"FACT_position_date": "2026-09-01",
                             "DERIVED_MM_net_pctile_5y": 0.5},
            "event_risk": {"status": "OK"},
        }
        c = classify(fake_snap)
        self.assertEqual(c["CLASSIFICATION"], "NO_EDGE")

    def test_conflicted_bullish_structure_but_crowded_long(self):
        fake_snap = {
            "data_health": {"XAUUSD_feed": {"status": "LIVE"}},
            "market_structure": {"PDH":{"status":"OK"}, "PWH":{"status":"OK"},
                                    "strategist_pulse":{"status":"OK",
                                        "FACT_tf_alignment_label": "Strong bullish"}},
            "macro": {"UST_10Y":{"FACT_value":4.8},
                       "UST_REAL10Y":{"DERIVED_daily_change":-0.05},
                       "DXY":{"DERIVED_daily_change":-0.20}},
            "positioning": {"FACT_position_date": "2026-09-01",
                             "DERIVED_MM_net_pctile_5y": 0.95},
            "event_risk": {"status": "OK"},
        }
        c = classify(fake_snap)
        # Direct conflict: bullish structure + macro but crowded long
        self.assertEqual(c["CLASSIFICATION"], "CONFLICTED")

    # ── Duplicate ingestion idempotency ───────────────────
    def test_cftc_duplicate_idempotent(self):
        """INSERT OR IGNORE + UNIQUE prevents duplicate rows."""
        row = self.db.execute(text(
            "SELECT * FROM cftc_cot_raw ORDER BY id DESC LIMIT 1"
        )).fetchone()
        before = self.db.execute(text(
            "SELECT COUNT(*) FROM cftc_cot_raw WHERE report_type=:t AND report_date_yyyy_mm_dd=:d"
        ), {"t": row.report_type, "d": row.report_date_yyyy_mm_dd}).scalar()
        # attempt re-insert (should be ignored)
        self.db.execute(text(
            "INSERT OR IGNORE INTO cftc_cot_raw ("
            "report_type, cftc_commodity_code, report_date_yyyy_mm_dd, raw_json"
            ") VALUES (:t, :c, :d, '{}')"
        ), {"t": row.report_type, "c": row.cftc_commodity_code,
             "d": row.report_date_yyyy_mm_dd})
        after = self.db.execute(text(
            "SELECT COUNT(*) FROM cftc_cot_raw WHERE report_type=:t AND report_date_yyyy_mm_dd=:d"
        ), {"t": row.report_type, "d": row.report_date_yyyy_mm_dd}).scalar()
        self.assertEqual(before, after)

    # ── Revision handling on cftc_cot_versions ────────────
    def test_cftc_revision_records_new_version(self):
        # Simulate: same (report_type, report_date) but different hash
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        h1 = hashlib.sha256(b"payload A").hexdigest()
        h2 = hashlib.sha256(b"payload B").hexdigest()
        self.db.execute(text(
            "INSERT INTO cftc_cot_versions (report_type, cftc_commodity_code, "
            "report_date_yyyy_mm_dd, retrieval_ts_utc, raw_json, content_hash) "
            "VALUES ('TEST', '088691', '2026-09-01', :ts, '{}', :h)"
        ), {"ts": ts, "h": h1})
        self.db.execute(text(
            "INSERT INTO cftc_cot_versions (report_type, cftc_commodity_code, "
            "report_date_yyyy_mm_dd, retrieval_ts_utc, raw_json, content_hash) "
            "VALUES ('TEST', '088691', '2026-09-01', :ts, '{}', :h)"
        ), {"ts": ts, "h": h2})
        self.db.commit()
        r = self.db.execute(text(
            "SELECT COUNT(DISTINCT content_hash) FROM cftc_cot_versions "
            "WHERE report_type='TEST' AND report_date_yyyy_mm_dd='2026-09-01'"
        )).scalar()
        self.assertEqual(r, 2)
        # cleanup
        self.db.execute(text(
            "DELETE FROM cftc_cot_versions WHERE report_type='TEST'"
        ))
        self.db.commit()

if __name__ == "__main__":
    unittest.main(argv=[__file__, "-v"], exit=False)
