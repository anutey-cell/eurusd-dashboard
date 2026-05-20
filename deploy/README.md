# XAU/USD Signal Dashboard — VPS Deployment Kit

> **Target**: Oracle Cloud Infrastructure (OCI) Always Free tier
> **Cost**: $0 / month, forever
> **Architecture**: Linux VPS runs the dashboard 24/7; Windows laptop runs a small MT5 bridge daemon to execute orders.

---

## What this kit gives you

A complete, idempotent deployment of the XAU/USD Signal Dashboard with:

| Concern | Solution |
|---|---|
| **24/7 uptime** | Always-on Oracle Cloud VPS (ARM Ampere, 4 OCPU / 24 GB RAM, free forever) |
| **Telegram alerts when laptop is off** | Backend, scanner, predictor, paper-observation all run on the VPS |
| **HTTPS** | Caddy reverse proxy with auto-provisioned Let's Encrypt certs |
| **Dynamic DNS** | Free DuckDNS subdomain, auto-updated every 5 min |
| **MT5 execution** | Bridge architecture: VPS queues orders, Windows daemon polls & executes locally |
| **Persistent SQLite** | Mounted host volume at `/opt/xauusd/data/` |
| **Daily backups** | Gzipped SQLite snapshots, 30-day retention |
| **Watchdog** | Health checks every 5 min; auto-restart + Telegram alert on outage |
| **Security** | UFW firewall, fail2ban, unattended security upgrades, bridge endpoint requires shared secret |

---

## Deploy order (READ THIS FIRST)

This kit is split into **5 phases**. Each one ends with a verification checklist.
**Do not skip ahead** — verify each phase before starting the next.

| Phase | What | Time | Doc |
|---|---|---|---|
| **1** | OCI infrastructure (VM + networking + IP + DNS) | 25 min | `01-oracle-cloud-provisioning.md` |
| **2** | VM bootstrap (Docker, Caddy, firewall, DuckDNS) | 10 min | `02-vm-bootstrap.md` |
| **3** | Source upload + Docker deployment | 10 min | This README, "Phase 3" below |
| **4** | MT5 bridge daemon on Windows laptop | 5 min | This README, "Phase 4" below |
| **5** | Monitoring (cron backup + watchdog) | 2 min | This README, "Phase 5" below |

Total: ~50 minutes the first time. Subsequent re-deploys: ~3 minutes (`git pull && bash deploy.sh`).

---

## Phase 1 — OCI Infrastructure

Open `01-oracle-cloud-provisioning.md` and work through Steps 1.1 → 1.9.

**You're done with Phase 1 when ALL of these are true:**
- [ ] Compute instance `xauusd-dashboard-vps` is RUNNING (Ampere ARM A1, 4 OCPU / 24 GB)
- [ ] `ssh xauusd` lands at a shell prompt
- [ ] `uname -m` → `aarch64`
- [ ] `nproc` → 4, `free -h` → ~24 GB
- [ ] OCI Security List has ingress for ports 22, 80, 443
- [ ] Reserved public IP is attached to the VM
- [ ] DuckDNS subdomain registered (you have the token)

> ⛔ **Do not proceed to Phase 2 until every box above is checked.**

---

## Phase 2 — VM Bootstrap

Open `02-vm-bootstrap.md` and work through Steps 2.1 → 2.4.

**You're done with Phase 2 when ALL of these are true:**
- [ ] `docker version` works without `sudo`
- [ ] `docker compose version` works
- [ ] `sudo ufw status` shows 22/80/443 ALLOW
- [ ] `dig +short <your>.duckdns.org` returns your VM's public IP
- [ ] `https://<your>.duckdns.org/` loads (502 from Caddy is expected — no backend yet)
- [ ] `sudo systemctl status duckdns.timer` is active
- [ ] Survived a `sudo reboot` (caddy + duckdns auto-started)

> ⛔ **Do not proceed to Phase 3 until every box above is checked.**

---

## Phase 3 — Docker deployment

Now the VPS infrastructure is verified ready. Time to ship the dashboard.

### 3.1 — Upload source to the VM

From your laptop:

```powershell
# Upload the whole repo to /opt/xauusd on the VM.
# --exclude lists keep node_modules + .git out of the transfer
rsync -avz --delete `
  --exclude='node_modules' --exclude='.git' --exclude='__pycache__' `
  --exclude='backend/.env' --exclude='deploy/.env.prod' `
  -e "ssh -i $HOME\.ssh\oci_xauusd" `
  C:\Users\anwar.mohamed\eurusd-dashboard\ ubuntu@xauusd:/opt/xauusd/
```

(On Windows without rsync? Install via `winget install -e --id cwRsync.cwRsync` or use WSL.)

### 3.2 — Create the production .env

SSH in and copy the template:

```bash
ssh xauusd
cd /opt/xauusd
cp deploy/.env.prod.example deploy/.env.prod
nano deploy/.env.prod
```

