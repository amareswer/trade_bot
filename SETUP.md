# Setup, Run & New-Machine Migration Guide

Everything needed to run both bots — or move the whole system to a new computer —
in one place. For strategy/validation rules see `CLAUDE.md`; for the crypto bot
user guide see `README.md`.

---

## What runs here

| Bot | Market | Money | Needs |
|-----|--------|-------|-------|
| **Crypto bot** (`bot/`) | Kraken BTC/CAD, 4h candles, 24/7 | **REAL** — capped at $77/slot (`MAX_SLOT_CASH_CAD`) | Kraken API key |
| **Stock bot** (`stock_bot/`) | US stocks via IBKR **paper** account (DUQ273338) | Simulated (real order routing) | TWS running + logged in |

Both bots also run background jobs themselves: unified dashboard refresh, daily
shadow audit, weekly live-comparison, monthly re-screen (crypto bot), SL/TP
watcher, TWS monitor, heartbeat pings (both). **No cron needed** — macOS cron
cannot write into `~/Desktop` (TCC) and was retired; see `ops/crontab.txt`.

---

## Daily run commands

```bash
cd ~/Desktop/Amaresh/projects/trade_bot

# Crypto bot (24/7)
caffeinate -i .venv/bin/python -m bot.main

# Stock bot (start TWS FIRST and log in — bot refuses to start without it)
caffeinate -i .venv/bin/python -m stock_bot.main

# Tests (see CLAUDE.md manifest for expected count)
.venv/bin/python -m pytest --tb=short -q
```

- **Always `.venv/bin/python`** — system python3 is 3.9 and must not run the bots.
- `caffeinate -i` prevents idle sleep but NOT lid-close sleep — keep the lid open
  or the bots stop (heartbeat emails you if that happens).
- Stop: `Ctrl-C`, or `kill <pid>` — SIGTERM saves state and shuts down gracefully.
- Emergency stop (crypto, no restart needed): `touch logs/HALT` blocks new BUYs
  (SL/TP exits still fire). `rm logs/HALT` resumes. Telegram alert both ways.
- Dashboards (static HTML, auto-regenerated): `unified_dashboard.html` (both
  bots, daily glance) and `stock_dashboard.html` (stock per-symbol drill-down).

---

## Secrets inventory

Secrets live ONLY in two git-ignored files. **Both must be `chmod 600`.**

### Root `.env` (crypto bot + shared)

| Key | What / where to get it | If missing |
|-----|------------------------|------------|
| `KRAKEN_API_KEY` / `KRAKEN_API_SECRET` | Kraken → Security → API. Permissions: Query Funds, Query/Create/Cancel Orders. **Withdrawals OFF — never enable on a bot key.** If the key is IP-restricted, update the restriction when the machine/network changes | Live trading fails at startup |
| `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID` | BotFather token for t.me/amaresh_tradebot. **One token for BOTH bots** — the stock bot reads it from this root file too | All alerts silently log-only |
| `HEARTBEAT_URL` | healthchecks.io → "crypto-bot" check ping URL | No dead-bot email detection |
| `OPENROUTER_API_KEY` | openrouter.ai — optional crypto AI advisory | AI advisory off, trading unaffected |

### `stock_bot/.env` (stock bot)

| Key | What / where to get it | If missing |
|-----|------------------------|------------|
| `NVIDIA_API_KEY` | build.nvidia.com (NIM) — active AI advisory provider | AI advisory off; rule trading unaffected (AI cannot open positions) |
| `OLLAMA_CLOUD_API_KEY` | Dormant fallback, confirmed unused — pending revoke (parked). Do NOT copy to a new machine; delete instead | Nothing |
| `IBKR_HOST` / `IBKR_PORT` / `IBKR_CLIENT_ID` | `127.0.0.1` / `7497` (TWS paper) / `7`. **No API key exists — the running, logged-in TWS session IS the authentication** | Bot can't connect |
| `IBKR_ALLOW_LIVE` | Keep `false` — refuses live ports (7496/4001) and non-DU accounts | Safety guard |
| `HEARTBEAT_URL` / `HEARTBEAT_TWS_URL` | healthchecks.io "stock-bot" and "stock-tws" ping URLs | No dead-bot / TWS-logoff emails |
| `ALERT_EMAIL_*` | Empty by design — Telegram (root `.env`) is the live channel | — |

Everything else in both `.env` files is validated strategy/risk configuration —
**do not change values without re-running validation** (see `CLAUDE.md`,
"Active .env settings" and "Validation Discipline").

