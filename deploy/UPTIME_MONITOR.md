# External Uptime Monitor — UptimeRobot Setup

The bot has no HTTP server, so use a **Heartbeat (cron) monitor**:
the VPS pings UptimeRobot every 5 minutes; if 3 consecutive pings are
missed, UptimeRobot sends an alert.

---

## Step-by-step setup

### 1. Create a free UptimeRobot account
Go to https://uptimerobot.com → Sign up (free tier allows 50 monitors).

### 2. Create a Heartbeat monitor
- Dashboard → **Add New Monitor**
- Monitor Type: **Heartbeat**
- Friendly Name: `Trade Bot — Kraken BTC/CAD`
- Heartbeat Interval: **5 minutes**
- Click **Save**
- UptimeRobot gives you a unique **Heartbeat URL** (copy it).

### 3. Add a cron job on the VPS to ping that URL
SSH into the VPS and run `crontab -e`, then add:

```cron
*/5 * * * * curl -s <PASTE_UPTIMEROBOT_HEARTBEAT_URL_HERE> > /dev/null
```

This pings every 5 minutes. If 3 consecutive pings are missed (15 min
of silence), UptimeRobot fires an alert.

### 4. Configure alert contacts
- UptimeRobot Dashboard → **Alert Contacts** → **Add Alert Contact**
- Type: **E-mail** → enter your email
- Optional: add a Telegram webhook via the Telegram Bot integration
  (Settings → Integrations → Telegram)

### 5. Verify it works
- After saving the cron job, wait 10 minutes
- UptimeRobot should show the monitor as **UP**
- To test an alert: temporarily comment out the cron line, wait 15 min,
  then restore it

---

## Why Heartbeat (not HTTP)?

The bot is a long-running Python process with no HTTP endpoint. A
Heartbeat monitor flips the model: the bot side (VPS cron) pushes a
ping to UptimeRobot; if pushes stop, the alert fires. This catches:
- systemd restart limit exhausted (bot stopped after 5 crashes)
- VPS network/power outage
- Runaway memory causing OOM kill

---

## systemd restart limit note

The trade_bot.service in this repo uses `Restart=always`. But systemd
stops restarting after `StartLimitBurst` (default 5) failures in
`StartLimitInterval` (default 10s). After that the service stays in
failed state with no further restarts and no external alert — UptimeRobot
is the only safety net that catches this.

To reset a stuck service manually:
```bash
sudo systemctl reset-failed trade_bot
sudo systemctl start trade_bot
```
