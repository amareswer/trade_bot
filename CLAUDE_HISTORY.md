# Project History Log

This file holds the dated, session-by-session narrative that used to live inline in
`CLAUDE.md` (research runs, incidents, audits, ops changes). It was split out on 2026-07-25
because `CLAUDE.md` had grown past 150k characters and was eating context budget every
session. **`CLAUDE.md` is the current-state file — read that first.** This file is for
"why does X work this way" / "what did we already try" lookups. Entries are in the order
they were originally written (roughly chronological, with some same-day follow-ups grouped
together as they were in the original).

---

## VALIDATED TRADING CONFIG — research history

As of 2026-06-19, the following configuration has passed:
- 5000-candle backtest (BTC/USDT 4h, Mar 2024–Jun 2026): PF 1.79, 58 trades, win rate 32.8%, max DD -5.12%, return -4.70% (at 0.8% fee)
  NOTE: This fingerprint was produced on an older strategy (simple RSI < 30 BUY gate).
  The current code uses Mode A/B (pullback RSI 38–58 + breakout); see "trade count evolution" below.
- SL/TP sweep: SL=1.5% / TP=10% validated 2026-06-19 (was TP=4.5%)
- Walk-forward as of 2026-06-19 (OLD strategy — RSI < 30 BUY gate, no EMA spread filter):

  | Window | Candles | PF   | Return  |
  |--------|---------|------|---------|
  | Full   | 5000    | 1.79 | -4.70%  |
  | 4000   | Sep24   | 1.83 | -4.06%  |
  | 3000   | Feb25   | 2.02 | -2.16%  |
  | 2000   | Aug25   | 1.37 | -2.17%  |
  | 1000   | Jan26   | 1.25 | -1.32%  |

  All 5 windows PF > 1.0 — walk-forward passed.

- Walk-forward re-run 2026-07-02 (CURRENT code — Mode A/B + EMA spread filter + MACD):

  | Window | Candles | Period        | Trades | PF   | Return  |
  |--------|---------|---------------|--------|------|---------|
  | Full   | 5000    | Mar24–Jul26   | 39     | 1.79 | -3.00%  |
  | 4000   | Sep24   | Sep24–Jul26   | 30     | 2.00 | -1.75%  |
  | 3000   | Feb25   | Feb25–Jul26   | 20     | 2.99 | +0.17%  |
  | 2000   | Aug25   | Aug25–Jul26   | 8      | 3.12 | +0.08%  |
  | 1000   | Jan26   | Jan26–Jul26   | 4      | 3.38 | +0.08%  |

  All 5 windows PF > 1.0. 2000c and 1000c have very few trades (8/4) — PF is directionally
  valid but not statistically reliable at these sample sizes. The 5000c (39 trades) and
  4000c (30 trades) windows are the most meaningful and both show PF ≥ 1.79.
- ADX sweep (18 / 25 / 30 / 35): ADX=18 is best on both full history and recent window
- RSI filter confirmed ON: RSI_FILTER_ENABLED=false drops PF from 1.38 → 1.19 and return from +1.51% → -0.10%
- Volume filter tested (VOLUME_K=1.2) and disabled: hurt PF (1.38→1.00), added noise not quality
- EMA spread filter validated 2026-06-27: MIN_EMA_SPREAD_PCT=0.004 (≥0.4%) confirmed real edge:

  | Window                    | Baseline PF | Filtered PF | ΔPF   | Trades filtered |
  |---------------------------|-------------|-------------|-------|-----------------|
  | In-sample  Mar24–Jun26    | 1.61        | 1.78        | +0.17 | 9               |
  | Out-of-sample 2019–2021   | 1.85        | 2.00        | +0.15 | 8               |

  ΔPF nearly identical across periods → not curve fitting. Ranging mode also deleted 2026-06-27
  (25% ranging win rate = trend win rate → no alpha). These two changes together bring in-sample PF from 1.21→1.78.

Note — trade count evolution:
- 2026-06-19 fingerprint (TP=10% validated):      ~58 trades, PF 1.79
  Strategy at this point: simple RSI < 30 oversold BUY gate, no EMA spread filter
- 2026-06-27 (MIN_EMA_SPREAD_PCT=0.004 added):    change reduced trade count further
- Post-commit c94d297 (Mode A/B dual entry):       strategy redesigned — pullback RSI 38–58
  and breakout mode replace simple RSI < 30. This is the primary cause of 58→39.
- 2026-07-02 current expected fingerprint:         39 trades, PF 1.79
  Proven: pinned window (2024-03-07→2026-06-19) gives IDENTICAL result to rolling window.
  Window drift is NOT a factor. Trade count difference is entirely from strategy redesign.
  PF 1.79 is the stable invariant across both old and new strategy versions.

Canonical strategy fingerprint history (BTC/USDT):
- 2026-07-03: hash `659d1c03987b72fd` stamped. Hashed files (behavior-defining only):
  `bot/strategy/indicator_strategy.py`, `bot/strategy/threshold_strategy.py`,
  `bot/indicators/indicators.py` (fingerprint.py and __init__.py excluded — non-behavioral).
  Window: BACKTEST_SINCE=2024-03-07 BACKTEST_UNTIL=2026-06-20 (pinned) or rolling 5000×4h
  (same trade count). Result at stamping time: 39 trades, PF 1.77 (range 1.77–1.79 depending
  on rolling-window end date; all > 1.0) — the fixed-SL-only fingerprint.
- As of 2026-07-17, `ATR_SL_MULT=2.0` went live (see ATR SL adopted live entry below) — hash
  itself unchanged (SL is config, not a strategy file) but a fresh `backtest.py` run returned
  35 trades / PF ~1.98 at that point, not the original 39/1.77 figure.
