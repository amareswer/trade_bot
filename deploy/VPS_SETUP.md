# VPS Setup Guide — Trade Bot

Provider-agnostic instructions for deploying on any Linux VPS.
Oracle Cloud ARM (aarch64 / Ampere A1) specifics noted inline.

---

## Prerequisites

| Item | Requirement |
|------|-------------|
| OS | Ubuntu 22.04 LTS (any amd64 or aarch64) |
| RAM | 1 GB minimum (512 MB free recommended) |
| Disk | 2 GB free (logs, venv, code) |
| Python | 3.10+ (in `apt` as `python3.10` or later) |
| Outbound internet | Port 443 to Kraken, Telegram |
| Inbound ports | **None required** — bot is outbound-only |

> **ARM / Oracle Cloud Ampere A1:** `ccxt` is pure Python — no C extensions, no native compilation needed. All `pip install` steps work identically on aarch64.

---

## Oracle Cloud — Firewall Note

Oracle Cloud wraps the OS-level firewall (`iptables`/`ufw`) with a separate **Security List** in the VNC console. Even if `ufw` is open, inbound ports blocked in the Security List will not reach the instance.

The bot needs **no inbound ports** (it connects outbound to Kraken and Telegram). No Security List changes are required. If you add a web dashboard later, open port 8080 in both `ufw` and the Security List.

---

## One-Time Setup

### 1. Provision the instance

Oracle Cloud Free Tier: Create > Compute > Instance  
Shape: VM.Standard.A1.Flex (ARM), 1 OCPU, 6 GB RAM  
OS: Ubuntu 22.04 Minimal  
SSH key: paste your public key

### 2. SSH in and harden

```bash
ssh ubuntu@<your-vps-ip>

# Create a non-root user if needed (skip if already ubuntu)
sudo adduser trade
sudo usermod -aG sudo trade

# Disable password SSH (keys only)
sudo sed -i 's/PasswordAuthentication yes/PasswordAuthentication no/' /etc/ssh/sshd_config
sudo systemctl restart sshd
```

### 3. Copy the repo

```bash
# From your local machine:
rsync -av --exclude='.git' --exclude='venv' --exclude='__pycache__' \
  /path/to/trade_bot/ ubuntu@<vps-ip>:/tmp/trade_bot_src/

# Or clone directly on the VPS:
# git clone https://github.com/yourrepo/trade_bot.git /tmp/trade_bot_src
```

### 4. Run the deploy script

```bash
ssh ubuntu@<vps-ip>
sudo bash /tmp/trade_bot_src/deploy/deploy.sh
```

This installs system packages, copies the repo to `/opt/trade_bot`, creates the venv, installs pip dependencies, and registers the systemd service. Does **not** start the bot yet.

### 5. Copy your .env

```bash
# From local machine — never commit .env to git:
scp .env ubuntu@<vps-ip>:/opt/trade_bot/.env
ssh ubuntu@<vps-ip> "chmod 600 /opt/trade_bot/.env"
```

The `.env` must contain at minimum:

```
KRAKEN_API_KEY=...
KRAKEN_API_SECRET=...
TELEGRAM_BOT_TOKEN=...
TELEGRAM_CHAT_ID=...
LIVE_TRADING=true
PAPER_MODE=false
UNIVERSE_WHITELIST=BTC/CAD
MAX_SLOT_CASH_CAD=77
```

### 6. Run the smoke check

```bash
cd /opt/trade_bot
source venv/bin/activate
python deploy/smoke_check.py
```

All checks must pass before starting the bot. If any fail:
- Strategy hash mismatch: run `python stamp_strategy.py` locally then re-deploy
- Kraken connectivity fail: verify API key and secret in .env
- Slot cap not set: add `MAX_SLOT_CASH_CAD=77` to .env

### 7. Set up logrotate

```bash
sudo tee /etc/logrotate.d/trade_bot << 'EOF'
/opt/trade_bot/logs/trade_bot.log {
    weekly
    rotate 4
    compress
    missingok
    notifempty
    copytruncate
}
EOF
```

### 8. Start the bot

```bash
sudo systemctl start trade_bot
sudo systemctl status trade_bot
```

