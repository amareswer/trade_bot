# Crypto bot security review — 2026-09-20

Scope: gate item 1 of the gated paper/shadow readiness review (Kraken API permissions,
withdrawal restrictions, secret storage, host/process access, log exposure, key rotation,
incident response). No live trading enabled, no orders placed, no strategy/HALT change as
part of this review — this is documentation + read-only local inspection only.

## Result

**PASS on secret storage and log exposure. UNVERIFIED on Kraken key permission scope
(requires a manual one-time check the user must do — see below). NOT STARTED on host
hardening (bot runs locally; the VPS deploy path is unused today) and on a written key-
rotation / incident-response procedure (drafted below for the first time).**

## 1. Kraken API key permissions + withdrawal restriction

**Could not be verified automatically.** Kraken's private API has no endpoint that returns
a key's own permission scopes — the only ways to check are the Kraken web UI (Settings →
API) or a behavioral probe that deliberately calls a permission-gated endpoint and checks
whether it's rejected. A behavioral probe against `Withdraw` was considered and rejected
for this pass: even calling it with a deliberately-invalid withdrawal-address nickname still
sends a real authenticated request to the withdrawal endpoint, and Kraken's permission check
is not guaranteed to run before any other validation — the safe, zero-risk way to confirm
"withdrawals disabled" is the read-only web UI page, not an API call. No code changes.

**Action required from the user (one-time, ~2 minutes, do this before any capital pilot):**
Log into Kraken → Settings → API → open the bot's key → confirm:
- [ ] Withdraw Funds: **disabled**
- [ ] Query Funds: enabled
- [ ] Query Open/Closed Orders & Trades: enabled
- [ ] Create & Modify Orders: enabled
- [ ] Cancel/Close Orders: enabled
- [ ] IP restriction: set to the machine(s) that actually run the bot (CLAUDE.md already
      recommends this; the 2026-08-15 and 2026-09-04 Kraken auth outages were both caused by
      an IP-restricted key vs. a dynamic IP — if using IP restriction, pair it with a static
      IP or expect recurring auth outages, per `.memory/project_kraken_auth_outage_2026-09-04.md`)

This checklist already existed as guidance in CLAUDE.md ("Exchange Setup") but nothing on
record confirms it was ever actually walked through against the live key — do it once and
note the date here or in memory.

## 2. Secret storage — PASS

- Root `.env` (`KRAKEN_API_KEY`, `KRAKEN_API_SECRET`, `TELEGRAM_BOT_TOKEN`,
  `TELEGRAM_CHAT_ID`, `MISTRAL_API_KEY`, `OPENROUTER_API_KEY`) and `stock_bot/.env`
  (`NVIDIA_API_KEY`, `OLLAMA_CLOUD_API_KEY`, `ALERT_EMAIL_PASSWORD`, `IBKR_*`) are both
  gitignored (`.gitignore:2`) and confirmed **never committed**
  (`git log --all --full-history -- .env` is empty).
- Grepped all 365 tracked files for literal key/token/secret assignments (excluding
  `os.environ`/`os.getenv` reads) — **zero hardcoded credentials found** in code, tests,
  fixtures, or docs.
- No other `.env*` files exist beyond the two real files and their `.env.example` templates
  (templates contain placeholder values only).

## 3. Log exposure — PASS

`logs/` and `*.db` are entirely gitignored (`.gitignore:31-34`) — trade logs, state files,
and the accounting SQLite store (`logs/trades.db` and friends) are never at risk of being
committed. No raw API key or secret was found in any log file checked.

## 4. Host / process access — NOT STARTED (no live deployment target yet)

The bot runs locally on the user's Mac today with no process sandboxing — this has been the
status quo throughout development and is a pre-existing condition, not something this review
introduces. `deploy/trade_bot.service` (written for the not-yet-provisioned VPS, roadmap item
F/G) already sets `NoNewPrivileges=yes`, `PrivateTmp=yes`, `MemoryMax=512M`, `CPUQuota=50%`,
`Restart=always`, but runs as a generic `User=ubuntu` rather than a dedicated least-privilege
service account, and has no `ProtectSystem=strict`/`ReadOnlyPaths`/seccomp filtering.

