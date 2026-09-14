"""Security regression tests for takeover control-plane authentication."""
from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from config import settings
from middleware import AuthMiddleware


def _app() -> FastAPI:
    app = FastAPI()
    app.add_middleware(AuthMiddleware)

    @app.get("/api/v1/strategist/decision")
    def strategist_decision():
        return {"ok": True}

    @app.get("/api/v1/bridge/status")
    def bridge_status():
        return {"private": True}

    @app.get("/api/v1/health")
    def health():
        return {"ok": True}

    return app


def test_control_route_fails_closed_with_placeholder_api_key(monkeypatch):
    monkeypatch.setattr(settings, "api_key", "change_me")
    monkeypatch.setattr(settings, "auth_enabled", False)
    r = TestClient(_app()).get("/api/v1/strategist/decision")
    assert r.status_code == 503


def test_control_route_requires_strong_operator_key_even_if_global_auth_off(monkeypatch):
    monkeypatch.setattr(settings, "api_key", "operator-secret-123456")
    monkeypatch.setattr(settings, "auth_enabled", False)
    client = TestClient(_app())

    assert client.get("/api/v1/strategist/decision").status_code == 401
    ok = client.get(
        "/api/v1/strategist/decision",
        headers={"X-API-Key": "operator-secret-123456"},
    )
    assert ok.status_code == 200


def test_bridge_status_is_private_and_accepts_bridge_secret(monkeypatch):
    monkeypatch.setattr(settings, "api_key", "operator-secret-123456")
    monkeypatch.setattr(settings, "mt5_bridge_shared_secret", "bridge-secret-abcdef")
    monkeypatch.setattr(settings, "mt5_bridge_shared_secret_prev", "")
    client = TestClient(_app())

    assert client.get("/api/v1/bridge/status").status_code == 401
    ok = client.get(
        "/api/v1/bridge/status",
        headers={"X-Bridge-Secret": "bridge-secret-abcdef"},
    )
    assert ok.status_code == 200


def test_public_health_remains_available_when_global_auth_off(monkeypatch):
    monkeypatch.setattr(settings, "api_key", "change_me")
    monkeypatch.setattr(settings, "auth_enabled", False)
    r = TestClient(_app()).get("/api/v1/health")
    assert r.status_code == 200
