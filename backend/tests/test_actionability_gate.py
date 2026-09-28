from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from services.actionability_gate import evaluate_db_actionability


def _db() -> Session:
    engine = create_engine("sqlite:///:memory:")
    with engine.begin() as conn:
        conn.execute(text("""
            CREATE TABLE historical_candles (
                instrument TEXT,
                timeframe TEXT,
                candle_time TEXT,
                open FLOAT,
                high FLOAT,
                low FLOAT,
                close FLOAT,
                volume INTEGER,
                source TEXT
            )
        """))
    return Session(engine)


def _seed(db: Session, tf: str, when: datetime) -> None:
    db.execute(text("""
        INSERT INTO historical_candles
        (instrument,timeframe,candle_time,open,high,low,close,volume,source)
        VALUES ('XAU/USD',:tf,:t,4300,4302,4298,4301,1,'tradingview')
    """), {"tf": tf, "t": when.isoformat()})
    db.commit()


def test_core_fresh_context_stale_remains_actionable(monkeypatch):
    now = datetime(2026, 9, 16, 10, 0, tzinfo=timezone.utc)
    db = _db()
    try:
        _seed(db, "M5", now - timedelta(minutes=5))
        _seed(db, "M15", now - timedelta(minutes=15))
        _seed(db, "H1", now - timedelta(minutes=60))
        _seed(db, "H4", now - timedelta(hours=20))
        _seed(db, "D1", now - timedelta(days=5))

        import services.actionability_gate as ag
        from services.data_freshness import check_freshness as real_check

        def frozen(db_, *, instrument, timeframes):
            return real_check(db_, instrument=instrument, timeframes=timeframes, now=now)

        monkeypatch.setattr("services.data_freshness.check_freshness", frozen)
        gate = ag.evaluate_db_actionability(db)

        assert gate["actionable"] is True
        assert gate["data_quality_score"] >= 70
        assert set(gate["context_stale"]) == {"H4", "D1"}
        assert gate["optional_context_is_gate"] is False
    finally:
        db.close()


def test_stale_m5_blocks_actionable_signal(monkeypatch):
    now = datetime(2026, 9, 16, 10, 0, tzinfo=timezone.utc)
    db = _db()
    try:
        _seed(db, "M5", now - timedelta(minutes=30))
        _seed(db, "M15", now - timedelta(minutes=15))
        _seed(db, "H1", now - timedelta(minutes=60))
        _seed(db, "H4", now - timedelta(minutes=240))
        _seed(db, "D1", now - timedelta(minutes=1440))

        import services.actionability_gate as ag
        from services.data_freshness import check_freshness as real_check

        def frozen(db_, *, instrument, timeframes):
            return real_check(db_, instrument=instrument, timeframes=timeframes, now=now)

        monkeypatch.setattr("services.data_freshness.check_freshness", frozen)
        gate = ag.evaluate_db_actionability(db)

        assert gate["actionable"] is False
        assert "M5" in gate["critical_stale"]
        assert gate["status"] == "DATA_STALE"
    finally:
        db.close()


def test_stale_buy_fails_closed(monkeypatch):
    import services.actionability_gate as ag

    gate = {
        "actionable": False,
        "status": "DATA_STALE",
        "reason": "critical_stale=M5",
    }
    monkeypatch.setattr(ag, "evaluate_db_actionability", lambda db: gate)

    verdict = {
        "decision": "BUY",
        "execution_status": "SIGNAL_ONLY",
        "execution_permission": {"allow_alert": True, "allow_execute": True},
    }
    ag.attach_actionability(verdict, object())

    assert verdict["execution_status"] == "DATA_STALE"
    assert verdict["execution_permission"]["allow_alert"] is False
    assert verdict["execution_permission"]["allow_execute"] is False
    assert verdict["execution_permission"]["data_gate"] == "BLOCKED"


def test_stale_watchlist_is_not_reclassified(monkeypatch):
    """Only actionable BUY/SELL verdicts fail closed on stale core data."""
    import services.actionability_gate as ag

    gate = {
        "actionable": False,
        "status": "DATA_STALE",
        "reason": "critical_stale=M5",
    }
    monkeypatch.setattr(ag, "evaluate_db_actionability", lambda db: gate)

    verdict = {
        "decision": "STAND ASIDE",
        "execution_status": "Watchlist",
        "execution_permission": {"allow_alert": True, "allow_execute": False},
    }
    ag.attach_actionability(verdict, object())

    assert verdict["execution_status"] == "Watchlist"
    assert verdict["execution_permission"]["allow_alert"] is True
