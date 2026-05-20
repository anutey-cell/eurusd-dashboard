# Oracle Cloud Infrastructure — Provisioning Checklist

> **Goal**: Get a 24/7 Linux VM running on Oracle Cloud Always Free, ready to
> host the XAU/USD Signal Dashboard. The VPS will run scanner, predictor,
> Telegram alerts, paper observations, and the auto-executor's order-queue
> producer. Your Windows laptop runs the MT5 order consumer (bridge).
>
> **Cost**: $0/month forever. Oracle's Always Free tier never expires.
>
> **Time**: ~25 minutes the first time, ~5 minutes for repeat runs.

---

## What you're provisioning

| Resource | Spec | Free? |
|---|---|---|
| Compute instance | **Ampere ARM A1**, 4 OCPU, 24 GB RAM, Ubuntu 22.04 LTS | ✓ Always Free |
| Boot volume | 50 GB | ✓ Always Free |
| Public IPv4 | 1× reserved IP | ✓ Always Free |
| VCN + subnet | Default `/16` virtual cloud network | ✓ Always Free |
| Egress | 10 TB/month outbound | ✓ Always Free |

> ⚠️ **Don't pick** AMD/Intel shape (those have only 1 GB RAM free). The **Ampere ARM A1 4-core / 24 GB** is the generous one.

---

## Step 1.1 — Region + Tenancy

You should already see your "home region" when you log in to https://cloud.oracle.com/.

**Choose a region close to you** for low Telegram-alert latency. For Kenya, the best options are:
- `eu-frankfurt-1` (Germany) — ~120 ms RTT, most ARM capacity
- `me-jeddah-1` (Saudi Arabia) — ~60 ms RTT but smaller capacity pool
- `ap-mumbai-1` (India) — ~110 ms RTT

> 💡 **Ampere capacity tip**: ARM A1 is in high demand. If you get "Out of host capacity" when creating the VM, switch regions or retry in a few hours.

You can change your home region once. If you accidentally picked a far one, change it now in **Profile → Tenancy → Edit home region** (this triggers a 24h cooldown but is one-time).

---

## Step 1.2 — Generate an SSH key pair on your laptop

Open PowerShell on Windows:

```powershell
# Creates ~/.ssh/oci_xauusd and oci_xauusd.pub if they don't exist
ssh-keygen -t ed25519 -C "xauusd-vps" -f $HOME\.ssh\oci_xauusd -N '""'
```

Print the **public** key — you'll paste it into the OCI console next:

```powershell
Get-Content $HOME\.ssh\oci_xauusd.pub
```

> 🔐 The **private** key (`oci_xauusd` without `.pub`) stays on your laptop. Never share it.

---

## Step 1.3 — Create the VCN (Virtual Cloud Network)

In the OCI console:

1. ☰ **Hamburger menu → Networking → Virtual Cloud Networks**
2. Click **Start VCN Wizard**
3. Pick **"Create VCN with Internet Connectivity"** → **Start**
4. Fill in:
   - **VCN name**: `xauusd-vcn`
   - **Compartment**: `<your tenancy>` (root) — fine for now
   - Leave CIDR blocks at defaults: VCN `10.0.0.0/16`, public subnet `10.0.0.0/24`
5. **Next → Create**

Wait ~30 s for it to finish. You'll have:
- VCN `xauusd-vcn`
- Public subnet `Public Subnet-xauusd-vcn`
- Internet gateway + route table + default security list

---

## Step 1.4 — Open firewall ports (Security List)

The default security list opens port 22 only. You need to also open 80 and 443 for HTTPS and the dashboard.

1. **Networking → Virtual Cloud Networks → xauusd-vcn**
2. Under **Resources**, click **Security Lists** → **Default Security List for xauusd-vcn**
3. **Ingress Rules → Add Ingress Rules**, add these two rules:

| Source CIDR | IP Protocol | Source Port | Destination Port | Description |
|---|---|---|---|---|
| `0.0.0.0/0` | TCP | (all) | **80** | HTTP (Caddy auto-redirects to HTTPS) |
| `0.0.0.0/0` | TCP | (all) | **443** | HTTPS (dashboard + API) |

Port 22 (SSH) should already be open. **Do NOT** open 5173 or 8000 — Caddy fronts them on 443.

---

## Step 1.5 — Reserve a public IP

The auto-assigned ephemeral IP changes if you stop+start the VM. Reserve one:

1. **Networking → IP Management → Reserved Public IPs**
2. Click **Reserve Public IP Address**
3. **Name**: `xauusd-public-ip`
4. Compartment: same as VCN
5. **Reserve**

