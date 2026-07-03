# Uptime Operations Guide

## How to check uptime

### Quick status
```bash
systemctl status trade_bot
```
Shows current state (active/failed), last 10 log lines, and restart count.

### Journal (last 100 lines)
```bash
journalctl -u trade_bot -n 100 --no-pager
```

### Live tail
```bash
journalctl -u trade_bot -f
```

### Restart count since last boot
```bash
systemctl show trade_bot --property=NRestarts
```

### Uptime since last (re)start
```bash
systemctl show trade_bot --property=ActiveEnterTimestamp
```

### Startup history (crash-loop detection log)
```bash
cat logs/startup_timestamps.txt
```
Each line is a UTC ISO timestamp. Three or more entries within any 5-minute
window triggers a Telegram crash-loop alert automatically.

---

## External monitor (UptimeRobot)

The bot has no HTTP server so we use a **Heartbeat (cron) monitor**.
See `deploy/UPTIME_MONITOR.md` for setup instructions.

**What UptimeRobot monitors:** A cron ping every 5 minutes from the VPS.
If 3 consecutive pings are missed (15 min silence), UptimeRobot fires an alert.

**Heartbeat URL location:** Stored in the UptimeRobot dashboard under
"Trade Bot — Kraken BTC/CAD". Add it to crontab:
```
*/5 * * * * curl -s <HEARTBEAT_URL> > /dev/null
```

---

## What each alert means

| Alert source | Message pattern | Meaning | Action |
|---|---|---|---|
| Telegram (startup) | "Bot started — LIVE kraken BTC/CAD" | Normal boot or systemd restart | None if infrequent |
| Telegram (crash-loop) | "Crash-loop: N restarts in 5 min" | Bot crashing repeatedly | `journalctl -u trade_bot -n 50` to find root cause |
| Telegram (error) | "Candle watchdog: no new Xmin candle" | Price feed or exchange down | Check Kraken status; bot will recover when feed resumes |
| Telegram (error) | "Price feed down N consecutive ticks" | Network or exchange issue | Usually self-healing; check after 15 min |
| Telegram (error) | "Position drift detected" | Bot state diverged from exchange | Check `logs/live_state_BTC_CAD.json` vs Kraken balance |
| UptimeRobot | "Trade Bot is DOWN" | VPS heartbeat stopped | SSH in and check `systemctl status trade_bot` |

---

## Manual restart

```bash
sudo systemctl restart trade_bot
```

If systemd shows the service in **failed** state (which should not happen with
`StartLimitIntervalSec=0` but can if manually stopped):
```bash
sudo systemctl reset-failed trade_bot
sudo systemctl start trade_bot
```

---

## systemd unit configuration

File: `/opt/trade_bot/deploy/trade_bot.service`

Key settings:
- `Restart=always` — restarts on any exit (crash, OOM, signal)
- `RestartSec=30` — 30-second cooldown between restarts
- `StartLimitIntervalSec=0` — **never gives up** restarting (no burst limit)
- `MemoryMax=512M` — OOM kill threshold
- `StandardOutput=journal` — all stdout/stderr goes to journald

**Why `StartLimitIntervalSec=0`?**  
The old value (`StartLimitIntervalSec=300` + `StartLimitBurst=5`) caused
systemd to permanently stop the service after 5 crashes in 5 minutes with no
further restarts and no alert — 58%+ observed downtime. With `=0`, systemd
never gives up; crash-loop protection is handled in-process via
`startup_timestamps.txt` + Telegram alert instead.

---

## Deploying the service

```bash
cd /opt/trade_bot
sudo cp deploy/trade_bot.service /etc/systemd/system/trade_bot.service
sudo systemctl daemon-reload
sudo systemctl enable trade_bot
sudo systemctl start trade_bot
```

After a service file change:
```bash
sudo systemctl daemon-reload
sudo systemctl restart trade_bot
```

---

## Log rotation

File: `/etc/logrotate.d/trade_bot` (install with `sudo cp deploy/logrotate_trade_bot.conf /etc/logrotate.d/trade_bot`)

Rotates `logs/trade_bot.log` weekly, keeps 4 rotations, compressed.
Uses `copytruncate` — no service restart required.

Test: `sudo logrotate -f /etc/logrotate.d/trade_bot`

---

## Diagnosing 58%+ downtime (historical)

Root cause (confirmed 2026-07-02): bot was running on a local Mac laptop
with `caffeinate` instead of on the VPS with systemd. Mac sleep and manual
stops between development sessions caused the observed downtime.

**Gaps > 12h in `logs/startup_timestamps.txt` indicate:**
- Mac sleep (no caffeinate, lid close)
- Manual stop (`Ctrl-C` or terminal close)
- No restart on the local machine

**Fix:** Deploy to VPS with the systemd service file as described above.
Once on VPS, expect 99%+ uptime except for intentional restarts.
