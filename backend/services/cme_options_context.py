"""Read-only CME gold options context for Strategist/Predator.

Purpose: give the live engine a *location/context* layer derived from the
latest accepted CME daily bulletin without pretending that unsigned OI is a
directional dealer-GEX signal.

Outputs:
  - latest bulletin date/status and age
  - current GC/XAU basis from the engine's own live bars
  - raw GC strikes mapped into XAUUSD-equivalent levels
  - call/put/total OI, OI change, and a delta-sensitivity-weighted OI score
  - nearest and strongest concentration zones

The `sensitivity_score` is NOT GEX. It simply weights OI highest when the
CME-published |delta| is near 0.50 and lower as delta approaches 0 or 1.
Signed dealer gamma is not inferred.

The first live caller starts one daemon refresh worker. It performs an
immediate bootstrap pull, then retries CME at the publication windows without
blocking Strategist/Predator. A failed CME fetch never deletes the last
accepted bulletin.
"""
from __future__ import annotations

import logging
import os
import threading
import time
from collections import defaultdict
from datetime import date, datetime, timezone
from typing import Any, Optional

from sqlalchemy import text
from sqlalchemy.orm import Session

log = logging.getLogger(__name__)

_STD_GOLD_PRODUCTS = ("OG", "OG1", "OG2", "OG3", "OG4", "GMW", "GWR", "GWT", "GWW")
_REFRESH_LOCK = threading.Lock()
_REFRESH_THREAD: threading.Thread | None = None


def _refresh_slot(now: datetime) -> str:
    """Publication slot key. Also gives every backend boot one bootstrap pull."""
    if now.hour >= 15:
        return f"{now.date().isoformat()}:FINAL"
    if now.hour >= 5:
        return f"{now.date().isoformat()}:PRELIM"
    return f"{now.date().isoformat()}:BOOTSTRAP"


def _refresh_worker() -> None:
    last_ok_slot: str | None = None
    # Retry failures every 30m. Successful slot sleeps until a new publication
    # window appears, while still waking every 5m to notice the transition.
    while True:
        try:
            now = datetime.now(timezone.utc)
            slot = _refresh_slot(now)
            if slot != last_ok_slot:
                from research.gold_intel.cme_live_refresh import refresh_cme_bulletins
                result = refresh_cme_bulletins()
                if result.get("status") == "OK":
                    last_ok_slot = slot
                    log.info(
                        "[cme_context] autonomous refresh OK slot=%s bulletin=%s %s",
                        slot, result.get("bulletin_date"), result.get("bulletin_status"),
                    )
                    time.sleep(300)
                    continue
                log.warning(
                    "[cme_context] autonomous refresh failed slot=%s detail=%s",
                    slot, result.get("detail"),
                )
                time.sleep(1800)
                continue
        except Exception as exc:
            log.warning("[cme_context] refresh worker error: %s", exc)
            time.sleep(1800)
            continue
        time.sleep(300)


def ensure_cme_refresh_worker() -> bool:
    """Start the single process-local daemon worker. Returns True if running."""
    global _REFRESH_THREAD
    if str(os.getenv("CME_OPTIONS_CONTEXT_REFRESH_ENABLED", "true")).lower() not in ("1", "true", "yes"):
        return False
    with _REFRESH_LOCK:
        if _REFRESH_THREAD and _REFRESH_THREAD.is_alive():
            return True
        _REFRESH_THREAD = threading.Thread(
            target=_refresh_worker,
            name="cme-options-refresh",
            daemon=True,
        )
        _REFRESH_THREAD.start()
        log.info("[cme_context] autonomous CME refresh worker started")
        return True


def _latest_price(db: Session, sql: str, params: Optional[dict] = None) -> Optional[float]:
    try:
        row = db.execute(text(sql), params or {}).fetchone()
        return float(row[0]) if row and row[0] is not None else None
    except Exception:
        return None


def _canonical_bulletin(db: Session) -> tuple[Optional[str], Optional[str]]:
    row = db.execute(text("""
        SELECT bulletin_date, bulletin_status
        FROM cme_gc_options_eod
        ORDER BY bulletin_date DESC,
                 CASE bulletin_status WHEN 'FINAL' THEN 2 WHEN 'PRELIMINARY' THEN 1 ELSE 0 END DESC
        LIMIT 1
    """)).fetchone()
    return (str(row[0]), str(row[1])) if row else (None, None)


def _sensitivity_weight(delta: Optional[float]) -> float:
    """0..1 weight peaking at |delta|=.50. NOT gamma/GEX."""
    if delta is None:
        return 0.25
    d = min(1.0, max(0.0, abs(float(delta))))
    return max(0.0, 1.0 - 2.0 * abs(d - 0.5))


