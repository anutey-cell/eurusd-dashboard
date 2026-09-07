"""Gold Intelligence — research-only namespace.

Nothing in this package is imported by production strategy modules
(strategist, predator, vp_trap, execution, risk, telegram, regime).

Provides:
  schemas.py        - DDL for research tables (idempotent CREATE IF NOT EXISTS)
  cftc_backfill.py  - CFTC Disaggregated COT bulk archive ingester
  cftc_derived.py   - Derived metrics + distribution analysis
  macro_backfill.py - US Treasury (nominal + real) + TradingView (anon)
  data_register.py  - Gold-data-discovery register seed
  snapshot_v11.py   - Daily Gold Intelligence Snapshot v1.1 composer
  gex_math.py       - Black-76 primitives (RESEARCH ONLY; not activated)
  tests/            - Validation tests
"""

__version__ = "1.1.0-phase1-closure"
