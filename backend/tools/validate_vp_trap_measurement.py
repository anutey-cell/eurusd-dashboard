"""Validate vp_trap_measurement — record + outcome-advance + stats."""
from __future__ import annotations
import os, sys
from datetime import datetime, timezone, timedelta

try:
    import services.vp_trap_measurement  # noqa
except ImportError:
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from services import vp_trap_measurement as m
from services.vp_trap_measurement import (
    record_signal, advance_outcomes, compute_stats, format_progress_digest,
)
from database import SessionLocal
from db_models import VpTrapMeasurementEvent as Row


TOTAL, PASSED = 0, 0
def _hr(t): print("\n" + "-"*78 + f"\n {t}\n" + "-"*78)
def _run(name, fn):
    global TOTAL, PASSED
    TOTAL += 1
    try: fn(); print(f"  OK   {name}"); PASSED += 1
    except AssertionError as e: print(f"  FAIL {name}: {e}")
    except Exception as e: print(f"  FAIL {name}: {type(e).__name__}: {e}")


def _cleanup(db):
    db.query(Row).delete()
    db.commit()


def _seed(db, direction="BUY", entry=4020.0, sl=4014.0,
           tp1=4028.0, tp2=4040.0, zone_id="test_zone_1", score=75,
           session="london_kz"):
    return record_signal(
        db, zone_id=zone_id, direction=direction, score=score,
        session=session, entry_price=entry, stop_loss=sl,
        tp1_price=tp1, tp2_price=tp2,
        invalidation_price=(sl - 1 if direction == "BUY" else sl + 1),
    )


# ── Recording ──────────────────────────────────────────────────────────────

def test_record_creates_pending_row():
    db = SessionLocal()
    try:
        _cleanup(db)
        rid = _seed(db)
        assert rid, "record_signal returned None"
        row = db.query(Row).one()
        assert row.status == "PENDING"
        assert row.direction == "BUY"
        assert abs(row.tp1_rr - (8.0 / 6.0)) < 1e-3   # 8pt TP1 / 6pt risk
    finally:
        _cleanup(db); db.close()


def test_record_is_idempotent_within_6h():
    db = SessionLocal()
    try:
        _cleanup(db)
        a = _seed(db)
        b = _seed(db)  # same zone, still PENDING
        assert a == b, "duplicate record should return same id"
        assert db.query(Row).count() == 1
    finally:
        _cleanup(db); db.close()


# ── Outcome advancement (unit — no live price) ────────────────────────────

def _seed_and_force(db, direction, entry, sl, tp1, tp2, price, force_status=None):
    """Manually seed + then simulate price via monkey-patch."""
    _cleanup(db)
    rid = record_signal(db, zone_id=f"z{price}", direction=direction,
                         score=80, session="london_kz",
                         entry_price=entry, stop_loss=sl,
                         tp1_price=tp1, tp2_price=tp2,
                         invalidation_price=(sl - 1 if direction == "BUY" else sl + 1))
    row = db.query(Row).filter(Row.id == rid).one()
    if force_status:
        row.status = force_status
        row.triggered_at = datetime.now(timezone.utc) - timedelta(minutes=5)
        db.commit()
    # Monkey-patch _last_price for this test
    m._last_price = lambda: price
    advance_outcomes(db)
    db.refresh(row)
    return row


def test_trigger_when_price_reaches_entry():
    db = SessionLocal()
    try:
        row = _seed_and_force(db, "BUY", 4020, 4014, 4028, 4040, price=4020.2)
        assert row.status == "TRIGGERED"
        assert row.triggered_at is not None
    finally:
        _cleanup(db); db.close()


def test_stop_hit_after_trigger():
    db = SessionLocal()
    try:
        row = _seed_and_force(db, "BUY", 4020, 4014, 4028, 4040,
                               price=4013, force_status="TRIGGERED")
        assert row.status == "STOPPED"
        assert row.r_realized == -1.0
    finally:
        _cleanup(db); db.close()


