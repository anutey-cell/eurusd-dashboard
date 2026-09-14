"""API router package takeover hardening.

The hardened Windows bridge uses an atomic claim-v2 endpoint registered onto
`routers.bridge.router` before the application includes that router.
"""
from __future__ import annotations

try:
    from . import bridge as _bridge
    from .bridge_claim_safety import install_atomic_claim_route

    BRIDGE_ATOMIC_CLAIM_INSTALLED = install_atomic_claim_route(_bridge)
except Exception:
    # Importing the router package in tooling should not become fatal. Runtime
    # verification/tests assert this flag is True before takeover deployment.
    BRIDGE_ATOMIC_CLAIM_INSTALLED = False
