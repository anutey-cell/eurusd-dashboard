"""Unit tests for shadow trade simulator (grade calibration data source)."""
import os
import sys
from datetime import datetime, timezone, timedelta
from types import SimpleNamespace

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from services.shadow_trade_simulator import (
    record_shadow_trade,
    advance_outcomes,
    compute_bucket_stats,
    format_bucket_summary,
    _fingerprint,
    _session_label_for_now,
    _in_rollover_window,
    _estimate_spread,
    _estimate_slippage,
    SPREAD_PTS_BY_SESSION,
    SLIPPAGE_PTS_BY_SESSION,
)


def _fresh_db():
    """SQLite in-memory session with just the shadow_trades + historical_candles tables."""
    engine = create_engine("sqlite:///:memory:")
    with engine.begin() as conn:
        conn.execute(text(
            "CREATE TABLE shadow_trades ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "created_at TIMESTAMP, updated_at TIMESTAMP, "
            "fingerprint TEXT UNIQUE, verdict_id INTEGER, instrument TEXT, "
            "grade TEXT, grade_reason TEXT, composite_score REAL, "
            "archetype TEXT, regime_at_entry TEXT, session_at_entry TEXT, "
            "direction TEXT, setup_score INTEGER, conditions_passed INTEGER, "
            "fired_at TIMESTAMP, entry_price REAL, stop_loss REAL, "
            "tp1_price REAL, tp2_price REAL, tp3_price REAL, "
            "invalidation_price REAL, tp1_rr REAL, tp2_rr REAL, "
            "est_spread_pts REAL, est_slippage_pts REAL, "
            "status TEXT, triggered_at TIMESTAMP, triggered_price REAL, "
            "closed_at TIMESTAMP, closed_price REAL, "
            "r_realized REAL, r_spread_adjusted REAL, mfe_pts REAL, mae_pts REAL, "
            "duration_min REAL, notes_json TEXT"
            ")"
        ))
        conn.execute(text(
            "CREATE TABLE historical_candles ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "instrument TEXT, timeframe TEXT, candle_time TIMESTAMP, "
            "open REAL, high REAL, low REAL, close REAL, volume REAL"
            ")"
        ))
    SessionLocal = sessionmaker(bind=engine)

    # Patch db_models.ShadowTrade so record_shadow_trade can insert via ORM
    # against the same engine's raw table (we route through db.execute anyway
    # inside record_shadow_trade — but it uses ShadowTrade class for insert).
    # Solution: use direct SQL insert in tests via a lightweight monkeypatch
    # OR reflect the table. We'll monkey-patch ShadowTrade at import time
    # by creating a minimal class bound to this engine.
    import db_models
    from sqlalchemy import Column, Integer, String, Float, DateTime, Text
    from sqlalchemy.orm import declarative_base

    # Build a mirror class bound to this test engine.
    Base = declarative_base()

    class _ShadowTradeTest(Base):
        __tablename__ = "shadow_trades"
        id = Column(Integer, primary_key=True, autoincrement=True)
        created_at = Column(DateTime); updated_at = Column(DateTime)
        fingerprint = Column(String, unique=True); verdict_id = Column(Integer)
        instrument = Column(String); grade = Column(String)
        grade_reason = Column(String); composite_score = Column(Float)
        archetype = Column(String); regime_at_entry = Column(String)
        session_at_entry = Column(String); direction = Column(String)
        setup_score = Column(Integer); conditions_passed = Column(Integer)
        fired_at = Column(DateTime); entry_price = Column(Float)
        stop_loss = Column(Float); tp1_price = Column(Float)
        tp2_price = Column(Float); tp3_price = Column(Float)
        invalidation_price = Column(Float); tp1_rr = Column(Float)
        tp2_rr = Column(Float); est_spread_pts = Column(Float)
        est_slippage_pts = Column(Float); status = Column(String)
        triggered_at = Column(DateTime); triggered_price = Column(Float)
        closed_at = Column(DateTime); closed_price = Column(Float)
        r_realized = Column(Float); r_spread_adjusted = Column(Float)
        mfe_pts = Column(Float); mae_pts = Column(Float)
        duration_min = Column(Float); notes_json = Column(Text)

    db_models.ShadowTrade = _ShadowTradeTest
    return SessionLocal()


