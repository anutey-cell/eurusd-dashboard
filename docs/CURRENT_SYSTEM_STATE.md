# XAUUSD Engine — Current System State

Status date: 2026-09-14
Baseline repository commit: `0c17987e777d274b2354f3dd4f48e717b69e1b55`
Ownership audit branch: `chatgpt/takeover-safety-audit`

## Ownership state

- GitHub repository access: CONFIRMED (read/write through connected GitHub integration)
- Architecture mapping: SUBSTANTIALLY COMPLETE for repository-visible execution/data paths
- Safety hardening branch: IMPLEMENTED and under CI
- Production mutation: FROZEN pending runtime reconciliation
- VPS/runtime access: NOT YET DIRECTLY CONNECTED
- Windows MT5 bridge runtime: NOT YET DIRECTLY CONNECTED

## Authoritative execution architecture

1. Mandate Strategist is the configured authoritative strategist path when `USE_MANDATE_STRATEGIST=true`.
2. Legacy `auto_executor.py` is a back-compat/development path. The takeover branch blocks its executable entry point while preserving dry-run/research use.
3. PREDATOR is an independent specialist engine with its own execution manager; repository default keeps its execution disabled/shadow-only.
4. Generic broker REST execution provider is an inert skeleton and cannot place broker orders.
5. The historical direct/local MT5 `place_demo_market_order()` path is explicitly disabled by takeover policy.
6. The intended executable order flow is therefore `PendingExecution -> hardened Windows MT5 bridge -> MetaTrader5`.

## Confirmed safety controls

- Repository defaults disable generic broker execution, MT5 execution, auto execution, live-trading authorization, and MT5 bridge execution.
- Mandate Strategist refuses SELL execution; SELLs are shadowed and PREDATOR owns SELL execution policy.
- Mandate enqueue includes Monday observation, position cap, fixed-lot/aggregate cap, bridge heartbeat, sanctioned demo login/server/symbol, `live_execution_allowed=false`, `ALLOW_DEMO_TRADING`, and bridge-enabled checks.
- The portfolio governor enforces a global 0.15 gross-lot ceiling, startup reconciliation, MT5-authoritative freshness/mismatch checks, and atomic capacity reservations.
- PREDATOR has local exposure control, sanctioned-demo verification and global portfolio-governor checks.
- PREDATOR expansion is hard-disabled in the scheduler path currently audited.

## Manipulation / accumulation capability

The repository already implements an ICT-style `Accumulation -> Manipulation -> Distribution` framework in `services/ict_advanced.py`, including Judas-swing/session liquidity-sweep and reversal detection. The repository also contains `services/four_hour_manipulation.py`; its predictive value still requires separate validation. Validation—not greenfield implementation—is the next research task: measure false positives/negatives and determine whether Track-A quote-level microstructure improves timing and confirmation.

## Takeover safety findings and mitigations

### S1 — global portfolio governor caller paths could fail open

Finding: Strategist and PREDATOR caller exception handlers could log a governor exception and continue.

Branch mitigation: `services/execution_safety_bootstrap.py` wraps `reserve_capacity()` and `check_new_order()` at the shared governor boundary. Unexpected errors now return explicit denials with `within_limit=false`, `state_unknown=true`, and zero remaining capacity. Existing caller fail-open exception handlers therefore no longer receive ordinary governor failures as exceptions.

Status: **MITIGATED IN BRANCH; CI COVERED; RUNTIME DEPLOYMENT PENDING.**

### S2 — legacy auto-executor confirmation failures could fail open

Finding: killzone-policy and ICT-framework exceptions in the historical legacy executor could continue toward execution.

Branch mitigation: executable `evaluate_and_execute()` is blocked by takeover policy. Dry-run/research paths remain available. This removes the unsafe execution authority without changing strategy logic.

Status: **MITIGATED IN BRANCH; CI COVERED; RUNTIME DEPLOYMENT PENDING.**

### S3 — bridge daemon lacked independent local sanctioned-account guard

Finding: upstream backend checks used the last bridge heartbeat, but the original Windows daemon did not independently re-check the currently connected account immediately before order execution.

