#!/usr/bin/env bash
# ============================================================================
# Health watchdog — runs every 5 minutes via cron
# ============================================================================
# Pings the dashboard's /api/v1/health endpoint. If it fails 2 times in a row,
# fires a Telegram alert AND attempts an automatic docker compose restart.
#
# Install with sudo crontab -e:
#   */5 * * * * /opt/xauusd/deploy/watchdog.sh >> /var/log/xauusd-watchdog.log 2>&1
# ============================================================================

set -euo pipefail

STATE_FILE=/var/run/xauusd-watchdog.state
FQDN_FILE=/opt/xauusd/.fqdn
ENV_FILE=/opt/xauusd/deploy/.env.prod

[[ -f "$FQDN_FILE" ]] || { echo "no FQDN file"; exit 0; }
FQDN="$(cat "$FQDN_FILE")"
URL="https://${FQDN}/api/v1/health"

# Telegram helper
notify() {
  local msg="$1"
  if [[ -f "$ENV_FILE" ]]; then
    source <(grep -E '^(TELEGRAM_BOT_TOKEN|TELEGRAM_CHAT_ID|TELEGRAM_ALERTS_ENABLED)=' "$ENV_FILE" | sed 's/^/export /')
    if [[ "${TELEGRAM_ALERTS_ENABLED:-false}" = "true" && -n "${TELEGRAM_BOT_TOKEN:-}" && -n "${TELEGRAM_CHAT_ID:-}" ]]; then
      curl -s "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage" \
        -d "chat_id=${TELEGRAM_CHAT_ID}" \
        -d "parse_mode=HTML" \
        -d "text=${msg}" >/dev/null || true
    fi
  fi
}

# Current failure count (0 if no state file)
FAILS=$(cat "$STATE_FILE" 2>/dev/null || echo 0)

if curl -sf --max-time 10 "$URL" >/dev/null 2>&1; then
  if [[ "$FAILS" != "0" ]]; then
    notify "✅ <b>Dashboard recovered</b>%0A${FQDN} is back online."
    echo "[$(date -u +%FT%TZ)] recovered (was $FAILS fails)"
  fi
  echo 0 > "$STATE_FILE"
  exit 0
fi

# Failed — increment counter
FAILS=$((FAILS + 1))
echo "$FAILS" > "$STATE_FILE"
echo "[$(date -u +%FT%TZ)] health check FAILED (${FAILS} consecutive)"

# Alert + auto-restart after 2 consecutive failures (10 min)
if [[ "$FAILS" = "2" ]]; then
  notify "🚨 <b>Dashboard DOWN</b>%0A${URL} failed 2 health checks.%0AAttempting docker compose restart…"
  cd /opt/xauusd
  docker compose -f deploy/docker-compose.prod.yml --env-file "$ENV_FILE" restart || true
fi
if [[ "$FAILS" = "6" ]]; then
  notify "🚨 <b>Dashboard STILL DOWN</b>%0A30 min of failures. Manual intervention needed: SSH in and check 'docker compose logs'."
fi
