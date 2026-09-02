---
name: known-gaps
description: Known bugs and inconsistencies that are logged but not yet fixed — prevents re-discovering from scratch
metadata:
  type: project
---

Logged 2026-07-01. Updated 2026-07-02 (Session 3 verification).

**Why:** Saves re-investigation time; each took real debugging to surface.
**How to apply:** Check here before assuming a symptom is new.

---

## 1. BUY fills not written to trades.db — RESOLVED 2026-07-02

**Where:** `bot/main.py` — unified execute block (~lines 1391–1408)
**Outcome:** Verified RESOLVED. The unified `trade_log.log_fill(side=order.side.value, ...)` call
at lines 1391–1398 handles both BUY and SELL. `OrderSide.BUY.value == "BUY"` confirmed.
`alerter.fill(side=order.side.value, ...)` is also called for BUY in the same block.
**Note:** The known-gap was written before the unified call existed; code was already correct
by the time of this audit. If trades.db shows only SELLs, check live_comparison.py query filters.

---

## 2. Regime monitor rolling PF omits MIN_EMA_SPREAD_PCT filter — RESOLVED 2026-07-02

**Where:** `regime_monitor.py` — `compute_rolling_pf()` is_buy block
**Fix:** Added `ema_spread_val >= MIN_EMA_SPREAD_PCT` to the is_buy condition.
Also added: when trade_count == 0, PF shows "N/A (no signals in window)" instead of WARN,
and the verdict excludes PF from the pass/fail count, reporting "X/3 measurable conditions met".

---

## 3. live_state.json (no symbol suffix) is dead code — ADDRESSED 2026-07-02

**Where:** `bot/execution/live_executor.py` — `_DEFAULT_STATE_PATH` constant (line ~28)
**Status:** Confirmed no active readers — every LiveExecutor call in bot/main.py passes an
explicit per-symbol path (logs/live_state_BTC_CAD.json etc.). Added comment to live_executor.py
clarifying this is a legacy fallback never reached at runtime. The file logs/live_state.json
(if it exists on disk) is inert — safe to delete.

---

## 5. XRP/CAD walk-forward fails with current strategy — RESOLVED 2026-07-02

**Where:** Live bot `bot/main.py`, config UNIVERSE_WHITELIST
**Symptom:** Walk-forward re-run on XRP/USDT (Binance proxy) with current code (Mode A/B + EMA filter):
  - 5000c PF **0.99** (62 trades, 12.9% win rate, 54/62 exits at SL)
  - 3000c PF **0.98** (31 trades, 12.9% win rate)
  - 1000c PF 1.33 (6 trades — too small to be meaningful)
  Two of three windows fail (PF < 1.0). Walk-forward FAILS for XRP on current strategy.
**Root cause:** Original XRP validation was done on the OLD strategy (simple RSI < 30 BUY gate).
  Current strategy (Mode A/B: pullback RSI 38–58 + breakout) doesn't have edge on XRP.
**Decision (2026-07-02):** XRP/CAD removed from UNIVERSE_WHITELIST and moved to watchlist.
  - `.env`: UNIVERSE_WHITELIST=BTC/CAD, MAX_CONCURRENT_POSITIONS=1, UNIVERSE_SIZE=1, STARTING_CASH=100
  - `regime_monitor.py`: XRP/CAD moved to MONITOR_WATCHLIST (health metrics, labeled NOT TRADED)
  - CLAUDE.md: XRP → WATCHLIST with exact basis text
  - Re-entry gate: full 3-window walk-forward pass on current strategy code

## 4. ATR SL config drift — RESOLVED 2026-07-02

**Where:** `config.py` BacktestConfig, `.env`, `bot/backtest/engine.py`, `backtest.py`
**Root cause:** Two independent ATR SL config systems existed:
  - `StrategyConfig` (live bot): reads `ATR_SL_MULT` — with .env `ATR_SL_MULT=0.0`, live was correct
  - `BacktestConfig` (backtest only): reads `ATR_SL_ENABLED` + `ATR_SL_MULTIPLIER` (not in .env.example)
  With `ATR_SL_ENABLED=true` in .env, backtest ran ATR SL at 2× → 33 trades / PF 2.19 (drift from validated 58 / 1.79)
**Live impact:** 1 fill at 2026-06-22 16:36 UTC under ATR config (pnl=-0.02 CAD, reason='trail_stop')
**Fix applied 2026-07-02:**
  - Removed `atr_sl_enabled`/`atr_sl_multiplier` from BacktestConfig; added `atr_sl_mult` (reads same key as StrategyConfig)
  - Removed `ATR_SL_ENABLED=true` from .env
  - Updated engine.py and backtest.py to use `atr_sl_mult` (0 = disabled convention)
  - Added startup drift guard in config.py: warns on unrecognised strategy-critical env keys
  - CLAUDE.md fingerprint updated: now expect ~39 trades / PF 1.79 (count lower due to EMA spread filter added 2026-06-27)
**Verified:** backtest gives PF 1.79, 12/12 tests pass

---

## 6. trades.db missing BUY fills + fee_cost columns — RESOLVED 2026-07-03

**Where:** `bot/data/trade_log.py`, `bot/execution/executor.py`, `bot/execution/live_executor.py`, `bot/main.py`
**Root cause A (no fee capture):** `trades.db` had no `fee_cost`/`fee_currency` columns. Fee dict was
  logged as WARNING text only — not persisted.
**Root cause B (5 Kraken trades missing):** The 3 original DB rows (2 phantom, 1 ATR trail_stop SELL)
  were missing 6 Kraken fills (3 BUYs + 3 SELLs from Jun 12–Jun 27). Phantom rows had qty=0 because
  executor SELL fired when bot state machine had no position (state machine mismatch, not real fills).
**Fixes applied 2026-07-03:**
  - `executor.py` Order dataclass: added `fee_cost: float = 0.0` and `fee_currency: str = ""` fields
  - `live_executor.py`: Order now carries fee_cost/fee_currency from exchange fill response
  - `trade_log.py`: added `fee_cost`/`fee_currency` columns + in-place migration (ALTER TABLE ADD COLUMN)
  - `bot/main.py`: all 3 log_fill() calls now pass fee_cost/fee_currency from order
  - `shadow_signal.py`: fill fidelity shows `actual_fee=X.XX%` when fee_cost is populated (falls back to `assumed_fee`)
  - `reconcile_ledger.py`: one-shot reconciliation script — fetches Kraken history, marks phantoms, backfills orphan fills
**Kraken reconciliation result (2026-07-03):**
  - True Kraken balance: 154.11 CAD (matches live_state files: 2 × 77.05)
  - True realized P&L: -2.20 CAD (Kraken data, all fees included); DB had recorded -0.02
  - 7 Kraken trades total (all BTC/CAD); XRP/CAD: 0 trades
  - 2 phantom rows marked (qty=0 SELLs fired without position): id=2 (BTC/CAD), id=3 (DOGE/CAD)
  - 6 orphan Kraken trades backfilled with source='kraken_backfill'
  - 1 SELL (Kraken id=TEYLVF, Jun 27 0.000378 BTC) — RESOLVED: came from pre-existing BTC deposit
    to Kraken wallet before the bot's first trade. BTC flow reconciled to 0 unexplained satoshis.
  - Pre-existing assets found: 0.000377 BTC (sold Jun 27), 218 DOGE (sold Jun 30), 0.034 ETH (still held)
  - Funding source: $100 CAD Interac E-Transfer deposit on 2026-06-07
  - Report: logs/reconciliation_20260703.md (updated 2026-07-03 Session 4)

