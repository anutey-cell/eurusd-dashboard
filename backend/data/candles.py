"""
EUR/USD candle data layer.

In demo mode (DATA_MODE=demo), delegates to the seeded mock generator below.
In live mode (DATA_MODE=live), delegates to candle_provider which routes to
the configured FX_DATA_PROVIDER (twelvedata | alpha_vantage | oanda | polygon | fmp).
"""
import random
from datetime import datetime, timedelta, timezone

from models.candle import Candle, CandleResponse

# Interval durations in minutes — used by the mock generator and for validation
INTERVAL_MINUTES: dict[str, int] = {
    "M5":  5,
    "M15": 15,
    "M30": 30,
    "H1":  60,
    "H4":  240,
    "D1":  1440,
    "W1":  10080,
}

BASE_PRICE = 1.08432
BASE_PRICE_XAUUSD = 2358.40
SPREAD = 0.00002


def _generate_candles(interval: str, limit: int) -> list[Candle]:
    rng = random.Random(42)  # seeded so responses are deterministic
    step_min = INTERVAL_MINUTES[interval]
    now = datetime.now(timezone.utc).replace(second=0, microsecond=0)
    # Align to interval boundary
    total_min = int(now.timestamp() // 60) * 60
    boundary = (total_min // step_min) * step_min
    end_time = datetime.fromtimestamp(boundary, tz=timezone.utc)

    candles: list[Candle] = []
    close = BASE_PRICE

    for i in range(limit - 1, -1, -1):
        t = end_time - timedelta(minutes=step_min * i)

        # Skip weekends for D1/W1
        if interval in ("D1", "W1") and t.weekday() >= 5:
            continue

        # Volatility scales with timeframe
        vol = step_min ** 0.5 * 0.00008
        body = rng.gauss(0, vol)
        o = round(close, 5)
        c = round(o + body, 5)
        wick_hi = abs(rng.gauss(0, vol * 0.6))
        wick_lo = abs(rng.gauss(0, vol * 0.6))
        h = round(max(o, c) + wick_hi, 5)
        l = round(min(o, c) - wick_lo, 5)
        vol_units = int(rng.uniform(800, 4000) * (step_min / 60) ** 0.5)

        candles.append(Candle(time=t, open=o, high=h, low=l, close=c, volume=vol_units))
        close = c

    return candles


def _get_mock_candles(interval: str, limit: int) -> CandleResponse:
    candles = _generate_candles(interval, limit)
    return CandleResponse(symbol="EUR/USD", interval=interval,
                          count=len(candles), candles=candles)


def _generate_xauusd_candles(interval: str, limit: int) -> list[Candle]:
    """Deterministic seeded mock gold candles around $2350–2380."""
    rng = random.Random(99)   # different seed from EUR/USD
    step_min = INTERVAL_MINUTES[interval]
    now = datetime.now(timezone.utc).replace(second=0, microsecond=0)
    total_min = int(now.timestamp() // 60) * 60
    boundary = (total_min // step_min) * step_min
    end_time = datetime.fromtimestamp(boundary, tz=timezone.utc)

    candles: list[Candle] = []
    close = 2358.40

    for i in range(limit - 1, -1, -1):
        t = end_time - timedelta(minutes=step_min * i)
        if interval in ("D1", "W1") and t.weekday() >= 5:
            continue
        # Gold volatility is higher (use pip_size=1.0 equivalent)
        vol = step_min ** 0.5 * 0.15
        body = rng.gauss(0, vol)
        o = round(close, 2)
        c = round(o + body, 2)
        wick_hi = abs(rng.gauss(0, vol * 0.6))
        wick_lo = abs(rng.gauss(0, vol * 0.6))
        h = round(max(o, c) + wick_hi, 2)
        l = round(min(o, c) - wick_lo, 2)
        vol_units = int(rng.uniform(200, 2000) * (step_min / 60) ** 0.5)
        candles.append(Candle(time=t, open=o, high=h, low=l, close=c, volume=vol_units))
        close = c

    return candles


def _get_mock_xauusd_candles(interval: str, limit: int) -> CandleResponse:
    candles = _generate_xauusd_candles(interval, limit)
    return CandleResponse(symbol="XAU/USD", interval=interval,
                          count=len(candles), candles=candles)


def get_candles(interval: str = "H4", limit: int = 200, pair: str = "eurusd") -> CandleResponse:
    interval = interval.upper()
    if interval not in INTERVAL_MINUTES:
        raise ValueError(f"Unknown interval '{interval}'. Valid: {list(INTERVAL_MINUTES)}")
    limit = max(1, min(limit, 5000))

    pair_code = pair.lower().replace("/", "").replace("_", "")

    from config import settings
    if settings.data_mode == "live":
        from services.candle_provider import get_eurusd_candles
        symbol = "XAU/USD" if pair_code == "xauusd" else "EUR/USD"
        return get_eurusd_candles(timeframe=interval, lookback=limit, pair=symbol)

    if pair_code == "xauusd":
        return _get_mock_xauusd_candles(interval, limit)
    return _get_mock_candles(interval, limit)