Fill in (at minimum):
- `FQDN` — your DuckDNS hostname
- `TELEGRAM_BOT_TOKEN` + `TELEGRAM_CHAT_ID`
- `FRED_API_KEY` — already filled with your key as default
- `MT5_BRIDGE_SHARED_SECRET` — generate with:
  ```
  python3 -c "import secrets; print(secrets.token_urlsafe(48))"
  ```
- `CORS_ORIGINS` — `["https://<your>.duckdns.org"]`

> 🔐 The bridge secret protects your MT5 account from anyone discovering the bridge URL. Make it long and random. It goes in TWO places: this file AND `.env.bridge` on your laptop. They must match exactly.

### 3.3 — Deploy

```bash
sudo bash /opt/xauusd/deploy/deploy.sh
```

The script:
1. Validates `.env.prod`
2. Installs `/etc/caddy/Caddyfile` with your FQDN
3. Builds Docker images (ARM64-native, ~3 min first time)
4. Starts the stack
5. Waits for backend `/api/v1/health` to return 200
6. Waits for frontend port 5173 to respond
7. Prints the public URL

**You're done with Phase 3 when:**
- [ ] `https://<your>.duckdns.org/` loads the dashboard (real XAU/USD price in header)
- [ ] `https://<your>.duckdns.org/api/v1/health` returns `{"status":"ok",...}`
- [ ] `docker ps` shows `xauusd-backend` and `xauusd-frontend` healthy
- [ ] Caddy access log at `/var/log/caddy/access.log` shows your browser's requests
- [ ] You received a "scanner started" or first scheduler tick in Telegram (within 60 s)

---

## Phase 4 — MT5 bridge daemon (on your Windows laptop)

The VPS produces orders; this small Python script consumes them on Windows.

### 4.1 — Install Python deps on your laptop

```powershell
cd C:\Users\anwar.mohamed\eurusd-dashboard
python -m pip install requests MetaTrader5
```

### 4.2 — Create the bridge env

```powershell
copy deploy\.env.bridge.example deploy\.env.bridge
notepad deploy\.env.bridge
```

Fill in:
- `DASHBOARD_URL=https://<your>.duckdns.org`
- `BRIDGE_SECRET=<same value as MT5_BRIDGE_SHARED_SECRET on VPS>`
- `MT5_LOGIN/PASSWORD/SERVER` — your Exness credentials

### 4.3 — Run it

```powershell
python deploy\mt5_bridge_daemon.py
```

You should see:
```
INFO mt5_bridge: MT5 Bridge Daemon starting
INFO mt5_bridge: Daemon ID: DESKTOP-XYZ-a3f7c1
INFO mt5_bridge: Dashboard: https://xauusd-anwar.duckdns.org
INFO mt5_bridge: MT5 connected: login=435888680 server=Exness-MT5Trial9 balance=10000.00 currency=USD
```

### 4.4 — Verify the bridge is registered

Open `https://<your>.duckdns.org/api/v1/bridge/status` in a browser. You should see:
```json
{
  "config":{"enabled":true,"secretSet":true,"ttlSeconds":300},
  "queue":{"PENDING":0,"EXECUTING":0,"ACCEPTED":0,"REJECTED":0,"FAILED":0,"EXPIRED":0},
  "daemons":{"DESKTOP-XYZ-a3f7c1":{"lastSeen":"...","ageSeconds":12,"isFresh":true}},
  "anyDaemonFresh": true
}
```

### 4.5 — (Optional) Run the daemon as a Windows service

So it survives reboots and runs without an open terminal:

```powershell
# Install NSSM service manager (one-time):
winget install -e --id NSSM.NSSM

# Register the daemon as a service:
nssm install XauusdBridge "C:\Python311\python.exe" "C:\Users\anwar.mohamed\eurusd-dashboard\deploy\mt5_bridge_daemon.py"
nssm set XauusdBridge AppDirectory "C:\Users\anwar.mohamed\eurusd-dashboard"
nssm set XauusdBridge Start SERVICE_AUTO_START
nssm start XauusdBridge
```

**You're done with Phase 4 when:**
- [ ] Daemon prints "MT5 connected"
- [ ] `/api/v1/bridge/status` shows your daemon with `isFresh: true`
- [ ] Test order flow: stop the daemon, manually insert a pending order via SQL, restart daemon, verify it picks up and reports the result

---

## Phase 5 — Monitoring (backup + watchdog)

Run once on the VPS:

```bash
ssh xauusd
sudo bash /opt/xauusd/deploy/install-cron.sh
```

This installs two cron entries:
- **02:00 UTC daily** — `backup.sh` → gzipped SQLite snapshot to `/opt/xauusd/backups/`, 30-day retention, Telegram confirmation
- **Every 5 min** — `watchdog.sh` → pings `/api/v1/health`; after 2 fails, sends Telegram alert + auto-restart