Branch mitigation:
- `deploy/bridge_safety.py` validates login, exact server, DEMO trade mode, terminal connectivity/trading permission.
- `deploy/mt5_bridge_daemon_hardened.py` performs those checks plus an XAU/USD symbol sanity check immediately before handing off to the existing executor.
- `deploy/switch_bridge_to_hardened.ps1` provides a dry-run-first Scheduled Task cutover with XML backup and rollback instructions.

Status: **IMPLEMENTED AND TESTED IN BRANCH; WINDOWS RUNTIME CUTOVER PENDING.**

### S4 — Strategist reservations could remain RESERVED after later refusal

Finding: capacity is reserved before several subsequent demo/config checks; refusal after reservation could leave capacity occupied until TTL cleanup.

Branch mitigation: the takeover bootstrap wraps `_maybe_enqueue_demo_order()`, identifies new Strategist `RESERVED` entries and releases them immediately whenever enqueue returns/refuses without producing an order. `SENT` reservations are explicitly preserved.

Status: **MITIGATED IN BRANCH; CI COVERED; RUNTIME DEPLOYMENT PENDING.**

### S5 — alternate direct/local MT5 execution path bypassed the hardened bridge

Finding: `/api/v1/mt5/demo-order` calls `services.mt5_provider.place_demo_market_order()` directly. Despite its historical “demo” naming, the provider's account-mode gate can admit a live account when `LIVE_TRADING_AUTHORIZED=true`, and the function ultimately calls `mt5.order_send()` locally. This is an alternate execution authority outside the hardened bridge path.

Branch mitigation: takeover bootstrap replaces `place_demo_market_order()` with a fail-closed wrapper raising `DIRECT_LOCAL_MT5_EXECUTION_DISABLED`. This also prevents the legacy local auto-executor path from bypassing the queue/bridge architecture. No signal-generation or research logic is affected.

Status: **MITIGATED IN BRANCH; CI COVERED; RUNTIME DEPLOYMENT PENDING.**

## Verification assets added

- `backend/tests/test_takeover_execution_safety.py` — fail-closed governor, package bootstrap, reservation cleanup, legacy-executor block, and direct/local MT5 block tests.
- `backend/tests/test_bridge_safety.py` — login/server/demo/terminal guard tests.
- `backend/scripts/takeover_safety_check.py` — read-only post-deploy assertion that all takeover safety patches are installed and dangerous live/generic switches remain off.
- `.github/workflows/safety-tests.yml` — non-deploying CI for takeover safety tests, syntax checks and PowerShell parser validation.
- `deploy/takeover_readonly_audit.py` — redacted read-only runtime collector.
- `docs/TAKEOVER_DEPLOYMENT_RUNBOOK.md` — deployment, demo smoke-test and rollback procedure.

Every branch commit must pass the safety workflow before merge.

## Data / research state

- Track A: broker quote-level microstructure only; never label as true order flow, delta, CVD or aggressor flow.
- Track B: HOLD.
- CME options/futures research: research-only until canonical integrity/mapping validation is complete.
- Gamma/GEX: experimental/disabled for production positioning claims.

## Runtime reconciliation

Run the read-only collector from repo root on each relevant host:

```bash
python deploy/takeover_readonly_audit.py > takeover_runtime_audit.txt
```

The collector reports git/runtime/config switch state while redacting secrets. It does not restart services, alter databases, or place orders.

After backend deployment, run inside the backend environment/container:

```bash
python scripts/takeover_safety_check.py
```

For the Windows bridge, first validate the proposed Scheduled Task change without mutation:

```powershell
powershell -ExecutionPolicy Bypass -File .\deploy\switch_bridge_to_hardened.ps1
```

Only after review should `-Apply` be used.

## Takeover completion gates

Operational ownership requires:

1. VPS runtime HEAD/config/services reconciled with this repository.
2. Windows bridge runtime/account state independently verified and hardened launcher cut over on DEMO.
3. Safety PR CI green at final head and reviewed before merge.
4. Signal -> grading -> notification -> queue -> bridge result lifecycle proven end-to-end in demo/shadow mode.
5. Current data-source freshness and fallback paths audited.
6. Post-deploy safety check, health and rollback checks completed.

Until those gates pass, no new live-capital authority should be enabled.