Verify it started cleanly:

```bash
sudo journalctl -u trade_bot -n 50 --no-pager
```

Look for `CapitalPool init` and `startup alert` lines. If the Telegram startup alert fires, the bot is live.

---

## Redeploy (code update)

```bash
# From local machine:
rsync -av --exclude='.git' --exclude='venv' --exclude='__pycache__' \
  --exclude='logs/' \
  /path/to/trade_bot/ ubuntu@<vps-ip>:/opt/trade_bot/

# Then on VPS:
ssh ubuntu@<vps-ip>
cd /opt/trade_bot && source venv/bin/activate
pip install -r requirements.txt -q   # only if requirements changed
python deploy/smoke_check.py         # verify before restart
sudo systemctl restart trade_bot
sudo journalctl -u trade_bot -f      # watch live
```

> **State files survive redeploy:** `rsync --exclude='logs/'` skips the whole logs directory, preserving `logs/live_state_*.json` and `logs/trades.db`. Never `--exclude='logs'` without the trailing slash or you risk wiping state.

---

## Kill switch (< 1 minute)

```bash
ssh ubuntu@<vps-ip> "sudo systemctl stop trade_bot"
```

The bot exits cleanly on SIGTERM. Any open limit order on Kraken is **not** cancelled automatically — log into Kraken web and cancel manually if needed.

To prevent automatic restart while you investigate:

```bash
sudo systemctl stop trade_bot
sudo systemctl disable trade_bot   # survives reboot without starting
# ... investigate ...
sudo systemctl enable trade_bot    # re-arm when ready
sudo systemctl start trade_bot
```

---

## Monitoring

```bash
# Live log tail
sudo journalctl -u trade_bot -f

# Last 100 lines
sudo journalctl -u trade_bot -n 100 --no-pager

# Check crash count since last boot
sudo journalctl -u trade_bot -b --no-pager | grep -c "Started Trade Bot"

# Bot startup timestamps (crash-loop detection)
cat /opt/trade_bot/logs/startup_timestamps.txt

# Uptime
sudo systemctl status trade_bot
```

See `deploy/UPTIME.md` for the full monitoring reference and alert table.

---

## systemd configuration rationale

The service at `deploy/trade_bot.service` uses:

```ini
Restart=always
RestartSec=30
StartLimitIntervalSec=0
```

- `Restart=always`: restarts on any exit (crash, OOM, SIGTERM from OS)
- `RestartSec=30`: 30-second cooldown between restarts (prevents CPU storm)
- `StartLimitIntervalSec=0`: never permanently stop restarting (old default was 5 crashes in 5 min → permanent stop with no alert)

In-process crash-loop detection (`bot/main.py`) fires a Telegram alert if 3+ restarts happen within 5 minutes, so you are notified even though systemd will keep restarting.

---

## ARM / aarch64 — compatibility notes

| Package | ARM-safe? | Notes |
|---------|-----------|-------|
| ccxt | ✓ | Pure Python — no native code |
| pandas | ✓ | Available in apt/pip for arm64 |
| numpy | ✓ | pip wheels available for aarch64 |
| python-dotenv | ✓ | Pure Python |
| sqlite3 | ✓ | Built into Python stdlib |

If `pip install` fails on any package, try `apt-get install python3-<pkg>` as a fallback.

---

## Secrets management

- `.env` file: `chmod 600` — readable by root only
- Never commit `.env` to git
- Kraken API key permissions: Query Funds, Query Orders, Create Orders, Cancel Orders only
- **Never enable Withdraw on the bot API key**
- If the key is compromised: disable it on Kraken (Security → API) immediately; the bot will error and Telegram will alert

---

## Capacity / cost estimates (Oracle Free Tier)

| Resource | Usage |
|----------|-------|
| CPU | < 5% average (candle fetch + math once per 60 min) |
| RAM | ~120 MB resident |
| Disk | < 500 MB (venv + logs) |
| Outbound traffic | ~2 MB/day (Kraken API + Telegram) |

Oracle Free Tier Ampere A1 (4 OCPU, 24 GB RAM) is far more than enough and costs $0/month.
