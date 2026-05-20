# Hetzner Cloud — Provisioning Checklist (Alternative to Oracle Cloud)

> **Use this instead of `01-oracle-cloud-provisioning.md`** if you'd rather pay €4.59/month for a more spec'd machine (4 GB RAM vs OCI's 24 GB ARM — but x86, more reliable provisioning, faster setup).
>
> **Cost**: €4.59/mo (~$5/mo) for the CX22 server + €0.50/mo IPv4 (often included).
> Hourly pricing applies — you can stop the server when you don't need it and only pay for the time it ran.
>
> **Time**: ~10 minutes (Hetzner provisioning is fast — 3-4 minutes from "create" to SSH).

---

## Step 1.1 — Sign up & top up

1. https://accounts.hetzner.com/signUp — sign up, verify email
2. **Identity verification**: Hetzner asks for a phone number + a small one-time charge (~€1, refunded) on a credit card. Takes 5-10 min the first time
3. Log in to **Hetzner Cloud Console**: https://console.hetzner.cloud/
4. Click **+ New project** → name it `xauusd-dashboard`

---

## Step 1.2 — Create an SSH key

On your Windows laptop (PowerShell):

```powershell
# Skip if you already created ~/.ssh/oci_xauusd in the OCI guide — you can reuse it
ssh-keygen -t ed25519 -C "xauusd-vps" -f $HOME\.ssh\hetzner_xauusd -N '""'
Get-Content $HOME\.ssh\hetzner_xauusd.pub   # copy the line that prints
```

In the Hetzner console:
1. Click your **project → SSH Keys → Add SSH Key**
2. Paste the public key, name it `laptop-anwar`, **Add SSH key**

---

## Step 1.3 — Create the server

1. **Project → Servers → + Add Server**
2. **Location**: pick the closest. For Kenya, **Nuremberg (NBG1)** or **Falkenstein (FSN1)** in Germany are best. Helsinki (HEL1) also fine. (Hetzner has 2 US locations too — Ashburn/Hillsboro — pick those if MT5 is on a US broker)
3. **Image**: **Ubuntu 22.04**
4. **Type → Shared vCPU**:
   - **CX22** ← pick this. €4.59/mo. **2 vCPU AMD, 4 GB RAM, 40 GB SSD, 20 TB traffic**
   - (CX32 / CX42 / CX52 are bigger if you want headroom for the future)
5. **Networking**:
   - ✓ Public IPv4
   - ✓ Public IPv6
6. **SSH keys**: ✓ tick `laptop-anwar`
7. **Volumes / Firewalls / Backups / Placement groups / Labels / Cloud config**: skip for now (we'll add firewall in next step)
8. **Name**: `xauusd-dashboard-vps`
9. **Create & Buy now**

Wait ~30 seconds. Status flips to **Running** with a public IP (e.g. `116.203.x.x`).

---

## Step 1.4 — Create + attach a Cloud Firewall

Hetzner's Cloud Firewall is **simpler than OCI's Security Lists** — and it's free.

1. **Project → Firewalls → + Create Firewall**
2. **Name**: `xauusd-fw`
3. **Inbound rules** — add these three:

| Source | Protocol | Port | Description |
|---|---|---|---|
| Any IPv4, Any IPv6 | TCP | **22** | SSH |
| Any IPv4, Any IPv6 | TCP | **80** | HTTP (Caddy → 443 redirect) |
| Any IPv4, Any IPv6 | TCP | **443** | HTTPS (dashboard) |

4. **Outbound rules**: leave default (Any/Any allowed)
5. **Apply to resources** → tick `xauusd-dashboard-vps` → **Create Firewall**

> 💡 The Hetzner Cloud Firewall sits **in front of** the VM (free, fast). You'll *also* enable UFW on the VM itself in Phase 2 — that's defense in depth.

---

## Step 1.5 — Verify SSH connectivity

```powershell
ssh -i $HOME\.ssh\hetzner_xauusd root@<YOUR_PUBLIC_IP>
```

Hetzner gives you **root** by default (no `ubuntu` user). The bootstrap script will create a non-root user if you want, but root is fine for our use case (firewall + ssh-key-only auth).

Add the SSH config shortcut on your laptop:

```powershell
Add-Content $HOME\.ssh\config @"

Host xauusd
  HostName <YOUR_PUBLIC_IP>
  User root
  IdentityFile ~/.ssh/hetzner_xauusd
  ServerAliveInterval 60
"@
```

Now `ssh xauusd` works from anywhere.

---

## Step 1.6 — Sanity-check the VM

```bash
ssh xauusd
nproc                              # → 2
free -h                            # → ~3.8 GB
uname -m                           # → x86_64
df -h /                            # → ~40 GB SSD
lsb_release -a                     # → Ubuntu 22.04 LTS
```

---

## Step 1.7 — DuckDNS subdomain

Same as the OCI guide:
1. Sign in at https://duckdns.org (Google/GitHub)
2. Pick a subdomain, e.g. `xauusd-anwar.duckdns.org`
3. Copy the 36-char token

---

## ✅ Infrastructure-ready checklist

Before moving on to Phase 2, confirm:

- [ ] Server `xauusd-dashboard-vps` is **Running** in Hetzner console
- [ ] `ssh xauusd` lands at a root prompt
- [ ] `nproc` → 2, `free -h` → ~4 GB, `uname -m` → x86_64
- [ ] `xauusd-fw` firewall is attached with rules for 22, 80, 443
- [ ] DuckDNS subdomain registered (you have the token)

Now proceed to **`02-vm-bootstrap.md`** — the bootstrap script and everything after it works identically on Hetzner.

---

## Adjustments vs. the OCI guide

The bootstrap script and all subsequent phases run unchanged. Only these tiny tweaks:

| Detail | OCI | Hetzner |
|---|---|---|
| SSH user | `ubuntu` | `root` |
| Architecture | `aarch64` | `x86_64` |
| Default disk | 50 GB | 40 GB (still plenty) |
| Public IP | Reserved (manual attach) | Auto-assigned, free, persists with VM |
| Firewall | Security List + UFW (two layers) | Cloud Firewall + UFW (two layers) |
| Bootstrap script | Same `bootstrap-vm.sh` | Same `bootstrap-vm.sh` |
| Docker images | Build for ARM64 | Build for AMD64 |

> ⚠ **One thing to watch**: when uploading source via rsync, the path on Hetzner is `/root/xauusd/` if you're SSHing as root, vs `/home/ubuntu/xauusd/` on OCI. The bootstrap script auto-detects this and creates `/opt/xauusd/` as the canonical app dir owned by the right user — but copy the files to `/opt/xauusd/` directly to avoid an extra move:
>
> ```powershell
> rsync -avz --delete `
>   --exclude='node_modules' --exclude='.git' --exclude='__pycache__' `
>   --exclude='backend/.env' --exclude='deploy/.env.prod' `
>   -e "ssh -i $HOME\.ssh\hetzner_xauusd" `
>   C:\Users\anwar.mohamed\eurusd-dashboard\ root@xauusd:/opt/xauusd/
> ```

---

## Operating cost (real numbers, monthly)

| Item | Cost |
|---|---|
| CX22 server (2 vCPU, 4 GB, 40 GB SSD, 20 TB traffic) | €4.59 |
| IPv4 address | €0.50 (often promo'd to free for new servers) |
| Backups (optional, +20% of server) | €0.92 |
| **Total minimum** | **€5.09 (~$5.50)** |
| **Total with Hetzner backups** | **€6.01 (~$6.50)** |

DuckDNS is free. Outbound traffic to Telegram + TradingView is < 1 GB/month, way below the 20 TB included.

---

## When you'd switch to Hetzner from OCI

- **OCI says "Out of capacity"** repeatedly when creating A1 instances (common in popular regions)
- You need to ship in < 10 minutes (Hetzner: 3-4 min provisioning vs OCI's 25+ min)
- You'd rather use x86 Docker images (broader compatibility, more pre-built ARM images don't exist for some niche tools)
- You want predictable billing without the "is this still in the free tier?" anxiety

## When OCI Always Free still wins

- True $0 cost forever — Hetzner's €5 is small but it's not zero
- 24 GB RAM (vs Hetzner CX22's 4 GB) — useful if you'll run multiple instruments or extensive backtests
- 4 OCPU vs 2 — better for the parallel probability sweeps

For a single XAU/USD instrument with 2-week paper testing, **Hetzner CX22 is more than enough**.
