"""Backend service package.

Takeover safety policy is installed at package import time so every execution
caller receives the same fail-closed governor behaviour and the historical
legacy autonomous executor cannot place orders during the takeover period.
"""
from __future__ import annotations

from .execution_safety_bootstrap import install_safety_patches

TAKEOVER_SAFETY_STATE = install_safety_patches()