## 7. 58% bot downtime — DIAGNOSED + MITIGATED 2026-07-03

**Root cause:** Bot was running locally on Mac via caffeinate. Mac sleep and manual stops
  between dev sessions caused all downtime. No automatic restart mechanism on Mac.
**Systemd fix (deploy/trade_bot.service):**
  - `Restart=on-failure` → `Restart=always`
  - `StartLimitIntervalSec=300` + `StartLimitBurst=5` → `StartLimitIntervalSec=0` (never give up)
  - With old config: systemd permanently stopped restarting after 5 crashes in 5 min with no alert.
**In-process crash-loop detection added (bot/main.py):**
  - `_record_startup_and_check_crash_loop()`: writes startup timestamps to logs/startup_timestamps.txt
  - Fires `alerter.error()` if 3+ restarts in 5 minutes
**Candle watchdog extracted to `_check_candle_watchdog()` helper (bot/main.py):**
  - Unit-testable with mocked clock
  - 5 new tests in test_candle_watchdog.py — all PASS
**deploy/UPTIME.md written:** How to check uptime, what each alert means, systemd commands.
**Resolution:** Only full fix is VPS deployment with systemd service. Mac-local running will always
  have downtime from sleep/manual stops.
**Tests:** 114/114 PASS (was 109 before this session)

## 8. qty=0 phantom SELL fills + fill-confirmation drift — RESOLVED 2026-07-03

**Root cause (BUG 1 — phantom rows):** In `live_executor.execute()`, a SELL market order's
  immediate Kraken response returns `filled=0` before settlement. The poll loop uses
  `float(last_raw.get("filled") or filled_qty)` which keeps `filled_qty=0` if Kraken always
  returns 0 in the filled field (even on a closed order). This propagated `quantity=0` to
  `Order.quantity`, then `on_sell(price, 0)` was a no-op in PositionManager, and
  `log_fill(quantity=0)` wrote a phantom row to trades.db.
**Fix (BUG 1):**
  - `bot/execution/live_executor.py`: After poll loop, if `side==SELL and quantity<=0`:
    - If `status=="closed"` and `amount>0`: use `amount` as fill qty (closed market SELL = fully filled)
    - If not closed or amount=0: log error and return None — do not create an Order
  - `bot/data/trade_log.py`: `log_fill(quantity<=0)` raises ValueError — prevents phantom rows at the DB layer
**Root cause (BUG 2 — drift never resolves):** After the phantom SELL (qty=0), PositionManager
  showed pos=0.000378 while exchange had 0. Drift check every 120 ticks called `alerter.error()`
  on EVERY detection — firing for hours with no escalation logic.
**Fix (BUG 2):**
  - `config.py`: Added `drift_alert_threshold: int = 3` to ExchangeConfig (env `DRIFT_ALERT_THRESHOLD`)
  - `bot/main.py`: Added `_drift_consecutive_count`. Drift warnings fire immediately; `alerter.error()`
    only after N consecutive drift checks (then counter resets). Drift resolution logs info + resets counter.
**Tests:** 11 new tests — all PASS. `test_fill_recording.py` (5 tests), `test_drift_escalation.py` (6 tests)
  Total suite: 110 PASS.

## 9. Execution/risk-layer audit findings — 3 RESOLVED 2026-07-28, 2 deferred

Full line-by-line review of `live_executor.py`, `risk_manager.py`, `retry.py`, and the
`bot/main.py` call sites. Three issues fixed same-day; two deferred to a separate pass.

**Issue A — RESOLVED 2026-07-28: cancel-race double-fill risk in limit chase**
- **Where:** `bot/execution/live_executor.py` `_place_limit_order()`, cancel-timeout branch (~510-548)
- **Bug:** After a limit order timed out and was cancelled, the code verified the order's
  fate via `fetch_order` before re-placing — but only aborted the retry if `cancel_order`
  itself had failed. If `cancel_order` succeeded and the *verification* `fetch_order` call
  then failed (a second, independent network blip), the code fell through and placed a
  brand-new order for the same quantity without ever confirming whether the cancelled order
  caught a fill in the race window — real double-fill risk on the live `ORDER_TYPE=limit`
  path (used for BUY entries and non-urgent strategy SELLs).
- **Fix:** `except Exception as post_exc` now always aborts the chase (`return raw`)
  regardless of `cancel_ok` — any unverifiable post-cancel state is treated as "may still be
  live," same as the failed-cancel case, and left for `execute()`'s poll loop to settle.
- **Tests:** no new test added (would require simulating two independent mocked failures in
  the same retry loop); existing `test_limit_chase_recovery.py` suite still passes unchanged.

**Issue B — RESOLVED 2026-07-28: order rejections never reached Telegram**
- **Where:** `bot/main.py` execute block, rejected-order branch (~1905-1908)
- **Bug:** `display.reject(...)` only printed to console. `ccxt.InsufficientFunds`, exchange
  minimum-size violations, and generic `ccxt.BaseError` rejections — including a rejected
  SL/TP exit — produced zero Telegram signal, contradicting the "full alert coverage" state
  from [[project_alerting_gap_and_heartbeat_2026-07-17]].
- **Fix:** added `alerter.error(f"ORDER REJECTED [{sym}] {order.side.value}: {reason}")`
  alongside `display.reject(...)`, covering both BUY and SELL rejections.