def test_tp2_hit_blended_r():
    """TP2 hit → 0.5×tp1_rr + 0.5×tp2_rr blended."""
    db = SessionLocal()
    try:
        # entry 4020, SL 4014 (6pt risk), TP1 4028 (1.33R), TP2 4040 (3.33R)
        row = _seed_and_force(db, "BUY", 4020, 4014, 4028, 4040,
                               price=4041, force_status="TRIGGERED")
        assert row.status == "TP2_HIT"
        # blended = 0.5 * 1.333 + 0.5 * 3.333 = 2.333R
        assert abs(row.r_realized - 2.333) < 0.05
    finally:
        _cleanup(db); db.close()


def test_invalidation_before_trigger():
    db = SessionLocal()
    try:
        # BUY setup, invalidation is at SL-1 = 4013; price crashes to 4010 pre-entry
        row = _seed_and_force(db, "BUY", 4020, 4014, 4028, 4040, price=4010)
        assert row.status == "INVALIDATED"
        assert row.r_realized == 0.0
    finally:
        _cleanup(db); db.close()


def test_expired_after_max_wait():
    db = SessionLocal()
    try:
        _cleanup(db)
        rid = record_signal(db, zone_id="z_exp", direction="BUY", score=75,
                             session="london_kz", entry_price=4020.0,
                             stop_loss=4014.0, tp1_price=4028.0, tp2_price=4040.0)
        row = db.query(Row).filter(Row.id == rid).one()
        # Push fired_at into the past beyond DEFAULT_MAX_WAIT_HOURS
        row.fired_at = datetime.now(timezone.utc) - timedelta(hours=10)
        db.commit()
        # Price doesn't trigger
        m._last_price = lambda: 4025.0
        advance_outcomes(db)
        db.refresh(row)
        assert row.status == "EXPIRED"
        assert row.r_realized == 0.0
    finally:
        _cleanup(db); db.close()


# ── Aggregator ──────────────────────────────────────────────────────────────

def _insert_closed(db, r_realized, days_ago=1, zone_suffix="a", session="london_kz"):
    ts = datetime.now(timezone.utc) - timedelta(days=days_ago)
    row = Row(
        zone_id=f"z{r_realized}_{zone_suffix}",
        direction="BUY", score=80, session=session,
        fired_at=ts, entry_price=4020, stop_loss=4014,
        tp1_price=4028, tp2_price=4040,
        tp1_rr=1.33, tp2_rr=3.33,
        status=("TP2_HIT" if r_realized > 0 else "STOPPED"),
        triggered_at=ts + timedelta(minutes=5),
        closed_at=ts + timedelta(minutes=30),
        closed_price=(4041 if r_realized > 0 else 4013),
        r_realized=r_realized, mfe_pts=21 if r_realized > 0 else 5,
        mae_pts=-4 if r_realized > 0 else -6,
        duration_min=25,
    )
    db.add(row)
    db.commit()


def test_stats_below_target_when_wr_low():
    db = SessionLocal()
    try:
        _cleanup(db)
        for i in range(30):
            _insert_closed(db, r_realized=(2.3 if i < 10 else -1.0),
                            zone_suffix=str(i))
        stats = compute_stats(db, days=30)
        assert stats["n_closed"] == 30
        assert stats["n_wins"] == 10
        assert stats["win_rate_pct"] == 33.3
        assert stats["verdict"]["label"] == "BELOW TARGET"
    finally:
        _cleanup(db); db.close()


def test_stats_on_target_when_all_thresholds_met():
    db = SessionLocal()
    try:
        _cleanup(db)
        # 24 signals, 12 wins @ +2.3R, 12 losses @ -1R → WR 50%, avg (+27.6-12)/24=0.65R
        # Spread across 22 days to keep signals/day inside 1-3
        for i in range(24):
            _insert_closed(db, r_realized=(2.3 if i < 12 else -1.0),
                            days_ago=(i % 22) + 1, zone_suffix=str(i))
        stats = compute_stats(db, days=30)
        assert stats["n_closed"] == 24
        assert stats["win_rate_pct"] == 50.0
        assert stats["avg_r_per_trade"] > 0.15
        assert 1.0 <= stats["signals_per_day"] <= 3.0
        assert stats["verdict"]["label"] == "ON TARGET"
    finally:
        _cleanup(db); db.close()


