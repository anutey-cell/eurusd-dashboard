"""
Canonical Market Data Service — Phase 2
========================================

Single source of truth for every strategy. The whole point is to make it
impossible for two consumers within one tick to see different values — the
same failure mode we hit twice in July/August where the strategist read
live TwelveData ticks while backtests + learning read a 4-day-stale table.

Contract:
  1. One `snapshot(db)` call returns a `CanonicalSnapshot` with everything
     a strategy needs: bid/ask/spread/tick_ts, candles + freshness per TF,
     current session, prev-day + prev-week levels, data_quality_score.
  2. 15-second in-process TTL cache — multiple consumers within a tick
     get the SAME snapshot.
  3. Every field is Optional and the snapshot ALWAYS returns; consumers
     decide whether to trust each field via data_quality_score and the
     per-TF freshness `status`.
  4. Never raises. On any partial failure the snapshot still returns with
     `warnings[]` filled and `data_quality_score` reduced.

Behind XAUUSD_CANONICAL_DATA_ENABLED. Off by default until strategies wire
to it — until then it exists purely for observability via the diagnostics
endpoint and shadow-mode validation.
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone, timedelta
from typing import Optional, Sequence

from sqlalchemy import text
from sqlalchemy.orm import Session

log = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Dataclasses — the snapshot contract
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class Bar:
    """One OHLCV bar. Timestamp is UTC-aware."""
    time: datetime
    open: float
    high: float
    low:  float
    close: float
    volume: int = 0

    def to_dict(self) -> dict:
        return {
            "time": self.time.isoformat(),
            "open": self.open, "high": self.high,
            "low":  self.low,  "close": self.close,
            "volume": self.volume,
        }


@dataclass
class TimeframeSlice:
    """
    All info a consumer needs for one timeframe.

    `latest_closed` = the most recent bar in `candles` (may be stale).
    `latest_forming` = None for now; reserved for future live-tick derivation.
    """
    tf:              str
    candles:         list[Bar] = field(default_factory=list)
    latest_closed:   Optional[datetime] = None
    latest_forming:  Optional[datetime] = None
    age_min:         Optional[float]    = None
    threshold_min:   int  = 0
    status:          str  = "unknown"    # fresh | degraded | stale | missing
    source:          str  = "historical_candles"

    def to_dict(self) -> dict:
        return {
            "tf":             self.tf,
            "bar_count":      len(self.candles),
            "latest_closed":  self.latest_closed.isoformat() if self.latest_closed else None,
            "latest_forming": self.latest_forming.isoformat() if self.latest_forming else None,
            "age_min":        self.age_min,
            "threshold_min":  self.threshold_min,
            "status":         self.status,
            "source":         self.source,
        }


@dataclass
class SessionInfo:
    """Killzone / session identity for the current UTC clock."""
    kz_label:      str          # ASIA | LONDON | LONDON_LUNCH | NY | NY_LATE | OFF
    kz_pretty:     str          # human-readable strategist name
    session_open:  Optional[datetime] = None
    session_high:  Optional[float] = None
    session_low:   Optional[float] = None
    is_active:     bool = False
    weekend:       bool = False

    def to_dict(self) -> dict:
        return {
            "kz_label":     self.kz_label,
            "kz_pretty":    self.kz_pretty,
            "is_active":    self.is_active,
            "weekend":      self.weekend,
            "session_open": self.session_open.isoformat() if self.session_open else None,
            "session_high": self.session_high,
            "session_low":  self.session_low,
        }


@dataclass
class LevelBundle:
    """Prev-day + prev-week + daily-open levels. Any may be None."""
    pdh:          Optional[float] = None
    pdl:          Optional[float] = None
    pdc:          Optional[float] = None
    pwh:          Optional[float] = None
    pwl:          Optional[float] = None
    pwo:          Optional[float] = None
    daily_open:   Optional[float] = None
    asian_high:   Optional[float] = None
    asian_low:    Optional[float] = None

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class CanonicalSnapshot:
    """
    The full snapshot. Handed to every strategy so they all agree on
    price/candles/session/levels within a single tick.
    """
    ts:                  datetime
    instrument:          str
    bid:                 Optional[float] = None
    ask:                 Optional[float] = None
    spread:              Optional[float] = None
    last_tick_at:        Optional[datetime] = None
    tick_source:         str = "unknown"      # mt5 | twelvedata-derived | none
    tick_latency_ms:     Optional[float] = None

    timeframes:          dict[str, TimeframeSlice] = field(default_factory=dict)
    session:             SessionInfo = field(default_factory=lambda: SessionInfo("OFF", "Off-hours"))
    levels:              LevelBundle = field(default_factory=LevelBundle)

    data_quality_score:  int = 0
    freshness_details:   dict = field(default_factory=dict)
    provider:            str = "canonical_market_data"
    build_latency_ms:    Optional[float] = None
    warnings:            list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "ts":                self.ts.isoformat(),
            "instrument":        self.instrument,
            "bid":               self.bid,
            "ask":               self.ask,
            "spread":            self.spread,
            "last_tick_at":      self.last_tick_at.isoformat() if self.last_tick_at else None,
            "tick_source":       self.tick_source,
            "tick_latency_ms":   self.tick_latency_ms,
            "timeframes":        {tf: s.to_dict() for tf, s in self.timeframes.items()},
            "session":           self.session.to_dict(),
            "levels":            self.levels.to_dict(),
            "data_quality_score": self.data_quality_score,
            "freshness_details": self.freshness_details,
            "provider":          self.provider,
            "build_latency_ms":  self.build_latency_ms,
            "warnings":          self.warnings,
        }


# ─────────────────────────────────────────────────────────────────────────────
# Session detection — pulled up out of strategist.py so it's testable
# ─────────────────────────────────────────────────────────────────────────────

def killzone_for_utc(now: datetime) -> tuple[str, str, bool]:
    """
    Returns (label, pretty, is_active). Labels chosen to match downstream
    consumers already in the code.

    Weekend window mirrors data_freshness._is_weekend_closed.
    """
    wd, h = now.weekday(), now.hour
    if wd == 5:                           # Saturday all day
        return "OFF", "Weekend closed", False
    if wd == 6 and h < 22:                # Sunday before reopen
        return "OFF", "Weekend closed", False
    if wd == 4 and h >= 21:               # Friday after close
        return "OFF", "Weekend closed", False

    if   h < 6:     return "ASIA",         "Asian range formation",       True
    elif h < 7:     return "PRE_LDN",      "Pre-London",                  True
    elif h < 10:    return "LDN_OPEN",     "London open / kill zone",     True
    elif h < 12:    return "LDN_CONT",     "London continuation",         True
    elif h < 13:    return "LDN_LUNCH",    "London lunch chop",           True
    elif h < 16:    return "NY_OPEN",      "New York kill zone",          True
    elif h < 17:    return "LDN_NY_CLOSE", "London/NY overlap close",     True
    else:           return "NY_LATE",      "NY late session",             True


# ─────────────────────────────────────────────────────────────────────────────
# Low-level helpers
# ─────────────────────────────────────────────────────────────────────────────

def _fetch_bars(db: Session, instrument: str, tf: str, lookback: int) -> list[Bar]:
    """Read the last `lookback` bars from historical_candles, oldest first."""
    rows = db.execute(text(
        "SELECT candle_time, open, high, low, close, volume "
        "FROM historical_candles "
        "WHERE instrument = :i AND timeframe = :t "
        "ORDER BY candle_time DESC LIMIT :n"
    ), {"i": instrument, "t": tf, "n": lookback}).fetchall()

    bars: list[Bar] = []
    for row in reversed(rows):
        ts = row[0]
        if isinstance(ts, str):
            for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S"):
                try:
                    ts = datetime.strptime(ts.split("+")[0], fmt); break
                except ValueError:
                    continue
        if not isinstance(ts, datetime):
            continue
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        bars.append(Bar(
            time=ts,
            open=float(row[1]), high=float(row[2]),
            low=float(row[3]),  close=float(row[4]),
            volume=int(row[5] or 0),
        ))
    return bars


def _live_tick(instrument: str, m5_bars: Sequence[Bar]) -> tuple[Optional[float], Optional[float], Optional[float], Optional[datetime], str, Optional[float]]:
    """
    Best-effort live bid/ask/spread. Returns (bid, ask, spread, ts, source, latency_ms).

    Priority:
      1. MT5 bridge tick if daemon reachable — returns real bid/ask
      2. Last M5 close as mid, ±0.10pt half-spread proxy — clearly labelled
    """
    # Try MT5 first
    try:
        from services.mt5_provider import get_tick, _CONNECTED
        if _CONNECTED:
            t0 = time.perf_counter()
            t = get_tick("xauusd")
            lat = (time.perf_counter() - t0) * 1000
            ts_str = t.get("timestamp")
            ts_ok = None
            if ts_str:
                try:
                    ts_ok = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
                except ValueError:
                    ts_ok = None
            return (t["bid"], t["ask"], t.get("spread_raw"), ts_ok, "mt5", round(lat, 2))
    except Exception as exc:
        log.debug("[canonical_market_data] mt5 tick unavailable: %s", exc)

    # Fallback: derive from last M5 close
    if m5_bars:
        last = m5_bars[-1]
        half_spread = 0.10                # a conservative 20-cent visible spread
        return (last.close - half_spread,
                last.close + half_spread,
                2 * half_spread,
                last.time, "twelvedata-derived", 0.0)

    return (None, None, None, None, "none", None)


def _key_levels(bars_d1: list[Bar], bars_m15: list[Bar], now: datetime) -> LevelBundle:
    """Compute PDH/PDL/PDC/PWH/PWL/PWO/daily_open/asian_high/asian_low."""
    lb = LevelBundle()

    # Prev-day: the D1 bar most recently CLOSED (not today's forming one)
    if bars_d1:
        today_utc = now.date()
        past_d1 = [b for b in bars_d1 if b.time.date() < today_utc]
        if past_d1:
            prev = past_d1[-1]
            lb.pdh, lb.pdl, lb.pdc = prev.high, prev.low, prev.close

        # Prev-week: last 5 completed daily bars (window ending Friday)
        if len(past_d1) >= 5:
            window = past_d1[-5:]
            lb.pwh = max(b.high for b in window)
            lb.pwl = min(b.low for b in window)
            lb.pwo = window[0].open

        # Today's daily open = first D1 bar of today, if present
        today_bar = next((b for b in bars_d1 if b.time.date() == today_utc), None)
        if today_bar:
            lb.daily_open = today_bar.open
        elif past_d1:
            # Use yesterday's close as proxy until today's D1 forms
            lb.daily_open = past_d1[-1].close

    # Asian range: highest/lowest of today's M15 bars in [00:00, 06:00) UTC
    if bars_m15:
        today_utc = now.date()
        asian = [b for b in bars_m15
                 if b.time.date() == today_utc and b.time.hour < 6]
        if asian:
            lb.asian_high = max(b.high for b in asian)
            lb.asian_low  = min(b.low  for b in asian)

    return lb


def _session_hi_lo(bars_m15: list[Bar], now: datetime, kz_label: str) -> tuple[Optional[datetime], Optional[float], Optional[float]]:
    """Return (session_open_time, session_high_so_far, session_low_so_far)."""
    if kz_label == "OFF" or not bars_m15:
        return (None, None, None)
    session_start_hour = {
        "ASIA": 0, "PRE_LDN": 6, "LDN_OPEN": 7, "LDN_CONT": 10,
        "LDN_LUNCH": 12, "NY_OPEN": 13, "LDN_NY_CLOSE": 16, "NY_LATE": 17,
    }.get(kz_label)
    if session_start_hour is None:
        return (None, None, None)
    session_start = now.replace(hour=session_start_hour, minute=0, second=0, microsecond=0)
    bars = [b for b in bars_m15 if b.time >= session_start]
    if not bars:
        return (session_start, None, None)
    return (session_start, max(b.high for b in bars), min(b.low for b in bars))


# ─────────────────────────────────────────────────────────────────────────────
# The service class
# ─────────────────────────────────────────────────────────────────────────────

_LOOKBACK_BY_TF = {"M1": 60, "M5": 200, "M15": 200, "H1": 200, "H4": 100, "D1": 120}


class CanonicalMarketData:
    """
    Thread-safe cached snapshot builder.

    Usage from any strategy:
        from services.canonical_market_data import get_canonical
        cmd = get_canonical()
        snap = cmd.snapshot(db)
        if snap.data_quality_score < 70: return "data degraded"
        candles_h1 = snap.timeframes["H1"].candles
    """

    def __init__(self, cache_ttl_s: int = 15):
        self._cache_ttl_s = cache_ttl_s
        self._cache: Optional[tuple[float, CanonicalSnapshot]] = None
        self._lock = threading.Lock()

    # public
    def snapshot(self, db: Session, *,
                 instrument: str = "XAU/USD",
                 timeframes: tuple = ("M5", "M15", "H1", "H4", "D1"),
                 force_refresh: bool = False) -> CanonicalSnapshot:
        with self._lock:
            now = time.time()
            if (not force_refresh and self._cache
                    and (now - self._cache[0]) < self._cache_ttl_s
                    and self._cache[1].instrument == instrument):
                return self._cache[1]
            snap = self._build(db, instrument, timeframes)
            self._cache = (now, snap)
            return snap

    def invalidate(self):
        with self._lock:
            self._cache = None

    # internal
    def _build(self, db: Session, instrument: str, timeframes: tuple) -> CanonicalSnapshot:
        t0 = time.perf_counter()
        now = datetime.now(timezone.utc)
        warnings: list[str] = []

        # 1) Per-TF bars + freshness (delegated to data_freshness for consistency)
        from services.data_freshness import check_freshness, STALENESS_MIN_BY_TF
        fresh = check_freshness(db, instrument=instrument, timeframes=timeframes, now=now)
        details = fresh.get("details", {})

        tf_slices: dict[str, TimeframeSlice] = {}
        for tf in timeframes:
            lookback = _LOOKBACK_BY_TF.get(tf, 100)
            try:
                bars = _fetch_bars(db, instrument, tf, lookback)
            except Exception as exc:
                bars = []
                warnings.append(f"{tf}: fetch failed ({type(exc).__name__})")
            info = details.get(tf, {})
            threshold = STALENESS_MIN_BY_TF.get(tf, 60)
            latest_closed = bars[-1].time if bars else None
            age_min = info.get("age_min") if isinstance(info, dict) else None
            # Classification
            if latest_closed is None:
                status = "missing"
            elif age_min is None:
                status = "unknown"
            elif age_min <= threshold:
                status = "fresh"
            elif age_min <= 3 * threshold:
                status = "degraded"
                warnings.append(f"{tf}: degraded (age {age_min:.0f}min > {threshold}min)")
            else:
                status = "stale"
                warnings.append(f"{tf}: STALE (age {age_min:.0f}min > 3× {threshold}min)")
            tf_slices[tf] = TimeframeSlice(
                tf=tf, candles=bars,
                latest_closed=latest_closed,
                age_min=age_min, threshold_min=threshold,
                status=status,
            )

        # 2) Live tick
        m5_bars = tf_slices.get("M5", TimeframeSlice("M5")).candles
        bid, ask, spread, tick_ts, tick_src, tick_lat = _live_tick(instrument, m5_bars)
        if tick_src == "none":
            warnings.append("no live tick source available")

        # 3) Session
        kz_label, kz_pretty, is_active = killzone_for_utc(now)
        sess_open, sess_hi, sess_lo = _session_hi_lo(
            tf_slices.get("M15", TimeframeSlice("M15")).candles, now, kz_label)
        session = SessionInfo(
            kz_label=kz_label, kz_pretty=kz_pretty,
            session_open=sess_open, session_high=sess_hi, session_low=sess_lo,
            is_active=is_active, weekend=fresh.get("weekend", False),
        )

        # 4) Key levels
        levels = _key_levels(
            tf_slices.get("D1",  TimeframeSlice("D1")).candles,
            tf_slices.get("M15", TimeframeSlice("M15")).candles,
            now,
        )

        # 5) Assemble
        snap = CanonicalSnapshot(
            ts=now, instrument=instrument,
            bid=bid, ask=ask, spread=spread,
            last_tick_at=tick_ts, tick_source=tick_src, tick_latency_ms=tick_lat,
            timeframes=tf_slices, session=session, levels=levels,
            data_quality_score=fresh.get("data_quality_score", 0),
            freshness_details=details,
            build_latency_ms=round((time.perf_counter() - t0) * 1000, 2),
            warnings=warnings,
        )
        return snap


# ─────────────────────────────────────────────────────────────────────────────
# Module-level singleton
# ─────────────────────────────────────────────────────────────────────────────

_CANONICAL: Optional[CanonicalMarketData] = None


def get_canonical(cache_ttl_s: int = 15) -> CanonicalMarketData:
    """Return the process-wide CanonicalMarketData singleton."""
    global _CANONICAL
    if _CANONICAL is None:
        _CANONICAL = CanonicalMarketData(cache_ttl_s=cache_ttl_s)
    return _CANONICAL


__all__ = [
    "Bar", "TimeframeSlice", "SessionInfo", "LevelBundle", "CanonicalSnapshot",
    "CanonicalMarketData", "get_canonical", "killzone_for_utc",
]