Test the watchdog by stopping the backend and waiting ~10 minutes:

```bash
docker stop xauusd-backend
# Wait 10 minutes. Watchdog should:
#   - Detect failure
#   - Fire Telegram "🚨 Dashboard DOWN" alert
#   - Auto-restart: docker compose restart
#   - Fire Telegram "✅ Dashboard recovered" once health returns
```

**You're done with Phase 5 when:**
- [ ] `crontab -l | grep xauusd` shows both cron entries
- [ ] `bash /opt/xauusd/deploy/backup.sh` produces a `.db.gz` in `/opt/xauusd/backups/`
- [ ] You received the test "Dashboard DOWN → recovered" Telegram alerts

---

## Operating playbook

### Common tasks

| Task | Command |
|---|---|
| View backend logs | `docker compose -f deploy/docker-compose.prod.yml logs -f backend` |
| View Caddy logs | `tail -f /var/log/caddy/access.log` |
| Restart everything | `docker compose -f deploy/docker-compose.prod.yml restart` |
| Update from git | `cd /opt/xauusd && git pull && sudo bash deploy/deploy.sh` |
| Update from laptop | Re-run the `rsync` from Phase 3.1, then `sudo bash deploy.sh` |
| List backups | `ls -lh /opt/xauusd/backups/` |
| Restore a backup | `gunzip -c backups/eurusd_signals-XXXXX.db.gz > data/eurusd_signals.db && docker compose restart backend` |
| Check bridge daemon | `curl -s https://<fqdn>/api/v1/bridge/status \| jq` |
| Disable auto-trading | Edit `.env.prod` → `AUTO_EXECUTION_ENABLED=false` → restart |
| Manual kill switch | `curl -X POST https://<fqdn>/api/v1/execution/kill-switch` |

### What lives where

```
/opt/xauusd/                              # app root on VPS
├── backend/                              # FastAPI source
├── src/                                  # Vite/React source
├── deploy/
│   ├── .env.prod                         # secrets (gitignored)
│   ├── docker-compose.prod.yml           # production compose
│   ├── Caddyfile                         # reverse proxy template
│   ├── bootstrap-vm.sh                   # ran once during Phase 2
│   ├── deploy.sh                         # re-runnable deployer
│   ├── backup.sh                         # cron daily 02:00 UTC
│   ├── watchdog.sh                       # cron every 5 min
│   ├── install-cron.sh                   # one-time cron installer
│   └── mt5_bridge_daemon.py              # copied to laptop, runs there
├── data/
│   └── eurusd_signals.db                 # SQLite (persistent volume)
├── logs/                                 # backend logs
├── backups/                              # SQLite backups (30-day retention)
└── .fqdn                                 # written by bootstrap-vm.sh
```

### Costs (real numbers)

Oracle Always Free, year-round:
- Compute: $0
- Storage (50 GB boot + small daily backups): $0
- Egress: well under the 10 TB/month limit (dashboard traffic is tiny)
- IPv4: $0 (reserved public IP is free as long as it's attached to a running VM)

**Total: $0/month, forever.** Oracle's Always Free tier never expires and doesn't degrade.

Only ongoing cost: the domain (if you bought one) — but DuckDNS is free too.

---

## Troubleshooting

### "Bridge daemon claims order, but order_send fails"

Check on the laptop:
- MT5 terminal is open and logged in
- Account number in `.env.bridge` matches the one MT5 is logged into
- Trading is enabled in MT5 (Tools → Options → Expert Advisors → Allow algorithmic trading)
- Symbol "XAUUSD" or "XAUUSDm" exists in Market Watch

### "VPS keeps getting OOM-killed"

The free ARM tier has 24 GB RAM — should be plenty. If you're hitting OOM:
- Check `docker stats` — likely a Python memory leak
- Disable Monte Carlo + walk-forward in `.env.prod` (the heaviest backtest paths)
- Reduce `lookback` defaults if you're running constant probability sweeps

### "I'm seeing $3285 prices again on the dashboard"

That's the synthetic fallback. Means TradingView session expired AND the live-candle cache emptied. Restart the backend:
```
docker compose restart backend
```
And verify TradingView credentials in `.env.prod`. Without TradingView, you'll need to add a candle provider with an API key (twelvedata is the cheapest paid option ~$8/mo).

### "Let's Encrypt cert won't provision"

- Port 80 must be reachable from the internet (Caddy uses HTTP-01 challenge)
- DuckDNS must resolve correctly — `dig +short <fqdn>` should return your VPS IP
- Check Caddy logs: `sudo journalctl -u caddy -n 100 --no-pager`

### "Can't SSH back into the VM"

If you accidentally `ufw deny` from a wrong rule, OCI has a console-based serial terminal:
- OCI console → Instance → Console connection → "Launch Cloud Shell connection"
- That lets you fix the firewall without SSH access.
