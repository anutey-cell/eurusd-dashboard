"""
Phase 15 — emergency-disable script.

Prints the env-var overrides needed to disable every Phase 2-14 layer.
Copy the output into deploy/.env.prod and restart the container:

    python -m scripts.emergency_disable > /tmp/xauusd_disable.env
    cat /tmp/xauusd_disable.env >> /opt/xauusd/deploy/.env.prod
    docker compose -f /opt/xauusd/deploy/docker-compose.prod.yml restart backend

Does NOT write to config on its own. Never touches broker credentials,
lot size, or MT5 execution flags.
"""
from __future__ import annotations

import sys


def main():
    try:
        from services.rollout_gates import emergency_env_reset
    except ImportError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)

    envs = emergency_env_reset()
    print("# XAUUSD directional intelligence — emergency disable")
    print("# Copy into deploy/.env.prod, then restart the backend container.")
    print("# This does NOT touch broker credentials, lot size, or MT5 execution.")
    print()
    for k, v in sorted(envs.items()):
        print(f"{k}={v}")


if __name__ == "__main__":
    main()
