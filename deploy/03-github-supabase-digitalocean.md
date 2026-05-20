# GitHub + Supabase + DigitalOcean — Production Deployment

> **Goal**: dashboard runs 24/7, scanner + predictor + Telegram alerts fire even
> when your laptop is off, all source code lives on GitHub, all data lives in
> Supabase Postgres.
>
> **Cost**: $6/month (DigitalOcean Basic Droplet) + $0/month (Supabase free tier
> 500 MB Postgres + 2 GB egress) = **~$6/month total**.
>
> **Time first run**: ~45 minutes. Re-deploys: ~3 minutes (`git push` triggers it).

---

## Architecture overview

```
   ┌────────────────┐                ┌──────────────────────┐
   │   GitHub       │  push main →   │  GitHub Actions      │
   │   (source)     │                │  (CI: build + ship)  │
   └────────────────┘                └─────────┬────────────┘
                                               │ docker push
                                               ▼
                                     ┌──────────────────────┐
                                     │ DigitalOcean Droplet │  ←─── 24/7 cloud
                                     │  • backend (Docker)  │
                                     │  • frontend (Docker) │
                                     │  • Caddy (HTTPS)     │
                                     └──────┬───────────────┘
                                            │ SQL over TLS
                                            ▼
                                     ┌──────────────────────┐
                                     │ Supabase Postgres    │  ←─── persistent state
                                     │  • paper_observations│       (survives droplet
                                     │  • scans/signals     │        rebuilds)
                                     │  • bridge queue      │
                                     └──────────────────────┘
                                            │
                                            ▼ pulls pending orders
                                     ┌──────────────────────┐
                                     │ Your Windows Laptop  │  ←─── only on when needed
                                     │  • mt5_bridge_daemon │
                                     │  • MetaTrader5       │
                                     └──────────────────────┘
```

**What runs 24/7 in the cloud** (everything except MT5 execution):
- Scanner loop (every 60s)
- High-probability predictor (every 5m)
- Killzone analyzer
- Telegram alerts on STRONG signals
- Paper observation logging + resolution
- Auto-executor (enqueues orders for the bridge)

**What runs only when your laptop is on** (the MT5 bridge):
- Polls `/api/v1/bridge/pending-orders` every 30s
- Places real MT5 orders on Exness
- Reports back execution result
- When laptop is off, orders auto-expire after 5 min unclaimed (and you still got the Telegram alert)

---

## Phase 1 — Supabase (5 minutes)

### 1.1 Create a Supabase project

1. Visit https://supabase.com/ → **Sign in with GitHub**
2. **New project** → fill in:
   - Name: `xauusd-dashboard`
   - Database password: generate a strong one (24+ chars), **save it** in a password manager
   - Region: pick closest to your DigitalOcean droplet region (`eu-central-1` if your droplet is in Frankfurt)
   - Pricing plan: **Free** (500 MB DB, 2 GB egress, 50k MAUs — way more than you need)
3. Wait ~2 min for provisioning

### 1.2 Copy the connection string

1. In the Supabase project: **Settings → Database**
2. Scroll to **Connection string** → choose the **Session pooler** (port 6543) tab
3. Copy the string. It looks like:
   ```
   postgresql://postgres.<project-ref>:<password>@aws-0-<region>.pooler.supabase.com:6543/postgres
   ```
4. Convert to SQLAlchemy format by prefixing `postgresql+psycopg2://`:
   ```
   postgresql+psycopg2://postgres.<project-ref>:<password>@aws-0-<region>.pooler.supabase.com:6543/postgres
   ```
   **Save this string — you'll paste it as `DATABASE_URL` later.**

> 💡 The **session pooler** (port 6543) is what you want for our use case. Direct connection (port 5432) is fine too but the pooler handles connection limits gracefully on the free tier.

### 1.3 (Optional) Migrate existing local data

If your local SQLite has paper observations + backtest history you want to keep:

```bash
# On your laptop, export from SQLite then import to Supabase
docker exec eurusd-dashboard-backend-1 sh -c "sqlite3 /app/storage/eurusd_signals.db .dump" > local.sql

# Edit local.sql to convert SQLite-specific syntax (DROP IF EXISTS lines, AUTOINCREMENT)
# Then import with psql:
psql "<your-DATABASE_URL>" < local.sql
```

