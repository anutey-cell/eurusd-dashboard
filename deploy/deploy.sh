#!/usr/bin/env bash
# ============================================================================
# XAU/USD Dashboard — VPS deployment runner
# ============================================================================
# Run this from /opt/xauusd on the VPS after `rsync` has uploaded the source.
#
#   cd /opt/xauusd
#   sudo bash deploy/deploy.sh
#
# What it does:
#   1. Validates .env.prod exists and required keys are set
#   2. Reads FQDN from /opt/xauusd/.fqdn (set by bootstrap)
#   3. Writes /etc/caddy/Caddyfile + reloads Caddy
#   4. Builds + starts docker compose stack
#   5. Waits for backend health, then frontend
#   6. Prints the public URL
#
# Re-running deploys updates: just `git pull` (or re-rsync) then run this.
# ============================================================================

set -euo pipefail

log()  { printf "\033[1;34m[deploy]\033[0m %s\n" "$*"; }
ok()   { printf "\033[1;32m[deploy]\033[0m ✓ %s\n" "$*"; }
warn() { printf "\033[1;33m[deploy]\033[0m ! %s\n" "$*" >&2; }
die()  { printf "\033[1;31m[deploy]\033[0m ✗ %s\n" "$*" >&2; exit 1; }

# ── Sanity checks ────────────────────────────────────────────────────────────
[[ $EUID -eq 0 ]] || die "Run as root (sudo $0)"
[[ -d /opt/xauusd ]] || die "Expected app dir /opt/xauusd. Did bootstrap-vm.sh run?"
[[ -f /opt/xauusd/.fqdn ]] || die "Missing /opt/xauusd/.fqdn — re-run bootstrap-vm.sh"

cd /opt/xauusd
FQDN="$(cat /opt/xauusd/.fqdn)"
log "FQDN: $FQDN"

# ── Validate .env.prod ───────────────────────────────────────────────────────
ENV_FILE="deploy/.env.prod"
[[ -f "$ENV_FILE" ]] || die "Missing $ENV_FILE. Copy deploy/.env.prod.example and fill in values."

require_env_var() {
  local key="$1"
  if ! grep -E "^${key}=[^[:space:]]+" "$ENV_FILE" >/dev/null 2>&1; then
    die "$ENV_FILE missing required key: $key"
  fi
}
require_env_var "DATA_MODE"
require_env_var "TELEGRAM_BOT_TOKEN"
require_env_var "TELEGRAM_CHAT_ID"
require_env_var "MT5_BRIDGE_SHARED_SECRET"
require_env_var "CORS_ORIGINS"

# Make sure FQDN in env matches the one we set during bootstrap
ENV_FQDN="$(grep -E '^FQDN=' "$ENV_FILE" | head -n1 | cut -d= -f2 | tr -d '"')"
if [[ -z "$ENV_FQDN" ]]; then
  warn "FQDN not set in .env.prod — adding it from /opt/xauusd/.fqdn"
  echo "FQDN=$FQDN" >> "$ENV_FILE"
elif [[ "$ENV_FQDN" != "$FQDN" ]]; then
  warn "FQDN mismatch: .env.prod has '$ENV_FQDN', bootstrap set '$FQDN'"
  warn "Using $ENV_FQDN (you might have changed it intentionally)"
  FQDN="$ENV_FQDN"
fi
ok ".env.prod validated"

# ── Install Caddyfile ────────────────────────────────────────────────────────
log "Installing Caddy config…"
# Substitute {$FQDN} placeholder so we don't need Caddy global env-var setup
sed "s/{\$FQDN}/${FQDN}/g" deploy/Caddyfile > /etc/caddy/Caddyfile
caddy validate --config /etc/caddy/Caddyfile >/dev/null 2>&1 \
  || die "Caddyfile failed validation. Review deploy/Caddyfile."
systemctl reload caddy || systemctl restart caddy
ok "Caddy reloaded with FQDN=$FQDN"

# ── Build + start the stack ──────────────────────────────────────────────────
log "Pulling latest images from Docker Hub (no local build)…"
docker compose -f deploy/docker-compose.prod.yml --env-file "$ENV_FILE" pull

log "Starting containers…"
docker compose -f deploy/docker-compose.prod.yml --env-file "$ENV_FILE" up -d --remove-orphans

# ── Wait for backend health ──────────────────────────────────────────────────
log "Waiting for backend health (up to 90s)…"
DEADLINE=$(($(date +%s) + 90))
while true; do
  if docker exec xauusd-backend python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/api/v1/health')" 2>/dev/null; then
    ok "Backend healthy"
    break
  fi
  if [[ $(date +%s) -gt $DEADLINE ]]; then
    docker compose -f deploy/docker-compose.prod.yml logs --tail=80 backend
    die "Backend failed to come up within 90s"
  fi
  sleep 3
done

# ── Wait for frontend ────────────────────────────────────────────────────────
log "Waiting for frontend (up to 60s)…"
DEADLINE=$(($(date +%s) + 60))
while true; do
  if curl -sf -o /dev/null http://localhost:5173/ 2>/dev/null || \
     docker exec xauusd-frontend wget -qO- http://localhost:5173/ >/dev/null 2>&1; then
    ok "Frontend up"
    break
  fi
  if [[ $(date +%s) -gt $DEADLINE ]]; then
    docker compose -f deploy/docker-compose.prod.yml logs --tail=60 frontend
    die "Frontend failed to come up within 60s"
  fi
  sleep 3
done

# ── Final status ─────────────────────────────────────────────────────────────
echo
echo "============================================================"
echo "  Deployment complete"
echo "============================================================"
ok "Public URL:   https://$FQDN"
ok "API health:   https://$FQDN/api/v1/health"
ok "Swagger UI:   https://$FQDN/docs"
echo
docker compose -f deploy/docker-compose.prod.yml --env-file "$ENV_FILE" ps
echo
echo "Next steps:"
echo "  1. Open https://$FQDN in a browser — confirm dashboard loads with HTTPS"
echo "  2. On your Windows laptop, run the MT5 bridge daemon:"
echo "       python deploy/mt5_bridge_daemon.py"
echo "  3. Set up daily backup cron (already configured):"
echo "       crontab -l | grep xauusd"
