#!/usr/bin/env bash
# ============================================================================
# Daily SQLite backup — runs from cron at 02:00 UTC (05:00 EAT)
# ============================================================================
# Install once with:
#   sudo crontab -e
# Then add this line:
#   0 2 * * * /opt/xauusd/deploy/backup.sh >> /var/log/xauusd-backup.log 2>&1
#
# Keeps the last 30 daily backups. Gzipped, ~50-200 KB each.
# Files land in /opt/xauusd/backups/.
# ============================================================================

set -euo pipefail

BACKUP_DIR=/opt/xauusd/backups
DB_PATH=/opt/xauusd/data/eurusd_signals.db
RETAIN_DAYS=30

mkdir -p "$BACKUP_DIR"
STAMP="$(date -u +%Y%m%d-%H%M%S)"
TARGET="$BACKUP_DIR/eurusd_signals-${STAMP}.db.gz"

if [[ ! -f "$DB_PATH" ]]; then
  echo "[backup] $DB_PATH missing — nothing to back up" >&2
  exit 0
fi

# Use sqlite3 .backup for a consistent snapshot even if the backend is writing
if command -v sqlite3 >/dev/null 2>&1; then
  sqlite3 "$DB_PATH" ".backup '$BACKUP_DIR/staging.db'"
  gzip -9 -c "$BACKUP_DIR/staging.db" > "$TARGET"
  rm -f "$BACKUP_DIR/staging.db"
else
  # Fall back to a plain copy + gzip (slight risk of inconsistent snapshot)
  gzip -9 -c "$DB_PATH" > "$TARGET"
fi

# Prune anything older than RETAIN_DAYS days
find "$BACKUP_DIR" -name 'eurusd_signals-*.db.gz' -mtime "+$RETAIN_DAYS" -delete

SIZE="$(du -h "$TARGET" | cut -f1)"
echo "[$(date -u +%FT%TZ)] backup OK: $TARGET ($SIZE)"

# Optional: Telegram notification on backup
if [[ -f /opt/xauusd/deploy/.env.prod ]]; then
  source <(grep -E '^(TELEGRAM_BOT_TOKEN|TELEGRAM_CHAT_ID|TELEGRAM_ALERTS_ENABLED)=' /opt/xauusd/deploy/.env.prod | sed 's/^/export /')
  if [[ "${TELEGRAM_ALERTS_ENABLED:-false}" = "true" && -n "${TELEGRAM_BOT_TOKEN:-}" && -n "${TELEGRAM_CHAT_ID:-}" ]]; then
    curl -s "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage" \
      -d "chat_id=${TELEGRAM_CHAT_ID}" \
      -d "text=📦 SQLite backup OK · ${SIZE} · ${STAMP}" >/dev/null || true
  fi
fi