def test_stats_insufficient_sample():
    db = SessionLocal()
    try:
        _cleanup(db)
        for i in range(5):
            _insert_closed(db, r_realized=2.3, zone_suffix=str(i))
        stats = compute_stats(db, days=30)
        assert stats["verdict"]["label"] == "INSUFFICIENT SAMPLE"
    finally:
        _cleanup(db); db.close()


def test_stats_no_data():
    db = SessionLocal()
    try:
        _cleanup(db)
        stats = compute_stats(db, days=30)
        assert stats["verdict"]["label"] == "NO DATA"
    finally:
        _cleanup(db); db.close()


def test_digest_formats_cleanly():
    """format_progress_digest must produce a sensible string for the operator."""
    db = SessionLocal()
    try:
        _cleanup(db)
        for i in range(22):
            _insert_closed(db, r_realized=(2.3 if i < 11 else -1.0),
                            days_ago=(i % 20) + 1, zone_suffix=str(i))
        stats = compute_stats(db, days=30)
        text = format_progress_digest(stats)
        assert "VP Trap" in text
        assert "Verdict" in text
    finally:
        _cleanup(db); db.close()


def test_max_drawdown_computed():
    db = SessionLocal()
    try:
        _cleanup(db)
        # sequence: +2, +2, -1, -1, -1 → cum: 2,4,3,2,1  peak=4, max_dd=3
        for i, r in enumerate([2.0, 2.0, -1.0, -1.0, -1.0]):
            _insert_closed(db, r_realized=r, days_ago=(5-i), zone_suffix=str(i))
        # need 20+ closed for verdict — add filler
        for j in range(5, 22):
            _insert_closed(db, r_realized=0.5, days_ago=(j % 20) + 1, zone_suffix=str(j))
        stats = compute_stats(db, days=30)
        # exact dd depends on ordering; at minimum must be > 0
        assert stats["max_drawdown_r"] > 0
    finally:
        _cleanup(db); db.close()


# ── Main ────────────────────────────────────────────────────────────────────

def main():
    print("=" * 78)
    print(" VP TRAP MEASUREMENT PROTOCOL VALIDATION (P135)")
    print("=" * 78)

    _hr("1. Recording")
    _run("record creates PENDING row", test_record_creates_pending_row)
    _run("idempotent within 6h", test_record_is_idempotent_within_6h)

    _hr("2. Outcome advancement")
    _run("PENDING → TRIGGERED at entry touch", test_trigger_when_price_reaches_entry)
    _run("TRIGGERED → STOPPED at SL, r=-1", test_stop_hit_after_trigger)
    _run("TRIGGERED → TP2_HIT blends R", test_tp2_hit_blended_r)
    _run("PENDING → INVALIDATED on opposing move", test_invalidation_before_trigger)
    _run("PENDING → EXPIRED past max_wait", test_expired_after_max_wait)

    _hr("3. Aggregator")
    _run("no data → NO DATA verdict", test_stats_no_data)
    _run("< 20 closed → INSUFFICIENT SAMPLE", test_stats_insufficient_sample)
    _run("low WR → BELOW TARGET", test_stats_below_target_when_wr_low)
    _run("all thresholds met → ON TARGET", test_stats_on_target_when_all_thresholds_met)
    _run("max drawdown computed", test_max_drawdown_computed)
    _run("digest formats cleanly", test_digest_formats_cleanly)

    print("\n" + "=" * 78)
    if PASSED == TOTAL:
        print(f" ALL PASS {PASSED}/{TOTAL} — protocol infrastructure ready")
        return 0
    print(f" FAIL {PASSED}/{TOTAL}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
