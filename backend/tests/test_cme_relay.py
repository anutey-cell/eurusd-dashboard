from __future__ import annotations

import base64
import os

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

# Never start autonomous network refresh in unit tests.
os.environ["CME_OPTIONS_CONTEXT_REFRESH_ENABLED"] = "false"

from config import settings
from routers import health
from services import cme_relay_ingest


def _pdf_bytes(tag: bytes = b"test") -> bytes:
    return b"%PDF-1.7\n" + tag + b"\n" + (b"0" * 11_000)


def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


def test_relay_ingest_rejects_non_pdf_before_parser(monkeypatch):
    with pytest.raises(ValueError, match="not a PDF"):
        cme_relay_ingest.ingest_relay_pair(
            _b64(b"NOTPDF" + b"x" * 11_000),
            _b64(_pdf_bytes(b"futures")),
        )


def test_relay_ingest_delegates_valid_pair_to_existing_pipeline(monkeypatch):
    calls = {"ddl": 0, "validate": 0, "ingest": 0}

    def fake_ddl():
        calls["ddl"] += 1

    def fake_validate(opt, fut):
        calls["validate"] += 1
        assert opt.read_bytes().startswith(b"%PDF")
        assert fut.read_bytes().startswith(b"%PDF")
        return (
            {"bulletin_date": "2026-09-15", "bulletin_status": "FINAL"},
            {"bulletin_date": "2026-09-15", "bulletin_status": "FINAL"},
        )

    def fake_ingest(opt, fut):
        calls["ingest"] += 1
        return {
            "status": "OK",
            "options_rows_written": 4321,
            "futures_rows_written": 38,
            "reconciliation_rows": 35,
            "options_archive_id": 11,
            "futures_archive_id": 12,
        }

    monkeypatch.setattr(cme_relay_ingest, "apply_ddl", fake_ddl)
    monkeypatch.setattr(cme_relay_ingest, "_validate_pair", fake_validate)
    monkeypatch.setattr(cme_relay_ingest, "ingest_pair", fake_ingest)

    result = cme_relay_ingest.ingest_relay_pair(
        _b64(_pdf_bytes(b"options")),
        _b64(_pdf_bytes(b"futures")),
    )

    assert calls == {"ddl": 1, "validate": 1, "ingest": 1}
    assert result["status"] == "OK"
    assert result["bulletin_date"] == "2026-09-15"
    assert result["bulletin_status"] == "FINAL"
    assert result["options_rows_written"] == 4321


def test_http_relay_requires_bridge_secret_and_accepts_authenticated_payload(monkeypatch):
    monkeypatch.setattr(settings, "mt5_bridge_shared_secret", "unit-cme-secret")
    monkeypatch.setattr(settings, "mt5_bridge_shared_secret_prev", "")

    monkeypatch.setattr(
        cme_relay_ingest,
        "ingest_relay_pair",
        lambda options_pdf_b64, futures_pdf_b64: {
            "status": "OK",
            "bulletin_date": "2026-09-15",
            "bulletin_status": "FINAL",
            "options_rows_written": 4000,
            "futures_rows_written": 38,
            "reconciliation_rows": 35,
        },
    )

    app = FastAPI()
    app.include_router(health.router, prefix="/api/v1")
    client = TestClient(app)

    payload = {
        "source": "unit-home",
        "options_pdf_b64": _b64(_pdf_bytes(b"options")),
        "futures_pdf_b64": _b64(_pdf_bytes(b"futures")),
    }

    missing = client.post("/api/v1/cme/bulletin-relay", json=payload)
    assert missing.status_code == 401

    wrong = client.post(
        "/api/v1/cme/bulletin-relay",
        json=payload,
        headers={"X-Bridge-Secret": "wrong"},
    )
    assert wrong.status_code == 401

    accepted = client.post(
        "/api/v1/cme/bulletin-relay",
        json=payload,
        headers={"X-Bridge-Secret": "unit-cme-secret"},
    )
    assert accepted.status_code == 200
    body = accepted.json()
    assert body["ok"] is True
    assert body["data"]["bulletin_date"] == "2026-09-15"
