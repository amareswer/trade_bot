---
name: telegram-control
description: Two-way Telegram control for the crypto bot (getUpdates poller) — commands, auth model, and the shared-token constraint with the stock bot
metadata:
  type: project
---

Built 2026-08-20, closing the gap flagged during the 2026-08-19 Freqtrade comparison: Telegram
was alert-only (`bot/alerts/telegram.py`'s `TelegramAlerter`, outbound `sendMessage` only) —
no way to query status or control the bot remotely without SSH.

**Why:** Full design/reasoning + test list is in `CLAUDE.md`'s "Two-way Telegram control"
section (crypto operational config area) — this file is the compressed pointer + the one fact
worth flagging fast if it ever comes up again: **the shared-token constraint.**

**How to apply:**

- New module `bot/alerts/telegram_control.py` (`TelegramCommandPoller`), long-polls
  `getUpdates`, zero trading imports — no `LiveExecutor`/`RiskManager`/`ccxt` reference
  anywhere in it (verified by a source-inspection test, not just convention).
- `bot/main.py` builds the actual handlers as extracted, testable module-level functions
  (`_status_crypto_text`, `_format_symbol_status`, `_pause_crypto_flag`,
  `_resume_crypto_flag`, `_status_stock_text`, `_help_crypto_text`) — same
  "extract for testability" pattern as `_check_halt_flag`/`_evaluate_drift`/
  `_seed_native_stop_state`.
- Commands: `/status_crypto`, `/pause_crypto`, `/resume_crypto`, `/status_stock`
  (read-only, reads stock_bot's own state file — no second poller), `/help_crypto`.
- `/pause_crypto`/`/resume_crypto` **only** touch `logs/HALT` — the exact same flag file
  `_check_halt_flag()` already polls every tick. No parallel halt path, no direct
  `risk.halt()`/`risk.resume()` call from a Telegram handler.
- Auth: `chat.id` must match `TELEGRAM_CHAT_ID` exactly. Mismatch → silently ignored (no
  reply, INFO log only) — a reply would confirm to a stranger the bot is live and worth
  probing. An authorized chat sending an unrecognized command gets the same silent-ignore
  treatment.
- Ships **off** by default — `TELEGRAM_CONTROL_ENABLED=false` (config.py default; not set in
  `.env`), separate flag from `TELEGRAM_ENABLED` (outbound alerts). Built and tested, not yet
  turned on live as of 2026-08-20.

**⚠️ Shared-token constraint — read this before ever adding a second `getUpdates` poller
anywhere in this repo:** `TELEGRAM_BOT_TOKEN`/`TELEGRAM_CHAT_ID` are the SAME credentials the
stock bot's outbound-only `TelegramAlerter` already uses (`stock_bot/alerts/notifier.py`'s
`_make_telegram()`, "one token/chat source for both bots" since 2026-07-17). Telegram's
`getUpdates` `offset` parameter is a **server-side, per-bot-token** acknowledgment — passing
`offset=N` permanently discards every update with `update_id < N` for that token, for every
caller, not just whoever passed it. Two independent processes each tracking their own local
offset against this one token WILL corrupt each other — this is Telegram's documented
single-consumer-per-token model, not an edge case to code around.

Consequence, verified during design: **exactly one process may ever run a
`TelegramCommandPoller` against this token.** Today that's the crypto bot only — confirmed
the stock bot has no inbound polling at all, and separately has no `logs/HALT`-equivalent
flag file to pause against yet (its breakers are all threshold-driven: drawdown/kill-switch/
weekly-loss, no operator flag). `/status_stock` sidesteps this entirely by having the
crypto-owned poller read `stock_bot/paper_state.json`/`ibkr_state.json` directly off disk —
same read-only cross-bot pattern `unified_dashboard.py` already uses from one process, zero
IPC, zero second consumer.

**If stock-bot two-way control is ever requested:** it must either (a) route through this
same crypto-owned poller (add more handlers to the existing dict in `bot/main.py`, plus first
build a `logs/HALT`-equivalent flag mechanism for `stock_bot/main.py` — that doesn't exist
yet and is its own small feature), or (b) use a second, dedicated Telegram bot token with its
own independent `getUpdates` queue. Never (c) a second independent poller against this same
token — that's the corruption case above, not a design choice to weigh.

Tests: `tests/crypto/test_telegram_control.py`, 28 cases. Suite 552→580.

See also [[execution_layer]] for the native-stop-loss Telegram alerting this control channel
sits alongside (still outbound-only, unrelated code path).
