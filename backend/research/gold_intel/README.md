# Gold Intelligence — research namespace

Research-only. Nothing in this package is imported by production strategy
modules (strategist, predator, vp_trap, execution, risk, telegram, regime).

## Scope

Phase 1A + 1B + Phase 2-architecture-prep, closed by v1.1.

## Layers implemented

| Layer | Source | Status |
|---|---|---|
| Positioning (CFTC Disaggregated) | CFTC bulk archives (`fut_disagg`, `com_disagg`) | LIVE |
| Macro nominal / real yields | US Treasury Direct | LIVE |
| Macro spot (DXY / VIX / MOVE / WTI / USDJPY / USDCNH) | TradingView anonymous | LIVE |
| Macro breakevens (derived) | Treasury pair identity | LIVE |
| GC front-month futures | TradingView `COMEX:GC1!` | LIVE |
| Event calendar | ForexFactory / faireconomy.media | LIVE |
| Quote-level microstructure | MT5 ticks (Track A) | CAPTURING |
| GC options / GEX | CME Daily Bulletin | NOT ACTIVATED |
| GC exchange microstructure (MBP/MBO) | Databento | HOLD |
| Physical / SGE | SGE public HTML | NOT INGESTED |
| Investment flows / ETF | WGC | NOT INGESTED |

## Discipline

- Every value carries `FACT_ / DERIVED_ / INFERENCE` prefix or a section-level
  `NOT_OBSERVED / NOT_AVAILABLE / DEGRADED` status.
- Missing data stays missing; never silently forward-filled.
- Classification is confidence-gated: BULLISH / BEARISH / NEUTRAL /
  CONFLICTED / NO_EDGE / INCOMPLETE / STAND_ASIDE. `NEUTRAL` requires enough
  observable independent layers; `NO_EDGE` and `INCOMPLETE` are distinct.
- Threshold-freezing for COT flow classification is deferred until the
  distribution analysis has been reviewed.
- The GEX solver is validated numerically but no live options data flows in.
- `provisional_basis` labels the GC-XAU difference PROVISIONAL and refuses
  to publish any mapped strike overlay until Phase 2 validation.

## Files

- `schemas.py`, `schemas_v11.py` — DDL for all research tables (idempotent).
- `cftc_backfill.py`  — CFTC Disaggregated COT bulk archives (2006-present).
- `cftc_derived.py`   — Derived metrics + distribution analysis.
- `macro_backfill.py` — Treasury Direct + TradingView.
- `data_register_and_gex.py` — Data-discovery register seed + Black-76 self-test.
- `snapshot_v11.py`   — Daily Gold Intelligence Snapshot v1.1 (composer +
  trading-day utilities + classification governance).
- `tests/test_closure.py` — 22 validation tests (all passing).

## Trading-day conventions

- **Previous trading day** = most recent UTC date strictly before the
  reference with ≥6 H1 bars in `historical_candles`. Excludes Saturdays;
  Sunday-open sessions (2 bars) are skipped.
- **Previous trading week** = last completed Monday–Friday UTC block
  (`ref.weekday() → Monday of ref week − 3 days = Fri of prev week`).
- **Asian session** = 22:00 UTC prev day → 06:00 UTC current day. Preferred
  source: `mt5_ticks`; fallback: H1 bars.

## Rebuild / refresh

Weekly CFTC + daily macro + hourly events + on-demand snapshot are the
intended cadences. No cron/scheduler is wired in this closure pass; each
script is idempotent and safe to re-run.

## Not touched

- STRATEGIST, PREDATOR, VP TRAP, execution, risk, Telegram — untouched.
- Track A tick capture — untouched.
- Track B — HOLD.
- Paid data — not purchased.
