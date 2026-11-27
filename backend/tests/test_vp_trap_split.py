from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from config import Settings
from db_models import VpTrapMeasurementEvent as Event
from services.vp_trap_measurement import compute_stats, record_signal
from services.vp_trap_scoring import valid_trade_plan


def test_sides_inherit_and_override_independently():
    settings = Settings(vp_trap_enabled=True, vp_trap_live_threshold=70,
                        vp_trap_trapped_sellers_enabled=False,
                        vp_trap_trapped_buyers_enabled=True,
                        vp_trap_trapped_buyers_threshold=78)
    assert settings.vp_trap_policy("BUY") == (False, 70)
    assert settings.vp_trap_policy("SELL") == (True, 78)
    settings.vp_trap_enabled = False
    assert settings.vp_trap_policy("SELL") == (False, 78)


def test_targets_follow_signal_direction():
    plan = dict(sl=105, entry=100, tp1=95, tp2=90, tp3=85)
    assert valid_trade_plan("SELL", plan)
    assert not valid_trade_plan("BUY", plan)
    assert not valid_trade_plan("SELL", dict(plan, tp1=101))
    assert not valid_trade_plan("SELL", dict(plan, sl=0))


def test_directional_samples_preserve_and_exclude_legacy_zero_stops():
    engine = create_engine("sqlite://")
    Event.__table__.create(engine)
    with Session(engine) as db:
        for zone, side, stop, first, second in [
            ("val", "BUY", 95, 105, 110), ("vah", "SELL", 105, 95, 90),
            ("pdh", "SELL", 106, 94, 88),
        ]:
            rid = record_signal(db, zone_id=zone, direction=side, score=80,
                                session="london", entry_price=100, stop_loss=stop,
                                tp1_price=first, tp2_price=second)
            assert db.get(Event, rid).stop_loss == stop
        db.get(Event, rid).stop_loss = 0
        db.commit()
        buy = compute_stats(db, direction="BUY")
        sell = compute_stats(db, direction="SELL")
        assert (buy["n_fired"], buy["excluded_invalid_stop"]) == (1, 0)
        assert (sell["n_fired"], sell["excluded_invalid_stop"]) == (1, 1)
        assert db.query(Event).count() == 3