- As of 2026-07-20, `macd_enabled` was added to `engine_kwargs_from_cfg()` (see "MACD
  live/backtest divergence resolved" below) — a fresh run then returned 32 trades, PF 1.72.
  Hash unchanged throughout all of this (none were `bot/strategy/*.py` edits).
- Run `python stamp_strategy.py` after each passing walk-forward to write
  `logs/validated_strategy_hash`. If the bot or backtest prints `STRATEGY CODE DIFFERS`,
  re-run walk-forward before trusting any PF numbers.
- Prior hash `d3c7c383d91d5ef9` (2026-07-02) was computed over all `bot/strategy/*.py`
  including fingerprint.py — that scope was wrong. Hash value changed when scope was
  corrected to behavior-only files. No strategy logic changed.

ATR SL drift incident (resolved 2026-07-02):
- Root cause: .env contained ATR_SL_ENABLED=true (a stale key from a second config system in BacktestConfig)
  while live bot correctly used ATR_SL_MULT=0.0. Backtest was running ATR SL at 2× multiplier
  → 33 trades / PF 2.19 (vs expected 58/1.79). Now resolved: BacktestConfig uses ATR_SL_MULT
  (same key as StrategyConfig), convention mult=0.0 means disabled. No separate _ENABLED key.
- 1 live fill occurred under unvalidated ATR config (2026-06-22 16:36 UTC, pnl=-0.02 CAD, reason='trail_stop')
- Live bot was on validated fixed SL=1.5% from 2026-06-22 21:24 UTC onwards.

### 1h day-trading walk-forward — FAILED (2026-07-10)
Tested whether the current strategy (hash `659d1c03987b72fd`, unchanged) has edge on 1h candles
instead of the validated 4h timeframe — i.e. whether day-trading is viable. Same 5-window
walk-forward discipline used for the 4h re-run on 2026-07-02, run on `EXCHANGE=binance
SYMBOL=BTC/USDT BACKTEST_TIMEFRAME=1h`.

| Window | Period | Trades | Win rate | PF | Return | SL-exit rate |
|--------|--------|--------|----------|-----|--------|--------------|
| 5000c (full) | 2025-12-13 → 2026-07-10 (~7mo) | 19 | 26.3% | **1.04** | -2.89% | 63% |
| 4000c | 2026-01-24 → 2026-07-10 | 16 | 31.2% | 1.25 | -2.10% | 63% |
| 3000c | 2026-03-07 → 2026-07-10 | 11 | 36.4% | **0.99** | -1.75% | 64% |
| 2000c | 2026-04-17 → 2026-07-10 | 4 | 75.0% | 5.20 | -0.01% | 25% |
| 1000c | 2026-05-29 → 2026-07-10 | 3 | 66.7% | 3.20 | -0.15% | 33% |

**Verdict: FAILED.** The two windows with meaningful sample size — the full 7-month history and
the 3000-candle window — come in at PF 1.04 and 0.99 (a straight fail). Compare to the 4h
re-run where all 5 windows passed at PF 1.79–3.38. The last two "passing" windows have only
3-4 trades each — the same small-sample caveat already noted for 4h's 1000c/2000c windows,
not enough to act on alone.

Failure mode matches every rejected altcoin (XRP, ETH, SOL, DOGE, the full USD screen): ~63%
of trades exit via stop-loss at 1h vs a much healthier exit mix on 4h. The Mode A/B entry
logic (pullback RSI 38-58 / breakout) does not have edge at hourly frequency — it is mostly
noise that gets stopped out. Binance also only has ~7 months of 1h history via pagination vs
2.3 years at 4h, so the full-window sample is inherently weaker too.

**Decision (now a standing rule in CLAUDE.md): no live day-trading (1h or faster) on this
strategy.** `CANDLE_MINUTES=240` (4h) stays the only validated live timeframe. Do not revisit
without either a new/modified strategy (which would need its own fresh walk-forward and hash
stamp) or materially more 1h history becoming available.

### Config change log (2026-06-19)
Previous validated config: TP=4.5% (PF 1.38 at zero fee)
New validated config: TP=10% (PF 1.79 at zero fee, 1.79 at 0.8% fee)
Reason: fee resilience — TP=10% exit mix is 37 SL / 9 TP / 12 strategy
vs TP=4.5% which was 56 SL / 25 TP / 3 strategy. Higher TP lets strategy
SELL signals do meaningful work, reducing fee sensitivity.

### New code added 2026-06-15
- `calc_trade_qty_sl(cash, entry_price, stop_loss_price)` on AppConfig — SL-based position sizing
  (risks exactly risk_per_trade_pct of cash per trade; falls back to calc_trade_qty if SL=0)
  — NOTE: this method was later removed 2026-07-20 as fully dead code (see position-sizing
  docstring entry below); never called anywhere.
- `volume_k` field wired through IndicatorConfig → StrategyConfig → AppConfig → engine.py → backtest.py → main.py
  (set VOLUME_K=0 to disable; VOLUME_K=1.2 requires current candle volume ≥ 1.2× avg of prior 3)

### Crypto bot hardening (2026-07-03)
- **Manual kill-switch:** `touch logs/HALT` engages the risk manager's manual halt without a
  restart (blocks BUY + strategy SELL; SL/TP exits still fire). `rm logs/HALT` resumes.
  Telegram alert on engage/lift. Helper: `_check_halt_flag()` in `bot/main.py`.
- **Risk breaker state persists across restarts:** `logs/risk_state.json` stores the all-time
  drawdown peak, day-open value, and daily fill count (live mode only — backtests stay
  stateless). Previously a crash/restart silently reset the max-drawdown breaker and the
  daily trade cap. Daily counters only restore if saved on the same UTC day; peak always restores.
- **RiskManager daily reset now uses UTC** (`_utc_today()`), matching candle timestamps and
  the daily P&L alert — was local `date.today()`, resetting counters at local midnight.
- **Daily P&L Telegram alert fires exactly once per UTC day** — date-change trigger replaced
  the `hour==0 and minute==0` window, which double-fired on a 30s loop and could skip entirely.

### Stock bot + unified dashboard (2026-07-04)
- **Stock daily-loss breaker fixed** (see test_stock_breaker.py).
- **`unified_dashboard.py` rewritten for the multi-symbol bot:**
  - Reads `logs/live_state_*.json` per-symbol files (was reading the legacy
    `logs/live_state.json`, stale since Jun 27 — it still showed the phantom external
    0.000378 BTC position). Legacy path is fallback-only now.
  - Symbols split by UNIVERSE_WHITELIST: active slots get cards (with STALE badge if
    state > 48h old); retired slots (XRP/DOGE leftovers) listed but not counted.
  - New ops strip: kill-switch status (`logs/HALT`), fills today per symbol and breaker
    peak/day-open from `logs/risk_state.json`.
  - Stock positions table shows live price / market value / unrealized P&L via the stock
    bot's own guarded `latest_price()` (falls back to cost marks when yfinance rate-limits).
  - **Now auto-refreshed by the crypto bot** (2026-07-04): `bot/main.py` runs
    `_unified_dashboard_loop()` as a daemon thread — regenerates every 60s via subprocess
    (same isolation pattern as the regime monitor). `UNIFIED_DASHBOARD_INTERVAL=0` disables.
    No separate `--watch` terminal needed. Because each refresh is a fresh subprocess, it
    always runs current code (a long-lived `--watch` from Jun 26 once held stale code in
    memory and overwrote new output every 30s — that failure mode is gone).
  - Stock prices are TTL-cached 15 min in `logs/stock_price_cache.json` (cross-process) —
    without it, per-cycle yfinance calls got rate-limited within minutes.

### Execution hardening (2026-07-04) — audit fixes, strategy files untouched (hash `659d1c03987b72fd` still valid)
- **SL/TP exits are always market orders.** `LiveExecutor.execute(..., urgent=True)` bypasses
  the limit-chase and the ORDER_TYPE=limit path entirely; the SL/TP block in `bot/main.py`
  passes it. Before this, a stop exit could sit in the post-only chase (placed ABOVE the ask,
  up to 4 × LIMIT_CHASE_TIMEOUT_S=120s repricing) while price ran away — with SL=1.5% and
  stops being the majority exit, chase slippage was directly eating the validated edge.
  BUY entries keep the limit-chase (0.40% maker saving, confirmed Jun 14 fill).
  Test: `test_urgent_sell_bypasses_limit_chase`.
- **Partial TP reclassified as an exit** — bypasses the risk gate like SL/TP (a manual HALT
  no longer freezes profit-taking; `RISK_HALT_BLOCKS_STOPS=true` still suppresses it).
- **MTF gate no longer runs on stale data.** Daily closes are fetched per symbol at decision
  time (gate 2c) with a per-symbol cache fallback. Previously loaded once at startup and only
  refreshed AFTER a BUY had been judged — the veto could run on weeks-old daily candles, and
  multi-symbol mode applied the active symbol's daily trend to every symbol.
- **Trailing-stop peak seeding respects TRAILING_STOP_ACTIVATION_PCT** — on BUY the peak is
  seeded only when activation is 0; otherwise the intra-candle block arms it at
  entry × (1 + activation). (Latent — trailing disabled in .env.)
- **Drift reconciliation compares Kraken `total`** (matching `_sync_position`) — `free`
  produced false drift alerts during the settlement window after a fill.
- **Dashboard capital-gate PF is NET of fees and window-scoped.** `unified_dashboard.py`
  `_read_gate_stats`: each SELL's pnl minus its fee minus an equal share of window BUY fees;
  only fills since 2026-06-22 21:24 UTC (validated-config go-live) count; `kraken_backfill`
  and phantom rows excluded; `partial_tp` fills count toward PF but not the 15-round-trip
  gate. Rationale: trades.db `pnl` is gross — at $77 capital, fees dwarf gross P&L (book was
  gross −$0.02 but net −$1.13 when this was fixed). A gross-PF gate could promote capital on
  a losing book.
- **Test suite hermeticity:** `test_fill_recording.py` now forces `limit_order_enabled=False`
  (autouse fixture). Since LIMIT_ORDER_ENABLED=true landed in `.env`, its never-closing-order
  test was rerouted into the limit-chase and busy-spun ~8 minutes at 100% CPU (time.sleep
  mocked + 120s wall-clock poll deadline). Suite runtime is back to ~5s; if it ever takes
  minutes again, suspect a test reading live `.env` config.
- **Known-inert leftover DELETED 2026-07-16:** the DOGE/CAD liquidity gate in `bot/main.py`
  was dead code (DOGE is BLOCKED) and hardcoded a symbol — gate block in `bot/main.py` plus
  the `doge_vol_min_cad` PortfolioConfig field/validation/loader all removed. `DOGE_VOL_MIN_CAD`
  stays in `.env` — `regime_monitor.py` still reads it directly for watchlist health reporting.
- `.env` chmod 600 (was world-readable). A bare un-named secret sitting as a comment under
  "── Secrets ──" in `.env` was RESOLVED 2026-07-13: identified as an exact duplicate of
  `OLLAMA_CLOUD_API_KEY` (already properly named in `stock_bot/.env`); stray comment copy
  deleted from root `.env`. 2026-07-16: confirmed the key is UNUSED — `AI_PROVIDER=nvidia_nim`
  is the active provider; Ollama Cloud is a dormant fallback. Action is revoke (delete at
  ollama.com) + strip the line from stock_bot/.env, not rotate. User parked this
  2026-07-16 — deferred indefinitely, low urgency (local machine only; exposure was the
  pre-2026-07-04 world-readable perms).

### Stock bot Phase A — expectancy measurement (2026-07-04)
Goal: get to 30 completed paper round-trips and measure per-trade expectancy net of costs.
Income targets ($/day) are NOT a bot setting — income = expectancy × capital × frequency, and
expectancy is unmeasured until trades complete. The report's expectancy number is the product.
- **Exit audit result:** main paper book SL/TP watcher healthy (30s cadence, market-hours
  gated); AC.TO/DLTR correctly held inside the −5%/+15% band. No missed exits.
- **FastValidator MAX_HOLD starvation fixed:** feed gaps (rate limit / holiday) skipped ALL
  exit checks, so positions could outlive the 48h design (observed AMZN/HOOD 2026-07-04).
  MAX_HOLD now falls back to the guarded `get_live_price()`; SL/TP still wait for real candles.
- **Price-corruption guard on the fast book:** candle close deviating > FAST_PRICE_SANITY_PCT
  (20%) from the prior close is rejected for entries AND exits. Incident: 2026-06-29 META
  $564.87 entry → phantom "SL" exit at $163.51 (−71% in 20 min, data corruption not market),
  which dragged the signal book to PF 0.07 / −16.85% per signal vs the 3 sane trades
  (+4.0% TP, +1.2% MAX_HOLD, −1.5% SL).
  **Fast book RESET 2026-07-05:** pre-guard history archived to
  `stock_bot/archive/fast_trades_pre_guard_20260705.csv` + `..._state_...json`.
  Every signal from Monday 2026-07-06 onward is post-guard — stats clean from trade one.
- **FastValidator scope widened:** scans watchlist + top FAST_MOVERS_COUNT (5) universe
  movers per cycle (was watchlist-only). Cap bounds yfinance fetch volume — raise carefully,
  rate-limit spirals are a known failure mode.
- **Expectancy report:** `stock_bot/analysis/paper_report.py` now prints per-trade net $ / net %,
  net PF, trades/week pace, and projected $/week — net of the IBKR Pro fixed commission model
  (COMMISSION_PER_SHARE_USD/CAD + COMMISSION_MIN_USD/CAD in stock_bot/.env). Slippage is NOT
  added there: the paper executor already applies PAPER_SLIPPAGE_BPS to every fill. The fast
  book is reported separately as a unit-sized signal book (% stats only — never $ expectancy).
- Phase A gate stays: 30 completed trades, PF ≥ 1.2, win rate ≥ 30% before any IBKR live step.

### Ops changes (2026-07-05)
- **Runtime interpreter is the `.venv` (Python 3.11.15)** — see CODING STYLE in CLAUDE.md for
  launch/test commands and the pandas-2.3.3 hold. System python3 (3.9) must not run the bots.
- **yfinance upgraded 1.2.0 → 1.5.1** (own change, after the venv switch): 168 tests green,
  all four live data paths smoke-tested (daily US/TSX candles, guarded live price,
  FastValidator 1h fetch, multi-ticker batch). Better rate-limit resilience via curl_cffi.
- **Runtime data untracked from git:** `stock_bot/fast_trades.csv` was accidentally tracked —
  removed from the index (`git rm --cached`). Root `.gitignore` now covers all stock-bot
  runtime files (`fast_trades.csv`, `paper_state.json`, `universe_cache.json`, `archive/`).
  Rule: code and config templates go in git; anything the bots write at runtime does not.
- **Startup banner shows the executor's restored cash**, not `.env` PAPER_STARTING_CASH
  (banner printed $1,000.00 while the restored book held $520.71).
- **Dashboard stock-bot heartbeat amber window 65h → 80h** — TSX holiday Mondays no longer
  show a false "down?" on Tuesday morning.
- Pending "restart stock bot under venv" — DONE 2026-07-05 10:42 (verified via ps: running
  on 3.11 with caffeinate).
- **Crypto bot log pollution fixed:** `bot/main.py` installed the root RotatingFileHandler at
  IMPORT time, so every pytest run wrote test noise into `logs/trade_bot.log` — it faked the
  dashboard heartbeat (log mtime = "alive"), buried real forensics, and one pytest run rotated
  the live log at 10MB. Handler setup now lives in `_setup_logging()`, called only from
  `run()`. Verified: importing bot.main installs zero handlers; pytest no longer touches the
  log. (`stock_bot/main.py` had the same import-time pattern — FIXED 2026-07-16: handlers
  now install in `_setup_logging()`, called only from `run()`; verified importing
  stock_bot.main installs zero handlers.)
- **Crypto bot found DOWN during this session** (no process; last real log 09:23 ET after
  ~3 min of Kraken network errors, no traceback — consistent with closed terminal or Mac
  sleep; it was launched WITHOUT caffeinate). Position was 0 — no exposure while down.
  It picked up the 2026-07-04 execution fixes at its 22:09 ET restart.
  **Launch it like the stock bot from now on:** `caffeinate -i .venv/bin/python -m bot.main`

### Ops changes (2026-07-06)
- **Scheduling is now repo-tracked:** `ops/crontab.txt` is the source of truth for cron on any
  machine — install with `crontab ops/crontab.txt`, never edit the live crontab by hand.
  Installed locally 2026-07-06: daily `shadow_signal.py` (02:00 local ≈ 06:00 UTC) and weekly
  Monday `live_comparison.py`. VPS migration note in the file: convert to systemd timers
  (`Persistent=true`) via deploy.sh. (Superseded 2026-07-14 — see "Cron retired" below: cron
  never actually worked on macOS due to a TCC permissions issue.)
- **Shadow signal 2026-07-06 run: 100% match rate (PASS ≥95%).** Report:
  `logs/shadow_report_20260706.md`.
- **pytest log-pollution fix verified:** full suite (168 passed, 3.6s) leaves
  `logs/trade_bot.log` mtime/size untouched. The test noise stamped 2026-07-05 10:42 in the
  log predates the fix — historical, not a regression.
- **`.env` secret rotation deferred by user** (2026-07-06, "will do it later") — still the
  only open security item. UPDATE 2026-07-13: secret identified (Ollama Cloud key duplicate,
  removed from root `.env`). UPDATE 2026-07-16: key confirmed unused (provider is
  nvidia_nim) — action is revoke, not rotate; user parked it indefinitely.

### Held-position visibility fixes (2026-07-10) — both bots, 173 tests pass
Root cause class: a held position whose symbol leaves the scanned universe becomes invisible
to exit logic. Found via DLTR (stock bot): bought 2026-06-25 from a universe pick, then rotated
out of the movers list — no price refresh, no AI verdict, could never get a strategy SELL, and
its missing price also produced a phantom -$227.80 unrealized P&L (missing-symbol fallback was
$0 instead of avg_cost in `unrealized_pnl()`/`total_value()` — fixed 2026-07-09).
- **Stock bot (`stock_bot/main.py`):** each scan cycle now builds `cycle_symbols =
  watchlist + movers + held positions` and adds held symbols to the screener-bypass set.
  A held symbol can no longer be screened out of its own exit evaluation. The SL/TP watcher
  was never affected (iterates `positions_snapshot()` directly).
- **Crypto bot (`bot/main.py`):** startup orphan guard `_check_orphaned_positions()` — scans
  all `logs/live_state_*.json`; any `position > 0` for a symbol not initialized this run
  (e.g. removed from UNIVERSE_WHITELIST while holding) fires logger.error + Telegram alert.
  Alert-only by design: auto-trading an orphan with a cold strategy would be worse. No live
  orphans existed at ship time (all slots flat). Tests: `test_orphaned_positions.py` (5).
- **Both bots restarted on the fixed code 2026-07-10** (usual caffeinate + .venv launch).
- **Kraken balance is now $146.31 CAD; slot stays capped at $77** (`MAX_SLOT_CASH_CAD=77`).
  Deliberate: capital increases go through the 15-fill / net-PF ≥ 1.2 gate, not deposits.
  Current gate progress: 0 fills. Raise the cap in `.env` only after the gate passes.

### Crypto bot audit (2026-07-10 late) — applying the stock-bot rebuild lessons, 202 tests pass
The crypto bot was the template for the stock rebuild, so "do the same thing" = audit, not
rebuild. Findings:
- **Backtest execution model verified honest for crypto:** intra-candle SL/TP vs low/high,
  SL-before-TP pessimism; strategy fills at signal-candle close, which is FAITHFUL here
  (live bot decides at candle close and fires a market order seconds later in a gapless
  24/7 market — unlike daily stock bars, where next-open fills are required). The daily
  shadow-signal check is the standing proof of that assumption.
- **Shadow fidelity re-verified manually 2026-07-11 02:04 UTC: 96.6% match, PASS** (1
  mismatch = known indicator-warmup boundary, no position at stake).
  `logs/shadow_report_20260711.md`.
- **Drift-alert spam fixed:** 0.000085 BTC (~$8) appeared in the Kraken account ~Jul 6
  (confirmed by user 2026-07-10: manual purchase, not bot activity). The reconciliation re-alerted
  every ~3h for 4 days because the counter reset after each escalation. `_evaluate_drift()`
  extracted from the inline loop in `bot/main.py` (now unit-tested directly, not via a
  hand-mirrored copy) and gained `drift_acked`: an escalated drift amount is acknowledged —
  no re-alert while unchanged; a CHANGED amount re-arms; resolution clears the ack.
  The 0.000085 BTC itself was safe by design (ADOPT_EXTERNAL_HOLDINGS=false — never traded),
  and was ultimately sold along with everything else 2026-07-17 (see crypto halt entry).
- **Cron jobs moved 02:00 → 12:05 local (weekly 09:00 Mon → 12:10 Mon):** macOS cron
  doesn't fire while the lid is closed and never catches up — the shadow job silently
  missed Jul 7–10. `caffeinate -i` prevents idle sleep, NOT lid-close sleep.
  `ops/crontab.txt` updated + reinstalled. (Superseded 2026-07-14 — cron replaced entirely,
  see "Cron retired" below.)

### Stock bot rule-based rebuild (2026-07-10) — AI demoted to advisory, 200 tests pass
The position book's trade trigger is now the backtested rule strategy, not AI verdicts.
Rationale: AI confidence numbers are uncalibrated, verdicts flip on unchanged data (AMD:
BUY 58 → SELL 60 → HOLD 58 → BUY 68 → SELL 62 in ~10 min), and AI opinions cannot be
backtested. This restores the project charter: "AI gives advisory signals only."

**Walk-forward result (2026-07-10, `stock_backtest.py`, report `logs/stock_backtest_20260710.md`):**
Crypto IndicatorStrategy (Mode A/B, unmodified — hash `659d1c03987b72fd` untouched) on
daily candles, 4 windows (full ~6y / 750d / 500d / 250d), 15 bps slippage per fill + IBKR
commissions, SL 5% / TP 15%. Parameters came from BTC — genuinely out-of-sample on stocks.
- **PASS (4): MRNA (PF 1.52–2.22), AMD (1.36–3.18), RY.TO (3.90–7.95, WR 67–75%), PLTR (1.76–2.29)**
- FAIL (10): HOOD, NCLH, AC.TO (PF 0.71!), CCL, INTC, NVDA, TSLA, SHOP.TO, META, AMZN
- Gate: full-window trades ≥ 10, PF ≥ 1.2 every window with ≥ 3 trades, SL-exit ≤ 70%.

**New pieces:**
- `stock_bot/strategy/rules.py` — `build_indicator_config()` is THE single parameter source
  (backtest imports it, live imports it — no drift possible; pinned by test). `rule_signal()`
  statelessly replays candles through a fresh IndicatorStrategy each cycle → live signal is
  identical to backtest by construction. `drop_last=True` excludes today's still-forming
  candle while the market is open (backtest only saw completed candles).
- `stock_bot/backtest/engine.py` — honest daily-bar engine: signal fills at NEXT open,
  intra-candle SL/TP vs low/high with gap-through fills at the open, SL-before-TP pessimism,
  slippage both ways + IBKR round-trip commission (same `_round_trip_commission` the paper
  report uses). Replaced the 2026-06-23 one-off `stock_backtest.py` (look-ahead fills,
  close-only SL checks, 0.5% notional commission, symbol-order-dependent shared cash).
- `main.py` wiring: `RULE_TRADING_ENABLED=true` + `RULE_WHITELIST=MRNA,AMD,RY.TO,PLTR` in
  stock_bot/.env (whitelist has since grown many times — see CLAUDE.md for current value).
  Rules may BUY only whitelist symbols; rule SELL exits anything held; AI exit policy (below)
  stays as an extra risk-reducing exit; SL/TP watcher unchanged. AI can never OPEN a position.
  `LOOKBACK_DAYS` 200 → 300 (200-day regime EMA needs ~204 warmup). Per-symbol "📐 RULES:"
  line printed each scan; dashboard BUY card notes AI is advisory.
- **Adding a symbol to RULE_WHITELIST requires a fresh `stock_backtest.py` PASS — never by hand.**
- **Dashboard shows the decider (2026-07-10):** `renderer.py` — new "📐 Rule Signals" strip at
  the top (per-symbol rule verdict with "→ buying" / "not whitelisted — no entry" / "→ exiting"
  annotations); AI strip relabeled "🤖 AI Advisory — opinions only, cannot open positions";
  each stock card carries a rule tag next to the AI verdict. `ScanResult` gained
  `rule_verdict` + `rule_whitelisted` (default None/False — backward compatible).
  Invariant restored twice today: what the dashboard shows is what the bot will do.
- **AI shadow votes (2026-07-10):** every rule trade's CSV reason records the AI's opinion at
  fill time (`RULE BUY ... | ai=SELL60`, `| ai=NONE` if unavailable) — frozen schema unchanged,
  content only. After ~30 rule trades, compare outcomes where AI agreed vs disagreed; the AI
  earns entry-veto power only if agreement proves predictive. Do not grant veto before that.
- Held FAIL-symbol positions (AC.TO, DLTR at ship time) are managed by: rule SELL, AI exit
  policy, SL/TP watcher. No new entries on FAIL symbols.
- Re-run trigger: any change to `bot/strategy/*` or `build_indicator_config()` invalidates
  RULE_WHITELIST until the stock walk-forward is re-run (same discipline as crypto).

### Stock bot asymmetric exit policy (2026-07-10) — 184 tests pass
Incident: AC.TO held while the AI issued SELL 60% then SELL 58% on consecutive cycles —
both silently ignored because ONE confidence bar (PAPER_MIN_CONFIDENCE=65) gated both BUY
and SELL. The dashboard showed "🔴 SELL — AC.TO" with no hint the bot wouldn't act.
Design fix: entries and exits are not symmetric — a BUY adds risk (high bar stays), a SELL
on a HELD position removes risk (lower bar). Mirrors the crypto risk manager's "SELL always
allowed" philosophy.
- **New module `stock_bot/execution/exit_policy.py` (`ExitPolicy`)** — pure logic, no I/O.
  A held position exits on: single SELL verdict ≥ `PAPER_MIN_CONFIDENCE_SELL` (55), OR
  `PAPER_SELL_STREAK_CYCLES` (2) consecutive SELL cycles each ≥ `PAPER_SELL_STREAK_MIN_CONF`
  (50). HOLD/BUY verdicts and sub-50 SELLs break the streak; streak is in-memory (restart
  resets it — SL/TP watcher is the crash-safe backstop and is untouched).
- **`main.py`:** `exit_policy.decide()` runs on EVERY verdict (so HOLD breaks streaks);
  BUY keeps the 65 bar; SELL path uses the policy. A held symbol with a below-bar SELL now
  logs + prints "HELD, not exiting: <reason>" instead of silence. Streak-triggered fills
  get " [streak]" appended to the CSV reason (schema unchanged — content only).
- **Dashboard (`renderer.py` `_summary_html`):** signal cards now show per-symbol
  confidence and action status — "AC.TO 60% → exiting" / "AMD 62% (not held)" /
  "(held · below exit bar)" — plus a caption of the act thresholds. Advice and action are
  no longer visually identical. Thresholds threaded via `render(exit_bars=...)`.
- **Phase A note:** this changes the strategy being measured — position-book trades closed
  before 2026-07-10 used the old single-bar exits. Only 8 completed trades existed; the
  30-trade gate continues counting without reset, but any pre/post expectancy split should
  use this date.
- **Known limitation (resolved by the rule-based rebuild above):** AI verdicts remained the
  trade trigger and were noisy/unstable (AMD flipped BUY 58 → SELL 60 → HOLD 58 → BUY 68 →
  SELL 62 within ~10 min on 2026-07-10) and unbacktestable — this exit policy was the stopgap
  until the rule-based rebuild landed the same day.

### Rule pipeline first live session + sizing-visibility fix (2026-07-13) — 202 tests pass
Monday 2026-07-13 was the first live session of the rule-based stock pipeline. Result: clean —
`rule_signal()` fired correctly all day (AMD BUY, 13 HOLDs, 1 no-op SELL on INTC), no crashes,
only routine NVIDIA NIM (AI advisory) timeouts which don't affect trading since AI cannot
open positions.

**Found while verifying: AMD's BUY signal was silently unfillable.** Root cause —
`PAPER_RISK_PCT=0.20` on the ~$1,014 paper account targets a ~$203 allocation per trade;
AMD trades at ~$538/share, so `int(alloc / price)` rounds to 0 shares and the old code had
no branch for that case — no log, no print, nothing. The dashboard's "AMD → buying" line
was technically true (an entry would be attempted) but gave no hint it could never clear.
RY.TO (~$298/share) and the pending GLD add (~$300+/share) hit the identical wall.

**Fix — visibility, not a forced fill (`stock_bot/main.py`, sizing block ~line 1081):**
when `shares == 0` the bot now logs `SIZE_SKIP` and prints the target allocation, risk %,
and price so the skip is diagnosable instead of silent.

**Deliberately did NOT fix this by forcing a fill** (bumping `PAPER_RISK_PCT`, allowing
fractional shares, or rounding up to a minimum of 1 share regardless of cost). Buying 1
AMD share would commit ~53% of account cash to one position — a 2.65x blowout over the
intended 20%-per-trade risk cap. That is exactly the failure mode the Buffett rule mapping
in the Investment philosophy section (see CLAUDE.md) calls "margin of safety": small,
capped sizing over conviction-sized bets. **Standing warning:** do not "fix" a SIZE_SKIP by
raising `PAPER_RISK_PCT`, adding fractional-share support, or special-casing a minimum share
count for expensive symbols — any of those bypasses the margin-of-safety sizing rule the
same way a crypto capital-gate bypass would. The correct lever is the documented one: let
the paper account grow through the Phase A gate, or don't whitelist single-share-unaffordable
symbols at the current account size.

### Dashboard updates (2026-07-14) — 202 tests pass
Three changes, no strategy files touched (hash `659d1c03987b72fd` still valid):
- **Rule strip no longer says "→ buying" for unfillable signals.** The 2026-07-13 SIZE_SKIP
  fix covered logs/stdout only; the dashboard still promised fills that would round to 0
  shares. `renderer.py` `_rule_summary_html()` now takes `buy_alloc` (threaded through
  `render()` from `main.py`, computed as `(cash + position value) × PAPER_RISK_PCT` at
  render time); a whitelisted BUY whose share price exceeds it renders
  "⚠ signal valid, can't fill — 1 share $538 > $203 allocation". AMD/RY.TO/GLD show this
  today — the "dashboard shows what the bot will do" invariant holds again. Omitting
  `buy_alloc` preserves old behavior (backward compatible).
- **"Gates at a Glance" strip:** `unified_dashboard.py` `_book_gates_section()` — all three
  books side by side (crypto 0/15 · position book n/30 · swing book n/30, each with net PF +
  win rate + progress bar). Position/swing numbers come from `stock_bot.analysis.paper_report`'s
  own `_pair_trades`/`_expectancy_stats` (imported, not duplicated) — the strip can never
  disagree with the report.
- **Retired slot state files archived:** `logs/live_state_XRP_CAD.json` and
  `logs/live_state_DOGE_CAD.json` (both flat, position 0.0, untouched since Jul 1) moved to
  `logs/archive/`. Dashboard "retired slots" note now empty; orphan guard unaffected.
- Note: position book gate showed 1/30 at this point — the current `paper_trades.csv` held
  exactly one round trip (AC.TO: BUY 2026-06-24, SL hit 2026-07-14 10:38, −$12.51 net) + open
  DLTR. The pre-Jun-24 $10k-era trades were deleted from the CSV (last tracked at git fb1751a).

### Cron retired → in-bot audit scheduler (2026-07-14) — 210 tests pass
**Discovery: the cron jobs NEVER worked.** `/var/mail/nishita` shows every run since the
2026-07-06 install — at 02:00 AND at the moved 12:05 slot — failed with
`/bin/sh: logs/shadow_signal.log: Operation not permitted`. macOS TCC denies `/usr/sbin/cron`
access to `~/Desktop` where the repo lives; errors went to local mail nobody reads. The
2026-07-10 "lid-close sleep" diagnosis was wrong (cron fired fine — it just couldn't write).
The Jul 6 and Jul 11 shadow reports were both manual runs. Consequence: Gate 3 of the capital
gate (shadow ≥ 95%) was silently running on stale data.
- **Replacement:** `_scheduled_audits_loop()` daemon thread in `bot/main.py` — runs
  `shadow_signal.py` daily (SHADOW_AUDIT_TIME, default 12:05 local) and `live_comparison.py`
  Mon-anchored weekly (WEEKLY_AUDIT_TIME, default 12:10). Fresh subprocess per run (same
  isolation pattern as the unified-dashboard thread), output appends to the same
  `logs/shadow_signal.log` / `logs/weekly.log`, last-run dates persist in
  `logs/audit_state.json`. **Catch-up by design:** a bot started after the scheduled time
  runs the missed audit immediately (fixes both cron failure modes). A failed script is
  logged and retried next period, not every minute. `AUDIT_SCHEDULER_ENABLED=false` disables.
  Pure due-logic in `_audit_due()` — tests: `test_audit_scheduler.py` (8, later 14).
- **`ops/crontab.txt` rewritten as a tombstone** (full failure history inside) and
  reinstalled — live crontab now has zero active jobs. VPS note: systemd timers or just
  keep the in-bot scheduler.
- **Dashboard: Gate 3 shows shadow-report age** — fresh (≤1d) plain, 2–3d amber "⚠ Nd old",
  >3d red + "STALE" subtitle. A stale report can no longer impersonate a fresh one.

### Stock bot SWITCHED to IBKR paper executor (2026-07-17 11:13 ET) — 241 tests pass
The deliberate switch decision was made and executed the same day the executor shipped.
No strategy files touched (hash `659d1c03987b72fd` still valid).
- **Preconditions verified first:** account reset landed ($1M → $995.30 CAD net_liq,
  no positions); ibkr_trades.csv contained no smoke-test rows (header only);
  ibkr_state.json re-seeded starting_cash=$995.30.
- **Sim book closed flat before the flip** (positions would otherwise become exit-less
  orphans under the new executor — the 2026-07-10 visibility failure mode). DLTR sold
  @ $130.39 (+$24.35) and CM.TO @ $171.03 (+$2.68), reason `EXECUTOR_SWITCH_TO_IBKR`,
  via the normal executor path (slippage applied, CSV rows written). Sim book final:
  $1,014.53 cash, +$14.52 realized (+1.45%), 3 completed round-trips. paper_state.json /
  paper_trades.csv stay in place as the frozen sim-era record — do not delete.
- **`STOCK_EXECUTOR=ibkr` set in stock_bot/.env; bot restarted 11:12 ET** — connected to
  DUQ273338 (PAPER), cash $995.30, positions []. TWS must now be running + logged in
  whenever the stock bot runs (connection failure stops startup, never falls back to sim).
- **Phase A gate counts ACROSS the switch:** `paper_report.read_position_book()` merges
  paper_trades.csv + ibkr_trades.csv in timestamp order (pairs never straddle executors
  because the sim book closed flat). Gate at switch: 3/30 trades, net PF 1.59, WR 67%.
  The report shows the IBKR account section when ibkr_state.json exists (current cash =
  last fill's cash_remaining — the report makes no network calls); realized P&L shown is
  sim + IBKR combined.
- **Dashboard follows the active executor:** `_load_stock_state()` reads STOCK_EXECUTOR
  from stock_bot/.env; ibkr mode synthesizes the card/table state from ibkr_state.json +
  ibkr_trades.csv (badge "IBKR PAPER · DUQ273338"); gate strip uses the merged book.
- Tests: test_paper_report.py 6 → 8 (merge + IBKR account section; existing report test
  made hermetic against real ibkr files). Suite 239 → 241.
- On connect TWS replays the day's account executions (execDetails) — fills from other
  client IDs (e.g. manual TWS orders) are ignored by the executor, by design.
- **Dashboard labels follow the active executor (same day, post-switch):** unified gate
  strip "Position Book · IBKR" (dynamic via `_stock_executor_type()`), positions table
  "Stock Positions — IBKR paper", and `stock_bot/dashboard/renderer.py`'s Paper Trading
  section badge "IBKR PAPER · real order routing, simulated money" (reads STOCK_EXECUTOR
  env). All numbers were already executor-correct; only labels lagged.
- **Dashboard roles (agreed with user 2026-07-17):** `unified_dashboard.html` is the
  daily-glance dashboard (both bots, gates, ops, P&L — auto-refreshed every 60s by the
  crypto bot). `stock_dashboard.html` stays as the stock bot's per-symbol drill-down
  (rule-signal strip, AI advisory, exit-bar status — the "why did/didn't it act" view).
  Both are kept; do not fold one into the other without a user decision.
- **Post-switch audit (same day) — remaining sim-file readers fixed, 242 tests pass:**
  `paper_report.load_active_book_state()` (new) returns the ACTIVE executor's book in
  the paper_state.json shape (env-driven; used by the daily Telegram/email summary in
  `alerts/notifier.py`, which previously read the frozen sim state); readiness Gate 3
  (`accuracy_tracker.check_gate3`) and the `stock_analysis.py` accuracy CLI default now
  count the merged paper+IBKR book. Verified non-issues: order placement runs
  `_ensure_connected_async()` (auto-reconnect after a TWS restart); read paths degrade
  to logged warnings when TWS is away; ibkr_state.json/ibkr_trades.csv are gitignored;
  fast_validator/tracker references were comment-only.
- **OPS: TWS auto-logoff.** Classic TWS logs itself off daily by default — set
  Global Configuration → Lock and Exit → "Auto restart" so it stays up (weekly re-login
  Sundays is still required by IBKR). Until that's set, a nightly logoff means the bot's
  orders fail (visibly, with logged errors) until TWS is logged back in.

### TSX API orders BLOCKED by regulation → whitelist moved to NYSE listings (2026-07-17 pm)
First live IBKR-era rule signal (CM.TO BUY, 12:37 + 13:41 ET retries) was rejected by IBKR:
**Error 201 "API/CTCI orders for Canadian products are not allowed."** Root cause is
regulatory, NOT a permission or account setting: CIRO rule DMR 3200 A.1.(b)(i) prohibits
IBKR Canada clients from placing orders on Canadian exchanges via ANY automated system
(API/third-party apps). US products via API are unaffected (KO smoke trade proved it).
Canada trading permission was already enabled (verified in Client Portal) — there is NO
fix; manual TWS orders are the only way to trade TSX. **This is now a permanent standing
rule — do not re-attempt .TO via API, ever.** Error 10349 (TIF=DAY) in the same log is the
known benign warning. The executor handled both rejections cleanly: no fill recorded, no
state change.
- **Consequence:** 5 of 12 whitelisted symbols (.TO) could never fill. Their dashboards
  said "→ buying" — the visibility invariant was violated by forces outside the code.
- **Response (same day):** ran `STOCK_BT_SYMBOLS=RY,TD,BNS,CM,SU stock_backtest.py` on
  the NYSE cross-listings (USD — .TO validation does NOT transfer across listings).
  Report `logs/stock_backtest_20260717.md`:
  **PASS: RY (PF 1.60–5.77), TD (1.46–7.47), CM (1.87–6.28)** → whitelisted.
  **BNS: gate-letter FAIL** (9 full-window trades < 10; PF 3.35–11.29 everywhere) —
  held out, re-eligible on a future re-screen. **SU: real FAIL** (full PF 0.97, 500d
  0.22) — proof the CAD/USD listings genuinely differ; SU.TO's pass did not carry over.
- **stock_bot/.env updated:** RULE_WHITELIST=MRNA,AMD,RY,PLTR,GLD,TD,CM,CSCO,KO,T
  (all US-listed, all API-tradeable at that point — has since grown further). WATCHLIST
  swapped .TO→US 1:1 (BNS, SU stay watched). AC.TO/SHOP.TO remain watch-only (advisory,
  never rule-buyable).
- **Known FX sizing quirk (accepted for paper):** account is CAD; `PAPER_RISK_PCT`
  allocation is computed in CAD but US share prices are USD, so USD positions can run
  ~35% over the 20% target (e.g. 3 TD shares ≈ $267 CAD ≈ 27%). Exposure/sector caps
  still bound it. Fix deliberately deferred — sizing change = measurement change;
  revisit before live.

### Contract-mapping bug found + fixed same day — bare NYSE cross-listings were routing to TSX (2026-07-17 evening) — 243 tests pass
The whitelist swap above (RY/TD/BNS/CM/SU as bare US tickers) didn't fully close the Error 201
hole. `IBKRExecutor.to_contract()` built bare symbols as `Stock(sym, "SMART", "USD")` with no
`primaryExchange` — for names whose *primary* listing is Toronto, IBKR's SMART/USD
qualification resolved the ambiguous symbol back to the TSX/CAD contract despite the USD
request. Live symptom: CM's rule-BUY signal fired and was rejected by the same CIRO Error 201
**8 times over ~4 hours** (12:13–16:13 ET) before this was caught — a live no-op each time
(no fill, no state change) but silently repeating.
- **Fix:** `to_contract()` now forces `primaryExchange="NYSE"` for a known cross-listed set
  (`_NYSE_CROSS_LISTED = {RY, TD, BNS, CM, SU}`) so the USD/NYSE contract wins over the
  CAD/TSE one. Symbols with only a US listing (MRNA, AMD, PLTR, GLD, CSCO, KO, T) are
  unaffected — `primaryExchange` stays unset for those, as before.
- Tests: `test_contract_mapping_nyse_cross_listed` (new) + `test_contract_roundtrip` extended
  to cover bare RY/CM. Suite 242 → 243.

### IBKR paper executor built + verified (2026-07-17) — roadmap item D, 239 tests pass
IBKR paper environment set up end-to-end and the executor written the same day. No strategy
files touched (hash `659d1c03987b72fd` still valid); stock bot NOT switched yet at this point
(the switch happened later the same day — see entry above).
- **Accounts:** live U26459664 (approved, deliberately UNFUNDED — paper needs no funding);
  paper **DUQ273338** (CAD-denominated). Paper page is HIDDEN in the portal menu on unfunded
  accounts — deep link: `interactivebrokers.com/sso/resolver?action=AccountSettings&config=PaperTrading`.
  Paper login = usual credentials with the Live/Paper toggle set to Paper.
- **Mac setup:** classic TWS at `~/Applications/Trader Workstation/` (paper mode, API port
  7497, localhost-only, Read-Only off, "Bypass Order Precautions for API Orders" checked).
  **`/Applications/IBKR Desktop` is the WRONG app** (no API support, installed by mistake
  first, kept for manual use) — never point the bot at it. TWS must be running + logged in
  for the executor to work.
- **`stock_bot/execution/ibkr.py` — `IBKRExecutor(StockExecutorBase)`:** full drop-in for
  StockPaperExecutor (same extra methods main.py calls, same pre-trade sanity gates, same
  daily-loss breaker + sector cap semantics — risk behavior unchanged, only fills are real).
  Dedicated event-loop daemon thread + `run_coroutine_threadsafe` (scan loop AND SL/TP
  watcher call it concurrently). Guards: live ports 7496/4001 raise without
  `IBKR_ALLOW_LIVE=true`; connected account must start "DU". Market orders wait for fill;
  on timeout cancel-then-recheck (a fill racing the cancel is recorded — Jul-15 lesson,
  and it actually happened in the smoke test: TWS reported `filled=0` on a cancelled BUY
  that later filled server-side, leaving an orphan share we cleaned up).
  Contract mapping `RY.TO` ↔ `Stock('RY', SMART, CAD, primaryExchange=TSE)`; realized P&L
  + starting_cash persist in `stock_bot/ibkr_state.json`; fills append to
  `stock_bot/ibkr_trades.csv` (frozen 9-col schema; both gitignored).
- **Library: `ib_async` 2.1.0** (maintained successor of the archived ib_insync — same
  API; earlier roadmap notes saying "ib_insync" mean this). Side effect: pip downgraded
  tzdata 2026.2→2025.3.
- **Wiring:** `STOCK_EXECUTOR=paper|ibkr` in stock_bot/.env (+ IBKR_HOST/PORT/CLIENT_ID/
  ALLOW_LIVE, defaults documented there). `main.py` instantiates by type; a failed IBKR
  connection raises at startup — never a silent fallback to sim fills.
- **Verified against live TWS:** read paths PASS; 1-share KO round trip PASS (BUY $83.52 /
  SELL $83.49). TWS error 10349 ("TIF set to DAY") is a WARNING, not a rejection.
  Smoke CLI: `.venv/bin/python ibkr_smoke.py [--trade SYMBOL]`.
- **Account reset $1M → $1,000 CAD requested 2026-07-17** (processes overnight; portal
  only offers preset $250k or "Other Amount" free entry). Local ibkr_state.json deleted so
  starting_cash re-seeds from the reset account.
- Tests: `test_ibkr_executor.py` (17, hermetic FakeIB — no network/TWS). Suite 222 → 239.

### Monthly automated re-screen + crypto re-research (2026-07-16) — 222 tests pass
User direction: "make the crypto bot more powerful" — interpreted as research freely +
automate evidence refresh; live-money gates unchanged (validation before capital, caps stay).
- **`rescreen.py` (new):** monthly orchestrator — runs `screen_universe.py` (Kraken CAD
  auto-discovery, so no hardcoded symbol list; includes BTC/CAD → live-symbol edge decay
  is caught) and `stock_backtest.py` (full WATCHLIST → re-validates every RULE_WHITELIST
  symbol). Compares PASS lists to live whitelists; flags 🔻 EDGE DECAY (whitelisted but
  failed) and 🆕 NEW QUALIFIERS (passed but not whitelisted). Writes
  `logs/rescreen_<date>.md` + Telegram alert on any flag. **Never changes a whitelist**
  — additions/removals stay manual per Validation Discipline.
- **Scheduler:** third job in `_scheduled_audits_loop` — monthly, 1st-of-month anchored
  at RESCREEN_AUDIT_TIME (default 12:20 local), catch-up mid-month if the bot was down,
  90-min subprocess timeout (jobs now carry per-job timeouts). `RESCREEN_ENABLED=false`
  disables. `_audit_due()` gained `monthly_first`; tests in `test_audit_scheduler.py` (14).
- **Fresh crypto re-screens (2026-07-16), all still FAIL but the picture moved:**
  - SOL/CAD: PF now 1.48/1.35/1.40 (all ≥ 1.2 — was all < 1.0 on 2026-07-02!) — fails
    ONLY the 79% SL-exit gate. ETH 1.05/1.43/0.73 + SL 79%; XRP 0.94/0.98/1.11 + SL 88%.
  - SYN/USD still PF-strong (1.80/2.56/2.39) + SL 79%; LINK regressed (1000c PF 0.77);
    PAXG/USD failed on a 1-trade recent window (2.36/3.65/0.00 — noise, but a fail).
  - **ATR experiment on SOL:** ATR×2.0 PASSES the full gate in-sample (PF 1.49/1.56/1.20,
    SL 79%→46%) — but single-multiplier pass with both neighbors failing (×1.5 and ×2.5
    fail) = curve-fit risk. Report `logs/atr_sl_experiment_20260716.md`. BTC control:
    ATR×2.0–3.0 beat fixed 1.5% SL in this window (×2.0: PF 2.07 vs 1.65). Next research
    step: OOS split for SOL@ATR×2.0 and a proper BTC ATR study (both done 2026-07-17,
    see entries below).
- Crypto HALT LIFTED 2026-07-15 ~23:20 local — user chose "Resume buying" when asked
  explicitly. Bot logged "HALT lifted"; the fixed fill-recorder code was already running
  (22:45 restart), so the resume precondition was met. BTC/CAD live again: $77 slot cap,
  capital gate 0/15, all risk gates active. (Superseded again 2026-07-17 — user sold all
  BTC, see the crypto halt memory file for the full timeline.)

### SOL ATR×2.0 OOS validation — HOLDS (2026-07-17) — 243 tests pass, no config changed
Closed the "next research step" from the entry above: `atr_oos_validation.py` (new) splits
one 5000-candle SOL/USDT fetch by DATE into two genuinely non-overlapping halves — unlike
`atr_sl_experiment.py`'s nested 5000c/3000c/1000c windows (1000c sits entirely inside 5000c),
these share zero candles. Same cfg-from-.env strategy params, same PF≥1.2/trades≥10/SL≤70%
gate as the screen.

| Period | Window | Trades | Win% | PF | SL rate | Return |
|--------|--------|--------|------|-----|---------|--------|
| TRAIN (2024-04-05→2025-05-26) | fixed 1.5% | 38 | 16% | 1.25 | 84% | -4.84% |
| TRAIN | ATRx2.0 | 28 | 39% | **1.40** | 46% | -1.38% |
| VALIDATION (2025-05-27→2026-07-17) | fixed 1.5% | 30 | 23% | 1.79 | 73% | -2.13% |
| VALIDATION | ATRx2.0 | 24 | 38% | **1.64** | 46% | -0.38% |

**Verdict: HOLDS.** ATRx2.0's PF and 46% SL-exit rate reproduce on validation-half data the
strategy never saw during the original screen — PF didn't collapse, it held (even ticked up
1.40→1.64). Both halves clear the 10-trade floor. This resolves the curve-fit concern flagged
2026-07-16 (single multiplier passing with both neighbors failing on nested windows).
Report: `logs/atr_oos_SOL_2.0_20260717.md`. Reusable for future symbols/multipliers via
`SYMBOL=`/`ATR_MULT=` env vars.

**This is evidence, not a promotion.** SOL/CAD stays BLOCKED — adding it still requires,
per CLAUDE.md's USD Expansion preconditions: SL-distance-based position sizing (a wider ATR
stop must not raise dollar risk per trade beyond the standard cap) + BTC/CAD ≥15 fills +
PF≥1.2, capital ≥$500, documented FX handling + a fresh full walk-forward pass on the
CURRENT strategy hash at promotion time. No .env or whitelist change made.

### BTC ATR×2.0 OOS validation — HOLDS, and this one is live-relevant (2026-07-17)
Same `atr_oos_validation.py`, `SYMBOL=BTC/USDT ATR_MULT=2.0` — the "proper BTC ATR study"
the 2026-07-16 entry flagged as still pending. Same non-overlapping date split as the SOL run.

| Period | Window | Trades | Win% | PF | SL rate | Return |
|--------|--------|--------|------|-----|---------|--------|
| TRAIN (2024-04-05→2025-05-26) | fixed 1.5% | 26 | 19% | 1.34 | 81% | -2.69% |
| TRAIN | ATRx2.0 | 21 | 33% | **1.79** | 52% | -0.39% |
| VALIDATION (2025-05-27→2026-07-17) | fixed 1.5% | 12 | 33% | 1.88 | 58% | -0.94% |
| VALIDATION | ATRx2.0 | 11 | 45% | **2.04** | 45% | -0.68% |

**Verdict: HOLDS,** and PF improved out-of-sample (1.79→2.04) rather than merely surviving —
win rate improved too (33%→45%). Fixed 1.5% SL fails the full gate outright on the training
half (81% SL-exit rate, over the 70% cap); ATRx2.0 roughly halves it in both halves (52%→45%).
Caveat: validation trade count is thin (11–12), just over the 10-trade floor — worth more
months of confirmation before treating as fully settled. Report: `logs/atr_oos_BTC_2.0_20260717.md`.

**Why this one was different from the SOL result: `ATR_SL_MULT` is a live-wired config knob,
not a new-symbol question.** `bot/main.py:1813` already reads `cfg.strategy.atr_sl_mult`
(env `ATR_SL_MULT`) to compute the live SL level. Switching it on was a config change, NOT a
`bot/strategy/*.py` change — it did not invalidate strategy hash `659d1c03987b72fd`. Given the
2026-07-02 ATR SL drift incident (a stale .env key silently ran ATR SL on backtest at the
wrong multiplier), the adoption double-checked BacktestConfig and StrategyConfig read the
identical `ATR_SL_MULT` key before trusting the result.

**Follow-up same day — full 5-window walk-forward** (`atr_walkforward.py`, new; same
nested-trailing-window shape — 5000/4000/3000/2000/1000 candles ending at present — used for
every prior BTC strategy sweep):

| Window | fixed 1.5% PF | ATRx2.0 PF | fixed SL rate | ATRx2.0 SL rate |
|--------|---------------|------------|----------------|-------------------|
| 5000c | 1.65 | **1.98** | 71% | 49% |
| 4000c | 2.09 | **2.33** | 66% | 42% |
| 3000c | 3.03 | **3.76** | 55% | 35% |
| 2000c | 2.56 | **2.88** | 56% | 38% |
| 1000c | 1.70 | **2.39** | 67% | 40% |

**Both variants pass PF > 1.0 on all 5 windows, but ATRx2.0 beats fixed 1.5% on every single
window** — higher PF and 20–30 points lower SL-exit rate everywhere, not a marginal edge.
Combined with the non-overlapping OOS split above (train 1.79 → validation 2.04, held), this
was validated by both methods this project uses (recency-robustness sweep + temporal
holdout) and both were clean. Report: `logs/atr_walkforward_BTC_2.0_20260717.md`. This was
the last research gate before the adoption decision — see next entry.

### ATR SL adopted live — ATR_SL_MULT 0.0 → 2.0 (2026-07-17) — 243 tests pass, strategy hash unchanged
User approved after reviewing both validation results above. `.env`: `ATR_SL_MULT=0.0` → `2.0`.
`STOP_LOSS_PCT=0.015` stays in place as the documented fallback — `bot/main.py:1813` already
falls back to it (logging "ATR SL disabled or unavailable — using fixed SL/TP") on any entry
where ATR can't be computed; this is pre-existing, tested behavior, not new code.
- **This is a config change, not a strategy change** — `bot/strategy/*.py` untouched, hash
  `659d1c03987b72fd` stays valid, no `stamp_strategy.py` re-run needed (per Validation
  Discipline: config/execution/risk/data/test changes do NOT invalidate the hash).
- **Verified same day:** `EXCHANGE=binance SYMBOL=BTC/USDT python backtest.py` returned
  35 trades / PF 1.98, exactly matching the 5000c row of the walk-forward report — confirmed
  `.env` and the backtest/live code paths agree.
- **Full suite (243) re-run after the `.env` edit — unaffected.** SUPERSEDED 2026-07-18:
  walkforward.py now builds its engine kwargs from the shared `engine_kwargs_from_cfg()` and
  runs the full live config, ATR SL + sizing included — see "Validation-script config drift
  fixed" entry below.
- Two new reusable research scripts added this session: `atr_oos_validation.py` (non-overlapping
  train/validation split, any symbol/multiplier via `SYMBOL=`/`ATR_MULT=`) and
  `atr_walkforward.py` (canonical 5000/4000/3000/2000/1000 trailing-window sweep, same env vars).
- Any position already open at restart keeps whatever SL was set at its own entry.
- **Watch for going forward:** expect a lower stop-out rate on the next live SL exits (walk-forward
  showed 71%→49% on the fullest window) and possibly wider-than-1.5%-stop losses on any trade that
  does hit stop, since ATR-based stops are usually looser than the old fixed 1.5%. This trades
  fewer-but-larger occasional losses for meaningfully fewer stop-outs overall.

### Ops + research build session (2026-07-17 evening) — 264 tests pass, strategy hash unchanged
Four builds in one session ("build all" from the post-adoption roadmap discussion). No
`bot/strategy/*` files touched — hash `659d1c03987b72fd` still valid.

**1. DISCOVERY: no outbound alert channel had EVER worked.** `TELEGRAM_*` keys were absent
from `.env` (TelegramAlerter constructs disabled, silently) and the stock bot's
`ALERT_EMAIL_FROM/TO/PASSWORD` were empty. Every "Telegram alert" described anywhere in this
project history — drift escalation, HALT engage/lift, daily P&L, fill alerts, candle
watchdog — only ever reached the log files. The code paths exist and are tested; the
delivery channel was never configured. Response is the heartbeat inversion below (no
credentials needed in repo) plus — same evening — actual BotFather setup: **Telegram
configured and verified 2026-07-17** (bot t.me/amaresh_tradebot, TELEGRAM_* keys in root
`.env`, test message delivered). Crypto-bot alerts went live at its next restart. **Stock bot
wired to the SAME Telegram channel (same evening):** `_make_telegram()` in
`stock_bot/alerts/notifier.py` sources TELEGRAM_* from the ROOT `.env` (one token, one
revoke point; process env overrides); `AlertNotifier` gained `fill()` (BUY/SELL fills with
P&L + reason, tagged sim/IBKR-paper), `ops_alert()` forwards (TWS disconnects, Sunday
reminder), and `notify()` relays HIGH-priority scan alerts only (MEDIUM chatter excluded by
design — same filter as desktop). `bot/alerts/telegram.py` gained a generic `message()`
method. Fill sites wired in `stock_bot/main.py`: scan-loop BUY/SELL + both SL/TP-watcher
exits. Tests: `test_stock_telegram.py` (7, hermetic — injected credentials, mock alerter).

**2. Heartbeat dead-man's switch:** `bot/alerts/heartbeat.py` (shared by both bots, same
cross-package import pattern as stock rules). Bot pings a healthchecks.io check URL every
60s; the service emails when pings STOP — covers process death, Mac sleep, and network loss
with zero secrets in the repo. Env (all empty = off): `HEARTBEAT_URL` in `.env` (crypto) and
in `stock_bot/.env` (stock process), plus `HEARTBEAT_TWS_URL` (stock; pinged only while
`executor.is_connected()` — separates "TWS logged off" from "bot died"). Fail-silent by
design; a broken healthy_fn counts as unhealthy (no ping) so monitoring can't mask an
outage. Tests: `test_heartbeat.py` (8). **healthchecks.io LIVE (2026-07-17 21:19, same
night):** 3 checks (crypto-bot / stock-bot / stock-tws), period 5 min, grace 10 min, all
attached to the account email channel (API-created checks get NO notification channel by
default — had to be assigned explicitly). Ping URLs in the three env keys; bots restarted
21:19; all checks verified "up" with pings received. Dead-bot detection now exists: bot
death, Mac sleep, network loss → email within ~10 min; TWS logoff → stock-tws email +
Telegram ops alert.