Copy the IP that appears — you'll attach it to the VM in the next step.

---

## Step 1.6 — Create the compute instance (the VM)

1. ☰ **Compute → Instances → Create instance**
2. **Name**: `xauusd-dashboard-vps`
3. **Compartment**: same as VCN
4. **Placement** → leave default Availability Domain
5. **Image and shape**:
   - Click **Edit** under Image and shape
   - Image: **Canonical Ubuntu 22.04** (the aarch64 / ARM64 variant)
   - Shape: click **Change shape** → **Ampere** → **VM.Standard.A1.Flex**
   - **OCPU**: 4
   - **Memory (GB)**: 24
   - These are the max Always Free amounts. Confirm the page shows the green "Always Free Eligible" badge.
6. **Primary VNIC**:
   - VCN: `xauusd-vcn`
   - Subnet: `Public Subnet-xauusd-vcn`
   - Public IPv4: **Assign a public IPv4 address** (we'll switch to reserved one after creation)
7. **Add SSH keys** → **Paste public keys** → paste the contents of `oci_xauusd.pub` from Step 1.2
8. **Boot volume** → leave default 50 GB
9. **Show advanced options** → expand → **Management** tab → **paste cloud-init script** (we'll fill this in Step 2 — for now leave blank or paste a placeholder echo)
10. **Create**

Wait ~3 minutes for it to provision. Status should reach **RUNNING**.

---

## Step 1.7 — Attach the reserved IP

Once the VM is running:

1. **Instance details → Networking → Primary VNIC** → click the VNIC name
2. **IPv4 Addresses** section → ⋮ next to the auto-assigned IP → **Edit**
3. **Public IP type** → **Reserved Public IP**
4. **Select existing reserved IP** → pick `xauusd-public-ip`
5. **Update**

The VM's public IP now persists across stop/start.

---

## Step 1.8 — Verify SSH connectivity

From PowerShell on your laptop:

```powershell
# Replace <YOUR_PUBLIC_IP> with the reserved IP from Step 1.5
ssh -i $HOME\.ssh\oci_xauusd ubuntu@<YOUR_PUBLIC_IP>
```

You should land at an `ubuntu@xauusd-dashboard-vps:~$` prompt. Type `exit` to disconnect.

**Add a shortcut to `~/.ssh/config`** so you can just type `ssh xauusd`:

```powershell
Add-Content $HOME\.ssh\config @"

Host xauusd
  HostName <YOUR_PUBLIC_IP>
  User ubuntu
  IdentityFile ~/.ssh/oci_xauusd
  ServerAliveInterval 60
"@
```

Now `ssh xauusd` from anywhere on your laptop drops you into the VM.

---

## Step 1.9 — Sanity-check the VM has what we need

SSH in and verify:

```bash
# Should show 4 CPUs and ~24 GB RAM
nproc
free -h

# Confirm aarch64 architecture (we'll need ARM-compatible Docker images)
uname -m   # → aarch64

# Confirm Ubuntu 22.04
lsb_release -a
```

---

## ✅ Infrastructure-ready checklist

Before moving on to Docker (Step 2), confirm:

- [ ] Compute instance `xauusd-dashboard-vps` is RUNNING
- [ ] You can `ssh xauusd` and reach a shell
- [ ] `nproc` shows 4, `free -h` shows ~24 GB
- [ ] `uname -m` returns `aarch64`
- [ ] Security list has ingress rules for **ports 22, 80, 443**
- [ ] Reserved public IP is attached (so it survives stop/start)
- [ ] You have a free **DuckDNS** subdomain (sign up at https://duckdns.org with GitHub/Google, pick something like `xauusd-anwar.duckdns.org`, copy the token they show you)

Now proceed to **`02-vm-bootstrap.md`** to install Docker, Caddy, firewall, and the DuckDNS auto-updater.

---

## Common gotchas

| Symptom | Cause | Fix |
|---|---|---|
| "Out of host capacity" when creating A1 VM | High demand for free ARM cores in your region | Wait an hour and retry, or switch to a less-busy region |
| SSH says "Permission denied (publickey)" | Wrong key or wrong username | Username is `ubuntu` (not root). Confirm `-i` points to the matching private key |
| `apt update` times out | Egress not routed | Confirm subnet has an Internet Gateway route (Step 1.3 should have set this up automatically) |
| HTTP timeout from outside | Security list closed | Re-check Step 1.4 — both 80 and 443 must be in ingress rules |
| Reserved IP says "in use" but you can't see where | Stale ephemeral assignment | Detach from old resource via Networking → Reserved Public IPs → ⋮ |
