from __future__ import annotations

import os
from datetime import date, timedelta

os.environ["CME_OPTIONS_CONTEXT_REFRESH_ENABLED"] = "false"
os.environ["CME_CHATGPT_SNAPSHOT_ENABLED"] = "false"

from services.cme_options_context import _snapshot_payload_valid


def test_snapshot_accepts_fresh_whole_surface_payload():
    payload = {
        "schema_version": 1,
        "source": "CME Daily Bulletin / ChatGPT retrieval",
        "bulletin_date": date.today().isoformat(),
        "bulletin_status": "FINAL",
        "options": [
            {
                "product_code": "OG3",
                "option_expiry_code": "SEP26",
                "option_type": "CALL",
                "strike": 4350,
                "open_interest": 416,
                "open_interest_change": 20,
                "delta_cme": 0.40,
            }
        ],
    }
    ok, reason = _snapshot_payload_valid(payload)
    assert ok is True
    assert reason == "ok"


def test_snapshot_rejects_stale_payload():
    payload = {
        "schema_version": 1,
        "source": "CME Daily Bulletin / ChatGPT retrieval",
        "bulletin_date": (date.today() - timedelta(days=10)).isoformat(),
        "bulletin_status": "FINAL",
        "options": [{"strike": 4350, "open_interest": 1}],
    }
    ok, reason = _snapshot_payload_valid(payload)
    assert ok is False
    assert "stale" in reason.lower()


def test_snapshot_rejects_empty_or_non_cme_payload():
    payload = {
        "schema_version": 1,
        "source": "unknown",
        "bulletin_date": date.today().isoformat(),
        "bulletin_status": "FINAL",
        "options": [],
    }
    ok, reason = _snapshot_payload_valid(payload)
    assert ok is False
    assert reason != "ok"