**3. TWS disconnect alert + Sunday re-login reminder:** `stock_bot/alerts/tws_monitor.py`
(`TwsConnectionMonitor`, pure state machine — blip-tolerant, alert-once per outage,
recovery notice only after an alert; tests `test_tws_monitor.py` 6). Wired as a 60s
monitor thread in `stock_bot/main.py` (IBKR executor only): 10+ min disconnect →
`notifier.ops_alert()` = log WARNING + terminal + desktop notification (plyer). The
Sunday-18:00 weekly-summary timer now also fires a TWS re-login reminder when
`STOCK_EXECUTOR=ibkr` (IBKR forces weekly Sunday re-auth; without it every Monday order
fails). `TWS_DISCONNECT_ALERT_MIN=10` to tune. Local-only delivery by design — the remote
email leg is HEARTBEAT_TWS_URL.

**4. ATR-aware position sizing — built, validated, adopted the same night.** Gap: with
`ATR_SL_MULT=2.0` live, stop distance varies per entry but sizing was plain notional
(`calc_trade_qty`), so a wide-ATR entry risks MORE dollars at its stop than the validated
fixed-SL baseline (cash × 10% × 1.5% = 0.15% of cash). New
`AppConfig.calc_trade_qty_atr_risk()` caps qty so a stop-out never exceeds that baseline;
a tight ATR stop does NOT size up past standard notional (min, not equality). Wired:
engine (`atr_risk_sizing` + `atr_sizing_baseline_sl_pct` params, default off),
`backtest.py`, live BUY path in `bot/main.py` (behind `ATR_SIZING_ENABLED`, same one key
read by BacktestConfig AND StrategyConfig — drift-incident rule), `atr_walkforward.py`
pass-through. Tests: `test_atr_sizing.py` (7). **Validation run (sizing ON, BTC ATRx2.0,
5-window):** PF 1.90/2.24/3.46/3.29/2.03 — all 5 windows > 1.0 and every window still
beats fixed-SL. Canonical fingerprint with flag OFF re-verified same session: 35 trades /
PF 1.98, unchanged. Report `logs/atr_walkforward_BTC_2.0_20260718.md` (UTC date).
**ADOPTED same night (user approved): `ATR_SIZING_ENABLED=true` in `.env`.** Fresh
`backtest.py` returned 35 trades / PF 1.90 (matches the sizing-ON 5000c walk-forward row
exactly — env/code agreement verified at adoption). Note: at $77 slot cash the cap rarely
binds (the existing 98%-affordability clamp dominates); this matters as capital grows.