def _insert_bar(db, ts, o, h, l, c, tf="M5", inst="XAU/USD"):
    db.execute(text(
        "INSERT INTO historical_candles (instrument, timeframe, candle_time, "
        "open, high, low, close, volume) VALUES (:i, :tf, :t, :o, :h, :l, :c, 1)"
    ), {"i": inst, "tf": tf, "t": ts, "o": o, "h": h, "l": l, "c": c})
    db.commit()


def _verdict(*, decision="BUY", entry=4200, sl=4180, tp1=4240, tp2=4280,
              score=85, cp=4, archetype="pullback", regime="TREND_UP"):
    return {
        "decision": decision,
        "setup_score": score,
        "conditions_passed": cp,
        "archetype": archetype,
        "regime": regime,
        "trade_plan": {
            "entry": entry,
            "stop_loss": sl,
            "tp1": tp1,
            "tp2": tp2,
            "tp1_rr": abs(tp1 - entry) / abs(entry - sl),
            "tp2_rr": abs(tp2 - entry) / abs(entry - sl),
        },
        "signal_grade": {"grade": "A", "reason": "score>=80 & RR>=2.5"},
    }


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def test_session_label_ranges():
    """Verify session labels cover the day correctly."""
    dt2 = datetime(2026, 8, 5, 3, 0, tzinfo=timezone.utc)  # 03:00 UTC
    assert _session_label_for_now(dt2) == "ASIA"
    dt6 = datetime(2026, 8, 5, 6, 30, tzinfo=timezone.utc)
    assert _session_label_for_now(dt6) == "PRE_LDN"
    dt8 = datetime(2026, 8, 5, 8, 0, tzinfo=timezone.utc)
    assert _session_label_for_now(dt8) == "LDN_OPEN"
    dt14 = datetime(2026, 8, 5, 14, 30, tzinfo=timezone.utc)
    assert _session_label_for_now(dt14) == "NY_OPEN"


def test_rollover_window_detected():
    """21:50-22:15 UTC is rollover blackout."""
    assert _in_rollover_window(datetime(2026, 8, 5, 21, 55, tzinfo=timezone.utc))
    assert _in_rollover_window(datetime(2026, 8, 5, 22, 10, tzinfo=timezone.utc))
    assert not _in_rollover_window(datetime(2026, 8, 5, 21, 30, tzinfo=timezone.utc))
    assert not _in_rollover_window(datetime(2026, 8, 5, 22, 30, tzinfo=timezone.utc))


def test_spread_and_slippage_lookup_defaults():
    """Unknown session falls back to OFF-hours cost model."""
    assert _estimate_spread("NY_OPEN") == SPREAD_PTS_BY_SESSION["NY_OPEN"]
    assert _estimate_slippage("ASIA") == SLIPPAGE_PTS_BY_SESSION["ASIA"]
    assert _estimate_spread("BOGUS") == SPREAD_PTS_BY_SESSION.get("OFF", 2.0)


def test_fingerprint_stable_same_bucket():
    """Same direction + hour + session + 5pt-bucketed entry → same fp."""
    now = datetime(2026, 8, 5, 14, 30, tzinfo=timezone.utc)
    fp1 = _fingerprint(direction="BUY", entry=4200.1, session="NY_OPEN", fired_at=now)
    fp2 = _fingerprint(direction="BUY", entry=4201.9, session="NY_OPEN", fired_at=now)
    assert fp1 == fp2, "Prices within 5pt should share the same fingerprint"


def test_fingerprint_differs_by_direction():
    now = datetime(2026, 8, 5, 14, 30, tzinfo=timezone.utc)
    fp_b = _fingerprint(direction="BUY", entry=4200, session="NY_OPEN", fired_at=now)
    fp_s = _fingerprint(direction="SELL", entry=4200, session="NY_OPEN", fired_at=now)
    assert fp_b != fp_s


def test_fingerprint_differs_by_hour():
    dt1 = datetime(2026, 8, 5, 14, 30, tzinfo=timezone.utc)
    dt2 = datetime(2026, 8, 5, 15, 30, tzinfo=timezone.utc)
    fp1 = _fingerprint(direction="BUY", entry=4200, session="NY_OPEN", fired_at=dt1)
    fp2 = _fingerprint(direction="BUY", entry=4200, session="NY_OPEN", fired_at=dt2)
    assert fp1 != fp2


