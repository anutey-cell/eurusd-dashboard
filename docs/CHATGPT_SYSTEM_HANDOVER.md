# XAUUSD System Handover — ChatGPT Baseline

**Baseline date:** 2026-09-11  
**Repository:** `anutey-cell/eurusd-dashboard`  
**Branch:** `vps-bridge-migration`  
**Baseline commit inspected:** `4b5a7223eb1ff4622cd5a3b24b7ecdb1d17132e4`  
**Purpose:** Make the repository, not prior Claude conversations, the authoritative engineering memory for the XAUUSD platform.

---

## 1. Operating architecture

### Windows home laptop

Runs MetaTrader 5 and the Windows-side bridge:

- `deploy/mt5_bridge_daemon.py`
- `deploy/mt5_ticks_extension.py`
- local private config: `deploy/.env.bridge` (not committed)
- persistent confirmed tick cursor: `deploy/.mt5_tick_cursor.json` (not committed)
- Exness MT5 terminal installed locally
- scheduled task: `Start MT5`
- scheduled task: `XAUUSD MT5 Bridge`

The bridge performs:

- MT5 connection / account-state heartbeat
- candle push
- raw XAUUSD quote-level tick capture
- VPS heartbeat
- optional execution queue polling

**Current cutover policy:** `EXECUTION_POLL_ENABLED=false` is forced at scheduled-task process level. The bridge remains market-data capable while pending-order polling/execution is locally disabled.

### VPS / DigitalOcean

Runs the Dockerized FastAPI backend and frontend. `docker-compose.yml` mounts persistent backend storage at `/app/storage` and starts `anudhe/xauusd-backend:latest` plus the frontend image.

Primary backend responsibilities:

- API and diagnostics
- historical candle ingestion
- strategist / Predator / secondary strategy loops
- database and outcome ledgers
- Telegram delivery
- portfolio governor
- bridge endpoints
- research-data storage

---

## 2. Authoritative background scheduler

`backend/services/background_scheduler.py` starts the following loops:

1. scanner
2. high-probability prediction alerts
3. drawdown monitoring
4. daily summary
5. daily briefing
6. weekly digest
7. weekend newsletter
8. fast candle ingestion
9. slow candle ingestion
10. data-freshness monitor
11. VP Trap measurement
12. market-intelligence pipeline
13. shadow-trade advancement
14. Predator
15. Strategist outcome-ledger resolver
16. exactly one execution authority:
   - mandate strategist when `use_mandate_strategist=true`
   - legacy auto-executor otherwise

The scheduler explicitly avoids running both execution authorities at the same time.

---

## 3. Primary engines and strategy surfaces

### Mandate Strategist

Authoritative when `use_mandate_strategist=true`.

Current execution-path characteristics observed in source:

- BUY execution path only
- SELL strategist verdicts are scored and shadow-ledgered, not enqueued
- lot-size and aggregate-exposure checks
- one-position-at-a-time strategist exposure ceiling in the current enqueue path
- global portfolio-governor reservation
- bridge heartbeat/account/server/symbol verification
- `live_execution_allowed` must be false
- `ALLOW_DEMO_TRADING` and `MT5_BRIDGE_ENABLED` must permit enqueue
- Monday observation mode can refuse enqueue

### Predator

Independent strategy and notification lifecycle with persistent setup registry / gateway architecture. Current configuration supports `legacy`, `shadow`, and `gateway` notification modes. Predator is directionally specialized alongside the Strategist and is governed by the shared account-level portfolio governor for execution capacity.

### VP Trap

Previous-day volume-profile / trapped-trader reversal strategy. Ships enabled for signal/measurement use with auto-execution default off.

### KZ Magnet

Killzone-POC magnet strategy. Ships enabled for signal use; execution remains separate from simple enablement.

### Legacy / paper engines

The repository still contains older scanner, swing, trend-pullback, predictor, adaptive and auto-executor components. These must not be assumed authoritative merely because they exist. Runtime authority is determined by scheduler wiring and configuration.

---

## 4. Market-intelligence pipeline

The scheduler's market-intelligence loop wires the following sequence:

1. `canonical_market_data`
2. `htf_weighted_alignment`
3. `market_regime`
4. `directional_evidence`
5. `breakout_acceptance`
6. `opportunity_state`
7. `separated_verdicts`
8. `key_level_ranking`
9. `macro_interpretation`
10. `market_intelligence_alerts`

Macro assembly is best-effort and can include calendar events, correlations, yields and DXY context.

This pipeline must be classified separately from the primary execution mandate: enabled detection or persistence does not automatically imply execution authority.

---

## 5. Account-level portfolio governor

`backend/services/portfolio_governor.py` is the shared gate above Strategist and Predator.

Key properties:

- gross exposure, not net exposure
- hard gross-lot ceiling defined in governor code
- atomic in-process reservation with `threading.RLock`
- durable reservation state mirrored to DB
- reservation states include RESERVED, SENT, FILLED, REJECTED and ABANDONED
- SENT reservations are not TTL-expired blindly
- startup reconciliation reconstructs durable state
- stale/missing MT5 heartbeat can keep governor NOT READY / fail closed
- MT5-vs-DB disagreement can block new orders

