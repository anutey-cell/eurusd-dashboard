"""Read-only CME gold options context for Strategist/Predator.

The engine accepts two independently validated CME context sources:
1) the parsed CME PDF database (legacy/research ingestion), and
2) a structured whole-surface snapshot produced by ChatGPT retrieval and
   published to the dedicated GitHub snapshot branch.

The fresher valid source wins. Both fail closed on stale dates. The options
metric remains unsigned: sensitivity_score is OI weighted by proximity of
CME-published |delta| to 0.50. It is NOT signed dealer GEX.
"""
from __future__ import annotations

import logging
import os
import threading
import time
from collections import defaultdict
from datetime import date, datetime, timezone
from typing import Any, Optional

import requests
from sqlalchemy import text
from sqlalchemy.orm import Session

log = logging.getLogger(__name__)

_STD_GOLD_PRODUCTS = ("OG", "OG1", "OG2", "OG3", "OG4", "GMW", "GWR", "GWT", "GWW")

# Legacy direct-CME PDF worker. Production currently disables this because CME
# returns Akamai 403 to hosted clients; kept for future recovery/testing.
_REFRESH_LOCK = threading.Lock()
_REFRESH_THREAD: threading.Thread | None = None

# ChatGPT -> GitHub structured snapshot worker. This path is independent of the
# CME web edge and is deliberately read-only from the VPS point of view.
_SNAPSHOT_LOCK = threading.Lock()
_SNAPSHOT_THREAD: threading.Thread | None = None
_SNAPSHOT_STATE: dict[str, Any] = {
    "status": "NEVER_RUN",
    "attempted_at": None,
    "detail": "",
    "payload": None,
}
_DEFAULT_SNAPSHOT_URL = (
    "https://raw.githubusercontent.com/anutey-cell/eurusd-dashboard/"
    "cme-snapshots/cme/latest.json"
)


def _refresh_slot(now: datetime) -> str:
    if now.hour >= 15:
        return f"{now.date().isoformat()}:FINAL"
    if now.hour >= 5:
        return f"{now.date().isoformat()}:PRELIM"
    return f"{now.date().isoformat()}:BOOTSTRAP"


def _refresh_worker() -> None:
    last_ok_slot: str | None = None
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
                        "[cme_context] direct CME refresh OK slot=%s bulletin=%s %s",
                        slot, result.get("bulletin_date"), result.get("bulletin_status"),
                    )
                    time.sleep(300)
                    continue
                log.warning(
                    "[cme_context] direct CME refresh failed slot=%s detail=%s",
                    slot, result.get("detail"),
                )
                time.sleep(1800)
                continue
        except Exception as exc:
            log.warning("[cme_context] direct refresh worker error: %s", exc)
            time.sleep(1800)
            continue
        time.sleep(300)


def ensure_cme_refresh_worker() -> bool:
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
        log.info("[cme_context] direct CME refresh worker started")
        return True


def _snapshot_payload_valid(payload: Any) -> tuple[bool, str]:
    if not isinstance(payload, dict):
        return False, "payload is not an object"
    if int(payload.get("schema_version") or 0) != 1:
        return False, "unsupported schema_version"
    source = str(payload.get("source") or "")
    if "CME" not in source.upper():
        return False, "source is not CME"
    status = str(payload.get("bulletin_status") or "").upper()
    if status not in ("FINAL", "PRELIMINARY", "PRELIM"):
        return False, "invalid bulletin_status"
    try:
        bd = date.fromisoformat(str(payload.get("bulletin_date"))[:10])
    except Exception:
        return False, "invalid bulletin_date"
    age = (datetime.now(timezone.utc).date() - bd).days
    if age < 0:
        return False, "bulletin_date is in the future"
    if age > 5:
        return False, f"snapshot stale age_days={age}"
    options = payload.get("options")
    if not isinstance(options, list) or not options:
        return False, "options surface is empty"
    valid_rows = 0
    for row in options:
        if not isinstance(row, dict):
            continue
        try:
            product = str(row.get("product_code") or "")
            opt_type = str(row.get("option_type") or "").upper()
            float(row.get("strike"))
            int(row.get("open_interest"))
        except Exception:
            continue
        if product in _STD_GOLD_PRODUCTS and opt_type in ("CALL", "PUT"):
            valid_rows += 1
    if valid_rows == 0:
        return False, "no valid standard-gold option rows"
    return True, "ok"