**5. SYN/LINK ATR OOS validations run:** same `atr_oos_validation.py` non-overlapping split
as the SOL/BTC runs.
- **SYN/USDT: HOLDS cleanly at both mults** — ATRx2.0 train PF 1.57 / validation 1.99
  (SL 25%/21%); ATRx2.5 train 2.04 / validation 1.86 (SL 11%/14%). Trades 19–28 per half.
- **LINK/USDT: mixed — not candidate-grade.** ATRx2.0 FAILS the train half outright
  (PF 0.85); ATRx2.5 passes both halves but thin margins (1.29 train / 1.61 validation).
  A multiplier that only works at one setting and fails at its neighbor on half the data
  is the same curve-fit profile the OOS script exists to catch. LINK stays out.
- Reports: `logs/atr_oos_{SYN,LINK}_{2.0,2.5}_20260717.md`. SYN remains blocked on the
  unchanged USD-pair preconditions.

**Two bugs caught during the restart cycle — both mine, both fixed, both carry a lesson:**
- **`stock_bot/main.py` had no `import os`** — the new heartbeat block used `os.getenv`,
  so the first restart (20:30) died with NameError right after "Weekly summary timer
  armed" (traceback to terminal only; the bot was down flat with TWS untouched).
  `import stock_bot.main` had passed pre-restart — an import check does NOT execute
  `run()`; runtime-path code needs runtime-path verification.
- **`IBKRExecutor.is_connected` is a PROPERTY, not a method** — the TWS monitor called
  the returned bool ("'bool' object is not callable" warning every 60s) and the TWS
  heartbeat's `healthy_fn=executor.is_connected` had evaluated the property ONCE at
  wiring time (frozen True — would never have detected a disconnect once a URL was set).
  Fixed with `lambda: bool(executor.is_connected)` / `bool(executor.is_connected)`;
  wiring pattern now exercised against a property-based fake.
- **Stock bot startup Telegram message added** (`notifier.startup()` — executor type,
  cash, position count): a silent boot is indistinguishable from a broken channel.

**Crash-hardening audit follow-up (same night) — 280 tests pass:**
- **Fatal-crash Telegram alert, both bots:** `__main__` wraps `run()` — any unhandled
  exception logs the full traceback (logger.critical) and fires a synchronous
  "💀 ... CRASHED" Telegram via new `TelegramAlerter.send_now()` (the usual daemon-thread
  send races process teardown), then re-raises. Helpers `_send_crash_alert` in both mains;
  never raise, no-op when Telegram is off.
- **Atomic state writes:** new `bot/atomic_json.py` (tmp + `os.replace`) now used by
  `live_executor._save_state`, `ibkr._save_state_json`, and the audit-state write in
  `bot/main.py`. RiskManager already wrote atomically — pattern now shared. A crash or
  power loss mid-write can no longer truncate live position/cash state.
- **SIGTERM = graceful shutdown:** crypto registers its SIGINT handler for SIGTERM too;
  stock bot routes SIGTERM into its KeyboardInterrupt path.
- Audit items NOT built (deliberate): launchd auto-start, requirements lockfile, lid-close
  sleep (ops habit / future VPS), FX sizing (deferred pre-live), TWS auto-restart setting
  (user side).

**Post-restart verified state (2026-07-17 ~20:40):** crypto bot live with Telegram
(startup message delivered 20:31); stock bot live on IBKR (DUQ273338, $995.30, flat),
TWS monitor running clean; Telegram end-to-end confirmed by user.

