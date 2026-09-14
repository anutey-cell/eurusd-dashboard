from datetime import datetime, timedelta

from services import predator_engine as pe


def _bar(ts, close, *, high=None, low=None, volume=100.0):
    high = close + 1.0 if high is None else high
    low = close - 1.0 if low is None else low
    return (ts, close, high, low, close, volume)


def _friday_reference_bars():
    start = datetime(2026, 9, 11, 10, 0)
    bars = []
    for i in range(20):
        ts = start + timedelta(minutes=5 * i)
        bars.append(_bar(ts, 105.0, high=110.0, low=100.0))
    return bars


def test_monday_previous_session_skips_sunday_reopen_fragment():
    friday = _friday_reference_bars()
    # Sunday 22:00 UTC is the OPEN of Monday's XAU trading date, not a
    # standalone "previous day" session.
    sunday_reopen = [
        _bar(datetime(2026, 9, 13, 22, 0), 96.0, high=99.0, low=95.0),
        _bar(datetime(2026, 9, 13, 22, 5), 97.0, high=100.0, low=94.0),
    ]
    monday = [_bar(datetime(2026, 9, 14, 13, 0), 101.0)]

    prev_h, prev_l = pe._prev_day_hl(friday + sunday_reopen + monday)

    assert prev_h == 110.0
    assert prev_l == 100.0


def test_pdl_break_requires_fresh_two_close_acceptance():
    bars = _friday_reference_bars()
    monday_start = datetime(2026, 9, 14, 12, 0)
    for i in range(17):
        bars.append(_bar(monday_start + timedelta(minutes=5 * i), 101.0))
    # PDL=100 => trigger level=97. Fresh sequence is >=97 -> <97 -> <97.
    bars.extend([
        _bar(datetime(2026, 9, 14, 13, 25), 98.0),
        _bar(datetime(2026, 9, 14, 13, 30), 96.0),
        _bar(datetime(2026, 9, 14, 13, 35), 95.0),
    ])

    sig = pe.detect_pdl_break(bars, vol_r=1.3)

    assert sig is not None
    assert sig.state == "FIRE"
    assert sig.entry == 95.0
    assert sig.bar_time.startswith("2026-09-14T13:35")
    assert sig.session == "NY_OPEN"
    assert "previous-session" in sig.thesis.lower()
    assert "yesterday" not in (sig.thesis + sig.counterparty).lower()


def test_pdl_break_does_not_recycle_an_old_break():
    bars = _friday_reference_bars()
    monday_start = datetime(2026, 9, 14, 12, 0)
    for i in range(14):
        bars.append(_bar(monday_start + timedelta(minutes=5 * i), 101.0))
    # An old valid-looking break exists, but price remains below the trigger.
    # The latest three bars do not form a fresh >= -> below -> below event.
    closes = [98.0, 96.0, 95.0, 94.5, 94.0, 93.5]
    for j, close in enumerate(closes):
        bars.append(_bar(datetime(2026, 9, 14, 13, 10) + timedelta(minutes=5 * j), close))

    sig = pe.detect_pdl_break(bars, vol_r=1.3)

    assert sig is None


def test_alert_cannot_mix_regime_and_signal_session():
    sig = pe.PredatorSignal(
        archetype="PDL_BREAK",
        direction="SELL",
        state="FIRE",
        entry=95.0,
        stop_loss=105.0,
        tp1=55.0,
        tp2=35.0,
        rr=4.0,
        thesis="Fresh M5 acceptance below previous-session low 100.00.",
        trigger="fresh two-close M5 acceptance below PDL-3pt",
        confidence="MED",
        counterparty="Previous-session dip-buyers",
        session="NY_OPEN",
        bar_time="2026-09-14T13:35:00",
        fingerprint="test",
    )

    msg = pe.format_telegram_alert(
        sig,
        regime={"direction": "range", "volatility": "expanded", "session": "NY_LATE"},
    )

    assert "Regime: range × expanded × NY_OPEN" in msg
    assert "Session: NY_OPEN" in msg
    assert "range × expanded × NY_LATE" not in msg