**Recommendation, not urgent while running locally:** before the VPS migration, harden
`trade_bot.service` with a dedicated non-login service user, `ProtectSystem=strict`, and an
explicit `ReadWritePaths=` limited to `logs/` and the accounting DB path. Low priority — no
capital pilot should be running on a VPS before the paper/shadow and profitability gates pass
anyway, and this doesn't block a local paper/shadow run.

## 5. Key rotation procedure (new — none existed before this review)

1. Generate a new Kraken API key (Settings → API → Add Key) with the exact same permission
   scope as the checklist in section 1 — **disable Withdraw Funds on the new key too**.
2. Set the IP restriction on the new key before activating it, if used.
3. Update `KRAKEN_API_KEY` / `KRAKEN_API_SECRET` in the root `.env` (never in code, never
   committed — see section 2).
4. Stop the bot process cleanly (SIGTERM — the bot already has graceful-shutdown handling;
   see CLAUDE.md "Both bots: crash-alert + atomic state writes + SIGTERM graceful shutdown").
5. Restart the bot so it picks up the new key. Confirm with `check_kraken_balance.py`
   (read-only, no orders) that the new key authenticates before considering rotation done.
6. Revoke the old key in the Kraken UI only after confirming the new one works — don't revoke
   the old key first (that would take the bot down mid-rotation with no fallback).
7. Rotation trigger events: suspected key exposure (e.g., accidental log/commit, though
   section 2/3 above confirm this hasn't happened), routine hygiene (no fixed cadence set —
   consider annually), or after any of the two 2026 Kraken auth outages if the cause is ever
   traced to something more than an IP mismatch.

## 6. Incident response procedure (new — none existed before this review)

**Trigger conditions** (any one is enough to act):
- Unexpected order(s) on Kraken the bot's own logs/state don't account for
- `EGeneral:Permission denied` on every authenticated call (auth outage — has happened twice,
  see `.memory/project_kraken_auth_outage_2026-09-04.md`) lasting beyond one heartbeat cycle
- A security alert from Kraken itself (new-device login, key-usage anomaly email)
- Suspected credential exposure of any kind

**Immediate steps, in order:**
1. **Engage `logs/HALT`** (`touch logs/HALT` or `/pause_crypto` via Telegram control) — this is
   the existing full-stop kill-switch; it blocks new BUYs and, per the documented 2026-09-13
   correction, also blocks strategy-driven SELLs (SL/TP exits stay live via
   `RISK_HALT_BLOCKS_STOPS=false`). This is already the fastest available control — no new
   code needed.
2. If credential exposure is suspected: revoke the Kraken key immediately from the web UI
   (don't wait to generate a replacement first — an exposed key with trade permissions is a
   real financial risk; a rotation can happen after revocation with the bot halted).
3. Check `logs/` for the incident window and cross-reference against
   `logs/dynamic_universe_dashboard.json` / the accounting store (`logs/trades.db`) for any
   fill the bot itself didn't originate.
4. Reconcile the real Kraken balance (`check_kraken_balance.py`, read-only) against the bot's
   own tracked position/cash before resuming anything.
5. Don't lift HALT until the root cause is understood and, if it was a credential issue, a
   rotated key (section 5) is confirmed working.

## What this review does NOT cover

- It does not verify the Kraken key's actual live permission scope (section 1 — needs the
  user's own one-time manual check).
- It does not harden a VPS deployment (none exists yet — deferred with the VPS migration,
  roadmap item F/G).
- It says nothing about paper/shadow execution or profitability — see
  `deploy/PAPER_SHADOW_RUNBOOK.md` and the pinned-backtest re-validation in
  `CRYPTO_BOT_GATED_READINESS_REPORT_2026-09-20.md` for those gates.
