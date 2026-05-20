# VPS Provider Comparison — Pick Your Host

You don't need Oracle Cloud — any Linux VPS with ≥ 2 GB RAM, public IPv4, and Ubuntu 22.04 works. The bootstrap script and deploy kit are provider-agnostic. Only the **provisioning** step (Phase 1) differs.

## Recommended providers (ranked for our use case)

| Rank | Provider | Plan | Price | RAM | CPU | Disk | Best Region | Provisioning Guide |
|---|---|---|---|---|---|---|---|---|
| 🥇 | **Hetzner Cloud** | CX22 | **€4.59/mo** (~$5) | 4 GB | 2 vCPU AMD | 40 GB SSD | Nuremberg/Falkenstein DE | `01b-hetzner-cloud-provisioning.md` |
| 🥈 | **Oracle Cloud** | A1.Flex Always Free | **$0/mo** | 24 GB | 4 OCPU ARM | 50 GB | EU-Frankfurt | `01-oracle-cloud-provisioning.md` |
| 🥉 | **Vultr** | High-Freq 1G | $6/mo | 1 GB | 1 vCPU | 32 GB NVMe | **Johannesburg ZA** | TBD (very similar to Hetzner) |
| | **DigitalOcean** | Basic Droplet | $6/mo | 1 GB | 1 vCPU | 25 GB SSD | Bangalore IN | DO has its own quickstart |
| | **Linode (Akamai)** | Nanode 1GB | $5/mo | 1 GB | 1 vCPU | 25 GB SSD | Singapore | similar to Hetzner |
| | **Contabo** | Cloud VPS 10 | €4.50/mo | **6 GB** | 3 vCPU | 100 GB NVMe | Germany/Singapore | budget pick |
| | **Google Cloud** | e2-micro Always Free | $0 | 1 GB | 0.25–1 vCPU shared | 30 GB | us-west1 | smaller, free forever |
| | **Scaleway** | DEV1-S | €3.20/mo | 2 GB | 2 vCPU | 20 GB | Paris/Amsterdam | EU only |
| | **Netcup** | VPS 200 G11 | €3.25/mo | 4 GB | 2 vCPU | 40 GB SSD | Germany | cheap, less polished UI |

## Decision matrix — pick by what matters most to YOU

### "I want the absolute cheapest 24/7 host"
→ **Oracle Cloud Always Free** (literally $0)
→ Fallback if OCI capacity unavailable: **Netcup VPS 200 G11** (€3.25/mo)

### "I want zero hassle, best engineered platform, $5 is fine"
→ **Hetzner Cloud CX22** ⭐
- Best RAM/€ ratio of any mainstream provider
- 3-minute provisioning, clean UI
- 4 GB RAM means no OOM headaches with our stack

### "I'm in Kenya and care about UI snappiness"
→ **Vultr Cloud Compute — Johannesburg**
- Only major provider with an Africa POP
- ~30 ms RTT to Nairobi vs 130-200 ms from EU
- Trade-off: 1 GB RAM at $6/mo (need to disable heavy backtest features)

### "I want maximum reliability + best docs (worth $1 more)"
→ **DigitalOcean Basic Droplet — Bangalore**
- Mature ecosystem, every tutorial exists
- Reliable network, fewer surprises
- Bangalore is closest to Kenya among DO regions (~110 ms)

### "I plan to scale to multiple instruments / heavy backtests"
→ **Contabo Cloud VPS 10** at €4.50/mo
- 6 GB RAM, 100 GB SSD — way more than competitors at this price
- Trade-off: shared host, slower disk IOPS, less consistent CPU performance

### "I already use AWS / GCP / Azure"
→ Use your existing cloud — but **none of them have a great free 24/7 tier**:
- AWS Lightsail: $3.50/mo for 512 MB (too small)
- GCP e2-micro Always Free: 1 GB shared, US-only (works if slimmed)
- Azure: 12-month free trial only, then expensive

## What's identical across all providers

Once the VM is up with SSH, the rest is the same:

```bash
# 1. Bootstrap (installs Docker, Caddy, UFW, DuckDNS, swap)
chmod +x bootstrap-vm.sh
sudo ./bootstrap-vm.sh

# 2. Upload source (from your laptop)
rsync -avz ... eurusd-dashboard/ <user>@<vps>:/opt/xauusd/

# 3. Configure secrets
cp deploy/.env.prod.example deploy/.env.prod
nano deploy/.env.prod

# 4. Deploy
sudo bash /opt/xauusd/deploy/deploy.sh

# 5. Install monitoring
sudo bash /opt/xauusd/deploy/install-cron.sh
```

The Caddyfile, docker-compose.prod.yml, MT5 bridge daemon, backup, and watchdog scripts work on any of these providers without modification.

## Quick-start: I just want a recommendation

If you haven't started OCI provisioning yet and want the fastest path:

1. **Hetzner Cloud CX22 in Falkenstein DE** (~$5/mo)
2. Follow `01b-hetzner-cloud-provisioning.md`
3. Then `02-vm-bootstrap.md` and everything in `README.md` from Phase 3 onward

You'll be live in **15-20 minutes** instead of OCI's 50.

If $0 is non-negotiable, stick with OCI but be patient with the A1.Flex capacity availability (often need to retry 2-3 times across regions/hours of day).