def refresh_chatgpt_snapshot() -> dict[str, Any]:
    global _SNAPSHOT_STATE
    attempted = datetime.now(timezone.utc).isoformat()
    url = os.getenv("CME_CHATGPT_SNAPSHOT_URL", _DEFAULT_SNAPSHOT_URL).strip()
    try:
        r = requests.get(
            url,
            timeout=10,
            headers={"User-Agent": "xauusd-cme-snapshot/1.0", "Accept": "application/json"},
        )
        r.raise_for_status()
        payload = r.json()
        ok, reason = _snapshot_payload_valid(payload)
        if not ok:
            raise RuntimeError(reason)
        _SNAPSHOT_STATE = {
            "status": "OK",
            "attempted_at": attempted,
            "detail": "validated whole-surface ChatGPT snapshot",
            "payload": payload,
        }
        log.info(
            "[cme_context] ChatGPT snapshot accepted bulletin=%s %s rows=%d",
            payload.get("bulletin_date"), payload.get("bulletin_status"), len(payload.get("options") or []),
        )
    except Exception as exc:
        # Preserve the last accepted payload on transport failures. The payload
        # itself is revalidated by age before it can be used.
        previous = _SNAPSHOT_STATE.get("payload")
        _SNAPSHOT_STATE = {
            "status": "FAILED",
            "attempted_at": attempted,
            "detail": f"{type(exc).__name__}: {exc}",
            "payload": previous,
        }
        log.warning("[cme_context] ChatGPT snapshot refresh failed: %s", exc)
    return dict(_SNAPSHOT_STATE)


def _snapshot_worker() -> None:
    while True:
        try:
            refresh_chatgpt_snapshot()
        except Exception as exc:
            log.warning("[cme_context] snapshot worker error: %s", exc)
        time.sleep(300)


def ensure_chatgpt_snapshot_worker() -> bool:
    global _SNAPSHOT_THREAD
    if str(os.getenv("CME_CHATGPT_SNAPSHOT_ENABLED", "true")).lower() not in ("1", "true", "yes"):
        return False
    with _SNAPSHOT_LOCK:
        if _SNAPSHOT_THREAD and _SNAPSHOT_THREAD.is_alive():
            return True
        _SNAPSHOT_THREAD = threading.Thread(
            target=_snapshot_worker,
            name="cme-chatgpt-snapshot",
            daemon=True,
        )
        _SNAPSHOT_THREAD.start()
        log.info("[cme_context] ChatGPT snapshot worker started")
        return True


def get_chatgpt_snapshot_status() -> dict[str, Any]:
    out = dict(_SNAPSHOT_STATE)
    payload = out.pop("payload", None)
    if isinstance(payload, dict):
        out["bulletin_date"] = payload.get("bulletin_date")
        out["bulletin_status"] = payload.get("bulletin_status")
        out["row_count"] = len(payload.get("options") or [])
    return out


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
    if delta is None:
        return 0.25
    d = min(1.0, max(0.0, abs(float(delta))))
    return max(0.0, 1.0 - 2.0 * abs(d - 0.5))


def _date_age(value: Optional[str]) -> int:
    if not value:
        return 999
    try:
        return (datetime.now(timezone.utc).date() - date.fromisoformat(str(value)[:10])).days
    except Exception:
        return 999


def _db_rows(db: Session, bulletin_date: str, bulletin_status: str) -> list[tuple]:
    placeholders = ",".join(f":p{i}" for i in range(len(_STD_GOLD_PRODUCTS)))
    params = {"d": bulletin_date, "s": bulletin_status}
    params.update({f"p{i}": p for i, p in enumerate(_STD_GOLD_PRODUCTS)})
    return list(db.execute(text(f"""
        SELECT product_code, option_expiry_code, option_type, strike,
               open_interest, open_interest_change, delta_cme
        FROM cme_gc_options_eod
        WHERE bulletin_date=:d AND bulletin_status=:s
          AND product_code IN ({placeholders})
          AND open_interest IS NOT NULL
    """), params).fetchall())