External accounts to keep access to: Kraken login, IBKR login (live U26459664
unfunded + paper DUQ273338), healthchecks.io account (amareswer@gmail.com),
Telegram, NVIDIA NIM.

---

## Moving to a new machine

### 1. Code

```bash
git clone <repo> ~/Desktop/Amaresh/projects/trade_bot   # or any path
cd trade_bot
```

### 2. Python environment

```bash
# Homebrew first, if the machine doesn't have it:
/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"

brew install python@3.11
python3.11 -m venv .venv
.venv/bin/pip install -r requirements.lock.txt
```

`requirements.lock.txt` is the exact frozen environment (Python 3.11.15,
pandas 2.3.3, ccxt 4.5.56, yfinance 1.5.1, ib_async 2.1.0). Install from the
lock, not by hand — pandas 3.x and other majors are deliberately NOT adopted.

### 3. Copy the files git does NOT carry

These exist only on the old machine. The state files are the **only copy of the
live trading record** (gate progress, realized P&L, breaker peaks) — losing them
resets history, not just convenience.

| Copy | Why |
|------|-----|
| `.env` | All crypto/shared secrets + validated config |
| `stock_bot/.env` | Stock secrets + validated config |
| `logs/trades.db` | Live fill history — capital-gate PF is computed from this |
| `logs/live_state_*.json` | Per-symbol position/cash state |
| `logs/risk_state.json` | Drawdown peak + daily counters (breaker memory) |
| `logs/audit_state.json` | Audit scheduler last-run dates |
| `stock_bot/paper_state.json`, `stock_bot/paper_trades.csv` | Frozen sim-era book (Phase A gate counts it) |
| `stock_bot/ibkr_state.json`, `stock_bot/ibkr_trades.csv` | IBKR-era book + realized P&L |
| `stock_bot/fast_validator_state.json`, `stock_bot/fast_trades.csv` | Swing book |
| `stock_bot/archive/`, `logs/archive/` | Historical records (pre-guard/retired) |

Optional but nice: `logs/*.md` reports and `logs/trade_bot.log` for history.
Re-creatable, skip: `universe_cache.json`, `stock_price_cache.json`, dashboards.

```bash
chmod 600 .env stock_bot/.env
```

### 4. TWS (stock bot only)

1. Install **classic Trader Workstation**:

   ```bash
   brew install --cask trader-workstation
   ```

   NOT "IBKR Desktop" (that app has no API support; installing it by mistake
   has happened before).
2. Log in with the Live/Paper toggle set to **Paper**.
3. Global Configuration → API → Settings: Enable ActiveX and Socket Clients ON,
   Read-Only API OFF, port **7497**, trusted IP 127.0.0.1 only,
   "Bypass Order Precautions for API Orders" ON.
4. Global Configuration → Lock and Exit → **Auto restart** (otherwise TWS logs
   itself off nightly and orders fail until re-login).
5. IBKR still forces a weekly re-login **every Sunday evening** — the bot
   reminds you (Telegram) and auto-reconnects within ~5 min of login.

### 5. Verify before trusting it

```bash
.venv/bin/python -m pytest --tb=short -q     # expected count: CLAUDE.md manifest
EXCHANGE=binance SYMBOL=BTC/USDT .venv/bin/python backtest.py
                                             # expected fingerprint: CLAUDE.md
.venv/bin/python ibkr_smoke.py               # TWS read paths (stock bot)
```

Then start both bots and confirm: startup message arrives on Telegram, all
three healthchecks.io checks turn green within ~5 min, dashboards regenerate.
If Kraken API is IP-restricted, the crypto bot fails visibly until the
restriction is updated for the new network.

### 6. Old machine

Stop both bots (`Ctrl-C` — never run the same live bot on two machines: both
would trade the same Kraken account) and archive its `.env` files securely.

---

## Known platform gotchas

- **macOS cron cannot write into `~/Desktop`** (TCC denies it, errors go to
  local mail) — that's why audits run inside the crypto bot. Don't re-add cron.
- **TSX (.TO) symbols cannot be traded via any API** — CIRO regulation, not a
  setting. The whitelist uses NYSE cross-listings instead. Never re-add `.TO`
  to `RULE_WHITELIST`.
- yfinance: never add custom sessions, never use `ticker.info` for names —
  see the hard rules in `CLAUDE.md` / memory before touching data code.
- The paper account is CAD while US prices are USD — sizing runs ~35% over
  target on US names (accepted for paper; revisit before live).