# ─────────────────────────────────────────────────────────────────────────────
# record_shadow_trade
# ─────────────────────────────────────────────────────────────────────────────

def test_record_success_buy_verdict():
    db = _fresh_db()
    r = record_shadow_trade(db, _verdict())
    assert r.recorded is True
    assert r.reason == "recorded"
    assert len(r.fingerprint) > 0
    rows = db.execute(text("SELECT COUNT(*) FROM shadow_trades")).fetchone()
    assert rows[0] == 1


def test_record_skips_stand_aside():
    db = _fresh_db()
    r = record_shadow_trade(db, {"decision": "STAND ASIDE"})
    assert r.recorded is False
    assert "decision=" in r.reason


def test_record_skips_incomplete_plan():
    db = _fresh_db()
    r = record_shadow_trade(db, {
        "decision": "BUY",
        "trade_plan": {"entry": 4200, "stop_loss": None, "tp1": 4240, "tp2": 4280},
    })
    assert r.recorded is False
    assert "incomplete" in r.reason


def test_record_dedupes_same_fingerprint():
    db = _fresh_db()
    v = _verdict()
    r1 = record_shadow_trade(db, v)
    r2 = record_shadow_trade(db, v)
    assert r1.recorded is True
    assert r2.recorded is False
    assert r2.reason == "duplicate fingerprint"


def test_record_captures_all_grades():
    """Every grade — A+, A, B, C — should record. Point of the simulator."""
    db = _fresh_db()
    class G:
        def __init__(self, grade, score):
            self.grade = grade
            self.reason = f"synthetic {grade}"
            self.composite_score = score

    v_aplus = _verdict(entry=4200); g_ap = G("A+", 92)
    v_a     = _verdict(entry=4300); g_a  = G("A",  85)
    v_b     = _verdict(entry=4400); g_b  = G("B",  75)
    v_c     = _verdict(entry=4500); g_c  = G("C",  65)

    for v, g in [(v_aplus, g_ap), (v_a, g_a), (v_b, g_b), (v_c, g_c)]:
        assert record_shadow_trade(db, v, grade_result=g).recorded

    grades = {row[0] for row in db.execute(
        text("SELECT grade FROM shadow_trades")).fetchall()}
    assert grades == {"A+", "A", "B", "C"}


def test_record_captures_session_estimates():
    """Every row gets session-conditional spread + slippage attached."""
    db = _fresh_db()
    r = record_shadow_trade(db, _verdict())
    assert r.recorded
    row = db.execute(text(
        "SELECT est_spread_pts, est_slippage_pts, session_at_entry "
        "FROM shadow_trades LIMIT 1"
    )).fetchone()
    assert row[0] is not None
    assert row[1] is not None
    assert row[2] in SPREAD_PTS_BY_SESSION


# ─────────────────────────────────────────────────────────────────────────────
# advance_outcomes
# ─────────────────────────────────────────────────────────────────────────────

def test_advance_no_price_returns_warning():
    db = _fresh_db()
    record_shadow_trade(db, _verdict())
    r = advance_outcomes(db)
    assert "warning" in r


def test_advance_triggers_pending_on_price_touch():
    """PENDING BUY with entry=4200 → M5 bar that dips through 4200 → TRIGGERED."""
    db = _fresh_db()
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    fired = now - timedelta(minutes=5)
    # Seed M5 bars after fired_at so _bar_extremes_since picks them up
    _insert_bar(db, fired + timedelta(seconds=30), 4210, 4215, 4198, 4205)
    _insert_bar(db, now,                             4205, 4212, 4200, 4208)
    # Insert row with backdated fired_at (matches production timeline where
    # fired_at precedes the price-check window)
    db.execute(text(
        "INSERT INTO shadow_trades (created_at, updated_at, fingerprint, "
        "instrument, grade, direction, fired_at, entry_price, stop_loss, "
        "tp1_price, tp2_price, invalidation_price, tp1_rr, tp2_rr, "
        "est_spread_pts, est_slippage_pts, status) "
        "VALUES (:c, :c, 'trigfp', 'XAU/USD', 'A', 'BUY', :f, 4200, 4180, "
        "4240, 4280, 4180, 2, 4, 0.8, 1.0, 'PENDING')"
    ), {"c": now, "f": fired})
    db.commit()

    r = advance_outcomes(db)
    assert r["pending_walked"] == 1
    assert r["triggered"] == 1
    row = db.execute(text(
        "SELECT status, triggered_at, triggered_price FROM shadow_trades "
        "WHERE fingerprint='trigfp'"
    )).fetchone()
    assert row[0] == "TRIGGERED"
    assert row[1] is not None
    assert abs(row[2] - 4200) < 0.001