### Validation-script config drift fixed — shared engine-kwargs builder (2026-07-18) — 286 tests pass
Follow-up to the ATR adoptions: audit found `walkforward.py` hand-listed a PARTIAL, stale
engine.run() arg set — it was validating a config that differed from live in at least six
ways: `volume_k` missing (engine default 1.2 vs validated 0 = filter OFF), `min_ema_spread_pct`
missing (0.002 vs validated 0.004), a stale hardcoded 0.5% `max_ema_spread_pct` ceiling
(live runs 0.0 = disabled), `adx_max`/regime/partial-TP params missing, and NO ATR keys
(`ATR_SL_MULT`, `ATR_SIZING_ENABLED`) — so the Validation Discipline workflow's step 3 ran a
config nobody trades. Same failure class as the 2026-07-02 ATR SL drift incident.
- **Fix: `bot/backtest/params.py` — `engine_kwargs_from_cfg(cfg)`** is now THE single source
  of engine kwargs (same pattern as the stock bot's `build_indicator_config()`). Both
  `backtest.py` (CLI flags override on top) and `walkforward.py` use it; a test pins that
  they keep doing so and that every emitted key is accepted by `engine.run()`.
  Tests: `test_engine_params.py` (6, hermetic — fake cfg, never reads live `.env`).
- **Fingerprint parity verified:** refactored `backtest.py` reproduces 35 trades / PF 1.90,
  hash `659d1c03987b72fd` — behavior byte-identical to pre-refactor baseline run same day.
- **New walkforward.py reference numbers (full live config: ATR SL 2.0 + sizing ON,
  VolK=0, EMA≥0.4%, run 2026-07-18):** training (2024-04-05→2025-02-21, 17 trades)
  PF 1.16 / −1.11%; validation (2025-02-22→2026-07-18, 18 trades) PF 3.00 / +0.22% —
  PASS (PF holds ≥1.2 out-of-sample). Prior walkforward numbers in project history were
  produced under the old drifted arg set and are not comparable.
- **False-positive CONFIG DRIFT warning fixed:** `ATR_SIZING_ENABLED` was missing from
  `_KNOWN_STRATEGY_ENV_KEYS` in `config.py` (the adoption added the reader but not the
  drift-guard whitelist), so EVERY config load — including live bot startups — logged
  "unrecognised strategy keys: ATR_SIZING_ENABLED". The key was always read correctly
  (PF 1.90 = sizing ON confirms); only the warning was wrong. Whitelist updated.

### TWS "restored" notice actually fires now — reconnect probe (2026-07-18) — 289 tests pass
First real outage (TWS daily logoff, Sat 08:00 ET) proved the down-alert works end-to-end
(user received it), and exposed the recovery half's gap: `is_connected` only REPORTS the
socket state, ib_async never redials on its own, and `_ensure_connected_async()` ran only
at order placement — so after logging back into TWS the socket stayed dead, the monitor's
"restored" notice never fired, and the stock-tws heartbeat stayed red until the next order
(possibly days later on a weekend).
- **`IBKRExecutor.try_reconnect()` (new):** non-raising redial probe (non-blocking lock
  guards concurrent probes; DEBUG-logs failures). The TWS monitor thread calls it every
  5th tick (~5 min) while disconnected — a relogged-in TWS now produces the
  "TWS connection restored" ops alert within ~5 min, and the TWS heartbeat resumes
  pinging healthchecks.io on its own.
- Tests: `test_ibkr_executor.py` 18 → 21. Suite 286 → 289.

### MACD live/backtest divergence resolved — option (a), validate what's live (2026-07-20) — 289 tests pass, strategy hash unchanged
Closed the OPEN DECISION flagged during the 2026-07-18 config-drift audit. `bot/main.py` and
`shadow_signal.py` have always run with `macd_enabled=True` (MACD gates both Mode A/B
entries), but `engine_kwargs_from_cfg()` never passed it through, so `backtest.py` and
`walkforward.py` silently validated a MACD-OFF variant — live was trading a stricter,
unvalidated subset of what was on record. User chose (a): validate the strategy actually
running, not turn MACD off to preserve the old numbers.
- **Fix:** `bot/backtest/params.py` — `macd_enabled = cfg.strategy.macd_enabled` added to
  `engine_kwargs_from_cfg()`. No `bot/strategy/*.py` edit, so strategy hash
  `659d1c03987b72fd` is unchanged and `stamp_strategy.py` did not need a re-run.
  `test_engine_params.py`'s `test_macd_enabled_is_deliberately_excluded` replaced with
  `test_macd_enabled_is_sourced_from_cfg`. Suite stays at 289 (one test replaced).
- **New canonical fingerprint:** `EXCHANGE=binance SYMBOL=BTC/USDT python backtest.py` →
  **32 trades, PF 1.72, 37.5% win rate** (was 35 trades / PF 1.90 / 40.0% under the old
  MACD-OFF backtest). This is now what a fresh run reproduces (still current as of the
  time this history file was split out — see CLAUDE.md for the live figure).
- **Walk-forward (`walkforward.py`, train 2024-04-08→2025-02-21 / validation 2025-02-22→2026-07-20):**
  training 15 trades, PF 1.09; validation 17 trades, PF **2.30**, 35.3% win. PF holds well
  out-of-sample — interpretation printed by the script: "Strong: PF holds above 1.2
  out-of-sample. This config may have a genuine edge worth pursuing."
- **Nothing else changes:** ADX/EMA/RSI/ATR thresholds, sizing, SL/TP — all untouched. This
  was purely closing a validation-script fidelity gap, not a strategy or risk change.

### Audit follow-up same day — Mode A/B entry params had the identical drift gap (2026-07-20) — 291 tests pass, strategy hash unchanged, fingerprint unchanged
Requested self-audit after the MACD fix ("find any other misjudgments"). Found one real
structural gap of the same shape, currently dormant.
- **The gap:** seven entry-mode parameters — `pullback_rsi_min/max`, `breakout_rsi_min/max`,
  `breakout_lookback`, `max_price_extension_pct`, `breakout_adx_threshold` — are live-configurable
  (`bot/main.py build_strategy()` sources all seven from `cfg.strategy`, which is `.env`-backed),
  but `engine.run()` didn't accept them as parameters at all, and `engine_kwargs_from_cfg()`
  didn't emit them. The backtest silently used `IndicatorConfig`'s hardcoded dataclass defaults
  regardless of `.env` — the same failure shape as the 2026-07-02 ATR SL incident and the
  macd_enabled gap just above, just with different variables.
- **No live impact found:** `.env` has never overridden any of the seven, so live and backtest
  happened to agree by coincidence, not by design. It was one future `.env` tuning edit away
  from silently repeating the incident.
- **Fix:** all seven added to `engine.run()`'s signature (`bot/backtest/engine.py`) and to
  `engine_kwargs_from_cfg()` (`bot/backtest/params.py`), with defaults matching
  `IndicatorConfig`'s own dataclass defaults — a no-op today, live protection going forward.
- **Fingerprint reconfirmed unchanged:** fresh `backtest.py` run still returns exactly
  32 trades, PF 1.72 — proves the fix is behavior-neutral at current `.env` values.
- **Future-proofed:** `test_engine_params.py` gained
  `test_every_shared_strategy_field_reaches_the_backtest` — introspects the REAL
  `StrategyConfig` and `IndicatorConfig` dataclasses and asserts every field name present on
  BOTH is sourced by `engine_kwargs_from_cfg()` and accepted by `engine.run()`. A future field
  added to both dataclasses with the same name but never wired through will fail this test
  immediately instead of sitting silent for weeks. Suite 289 → 291.
- **Audited and found clean (no fix needed):** strategy hash genuinely in sync with
  `logs/validated_strategy_hash`; zero "STRATEGY CODE DIFFERS" or "unrecognised strategy
  keys" warnings in either bot's full log history; stock bot's `build_indicator_config()`
  is structurally immune to this bug class; no stale/orphaned `.env` keys; no silent
  `except: pass` exception-swallowing found in either bot's execution/risk code.

### Second audit pass same day — ATR_TP_MULT removed (dead config), drawdown breaker documented (2026-07-20) — 291 tests pass, strategy hash unchanged, fingerprint unchanged
User asked for a further pass. Two more findings, both closed same session.

**1. `ATR_TP_MULT` was fully-configured dead code — removed.** It had a dataclass default
(4.0), an `.env` reader, a validator, a startup log line, and a `_KNOWN_STRATEGY_ENV_KEYS`
whitelist entry — everything a real config field has. But nothing ever computed a value from
it: `bot/main.py`'s exit check was built to use `ss['atr_tp']`, but `ss['atr_tp']` was only
ever initialized/reset to `0.0` — the line that would have set it to
`order.price + atr × atr_tp_mult` on BUY was never written. `engine.run()` never had it as a
parameter either. Net effect: TP has always been the fixed `TAKE_PROFIT_PCT` (10%), live and
backtest, completely independent of `ATR_TP_MULT` — the field looked live and never was.
Chose removal over finishing the feature: an unfinished feature masquerading as configured
is worse than no feature, and implementing real ATR-TP would be new research.
- Removed: `StrategyConfig.atr_tp_mult` field + validator + `_load()` reader + startup log
  arg; `ATR_TP_MULT` from `_KNOWN_STRATEGY_ENV_KEYS`; `ss['atr_tp']` init/reset (4 sites) and
  the dead `_ic_tp` ATR branch — TP check is now just the fixed-percent condition,
  unconditionally. Also removed an adjacent always-unused local `_atr_tp_price`.
  Log messages "ATR SL disabled or unavailable — using fixed SL/TP" → "...using fixed SL" and
  "ATR SL/TP [%s]: ..." → "ATR SL [%s]: ..." (both only ever reported SL).
- `.env` never set `ATR_TP_MULT`, so this was a pure no-op for live/backtest behavior. Fresh
  `backtest.py` run reconfirmed unchanged: 32 trades, PF 1.72.

**2. `max_drawdown_pct` backtest/live divergence — documented, not changed.** Audit-flagged:
`engine_kwargs_from_cfg()` hardcodes `max_drawdown_pct=0.25` (25%) with zero comment, while
live's `RiskManager` runs at `cfg.risk.max_drawdown_pct=0.05` (5%, a real .env-driven value).
Decided this is intentional and left it as-is: backtests/walk-forward measure the strategy's
raw signal quality without being reshaped by where live's capital-protection breaker would
have halted new BUYs. The breaker itself is a separate, independently-tested safety layer
that runs for real in live/paper trading and doesn't need re-proving inside every backtest.
Documented in `bot/backtest/params.py`'s module docstring and an inline comment at the
`max_drawdown_pct = 0.25` line itself. No code behavior changed.

### Third audit pass same day — 4 orphaned StockConfig fields removed (2026-07-20) — 291 tests pass, no behavior change
Continued the audit further into the stock bot (crypto side came back clean on this pass).
Ran the systematic "is every config field actually consumed" sweep against `StockConfig` and
found four fields with a milder version of the same shape: defined, `.env`-loaded, validated
where applicable — but never actually read through `cfg` anywhere.
- **`nvidia_api_key`, `nvidia_model`:** `stock_bot/ai/ai_engine.py` reads `NVIDIA_API_KEY` /
  `NVIDIA_MODEL` directly via `os.getenv()`, bypassing `cfg` entirely. The AI provider works
  correctly — these two `StockConfig` fields were just an unused duplicate path.
- **`price_outlier_factor`:** same pattern — `stock_bot/data/price_feed.py` reads
  `PRICE_OUTLIER_FACTOR` directly via its own module-level `os.getenv()`. The guard works
  correctly; the `StockConfig` field was the unused duplicate.
- **`base_currency`:** zero consumers anywhere — looks like a stub for a mixed-currency
  portfolio display that was never built.
- **Not a live bug in any case** — unlike `ATR_TP_MULT`, nothing was ever built to consume
  these through `cfg`, so there was no half-wired feature silently failing.
- **Removed:** all four field declarations, `_load()` reader lines, and their inline comments
  from `stock_bot/config.py`. Confirmed via `hasattr(cfg, ...)` returning `False` post-removal.
- **`.env` left untouched** — `NVIDIA_API_KEY`/`NVIDIA_MODEL` in `stock_bot/.env` are still
  live and required (read directly by `ai_engine.py`); `PRICE_OUTLIER_FACTOR` isn't even set
  there; `BASE_CURRENCY=CAD` is now fully inert but harmless to leave.
- Full suite reconfirmed: 291/291 (same count — no test referenced any of the four fields).

### Fourth audit pass same day — position-sizing docstring fixed, 2 dead sizing methods removed (2026-07-20) — 291 tests pass, fingerprint unchanged, no behavior change
Continued the audit into `config.py`'s position-sizing methods — the highest-stakes area
checked yet, since it directly determines real dollar risk per trade.

- **`calc_trade_qty()` — the function that sizes every live BUY — had a wrong docstring.**
  It claimed "industry standard fixed-fractional method... risk exactly risk_per_trade_pct
  of current cash per trade," but the formula (`cash × risk_per_trade_pct / price`) is
  **notional allocation** (invest X% of cash), not risk-based sizing (size so a stop-out
  costs X% of cash) — those are different formulas with very different outputs. Textbook
  fixed-fractional risk sizing is `(cash × risk_pct) / stop_distance`; this function never
  looks at a stop distance at all.
- **What this means for real risk, verified numerically:** with the live config
  (`RISK_PER_TRADE_PCT=0.10`, fixed SL fallback 1.5%), actual dollar loss if a stop-loss hits
  is `10% × 1.5% = 0.15%` of cash (≈$0.12 on the $77 slot at the time) — not the 10%/$7.70 the
  old docstring implied. This is good news (real risk is far more conservative than the "10%,
  intentionally high" framing elsewhere suggests) but means "RISK_PER_TRADE_PCT" had been
  informally read as "% risked" when it's actually a capital-allocation dial that combines
  with the SL% to produce a much smaller real risk number. Not dangerous — mislabeled.
  **No live risk-per-trade number changed as a result of this entry** — correction to what
  the number MEANS, not a change to it.
- **Two fully dead sizing methods removed:** `calc_trade_qty_sl()` (correctly implemented the
  real textbook fixed-fractional method — its own docstring was accurate) and
  `calc_trade_qty_atr()` (ATR-based sizing without the fixed-SL-baseline cap). Neither was
  called anywhere. `calc_trade_qty_atr_risk()` — the method that DOES run live whenever
  `ATR_SIZING_ENABLED=true` — is the actual real-dollar-risk cap in production, and its own
  docstring was already accurate; left untouched.
- **Not acted on, noted only:** actual risk-per-trade (~0.15% of cash) is quite conservative
  by expert standards — there would be room to size up within the standard 1–2% band if
  faster statistical signal on the capital gates is ever wanted. Deliberately left as a
  future decision for the user, not changed here.
- Verified: full suite 291/291 pass; fresh `backtest.py` run reconfirmed the canonical
  fingerprint unchanged (32 trades, PF 1.72).

### IBKR $2,500 CAD equity floor discovered + guarded — real root cause of zero stock fills (2026-07-20) — 293 tests pass
CM's rule strategy produced its first live BUY signal (RULE BUY, contract correctly routed
NYSE/USD by the 2026-07-17 contract-mapping fix) and IBKR rejected it: **Error 201 —
"YOUR ORDER IS NOT ACCEPTED. MINIMUM OF 2500 CAD ... IS REQUIRED IN ORDER TO ... TRADE
CURRENCY."** IBKR treats buying a USD-denominated security from a CAD-base account as an
implicit margin/currency trade and refuses it outright below $2,500 CAD equity — unrelated to
order size.
- **This was the actual reason the stock bot had zero fills since the 2026-07-17 IBKR switch**
  — not just "signals are rare." The account (DUQ273338) held ~$995 CAD, and every single
  RULE_WHITELIST symbol at that point was USD-denominated. Every future rule BUY on any
  whitelisted symbol would have repeated this exact rejection until the account crossed
  $2,500 CAD.
- **Fix — proactive guard, not just noise reduction:** `IBKRExecutor.buy()`
  (`stock_bot/execution/ibkr.py`) now checks `contract.currency != "CAD" and self.cash <
  _MIN_EQUITY_FOR_FX_TRADE_CAD` (2500.0, matching IBKR's own stated minimum) and rejects
  early with a clear reason — before ever placing the order. A CAD-denominated (.TO) buy is
  unaffected by the floor.
- Tests: `test_buy_rejected_low_equity_fx_trade` + `test_buy_allowed_low_equity_cad_security`
  + `test_buy_rejected_insufficient_cash` adjusted (cash raised to $3,000). Suite 291 → 293.
- **Action required (outside code):** request an IBKR paper account top-up above $2,500 CAD
  via the portal — done same week, see next entry.

### Resolved same day — paper account reset to $5,000 CAD, first live fill, starting_cash auto-rebaseline built (2026-07-20) — 297 tests pass
User requested and completed the account top-up via the IBKR portal. It landed the same day
(faster than the portal's stated "next business day") while the stock bot kept running — no
restart was needed for trading to unblock, because the $2,500-equity guard added above reads
live IBKR data (`self.cash`) on every order, not a cached value.
- **First real stock-bot fill ever:** CM's rule strategy fired again once equity cleared
  $2,500 and this time filled — 10 shares @ $117.72, `RULE BUY rsi=70 adx=23 | ai=BUY70`.
  Confirmed via a direct IBKR query (separate short-lived client connection, clientId=99,
  read-only): `NetLiquidation` = $4,999.72 CAD — matches the requested $5,000 reset almost
  exactly.
- **New gap found immediately after — `starting_cash` doesn't know about external resets.**
  `IBKRExecutor.__init__` only ever auto-seeds `starting_cash` from live `NetLiquidation` on
  the very first-ever connection then freezes it forever by design — correct for tracking
  real trading return against a stable baseline, but it has no way to detect a human manually
  resetting the paper account balance outside of any trade. After this reset,
  `ibkr_state.json` was still holding the stale pre-reset `995.30` baseline, which would have
  made every future % return calculation wrong until someone noticed and manually edited the
  file — exactly the same manual correction this project already had to do once before, after
  the 2026-07-17 $1M→$1,000 reset. That's a recurring manual chore, not a one-off.
- **Fix: `IBKRExecutor._rebaseline_if_external_change()`** (new) — runs on every connect after
  the first. At cost basis, a BUY only moves cash into inventory at no gain/loss, so absent
  any external change `net_liq` should always equal `starting_cash + realized_pnl` to within
  small unrealized mark-to-market drift on any open position. A gap bigger than
  `max($50, 2% of starting_cash)` can only come from something outside this executor's own
  trading — most commonly a manual portal reset — and triggers an automatic re-baseline
  (`starting_cash = net_liq - realized_pnl`, preserving already-tracked realized P&L) plus a
  `logger.warning` and a `save_state()` so the corrected value persists.
- Tests (new): `test_starting_cash_seeds_on_first_ever_connect`,
  `test_starting_cash_rebaselines_on_external_reset` (995.30 → 5000.0 jump triggers and
  persists), `test_starting_cash_not_rebaselined_for_small_drift` ($10 drift on a $1,000
  baseline stays untouched), `test_starting_cash_rebaseline_accounts_for_realized_pnl`
  (a $4,000 external jump on top of $50 already-realized P&L re-baselines to exactly $5,000,
  not $5,050). Suite 293 → 297.
- `stock_bot/ibkr_state.json`'s `starting_cash` manually corrected to `5000.0` this session
  (before the auto-rebaseline code existed) — future resets self-correct now.

### Affordable-symbol screen (2026-07-15) — 7 new RULE_WHITELIST symbols, 216 tests pass
Goal: widen the funnel of symbols that can actually FILL at the ~$197 target allocation
(0.20 × ~$987 account) — 3 of 5 whitelisted symbols were stuck in SIZE_SKIP, so the
30-trade Phase A gate was fed by MRNA+PLTR alone. Ran `stock_backtest.py` (same 4-window
gate, strategy hash `659d1c03987b72fd` unchanged) on 18 untested liquid candidates chosen
for affordability. Report: `logs/stock_backtest_20260715.md`.
- **PASS + whitelisted (7): TD.TO, BNS.TO, CM.TO, SU.TO, CSCO, KO, T** — all fill 1–9
  shares at current prices. TSX banks echo RY.TO's strong pass (TD full PF 2.41,
  CM 2.09, BNS 1.89, all SL ≤ 45%); CSCO 2.46/KO 2.00/T 2.20 with SL ≤ 33%.
- **PASS but held out (1): UBER** — gate-letter pass (full PF 1.32, SL 66.7%) but
  0-for-2 in the last 500d: decayed-edge profile, margin-of-safety skip. Re-eligible
  on a future re-screen if recent windows recover.
- FAIL (9): ENB.TO, CNQ.TO, PFE, BAC, DIS, TGT (9 trades < 10, PF was fine), XOM, GDX,
  XLF. MU skipped (thin yfinance history).
- Concentration note: whitelist grew to 4 Canadian banks (RY + TD + BNS + CM) at this point
  (before the .TO→NYSE swap 2 days later). Each position risks ~1% of account (20% alloc ×
  5% SL) — acceptable for paper-book data collection.
- Watchlist grew 15 → 22 symbols — watch for yfinance rate-limit pressure (known
  failure mode; 15-min price cache mitigates).

### IPO policy — no automated IPO trading (2026-07-11, agreed with user) — full detail
Trigger: SpaceX IPO'd 2026-06-12 as NASDAQ:SPCX — largest IPO in history (offer $135,
raised ~$75B, ~$1.8T valuation). Pop-and-fade played out in 3 sessions: peak $225.64 on
Jun 16, then multi-week decline to ~$145 by Jul 10. Day-1 open-market buyers ($150 open)
were underwater within a month; only offer-price allocations (institutions) kept the pop.

**Policy (standing, applies to every future IPO — kept as a one-liner in CLAUDE.md):**
- The bots never trade IPOs or recent listings via any special path. New listings earn
  entry exactly like every other symbol: accumulate history → screener eligibility
  (needs ~36 daily candles for MACD scoring) → full `stock_backtest.py` walk-forward
  PASS → RULE_WHITELIST. No exceptions for famous names.
- Rationale: (1) the IPO pop belongs to offer-price allocations, not open-market buyers —
  pooled research shows day-1 open→close averages ~zero for public buyers; (2) per-symbol
  backtesting is impossible by definition on day 1; (3) fast trading already failed our own
  validation (1h walk-forward FAILED 2026-07-10).
- Hand-trades on hype names are the user's personal decision, outside bot capital. The bot
  never touches holdings it didn't open.

**SPCX timeline:**
- ~Early Aug 2026: ~36 trading days accumulated → screener can score it; may appear in
  universe movers / AI advisory / paper-only swing-book signals automatically. No code change.
- ~Mid-2027: enough daily history for a meaningful walk-forward (250d window needs ~a year).
  Run `stock_backtest.py` on SPCX then; whitelist only on a PASS — same gate as MRNA/AMD/RY/PLTR.
- User personally holds 2 SPCX shares (visible in portfolio tracker; never bot-traded).

### Investment philosophy — two-bucket policy + plan queue (2026-07-11, agreed with user) — full detail
User defers strategy decisions ("follow the rules from great leaders and experienced persons,
not what I set"). Standing interpretation: follow *evidence* and established-investor
discipline, not hype or pasted "success rules" content.

**Two buckets:**
- **Bucket 1 — wealth building (personal, outside the bots):** Buffett's explicit guidance for
  non-professionals — low-cost broad index fund (e.g., S&P 500 ETF), regular automatic
  contributions, hold for decades (his 90/10 will instruction). The bots are NOT the wealth
  engine and must never be treated as one; studies show 70–97% of active day traders lose money.
- **Bucket 2 — trading system (this repo):** capped, gate-controlled experiment. Capital grows
  only through the documented fill-count / net-PF gates — never through conviction, streaks,
  or excitement.

**Buffett rule mapping (already enforced in code):** capital protection = risk engine +
breakers + slot caps · circle of competence = default-deny whitelists + walk-forward gates ·
patience = HOLD through weak regimes (ADX gate) · margin of safety = PF ≥ 1.2 net-of-fee
gates + small sizing. Honest difference: the bots trade price patterns, not businesses —
momentum trading, labeled as such, not Buffett-style investing. Permanently out of scope
(unbacktestable macro plays): raw gold as store-of-value, forex speculation, commodity
supply-deficit bets, IPO flips.

**Plan queue as of 2026-07-11 (all items now resolved — see CLAUDE.md for current state):**
1. Rule-based pipeline first live session (2026-07-13) — DONE, clean session.
2. GLD added to whitelist (2026-07-13) — DONE, after item 1 verified clean.
3. Keep filling gates — ongoing, see CLAUDE.md current status.
4. Ops items (Ollama key revoke, VPS logrotate, uptime monitor) — Ollama parked by user,
   uptime monitor done as healthchecks.io, VPS logrotate still open.
5. IBKR paper executor — DONE 2026-07-17.

### Metals & currency screen (2026-07-11) — GLD passes, currencies ruled out
User asked whether metals/currencies could join the system. Answer: they go through the same
gates as everything else — so we ran them. Zero new code: `STOCK_BT_SYMBOLS=GLD,SLV,FXE,UUP
stock_backtest.py` (daily engine, same gate as the stock rebuild) + `EXCHANGE=binance
SYMBOL=PAXG/USDT walkforward.py` (crypto 4h engine). Report: `logs/stock_backtest_20260711.md`.

| Symbol | What | Result | Verdict |
|--------|------|--------|---------|
| GLD | Gold ETF (daily) | PF 2.18/4.66/6.07/3.60 across 4 windows, 12 full-window trades, SL ≤ 42% | **PASS** |
| SLV | Silver ETF | PF ok (1.35–2.68) but only 8 full-window trades (< 10) + SL 62–67% | FAIL |
| FXE | Euro ETF | 5 trades in ~6y, 0 in last 250d — FX volatility too low to trigger entries | FAIL |
| UUP | USD ETF | 750d PF 0.99, 500d PF 0.84 | FAIL |
| PAXG/USDT | Tokenized gold (4h crypto engine) | Train PF 1.82 (9 trades) / validation PF 5.13 (12 trades) — holds OOS | Promising, parked |

**Decisions:**
- **Currencies: closed.** The strategy structurally can't trade FX — daily moves are too small
  to trigger Mode A/B entries (FXE: zero trades in the last year). Do not revisit with this
  strategy.
- **SLV: FAIL** — standard re-screen triggers apply.
- **GLD: legitimate PASS.** Added to WATCHLIST + RULE_WHITELIST 2026-07-13. Paper book only.
- **PAXG/USDT: parked as conditional candidate.** Small samples and all USD-pair preconditions
  still apply.

### Dual-strategy formalization (2026-07-06) — superseded, swing book retired 2026-07-22
Stock bot had two formally separated, named strategy books at this point. 168 tests pass.

**Strategy architecture (at the time):**
| Book | Label | Candle | Max hold | Capital | Stats |
|------|-------|--------|----------|---------|-------|
| Main paper executor | **Position book** | `1d` daily | Days–weeks (no forced exit) | Real $ tracked | $ expectancy, net PF |
| FastValidator | **Swing book** | `1h` hourly | 48h forced exit | Unit-sized (1.0 share) | % stats only |

Bidirectional symbol conflict guard existed between the two books (a held symbol in one book
blocked the other from opening it). This whole architecture is now historical — the swing
book was retired 2026-07-22 (see that entry below) after it proved to have no edge at 1h.

### Broker platform research (2026-07-06)
**Decision: Interactive Brokers (IBKR) is the target live broker for the stock bot.**
Alpaca ruled out — no Canadian TSX support (SHOP.TO, RY.TO etc.), which would require splitting
into two brokers and violates modularity.
- IBKR facts at research time: Python `ib_insync` library (later replaced by its maintained
  successor `ib_async`, see IBKR paper executor entry); paper trading built-in (TWS paper port
  7497, live port 7496); supports US stocks (SMART routing) + TSX stocks; commission US $0/trade,
  TSX CAD $1/trade minimum; $10/month small-account fee (waived at >$125k monthly volume);
  account minimum $1 live, PDT rule ($25k) applies for >3 day-trades in 5 days.
- Integration point: `StockExecutorBase` in `stock_bot/execution/base.py` — later implemented
  by `IBKRExecutor(StockExecutorBase)` in `stock_bot/execution/ibkr.py`, 2026-07-17.

### Swing book hardening (2026-07-06 continued) — superseded, swing book retired 2026-07-22
Features built at the time: swing book cash tracking (`FAST_STARTING_CASH`, real cash in
`FastValidatorState`, sized real share counts via `int(cash × risk_pct / entry_price)`),
a separate Phase A gate for the swing book (30-trade / PF ≥ 1.2 / WR ≥ 30%, independent of
the position book), and an earnings blackout (`FAST_EARNINGS_BLACKOUT_DAYS=7`) sharing the
same yfinance earnings fetch as the position book. All of this became moot when the swing
book was retired 2026-07-22 — kept here only for historical reference.

### Multi-coin readiness (2026-07-03)
The live loop became safe to run with >1 symbol in UNIVERSE_WHITELIST at this point.
Single-symbol behavior is numerically identical; strategy files untouched.
- Aggregate account breakers: `risk.evaluate(..., account_value=..., symbol=...)` — daily-loss
  and max-drawdown measure the whole account, not whichever slot happens to evaluate that tick.
  Position-size check stays per-slot. Backtests use the old positional signature, unchanged.
- Per-symbol daily trade cap, monitoring covers every symbol, universe refresh guard added.
- Adding a second coin still requires: walk-forward pass on current strategy code, capital
  ≥ $250, and the Capital Sizing Rules (see CLAUDE.md). The code was ready; the edge and
  capital are the gates — see "Multi-coin prep work" below for the CapitalPool gotcha found
  on a later audit.

### Multi-coin prep work (2026-07-21) — CapitalPool config checklist — full detail
User asked to start preparing for multiple crypto coins ahead of the gate opening. Two findings:

**1. SL-distance position sizing for SOL/SYN was already built** — the roadmap note claiming
it still needed building was stale. `AppConfig.calc_trade_qty_atr_risk(cash, price,
atr_value, atr_mult, baseline_sl_pct)` takes no symbol-specific inputs — built generically
2026-07-17 for the BTC ATR-sizing adoption, and `bot/main.py`'s call site already runs inside
the per-symbol loop using that symbol's own cash and own ATR series. It would work
identically for SOL/CAD or SYN/USD the moment either is added to `UNIVERSE_WHITELIST` — no
new sizing code needed.

**2. Real remaining gap: `CapitalPool` is a single shared slot, not per-symbol capital.**
`bot/portfolio/capital_pool.py`: `slot_cash = total_capital / max_concurrent` (capped at
`slot_cap`). At the time: `STARTING_CASH=100.0`, `MAX_CONCURRENT_POSITIONS=1`,
`MAX_SLOT_CASH_CAD=77` → `slot_cash = min(100/1, 77) = 77`, matching BTC's actual slot. This
is a **shared pool split N ways**, not N independent pools. If a second symbol were added by
only bumping `MAX_CONCURRENT_POSITIONS` to 2 without also raising `STARTING_CASH`,
`slot_cash = min(100/2, 77) = 50` — BTC's slot would silently shrink from $77 to $50, a ~35%
cut with no deliberate "reduce BTC capital" decision behind it.
- **The correct sequence when a second symbol's gate opens:** raise `STARTING_CASH` by the
  new symbol's own capital AND `MAX_CONCURRENT_POSITIONS` together, in the same change —
  never one without the other. This is now captured as a standing rule under Capital Sizing
  Rules in CLAUDE.md. No `.env` change was made in this session.

### Swing book retired (2026-07-22) — `FAST_ENABLED=false` — full detail
The swing book (`stock_bot/fast_validator.py`, 1h candles, 48h max hold, separate $1,000
virtual pool) was live-losing money (9 completed trades, PF 0.53 gross / 0.24 net, -$12.43
realized) and the question was whether migrating it from raw AI-confidence entries to the
position book's backtested rule strategy (Mode A/B) would fix it.

**Tested, not guessed:** ran the real Mode A/B strategy + real engine against 1h candles for
the swing book's own traded symbols (HOOD, MRNA, NCLH, AC.TO, RY, AMZN, BNS), first with the
wrong risk params (position book's 5%/15% SL/TP — gave a false-positive PASS signal), then
corrected to the swing book's REAL 1.5%/3.0% SL/TP. Corrected result: **combined PF 0.76
across 394 backtested trades, 64.0% SL-exit rate, only 1/7 symbols (BNS, thin sample) pass
the standard gate.** That SL-exit rate is within a point of the exact number (63%) that got
crypto's 1h day-trading experiment ruled out on 2026-07-10 — same failure shape: a 1.5% stop
is too tight for hourly noise regardless of what triggers the entry. Rule-based signals
would not have fixed this; the live losing streak (concentrated in HOOD 0-2 and MRNA 0-2)
was a symptom of the timeframe/stop-distance mismatch, not the AI-trigger architecture.
**CORRECTION (2026-07-28, see entry below):** this "stop-distance mismatch, not the
AI-trigger architecture" conclusion was never actually tested against the timeframe/stop
mismatch it names — no ATR-stop experiment had been run yet. Widening the stop to ATR×2.0
(2026-07-28) made combined PF *worse* (0.54, not better), which points toward the entry
signal (Mode A/B on 1h) being the more likely root cause after all. Treat the original
"not the AI-trigger architecture" framing as unconfirmed, possibly wrong — not settled.

**Bug found while closing out:** the swing-book worker thread had silently hung mid-cycle at
13:24 EDT that day — successfully sold RY (MAX_HOLD, correctly written to `fast_trades.csv`)
but never reached `self.state.save()` afterward (likely blocked on an AI call that didn't
time out cleanly — this turned out to be the same root cause fixed 2026-07-23, see below).
Net effect: BNS went unmonitored for 5+ hours, `fast_validator_state.json` was stale. Not
fixed in code since the book was being retired anyway; a scratch script reconciled the stale
RY entry and closed BNS for real through the normal `_write_trade()` path. Final swing-book
state: flat, 10 completed round-trips, realized P&L -$12.71.
- `stock_bot/fast_trades.csv` and `stock_bot/fast_validator_state.json` left in place as the
  frozen historical record.
- **Do not re-enable without a fresh walk-forward pass.** ~~If revisited, an ATR-based stop
  (same fix that worked for BTC 2026-07-17) is the most likely path to a passing result —
  untested, not yet researched.~~ **Tested 2026-07-28 — FAILED, see entry below.** Do not
  re-attempt a stop-mechanism fix here without first testing entry-signal edge independent
  of exit rules (standing note, `.memory/decisions/known-gaps.md` gap #12).
- No code changed — `FAST_ENABLED=false` in `stock_bot/.env` is the entire mechanism.

### Heartbeat blind spot fixed — loop-liveness tracking, both bots (2026-07-22) — 313 tests pass
Direct follow-up to the swing-book hang discovered while closing it out above. That thread
froze silently for 5+ hours while `heartbeat-stock` kept pinging healthchecks.io the whole
time — its `healthy_fn` only checked process-alive. Neither bot's heartbeat verified the
actual work loop was still making progress.
- **New shared module `bot/alerts/liveness.py` — `LivenessTracker`:** thread-safe "when did
  the monitored loop last make progress" clock. `touch()` marks progress; `is_alive(max_stale_s)`
  answers whether a touch happened recently enough. Injectable `time_fn` keeps it hermetic.
- **Crypto:** `_liveness.touch()` once per completed outer-loop tick (~30s cadence).
  `heartbeat-crypto`'s `healthy_fn` now requires a touch within the last 10 minutes.
- **Stock:** `_liveness.touch()` after every symbol's AI-call attempt (the code path most
  likely to freeze the same way the swing book did) plus a fallback touch once per full scan
  cycle. `heartbeat-stock`'s `healthy_fn` requires a touch within the last 30 minutes
  (generous — individual AI call latencies as high as ~800s were observed live the same day);
  `heartbeat-tws` untouched, still purely about the IBKR socket.
- Tests: `test_liveness.py` (7, hermetic — injectable clock, includes a test that explicitly
  simulates the 2026-07-22 incident shape). Suite 306 → 313.

### Swing-book hang root cause found + fixed — nvidia_nim client missing timeout= (2026-07-23) — 314 tests pass
Root-caused the 2026-07-22 hang properly instead of leaving it as an unexplained workaround.
`stock_bot/ai/ai_engine.py` defines `_TIMEOUT_S = 20`, intended to bound every AI provider's
calls at 20 seconds — correctly wired for the `openrouter`/`ollama_local` path, but **the
`nvidia_nim` branch — the one actually active — never passed `timeout=` to the OpenAI client
at all.** SDK fallback when omitted: `Timeout(connect=5.0, read=600, write=600, pool=600)` —
a 600-second read timeout, 30x longer than intended, plus up to 2 SDK-level retries on top.
This is exactly why AI verdicts were being logged as "successful" at latencies of 219s and
797s the same day (2026-07-22) — those weren't network flukes, that's what the *real*
(600s+) timeout looks like in practice. Fully explains the swing-book thread hang.
- **Not swing-book-specific** — the position book's own AI-advisory calls in `bot/main.py`
  use the identical `analyze()` method and the same broken path. The swing book happened to
  hit it first (and is now retired), but the live position book was equally exposed before
  this fix.
- **Fix:** one line — `timeout=_TIMEOUT_S` added to the `nvidia_nim` client constructor.
  Retry count left at the SDK default (2) — `timeout=20` alone bounds the worst case to
  ~60s (3 attempts × 20s), and AI is advisory-only, so a little retry resilience is fine.
- The 2026-07-22 liveness fix would have caught this specific hang shape going forward even
  without this root-cause fix — but that's a symptom-detector, not a cure. Both are now in
  place.
- Tests: `test_ai_engine_timeout.py` (1, hermetic — fake OpenAI client class records its
  constructor kwargs). Suite 313 → 314.

### NVIDIA_MODEL research + switch (2026-07-23) — .env only, no code changes
Same-day follow-up: the timeout fix made every nvidia_nim call fail fast (~41-42s to exhaust
3 retries) rather than hang for hours, but `openai/gpt-oss-120b` was still failing 100% of
the time — confirmed via direct API test that this was model-specific, not an
account/auth/quota/outage issue.
- Researched the account's full model catalog (119 models). Most weren't actually usable:
  `writer/palmyra-fin-70b-32k` (finance-specialized — would have been the obvious first
  choice), `nvidia/llama-3.1-nemotron-70b-instruct`, `gemma-3-12b-it`, `phi-3.5-moe-instruct`,
  `granite-3.0-8b-instruct`, `mistral-nemo-12b-instruct`, `mistral-7b-instruct-v0.3` all
  returned 404 "not found for account."
- The timeout failure wasn't a "big model" problem specifically — several 70B+ models timed
  out account-wide, but so did `meta/llama-3.2-3b-instruct`, a 3B model. Per-model infra
  load/availability, not size.
- `nvidia/nvidia-nemotron-nano-9b-v2` is a reasoning model (output goes to a
  `reasoning`/`reasoning_content` field, `content: null`) — incompatible with the bot's
  direct-JSON-response parsing without code changes. Not pursued.
- Quality-tested the models that actually worked (bullish + bearish test scenarios, twice
  each): `meta/llama-3.1-8b-instruct` missed the bearish case (returned HOLD both times);
  `google/gemma-2-2b-it` was fastest (1.2-1.4s) and correctly said SELL both times but with
  suspicious 80→95% confidence for a 2B model; `mistralai/mixtral-8x7b-instruct-v0.1` was
  correct and consistent but 15-19s — too slow for the sequential per-symbol scan loop;
  `mistralai/mistral-small-4-119b-2603` (119B params despite the "small" name) read both
  scenarios reasonably with moderate, not overconfident, confidence, at 1.7-3.1s.
- **Adopted: `NVIDIA_MODEL=mistralai/mistral-small-4-119b-2603`** (user's choice from the
  three real candidates). Verified end-to-end through the actual `AIEngine` class — 5.2s,
  valid verdict, no timeout. Full suite still 314/314 (config-only change).

### Earnings-fetch failure cache fixed — was silently disabling the earnings blackout for a full day (2026-07-23) — 317 tests pass
Investigated a batch of yfinance warnings user pasted rather than dismissing them as routine
noise — found one is genuinely cosmetic, the other was a real bug with a safety-feature
consequence.
- **"No earnings dates found, symbol may be delisted" is yfinance's own internal message,
  not ours, and not a real problem.** yfinance exposes earnings data two ways: `.calendar`
  (has the actual date) and `.earnings_dates` (a less reliable Yahoo endpoint). When
  `.earnings_dates` is empty, yfinance prints that alarming line even when `.calendar` has
  perfectly good data. Confirmed directly for NVDA and RY — both returned correct dates via
  `.calendar` moments later.
- **"Fetch failed X:earnings: ['Earnings Date']" is real** (our own `yf_client.py` logging an
  actual exception) **but transient** — the same retest proved non-persistent.
- **The actual bug:** `stock_bot/research/earnings.py` cached a fetch *failure* for the same
  24-hour TTL as a real success. The earnings blackout feature
  (`_is_earnings_blackout()` in `stock_bot/main.py`) depends entirely on
  `next_earnings_date` being populated — so one transient yfinance hiccup was silently
  disabling that protection for the affected symbol for the rest of the day.
- **Fix:** `_earnings_cache` now stores a success flag alongside each entry; failures use a
  new `_EARNINGS_FAILURE_TTL = 3600` (1 hour) instead of the full `_EARNINGS_TTL = 86400`
  (24 hours) that successes still get.
- Tests: `test_earnings_cache.py` (3, hermetic). Suite 314 → 317.

**Follow-up same day — added the missing lock (2026-07-23, 318 tests pass):** user asked to
dig further after the TTL fix landed. Found `stock_bot/research/earnings.py` is the one of
three yfinance call sites in the repo with **no lock** — `stock_bot/data/price_feed.py`
(`_yf_download_lock`) and `stock_bot/fast_validator.py` (`_yf_lock`) both already serialize
their calls for this exact class of problem, but earnings fetches ran through `main.py`'s
research phase with `ThreadPoolExecutor(max_workers=5)` — up to 5 concurrent, totally
unserialized yfinance calls. Tried to force a deterministic reproduction first (both isolated
and 5-way concurrent) — came back 100% clean, consistent with this being a low, probabilistic
per-call failure rate rather than a guaranteed trigger.
- **Fix:** added `_yf_lock = threading.Lock()` to `earnings.py`, same pattern as the two
  sibling modules. Serializes the research phase's 5 concurrent earnings fetches into
  sequential ones; cheap since results are cached 24h (1h on failure).
- Honest framing: could not be proven to be *the* root cause with certainty — closes a real,
  structural inconsistency present in 2 of 3 modules, absent in the third.
- Tests: `test_earnings_cache.py` gained `test_concurrent_fetches_are_serialized_by_the_lock`.
  Suite 317 → 318.

**Second follow-up same day — the real fix (2026-07-23, 323 tests pass):** user reported the
earnings failures were still happening minutes after the lock-fix restart. Investigated the
retry logic itself and found the real gap: `fetch_with_retry()` (`stock_bot/data/yf_client.py`,
the shared retry helper used by every yfinance call site in the repo) only retries on
`YFRateLimitError` — any other exception gets zero retries, immediate give-up. Since these
earnings failures throw something else, they never got retried at all, despite manual
retesting proving them transient.
- **Fix:** generic (non-rate-limit) exceptions now get retried too, up to the same
  `max_attempts` (default 3), with a new short fixed `_GENERIC_RETRY_DELAY_S = 2` — much
  shorter than the rate-limit ladder (5/15/30s escalating), since the server isn't actually
  throttling here, just occasionally glitching. Rate-limit handling itself unchanged.
- This is the shared retry point for the WHOLE stock bot's yfinance usage, so this improves
  resilience broadly, not just for earnings.
- **Second, separate bug found and fixed in the same investigation:** `nvidia_nim FULL ERROR
  for HOOD: TypeError: 'NoneType' object is not subscriptable` — `stock_bot/ai/ai_engine.py`
  subscripted `completion.choices[0]` without checking whether `choices` came back `None`.
  Already safely degraded to a HOLD verdict via the outer exception handler either way, but
  the error was opaque. Added an explicit `if not completion.choices:` guard, same safe HOLD
  fallback — diagnosable cause now.
- Tests: `test_yf_client_retry.py` (4, hermetic). `test_ai_engine_timeout.py` gained
  `test_nvidia_nim_empty_choices_falls_back_to_hold_without_crashing`. Suite 318 → 323.

**Third follow-up same day — the retry fix's own side effect (2026-07-23, 324 tests pass):**
user restarted and immediately saw a NEW message pattern: "Research fetch timed out for HOOD
(earnings)" appearing alongside the expected retry lines, for the same underlying failures.
Self-inflicted regression from the retry fix above: `stock_bot/research/aggregator.py`'s
`fetch_research()` wraps the earnings fetch in its own SEPARATE `future.result(timeout=15)`
— completely independent of `fetch_with_retry`'s own retry logic. Before the retry fix, a
failing earnings fetch gave up almost instantly (well under 15s); after the retry fix, 3
attempts + 2×2s delays can legitimately take close to (or over) 15s, so the SAME underlying
failure started tripping this second, unrelated timeout too — one failure, two log lines,
no new information.
- **Fix:** per-source timeout instead of one blanket 15s for both. Earnings → 45s. News stays
  at 15s — confirmed `news_fetcher.py` doesn't use `fetch_with_retry` at all (feedparser, not
  yfinance), so its latency profile never changed.
- Tests: `test_research_aggregator_timeout.py` (1, hermetic). Suite 323 → 324.
- **Pattern worth remembering:** a retry/backoff fix at one layer can silently blow through
  an unrelated timeout at a layer above it that was sized assuming the old, faster-failing
  behavior. Worth checking for this same shape anywhere else multiple timeout/retry layers
  stack (none currently known, but not exhaustively audited).

### Large-cap screen (2026-07-23) — 4 new RULE_WHITELIST symbols, no code changes
Goal: widen the stock-bot symbol funnel further (user asked for "a good portfolio" of stock
symbols). Ran `stock_backtest.py` (same 4-window gate, strategy hash `659d1c03987b72fd`
unchanged) on 20 untested, liquid, well-known large-caps spanning tech/financials/consumer/
healthcare/industrials. Report: `logs/stock_backtest_20260723.md`.
- **PASS + whitelisted (4): CAT** (full PF 3.41, range 3.41–14.44 across all 4 windows,
  SL-exit ≤ 37% everywhere — the strongest pass on record for this whitelist),
  **GOOGL** (full PF 3.50, range 3.50–10.90, SL-exit ≤ 30%), **WMT** (full PF 1.69, range
  1.69–3.71, SL-exit ≤ 33%), **MSFT** (full PF 2.23, but 500d window SL-exit rate hit
  66.7% — passes the letter of the gate, weakest of the four).
- **FAIL (16):** AAPL (close — full PF 1.20/17 trades, but 750d 0.70 and 500d 0.72 both
  dip below 1.2), JPM, V, MA, COST, HD, JNJ (full PF 1.18, just under the bar), UNH, PG,
  MCD, ORCL, ADBE, CRM, AVGO (close — full PF 1.88 but 500d window 1.14 < 1.2), DE, IBM.
- User approved adding all 4 PASS symbols. `stock_bot/.env`: WATCHLIST and RULE_WHITELIST
  both gained `CAT,GOOGL,WMT,MSFT`.
- **Unrelated ops note surfaced during this session:** `logs/stock_bot.log` showed the live
  stock bot's IBKR/TWS connection dropped ~18:54 ET that day with `ConnectionRefusedError` on
  every reconnect attempt since (ops alert already fired at 19:05 for the 10+ minute
  disconnect). TWS likely needed to be relaunched/logged back into before the stock bot
  (including these 4 new symbols) could trade again.

### Root-caused why BTC/CAD is still 0/15 + Kraken call retry resilience added (2026-07-24) — 328 tests pass, strategy hash unchanged
User pushed back on "we've been at 0/15 for weeks, is this ever going to happen" — instead of
re-asserting patience, did a forensic pass over the actual log/trade history.

**Finding: the strategy fired BUY twice in the visible log window (since 2026-07-05), not
zero times.** Both failed to become a recorded trade — but for execution reasons, not entry-
rule reasons:
- **2026-07-06 20:00 UTC:** clean BUY signal (RSI 66, ADX 27.8, spread 0.87%). Kraken's
  order-book depth endpoint threw a network error, `_place_limit_order` fell back to a market
  order, and the code at the time (pre-dating the 2026-07-15 fix below) had no polling/retry
  logic — it read `filled=0` once and gave up. No drift appeared for this order, so this
  specific order most likely never filled at Kraken at all — a lost opportunity, not a lost
  fill.
- **2026-07-15 12:00 UTC:** same pattern — except this one **did** fill (0.000169 BTC
  appeared minutes later as a drift alert) and the unpolled `filled=0` response caused the
  fill to go unrecorded. This is the exact incident `test_limit_chase_recovery.py` was
  already built to fix (shipped same day, 2026-07-15) — verified the fix is real by reading
  `bot/execution/live_executor.py`'s poll-and-recover logic and re-running that suite:
  `test_limit_chase_recovery.py` + `test_fill_recording.py` — 14/14 pass.
- **Conclusion:** 0/15 reflects a strategy that trades roughly every 1–3 weeks (consistent
  with backtest frequency) losing its first two real chances to execution fragility, not
  entry rules that never fire. One of the two failure modes was already fixed; this session
  addressed the shared root cause of both.

**Fix — retry resilience on transient Kraken calls, added same session:** new
`bot/exchanges/retry.py` (`fetch_with_retry`) — retries a zero-arg callable on any exception
with a short fixed delay, raises the last exception if every attempt fails. Wired into the
three call sites implicated above:
- `bot/execution/live_executor.py` `_place_limit_order`'s order-book depth fetch (2 attempts,
  1.5s delay).
- `bot/main.py` `_fetch_completed_candle`'s `fetch_ohlcv` call (3 attempts, 2.0s delay).
- `bot/main.py`'s per-tick `fetch_ticker` live-price call (3 attempts, 2.0s delay).
- Tests: `test_kraken_retry.py` (4, hermetic). Full suite re-run: 328/328 pass, no existing
  test broke.
- Not a strategy change — `bot/strategy/*.py` untouched, hash `659d1c03987b72fd` still valid.

### Swing book ATR-stop research (2026-07-28) — FAILED, corrects the 2026-07-22 diagnosis
Pre-registered experiment (`swing_atr_walkforward.py`, research-only, `stock_bot/.env` and
`stock_bot/fast_validator.py` untouched throughout) testing whether the ATR×2.0 stop-loss
that fixed BTC's 1h problem (2026-07-17) would also fix the swing book's 1h problem
(retired 2026-07-22 at combined PF 0.76, 64.0% SL-exit rate, diagnosed then as "1.5% fixed
stop too tight for hourly noise, not an entry-signal problem").

**Pre-registered, before running anything:** hypothesis (ATR×2.0 stop, TP unchanged at
3.0%, raises combined PF above 1.0 on the same 7 symbols without materially raising the
SL-exit rate) and 4 pass criteria (in-sample combined PF ≥ 1.2, ≥4/7 symbols PF ≥ 1.0,
SL-exit rate < 50%, holds on a genuinely separate out-of-sample window — same 7 symbols:
HOOD, MRNA, NCLH, AC.TO, RY, AMZN, BNS). Checked real yfinance 1h data availability first
(consistent ~2023-09-01 → 2026-07-28 across all 7, yfinance's ~730-day 1h cap) before fixing
the split — IN-SAMPLE 2023-09-01→2026-01-28, OUT-OF-SAMPLE 2026-01-28→2026-07-28 — decided
from data availability alone, before seeing any PF numbers.

**Result: FAILED all 4 criteria, run once at ATR×2.0, no grid search.**
| Window | Combined PF | Symbols PF≥1.0 | SL-exit rate | Trades |
|---|---|---|---|---|
| In-sample | 0.54 | 0/7 | 53.0% | 287 |
| Out-of-sample | 0.35 | 1/7 (BNS) | 56.2% | 73 |

Combined PF got **worse** than the 0.76 fixed-SL baseline, not better, and the SL-exit rate
barely moved (53% vs. 64%, still fails the <50% bar) while PF collapsed anyway — losing
trades that still hit SL got bigger, not fewer trades hit SL. Zero of 7 symbols clear
PF≥1.0 in-sample; BNS is the only symbol ever positive, only in a 7-trade OOS slice.

**This does not replicate the BTC mechanism.** On BTC a tight stop was whipsawing a real
entry edge; widening it let winners run. Here widening the stop mostly let losers lose more
before exiting, with no offsetting win-rate improvement (RY 21.4% win rate in-sample, AMZN
37.5%) — consistent with the entry signal itself (Mode A/B on 1h candles), not stop
distance, being the more likely root cause. **This corrects the 2026-07-22 entry's "not the
AI-trigger architecture" conclusion** — that was asserted, not tested, at the time.

**Standing note (also `.memory/decisions/known-gaps.md` gap #12): do not re-attempt a
stop-mechanism fix for the swing book without first testing entry-signal edge independent
of exit rules.** A fixed-SL/fixed-TP sanity check (or even a same-candle-close exit) that
isolates whether Mode A/B's raw BUY/SELL timing has any edge on 1h stock candles, before
touching the stop mechanism again, would tell us which side of this is actually broken.

- Verified the ATR value feeding the stop uses already-validated candle data (same
  `fetch_candles()` sanity gauntlet as live), and reuses the unmodified crypto
  `IndicatorStrategy` (Mode A/B) + `stock_bot/backtest/engine.py`'s cost model
  (`BacktestTrade`, `BacktestResult`, IBKR commission function) — same engine as the
  original 0.76 PF finding, only the SL calculation swapped.
- `swing_atr_walkforward.py` left at repo root as the reusable script for any future
  ATR-multiplier or entry-isolation follow-up experiment.
- Suite unaffected (332/332, no source files touched — this is a standalone script).

### Bug fixes applied 2026-06-20
All critical bugs resolved (this is the earliest recorded bulk cleanup):

**Crypto bot (bot/):**
- `bot/risk/risk_manager.py`: daily-loss, max-drawdown, and trade-cap checks block BUY only — SELL always allowed. Manual HALT blocks BUY and strategy SELL, but SL/TP exits bypass the risk gate entirely (unless `RISK_HALT_BLOCKS_STOPS=true`), so stops always fire during a halt.
- `bot/backtest/engine.py`: Added `forced_exit` flag — SL/TP triggers bypass cooldown state machine (stop-losses were being suppressed)
- `walkforward.py` + `montecarlo.py`: ADX threshold corrected 15.0 → 18.0 (was testing wrong strategy vs live)
- `config.py`: Defaults corrected — fee 0.001→0.008, SL 0.02→0.015, TP 0.04→0.10 (both dataclass and _load())
- `bot/backtest/report.py`: Added Buy-and-Hold benchmark section with alpha comparison

**Stock bot (stock_bot/):**
- `stock_bot/data/screener.py`: Removed $200 price cap (was blocking NVDA, AAPL, MSFT); RSI_OVERBOUGHT 70→75; price filter long-only (abs→positive)
- `stock_bot/main.py`: Added 5% sanity check on live_price vs candle_close — TSX fast_info currency mismatch caused impossible P&L like +921%
- `stock_bot/research/sentiment_scraper.py`: Replaced 12-word flat keyword list with phrase-pattern rules + negation detection window (3 tokens)

### USD Expansion screen (2026-07-03) — full results table
603 Kraken USD spot pairs → 178 cleared $50,000/day liquidity gate → top 15 by volume walk-forwarded.
Strategy hash `659d1c03987b72fd` at time of screen.

| Symbol | Vol (USD/day) | 5000c PF | 3000c PF | 1000c PF | SL rate | Verdict |
|--------|-------------|--------|--------|--------|---------|---------|
| HYPE/USD | $13,411,669 | — | — | — | N/A | SKIP (no Binance proxy) |
| ZEC/USD | $6,964,638 | 0.99 | 1.43 | 2.65 | 87% | FAIL — full PF < 1.0 + SL 87% |
| ADA/USD | $6,669,035 | 0.60 | 0.53 | 0.00 | 90% | FAIL — PF + SL |
| SUI/USD | $5,784,470 | 1.32 | 0.98 | 2.50 | 82% | FAIL — 3000c PF 0.98 + SL 82% |
| TAO/USD | $4,137,799 | 0.93 | 1.38 | 1.64 | 86% | FAIL — full PF + SL |
| M/USD | $3,942,638 | — | — | — | N/A | SKIP (no Binance proxy) |
| SYN/USD | $3,546,542 | 1.80 | 2.56 | 2.39 | 79% | FAIL — SL 79% > 70% cap |
| XLM/USD | $2,970,677 | 0.96 | 1.14 | 0.66 | 87% | FAIL — full PF + SL |
| UNI/USD | $2,879,284 | 0.91 | 0.95 | 0.59 | 88% | FAIL — PF + SL |
| NEAR/USD | $2,573,455 | 1.05 | 1.30 | 1.18 | 86% | FAIL — full PF + SL |
| LINK/USD | $2,261,609 | 1.54 | 2.19 | 1.28 | 79% | FAIL — SL 79% > 70% cap |
| LTC/USD | $2,194,392 | 0.90 | 0.92 | 0.00 | 88% | FAIL — PF + SL |
| AAVE/USD | $1,962,514 | 0.89 | 0.79 | 1.32 | 88% | FAIL — PF + SL |
| XMR/USD | $1,891,217 | — | — | — | N/A | SKIP (Binance delisted) |
| BASED/USD | $1,879,930 | — | — | — | N/A | SKIP (no Binance proxy) |

**Dominant failure mode:** SL-exit rate 79–90% on every alt tested. The Mode A/B pullback entry
(RSI 38–58) has no edge on these assets — same pathology as XRP/CAD (87% SL rate).

### ATR stop-loss experiment (2026-07-04) — near-miss follow-up
`atr_sl_experiment.py` tested ATR-scaled stops (1.5–3.0 × ATR14) vs the fixed 1.5% SL on
SYN, LINK, XRP, BTC. Report: `logs/atr_sl_experiment_20260704.md`.
- SL-exit rates drop 76–87% → 9–43% everywhere; SYN and LINK clear the full screen gate
  in-sample at ATR×2.0–2.5 (PF ≥ 1.2 all windows). XRP still fails (entries have no edge).
- OOS shows PF parity, not improvement at this stage — ATR SL looked like a variance/fee
  improvement, not alpha, until the later non-overlapping OOS runs (2026-07-17, see above)
  confirmed it holds for both SOL and BTC.
- BTC/CAD stayed on validated fixed SL at this point (later changed 2026-07-17 — see ATR SL
  adopted live entry above). SYN/LINK remain conditional candidates: all USD preconditions
  above + fresh per-symbol walk-forward at the chosen mult + SL-distance-based position
  sizing (built generically 2026-07-17, confirmed symbol-generic 2026-07-21).

### Stock bot: RULE_WHITELIST gate removed from BUY logic (2026-08-23)

**Change:** `stock_bot/main.py`'s `_rule_buy` no longer requires
`symbol.upper() in _rule_whitelist`. It now fires on `rule_v.signal == "BUY" and
rule_v.warmed_up` alone — a rule-based BUY now applies to ANY symbol in that cycle's scan
universe, not just the symbols that previously passed a `stock_backtest.py` walk-forward and
got added to `RULE_WHITELIST`.

**Reason:** explicit user request — full-universe trading without per-symbol backtest
validation as a precondition for entry. This is a deliberate policy reversal of the
whitelist-gating discipline documented throughout the rest of this file and CLAUDE.md (e.g.
the XRP/CAD incident above, where a stale-validation symbol traded live for weeks before
being caught) — the walk-forward-before-whitelisting discipline is NOT being applied to the
stock bot's rule-based entries going forward. This does not change crypto (`bot/`) at all;
`UNIVERSE_WHITELIST=BTC/CAD` and its walk-forward-gated re-entry rules are untouched.

**What was removed / left alone:**
- `stock_bot/main.py`: the local `_rule_whitelist` set (built from
  `cfg.rule_whitelist_str`), the `_rule_buy` whitelist check, the "(not in RULE_WHITELIST —
  no entry)" console note, and the `rule_whitelisted` field passed into `ScanResult` were all
  removed — nothing else in `main.py` referenced them.
- `stock_bot/dashboard/renderer.py`: `ScanResult.rule_whitelisted` field removed;
  `_rule_summary_html()`'s "📐 Rule Signals" strip and the per-card "📐 Rules:" tag both
  dropped the "(not whitelisted — no entry)" branch — a rule BUY now always shows "→ buying"
  (still subject to the existing `buy_alloc` SIZE_SKIP check in the summary strip, which is
  a sizing constraint, not a whitelist one, and was left unchanged).
- `stock_bot/config.py`'s `rule_whitelist_str` loading (`RULE_WHITELIST` env var) is
  UNCHANGED and still required — `LiveTradingGate.check_gate1()`
  (`stock_bot/analysis/accuracy_tracker.py`) still reads it directly via
  `_load_stock_config().rule_whitelist_str` to validate that every whitelisted symbol passed
  the latest `stock_backtest.py` walk-forward, as part of the code-enforced IBKR
  live-trading readiness gate in `IBKRExecutor.__init__()`. That gate is unrelated to paper
  BUY entry and was not touched.
- No file under `bot/strategy/` or `build_indicator_config()` was touched — the strategy
  fingerprint (`b30f2f9e769c8d41`) is unaffected, no walk-forward re-run needed.

**What now gates a new BUY, post-change:**
1. The rule signal itself (`rule_signal()` in `stock_bot/strategy/rules.py`, unchanged Mode
   A/B logic) must say BUY and be warmed up.
2. The five paper/IBKR risk-gate tiers in `stock_bot/execution/` (`PAPER_DAILY_LOSS_PCT`,
   `PAPER_WEEKLY_LOSS_PCT`, `PAPER_DRAWDOWN_HALT_PCT`, `PAPER_KILL_SWITCH_PCT`, plus the
   sector-concentration/correlation/macro-blackout/VIX-crisis gates) — all untouched by this
   change, still the real safety net on any new entry. (Note: `RISK_MAX_POSITION_PCT` /
   `RISK_DAILY_LOSS_LIMIT` / `RISK_MAX_DRAWDOWN` / `RISK_MAX_TRADES_PER_DAY` /
   `COOLDOWN_TICKS` are crypto-bot (`bot/risk/risk_manager.py`, root `config.py`) env-var
   names, not stock-bot ones — confirmed via `grep`, they don't exist in `stock_bot/`. Those
   crypto files were not touched by this change either way.)
3. **The scan universe itself** — with per-symbol backtest validation gone, this is the only
   remaining filter on which symbols ever reach `rule_signal()` at all. Built in
   `stock_bot/main.py`'s `run()`: `cycle_symbols = watchlist_symbols + top_movers (deduped) +
   held_symbols`. `watchlist_symbols` = `cfg.watchlist` (user-configured, `.env`
   `WATCHLIST`/`get_watchlist()`). `top_movers` = `StockUniverse.get_universe()` (S&P500 +
   TSX60 constituent list) filtered by `.pre_filter(raw_symbols, cfg.universe_size,
   market_status=...)`, capped to `UNIVERSE_SIZE` (default 20) symbols/day, refreshed once
   daily — only active when `UNIVERSE_ENABLED=true` (default false). Within a cycle, any
   symbol not already in `watchlist_set` additionally has to pass
   `StockScreener.screen(symbol, candles)` in `_fetch_symbol_data()` (min price, RSI extreme,
   recent MACD cross, or a large single-candle price move — a liquidity/momentum filter, not
   a strategy-edge validation) before candles are computed at all. TSX (`.TO`) symbols remain
   permanently advisory-only regardless of any of this — CIRO DMR 3200 blocks API orders on
   Canadian exchanges, unrelated to and unaffected by this change.
- Full test suite: `.venv/bin/python -m pytest --tb=short -q` → **605 passed** (unchanged
  count — no test referenced `rule_whitelisted`/`_rule_whitelist`, confirmed by grep before
  the change).

### Stock bot: risk-control hardening after the whitelist removal (2026-08-23, same day)

Follow-up pass, explicitly requested to compensate for the RULE_WHITELIST removal above.
`bot/strategy/*` and `build_indicator_config()` untouched throughout — strategy hash
`b30f2f9e769c8d41` unchanged (`bot.strategy.fingerprint.compute_strategy_hash()` re-run to
confirm). Full writeup: `.memory/decisions/stock-whitelist-gate-removed-2026-08-23.md`
("Follow-up hardening pass" section).

**1. In-distribution ATR%/liquidity filter (implemented).** `stock_bot/data/screener.py`'s
`StockScreener.screen()` now returns `(passed, reason)` instead of a bare bool, and gained a
new `_check_in_distribution()` check ahead of the existing RSI/MACD/price-move logic.
`logs/stock_backtest_20260710.md` (the report naming MRNA/AMD/RY.TO/PLTR as the 4 symbols
that PASSED) has no ATR/volume columns — it's a trade-stats table — so the actual range was
computed directly from live 300-day daily candles (matching live `LOOKBACK_DAYS`/`INTERVAL`)
on 2026-08-23:

| Symbol | ATR% | Avg $ volume/day |
|--------|------|-------------------|
| MRNA   | 10.26% | $1.54B |
| AMD    | 6.00%  | $20.86B |
| RY.TO  | 1.73%  | $0.99B |
| PLTR   | 4.28%  | $9.41B |

Observed range: ATR% [1.73%, 10.26%], avg $ volume [$0.99B, $20.86B]. Thresholds picked with
generous margin above/below that range (not tight to it): reject if ATR% > 3× the observed
max (`BACKTESTED_ATR_PCT_MAX × ATR_PCT_REJECT_MULT` = 10.26 × 3.0 ≈ 30.8%, well past even
MRNA — only catches meme-stock/pre-earnings-blowup-level chaos); reject if avg $ volume <
$50,000,000/day (`MIN_AVG_DOLLAR_VOLUME`, about 1/20th of RY.TO's ~$989M — the thinnest of
the 4 — only catches names genuinely illiquid relative to anything backtested). No ATR% floor
— low volatility isn't a risk this filter needs to catch. Verified none of the 4 backtested
symbols nor the current 16-symbol `RULE_WHITELIST` would themselves be rejected by their own
reference filter (`test_backtested_symbols_actually_pass_the_filter`, plus manual check of
the probe data against the whitelist). Rejection reason is a formatted string, e.g.
`"SCREEN_SKIP: WILD ATR 80.0% is 7.8x the backtested max (10.3%, MRNA) — outside the
validated volatility regime"`, logged at WARNING (previously the pre-existing "boring stock"
rejections only logged at INFO/DEBUG and were dropped silently) and now surfaced in a new
dashboard section ("🔬 Screened Out — Outside Backtested Regime") via a new `screen_skips`
param threaded through `stock_bot/main.py` → `DashboardRenderer.render()` →
`stock_bot/dashboard/renderer.py`'s `_screen_skips_html()`. **Scoping decision:** this filter
only applies to symbols not already in `watchlist_set` (same gating the pre-existing screener
already uses) — held positions and configured watchlist symbols are exempt, so a currently-
held position can never lose its ability to generate a rules-engine SELL because of this
filter (held symbols are added to `watchlist_set` before the scan loop runs). This does leave
one gap, named not silently: a watchlist symbol that was never backtest-PASSed (e.g. AC.TO,
SHOP.TO — TSX advisory-only anyway) still bypasses this new filter, same as it already bypassed
the pre-existing screener. Tests: `tests/stock/test_screener_in_distribution.py`, 5 new cases
(normal case passes, extreme ATR rejected with reason, illiquid symbol rejected with reason,
insufficient-candles passthrough, sanity check that the 4 backtested symbols clear their own
reference filter). Suite 607→612.

**2. Per-symbol volatility sizing — audited, reported, NOT implemented pending a decision.**
Current sizing in `stock_bot/main.py` (near the `executor.buy()` call): flat notional —
`alloc = (cash + position_value) * PAPER_RISK_PCT` (0.20 live), `shares = int(alloc /
price_cad)`. Separately, `stock_bot/config.py`'s `calc_shares_atr_risk()` — already wired
into the same BUY path, gated behind `PAPER_ATR_SIZING_ENABLED` (default false, currently
off) — turns out to already implement almost exactly what was asked for: algebraically,
`capped_shares = base_shares × (baseline_sl_pct ÷ (atr_mult × ATR%))`, i.e. size ∝ 1/ATR%
relative to a baseline (`baseline_sl_pct ÷ atr_mult` acting as the reference ATR%), and it's
`min()`-capped against the flat notional baseline so no symbol can size UP past today's
default — exactly the two properties requested. It does not cover liquidity, only volatility
— a real gap if liquidity-based downsizing (not just the new hard liquidity floor above) is
wanted too. Real example sizes computed against the live IBKR paper account
(~$5,087 total value, `peak_equity` in `stock_bot/ibkr_state.json`, `PAPER_RISK_PCT=0.20`,
`PAPER_STOP_LOSS_PCT=0.05`, `PAPER_ATR_SL_MULT=2.0` default) if this were turned on today:

| Symbol | Price | ATR% | Flat shares (today) | ATR-capped shares (if enabled) |
|--------|-------|------|----------------------|----------------------------------|
| MRNA | $145.13 | 10.26% | 7 | 1 |
| AMD  | $473.25 | 6.00%  | 2 | **0 — SIZE_SKIP** |
| GM   | $87.93  | 2.66%  | 11 | 10 |

AMD sizing to 0 at today's account size is a real, honest consequence of enabling this as-is
— worth knowing before flipping the flag, not just an abstract formula. Presented to the user
for a decision: (a) enable `PAPER_ATR_SIZING_ENABLED=true` as already built, (b) additionally
add a liquidity-scaling factor (not yet built), or (c) something else. **User chose: run the
walk-forward first** (a follow-up question, prompted by noticing enabling this also swaps the
SL trigger — not just position size — from flat 5% to ATR×2.0 via `calc_shares_atr_risk`'s
paired stop-distance override, and CLAUDE.md's own existing rule requires exactly that
validation before enabling live, which had never actually been run for this specific setting).

**Walk-forward built and run, same session.** `stock_bot/backtest/engine.py`'s `run_symbol()`
had no ATR-stop mode at all — only a flat `stop_loss_pct`. Added a new optional
`atr_sl_mult` field to `StockBacktestConfig` (default `None` = byte-for-byte identical to the
pre-existing behavior — every one of the 11 pre-existing engine tests still passes unchanged,
proving this); when set, each trade's SL distance is computed ONCE at entry fill as
`min(ATR(14)*atr_sl_mult/entry_price, atr_sl_cap)` — same "known at entry, never repriced"
semantics as the live executor's own `_atr_stop_pct`, with the same flat-stop fallback when
ATR is unavailable (thin history). Deliberately NOT added as a flag to `stock_backtest.py`
itself — that script's `logs/stock_backtest_latest.json` output is a fixed path
`LiveTradingGate.check_gate1()` depends on for the CURRENT (flat-stop) live behavior, and an
ATR-mode run must not corrupt that gate's read. Instead: a new standalone script,
`validate_atr_sizing.py` (repo root), reusing the same engine, same walk-forward windows
(`[0, 750, 500, 250]`), and identical PASS/FAIL gate criteria as `stock_backtest.py`, writing
its own separate report (`logs/stock_backtest_atr_validation_<date>.md`) and touching no JSON.
Tests: `tests/stock/test_stock_backtest_engine.py`, +3 (ATR stop overriding the flat distance
on a candle sequence engineered so the two modes diverge — proven by running the identical
candles through both configs and asserting different exit prices/timing; the same-candles
flat-mode control; fallback-to-flat when history is too short for a 14-period ATR). Suite
612→615. `bot/strategy/*` and `build_indicator_config()` untouched — only the SL/TP check
inside `stock_bot/backtest/engine.py` changed; strategy hash `b30f2f9e769c8d41` confirmed
unchanged via `bot.strategy.fingerprint.compute_strategy_hash()`.

**Run against RULE_WHITELIST (the trusted, already-PASSED set — not the full 28-symbol
watchlist, which includes symbols that already fail under flat stops and wouldn't answer the
actual question), ATR(14) × 2.0 (`PAPER_ATR_SL_MULT` default), 1500 days, same $1,000/trade
notional as `stock_backtest.py`:**

**Result: 14 PASS, 2 FAIL — AMD and KO.** AMD is a genuine regression: it PASSED under the
original flat-5%-stop backtest (`logs/stock_backtest_20260710.md`) but FAILS under ATR×2.0
(full-window PF 1.05 < 1.2 gate, 250d window PF 0.92 < 1.2). This is the same symptom the
Part 2 sizing example above already hinted at (AMD sizing to 0 shares at today's account
size) — AMD's price/ATR combination genuinely does not tolerate this stop distance well. KO
also fails (250d PF 0.56) but was never one of the original 4 backtest-PASS symbols (added
later via the 2026-07-15 affordable-symbol screen), so it's a new finding, not a regression
per se. The other three of the original 4 (MRNA, RY, PLTR) all still PASS cleanly. Full
per-window table: `logs/stock_backtest_atr_validation_20260823.md` (RY's result was appended
to that file manually after a transient yfinance fetch blip dropped it from the main batch
run — a standalone re-fetch immediately after succeeded cleanly, RY PASS).

**Decision: `PAPER_ATR_SIZING_ENABLED` NOT enabled.** The walk-forward did not produce a
clean PASS — it found a real regression on a symbol that was part of the original trusted
set. Enabling the flag as-is today would put AMD (and KO) live under a stop distance that
just failed its own validation, which is exactly the outcome the existing CLAUDE.md rule
("do not enable live without a walk-forward PASS first") exists to prevent. Left off,
reported to the user with the concrete finding rather than silently flipping it. Options for
the user to consider next (not decided/built): (a) leave ATR sizing off entirely, (b) enable
it only for the 14 symbols that passed and keep AMD/KO on flat sizing (would need per-symbol
config, not built), (c) try a different `atr_sl_mult` and re-run
`validate_atr_sizing.py` to see if a less aggressive multiplier holds up for AMD/KO too, (d)
something else.

**3. Kill-switch/drawdown thresholds — audited, reported, NOT changed (per explicit
instruction).** `git blame stock_bot/config.py`: `PAPER_DAILY_LOSS_PCT=0.03` — commit
`26f175e7`, 2026-06-19 (original circuit-breaker feature). `PAPER_WEEKLY_LOSS_PCT=0.05`,
`PAPER_DRAWDOWN_HALT_PCT=0.15`, `PAPER_KILL_SWITCH_PCT=0.20` — all three commit `7d7c90fc`,
2026-08-05 (the four-tier breaker expansion, same day as the crypto bot's equivalent — see
"Risk-gate config (stock bot)" in CLAUDE.md). None are set in `stock_bot/.env` — all four are
running on `stock_bot/config.py` defaults, confirmed via grep. Matches CLAUDE.md exactly, no
drift found. Left as-is for the user to decide whether the wider entry universe warrants
tightening.

**4. Sector/correlation/macro gate audit — confirmed generic, no hardcoded gap found.**
`get_sector()` (`stock_bot/data/price_feed.py`) is a live `yf.Ticker(sym).info` lookup for
ANY symbol (cached, falls back to `"other"` on failure) — used identically by both
`stock_bot/execution/paper.py` and `stock_bot/execution/ibkr.py`'s sector-concentration gate
(`_MAX_PER_SECTOR=2`), no symbol-keyed mapping anywhere. The correlation gate
(`stock_bot/risk/correlation.py`'s `fetch_correlation_from_closes` + `_check_correlation_gate`
in `main.py`) is pure Pearson math over candle closes for whatever
`executor.positions_snapshot()` actually holds that cycle — also symbol-agnostic, no
hardcoded pair list. Macro blackout (`_is_macro_event_blackout`) and VIX crisis mode are
market-wide and were never per-symbol to begin with. **Conclusion: (a), not (b)** — nothing
silently no-ops for a newly-opened symbol; both gates already work correctly for any symbol
the newly-opened universe can produce. The one residual limitation (not a hardcoding gap): the
correlation gate fail-opens if a held peer's candle data is missing from that cycle's
`price_data` (e.g. a fetch failure) — a data-availability edge case, not a symbol-coverage one,
and pre-existing/documented behavior.

**5. AI shadow-vote review criteria — new documented decision, no code.** See
`.memory/decisions/stock-whitelist-gate-removed-2026-08-23.md` for the full criteria: ≥15
completed round-trips on symbols outside {MRNA, AMD, RY.TO, PLTR} (mirrors the crypto bot's
own 15-fill capital-gate convention), AND either a ≥15-point win-rate gap vs. backtested-symbol
trades, PF < 1.0 on the non-backtested population, or a wide AI-agreement/disagreement outcome
gap (the existing "~30 trades" AI-shadow-vote comment in `main.py`, evaluated early and split
by this population specifically). Triggers a review, not an automatic reversal — decided
2026-08-23, before any bad outcome, per explicit instruction not to decide this retroactively.

Full test suite after all of the above: **612 passed** (was 605 immediately after the
whitelist-removal session; +5 from `test_screener_in_distribution.py`, +2 already added
earlier the same day for the unrelated Telegram getUpdates backoff fix — see that entry
above). `bot/strategy/*` untouched, strategy hash unchanged.
