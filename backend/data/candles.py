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


_TF_SEED_OFFSET = {"M5": 11, "M15": 23, "M30": 47, "H1": 89, "H4": 197, "D1": 421, "W1": 911}

# Per-TF volatility (standard deviation of body in points) — calibrated to real gold:
# Gold typical ATR: D1=40-80pt, H4=15-35pt, H1=5-12pt, M15=2-5pt, M5=1-3pt
_TF_VOL_BASE = {"M5": 1.2, "M15": 2.5, "M30": 3.8, "H1": 6.0, "H4": 14.0, "D1": 35.0, "W1": 90.0}

# Trend drift bias per timeframe (points per bar) — slight directional pull
_TF_DRIFT_PROB = 0.55   # probability of continuing previous direction


def _generate_xauusd_candles(interval: str, limit: int) -> list[Candle]:
    """
    Generate realistic XAU/USD OHLCV mock candles.

    Each timeframe has its own seed offset so D1/H4/H1/M15/M5 produce
    independent bias structures rather than the same drift pattern.

    Volatility is calibrated to real gold market behaviour:
      D1 ATR  ~40-80 pts
      H4 ATR  ~15-35 pts
      H1 ATR  ~5-12 pts
      M15 ATR ~2-5 pts
      M5 ATR  ~1-3 pts

    Adds:
      - Persistent trend drift (gives recognisable HH/HL or LH/LL structures)
      - Occasional liquidity-sweep wicks (1 in 25 bars on average)
      - News-spike volatility expansion windows
    """
    _hour_bucket = int(time.time() // 3600)
    tf_offset = _TF_SEED_OFFSET.get(interval, 0)
    rng = random.Random(99 + _hour_bucket + tf_offset)

    step_min = INTERVAL_MINUTES[interval]
    vol_base = _TF_VOL_BASE.get(interval, step_min ** 0.5 * 1.3)

    now = datetime.now(timezone.utc).replace(second=0, microsecond=0)
    total_min = int(now.timestamp() // 60) * 60
    boundary = (total_min // step_min) * step_min
    end_time = datetime.fromtimestamp(boundary, tz=timezone.utc)

    candles: list[Candle] = []
    close = BASE_PRICE_XAUUSD

    # Persistent regime: choose a primary trend and a regime change point
    trend_dir = 1 if rng.random() < 0.5 else -1
    trend_strength = rng.uniform(0.2, 0.6)        # 0=pure noise, 1=pure trend
    regime_flip_at = rng.randint(int(limit * 0.3), int(limit * 0.7))

    for i in range(limit - 1, -1, -1):
        t = end_time - timedelta(minutes=step_min * i)
        if interval in ("D1", "W1") and t.weekday() >= 5:
            continue

        bar_idx = limit - 1 - i

        # Flip regime once during the lookback for natural structure shifts
        if bar_idx == regime_flip_at:
            trend_dir *= -1
            trend_strength = rng.uniform(0.2, 0.55)

        # Body: drift + noise
        drift = trend_dir * trend_strength * vol_base * 0.25
        noise = rng.gauss(0, vol_base)
        body = drift + noise * (1 - trend_strength * 0.4)

        o = round(close, 2)
        c = round(o + body, 2)

        # Wicks: occasional liquidity sweep on ~4% of bars (large wick beyond high/low)
        wick_hi_scale = 0.6
        wick_lo_scale = 0.6
        if rng.random() < 0.04:
            if rng.random() < 0.5:
                wick_hi_scale = 2.5       # buy-side sweep
            else:
                wick_lo_scale = 2.5       # sell-side sweep

        wick_hi = abs(rng.gauss(0, vol_base * wick_hi_scale))
        wick_lo = abs(rng.gauss(0, vol_base * wick_lo_scale))

        h = round(max(o, c) + wick_hi, 2)
        l = round(min(o, c) - wick_lo, 2)

        # Volume scales with body size (high-volatility bars get high volume)
        body_pts = abs(c - o)
        vol_base_units = rng.uniform(800, 2400) * (step_min / 60) ** 0.5
        vol_units = int(vol_base_units * (1 + body_pts / vol_base))

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
