#!/bin/bash
# Phase 1/2 research scheduler — systemd units on the droplet.
# All units run under root and only exec:
#   docker exec xauusd-backend python /app/<script>
# nothing more.
set -euo pipefail

echo "==> installing systemd units under /etc/systemd/system/gold-intel-*.{service,timer}"

# ── CFTC weekly (Sat 03:00 UTC — cushion for Fri publication) ────────────
cat > /etc/systemd/system/gold-intel-cftc.service <<'UNIT'
[Unit]
Description=Gold Intel — CFTC Disaggregated COT weekly refresh
After=docker.service
[Service]
Type=oneshot
ExecStart=/usr/bin/docker exec xauusd-backend python /app/research/gold_intel/cftc_backfill.py
StandardOutput=journal
StandardError=journal
UNIT
cat > /etc/systemd/system/gold-intel-cftc.timer <<'UNIT'
[Unit]
Description=Gold Intel — CFTC weekly timer
[Timer]
OnCalendar=Sat *-*-* 03:00:00 UTC
Persistent=true
[Install]
WantedBy=timers.target
UNIT

# ── CFTC derived + distribution (Sat 03:15 UTC — 15 min after raw) ───────
cat > /etc/systemd/system/gold-intel-cftc-derived.service <<'UNIT'
[Unit]
Description=Gold Intel — CFTC derived + distribution
After=gold-intel-cftc.service
[Service]
Type=oneshot
ExecStart=/usr/bin/docker exec xauusd-backend python /app/research/gold_intel/cftc_derived.py
StandardOutput=journal
StandardError=journal
UNIT
cat > /etc/systemd/system/gold-intel-cftc-derived.timer <<'UNIT'
[Unit]
Description=Gold Intel — CFTC derived timer
[Timer]
OnCalendar=Sat *-*-* 03:15:00 UTC
Persistent=true
[Install]
WantedBy=timers.target
UNIT

# ── Macro (daily 21:30 UTC — after US EOD close) ─────────────────────────
cat > /etc/systemd/system/gold-intel-macro.service <<'UNIT'
[Unit]
Description=Gold Intel — Macro (Treasury + TradingView) daily refresh
After=docker.service
[Service]
Type=oneshot
ExecStart=/usr/bin/docker exec xauusd-backend python /app/research/gold_intel/macro_backfill.py
StandardOutput=journal
StandardError=journal
UNIT
cat > /etc/systemd/system/gold-intel-macro.timer <<'UNIT'
[Unit]
Description=Gold Intel — Macro daily timer
[Timer]
OnCalendar=*-*-* 21:30:00 UTC
Persistent=true
[Install]
WantedBy=timers.target
UNIT

# ── Event calendar (every 2 hours) ──────────────────────────────────────
cat > /etc/systemd/system/gold-intel-events.service <<'UNIT'
[Unit]
Description=Gold Intel — ForexFactory calendar refresh
After=docker.service
[Service]
Type=oneshot
ExecStart=/usr/bin/docker exec xauusd-backend python /app/research/gold_intel/pipeline_events.py
StandardOutput=journal
StandardError=journal
UNIT
cat > /etc/systemd/system/gold-intel-events.timer <<'UNIT'
[Unit]
Description=Gold Intel — Events every 2h
[Timer]
OnCalendar=*-*-* 00/2:05:00 UTC
Persistent=true
[Install]
WantedBy=timers.target
UNIT

# ── GC futures ref (every 30 min during US session) ─────────────────────
cat > /etc/systemd/system/gold-intel-gc.service <<'UNIT'
[Unit]
Description=Gold Intel — GC futures ref + basis observations
After=docker.service
[Service]
Type=oneshot
ExecStart=/usr/bin/docker exec xauusd-backend python /app/research/gold_intel/pipeline_gc.py
StandardOutput=journal
StandardError=journal
UNIT
cat > /etc/systemd/system/gold-intel-gc.timer <<'UNIT'
[Unit]
Description=Gold Intel — GC futures every 30 min
[Timer]
OnCalendar=*-*-* *:00,30:00 UTC
Persistent=true
[Install]
WantedBy=timers.target
UNIT

# ── CBOE GLD delayed options (daily 22:15 UTC — after US close) ─────────
cat > /etc/systemd/system/gold-intel-options.service <<'UNIT'
[Unit]
Description=Gold Intel — CBOE GLD delayed options (research proxy)
After=docker.service
[Service]
Type=oneshot
ExecStart=/usr/bin/docker exec xauusd-backend python /app/research/gold_intel/pipeline_options.py
StandardOutput=journal
StandardError=journal
UNIT
cat > /etc/systemd/system/gold-intel-options.timer <<'UNIT'
[Unit]
Description=Gold Intel — CBOE GLD daily options timer
[Timer]
OnCalendar=*-*-* 22:15:00 UTC
Persistent=true
[Install]
WantedBy=timers.target
UNIT

# ── Snapshot (daily 22:30 UTC — after all upstreams) ────────────────────
cat > /etc/systemd/system/gold-intel-snapshot.service <<'UNIT'
[Unit]
Description=Gold Intel — Daily snapshot v1.2
After=gold-intel-macro.timer gold-intel-options.timer
[Service]
Type=oneshot
ExecStart=/usr/bin/docker exec xauusd-backend python /app/research/gold_intel/snapshot_v12.py
StandardOutput=journal
StandardError=journal
UNIT
cat > /etc/systemd/system/gold-intel-snapshot.timer <<'UNIT'
[Unit]
Description=Gold Intel — Daily snapshot timer
[Timer]
OnCalendar=*-*-* 22:30:00 UTC
Persistent=true
[Install]
WantedBy=timers.target
UNIT

systemctl daemon-reload
for t in gold-intel-cftc.timer gold-intel-cftc-derived.timer gold-intel-macro.timer \
         gold-intel-events.timer gold-intel-gc.timer gold-intel-options.timer \
         gold-intel-snapshot.timer; do
    systemctl enable "$t"
    systemctl start "$t"
done

echo "==> installed. Listing timers:"
systemctl list-timers 'gold-intel-*' --no-pager
