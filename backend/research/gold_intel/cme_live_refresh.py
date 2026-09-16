"""Best-effort CME daily-bulletin refresh for gold research/context.

Downloads the published Metals Options (Section 64) and Metals Futures
(Section 62) PDFs, validates them through the existing detector/parser, and
persists the pair using cme_ingestion.ingest_pair().

This is market-intelligence only. It never places orders and never changes
execution gates. A stale/blocked CME source fails closed and preserves the
last accepted bulletin.
"""
from __future__ import annotations

import logging
import os
import tempfile
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

import requests

from research.gold_intel.schemas_v13 import apply_ddl
from research.gold_intel.cme_bulletin_detector import detect
from research.gold_intel.cme_ingestion import ingest_pair, pdf_to_text

log = logging.getLogger(__name__)

CME_BASE = "https://www.cmegroup.com/daily_bulletin/current"
OPTIONS_URL = f"{CME_BASE}/Section64_Metals_Option_Products.pdf"
FUTURES_URL = f"{CME_BASE}/Section62_Metals_Futures_Products.pdf"

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "Chrome/124 Safari/537.36 XAUUSD-research/1.0"
    ),
    "Accept": "application/pdf,*/*;q=0.8",
}

_last_refresh: dict[str, Any] = {
    "attempted_at": None,
    "status": "NEVER_RUN",
    "detail": "",
    "bulletin_date": None,
    "bulletin_status": None,
}


def get_last_refresh_status() -> dict[str, Any]:
    return dict(_last_refresh)


def _download(url: str, dest: Path) -> None:
    r = requests.get(url, headers=_HEADERS, timeout=30)
    r.raise_for_status()
    body = r.content
    if len(body) < 10_000 or not body.startswith(b"%PDF"):
        raise RuntimeError(f"CME response is not a valid bulletin PDF ({len(body)} bytes)")
    dest.write_bytes(body)


def _validate_pair(options_pdf: Path, futures_pdf: Path) -> tuple[dict, dict]:
    opt_meta = detect(pdf_to_text(str(options_pdf)))
    fut_meta = detect(pdf_to_text(str(futures_pdf)))
    if opt_meta.get("bulletin_type") != "OPTIONS_METALS":
        raise RuntimeError(f"Section64 detector mismatch: {opt_meta}")
    if fut_meta.get("bulletin_type") != "FUTURES_METALS":
        raise RuntimeError(f"Section62 detector mismatch: {fut_meta}")
    if not opt_meta.get("bulletin_date") or not fut_meta.get("bulletin_date"):
        raise RuntimeError("CME bulletin date missing")
    if opt_meta["bulletin_date"] != fut_meta["bulletin_date"]:
        raise RuntimeError(
            f"CME pair date mismatch options={opt_meta['bulletin_date']} futures={fut_meta['bulletin_date']}"
        )
    # Daily bulletin is previous-trade-date data. Weekends/holidays can make it
    # several calendar days old, so we reject only clearly stale CDN content.
    bd = date.fromisoformat(str(opt_meta["bulletin_date"]))
    age_days = (datetime.now(timezone.utc).date() - bd).days
    if age_days > 5:
        raise RuntimeError(f"CME current endpoint is stale: bulletin_date={bd} age_days={age_days}")
    return opt_meta, fut_meta


def refresh_cme_bulletins() -> dict[str, Any]:
    global _last_refresh
    attempted = datetime.now(timezone.utc)
    _last_refresh = {
        "attempted_at": attempted.isoformat(),
        "status": "RUNNING",
        "detail": "",
        "bulletin_date": None,
        "bulletin_status": None,
    }
    try:
        apply_ddl()
        with tempfile.TemporaryDirectory(prefix="cme_gold_") as td:
            root = Path(td)
            opt = root / "Section64_Metals_Option_Products.pdf"
            fut = root / "Section62_Metals_Futures_Products.pdf"
            _download(OPTIONS_URL, opt)
            _download(FUTURES_URL, fut)
            opt_meta, fut_meta = _validate_pair(opt, fut)
            result = ingest_pair(str(opt), str(fut))
            if result.get("status") != "OK":
                raise RuntimeError(f"CME ingestion failed: {result}")
            _last_refresh = {
                "attempted_at": attempted.isoformat(),
                "status": "OK",
                "detail": (
                    f"options_rows={result.get('options_rows_written', 0)} "
                    f"futures_rows={result.get('futures_rows_written', 0)} "
                    f"reconciliation={result.get('reconciliation_rows', 0)}"
                ),
                "bulletin_date": opt_meta.get("bulletin_date"),
                "bulletin_status": opt_meta.get("bulletin_status"),
            }
            log.info("[cme_refresh] %s", _last_refresh)
            return dict(_last_refresh)
    except Exception as exc:
        _last_refresh = {
            "attempted_at": attempted.isoformat(),
            "status": "FAILED",
            "detail": f"{type(exc).__name__}: {exc}",
            "bulletin_date": None,
            "bulletin_status": None,
        }
        log.warning("[cme_refresh] failed closed: %s", exc)
        return dict(_last_refresh)
