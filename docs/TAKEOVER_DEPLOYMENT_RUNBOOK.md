# ChatGPT Takeover — Safety Deployment Runbook

This runbook deploys the takeover hardening without changing strategy thresholds or enabling live capital.

## 1. Preconditions

- Safety PR tests are green at the final branch head.
- Runtime audit output has been captured from the VPS and Windows bridge host.
- No active production incident is in progress.
- The current `main` SHA is recorded for rollback.
- MT5 is connected to the sanctioned DEMO account only.

## 2. What this hardening changes

Backend service import installs four defence-in-depth controls:

1. Portfolio-governor exceptions return explicit execution rejections rather than escaping into caller fail-open handlers.
2. Strategist reservations created before a later refusal are released immediately if still `RESERVED`.
3. The historical legacy autonomous executor is blocked from executable use; its dry-run/research paths remain available.
4. Direct/local `services.mt5_provider.place_demo_market_order()` execution is blocked, including `/api/v1/mt5/demo-order`, so execution authority is funnelled through `PendingExecution -> hardened Windows bridge`.

Windows bridge hardening is a separate cutover:

- `deploy/mt5_bridge_daemon_hardened.py` re-verifies active MT5 login, exact server, DEMO trade mode, terminal connectivity/trading permission, and XAU/USD symbol immediately before each execution attempt.

## 3. VPS pre-deploy audit

From repository root:

```bash
python deploy/takeover_readonly_audit.py > takeover_runtime_audit.before.json
```

Confirm the expected execution flags. Do not expose secrets in tickets/chat.

## 4. Merge/deploy backend

The repository deployment workflow triggers only on `main` push (or explicit workflow dispatch). Merging the safety PR will therefore build and deploy the backend/frontend images through the existing pipeline.

After deploy:

```bash
python deploy/takeover_readonly_audit.py > takeover_runtime_audit.after.json
```

Then run the takeover assertions inside the backend runtime/container:

```bash
python scripts/takeover_safety_check.py
```

Required result: `"ok": true`, with all four takeover patches true and `BROKER_EXECUTION_ENABLED=false`, `LIVE_TRADING_AUTHORIZED=false`, `USE_MANDATE_STRATEGIST=true`.

Check backend health and logs for `[takeover-safety]`. Confirm there are no patch-install exceptions.

## 5. Windows bridge cutover

Pull the merged repository on the Windows bridge machine first.

Dry-run the task update:

```powershell
powershell -ExecutionPolicy Bypass -File .\deploy\switch_bridge_to_hardened.ps1
```

Review the detected Scheduled Task executable, current arguments and proposed arguments. Then apply:

```powershell
powershell -ExecutionPolicy Bypass -File .\deploy\switch_bridge_to_hardened.ps1 -Apply
```

The script exports the existing Scheduled Task XML before changing it and prints the rollback command.

## 6. Demo smoke test

Do not create a live-account test. Validate only on the sanctioned demo account.

Required observations:

- bridge heartbeat reaches the VPS;
- active login/server are the expected demo credentials;
- candle and Track-A research pushes continue;
- no legacy auto-executor order can fire;
- direct `/api/v1/mt5/demo-order` execution returns the takeover safety rejection rather than reaching `order_send`;
- a simulated governor exception produces an execution denial in tests/logs;
- a deliberately mismatched MT5 account is rejected locally by the hardened bridge before `order_send`;
- normal demo queue lifecycle remains `PENDING -> EXECUTING -> ACCEPTED/REJECTED -> CLOSED`;
- Strategist/PREDATOR signal generation and Telegram/shadow analytics continue independently of the disabled alternate execution paths.

## 7. Rollback

Backend rollback:

1. Revert the takeover safety PR on `main` or redeploy the previous known-good SHA/image tag.
2. Confirm backend health.

Windows bridge rollback:

Use the Scheduled Task XML backup path printed by `switch_bridge_to_hardened.ps1`:

```powershell
Register-ScheduledTask -TaskName "XAUUSD MT5 Bridge" -Xml (Get-Content -Raw "<backup.xml>") -Force
Start-ScheduledTask -TaskName "XAUUSD MT5 Bridge"
```

## 8. Completion gate

Do not mark operational ownership complete until both runtime audits are reconciled, the backend takeover check is green, the hardened Windows bridge has been verified on demo, and the full signal-to-result lifecycle has been observed. GitHub/code ownership alone is not equivalent to runtime ownership.