def get_cme_options_context(
    db: Session,
    *,
    current_xau: Optional[float] = None,
    max_raw_distance: float = 300.0,
    top_n: int = 8,
) -> dict[str, Any]:
    # Non-blocking: this only starts a daemon; HTTP/PDF work happens off-thread.
    ensure_cme_refresh_worker()

    try:
        bulletin_date, bulletin_status = _canonical_bulletin(db)
    except Exception as exc:
        return {
            "status": "UNAVAILABLE",
            "reason": f"CME tables unavailable: {type(exc).__name__}: {exc}",
            "directional_bias": "UNSIGNED_NEUTRAL",
            "gamma_status": "NOT_COMPUTED",
        }

    if not bulletin_date:
        return {
            "status": "NO_DATA",
            "reason": "No accepted CME gold options bulletin in database",
            "directional_bias": "UNSIGNED_NEUTRAL",
            "gamma_status": "NOT_COMPUTED",
        }

    try:
        bd = date.fromisoformat(bulletin_date[:10])
        age_days = (datetime.now(timezone.utc).date() - bd).days
    except Exception:
        age_days = 999

    gc_price = _latest_price(
        db,
        "SELECT close FROM gc_futures_bars ORDER BY candle_time DESC LIMIT 1",
    )
    if current_xau is None:
        current_xau = _latest_price(
            db,
            "SELECT close FROM historical_candles WHERE instrument='XAU/USD' AND timeframe='M5' "
            "ORDER BY candle_time DESC LIMIT 1",
        )
    basis = (gc_price - current_xau) if (gc_price is not None and current_xau is not None) else None

    if age_days > 5:
        return {
            "status": "STALE",
            "bulletin_date": bulletin_date,
            "bulletin_status": bulletin_status,
            "age_days": age_days,
            "gc_price": gc_price,
            "xau_price": current_xau,
            "gc_xau_basis": basis,
            "directional_bias": "UNSIGNED_NEUTRAL",
            "gamma_status": "NOT_COMPUTED_STALE_SOURCE",
            "zones": [],
            "reason": "Latest CME bulletin is too old for live decision context",
        }

    placeholders = ",".join(f":p{i}" for i in range(len(_STD_GOLD_PRODUCTS)))
    params = {"d": bulletin_date, "s": bulletin_status}
    params.update({f"p{i}": p for i, p in enumerate(_STD_GOLD_PRODUCTS)})
    rows = db.execute(text(f"""
        SELECT product_code, option_expiry_code, option_type, strike,
               open_interest, open_interest_change, delta_cme
        FROM cme_gc_options_eod
        WHERE bulletin_date=:d AND bulletin_status=:s
          AND product_code IN ({placeholders})
          AND open_interest IS NOT NULL
    """), params).fetchall()

    agg: dict[float, dict[str, Any]] = defaultdict(lambda: {
        "call_oi": 0, "put_oi": 0, "total_oi": 0,
        "oi_change": 0, "sensitivity_score": 0.0,
        "contracts": 0, "expiries": set(), "products": set(),
    })

    for product, expiry, opt_type, strike, oi, oi_change, delta in rows:
        try:
            k = float(strike); oi_i = int(oi or 0)
        except Exception:
            continue
        if gc_price is not None and abs(k - gc_price) > max_raw_distance:
            continue
        a = agg[k]
        if str(opt_type).upper() == "CALL":
            a["call_oi"] += oi_i
        elif str(opt_type).upper() == "PUT":
            a["put_oi"] += oi_i
        a["total_oi"] += oi_i
        a["oi_change"] += int(oi_change or 0)
        a["sensitivity_score"] += oi_i * _sensitivity_weight(delta)
        a["contracts"] += 1
        a["expiries"].add(str(expiry))
        a["products"].add(str(product))

    zones = []
    for strike, a in agg.items():
        mapped = strike - basis if basis is not None else None
        zones.append({
            "gc_strike": round(strike, 2),
            "xau_equiv": round(mapped, 2) if mapped is not None else None,
            "call_oi": a["call_oi"],
            "put_oi": a["put_oi"],
            "total_oi": a["total_oi"],
            "oi_change": a["oi_change"],
            "sensitivity_score": round(a["sensitivity_score"], 2),
            "expiries": sorted(a["expiries"]),
            "products": sorted(a["products"]),
            "distance_to_xau": (
                round(abs(mapped - current_xau), 2)
                if mapped is not None and current_xau is not None else None
            ),
        })

    strongest = sorted(zones, key=lambda z: (z["sensitivity_score"], z["total_oi"]), reverse=True)[:top_n]
    nearest = sorted(
        [z for z in zones if z["distance_to_xau"] is not None],
        key=lambda z: z["distance_to_xau"],
    )[:top_n]
    biggest_change = sorted(zones, key=lambda z: abs(z["oi_change"]), reverse=True)[:top_n]

    return {
        "status": "OBSERVED",
        "bulletin_date": bulletin_date,
        "bulletin_status": bulletin_status,
        "age_days": age_days,
        "gc_price": round(gc_price, 2) if gc_price is not None else None,
        "xau_price": round(current_xau, 2) if current_xau is not None else None,
        "gc_xau_basis": round(basis, 2) if basis is not None else None,
        "basis_status": "LIVE_ENGINE_BASIS" if basis is not None else "UNAVAILABLE",
        "directional_bias": "UNSIGNED_NEUTRAL",
        "gamma_status": "NOT_COMPUTED_EXACT_GEX_PENDING_VALIDATED_EXPIRY_T",
        "metric_note": (
            "sensitivity_score = OI weighted by proximity of CME-published |delta| to 0.50; "
            "it is a concentration/location metric, not signed dealer GEX"
        ),
        "strongest_zones": strongest,
        "nearest_zones": nearest,
        "largest_oi_changes": biggest_change,
        "zone_count": len(zones),
    }