def _snapshot_rows(payload: dict[str, Any]) -> list[tuple]:
    out: list[tuple] = []
    for row in payload.get("options") or []:
        try:
            product = str(row.get("product_code") or "")
            opt_type = str(row.get("option_type") or "").upper()
            if product not in _STD_GOLD_PRODUCTS or opt_type not in ("CALL", "PUT"):
                continue
            out.append((
                product,
                str(row.get("option_expiry_code") or ""),
                opt_type,
                float(row.get("strike")),
                int(row.get("open_interest") or 0),
                int(row.get("open_interest_change") or 0),
                (float(row.get("delta_cme")) if row.get("delta_cme") is not None else None),
            ))
        except Exception:
            continue
    return out


def get_cme_options_context(
    db: Session,
    *,
    current_xau: Optional[float] = None,
    max_raw_distance: float = 300.0,
    top_n: int = 8,
) -> dict[str, Any]:
    ensure_cme_refresh_worker()
    ensure_chatgpt_snapshot_worker()

    try:
        db_date, db_status = _canonical_bulletin(db)
    except Exception:
        db_date, db_status = None, None

    db_age = _date_age(db_date)
    snapshot_payload = _SNAPSHOT_STATE.get("payload")
    snap_ok, _ = _snapshot_payload_valid(snapshot_payload) if snapshot_payload else (False, "none")
    snap_date = str(snapshot_payload.get("bulletin_date")) if snap_ok else None
    snap_age = _date_age(snap_date)

    # Prefer the newest valid bulletin. On the same date FINAL outranks PRELIMINARY.
    source = None
    bulletin_date = None
    bulletin_status = None
    rows: list[tuple] = []

    db_fresh = bool(db_date and db_age <= 5)
    snap_fresh = bool(snap_ok and snap_age <= 5)

    if db_fresh and snap_fresh:
        db_key = (str(db_date), 2 if str(db_status).upper() == "FINAL" else 1)
        snap_status = str(snapshot_payload.get("bulletin_status") or "")
        snap_key = (str(snap_date), 2 if snap_status.upper() == "FINAL" else 1)
        if snap_key > db_key:
            source = "CME_CHATGPT_SNAPSHOT"
        else:
            source = "CME_PDF_DB"
    elif snap_fresh:
        source = "CME_CHATGPT_SNAPSHOT"
    elif db_fresh:
        source = "CME_PDF_DB"

    if source == "CME_CHATGPT_SNAPSHOT":
        bulletin_date = snap_date
        bulletin_status = str(snapshot_payload.get("bulletin_status") or "")
        rows = _snapshot_rows(snapshot_payload)
    elif source == "CME_PDF_DB":
        bulletin_date = db_date
        bulletin_status = db_status
        rows = _db_rows(db, str(db_date), str(db_status))
    else:
        best_date = snap_date or db_date
        best_status = (
            str(snapshot_payload.get("bulletin_status") or "") if snap_date and snapshot_payload else db_status
        )
        return {
            "status": "STALE" if best_date else "NO_DATA",
            "bulletin_date": best_date,
            "bulletin_status": best_status,
            "age_days": _date_age(best_date) if best_date else None,
            "directional_bias": "UNSIGNED_NEUTRAL",
            "gamma_status": "NOT_COMPUTED_STALE_SOURCE" if best_date else "NOT_COMPUTED",
            "zones": [],
            "source": None,
            "reason": (
                "No fresh validated CME bulletin source available"
                if best_date else "No accepted CME gold options bulletin available"
            ),
            "chatgpt_snapshot": get_chatgpt_snapshot_status(),
        }

    age_days = _date_age(bulletin_date)
    gc_price = _latest_price(db, "SELECT close FROM gc_futures_bars ORDER BY candle_time DESC LIMIT 1")
    if current_xau is None:
        current_xau = _latest_price(
            db,
            "SELECT close FROM historical_candles WHERE instrument='XAU/USD' AND timeframe='M5' "
            "ORDER BY candle_time DESC LIMIT 1",
        )
    basis = (gc_price - current_xau) if (gc_price is not None and current_xau is not None) else None

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
        "source": source,
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
        "chatgpt_snapshot": get_chatgpt_snapshot_status(),
    }
