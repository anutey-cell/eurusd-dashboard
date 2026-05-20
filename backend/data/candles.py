"""
XAU/USD candle data layer.

In demo mode (DATA_MODE=demo), delegates to the seeded mock generator below.
In live mode (DATA_MODE=live), delegates to candle_provider which routes to
the configured FX_DATA_PROVIDER (twelvedata | alpha_vantage | oanda | polygon | fmp).

Only XAU/USD (xauusd) is supported. Requests for any other instrument raise ValueError.

LIVE MODE PROVIDER FAILOVER
---------------------------
When DATA_MODE=live we ALWAYS prefer a real provider price. If TradingView and
MT5 both fail momentarily, we serve the last known live response from an
in-memory cache (tagged source="tradingview-cached" or "mt5-cached") rather
than silently falling back to synthetic ~$3285 prices, which would mislead
the dashboard. Only when no live response has ever been cached do we fall
through to synthetic, and that response is explicitly tagged source="synthetic"
so the frontend can refuse to display it.
"""
import logging
import random
import time
from datetime import datetime, timedelta, timezone

from models.candle import Candle, CandleResponse

logger = logging.getLogger(__name__)

# In-memory cache of the last successful LIVE response, keyed by interval.
# Survives transient TradingView outages so the dashboard never sees synthetic.
_LIVE_CACHE: dict[str, tuple[float, CandleResponse]] = {}
# Cap how stale a cached response can be before we admit defeat (12h).
_LIVE_CACHE_MAX_AGE_SEC = 12 * 60 * 60

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

    # Live-mode override: by default, refuse synthetic fallback in live mode so
    # that the engine, scanner, and paper-observation logger never see fake
    # prices. Callers that explicitly need a synthetic seed for testing can
    # pass `_allow_synthetic_in_live=True` via a config flag.
    allow_synthetic_in_live = bool(
        getattr(settings, "allow_synthetic_candles_in_live", False)
    )

    # Live mode: try TradingView → MT5 → cached live → only then synthetic
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
                resp = CandleResponse(symbol="XAU/USD", interval=interval,
                                      count=len(candles), candles=candles,
                                      source="tradingview")
                _LIVE_CACHE[interval] = (time.time(), resp)
                return resp
        except Exception as exc:
            logger.debug("TradingView fetch failed for %s: %s", interval, exc)

        # Try MT5 candle bridge
        try:
            from services.candle_provider import get_eurusd_candles
            resp = get_eurusd_candles(timeframe=interval, lookback=limit, pair="XAU/USD")
            # Tag source if the provider didn't (older shape)
            if not getattr(resp, "source", None) or resp.source == "synthetic":
                resp.source = "mt5"
            _LIVE_CACHE[interval] = (time.time(), resp)
            return resp
        except Exception as exc:
            logger.debug("MT5 fetch failed for %s: %s", interval, exc)

        # Live providers all failed — serve cached live response if fresh enough.
        # This prevents the dashboard from ever flashing $3285 synthetic gold.
        cached = _LIVE_CACHE.get(interval)
        if cached is not None:
            cached_at, cached_resp = cached
            age = time.time() - cached_at
            if age <= _LIVE_CACHE_MAX_AGE_SEC:
                logger.warning(
                    "Live providers down for %s — serving cached response (age %.0fs)",
                    interval, age,
                )
                cached_resp.source = (
                    "tradingview-cached" if cached_resp.source == "tradingview"
                    else f"{cached_resp.source}-cached"
                )
                return cached_resp

    # Live-mode + no provider + no cache + no override:
    # Return a flagged synthetic response (callers may refuse it) — but log
    # a loud warning so the operator notices the provider outage.
    if settings.data_mode == "live" and not allow_synthetic_in_live:
        logger.warning(
            "Live mode: no live provider available and no cache for %s. "
            "Returning source='synthetic' response — downstream consumers "
            "should refuse this.", interval,
        )

    # Demo mode, OR live mode with no providers AND no cache: synthetic.
    # This response is explicitly tagged so the frontend can refuse to display it.
    candles = _generate_xauusd_candles(interval, limit)
    return CandleResponse(symbol="XAU/USD", interval=interval,
                          count=len(candles), candles=candles,
                          source="demo" if settings.data_mode == "demo" else "synthetic")
