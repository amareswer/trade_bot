# IB Gateway + IBC Setup — Headless Stock Bot

How to run the **stock bot** on a headless server (VPS or always-on box) by
replacing the GUI TWS application with **IB Gateway** (API-only, no charts)
managed by **IBC** (auto-login, auto-restart, dialog handling).

> **The bot needs no code change.** `IBKRExecutor` already connects by
> `host:port` and its docstring/`_LIVE_PORTS` already cover IB Gateway. The
> only bot-side change is one line in `stock_bot/.env` (`IBKR_PORT`). Everything
> below is infrastructure.

This guide is the stock-bot companion to `deploy/VPS_SETUP.md` (crypto bot).
The crypto bot has none of these requirements — it talks to Kraken over plain
HTTPS with no local broker software.

---

## Why this is needed

| | TWS (today, on the Mac) | IB Gateway + IBC (headless) |
|---|---|---|
| UI | Full Java GUI (charts, order tickets) | Minimal login window only |
| Runs headless | No — needs a desktop | Yes — with Xvfb (virtual display) |
| Login | You click through it | IBC types the credentials |
| Daily 00:00 logout | You'd have to re-login by hand | IBC auto-restarts it |
| RAM | ~400–700 MB | ~250–400 MB |

IBKR force-logs-out every session once per day (~23:45 US Eastern). Without IBC
the bot would lose its broker connection every night until someone logged back
in. IBC exists specifically to make an unattended Gateway viable.

---

## Prerequisites

| Item | Requirement |
|------|-------------|
| OS | Ubuntu 22.04 LTS (amd64 or aarch64) |
| RAM | 2 GB minimum (Gateway ~350 MB + bot ~400 MB + headroom) |
| Disk | 3 GB free (Gateway + IBC + venv + logs) |
| IBKR account | The **paper trading username/password** (NOT the live account login — see "2FA" below) |
| Outbound | 443 to IBKR (`*.interactivebrokers.com`), Telegram, Yahoo Finance |
| Inbound | **None** — Gateway's API port stays bound to `127.0.0.1` |

> **Confirm before you start:** log into <https://www.interactivebrokers.com>
> with your **paper** credentials once, in a browser. If it lets you in with
> just username + password (no phone approval), you are clear. If it demands
> 2FA, read the "2FA" section before going further — but this is uncommon for
> dedicated paper logins.

---

## Path A — Docker (recommended)

`gnzsnz/ib-gateway-docker` bundles Gateway + IBC + Xvfb + a VNC server in one
maintained image. It is the lowest-friction path and the one to use unless you
have a specific reason not to run Docker on the host.

