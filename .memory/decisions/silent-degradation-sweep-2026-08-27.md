# Silent-degradation bug sweep + bot-improvement plan — 2026-08-27

## Context

User asked Claude to broadly improve both bots ("the most important thing is you
need to improve our bots"), then when offered a 4-track plan selected **all four
tracks** and fully delegated ("you also learning a lot so its all yours"). Not a
one-off task — a standing mandate. Work proceeds one change at a time (core rule
#11), discuss/plan before code (core rule #4).

## The 4-track plan (agreed)

1. **Robustness / bug hardening** — hunt for silent-degradation bugs (the class
   the 2026-08-26 post-only fee bug belonged to). START HERE.
2. **More trading opportunity** — validation-gated: get SYN/USD + PUMP/USD
   promotion-ready; crypto is capital-blocked otherwise. Stock universe just
   went 15→30, instrument whether it yields more qualified BUYs.
3. **Strategy edge / returns** — research, walk-forward gated, slowest. Diagnose
   crypto strategy selectivity (0/15 fills in 65+ days — variance or mis-cal filter?).
4. **Observability / control** — fold in as gaps surface.

**Current position: Track 1 done (both fixes below). Track 2 is next.**

## Track 1 — silent-degradation sweep (COMPLETE)

Swept execution / exchange / risk / AI layers for the pattern: catch exception →
`logger.warning` → silently continue on a worse path, no Telegram alert. That's
exactly how the post-only bug hid for 2 months (maker→taker fallback was
log-only).

### Fix #1 — maker→taker silent-fallback alert (`bot/execution/live_executor.py`)
`_place_limit_order()` has 4 paths that fall back from a post-only limit (maker
~0.25-0.40%) to a market order (taker ~0.80%): orderbook-fetch fail,
spread-too-tight, exchange rejection, chase-timeout. Each now sets
`self._maker_fallback_reason` (cleared per `execute()` call + at top of
`_place_limit_order()` so a stale flag can't misfire); `execute()` reads it after
the fill resolves and fires an `alerter.error()` **MAKER FALLBACK** alert naming
the reason. Post-fill only, can't block. Tests: `tests/crypto/test_live_executor.py`
+2 (63→65). Suite 676→678.

### Fix #2 — MTF gate fail-open alert (`bot/main.py`, `run()` gate 2c)
The 1D BEARISH veto silently failed open (allowed the BUY) when the daily-candle
fetch failed AND no cached closes existed — `logger.warning` only. Now fires an
`alerter.error()` **MTF GATE BYPASSED** alert in exactly the no-cache branch; the
cached-closes path (gate still runs on slightly older data) does not alert.
Needs a rare triple-coincidence to fire. Tests: new file
`tests/crypto/test_mtf_gate_alert.py` +2 (source-inspection guards — `run()` needs
a full live stack). Suite 678→680.

### Checked and cleared (no fix needed)
- `_sync_cash` / `_sync_position` → `starting_cash` fallback: already alerts (fixed 2026-07-28)
- Crypto AI: `AI_ENABLED=false`, not in play
- Stock bot CSV-write / IBKR-value failures: `logger.warning`-only but paper-only +
  IBKR gate-blocked from live; best-effort writes are by design. **Deferred — revisit
  when IBKR goes live.**

## Notes

- Both fixes: no `bot/strategy/*` touched → no walk-forward, hash unchanged.
- Changes staged in working tree, NOT committed (user handles git).
- `CLAUDE.md` is tracked in git as lowercase `claude.md` (pre-existing, harmless on
  macOS's case-insensitive FS, would bite on a Linux VPS). Flagged to user, not fixed.
- CLAUDE.md updated: Test Suite Manifest (676→680), "Post-only param bug" section
  got a monitoring addendum + a "second sweep fix" subsection.

## Track 2 — SYN/USD + PUMP/USD (2026-08-27, concluded)

Re-checked: walk-forwards confirmed current on hash b30f2f9e769c8d41 (no re-run needed).
Liquidity re-checked live — BOTH now pass (SYN spread 0.091% / vol $109k, back above the
floor it failed 2026-08-26; PUMP 0.063% / $6.1M). Both symbols are now
**validation-complete + liquidity-clean**. Two remaining gates — capital ($150-$1,518 gap)
AND a real CAD↔USD multi-currency/FX-conversion build (NOT trivial: Kraken account is
CAD-only, needs an actual on-exchange conversion step) — are BOTH contingent on a deposit.
**FX-build DEFERRED, not started** (premature to build speculatively). When the user funds a
USD symbol, the FX build is the immediate next task. Full detail:
[[multi-symbol-validation]] "USD candidates status re-check + FX-build decision — 2026-08-27".

## Track 4 — blocked-BUY Telegram alert (2026-08-27, DONE)

Found live while looking for a Track 4 gap: `logs/live_signals.csv` showed SOL/CAD firing
BUY signals on 2026-08-27 04:00 + 08:00 UTC, both `blocked_gate=state_machine` — which turned
out to be correct (SOL/CAD DOES hold a position from its first fill 2026-08-26, BUY 0.080808
@ $134.02; CLAUDE.md's "zero live fills" line for SOL is stale). No bug — but the pattern
exposed the gap: a blocked BUY is only visible if you go read the CSV. That's exactly the
2026-08-18 incident (bot flat through a $90k→$108k rally, a real BUY vetoed by the MTF gate,
nobody knew).

New `bot.main._evaluate_blocked_buy_alert()` — edge-triggered `alerter.error()` when the raw
strategy signal is BUY but an external gate holds it. One alert per fresh (symbol, gate)
block, re-alerts on gate change, clears when the BUY clears / raw signal stops being BUY.
Strategy-internal HOLDs never reach it. Called once per candle close after the
`live_signals.csv` write. Tests: new file `tests/crypto/test_blocked_buy_alert.py`, 7 cases.
Suite 680→687. No `bot/strategy/*` touched.

**Corrected in this session:** CLAUDE.md "Current operational status" + the roadmap table
said SOL/CAD had 0 live fills — it has 1 (2026-08-26 BUY @ $134.02, position still open, 0
completed round-trips). The "Post-only param bug" section of the same file already documented
that fill; the two had diverged. Now says 1/15.

## Track 3 — crypto strategy selectivity diagnosis (2026-08-27, no code change)

Question: is BTC/CAD's 0/15 fills in 65+ days a mis-calibrated filter or genuine variance?
**Answer: genuine variance + unfavorable regime — the strategy is faithful and correctly
selective. No actionable fix.** Evidence (all re-verified, not assumed):
- Shadow fidelity 100% (35/35, `logs/shadow_report_20260826.md`) — strategy executes exactly
  as backtested, zero drift.
- Backtest frequency ~1 trade / 27 days (31 trades / 833 days). Live 0-in-56d vs expected ~2
  is P≈12.5% — uncommon, not remarkable.
- `live_signals.csv` (2026-07-02→08-27): 187 near-misses, only 1 raw BUY (08-18, MTF-vetoed),
  blocked-gate distribution rotates with measured ADX = genuine chop signature, no stuck gate.
  Confirms + extends the 2026-08-20 investigation.
- ADX sensitivity (research, params NOT changed): loosening ADX 18→12/15 adds ~2 trades over
  2+ years and *lowers* PF. ADX is not the throttle.
- No safe lever: timeframe locked (1h FAILED WF), params walk-forward-locked, more symbols =
  Track 2 (capital-blocked), shorts impossible on Kraken spot.
- **Doc fix:** CLAUDE.md's "How to verify" pinned-window check claimed "identical result to
  rolling run" — false since ~2026-08-20 (rolling window advanced). Pinned window now gives
  30 trades/PF 1.94 (deterministic); rolling gives the canonical 31/PF 2.19. Both > 1.72
  fingerprint floor, strategy unchanged. Corrected with the pinned window's own expected
  numbers.

Full trail: [[multi-symbol-validation]] "BTC/CAD live signal drought" section,
2026-08-27 re-confirmation addendum.

## Stock bot — same review extended (2026-08-27, "how about stock bot")

**State check:** stock bot live on IBKR paper, holding 1 position (T, 28 sh @ $25.75), realized
+$2.81 since the $5000 rebase, gate at ~5/30. Trades ~1/week (CM/RY/CM/T since 2026-07-20) —
NOT starved like crypto, no selectivity concern. nemotron model swap confirmed active
(`nvidia/nemotron-3-nano-30b-a3b`); the 2026-08-25 `_update_ai_health` monitor fired correctly
on the 08-26 nvidia_nim EOL. paper_state.json is stale/inert (switched to IBKR 2026-07-17).

**Observability fix — blocked rule-BUY digest.** Same gap as the crypto bot: a rule BUY
signal that fires and gets held by a gate (MACRO/EARNINGS_BLACKOUT, REGIME_SKIP, VIX_CRISIS,
MAX_EXPOSURE, MAX_POSITIONS, CORRELATION, SIZE_SKIP) was `print()`-only — gone with the
terminal. The 2026-08-26 RULES-log fix made the *signal* visible but not the *block*. New
`stock_bot.main._evaluate_blocked_rule_buys_alert()` — end-of-cycle DIGEST (universe ~40
symbols, so one summary message, not per-symbol), edge-triggered on the `{symbol: gate}`
mapping. Also covers the SPY-regime-fetch-failure path (regime → UNKNOWN → REGIME_SKIP for
every rule BUY, digest names it). 8 one-line `if _rule_buy:` additions at the gate sites +
one cycle-end call. Tests: new file `tests/stock/test_blocked_rule_buys_alert.py`, 7 cases.
Suite 687→694. No strategy files touched.

**Silent-degradation sweep (stock):** lower stakes than crypto — IBKR is paper-only and
code-gate-blocked from live (LiveTradingGate). CSV write failures + order timeouts are worth
hardening *when IBKR goes live*, deferred. The SPY-regime-fetch-failure path is now surfaced
by the digest above.

**IBKR TWS-query resilience — FIXED 2026-08-27 (user: "keep stock bot ready, improve it").**
The one finding from this sweep worth fixing now rather than deferring: `IBKRExecutor.
_account_value()` and `positions_snapshot()` returned a fabricated `0.0` / `{}` on a transient
TWS-query failure, `logger.warning` only. Live consequences: `cash=0.0` rejects every BUY;
empty position book blinds the SL/TP watcher to a real triggered stop AND corrupts the
breaker equity math. Same class as the crypto `_sync_cash`/`_sync_position` gap (fixed
2026-07-28). Fix: last-good cache served on failure (strictly safer — stale figures only ever
cause a broker reject, which alerts), `executor.sync_healthy` edge flag, `stock_bot/main.py`
fires an edge-triggered `ops_alert` on the failure/recovery transition. First-call-before-any-cache
still returns `0.0`/`{}` (startup, covered by connection guards). Tests +3 (56→59), suite 694→697.

**IBKR `ibkr_trades.csv` write resilience — FIXED same pass (2026-08-27, "fix whatever
needed").** `_record_trade()`'s CSV append was `logger.warning`-and-continue on `OSError` — a
real filled trade missing from the frozen 9-column CSV the readiness gate reads exactly →
gate under-counts. Now `_write_trade_row()` buffers the row and retries next fill;
`csv_write_healthy` edge-alerted from main.py like `sync_healthy`. Order-timeout path checked,
left as-is (already raises → rejected Order → `ops_alert`). `_log_settlement_csv` left
warning-only (tax file, not gate schema, docstring already says best-effort). +1 test (59→60),
suite 697→698.

**IBKR remaining deferred:** nothing material left — the order path, state save, and reconnect
probe are all handled. The stock bot's IBKR executor is now readiness-hardened.

**AI auto-failover (Mistral) — BUILT 2026-08-27 (user: "we can use mistral.ai free API
right" → "yes").** New `mistral` provider in `stock_bot/ai/ai_engine.py` (OpenAI-compatible
`api.mistral.ai/v1/chat/completions`, free "Experiment" tier — verified via web search:
~1 req/s, ~1B tokens/mo, no card; ~10x this bot's realistic volume, and beats OpenRouter's
50/day free cap). One-shot `_switch_to_fallback()`: after `_FALLBACK_AFTER=5` consecutive
nvidia_nim *API* failures (not parse errors), switch to `AI_FALLBACK_PROVIDER` for the rest of
the session + retry the current symbol. `_update_ai_health`'s Telegram alert still fires.
nvidia_nim stays primary. Removed the dead `_fallback_openrouter()`/`_fallback_to_openrouter()`
(zero callers). Fixed stale model strings; made `OPENROUTER_MODEL` env-configurable;
`stock_bot/main.py` `_ai_fallback_n` now counts any non-primary provider. Tests: new file `tests/stock/test_ai_failover.py`, 8 cases.
Suite 698→706. **ACTIVATED same day** — user provided a Mistral key; added to root `.env`
`MISTRAL_API_KEY` + `stock_bot/.env` `AI_FALLBACK_PROVIDER=mistral` (both gitignored).
Verified live before enabling: key auth OK, `mistral-small-latest` available, real round-trip
through `_parse()` → BUY 85 / SELL 90 (correct, ~1.2s). Takes effect on next stock-bot
restart. Advisory-layer only — no walk-forward.

**IB Gateway + IBC headless deploy — SCOPED + DOCUMENTED 2026-08-27 (not executed).**
Roadmap item G. Key finding: **no bot code change needed** — `IBKRExecutor` already connects
by host:port and `_LIVE_PORTS`/docstring already cover Gateway. Pure infra. Written:
`deploy/IBKR_GATEWAY_SETUP.md` (Docker path via `gnzsnz/ib-gateway-docker` recommended; native
Gateway+IBC+Xvfb+systemd path as alternative; 2FA handling — paper logins usually have none;
IBKR's forced daily ~23:45 restart handled by IBC's AutoRestartTime + the bot's existing
try_reconnect; verification via `ibkr_smoke.py --port 4002`) and `deploy/stock_bot.service`
(systemd unit, uses an API-port TCP wait as `ExecStartPre` readiness gate rather than a blind
sleep, no `EnvironmentFile=` because `stock_bot/config.py` loads its own `.env` and systemd's
parser chokes on that file's inline comments, MemoryMax 1G since stock bot is heavier).
`deploy/VPS_SETUP.md` got a scope note (it's crypto-only). The only bot edit when executed:
`IBKR_PORT=7497 → 4002` in `stock_bot/.env`. Est. ~4h + a day's observation (Docker path).
Deferred with the rest of VPS migration — crypto bot moves first (real money, no local broker
software). Also flagged in the doc: test yfinance from the VPS IP before committing the stock
bot (datacenter IPs get rate-limited harder by Yahoo — the 2026-08-05 outage class).

Related: [[execution_layer]], [[fee-structure]], [[2026-08-18-missed-buy-signal]]
(that investigation is why the MTF gate's blocked-reason labels exist), [[known-gaps]],
[[multi-symbol-validation]], [[stock-bot]].