def test_advance_invalidates_on_opposite_break_before_trigger():
    """PENDING BUY with invalidation=4180 → price drops to 4165 → INVALIDATED."""
    db = _fresh_db()
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    fired = now - timedelta(minutes=5)
    _insert_bar(db, fired + timedelta(seconds=30), 4195, 4200, 4165, 4175)
    db.execute(text(
        "INSERT INTO shadow_trades (created_at, updated_at, fingerprint, "
        "instrument, grade, direction, fired_at, entry_price, stop_loss, "
        "tp1_price, tp2_price, invalidation_price, tp1_rr, tp2_rr, "
        "est_spread_pts, est_slippage_pts, status) "
        "VALUES (:c, :c, 'invfp', 'XAU/USD', 'A', 'BUY', :f, 4210, 4180, "
        "4240, 4280, 4180, 1, 3, 0.8, 1.0, 'PENDING')"
    ), {"c": now, "f": fired})
    db.commit()

    r = advance_outcomes(db)
    assert r["invalidated"] == 1
    row = db.execute(text(
        "SELECT status FROM shadow_trades WHERE fingerprint='invfp'"
    )).fetchone()
    assert row[0] == "INVALIDATED"


def test_advance_expires_stale_pending():
    """PENDING beyond expiry_hours → EXPIRED regardless of price."""
    db = _fresh_db()
    old = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=24)
    _insert_bar(db, old, 4200, 4205, 4195, 4202)  # price bar to enable _last_price
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    _insert_bar(db, now, 4200, 4205, 4195, 4202)

    # Insert row directly to backdate fired_at
    db.execute(text(
        "INSERT INTO shadow_trades (created_at, updated_at, fingerprint, "
        "instrument, grade, direction, fired_at, entry_price, stop_loss, "
        "tp1_price, tp2_price, invalidation_price, tp1_rr, tp2_rr, "
        "est_spread_pts, est_slippage_pts, status) "
        "VALUES (:c, :u, 'oldfp', 'XAU/USD', 'A', 'BUY', :f, 4500, 4480, "
        "4540, 4580, 4480, 2, 4, 0.5, 0.3, 'PENDING')"
    ), {"c": old, "u": old, "f": old})
    db.commit()

    r = advance_outcomes(db, expiry_hours=12)
    assert r["expired"] >= 1
    row = db.execute(text(
        "SELECT status FROM shadow_trades WHERE fingerprint='oldfp'"
    )).fetchone()
    assert row[0] == "EXPIRED"


def test_advance_stops_triggered_buy_at_sl():
    """TRIGGERED BUY with SL 4180 → bar with low 4175 → STOPPED r=-1."""
    db = _fresh_db()
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    trig_time = now - timedelta(minutes=10)
    # Ensure _last_price and bar-extreme queries have data
    _insert_bar(db, trig_time, 4200, 4205, 4195, 4200)
    _insert_bar(db, now,       4200, 4210, 4175, 4185)  # tags SL

    db.execute(text(
        "INSERT INTO shadow_trades (created_at, updated_at, fingerprint, "
        "instrument, grade, direction, fired_at, entry_price, stop_loss, "
        "tp1_price, tp2_price, invalidation_price, tp1_rr, tp2_rr, "
        "est_spread_pts, est_slippage_pts, status, triggered_at) "
        "VALUES (:c, :c, 'stopfp', 'XAU/USD', 'A', 'BUY', :c, 4200, 4180, "
        "4240, 4280, 4180, 2, 4, 0.8, 1.0, 'TRIGGERED', :t)"
    ), {"c": now, "t": trig_time})
    db.commit()

    r = advance_outcomes(db)
    assert r["stopped"] == 1
    row = db.execute(text(
        "SELECT status, r_realized, r_spread_adjusted "
        "FROM shadow_trades WHERE fingerprint='stopfp'"
    )).fetchone()
    assert row[0] == "STOPPED"
    assert row[1] == -1.0
    # Spread-adjusted should be MORE negative than -1
    assert row[2] < -1.0