For a fresh start (recommended on first deploy), skip this — the backend's `lifespan` startup creates all tables automatically via `Base.metadata.create_all()`.

---

## Phase 2 — GitHub (5 minutes)

### 2.1 Confirm your repo is up to date

We just pushed everything (see top of this conversation). Your repo:
```
https://github.com/anutey-cell/eurusd-dashboard
```

If you need to re-push later:
```powershell
cd C:\Users\anwar.mohamed\eurusd-dashboard
git add -A
git commit -m "Update: ..."
git push origin main
```

### 2.2 Set GitHub Actions secrets

You need to give GitHub Actions the credentials to deploy on push:

1. Go to https://github.com/anutey-cell/eurusd-dashboard
2. **Settings → Secrets and variables → Actions → New repository secret**

Add these **secrets** (the workflow file `.github/workflows/deploy.yml` already references them):

| Secret name | Value |
|---|---|
| `DO_HOST`              | Your DigitalOcean droplet IP (you'll create this in Phase 3) |
| `DO_SSH_PRIVATE_KEY`   | Contents of `~/.ssh/digitalocean_xauusd` (private key) |
| `DO_USER`              | `root` |
| `DATABASE_URL`         | The Supabase connection string from Step 1.2 |
| `TELEGRAM_BOT_TOKEN`   | From `backend/.env` |
| `TELEGRAM_CHAT_ID`     | From `backend/.env` |
| `FRED_API_KEY`         | From `backend/.env` |
| `TRADINGVIEW_USERNAME` | From `backend/.env` |
| `TRADINGVIEW_PASSWORD` | From `backend/.env` |
| `MT5_BRIDGE_SHARED_SECRET` | Generate fresh: `python -c "import secrets; print(secrets.token_urlsafe(48))"` |
| `FQDN`                 | Your DigitalOcean droplet public hostname or DuckDNS name |

> 🔐 **Why secrets, not env files**: secrets are encrypted at rest, only injected at workflow runtime, and never appear in logs. Committing `.env.prod` to git is the #1 security mistake of this kind of project.

---

## Phase 3 — DigitalOcean Droplet (10 minutes)

### 3.1 Create an SSH key for the Droplet

In PowerShell:
```powershell
ssh-keygen -t ed25519 -C "xauusd-do" -f $HOME\.ssh\digitalocean_xauusd -N '""'
Get-Content $HOME\.ssh\digitalocean_xauusd.pub
```
Copy the public key output.

### 3.2 Create the Droplet

1. https://cloud.digitalocean.com/ → Sign up (referral credit often available)
2. **Create → Droplets**
3. Choose:
   - **Region**: closest to you (or to your TradingView session — Frankfurt FRA1 is solid for EU/Africa)
   - **Image**: Ubuntu 22.04 (LTS) x64
   - **Size**: **Basic → Regular SSD → $6/month** (1 GB RAM, 1 vCPU, 25 GB SSD, 1 TB transfer)
     - This is tight. Bump to **$12/mo (2 GB RAM)** if you'll run probability sweeps often.
   - **Authentication**: SSH Keys → **Add new SSH Key** → paste the public key from Step 3.1 → name it `xauusd-laptop`
   - **Hostname**: `xauusd-dashboard-do`
   - **Backups**: optional (+$1.20/mo, daily snapshots)
4. **Create Droplet** → wait ~1 min for it to boot
5. Copy the assigned IP address (e.g. `134.209.x.x`)

### 3.3 Reserve the IP (free, but persistent)

1. **Networking → Reserved IPs → Reserve IP**
2. Assign to the droplet you just created
3. Use this IP wherever you need the public address — survives droplet rebuilds

### 3.4 Set up SSH alias on your laptop

```powershell
Add-Content $HOME\.ssh\config @"

Host doxau
  HostName <YOUR_DO_IP>
  User root
  IdentityFile ~/.ssh/digitalocean_xauusd
  ServerAliveInterval 60
"@

# Verify
ssh doxau 'uname -a; nproc; free -h'
```

### 3.5 Bootstrap the droplet

The same `bootstrap-vm.sh` we built for Hetzner works unchanged on DigitalOcean:

```powershell
scp -i $HOME\.ssh\digitalocean_xauusd deploy\bootstrap-vm.sh root@doxau:~/bootstrap-vm.sh
ssh doxau
chmod +x ~/bootstrap-vm.sh
sudo ~/bootstrap-vm.sh
```

It will install Docker, Caddy (HTTPS), UFW firewall, swap (important on the 1 GB plan), and unattended security upgrades. You'll be prompted for:
- DuckDNS subdomain (e.g. `xauusd-anwar.duckdns.org`)
- DuckDNS token (from https://duckdns.org)
- Admin email (for Let's Encrypt cert renewal warnings)

### 3.6 Configure DigitalOcean cloud firewall

In addition to the on-droplet UFW, add a Cloud Firewall:

1. **Networking → Firewalls → Create Firewall**
2. **Name**: `xauusd-fw`
3. **Inbound rules**:
   - SSH (22) — All IPv4
   - HTTP (80) — All IPv4
   - HTTPS (443) — All IPv4
4. **Outbound rules**: keep defaults (all allowed)
5. **Apply to droplet**: tick `xauusd-dashboard-do`
6. **Create Firewall**

---

## Phase 4 — Deploy (one-time setup, then auto-deploys)

### 4.1 First deploy via SSH (manual)

```powershell
# Upload source
rsync -avz --delete `
  --exclude='node_modules' --exclude='.git' --exclude='__pycache__' `
  --exclude='backend/.env' --exclude='backend/*.db*' `
  -e "ssh -i $HOME\.ssh\digitalocean_xauusd" `
  C:\Users\anwar.mohamed\eurusd-dashboard\ root@doxau:/opt/xauusd/

# SSH in and configure .env.prod
ssh doxau
cd /opt/xauusd
cp deploy/.env.prod.example deploy/.env.prod
nano deploy/.env.prod
```

In `deploy/.env.prod`, set at minimum:

```bash
FQDN=xauusd-anwar.duckdns.org
DATA_MODE=live

# The Supabase connection string from Phase 1.2
DATABASE_URL=postgresql+psycopg2://postgres.xxx:PASSWORD@aws-0-eu-central-1.pooler.supabase.com:6543/postgres

# Telegram (from your local .env)
TELEGRAM_ALERTS_ENABLED=true
TELEGRAM_BOT_TOKEN=...
TELEGRAM_CHAT_ID=...

# Live providers
TRADINGVIEW_ENABLED=true
TRADINGVIEW_USERNAME=...
TRADINGVIEW_PASSWORD=...
FRED_API_KEY=...

# MT5 bridge
MT5_BRIDGE_ENABLED=true
MT5_BRIDGE_SHARED_SECRET=<long random>

# Auto-execution (system fires only when 3 layers agree)
AUTO_EXECUTION_ENABLED=true
AUTO_EXECUTION_MAX_LOT=0.05
AUTO_EXECUTION_MAX_TRADES_PER_DAY=3

CORS_ORIGINS=["https://xauusd-anwar.duckdns.org"]
```

The compose file we already wrote — `deploy/docker-compose.prod.yml` — works unchanged because it mounts `DATABASE_URL` from `.env.prod`. Switch from SQLite happens automatically.

Now deploy:

```bash
sudo bash /opt/xauusd/deploy/deploy.sh
```

The script will:
1. Validate `.env.prod`
2. Install `/etc/caddy/Caddyfile` with your FQDN
3. Build Docker images (first run ~3 min)
4. Start backend + frontend containers
5. Wait for `/api/v1/health` to succeed
6. Print your live URL

### 4.2 Verify Supabase is being used

```bash
ssh doxau
docker logs xauusd-backend 2>&1 | grep -iE 'database|postgres|engine' | head -10
```

You should see SQLAlchemy logging Postgres connection. If you see SQLite, your `DATABASE_URL` isn't being read.

Or check the live data:

```bash
curl https://xauusd-anwar.duckdns.org/api/v1/health | jq
# Should show "database": "connected"
```

Open Supabase **Table Editor** in your browser — you should now see tables created: `paper_observations`, `scans`, `pending_executions`, etc. They start empty.

### 4.3 Install monitoring + backups

```bash
sudo bash /opt/xauusd/deploy/install-cron.sh
```

This installs:
- **Daily SQLite backup at 02:00 UTC** — NOTE: this becomes redundant if you're using Supabase, because Supabase has built-in PITR backups on the free tier. You can comment out the backup cron line if you wish.
- **Health watchdog every 5 min** — fires Telegram if backend dies, auto-restarts after 2 consecutive failures.

---

## Phase 5 — GitHub Actions auto-deploy (10 minutes)

Once the manual deploy works, set up the workflow so every `git push` to `main` automatically redeploys:

### 5.1 The workflow file is already in your repo at:

```
.github/workflows/deploy.yml
```

(See Task 42 below for what it contains.)

### 5.2 Trigger the workflow

```powershell
cd C:\Users\anwar.mohamed\eurusd-dashboard
git pull
git add .github/
git commit -m "ci: add auto-deploy workflow"
git push origin main
```

Within 30 seconds, GitHub Actions starts running. You can watch progress at:
```
https://github.com/anutey-cell/eurusd-dashboard/actions
```

The workflow:
1. SSH into your DO droplet using `DO_SSH_PRIVATE_KEY`
2. `git pull` the latest code
3. Rebuild Docker images
4. Restart containers
5. Wait for healthcheck

Total time: ~2 minutes per deploy.

---

## Phase 6 — MT5 bridge daemon (your Windows laptop)

This part is unchanged from earlier docs. Run on your Windows laptop only:

```powershell
cd C:\Users\anwar.mohamed\eurusd-dashboard
python -m pip install requests MetaTrader5
# Edit deploy\.env.bridge to point at the DO droplet:
# DASHBOARD_URL=https://xauusd-anwar.duckdns.org
# BRIDGE_SECRET=<same value as MT5_BRIDGE_SHARED_SECRET on droplet>
python deploy\mt5_bridge_daemon.py
```

Verify the daemon registered:
```bash
curl https://xauusd-anwar.duckdns.org/api/v1/bridge/status | jq '.data.daemons'
```

---

## ✅ End-to-end verification checklist

You're fully deployed when ALL of these are true:

- [ ] `https://<your>.duckdns.org/` loads the dashboard with HTTPS (green padlock)
- [ ] `/api/v1/health` returns `{"status":"ok", "database": "connected"}`
- [ ] Supabase Table Editor shows tables: `paper_observations`, `pending_executions`, `mt5_trade_logs`
- [ ] Telegram receives the **"Background scheduler started"** ping (or first scan log within 5 min)
- [ ] **TURN YOUR LAPTOP OFF for 30 min** — Telegram still pings you when the scanner detects a SIGNAL_READY state
- [ ] GitHub Actions shows the deploy workflow ✅ green
- [ ] Push a trivial change (README typo fix) → workflow auto-redeploys within 2 min

---

## Cost summary

| Item | Monthly |
|---|---|
| DigitalOcean Basic Droplet (1 GB / 1 vCPU) | $6.00 |
| Reserved IP | $0 (free while attached to a running droplet) |
| Optional DO backups | $1.20 |
| Supabase Postgres (Free tier — 500 MB) | $0 |
| Supabase egress | $0 (way under 2 GB/mo limit) |
| DuckDNS dynamic DNS | $0 |
| GitHub Actions (public repo) | $0 (2000 min/mo for private repo also free) |
| **TOTAL** | **$6/mo** (or $7.20 with backups) |

---

## Troubleshooting

| Symptom | Fix |
|---|---|
| `psycopg2.OperationalError: could not connect to server` | Check the Supabase URL prefix is `postgresql+psycopg2://` (not `postgres://`). Pooler URL uses port `6543`, direct uses `5432` |
| Tables don't appear in Supabase | Backend hasn't started yet — wait 30s after `deploy.sh` finishes, then refresh Supabase Table Editor |
| GitHub Action fails with "Host key verification failed" | The workflow uses `StrictHostKeyChecking=no` for that exact reason — if you still get this, your `DO_SSH_PRIVATE_KEY` secret was pasted incorrectly (missing trailing newline) |
| Telegram alerts stop arriving | Check `docker logs xauusd-backend | tail -50` on the droplet. Most common cause: Telegram bot token rotated. Update both `.env.prod` and the GitHub secret |
| Droplet swap-thrashing on 1GB plan | Either: resize to 2GB ($12/mo), OR disable `walk_forward_segments` + `monte_carlo_runs` in backtester defaults |
| Supabase shows "Connection terminated" errors | Use the pooler URL (port 6543), not direct (port 5432). The pooler handles transient connection drops gracefully |
