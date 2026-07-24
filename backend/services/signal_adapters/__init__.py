"""
Signal Adapters — Strategy → CanonicalSignal Translation
=========================================================

Each adapter takes a strategy's native output shape and produces
CanonicalSignal upserts + state transitions. The canonical layer never
knows about strategy internals; strategies never know about Telegram.

Adapters:
  mandate_adapter    — mandate 5-gate verdict → canonical
  vp_trap_adapter    — VP Trap zone/setup → canonical  (P6)
  momentum_adapter   — momentum-continuation signal → canonical  (P7)
  kz_magnet_adapter  — KZ Magnet setup → canonical  (P7)

Shadow mode
-----------
Every adapter accepts a `dry_run` flag. When True, canonical persistence
still happens (registry + audit rows) but no Telegram send occurs — the
legacy path continues to serve users, while the canonical path builds an
audit trail we can diff against. Cutover flips `dry_run=False` per
strategy once shadow observation validates parity.
"""

from services.signal_adapters.mandate_adapter import (  # noqa: F401
    on_mandate_verdict, mandate_verdict_to_signal,
)

__all__ = ["on_mandate_verdict", "mandate_verdict_to_signal"]