- **Note (not fixed, flagged for a future pass):** while editing this branch, found a
  pre-existing latent bug a few lines up — if `order.status == FILLED and order.quantity <= 0`
  the code sets `order = None`, and the subsequent `else: display.reject(order.reject_reason...)`
  would then call `.reject_reason` on `None` and raise `AttributeError`. Only reachable if
  `LiveExecutor.execute()` ever returns a FILLED order with qty<=0 — the internal qty=0 guard
  (see gap #8 above) is supposed to prevent this by returning `None` from `execute()` instead,
  so the crash path may be unreachable in practice, but the two guards are inconsistent.
  Left untouched per explicit scope instruction.

**Issue 3 — RESOLVED 2026-07-28: startup balance/position sync had no retry, no alert**
- **Where:** `bot/execution/live_executor.py` `_sync_cash()` (~157) and `_sync_position()` (~193)
- **Bug:** Both call `self._exchange.fetch_balance()` directly with no retry, unlike every
  other exchange call hardened in the 2026-07-24 retry pass (order book, candle, ticker —
  see [[project_rescreen_and_crypto_research_2026-07-16]] context / CLAUDE.md operational
  status). A single transient blip on these two calls silently degrades to `starting_cash`
  fallback / stale on-disk state, console-print only — and `_sync_cash`'s result on the
  *first* executor becomes `capital_pool.total_capital` (`bot/main.py` `_pool_total =
  _first_exec.cash`), so a startup blip could mis-size every symbol's slot.
- **Fix:** both calls now go through the existing `fetch_with_retry` import (3 attempts,
  2s delay, matching the retry.py default). On persistent failure after retries, both methods
  now call a new `self._alerter.error(...)` (a `TelegramAlerter` instance constructed in
  `LiveExecutor.__init__` from `cfg.alerts.*`, since `LiveExecutor` is built before `main.py`'s
  own `alerter` exists) in addition to the existing console FALLBACK print.
- **Test suite side-effect:** `test_sync_cash_falls_back_on_error` (`test_live_executor.py`)
  exercises the failure path and was now hitting `fetch_with_retry`'s real `time.sleep()`
  between attempts (~8s added, suite went 6s→14s). Fixed by patching
  `bot.exchanges.retry.time.sleep` in that test, same pattern already used in
  `test_kraken_retry.py`. Suite back to ~6s.

**Deferred (explicitly out of scope for this pass) — BOTH RESOLVED, see gap #14 below:**
- Config-documentation gaps (`RISK_MAX_POSITION_PCT`, `RISK_DAILY_LOSS_LIMIT`,
  `RISK_MAX_DRAWDOWN`, `RISK_MAX_TRADES_PER_DAY`, `COOLDOWN_TICKS`, `RISK_HALT_BLOCKS_STOPS`
  live in `.env`/`config.py` but absent from CLAUDE.md's config tables). ~~Not touched.~~
  Turned out already documented by the time gap #14 checked (2026-07-29) — no action needed.
- Fee-currency-mismatch silent cash drift (`live_executor.py` ~852-858, warning-only, no
  alert). ~~Not touched — lower severity, no capital-pool sizing impact.~~ Fixed 2026-07-29:
  now also fires `alerter.error(...)`, with test coverage
  (`test_fee_currency_mismatch_alerts_telegram`).
  **2026-08-19: this note itself was stale for three weeks after the fix landed** — gap #14
  said as much at the time ("the Deferred note inside this gap's own item #9 text body...
  was just never cleaned up") but nobody actually edited these two bullets until now.
  Corrected while auditing "what's missing" from a fresh session with no memory of gap #14's
  existence — a reminder that a forward-pointing note in the resolving entry isn't enough;
  the original entry needs the strikethrough too, or a stale "Not touched" here will keep
  reading as an open gap to anyone who doesn't also find #14.

**Strategy hash:** unchanged, `659d1c03987b72fd` — confirmed via
`bot/strategy/fingerprint.compute_strategy_hash()` after all three fixes (execution/risk
files only, no `bot/strategy/*` touched).
**Tests:** 328/328 PASS after each of the three changes; suite runtime back to ~6.5s.

## 10. Issue 2 (cost_basis=0.0 silent fallback) + None.reject_reason crash — RESOLVED 2026-07-28

Follow-up pass on the two items explicitly deferred from gap #9.

**Issue 2 — `_sync_position` reseed branch silently zeroed cost_basis on fetch failure**
- **Where:** `bot/execution/live_executor.py`, "Bot opened this position on a prior run —
  reseed cost_basis" branch (originally ~264-268, now ~286-315 after the gap-#9 line shift)
- **Bug:** `except Exception: current_price = 0.0` had zero logging (the only silent handler
  in the file) and wrote a fabricated `_cost_basis = 0.0`. The next SELL would then compute
  `pnl = (fill_price - 0.0) * quantity`, overstating realized P&L by the full sale proceeds.
- **Fix:** split into `try/except Exception as exc/else`. On failure: log a warning naming
  the exchange error and the saved `cost_basis` value being left in place, print a matching
  console line ("POSITION RESEED SKIPPED... verify manually"), and do **not** write to
  `_portfolio._cost_basis` at all — it stays whatever `_load_state()` restored from disk
  rather than being overwritten with a wrong number. On success, reseed as before (unchanged
  behavior, now inside the `else` clause).
  Confirmed `_sync_position` is called exactly once, from `__init__` at startup — there is no
  "current tick" price in scope in the caller to fall back to (it's not part of the per-tick
  loop), so per the audit instruction the reseed is skipped entirely on failure rather than
  substituting an approximate price.

**None.reject_reason crash — bot/main.py rejected-order branch**
- **Where:** `bot/main.py` execute block (~1826-1910)
- **Bug:** flagged in gap #9. `order` is reassigned to `None` a few lines up when
  `order.status == FILLED and order.quantity <= 0` (the qty=0-after-fill guard). The
  subsequent `else: display.reject(order.reject_reason...)` — and the gap-#9
  `alerter.error(...)` added alongside it — would then raise `AttributeError` on
  `None.reject_reason` if that guard ever actually fires (believed unreachable in practice
  since `LiveExecutor.execute()`'s internal qty=0 guard is supposed to return `None` before
  reaching this point, but the two guards were inconsistent).
- **Fix:** both `display.reject(...)` and `alerter.error(...)` now read from
  `_reject_reason = order.reject_reason if order else "internal: FILLED order returned
  qty<=0 — see log for detail"` and `_reject_side = order.side.value if order else
  final_signal.value` (the signal already in scope from the execute() call). A future
  qty<=0 edge case now alerts cleanly with a diagnosable message instead of crashing the
  trading loop.

**Strategy hash:** unchanged, `659d1c03987b72fd` — confirmed via
`bot/strategy/fingerprint.compute_strategy_hash()` (execution-layer files only, no
`bot/strategy/*` touched).
**Tests:** 328/328 PASS.

## 11. Stock bot: daily-loss breaker staleness, apparent stall, silent cycle failure — RESOLVED 2026-07-28

Three related stock-bot findings from one session: a real bug (fixed + tested), an
operational incident that turned out to most likely be a misdiagnosis (corrected), and a
genuine observability gap (fixed).

**Bug — RESOLVED: daily-loss breaker used a stale position mark between fills**
- **Where:** `stock_bot/execution/paper.py` `StockPaperExecutor._open_position_value`
- **Root cause:** only refreshed inside `buy()`/`sell()` at fill time. Between fills — which
  can be days apart on this book's cadence — `_is_daily_loss_tripped()` checked drawdown
  against whatever price was current at the last fill, so a held position that moved
  significantly with **no new fill** was invisible to the breaker. (Distinct from the
  2026-07-04 cash-only-baseline bug in `test_stock_breaker.py`'s docstring — that one was
  already fixed; this is a second, narrower gap in the same breaker.)
- **Fix:** added `StockPaperExecutor.refresh_position_marks(prices)` (thin wrapper over the
  existing `_update_position_value`) and a new module-level `_mark_positions_to_market(executor,
  price_data)` in `stock_bot/main.py`, called once per scan cycle right after Phase 1 prices
  are fetched and before any buy/sell decision runs. `IBKRExecutor` gets a no-op
  `refresh_position_marks()` for interface parity (its breaker already marks live via
  `_net_liquidation()`).
- **Verified the price value feeding this is already sanity-checked**, not raw feed output:
  `price_data[sym]["price"]` is `fetch_candles()`'s validated `closes[-1]` — bounds checks,
  `_is_duplicate_price()` (holiday-corruption detection), outlier-vs-batch-median check, and
  (`.TO` only) a fast_info currency-mismatch cross-check all run inside `fetch_candles()`
  before it can return non-`None`. A rejected price yields `price_data[sym] = None`, which
  `_mark_positions_to_market`'s filter skips — degrades to "stale one more cycle," never to
  "corrupted number used." The separate `price_sanity_pct` guard (`main.py` ~1183) validates
  a *different*, later-fetched `live_price` against this same already-validated candle close
  — it doesn't gate the mark-to-market value at all, so there's no ordering issue.
- **Tests:** `test_stock_position_mark_refresh.py` (4 new) — imports and calls the real
  `_mark_positions_to_market()` from `stock_bot.main` (not a reimplementation) via a mocked
  `_fetch_symbol_data`, proves the breaker trips from a price move alone with zero
  `buy()`/`sell()` calls, plus a source-inspection test guarding that `run()` still wires the
  call up (the other 3 tests wouldn't catch someone deleting just the call site). Verified
  both failure modes for real by temporarily reverting each half of the fix and confirming
  the corresponding test fails, then restoring.

**Operational — apparent ~6h scan-loop stall, restarted, root cause probably misdiagnosed**
- **Symptom:** stock bot (PID 95757, alive since prior Monday) showed no `__main__` scan-cycle
  log activity in `logs/stock_bot.log` from `15:59:32` to `~21:51` — no `"Alerts: N triggered
  this cycle"`, `stock_dashboard.html` mtime frozen at `15:59`, file byte-identical across an
  8s window — while `ib_async.wrapper`'s IBKR portfolio-update ping kept firing every ~3min,
  keeping `ps` showing it as healthy.
- **Investigated:** `sample`/`lsof` showed the main thread genuinely in `time.sleep()` (not
  deadlocked), 59 sockets to `*.ycpi.vip.dca.yahoo.com` stuck in `CLOSE_WAIT`. Restarted
  (new PID 25877) rather than debug further live, given the ambiguity.
- **Correction (caught one turn later, before over-reacting further):** `15:59:32` lines up
  almost exactly with NYSE close (4:00pm ET). `AFTER_HOURS` mode's loop body
  (`_run_news_scan()` + `time.sleep(1800)` + `continue`) never touches yfinance and only logs
  at `debug` level in its per-symbol failure path — below the file handler's INFO threshold —
  so hours of file-log silence after market close is likely **normal, by-design behavior**,
  not a hang. The 59 `CLOSE_WAIT` sockets most likely accumulated over the full LIVE trading
  day's yfinance call volume (hundreds+ calls), not during a silent stall. Session-audit
  conclusion (below) supports "no bug found" on the socket-leak side specifically.
  **Not fully resolved either way** — the restart means the original process can't be
  re-examined; flagging so a future occurrence isn't immediately assumed benign either.

**Observability gap — RESOLVED: total fetch failure was silent**
- **Where:** `stock_bot/main.py`, Phase 1 price-fetch block in `run()`
- **Root cause:** per-symbol fetch failures were already logged
  (`"Price fetch failed for %s: %s"`), but two cycle-level failure modes had no signal at
  all: the fetch phase itself raising (orchestration failure, not per-symbol), and a "clean"
  completion where literally every symbol failed (total outage / global rate limit) — both
  left the loop silently finishing an empty cycle and going back to sleep, undetectable
  without `lsof`/`sample`.
- **Fix:** wrapped the Phase 1 block in a try/except logging
  `"cycle %d failed: price-fetch phase raised %s: %s"` on an orchestration exception, and
  added a check logging `"cycle %d failed: 0/%d symbols returned data — likely a total fetch
  outage"` when every symbol returns `None`. Both `continue` to the next iteration (mirrors
  the existing `PRE_MARKET`/`AFTER_HOURS`/`WEEKEND` `sleep+continue` pattern) instead of
  silently completing the cycle.

**Session-audit finding (no fix needed):** confirmed `stock_bot/data/price_feed.py` never
creates or holds a yfinance session — all 3 call sites (`yf.Ticker(sym).info`,
`yf.download(...)`, `yf.Ticker(symbol).fast_info`) use yfinance's own default session
management, no `session=` passed anywhere, consistent with the hard rule in
`.memory/core.md` #6 ("Never add session management to yfinance") and enforced by yfinance
1.5.1 itself (raises if you try). If the `CLOSE_WAIT` leak recurs, it's inside
yfinance/curl_cffi's own connection lifecycle, not fixable from this codebase without
violating that rule — a safe mitigation if it becomes a real problem would be an explicit
`gc.collect()` once per Phase 1 batch (not implemented — not asked for, and unconfirmed the
leak is actually a live problem rather than one day's normal call volume).

**Tests:** 332/332 PASS (328 → 332, the 4 new tests above). No strategy files touched.

## 12. Swing book ATR-stop research — FAILED 2026-07-28, corrects the 2026-07-22 diagnosis

**Not a bug — a pre-registered research result.** Logged here (not just
`CLAUDE_HISTORY.md`) because it corrects a prior conclusion and carries a standing
constraint on future work in this area.

**Background:** the swing book (`stock_bot/fast_validator.py`, 1h candles) was retired
2026-07-22 at combined PF 0.76 across 394 trades, 64.0% SL-exit rate. That entry concluded
the fixed 1.5% stop was "too tight for hourly noise... not the AI-trigger architecture" —
but no stop-mechanism fix had actually been tested at the time; the conclusion was inferred
from the SL-exit-rate shape matching crypto's 1h day-trading failure, not verified directly.

**Tested 2026-07-28:** pre-registered experiment (`swing_atr_walkforward.py`, hypothesis +
4 pass criteria committed before running anything, same 7 symbols, real
data-availability check before choosing the IS/OOS split) — ATR×2.0 stop (the exact fix
that worked for BTC 2026-07-17), run once, no grid search. **FAILED all 4 criteria:**
combined PF 0.54 in-sample / 0.35 OOS (worse than the 0.76 baseline, not better), 0/7 and
1/7 symbols passing, SL-exit rate 53.0%/56.2% (barely moved from 64%, still fails the <50%
bar). Full table and reasoning: `CLAUDE_HISTORY.md` "Swing book ATR-stop research
(2026-07-28)".

**Correction:** the 2026-07-22 "not the AI-trigger architecture" conclusion is now flagged
as unconfirmed / possibly wrong. Widening the stop made things worse with no win-rate
improvement (RY 21.4%, AMZN 37.5% in-sample) — that pattern points toward the entry signal
(Mode A/B on 1h candles) lacking edge, not stop distance being the problem.

**Standing note — applies to any future swing-book work:** do not re-attempt a
stop-mechanism fix (ATR or otherwise) for the swing book without first testing entry-signal
edge independent of exit rules. A fixed-SL/fixed-TP (or same-candle-close) isolation test
that measures whether Mode A/B's raw BUY/SELL timing has any edge on 1h stock candles,
before touching the stop mechanism again, is the correct next step if this is revisited —
not another stop-multiplier sweep.

**What this doesn't change:** swing book stays retired (`FAST_ENABLED=false`,
`stock_bot/.env` untouched by the experiment). No live code touched
(`stock_bot/fast_validator.py`, `stock_bot/backtest/engine.py` both untouched — the ATR
variant is a standalone copy in `swing_atr_walkforward.py`). Suite unaffected, 332/332.

## 13. Test suite firing real Telegram alerts — RESOLVED 2026-07-29 (was investigated as a possible security incident — it wasn't one)

**Read this before re-investigating "unexplained Telegram alerts" or a "second bot
instance" as a security scare.** This is exactly that symptom, fully explained and fixed.

**Symptom:** two Telegram alerts (balance/position sync failures, fallback to
`starting_cash`) arrived 16 minutes apart with no matching entry in
`logs/startup_timestamps.txt`, no matching `bot.main` process restart, and no trace in
`logs/trade_bot.log`. Investigated as a possible rogue process / leaked API key
(`screen`/`tmux`/`cron`/`launchctl`/duplicate `.env` files/shell history — all came back
clean; see session transcript 2026-07-29 for the full sweep).

**Root cause:** `bot/execution/live_executor.py`'s `LiveExecutor.__init__()` builds
`self._alerter = TelegramAlerter(cfg.alerts.telegram_bot_token, cfg.alerts.telegram_chat_id,
enabled=cfg.alerts.telegram_enabled)` from the real module-level `cfg` singleton (added as
part of gap #9's retry/alert hardening). `TELEGRAM_ENABLED=true` in the real `.env`. Four
test files construct real `LiveExecutor` instances without ever mocking Telegram:
`test_live_executor.py`, `test_external_holdings.py`, `test_limit_chase_recovery.py`,
`test_fill_recording.py`. `test_live_executor.py::test_sync_cash_falls_back_on_error`
deliberately makes `fetch_balance()` raise to test the fallback path — which also hits the
gap-#9 `self._alerter.error(...)` calls in both `_sync_cash()` and `_sync_position()`.
`TelegramAlerter._send_async()` spawns a daemon thread that does a real
`requests.post("https://api.telegram.org/bot<TOKEN>/sendMessage", ...)` — fire-and-forget,
never raises, so the test itself never fails or shows anything unusual.

**Why it left no trace anywhere we looked:** pytest never calls `bot.main.run()`, so
`_setup_logging()`'s `RotatingFileHandler` (pointed at `logs/trade_bot.log`) is never
attached — these `LiveExecutor` instances log through pytest's own capture, not the real
log file. And a pytest worker process exits within seconds of the suite finishing, long
before anyone runs `ps aux` — no new PID, no restart recorded in
`startup_timestamps.txt` (that's only written by `bot.main.run()`'s crash-loop detector).
Every full-suite run after gap #9 landed (roughly 8-10+ runs this session) very likely sent
2 real alerts.

**Fix 1 — can't recur, any test, ever:** new repo-root `conftest.py`, autouse
session-wide fixture patching `TelegramAlerter._send` (the one method that calls
`requests.post`) to a no-op for every test. Chosen over patching the whole class so that
`test_crash_hardening.py` and `test_crypto_telegram.py` — which deliberately test
`TelegramAlerter`'s own formatting/dispatch logic via `patch.object(instance, "_send")` —
keep working unchanged; an instance-level patch shadows the class-level default for the
duration of their own `with` block.

**Fix 2 — leaked tmp directories:** the same 4 `LiveExecutor`-constructing test files used
`tempfile.mkdtemp()` for their state-file sandbox, which never auto-cleans (unlike
`tempfile.TemporaryDirectory()`). 618 leftover `.../T/tmpXXXXXXXX/live_state_BTC_CAD.json`
directories had accumulated across this session's many full-suite runs — this volume is
what made the investigation take "duplicate file on disk" seriously as a possible
second-checkout scenario. Converted all 4 files' helper functions
(`_make_executor`, `_make_live_executor` ×2, `_make_live_executor_with_position`) to accept
pytest's built-in `tmp_path` fixture instead of calling `mkdtemp()` themselves — every
calling test function now passes its own `tmp_path` through. `tmp_path` is auto-cleaned by
pytest's own retention policy (keeps the last few `pytest-N` sessions, prunes older ones
automatically) — verified post-fix: the 19 test functions across these 4 files now produce
directories under `pytest-of-<user>/pytest-N/<testname>0/`, not raw `/T/tmpXXXXXXXX/`.
One-time cleanup of the pre-existing 306 verified-orphaned raw-mkdtemp directories (each
confirmed to contain nothing but the one expected json file before deletion) — removed,
0 failures. The real `logs/live_state_BTC_CAD.json` is untouched (different code path).

**Not fixed (named scope was these 4 files only) — same `mkdtemp()` gap still present in:**
`test_fast_validator_exits.py` (`fast_trades.csv` sandbox), `test_paper_report.py` (5
instances), `test_live_executor.py:57`'s own `_make()` default `state_path` (filename
`live_state.json`, no `BTC_CAD` suffix — this one doesn't construct a real `TelegramAlerter`
risk since `_make()` isn't used by the risky fallback test, but still leaks a directory per
call). None of these produce the `live_state_BTC_CAD.json` filename this investigation
searched for, so they weren't part of what made this look like a duplicate checkout — but
they're the same class of leak and worth the same fix in a future pass.

**Verification:** full suite 332/332 pass after both fixes. Strategy hash unchanged,
`659d1c03987b72fd` (test-file and `conftest.py` changes only, no `bot/strategy/*` touched).

## 14. Follow-up pass on remaining deferred items — RESOLVED/CONFIRMED 2026-07-29

Worked through the full deferred list from gap #9 and the feature_plan tech-debt table,
one item at a time, full suite + hash check after each (per the "one change at a time"
project rule). Six items; four already-clean, two real fixes.

**Item — "5 pre-existing failing crypto tests" — STALE, not a real gap**
`test_halt_blocks_all_signals`, `test_fetch_order_polling_timeout_uses_partial_fill`,
`test_state_save_load_roundtrip`, `test_sync_cash_uses_exchange_free_balance`,
`test_restart_recovery_seeds_position_manager_and_state_machine` (the last one had been
renamed with a `_and_state_machine` suffix since the tech-debt note was written, which is
likely why it looked unresolved). All 5 pass individually and as part of the full suite —
confirmed twice this session. The `feature_plan.md` deferred-table entry was simply never
cleaned up after whatever earlier session actually fixed this; removed below.

**Item — mkdtemp() cleanup finished — RESOLVED**
`test_fast_validator_exits.py` (1 instance), `test_paper_report.py` (5 instances),
`test_live_executor.py`'s `_make()` default `state_path` (1 instance) — all converted to
pytest's `tmp_path` fixture, same pattern as gap #13. `_make()` and `_make_validator()` now
take `tmp_path` (optional/positional respectively) instead of calling `tempfile.mkdtemp()`
directly.
**Side effect caught and fixed:** three of these files (`test_live_executor.py`,
`test_paper_report.py`, `test_fast_validator_exits.py`) have legacy `if __name__ ==
"__main__":` manual-runner blocks (not used by pytest, not referenced anywhere else in the
repo, but present in 13 test files as a repo-wide convention) that called the now-`tmp_path`-
requiring functions with no arguments — this would have silently broken direct invocation
(`python test_X.py`). Fixed by building a throwaway `pathlib.Path(tempfile.mkdtemp())` in
each `__main__` block and passing it through, with manual cleanup via `shutil.rmtree` after
each test (the pytest auto-clean benefit doesn't apply outside pytest, but at least direct
invocation still runs and still cleans up after itself). One subtlety: for
`@patch`-decorated tests, the `tmp_path` substitute must be passed as a **keyword** arg
(`t(tmp_path=fake_tmp_path)`), not positional — `unittest.mock.patch`'s wrapper appends its
own injected mocks *after* whatever positional args the caller supplies, so a positional
`tmp_path` lands in the wrong (`mock_cfg`) parameter slot and raises `AttributeError`.
Confirmed via `test_fetch_order_polling_resolves_on_close` failing this way, then fixed.
**Verified:** all three files' pytest suites pass, and all three manual `__main__` runners
(`python test_live_executor.py`, `python test_paper_report.py`,
`python test_fast_validator_exits.py`) also run clean end-to-end.

**Item — fee-currency-mismatch alert — RESOLVED**
`bot/execution/live_executor.py`, the `fee_currency != quote` branch (~line 892, inside
`execute()`'s fee-deduction block). Was warning-only. Added `self._alerter.error(...)`
alongside the existing `logger.warning(...)`, same pattern as every other hardened alert
path from gap #9 — `self._alerter` was already built in `__init__` for exactly this
purpose. New test `test_fee_currency_mismatch_alerts_telegram` in `test_live_executor.py`
patches `ex._alerter.error` and asserts it fires with the fee/currency/quote detail on a
BTC-denominated-fee fill.

**Item — undocumented risk-gate .env keys — ALREADY DONE, no action needed**
`RISK_MAX_POSITION_PCT`, `RISK_DAILY_LOSS_LIMIT`, `RISK_MAX_DRAWDOWN`,
`RISK_MAX_TRADES_PER_DAY`, `COOLDOWN_TICKS`, `RISK_HALT_BLOCKS_STOPS` are all already
documented in `CLAUDE.md`'s "Risk-gate config" section (values, defaults, one-line
description each). This matches what `feature_plan.md`'s 2026-07-28 "Crypto Execution/Risk
Audit" entry already said was done — the "Deferred" note inside this gap's own item #9 text
body (above) was just never cleaned up after the fact. No CLAUDE.md change needed this pass.

**Item — None.reject_reason crash path — CONFIRMED provably unreachable, no code change**
Traced `LiveExecutor.execute()` (`bot/execution/live_executor.py:597-917`) fully. The one
place that can produce `quantity<=0` after a fill attempt (the `if quantity <= 0:` block at
~line 784) has every branch either recover a positive quantity or explicitly `return None`
— there is no fall-through to the `Order(status=FILLED, ...)` construction with
`quantity<=0`. The dry-run path is separately guarded upstream (BUY quantity≤0 and
SELL-with-no-position both `return None` before dry-run fill logic runs). So
`bot/main.py`'s `order.status == FILLED and order.quantity <= 0` branch (~line 1829) is
checking a state `execute()` can never actually produce. The gap-#10 None-safe
`reject_reason`/`side` fallback stays in place as cheap defense-in-depth against a future
change to `execute()` breaking this invariant — not because it's currently reachable.

**Item — FX sizing quirk (stock bot) — RESOLVED 2026-07-31, further hardened 2026-08-05**
Full detail of the original quirk: `CLAUDE_HISTORY.md` 2026-07-17 entry ("Known FX sizing
quirk"). This entry previously said "no commits have touched `check_exposure()`... since
2026-07-17" — that was true when written but is now stale. Two rounds of real fixes have
landed since:
- **2026-07-31 (`e8844e6`, "Implement USD/CAD conversion for pricing and exposure
  calculations"):** `check_exposure()` in both `stock_bot/execution/paper.py` and
  `ibkr.py` now converts non-CAD symbol prices through `get_usd_cad_rate()`
  (`_price_in_cad()`) before comparing against `PAPER_MAX_EXPOSURE_PCT` — the "same
  unconverted USD price basis" this entry described is gone. Found live the same day on
  RY: a $842 USD spend was being sized against a CAD target as if $1 USD == $1 CAD,
  running ~23% actual exposure against an intended 20%.
- **2026-08-05 (this session, punch-list item #7):** `check_exposure()` gained an optional
  `pending_trade_value` parameter — the FX-converted fix above still only checked exposure
  *before* a trade, not projected after it, so one oversized single BUY could still blow
  past the cap in one shot (caught the *next* attempt, not that one). `stock_bot/main.py`
  now computes the target allocation before the exposure gate and passes it through. See
  `test_fx_sizing.py`/`test_ibkr_executor.py`'s `check_exposure_*pending_trade_value*` tests.

No further action needed here — both the original quirk and the follow-on precision gap
are closed and tested.

**Strategy hash:** unchanged, `659d1c03987b72fd`, confirmed after every one of the above
changes (only `bot/execution/live_executor.py` and test files touched — no `bot/strategy/*`).
**Tests:** 332 → 333 (one new test, `test_fee_currency_mismatch_alerts_telegram`). Final
count: **333/333 PASS**.

## 15. validate_symbol.py hand-listed engine.run() kwargs — same drift class as macd_enabled/Mode A/B — RESOLVED 2026-07-30

**Where:** `validate_symbol.py`'s `_CFG` dict (the script's "Validated strategy config —
do not change without re-running validation" block).

**This is the third occurrence of the exact bug class `bot/backtest/params.py`'s docstring
was written to prevent** (macd_enabled drift 2026-07-20, seven Mode A/B entry-param drift,
same day). `validate_symbol.py` was never converted when `engine_kwargs_from_cfg()` was
introduced — it kept hand-listing its own snapshot of the strategy config, silently
missing two live changes:
- `macd_enabled=True` — live (and the canonical fingerprint) since 2026-07-20; the script
  hardcoded `False`.
- `ATR_SL_MULT=2.0` + ATR sizing — live since 2026-07-17; the script never passed
  `atr_sl_mult`/`atr_risk_sizing` at all, so it silently ran the old fixed-1.5%-SL-only
  model.

**Real-world impact — not theoretical:** every multi-symbol screen this script had ever
produced (including the 2026-07-30 batch earlier the same day, see
`.memory/feature_plan.md`) validated candidates against a stricter/older strategy shape
than what actually trades. Rescreening the same 10 symbols after the fix
(`logs/multi_symbol_rescreen_20260730.md`) produced **3 verdict-category flips**: SOL and
ATOM went BLOCKED→WATCHLIST (PF materially improved with the ATR stop + MACD gate), and —
notably — **LINK went WATCHLIST→BLOCKED**, reversing a conclusion reached earlier the same
session that LINK's old "permanently excluded" verdict was stale and worth revisiting. That
conclusion had itself been produced by the same broken script; on the corrected config LINK
does not clear the bar. Two more symbols (POL, UNI) stayed BLOCKED but moved from clear
fails to near-misses. This is exactly the "one .env edit away from repeating the incident
undetected" risk `params.py`'s docstring warns about, except here it wasn't an .env edit —
it was a second script simply never having been migrated in the first place.

**Fix:** `validate_symbol.py` now calls `engine_kwargs_from_cfg(cfg)` (same shared builder
`backtest.py`/`walkforward.py` use) inside `run_backtest_window()`, overriding only
`symbol`/`timeframe` (per-candidate, not from `.env`) and `strategy_mode="indicator"`
(pinned regardless of whatever `cfg.strategy.mode` happens to be). The old 33-line hardcoded
`_CFG` dict is gone; the CLI banner and Binance-fetch section now read `cfg.*` directly
(plus two script-local constants, `_CANDLES_FULL` and the liquidity-gate thresholds, which
aren't strategy params and correctly stay local). Banner now also displays MACD/ATR status
so the config actually being tested is visible, matching `walkforward.py`'s
`_config_summary()` convention.

**Side effect, disclosed not hidden:** full builder adoption also changed `starting_cash`
from a hardcoded $10,000 research baseline to `cfg.portfolio.starting_cash` (live, $100) —
matching what `walkforward.py`/`atr_walkforward.py` already use. No verdict in the rescreen
batch appeared to hinge on this, but it's a real behavior change from the fix, not just the
MACD/ATR keys, and is called out in the rescreen report.

**Guard against a fourth recurrence:** `test_engine_params.py::test_validation_scripts_use_the_builder()`
now checks `validate_symbol.py` alongside `backtest.py`/`walkforward.py` — asserts
`engine_kwargs_from_cfg` appears in its source. No new test function needed; extended the
existing loop.

**Strategy hash:** unchanged, `659d1c03987b72fd` (`validate_symbol.py` and
`test_engine_params.py` only — no `bot/strategy/*`, `.env`, or live-trading path touched).
**Tests:** 344/344 PASS (no new test count change — existing parity test extended, not a
new test added).

## 16. Housekeeping — stray empty root-level trades.db, RESOLVED 2026-07-30

A 0-byte `trades.db` at the repo root (distinct from the real `logs/trades.db` every
reader — `bot/data/trade_log.py`, `live_stats.py`, `reconcile_ledger.py`,
`deploy/smoke_check.py` — actually points at) was found during a live-status check. Origin:
`git log` shows it was accidentally swept into commit `b356a93` ("Add grid stress test
script and unit tests for validation," 2026-07-30) as a committed empty blob — likely a
`sqlite3.connect('trades.db')` diagnostic one-liner run from repo root created it
untracked, then a broad `git add` picked it up alongside that session's real work. Verified
0 rows / no `fills` table, verified no code references the bare root path (only
`logs/trades.db` and pytest `tmpdir` fixtures in tests), then `rm`'d. Left unstaged — it
was git-tracked, so the deletion needs its own `git add`/commit to persist; not done here
per this project's "user handles git" convention. `.gitignore` covers `logs/trades.db` via
the `logs/` pattern but has no bare root-level `trades.db` rule, so this could quietly
reappear the same way — flagged, not added, a one-line judgment call for the user.

## 17. Crypto bot's Telegram sends fail with DNS resolution errors, recurring since 2026-08-05 — symptom FIXED 2026-08-17, root cause still open

**Symptom:** `bot.alerts.telegram` logs `Telegram send error: ... NameResolutionError("...
Failed to resolve 'api.telegram.org' ...")` recurring in `logs/trade_bot.log`. Not a one-off
blip — 1,320 occurrences found on a 2026-08-17 log sweep:
- **2026-08-05, 03:53–16:41 (~13h straight):** failing continuously every ~40s. The crypto
  bot's Telegram channel was effectively dead for over half a day.
- **2026-08-08 through 2026-08-10:** dozens/day, spread across all hours.
- **2026-08-16 onward, ongoing as of 2026-08-18 02:07 (most recent check):** still recurring
  every 15–45 min.

**Key clue toward root cause:** `logs/stock_bot.log` has **zero** occurrences of this exact
error, despite both bots sharing the same `bot.alerts.telegram` module, presumably the same
machine, and overlapping uptime. A genuine system-wide DNS/network outage should hit both
identically — it doesn't. Points toward something specific to the crypto bot process (its
tighter retry/tick cadence catching transient resolver hiccups the stock bot's looser
polling doesn't hit, or a caffeinate/sleep-wake interaction specific to that process) rather
than a general network problem. Not yet investigated further.

**Practical impact:** `TelegramAlerter._send_async()` is fire-and-forget with no retry/queue
on a failed send (see gap #13's description of the same method) — any real alert
(fill, risk-breaker trip, native stop-loss failure, drift alert) that happened to fall in one
of these windows would be silently dropped, no re-send. Checked risk_state.json/
live_state_BTC_CAD.json at the time this was found (2026-08-17 ~18:40 EDT) — 0 fills, no
HALT, no kill-switch trip — so nothing was actually missed by this particular gap so far,
but the exposure is real and ongoing.

**Status:** logged per user decision 2026-08-17 ("just log it, investigate later"), then the
same session the user asked to fix whatever was fixable, so the practical symptom (silent
drop) was closed the same day.

**Fix (2026-08-17):** `bot/alerts/telegram.py`'s `_send()` now wraps its `requests.post` call
in the existing `fetch_with_retry` helper (`bot/exchanges/retry.py` — same 3-attempts/2s-delay
default already used for Kraken price/candle/balance calls and `shadow_signal.py`'s fetch).
A transient DNS blip now gets up to 3 tries (~4s of retry delay) before the send is given up
on; still fails silently (never raises) per the class's existing contract if all 3 fail. Both
`_send_async` (the normal fire-and-forget path, runs in its own daemon thread — the extra
delay doesn't block the trading loop) and `send_now` (the synchronous crash-exit path) go
through this, so a crash alert now also gets retried rather than only getting one shot.
**Tests:** new `test_telegram_retry.py` (3 tests) — healthy send calls `requests.post` once
with no retry, a transient failure recovers on the second attempt, a persistent failure still
degrades to a warning log with no raise after exhausting attempts. Had to capture
`TelegramAlerter._send` at module-import time in the test file (before conftest.py's autouse
`_block_real_telegram_sends` fixture — see gap #13 — monkeypatches it to a no-op) to actually
exercise the real retry logic instead of the safety-net no-op. Full suite 521/521 PASS
(518 → 521), strategy hash unchanged (`659d1c03987b72fd` — only `bot/alerts/telegram.py` and
a test file touched, no `bot/strategy/*`). CLAUDE.md's test manifest and count updated to
match.

**Not fixed — still open:** root cause of *why only the crypto bot's process* hits this DNS
failure (never the stock bot, despite sharing the same alerts module and presumably the same
machine/network) is still unknown. The retry closes the "alert silently dropped" symptom for
short blips but would NOT have saved the 2026-08-05 ~13h continuous outage (retries exhaust in
seconds, the outage lasted hours) — if this recurs at that scale, investigate the underlying
resolver/process-specific cause rather than assuming the retry alone is sufficient.

## 18. Stock-bot daily-loss breaker was session-lifetime, not calendar-day — RESOLVED 2026-08-28

**Where:** `stock_bot/execution/paper.py` + `stock_bot/execution/ibkr.py`, `_is_daily_loss_tripped()`.

**Gap:** the daily-loss circuit breaker (`PAPER_DAILY_LOSS_PCT=0.03`, blocks new BUYs when
equity is down >3%) was anchored to `_session_start_value`, set **once per process start**
(paper: cash + restored position marks; IBKR: TWS net-liq at connect). Two consequences:
1. A restart mid-drawdown re-baselined to the now-lower equity → the day's loss was
   forgotten, breaker silently re-armed at 0%. This bot restarts often (config changes,
   incident recovery, future VPS), and the UTC day rolls at ~8pm ET so essentially every
   next-morning restart reset the baseline.
2. Never reset without a restart either — a bot running continuously for days never rolled
   the "daily" baseline at all.
Surfaced in the 2026-08-28 "gaps in the stock bot" review as the #1 item. CLAUDE.md's own
`PAPER_DAILY_LOSS_PCT` comment already flagged it ("Session-lifetime, not calendar-day ...
not yet unified — see roadmap") but there was no roadmap item and no fix.

**Fix:** mirror the executors' own weekly tier + the crypto `RiskManager._maybe_reset_day()`:
- New `_day_open_equity` / `_day_start_iso` (UTC `YYYY-MM-DD`), **persisted** to
  `paper_state.json` / `ibkr_state.json` alongside the existing `week_*`/`peak_equity` fields.
- `_maybe_roll_daily_baseline(current_total)` rolls on a UTC date change (or seeds if unset).
  Called from `_is_daily_loss_tripped()` and — paper — `refresh_position_marks()` (the
  once-per-cycle call carrying live prices); IBKR rolls from `_update_breaker_marks(net_liq)`
  (connect + every buy()) since its data is always live TWS net-liq.
- Paper's daily baseline is deliberately **not** seeded in `__init__` (which only has
  avg_cost marks) — the first live-priced `refresh_position_marks` anchors it, so a
  next-morning restart with a moved position anchors the new day to real mark-to-market
  equity, not cost basis.
- Removed the sticky `_daily_loss_tripped` bool → non-sticky pure recompute each call, same
  as `_is_weekly_loss_tripped` and the crypto daily tier: a mid-day recovery above the
  threshold re-enables BUYs. `_is_daily_loss_tripped()` is now silent (was a one-shot
  WARNING); `buy()`'s existing "PAPER BUY BLOCKED ... daily loss circuit breaker" call-site
  log is the visibility. Removed `_session_start_value` entirely (only the breaker used it).

**Behaviour change to be aware of:** the daily breaker is no longer sticky-within-day. If
equity dips >3% then recovers, BUYs resume the same day (previously blocked till restart).
This matches the weekly tier and the real-money crypto bot; accepted as the intended
unification, not a regression.

**Tests:** +7. `tests/stock/test_stock_breaker.py` 14→18 (same-day-restart persistence +
still-blocks, new-day roll on restart, new-day roll mid-run without restart, non-sticky
intraday recovery). `tests/stock/test_ibkr_executor.py` 62→65 (same three, FakeIB +
`_current_day_iso` monkeypatch to pin the date). `_current_day_iso()` is a staticmethod on
both executors specifically so a test can pin "today". Full suite 763→**770 PASS**. Strategy
hash unchanged (`b30f2f9e769c8d41` — `stock_bot/execution/*` + test files only, no
`bot/strategy/*` or `stock_bot/strategy/*`, no walk-forward). CLAUDE.md updated: the
`PAPER_DAILY_LOSS_PCT` block, both manifest rows + total, and the running manifest narrative.
Not committed (user handles git).

**Still open from the same review (not this fix):** ATR sizing dormant (AMD/KO fail ATR×2.0
walk-forward), AMD fails LiveTradingGate Gate 1, no remote control / reachable dashboard for
the stock bot, IB Gateway headless deploy (roadmap G), yfinance-from-datacenter-IP untested.

---

## 19. Stock bot could place `.TO` (TSX) orders after the RULE_WHITELIST removal — RESOLVED 2026-09-02

**Symptom:** `⚠️ Order rejected: BUY AC.TO — IBKR order failed: order ended 'Inactive' with
no fill` on Telegram. Stock bot tried to BUY 35 shares of AC.TO; IBKR blocked it (CIRO rule
DMR 3200 — no automated orders on Canadian exchanges). No position opened, no money moved,
but a false "Order rejected" ops alert every cycle AC.TO's rule signals BUY.

**Root cause:** the TSX guard was *implicit* in `RULE_WHITELIST` — it had no `.TO` members,
so `.TO` names were never rule-buyable. The 2026-08-23 whitelist removal
([[stock-whitelist-gate-removed-2026-08-23]]) made `_rule_buy` fire for any scanned symbol
and nothing replaced the guard. AC.TO is in the WATCHLIST and got picked into top-movers.

**Fix:** `stock_bot/main.py` `run()` — explicit guard right after `_act_buy` is computed:
`.TO` symbol → log `TSX_BLOCKED`, record in `_blocked_rule_buys` for the digest, set
`_act_buy = False`. BUY-only (a manual `.TO` position stays exit-manageable). +4 source-guard
tests (`tests/stock/test_tsx_rule_buy_block.py`). Committed b8deaed. Not a `strategy/` file.

## 20. Cosmetic console print crashed the whole crypto bot on a broken stdout pipe — RESOLVED 2026-09-02

**Symptom:** `CRITICAL FATAL CRASH — bot exiting: BrokenPipeError: [Errno 32] Broken pipe`
at `bot/display.py:55` (`display.warmup()`'s `print`), via `_warmup_strategy` in
`bot/main.py`. Fired a Telegram crash alert.

**Root cause (this instance):** a `timeout`-killed `python -m bot.main` smoke run — `timeout`
closed stdout mid-warmup, the dying process threw `BrokenPipeError` from `print()`. But the
real gap: a purely decorative progress bar could take down a live-money bot. Would also bite
on a terminal disconnect / journald hiccup during the ~5 min warmup on a VPS.

**Fix:** `bot/display.py` shadows `print` with a wrapper that swallows
`BrokenPipeError`/`OSError` — console output is cosmetic, the file log handler is
independent. +4 tests (`tests/crypto/test_display_broken_pipe.py`). Committed b8deaed.
**Lesson: don't run the live bot binary as a smoke test — `timeout`-killing it fires a real
crash alert.**