The governor never automatically closes positions.

---

## 6. Data architecture

### Live / operational inputs

- MT5 XAUUSD terminal feed on the home Windows laptop
- raw MT5 quote-level ticks captured by `mt5_ticks_extension.py`
- TwelveData candle feed / historical candle ingestion
- economic-calendar provider(s)
- best-effort macro inputs including DXY/yields/correlation where configured

### Tick research semantics

The MT5 tick table is **quote-level microstructure**, not true centralized order flow. Spot XAUUSD on this broker feed does not provide a trustworthy aggressor-side / exchange-volume feed. Research must preserve that terminology.

The tick cursor represents server-confirmed persistence. HTTP/transport failure does not advance it; replay is safe because server-side deduplication protects duplicate ingestion.

---

## 7. Gold intelligence research stack

`backend/research/gold_intel/` currently contains, among other modules:

- CFTC backfill
- CFTC derived analytics
- CME bulletin detector
- CME bulletin parser
- CME futures parser
- CME gamma module
- CME ingestion
- data register / GEX work
- macro backfill
- additional options / mathematical research modules

This area must remain explicitly classified as **research** unless and until a reviewed integration promotes a derived feature into the production decision path.

A separate `research/orderflow/` area documents quote-level microstructure research.

---

## 8. Deployment/source-of-truth rules

1. GitHub is the authoritative source code record.
2. VPS runtime state and database state are not assumed to equal GitHub until verified.
3. Docker Hub images are deployment artifacts, not the authoritative source of live data.
4. The home laptop is the authoritative local MT5 gateway.
5. The retired work laptop must not run the bridge simultaneously.
6. Secrets remain outside Git and must never be pasted into engineering documentation.
7. Historical Claude handovers are reference material only; current code and verified runtime state override them.

---

## 9. Home-laptop migration — completed 2026-09-11

Verified during cutover:

- repository cloned on `vps-bridge-migration`
- venv Python 3.14.7 operational
- `MetaTrader5` 5.0.6180 installed
- MT5 terminal connects to the currently logged-in account
- bridge secret/config transferred privately
- authoritative tick cursor transferred
- work-laptop scheduled tasks disabled
- home scheduled tasks created
- home bridge started automatically through Task Scheduler
- VPS accepted tick batches
- a temporary MT5 history-session stall preserved the cursor
- after clean daemon restart, 3,618 backlog ticks were recovered in two acknowledged chunks
- cursor resumed advancing via `server_ack`

The observed `cmd.exe -> venv python.exe -> python.exe` chain is one scheduled-task process tree, not two independent bridge daemons.

---

## 10. Known technical debt / first hardening items

### A. Tick-worker MT5 session recovery

Observed on 2026-09-11:

- live MT5 probe remained healthy
- scheduled daemon's `copy_ticks_from()` began returning `None` repeatedly
- cursor correctly stayed fixed
- daemon did not self-reinitialize MT5
- manual bridge restart recovered the full stalled interval

Required hardening:

- count consecutive MT5 history-call failures
- after a bounded retry threshold, safely reinitialize the MT5 API session
- resume from the same confirmed cursor
- keep execution polling independently gated
- expose reconnect count / last reconnect reason in bridge health

### B. TLS verification

The Windows bridge currently sets `requests.Session.verify = False`. This should be replaced with normal certificate verification once the Windows trust-chain issue is corrected. Shared-secret authentication is not a substitute for TLS server verification.

### C. Configuration/documentation drift

`backend/config.py` contains historical comments/settings from several architecture eras. Runtime authority must be derived from actual scheduler wiring plus current execution paths, and obsolete/conflicting settings should be deprecated or documented.

### D. Production-vs-research classification

The repository contains many implemented modules. Presence in `backend/services` does not prove production authority. Every engine/service should be tagged as:

- PRODUCTION-AUTHORITATIVE
- PRODUCTION-SUPPORTING
- SHADOW
- PAPER/OBSERVATION
- RESEARCH-ONLY
- LEGACY/DEPRECATED

---

## 11. ChatGPT takeover workflow

Going forward, engineering work should follow this sequence:

1. inspect current GitHub branch/code
2. verify VPS runtime state before making runtime claims
3. identify whether the change affects data, alpha, notifications, risk or execution
4. preserve fail-closed execution gates during development
5. implement on a controlled branch
6. run targeted tests plus regression suite
7. review diff
8. deploy deliberately
9. verify health/data freshness/Telegram/execution state
10. update this handover when architecture changes materially

Claude is no longer an operating dependency. Prior Claude documents may be consulted for historical rationale, but they are not the source of truth.

---

## 12. Next audit work

The next structured audit should produce:

- complete service inventory and status classification
- complete database/table inventory
- exact current `.env.prod` runtime-flag matrix (values verified on VPS, secrets excluded)
- actual Docker image digest / deployed commit reconciliation
- Telegram sender inventory and noise paths
- Strategist decision-path diagram
- Predator decision-path diagram
- VP Trap / KZ Magnet authority review
- test-suite inventory and current pass/fail state
- stale-session auto-reconnect patch for the Windows bridge
- cleanup plan for legacy execution settings and misleading comments
