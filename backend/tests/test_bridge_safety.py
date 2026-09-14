"""Pure tests for the Windows bridge pre-order safety guard."""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

DEPLOY = Path(__file__).resolve().parents[2] / "deploy"
if str(DEPLOY) not in sys.path:
    sys.path.insert(0, str(DEPLOY))

from bridge_safety import verify_demo_account, verify_terminal


def test_demo_account_guard_accepts_exact_configured_demo():
    acc = SimpleNamespace(login=123456, server="Exness-Trial", trade_mode=0)
    r = verify_demo_account(
        acc,
        expected_login="123456",
        expected_server="Exness-Trial",
    )
    assert r.ok is True


def test_demo_account_guard_blocks_account_switch():
    acc = SimpleNamespace(login=999999, server="Exness-Trial", trade_mode=0)
    r = verify_demo_account(
        acc,
        expected_login="123456",
        expected_server="Exness-Trial",
    )
    assert r.ok is False
    assert "active login" in r.reason


def test_demo_account_guard_blocks_live_trade_mode():
    acc = SimpleNamespace(login=123456, server="Exness-Trial", trade_mode=2)
    r = verify_demo_account(
        acc,
        expected_login="123456",
        expected_server="Exness-Trial",
    )
    assert r.ok is False
    assert "DEMO" in r.reason


def test_demo_account_guard_requires_server_binding():
    acc = SimpleNamespace(login=123456, server="Exness-Real", trade_mode=0)
    r = verify_demo_account(
        acc,
        expected_login="123456",
        expected_server="Exness-Trial",
    )
    assert r.ok is False
    assert "active server" in r.reason


def test_terminal_guard_blocks_disconnected_or_trade_disabled():
    assert verify_terminal(SimpleNamespace(connected=False, trade_allowed=True)).ok is False
    assert verify_terminal(SimpleNamespace(connected=True, trade_allowed=False)).ok is False
    assert verify_terminal(SimpleNamespace(connected=True, trade_allowed=True)).ok is True