def test_advance_closes_tp2_when_both_targets_hit():
    """TRIGGERED BUY with bar high sweeping past TP2 → TP2_HIT with r ≥ tp1_rr."""
    db = _fresh_db()
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    trig_time = now - timedelta(minutes=15)
    _insert_bar(db, trig_time, 4200, 4205, 4195, 4200)
    _insert_bar(db, now,       4210, 4285, 4205, 4280)  # tags TP1 4240 + TP2 4280

    db.execute(text(
        "INSERT INTO shadow_trades (created_at, updated_at, fingerprint, "
        "instrument, grade, direction, fired_at, entry_price, stop_loss, "
        "tp1_price, tp2_price, invalidation_price, tp1_rr, tp2_rr, "
        "est_spread_pts, est_slippage_pts, status, triggered_at) "
        "VALUES (:c, :c, 'tp2fp', 'XAU/USD', 'A+', 'BUY', :c, 4200, 4180, "
        "4240, 4280, 4180, 2, 4, 0.8, 1.0, 'TRIGGERED', :t)"
    ), {"c": now, "t": trig_time})
    db.commit()

    r = advance_outcomes(db)
    assert r["closed_tp2"] == 1
    row = db.execute(text(
        "SELECT status, r_realized, mfe_pts, mae_pts "
        "FROM shadow_trades WHERE fingerprint='tp2fp'"
    )).fetchone()
    assert row[0] == "TP2_HIT"
    # r_realized = 0.5*2 + 0.5*4 = 3.0
    assert abs(row[1] - 3.0) < 0.01
    assert row[2] > 0
    assert row[3] >= 0


# ─────────────────────────────────────────────────────────────────────────────
# compute_bucket_stats
# ─────────────────────────────────────────────────────────────────────────────

def test_bucket_stats_empty_returns_zero():
    db = _fresh_db()
    stats = compute_bucket_stats(db, days=30)
    assert stats["overall"]["n_total"] == 0
    assert stats["buckets"] == []


def test_bucket_stats_aggregates_by_grade():
    """Two closed trades per grade → grades appear as separate buckets."""
    db = _fresh_db()
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    for i, (grade, r) in enumerate([("A+", 3.0), ("A+", -1.0), ("A", 2.0), ("A", -1.0)]):
        db.execute(text(
            "INSERT INTO shadow_trades (created_at, updated_at, fingerprint, "
            "instrument, grade, archetype, regime_at_entry, session_at_entry, "
            "direction, fired_at, entry_price, stop_loss, tp1_price, tp2_price, "
            "invalidation_price, tp1_rr, tp2_rr, est_spread_pts, est_slippage_pts, "
            "status, r_realized, r_spread_adjusted) "
            "VALUES (:c, :c, :fp, 'XAU/USD', :g, 'pullback', 'TREND_UP', "
            "'NY_OPEN', 'BUY', :c, 4200, 4180, 4240, 4280, 4180, 2, 4, "
            "0.8, 1.0, 'TP2_HIT', :r, :ra)"
        ), {"c": now, "fp": f"fp_{i}", "g": grade, "r": r, "ra": r - 0.05})
    db.commit()

    stats = compute_bucket_stats(db, days=30)
    assert stats["overall"]["n_total"] == 4
    # 2 buckets — one per grade
    assert len(stats["buckets"]) == 2
    for b in stats["buckets"]:
        assert b["n"] == 2
        assert b["meets_min_sample"] is False  # 2 < 20


def test_format_bucket_summary_readable():
    """Digest string mentions the window and can be shown to Telegram."""
    stats = {
        "window_days": 30,
        "overall": {"n_total": 40, "mean_r_all": 0.75, "mean_r_adjusted_all": 0.55},
        "buckets": [
            {"bucket_key": "A|pullback|TREND_UP|NY_OPEN",
             "n": 25, "wins": 15, "losses": 10, "hit_rate": 0.6,
             "mean_r": 0.9, "mean_r_adjusted": 0.7, "meets_min_sample": True},
        ],
    }
    out = format_bucket_summary(stats)
    assert "30 days" in out
    assert "40" in out
    assert "A|pullback" in out
    assert "WR=60%" in out
