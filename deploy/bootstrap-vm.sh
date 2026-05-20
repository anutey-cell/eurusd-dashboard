#!/usr/bin/env bash
# ============================================================================
# XAU/USD Dashboard — Oracle Cloud VM Bootstrap
# ============================================================================
# One-shot, idempotent bootstrap for a fresh Ubuntu 22.04 LTS ARM64 VM.
# Installs Docker + Caddy + UFW + DuckDNS auto-updater + swap + auto-upgrades.
#
# Usage:
#   chmod +x bootstrap-vm.sh
#   sudo ./bootstrap-vm.sh
#
# Re-running is safe; every step checks for existing state.
# ============================================================================

set -euo pipefail

# ── Pretty logging ───────────────────────────────────────────────────────────
log()  { printf "\033[1;34m[bootstrap]\033[0m %s\n" "$*"; }
ok()   { printf "\033[1;32m[bootstrap]\033[0m ✓ %s\n" "$*"; }
warn() { printf "\033[1;33m[bootstrap]\033[0m ! %s\n" "$*" >&2; }
die()  { printf "\033[1;31m[bootstrap]\033[0m ✗ %s\n" "$*" >&2; exit 1; }

# ── Sanity ───────────────────────────────────────────────────────────────────
[[ $EUID -eq 0 ]] || die "Run as root (sudo $0)"
[[ "$(lsb_release -is 2>/dev/null)" = "Ubuntu" ]] || die "Ubuntu only"

ARCH="$(uname -m)"
log "Architecture: $ARCH"
[[ "$ARCH" = "aarch64" || "$ARCH" = "x86_64" ]] || die "Unsupported arch $ARCH"

# ── Interactive prompts (skip if env vars already set) ───────────────────────
DUCKDNS_SUBDOMAIN="${DUCKDNS_SUBDOMAIN:-}"
DUCKDNS_TOKEN="${DUCKDNS_TOKEN:-}"
ADMIN_EMAIL="${ADMIN_EMAIL:-}"

if [[ -z "$DUCKDNS_SUBDOMAIN" ]]; then
  read -rp "DuckDNS subdomain (e.g. 'xauusd-anwar' without '.duckdns.org'): " DUCKDNS_SUBDOMAIN
fi
if [[ -z "$DUCKDNS_TOKEN" ]]; then
  read -rp "DuckDNS token (36-char UUID): " DUCKDNS_TOKEN
fi
if [[ -z "$ADMIN_EMAIL" ]]; then
  read -rp "Email for Let's Encrypt notifications: " ADMIN_EMAIL
fi

[[ -n "$DUCKDNS_SUBDOMAIN" && -n "$DUCKDNS_TOKEN" && -n "$ADMIN_EMAIL" ]] \
  || die "All three values required"

FQDN="${DUCKDNS_SUBDOMAIN}.duckdns.org"
log "Will configure VM as: $FQDN"

# ── 1. apt update + base tooling ─────────────────────────────────────────────
log "Updating apt index and installing base tools…"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq \
  ca-certificates curl gnupg lsb-release \
  ufw fail2ban unattended-upgrades \
  jq dnsutils tmux htop ncdu

# ── 2. Docker CE + Compose v2 plugin ─────────────────────────────────────────
if ! command -v docker >/dev/null 2>&1; then
  log "Installing Docker CE…"
  install -m 0755 -d /etc/apt/keyrings
  curl -fsSL https://download.docker.com/linux/ubuntu/gpg | \
    gpg --dearmor -o /etc/apt/keyrings/docker.gpg
  chmod a+r /etc/apt/keyrings/docker.gpg
  echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] \
https://download.docker.com/linux/ubuntu $(lsb_release -cs) stable" \
    > /etc/apt/sources.list.d/docker.list
  apt-get update -qq
  apt-get install -y -qq docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
else
  log "Docker already installed: $(docker --version)"
fi

# Add the invoking user (ubuntu) to docker group
TARGET_USER="${SUDO_USER:-ubuntu}"
if ! id -nG "$TARGET_USER" | grep -qw docker; then
  usermod -aG docker "$TARGET_USER"
  log "Added $TARGET_USER to docker group (effective next login)"
fi
systemctl enable --now docker
ok "Docker installed"

# ── 3. Caddy reverse proxy with auto-HTTPS ──────────────────────────────────
if ! command -v caddy >/dev/null 2>&1; then
  log "Installing Caddy from Cloudsmith…"
  curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' | \
    gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
  curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' \
    > /etc/apt/sources.list.d/caddy-stable.list
  apt-get update -qq
  apt-get install -y -qq caddy
else
  log "Caddy already installed: $(caddy version)"
fi

systemctl enable caddy
ok "Caddy installed"