> **Verify against the current image README before you start:**
> <https://github.com/gnzsnz/ib-gateway-docker>. This image's exact environment
> variable names and its *internal* API ports have changed across releases. The
> compose file below is a starting point — reconcile the `environment:` keys and
> the right-hand side of the `ports:` mapping with the README for the tag you
> pin. The **invariants** that must hold regardless of version:
> - host side of the API port mapping = **`127.0.0.1:4002`** (matches
>   `IBKR_PORT` and the `stock_bot.service` port-wait)
> - read-only API **disabled** (the bot places orders)
> - trading mode **paper**
> - a daily auto-restart time **set** (before IBKR's forced logout)

### A1. Install Docker

```bash
curl -fsSL https://get.docker.com | sudo sh
sudo usermod -aG docker $USER   # log out/in for this to take effect
```

### A2. Compose file

Create `/opt/trade_bot/deploy/ib-gateway/docker-compose.yml`:

```yaml
services:
  ib-gateway:
    image: ghcr.io/gnzsnz/ib-gateway:stable   # pin a real tag in production, e.g. :10.30
    container_name: ib-gateway
    restart: unless-stopped
    environment:
      TWS_USERID: ${IB_PAPER_USER}
      TWS_PASSWORD: ${IB_PAPER_PASSWORD}
      TRADING_MODE: paper
      READ_ONLY_API: "no"
      TWS_ACCEPT_INCOMING: accept
      BYPASS_WARNING: "yes"
      AUTO_RESTART_TIME: "23:45"      # IBC restarts Gateway daily before IBKR's forced logout
      TIME_ZONE: America/Toronto
    ports:
      - "127.0.0.1:4002:4004"          # host 4002 → container paper API port (VERIFY internal port in README)
      - "127.0.0.1:5900:5900"          # VNC — for first-run debugging ONLY, close it after
    volumes:
      - ./config:/root/Jts/config      # persists settings between restarts
```

`READ_ONLY_API: "no"` is mandatory — the bot places orders. `BYPASS_WARNING`
dismisses the "you are using a paper account" nag. The container's internal
paper-API port (right-hand side of the `4002:XXXX` mapping) depends on the image
version — **check the README** and adjust; the host side stays **4002** so it
matches `IBKR_PORT` below and the `stock_bot.service` port-wait.

### A3. Secrets

```bash
cat > /opt/trade_bot/deploy/ib-gateway/.env <<'EOF'
IB_PAPER_USER=your_paper_username
IB_PAPER_PASSWORD=your_paper_password
EOF
chmod 600 /opt/trade_bot/deploy/ib-gateway/.env
```

Never commit this file. It is separate from `stock_bot/.env`.

### A4. First start + verify via VNC

```bash
cd /opt/trade_bot/deploy/ib-gateway
docker compose --env-file .env up -d
docker compose logs -f            # watch for "IBC: Login has completed"
```

From your laptop, tunnel VNC and look once:

```bash
ssh -L 5900:127.0.0.1:5900 ubuntu@<vps-ip>
# then point a VNC client at localhost:5900 (no password by default in this image —
# set VNC_SERVER_PASSWORD in the compose env if you keep the port open)
```

You should see a logged-in Gateway with "API" green. Accept any first-run dialog.
Then **remove the `5900:5900` port line** from the compose file and
`docker compose up -d` again — you do not want VNC exposed long-term.

### A5. Auto-start on boot

`restart: unless-stopped` handles container restarts. For host reboots, Docker's
own service starts it. If you want systemd ordering with the bot, add to
`stock_bot.service`:

```
After=docker.service
Wants=docker.service
```

Nothing else — the `ExecStartPre` port-wait in `stock_bot.service` is what
actually blocks the bot until Gateway's API is live.

---

## Path B — Native install (no Docker)

Only if Docker is off the table. More moving parts, and Gateway auto-updates
periodically change the install path IBC points at.

### B1. Packages

```bash
sudo apt-get update
sudo apt-get install -y openjdk-17-jre xvfb x11vnc unzip curl
```

### B2. IB Gateway

```bash
curl -sSL https://download2.interactivebrokers.com/installers/ibgateway/stable-standalone/ibgateway-stable-standalone-linux-x64.sh -o /tmp/ibgw.sh
sudo bash /tmp/ibgw.sh -q -dir /opt/ibgateway
```

### B3. IBC

```bash
cd /opt
sudo curl -sSL -o ibc.zip https://github.com/IbcAlpha/IBC/releases/latest/download/IBCLinux-3.20.0.zip
sudo unzip ibc.zip -d /opt/ibc && sudo rm ibc.zip
sudo chmod +x /opt/ibc/*.sh /opt/ibc/scripts/*.sh
```

### B4. IBC config — `/opt/ibc/config.ini`

```ini
IbLoginId=your_paper_username
IbPassword=your_paper_password
TradingMode=paper
IbDir=/root/Jts
OverrideTwsApiPort=4002
ReadOnlyApi=no
AcceptIncomingConnectionAction=accept
AcceptNonBrokerageAccountWarning=yes
ClosedownAt=
AutoRestartTime=23:45
SecondFactorAuthenticationExitInterval=
ExistingSessionDetectedAction=primary
MinimizeMainWindow=yes
```

`chmod 600 /opt/ibc/config.ini` — it holds the password.

### B5. `ibc.service` — `/etc/systemd/system/ibc.service`

```ini
[Unit]
Description=IB Gateway via IBC
After=network-online.target
Wants=network-online.target
StartLimitIntervalSec=0

[Service]
Type=simple
User=ubuntu
Environment=DISPLAY=:99
Environment=TWS_MAJOR_VRSN=1030
ExecStartPre=/usr/bin/Xvfb :99 -screen 0 1024x768x24 -nolisten tcp &
ExecStart=/opt/ibc/scripts/ibcstart.sh "${TWS_MAJOR_VRSN}" --gateway \
  "--tws-path=/opt/ibgateway" "--ibc-path=/opt/ibc" \
  "--ibc-ini=/opt/ibc/config.ini" "--mode=paper"
Restart=always
RestartSec=30
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now ibc
sudo journalctl -u ibc -f      # wait for "IBC: Login has completed"
```

Then in `stock_bot.service` uncomment `After=ibc.service` / `Wants=ibc.service`.

`TWS_MAJOR_VRSN` must match the installed Gateway major version — check
`ls /opt/ibgateway` and update after every Gateway auto-update.

---

## 2FA

Dedicated **paper** logins usually have no second factor. If yours does:

- **Preferred:** in IBKR Client Portal → Settings → User Settings → Secure Login
  System, check whether the paper user can be set to "No second factor". Paper
  accounts often allow this even when the live account cannot.
- **If not:** IBC supports the *IBKR Mobile* second-factor flow — Gateway sends a
  push to your phone on each login/restart, and IBC waits
  (`SecondFactorAuthenticationExitInterval`). Workable but requires you to tap
  approve once a day around the restart window. Set `AutoRestartTime` so the
  restart lands at a predictable time you can be near your phone.
- **Live account 2FA does not block paper.** This only matters for the paper
  connection today; the live-trading path is separately gate-blocked
  (`IBKR_ALLOW_LIVE=false`, code-enforced).

---

## Daily restart

IBKR forcibly ends every session once per day. Both paths above set
`AutoRestartTime` / `AUTO_RESTART_TIME` to **23:45** local so IBC restarts
Gateway a few minutes *before* the forced logout — a clean ~60s gap instead of a
hard disconnect.

The bot rides through it: `IBKRExecutor.try_reconnect()` re-dials on the next
cycle, and `TwsConnectionMonitor` only alerts after 10 minutes of continuous
disconnect (so a 60s restart never pages you). No bot config needed.

The `HEARTBEAT_TWS_URL` ping in `stock_bot/.env` pauses during the gap and
resumes on reconnect — that is expected, not an incident.

---

## Bot configuration change

The only edit to the bot:

```diff
# stock_bot/.env
- IBKR_PORT=7497               # 7497 = TWS paper
+ IBKR_PORT=4002               # 4002 = IB Gateway paper (was 7497 TWS)
```

Leave `IBKR_HOST=127.0.0.1`, `IBKR_CLIENT_ID=7`, `IBKR_ALLOW_LIVE=false`,
`STOCK_EXECUTOR=ibkr` unchanged.

> `IBKR_CLIENT_ID=7` must not collide with anything else connecting at the same
> time. `ibkr_smoke.py` defaults to client id `42`, so running the smoke check
> while the bot is up is fine.

---

## Verification

Before enabling `stock_bot.service`, prove the connection through the real
executor:

```bash
cd /opt/trade_bot && source venv/bin/activate

# read-only — account, cash, positions, every path main.py uses
python ibkr_smoke.py --port 4002

# 1-share round trip (market hours only, paper account — the executor refuses live)
python ibkr_smoke.py --port 4002 --trade KO
```

All read paths must print real numbers (cash ≈ your paper balance, account
`DUQ273338`). The round trip must show a BUY fill then a SELL fill and land the
two rows in `stock_bot/ibkr_trades.csv`.

Then run the bot in the foreground for one full session before enabling the
service:

```bash
python -m stock_bot.main       # Ctrl-C after a clean cycle or two
```

Watch for `IBKRExecutor connected | account=DUQ273338 (PAPER)` and a normal
scan cycle. Only then:

```bash
sudo cp deploy/stock_bot.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now stock_bot
sudo journalctl -u stock_bot -f
```

---

## Monitoring

```bash
# bot
sudo journalctl -u stock_bot -f
sudo journalctl -u stock_bot -n 100 --no-pager

# gateway (Docker)
docker compose -f /opt/trade_bot/deploy/ib-gateway/docker-compose.yml logs -f
# gateway (native)
sudo journalctl -u ibc -f

# is the API port actually up?
(exec 3<>/dev/tcp/127.0.0.1/4002) && echo "API port open" || echo "API port DOWN"
```

Existing alerting already covers Gateway:
- `HEARTBEAT_TWS_URL` (healthchecks.io) — pings only while the IBKR connection is up
- `TwsConnectionMonitor` — Telegram ops alert after 10 min disconnected
- The 2026-08-27 `sync_healthy` / `csv_write_healthy` edge alerts — fire if
  Gateway answers the socket but stalls on `accountValues()`/`positions()`

---

## Troubleshooting

| Symptom | Cause / fix |
|---------|-------------|
| `stock_bot` stuck in `ExecStartPre`, then fails | Gateway not logged in. Check `journalctl -u ibc` / `docker compose logs` for the IBC login result. |
| `IBKR order failed: ... 10197` or `2119` | Market-data farm not connected yet — usually clears within a minute of Gateway login; the bot's reconnect handles it. |
| Login loops / "Existing session detected" | Another Gateway/TWS is logged in with the same user (e.g. TWS still open on the Mac). One session per login. Set `ExistingSessionDetectedAction=primary` (native) or stop the other one. |
| Works, then dies every night ~23:45 | `AUTO_RESTART_TIME` / `AutoRestartTime` not set, or set to a value IBC parses as "close, don't restart". Must be `HH:MM`, and `ClosedownAt` must be empty. |
| `ReadOnlyApi` errors on BUY | `READ_ONLY_API: "no"` (Docker) / `ReadOnlyApi=no` (native) missing. |
| Native: IBC can't find Gateway after an update | `TWS_MAJOR_VRSN` in `ibc.service` no longer matches `ls /opt/ibgateway`. Update it. |
| yfinance returns "possibly delisted" for real large caps from the VPS | Yahoo rate-limiting the datacenter IP. Test with `python -c "import yfinance as yf; print(yf.download('SPY', period='5d'))"` from the VPS *before* migrating. If it's bad, the stock bot may need to stay on a residential IP. |

---

## Going live (later — not now)

`IBKR_ALLOW_LIVE=false` is code-enforced and gated on `LiveTradingGate` Gates
1–3 (see `stock_bot/execution/ibkr.py`). When that day comes:

1. `TRADING_MODE: live` (Docker) / `TradingMode=live` (native)
2. Host port `4001` instead of `4002`; `IBKR_PORT=4001` in `stock_bot/.env`
3. `IBKR_ALLOW_LIVE=true` — only after a fresh `stock_backtest.py` walk-forward
   pass and all three gates PASS
4. Live account 2FA is real and mandatory — plan the daily-restart window around
   being able to approve the push

Do not skip straight to this. The paper setup must run clean for a meaningful
stretch first.
