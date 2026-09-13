#!/usr/bin/env python3
"""Read-only runtime audit collector for ChatGPT takeover.

This script does NOT mutate services, databases, orders, config, or files.
It deliberately redacts secrets and reports only operational state needed to
reconcile the GitHub repository with the running VPS/bridge environment.

Run from repository root:
    python deploy/takeover_readonly_audit.py

Optional:
    python deploy/takeover_readonly_audit.py > takeover_runtime_audit.txt
"""
from __future__ import annotations

import json
import os
import platform
import re
import socket
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

SECRET_MARKERS = (
    "PASSWORD", "SECRET", "TOKEN", "API_KEY", "APIKEY", "PRIVATE", "CREDENTIAL"
)

SAFE_ENV_KEYS = [
    "DATA_MODE",
    "XAUUSD_MODE",
    "BROKER_EXECUTION_ENABLED",
    "MT5_ENABLED",
    "MT5_MODE",
    "MT5_EXECUTION_ENABLED",
    "ALLOW_DEMO_TRADING",
    "AUTO_EXECUTION_ENABLED",
    "LIVE_TRADING_AUTHORIZED",
    "MT5_BRIDGE_ENABLED",
    "USE_MANDATE_STRATEGIST",
    "MONDAY_OBSERVATION_MODE",
    "PREDATOR_ENABLED",
    "PREDATOR_EXECUTION_ENABLED",
    "PREDATOR_NOTIFICATION_ARCHITECTURE",
    "TELEGRAM_ALERTS_ENABLED",
    "DEMO_AUTO_ENQUEUE",
    "MANDATE_DEMO_LOGIN",
    "MANDATE_DEMO_SERVER_CONTAINS",
    "MANDATE_DEMO_SYMBOL",
]


def run(cmd: list[str], timeout: int = 5) -> dict:
    try:
        p = subprocess.run(
            cmd, cwd=ROOT, capture_output=True, text=True, timeout=timeout, check=False
        )
        return {
            "ok": p.returncode == 0,
            "rc": p.returncode,
            "stdout": p.stdout.strip()[:12000],
            "stderr": p.stderr.strip()[:4000],
        }
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


def redact_url(value: str | None) -> str | None:
    if not value:
        return value
    # Hide credentials in URLs such as scheme://user:pass@host/db
    return re.sub(r"(://)[^/@:]+(?::[^/@]*)?@", r"\1<redacted>@", value)


def read_env_file(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    if not path.exists():
        return out
    try:
        for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip().strip('"').strip("'")
    except Exception:
        pass
    return out


def safe_env_snapshot() -> dict:
    merged: dict[str, str] = {}
    for candidate in (ROOT / ".env", ROOT / "backend" / ".env", ROOT / ".env.bridge", ROOT / "deploy" / ".env.bridge"):
        merged.update(read_env_file(candidate))
    merged.update(os.environ)

    result = {}
    for key in SAFE_ENV_KEYS:
        if key in merged:
            result[key] = merged[key]
        else:
            result[key] = "<unset>"

    # Report existence only for secret-like variables; NEVER values.
    secret_presence = {}
    for key, value in merged.items():
        upper = key.upper()
        if any(marker in upper for marker in SECRET_MARKERS):
            secret_presence[key] = bool(value)
    result["SECRET_VARIABLE_PRESENCE_ONLY"] = secret_presence

    if "DATABASE_URL" in merged:
        result["DATABASE_URL_REDACTED"] = redact_url(merged.get("DATABASE_URL"))
    return result


def docker_snapshot() -> dict:
    probe = run(["docker", "ps", "--format", "{{.Names}}|{{.Image}}|{{.Status}}"])
    if not probe.get("ok"):
        return probe
    return {"ok": True, "containers": probe.get("stdout", "").splitlines()}


def service_snapshot() -> dict:
    if platform.system().lower() != "linux":
        return {"ok": False, "note": "systemd check skipped: non-Linux host"}
    p = run([
        "systemctl", "list-units", "--type=service", "--all", "--no-pager", "--no-legend"
    ])
    if not p.get("ok"):
        return p
    keep = []
    for line in p.get("stdout", "").splitlines():
        low = line.lower()
        if any(term in low for term in ("xau", "mt5", "bridge", "dashboard", "uvicorn", "docker")):
            keep.append(line)
    return {"ok": True, "matching_services": keep}


def main() -> int:
    report = {
        "audit_version": 1,
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "read_only": True,
        "host": {
            "hostname": socket.gethostname(),
            "platform": platform.platform(),
            "python": sys.version.split()[0],
        },
        "git": {
            "head": run(["git", "rev-parse", "HEAD"]),
            "branch": run(["git", "branch", "--show-current"]),
            "status": run(["git", "status", "--short"]),
            "remote": run(["git", "remote", "-v"]),
        },
        "config_state": safe_env_snapshot(),
        "docker": docker_snapshot(),
        "systemd": service_snapshot(),
    }

    # Optional database existence metadata only; no rows are changed/read here.
    sqlite_candidates = [ROOT / "xauusd_signals.db", ROOT / "backend" / "xauusd_signals.db"]
    report["sqlite_files"] = [
        {"path": str(p.relative_to(ROOT)), "exists": p.exists(), "size_bytes": p.stat().st_size if p.exists() else 0}
        for p in sqlite_candidates
    ]

    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
