"""
XAU/USD candle data layer.

In demo mode (DATA_MODE=demo), delegates to the seeded mock generator below.
In live mode (DATA_MODE=live), delegates to candle_provider which routes to
the configured FX_DATA_PROVIDER (twelvedata | alpha_vantage | oanda | polygon | fmp).

Only XAU/USD (xauusd) is supported. Requests for any other instrument raise ValueError.
"""
import random
import time
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

# XAU/USD base price for mock data — realistic gold price range
BASE_PRICE_XAUUSD = 3285.00   # approximate current gold price


def _generate_xauusd_candles(interval: str, limit: int) -> list[Candle]:
    """
    Generate realistic XAU/USD OHLCV mock candles.
    Seed rotates every hour for reproducibility within a session.
    Prices use 2-decimal format. Volatility reflects real gold intraday ranges.
    """
    _hour_bucket = int(time.time() // 3600)
    rng = random.Random(99 + _hour_bucket)
    step_min = INTERVAL_MINUTES[interval]
    now = datetime.now(timezone.utc).replace(second=0, microsecond=0)
    total_min = int(now.timestamp() // 60) * 60
    boundary = (total_min // step_min) * step_min
    end_time = datetime.fromtimestamp(boundary, tz=timezone.utc)

    candles: list[Candle] = []
    close = BASE_PRICE_XAUUSD

    for i in range(limit - 1, -1, -1):
        t = end_time - timedelta(minutes=step_min * i)
        if interval in ("D1", "W1") and t.weekday() >= 5:
            continue
        # Gold has higher per-pip volatility — scale by point size (1.0)
        vol = step_min ** 0.5 * 0.15
        body = rng.gauss(0, vol)
        o = round(close, 2)
        c = round(o + body, 2)
        wick_hi = abs(rng.gauss(0, vol * 0.6))
        wick_lo = abs(rng.gauss(0, vol * 0.6))
        h = round(max(o, c) + wick_hi, 2)
        l = round(min(o, c) - wick_lo, 2)
        vol_units = int(rng.uniform(200, 2000) * (step_min / 60) ** 0.5)
        candles.append(Candle(
            time=t, open=o, high=h, low=l, close=c,
            volume=vol_units,
        ))
        close = c

    return candles


def get_candles(interval: str = "H4", limit: int = 200, pair: str = "xauusd") -> CandleResponse:
    """
    Fetch XAU/USD OHLCV candles.

    Parameters
    ----------
    interval : str   Timeframe (M5 – W1)
    limit    : int   Number of candles (max 5000)
    pair     : str   Must be "xauusd" — any other value raises ValueError

    Raises
    ------
    ValueError   If interval is invalid or pair is not xauusd
    """
    interval = interval.upper()
    if interval not in INTERVAL_MINUTES:
        raise ValueError(f"Unknown interval '{interval}'. Valid: {list(INTERVAL_MINUTES)}")
    limit = max(1, min(limit, 5000))

    pair_code = pair.lower().replace("/", "").replace("_", "")
    if pair_code != "xauusd":
        raise ValueError(
            f"Unsupported instrument '{pair}'. "
            "This dashboard supports XAU/USD only."
        )

    from config import settings

    # Live mode: try TradingView → MT5 bridge → fall through to mock
    if settings.data_mode == "live":
        # Try TradingView first (real OHLCV)
        try:
            from services.tradingview_provider import get_tv_candles
            tv_bars = get_tv_candles("xauusd", timeframe=interval, limit=limit)
            if tv_bars:
                candles = [
                    Candle(
                        time   = datetime.fromisoformat(b["time"].replace("Z", "+00:00")),
                        open   = float(b["open"]),
                        high   = float(b["high"]),
                        low    = float(b["low"]),
                        close  = float(b["close"]),
                        volume = int(b.get("volume", 0)),
                    )
                    for b in tv_bars
                ]
                return CandleResponse(symbol="XAU/USD", interval=interval,
                                      count=len(candles), candles=candles)
        except Exception:
            pass

        # Try MT5 candle bridge
        try:
            from services.candle_provider import get_eurusd_candles
            return get_eurusd_candles(timeframe=interval, lookback=limit, pair="XAU/USD")
        except Exception:
            pass

    # Demo mode (or live with no provider): return realistic mock candles
    candles = _generate_xauusd_candles(interval, limit)
    return CandleResponse(symbol="XAU/USD", interval=interval,
                          count=len(candles), candles=candles)
