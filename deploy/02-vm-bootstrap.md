# VM Bootstrap — Docker, Caddy, Firewall, DuckDNS

> Prerequisite: You completed `01-oracle-cloud-provisioning.md` and can `ssh xauusd`.
>
> This step turns a bare Ubuntu 22.04 ARM VM into a hardened, Docker-ready
> host with HTTPS termination via Caddy and a free dynamic DNS hostname.
>
> **Time**: ~10 minutes (mostly waiting for apt + Docker download).

---

## Step 2.1 — Copy the bootstrap script up to the VM

From your laptop's PowerShell:

```powershell
cd C:\Users\anwar.mohamed\eurusd-dashboard
scp -i $HOME\.ssh\oci_xauusd deploy\bootstrap-vm.sh ubuntu@xauusd:~/bootstrap-vm.sh
```

(`xauusd` is the SSH alias you set up in Step 1.8.)

---

## Step 2.2 — Run it once

SSH in and execute:

```bash
ssh xauusd
chmod +x ~/bootstrap-vm.sh
sudo ~/bootstrap-vm.sh
```

The script is **idempotent** — safe to re-run if anything fails mid-way.

It will prompt you for:

1. **DuckDNS subdomain** — e.g. `xauusd-anwar` (the prefix you registered at duckdns.org, without the `.duckdns.org` suffix)
2. **DuckDNS token** — the 36-char UUID shown on your duckdns.org page
3. **Your email** for Let's Encrypt cert renewal warnings (Caddy uses it)

After it finishes (~5 min) you should see:

```
[bootstrap] ✓ Docker installed
[bootstrap] ✓ Caddy installed
[bootstrap] ✓ UFW firewall configured (22, 80, 443 open)
[bootstrap] ✓ Swap configured (4 GB)
[bootstrap] ✓ DuckDNS auto-updater armed (every 5 min via systemd timer)
[bootstrap] ✓ Unattended security upgrades enabled
[bootstrap] All systems green. Ready for Docker deployment.
```

---

## Step 2.3 — Verify each component

```bash
# Docker installed and your user can run it without sudo
docker version
docker compose version

# Caddy listening (will show 0 servers until we deploy compose)
sudo systemctl status caddy

# Firewall: only 22 (SSH), 80, 443 should be open
sudo ufw status verbose

# Swap is active (helps with the 24 GB free tier — Caddy + Python eat RAM during compiles)
free -h

# DuckDNS A-record points to your reserved public IP
dig +short xauusd-anwar.duckdns.org   # → your VPS public IP
```

Try opening **http://xauusd-anwar.duckdns.org/** in a browser:
- You should see Caddy's default response (or a 502 — that's fine, the backend isn't deployed yet)
- Caddy is already attempting Let's Encrypt — once HTTPS provisions (~30 s), `https://` works too

---

## Step 2.4 — Reboot once to verify everything auto-starts

```bash
sudo reboot
# wait 60 s
ssh xauusd
docker ps                          # should be empty but daemon up
sudo systemctl status caddy        # active
sudo systemctl status duckdns      # active (5-min timer)
```

If any of those failed to come back, see the troubleshooting table at the bottom.

---

## ✅ VM bootstrap complete checklist

- [ ] `docker version` returns a version string without sudo
- [ ] `docker compose version` works (Compose v2 plugin)
- [ ] `sudo ufw status` shows ports 22/tcp, 80/tcp, 443/tcp ALLOW
- [ ] `free -h` shows 4 GB swap active
- [ ] `dig +short <your>.duckdns.org` returns the VM's public IP
- [ ] Browser can reach `http://<your>.duckdns.org/` (Caddy responds even with no backend)
- [ ] Both Caddy + DuckDNS survive a `sudo reboot`

Now proceed to **`03-docker-deploy.md`** to ship the actual dashboard.

---

## What the bootstrap script does (in detail)

If you want to read the script before running it, it lives at `deploy/bootstrap-vm.sh`. In summary it:

1. Updates apt index and upgrades any pending security patches
2. Installs Docker CE + Compose v2 plugin from the official Docker apt repo
3. Adds the `ubuntu` user to the `docker` group (so you don't need sudo for docker)
4. Installs Caddy from the official Cloudsmith apt repo
5. Configures UFW: deny incoming by default, allow OpenSSH/80/443
6. Creates a 4 GB swap file (free tier ARM machines have 24 GB RAM but compiles + caching benefit from swap)
7. Writes `/etc/systemd/system/duckdns.service` + `duckdns.timer` that ping DuckDNS every 5 minutes with your reserved IP
8. Enables `unattended-upgrades` for automatic security patches
9. Creates `/opt/xauusd/` as the app directory with sane permissions

---

## Common issues

| Symptom | Fix |
|---|---|
| `docker: command not found` after bootstrap | Log out and back in (group changes take effect on new sessions). Or `newgrp docker` |
| Caddy fails to obtain Let's Encrypt cert | Confirm port 80 is open (Step 1.4) and DuckDNS resolves to your IP. Run `sudo journalctl -u caddy -n 100` for details |
| DuckDNS A-record doesn't update | Run `sudo systemctl start duckdns` manually, then `journalctl -u duckdns -n 20`. Most common cause is a typo in token |
| UFW blocks Docker network | Don't run `ufw enable` with `--force` from another script — use the bootstrap script which correctly orders Docker + UFW init |
| Reboot leaves Docker / Caddy stopped | `sudo systemctl enable docker caddy duckdns.timer` (the bootstrap does this; if you ran a partial script, re-run the full one) |