# ── 4. UFW firewall ──────────────────────────────────────────────────────────
log "Configuring UFW firewall…"
# Important: configure UFW before "ufw enable" so existing SSH session isn't dropped
ufw --force default deny incoming
ufw --force default allow outgoing
ufw allow OpenSSH                 # 22/tcp
ufw allow 80/tcp                  # HTTP (Caddy → redirect to 443)
ufw allow 443/tcp                 # HTTPS
# Don't open 8000/5173 — Caddy proxies them on localhost
ufw --force enable
ok "UFW firewall configured (22, 80, 443 open)"

# ── 5. Swap file (4 GB) ──────────────────────────────────────────────────────
if [[ ! -f /swapfile ]]; then
  log "Creating 4 GB swap file…"
  fallocate -l 4G /swapfile
  chmod 600 /swapfile
  mkswap /swapfile >/dev/null
  swapon /swapfile
  grep -q '^/swapfile' /etc/fstab || echo '/swapfile none swap sw 0 0' >> /etc/fstab
  # Reduce swappiness — we have plenty of RAM, only swap under real pressure
  sysctl -w vm.swappiness=10 >/dev/null
  grep -q '^vm.swappiness' /etc/sysctl.conf || echo 'vm.swappiness=10' >> /etc/sysctl.conf
fi
ok "Swap configured (4 GB)"

# ── 6. DuckDNS auto-updater (systemd timer, every 5 min) ────────────────────
log "Setting up DuckDNS auto-updater…"
install -d -m 700 /opt/duckdns
cat > /opt/duckdns/update.sh <<EOF
#!/usr/bin/env bash
set -euo pipefail
echo url="https://www.duckdns.org/update?domains=$DUCKDNS_SUBDOMAIN&token=$DUCKDNS_TOKEN&ip=" \\
  | curl -k -s -K - >> /var/log/duckdns.log
EOF
chmod +x /opt/duckdns/update.sh
touch /var/log/duckdns.log
chown root:root /var/log/duckdns.log

cat > /etc/systemd/system/duckdns.service <<EOF
[Unit]
Description=DuckDNS dynamic-DNS updater (one-shot)
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
ExecStart=/opt/duckdns/update.sh
EOF

cat > /etc/systemd/system/duckdns.timer <<EOF
[Unit]
Description=Run DuckDNS updater every 5 minutes

[Timer]
OnBootSec=1min
OnUnitActiveSec=5min
Unit=duckdns.service

[Install]
WantedBy=timers.target
EOF

systemctl daemon-reload
systemctl enable --now duckdns.timer
# Run once immediately so the record is up to date
/opt/duckdns/update.sh
RESP="$(tail -n1 /var/log/duckdns.log 2>/dev/null || echo "")"
if [[ "$RESP" = "OK" ]]; then
  ok "DuckDNS auto-updater armed (every 5 min via systemd timer)"
else
  warn "DuckDNS update response: '$RESP' — check subdomain/token, then retry"
fi

# ── 7. Unattended security upgrades ─────────────────────────────────────────
log "Enabling unattended security upgrades…"
dpkg-reconfigure -plow unattended-upgrades </dev/null || true
cat > /etc/apt/apt.conf.d/20auto-upgrades <<EOF
APT::Periodic::Update-Package-Lists "1";
APT::Periodic::Unattended-Upgrade "1";
APT::Periodic::AutocleanInterval "7";
EOF
ok "Unattended security upgrades enabled"

# ── 8. App directory + admin email file ─────────────────────────────────────
install -d -m 755 -o "$TARGET_USER" -g "$TARGET_USER" /opt/xauusd
install -d -m 755 -o "$TARGET_USER" -g "$TARGET_USER" /opt/xauusd/backups
echo "$ADMIN_EMAIL" > /opt/xauusd/.admin-email
echo "$FQDN"        > /opt/xauusd/.fqdn
chmod 644 /opt/xauusd/.admin-email /opt/xauusd/.fqdn

# ── Summary ─────────────────────────────────────────────────────────────────
echo
echo "============================================================"
echo "  Bootstrap complete — VM is Docker-ready."
echo "============================================================"
ok "Docker installed"
ok "Caddy installed"
ok "UFW firewall configured (22, 80, 443 open)"
ok "Swap configured (4 GB)"
ok "DuckDNS auto-updater armed (every 5 min via systemd timer)"
ok "Unattended security upgrades enabled"
echo
echo "FQDN:        $FQDN"
echo "App dir:     /opt/xauusd"
echo "Admin email: $ADMIN_EMAIL"
echo
echo "Next: copy your source tree to /opt/xauusd and follow 03-docker-deploy.md"
echo "  rsync -avz -e 'ssh -i ~/.ssh/oci_xauusd' \\"
echo "    /local/path/eurusd-dashboard/ ubuntu@xauusd:/opt/xauusd/"
echo
