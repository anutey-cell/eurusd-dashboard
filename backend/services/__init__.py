"""Backend service package.

Takeover safety policy is installed at package import time so every execution
caller receives the same fail-closed governor behaviour and unsafe historical
execution paths are unavailable during the takeover period.

If a required safety patch cannot be installed, importing `services` fails.
That deliberately prevents the scheduler/execution service from starting in an
unknown state rather than merely logging the problem and continuing.
"""
from __future__ import annotations

from .execution_safety_bootstrap import install_safety_patches

TAKEOVER_SAFETY_STATE = install_safety_patches()
_REQUIRED_TAKEOVER_PATCHES = (
    "governor",
    "strategist_cleanup",
    "legacy_executor",
    "direct_local_mt5",
)
_missing = [k for k in _REQUIRED_TAKEOVER_PATCHES if TAKEOVER_SAFETY_STATE.get(k) is not True]
if _missing:
    raise RuntimeError(
        "TAKEOVER_SAFETY_INSTALL_FAILED — refusing service startup; "
        f"missing patches: {', '.join(_missing)}"
    )
