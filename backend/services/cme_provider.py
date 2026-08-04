"""
CME GC futures context.

CME's public web page doesn't expose GC price/OI in a scrape-friendly
JSON — the paid CME DataMine feed does, but we don't ship a key.
Practical substitute: use TwelveData for GC futures prices (symbol
GC or GC=F depending on subscription tier) and lean on our existing
CFTC COT data (services.cot_provider) for positioning direction.

Same fail-open contract as fastbull_provider — any failure returns
available=False and the confluence layer treats it as neutral.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

log = logging.getLogger(__name__)


def _empty(reason: str = "no data") -> dict:
    return {
        "available":                False,
        "gc_price":                 None,
        "gc_spot_basis":            None,
        "volume_context":           "unknown",
        "open_interest_context":    "unknown",
        "futures_bias":             "unknown",
        "cot_commercial_net":       None,
        "interpretation":           f"unavailable — {reason}",
        "fetched_at":               datetime.now(timezone.utc).isoformat(),
        "raw_symbol":               "GC",
    }


def _pull_gc_price(timeout_s: float) -> Optional[dict]:
    """Best-effort TwelveData pull for GC futures. Returns close+prev or None."""
    try:
        from services.candle_provider import get_twelvedata_candles
        # TwelveData accepts several futures symbol notations. Try GC first,
        # then fall back to GC=F. If neither works, we return None.
        for sym in ("GC", "GC=F"):
            try:
                r = get_twelvedata_candles(interval="H1", lookback=5, symbol=sym)
                if r and r.candles:
                    latest, prev = r.candles[-1], r.candles[-2] if len(r.candles) >= 2 else r.candles[-1]
                    vols = [(c.volume or 0) for c in r.candles]
                    return {
                        "close":       float(latest.close),
                        "prev_close":  float(prev.close),
                        "vol_avg":     sum(vols) / max(1, len(vols)),
                        "vol_latest":  float(latest.volume or 0),
                        "symbol":      sym,
                    }
            except Exception as exc:
                log.debug("[cme] %s pull failed: %s", sym, exc)
                continue
    except Exception as exc:
        log.debug("[cme] pull-gc-price top-level: %s", exc)
    return None


def _pull_cot_net(db) -> Optional[float]:
    """Latest COT commercial-net position. None on any failure."""
    try:
        from services.cot_provider import get_latest_cot
        row = get_latest_cot(db=db, instrument="GC")
        if row and getattr(row, "commercial_net", None) is not None:
            return float(row.commercial_net)
    except Exception as exc:
        log.debug("[cme] cot pull failed: %s", exc)
    return None


def _classify_volume(vol_latest: float, vol_avg: float) -> str:
    if vol_avg <= 0:
        return "unknown"
    ratio = vol_latest / vol_avg
    if ratio >= 1.30: return "high"
    if ratio <= 0.70: return "low"
    return "normal"


def _classify_oi_trend(cot_net: Optional[float]) -> str:
    """COT net-position trend proxy. Actual OI direction needs a series;
    for now we only surface the current commercial-net sign and let the
    aggregator interpret. Returns 'unknown' by default until we have a
    time-series in the DB."""
    if cot_net is None:
        return "unknown"
    return "flat"   # placeholder — sign is used elsewhere


def _classify_futures_bias(
    gc: Optional[dict],
    cot_net: Optional[float],
    spot_price: Optional[float] = None,
) -> tuple[str, str, Optional[float]]:
    """Return (bias, interpretation, basis)."""
    if gc is None:
        return ("unknown", "GC futures price unavailable", None)

    # Direction of GC move H1
    move = gc["close"] - gc["prev_close"]
    move_bias = "bullish" if move > 0.5 else ("bearish" if move < -0.5 else "neutral")

    # Spot-futures basis (positive contango is normal for gold)
    basis = None
    if spot_price is not None:
        basis = round(gc["close"] - spot_price, 2)

    # Commercial net position (contrarian)
    commercial = ""
    if cot_net is not None:
        if cot_net < -50000:
            commercial = " · Commercials net-short (contrarian bullish)"
        elif cot_net > 50000:
            commercial = " · Commercials net-long (contrarian bearish)"

    interp = f"GC H1 {move:+.2f} ({move_bias}){commercial}"
    if basis is not None:
        interp += f" · Basis vs spot {basis:+.2f}"

    return (move_bias, interp, basis)


def fetch_cme_context(db=None, timeout_s: float = 3.0,
                       spot_price: Optional[float] = None) -> dict:
    """Public entry. Never raises."""
    gc = _pull_gc_price(timeout_s)
    if gc is None:
        return _empty("GC futures unreachable")

    cot_net = _pull_cot_net(db) if db is not None else None

    vol_ctx = _classify_volume(gc["vol_latest"], gc["vol_avg"])
    oi_ctx  = _classify_oi_trend(cot_net)
    bias, interp, basis = _classify_futures_bias(gc, cot_net, spot_price)

    return {
        "available":                True,
        "gc_price":                 gc["close"],
        "gc_spot_basis":            basis,
        "volume_context":           vol_ctx,
        "open_interest_context":    oi_ctx,
        "futures_bias":             bias,
        "cot_commercial_net":       cot_net,
        "interpretation":           interp,
        "fetched_at":               datetime.now(timezone.utc).isoformat(),
        "raw_symbol":               gc["symbol"],
    }


__all__ = ["fetch_cme_context"]
