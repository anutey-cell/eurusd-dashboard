#!/usr/bin/env bash
# ============================================================================
# Install cron entries for backup + watchdog
# ============================================================================
# Run once after deploy.sh:
#   sudo bash /opt/xauusd/deploy/install-cron.sh
# ============================================================================

set -euo pipefail

CRON_TAG='# xauusd-dashboard'

# Build the cron block
CRON_BLOCK="$(cat <<EOF
$CRON_TAG (auto-installed; do not edit by hand)
0 2 * * * /opt/xauusd/deploy/backup.sh    >> /var/log/xauusd-backup.log 2>&1
*/5 * * * * /opt/xauusd/deploy/watchdog.sh >> /var/log/xauusd-watchdog.log 2>&1
$CRON_TAG end
EOF
)"

# Strip any previous block, then append the new one
TMP="$(mktemp)"
crontab -l 2>/dev/null \
  | awk -v tag="$CRON_TAG" '
      $0 ~ tag" \\(auto" { skip = 1; next }
      $0 ~ tag" end"     { skip = 0; next }
      !skip
    ' > "$TMP" || true
printf "\n%s\n" "$CRON_BLOCK" >> "$TMP"
crontab "$TMP"
rm -f "$TMP"

echo "Installed cron entries:"
crontab -l | grep -E 'xauusd|backup|watchdog' || true
