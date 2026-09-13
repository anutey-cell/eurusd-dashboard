# XAUUSD Engine — Current System State

Status date: 2026-09-13
Baseline repository commit: `0c17987e777d274b2354f3dd4f48e717b69e1b55`
Ownership audit branch: `chatgpt/takeover-safety-audit`

## Ownership state

- GitHub repository access: CONFIRMED (read/write/admin through connected GitHub integration)
- Architecture mapping: IN PROGRESS, substantially complete
- Production mutation: FROZEN during takeover audit
- VPS/runtime access: NOT YET DIRECTLY CONNECTED
- Windows MT5 bridge runtime: NOT YET DIRECTLY CONNECTED

## Authoritative execution architecture

1. Mandate Strategist is the configured authoritative strategist path when `USE_MANDATE_STRATEGIST=true`.
2. Legacy `auto_executor.py` remains a back-compat/development path and should remain dormant in mandate mode.
3. PREDATOR is an independent specialist engine with its own execution manager; repository default keeps its execution disabled/shadow-only.
4. Generic broker REST execution provider is currently an inert skeleton and cannot place broker orders.
5. Actual executable order flow is through `PendingExecution` -> MT5 bridge daemon -> MetaTrader5.

## Confirmed safety controls

- Repository defaults disable generic broker execution, MT5 execution, auto execution, live-trading authorization, and MT5 bridge execution.
- Mandate strategist refuses SELL execution; SELLs are shadowed and PREDATOR owns SELL execution policy.
- Mandate enqueue includes Monday observation, position cap, fixed-lot/aggregate cap, bridge heartbeat, sanctioned demo login/server/symbol, `live_execution_allowed=false`, `ALLOW_DEMO_TRADING`, and bridge-enabled checks.
- PREDATOR execution manager has local exposure control, sanctioned-demo verification and global portfolio-governor checks.
- PREDATOR expansion is hard-disabled in the scheduler path currently audited.

## Manipulation / accumulation capability

The repository already implements an ICT-style `Accumulation -> Manipulation -> Distribution` framework in `services/ict_advanced.py`, including Judas-swing/session liquidity-sweep and reversal detection. This is already consumed as a confirmation layer by the legacy auto-executor. The next research task is validation, not greenfield implementation: test false-positive/false-negative rates and determine whether Track-A quote-level microstructure improves timing and confirmation.

## Safety defects found during takeover

### S1 — global portfolio governor can fail open

Both the legacy/strategist execution paths contain exception handlers around governor checks that log an error but can continue. Capital/risk governors must fail CLOSED: if the governor cannot evaluate, no new order should proceed.

Affected areas observed:
- `services/strategist_runner.py` global governor reservation exception path
- `services/predator_execution_manager.py` global governor check exception path

### S2 — legacy auto-executor confirmation gates can fail open

`services/auto_executor.py` currently allows execution to continue if the killzone-policy or ICT-framework module raises. Research/diagnostic enrichments may fail open; execution-authorisation gates should not.

### S3 — bridge daemon lacks an independent local sanctioned-account guard before `order_send`

The backend mandate and PREDATOR enqueue paths verify a sanctioned demo heartbeat, but the Windows bridge daemon itself should independently verify the active MT5 login/server immediately before every order. This protects against account switching after heartbeat/enqueue and provides defence in depth.

### S4 — reservation cleanup path requires review

Strategist reserves portfolio capacity before several later demo-account/config checks. Some early returns after reservation do not visibly release the reservation in the audited code window. TTL may eventually recover capacity, but all post-reservation refusal paths should explicitly release.

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

The collector reports git/runtime/config switch state while redacting secrets. It does not restart services, query trading rows, alter databases, or place orders.

## Takeover completion gates

Ownership is considered operationally complete only after:

1. VPS runtime HEAD/config/services are reconciled with this repository.
2. Windows bridge runtime/account state is independently verified.
3. S1-S4 are fixed and tested on a branch/PR.
4. Signal -> grading -> notification -> queue -> bridge result lifecycle is proven end-to-end in demo/shadow mode.
5. Current data-source freshness and fallback paths are audited.
6. A single production deployment/runbook and rollback procedure is documented.

Until those gates pass, no new live-capital authority should be enabled.
