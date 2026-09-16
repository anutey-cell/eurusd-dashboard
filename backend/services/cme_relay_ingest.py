"""Ingest CME Metals bulletin PDFs relayed by the Windows edge.

The CME public site blocks common cloud-hosting address ranges. The Windows
MT5 bridge machine therefore acts as the acquisition edge and relays the raw
Section 64 (Metals Options) + Section 62 (Metals Futures) PDFs to the VPS.

This service is market-intelligence only. It validates the pair, applies the
research schema, and delegates to the existing idempotent ingestion pipeline.
It never changes execution gates or order state.
"""
from __future__ import annotations

import base64
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from research.gold_intel.schemas_v13 import apply_ddl
from research.gold_intel.cme_live_refresh import _validate_pair
from research.gold_intel.cme_ingestion import ingest_pair

MAX_PDF_BYTES = 12 * 1024 * 1024

_last_relay: dict[str, Any] = {
    "attempted_at": None,
    "status": "NEVER_RUN",
    "detail": "",
    "bulletin_date": None,
    "bulletin_status": None,
}


def get_last_relay_status() -> dict[str, Any]:
    return dict(_last_relay)


def _decode_pdf(value: str, label: str) -> bytes:
    try:
        raw = base64.b64decode(value, validate=True)
    except Exception as exc:
        raise ValueError(f"{label}: invalid base64: {exc}") from exc
    if len(raw) < 10_000:
        raise ValueError(f"{label}: PDF too small ({len(raw)} bytes)")
    if len(raw) > MAX_PDF_BYTES:
        raise ValueError(f"{label}: PDF exceeds {MAX_PDF_BYTES} byte limit")
    if not raw.startswith(b"%PDF"):
        raise ValueError(f"{label}: payload is not a PDF")
    return raw


def ingest_relay_pair(options_pdf_b64: str, futures_pdf_b64: str) -> dict[str, Any]:
    global _last_relay
    attempted = datetime.now(timezone.utc)
    _last_relay = {
        "attempted_at": attempted.isoformat(),
        "status": "RUNNING",
        "detail": "",
        "bulletin_date": None,
        "bulletin_status": None,
    }
    try:
        opt_raw = _decode_pdf(options_pdf_b64, "section64")
        fut_raw = _decode_pdf(futures_pdf_b64, "section62")

        apply_ddl()
        with tempfile.TemporaryDirectory(prefix="cme_relay_") as td:
            root = Path(td)
            opt = root / "Section64_Metals_Option_Products.pdf"
            fut = root / "Section62_Metals_Futures_Products.pdf"
            opt.write_bytes(opt_raw)
            fut.write_bytes(fut_raw)

            opt_meta, fut_meta = _validate_pair(opt, fut)
            result = ingest_pair(str(opt), str(fut))
            if result.get("status") != "OK":
                raise RuntimeError(f"CME relay ingestion failed: {result}")

            out = {
                "status": "OK",
                "bulletin_date": opt_meta.get("bulletin_date"),
                "bulletin_status": opt_meta.get("bulletin_status"),
                "options_rows_written": result.get("options_rows_written", 0),
                "futures_rows_written": result.get("futures_rows_written", 0),
                "reconciliation_rows": result.get("reconciliation_rows", 0),
                "options_archive_id": result.get("options_archive_id"),
                "futures_archive_id": result.get("futures_archive_id"),
            }
            _last_relay = {
                "attempted_at": attempted.isoformat(),
                "status": "OK",
                "detail": (
                    f"options_rows={out['options_rows_written']} "
                    f"futures_rows={out['futures_rows_written']} "
                    f"reconciliation={out['reconciliation_rows']}"
                ),
                "bulletin_date": out["bulletin_date"],
                "bulletin_status": out["bulletin_status"],
            }
            return out
    except Exception as exc:
        _last_relay = {
            "attempted_at": attempted.isoformat(),
            "status": "FAILED",
            "detail": f"{type(exc).__name__}: {exc}",
            "bulletin_date": None,
            "bulletin_status": None,
        }
        raise
