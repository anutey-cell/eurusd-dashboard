from __future__ import annotations

import os
from datetime import date, timedelta

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

# Unit tests must never start the network refresh worker.
os.environ["CME_OPTIONS_CONTEXT_REFRESH_ENABLED"] = "false"

from services.cme_options_context import get_cme_options_context, _sensitivity_weight


DDL = [
    """
    CREATE TABLE cme_gc_options_eod (
        bulletin_date TEXT,
        bulletin_status TEXT,
        product_code TEXT,
        option_expiry_code TEXT,
        option_type TEXT,
        strike FLOAT,
        open_interest INTEGER,
        open_interest_change INTEGER,
        delta_cme FLOAT
    )
    """,
    """
    CREATE TABLE gc_futures_bars (
        candle_time TEXT,
        close FLOAT
    )
    """,
    """
    CREATE TABLE historical_candles (
        instrument TEXT,
        timeframe TEXT,
        candle_time TEXT,
        close FLOAT
    )
    """,
]


def _db() -> Session:
    engine = create_engine("sqlite:///:memory:")
    with engine.begin() as conn:
        for stmt in DDL:
            conn.execute(text(stmt))
    return Session(engine)


def _seed_prices(db: Session, *, gc: float = 4380.0, xau: float = 4330.0) -> None:
    db.execute(text("INSERT INTO gc_futures_bars VALUES ('2026-09-16 03:00:00', :p)"), {"p": gc})
    db.execute(text("INSERT INTO historical_candles VALUES ('XAU/USD','M5','2026-09-16 03:00:00',:p)"), {"p": xau})
    db.commit()


def _insert_option(db: Session, *, day: str, strike: float, kind: str,
                   oi: int, delta: float, oi_change: int = 0,
                   product: str = "OG3", expiry: str = "SEP26") -> None:
    db.execute(text("""
        INSERT INTO cme_gc_options_eod
        (bulletin_date, bulletin_status, product_code, option_expiry_code,
         option_type, strike, open_interest, open_interest_change, delta_cme)
        VALUES (:d, 'FINAL', :p, :e, :t, :k, :oi, :chg, :delta)
    """), {
        "d": day, "p": product, "e": expiry, "t": kind,
        "k": strike, "oi": oi, "chg": oi_change, "delta": delta,
    })
    db.commit()


def test_sensitivity_weight_is_unsigned_and_peaks_near_half_delta():
    assert _sensitivity_weight(0.50) == pytest.approx(1.0)
    assert _sensitivity_weight(-0.50) == pytest.approx(1.0)
    assert _sensitivity_weight(0.10) < _sensitivity_weight(0.40)
    assert _sensitivity_weight(0.90) < _sensitivity_weight(0.60)


def test_context_maps_full_surface_through_live_gc_xau_basis_and_ranks_sensitivity():
    db = _db()
    try:
        _seed_prices(db, gc=4380.0, xau=4330.0)  # +50 basis
        day = date.today().isoformat()

        # Strike 4350: lower OI but almost ATM -> higher sensitivity score.
        _insert_option(db, day=day, strike=4350, kind="CALL", oi=500, delta=0.50, oi_change=120)
        _insert_option(db, day=day, strike=4350, kind="PUT",  oi=500, delta=-0.50, oi_change=80)

        # Strike 4400: more raw OI but far from 0.50 delta -> lower score.
        _insert_option(db, day=day, strike=4400, kind="CALL", oi=750, delta=0.90, oi_change=-10)
        _insert_option(db, day=day, strike=4400, kind="PUT",  oi=750, delta=-0.90, oi_change=5)

        ctx = get_cme_options_context(db, top_n=5)

        assert ctx["status"] == "OBSERVED"
        assert ctx["gc_xau_basis"] == pytest.approx(50.0)
        assert ctx["directional_bias"] == "UNSIGNED_NEUTRAL"
        assert ctx["gamma_status"].startswith("NOT_COMPUTED")
        assert ctx["zone_count"] == 2

        strongest = ctx["strongest_zones"][0]
        assert strongest["gc_strike"] == pytest.approx(4350.0)
        assert strongest["xau_equiv"] == pytest.approx(4300.0)
        assert strongest["total_oi"] == 1000
        assert strongest["oi_change"] == 200

        mapped_4400 = next(z for z in ctx["nearest_zones"] if z["gc_strike"] == 4400.0)
        assert mapped_4400["xau_equiv"] == pytest.approx(4350.0)
    finally:
        db.close()


def test_stale_bulletin_fails_closed_and_publishes_no_zones():
    db = _db()
    try:
        _seed_prices(db)
        stale = (date.today() - timedelta(days=10)).isoformat()
        _insert_option(db, day=stale, strike=4350, kind="CALL", oi=1000, delta=0.50)

        ctx = get_cme_options_context(db)
        assert ctx["status"] == "STALE"
        assert ctx["directional_bias"] == "UNSIGNED_NEUTRAL"
        assert ctx["zones"] == []
        assert "too old" in ctx["reason"]
    finally:
        db.close()


def test_no_bulletin_returns_no_data_not_a_directional_guess():
    db = _db()
    try:
        _seed_prices(db)
        ctx = get_cme_options_context(db)
        assert ctx["status"] == "NO_DATA"
        assert ctx["directional_bias"] == "UNSIGNED_NEUTRAL"
    finally:
        db.close()
