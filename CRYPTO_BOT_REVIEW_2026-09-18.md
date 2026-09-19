# Crypto bot review and Claude Code implementation brief

Reviewed 2026-09-18. Scope: crypto execution, portfolio/risk integration, backtesting, validation and reporting. This is an engineering review, not a profitability certification. No trading code or live configuration was changed. No live exchange orders were submitted. `.env` secrets were not inspected.

## Assessment

The project has useful foundations: exchange-native protection, account-level loss limits, atomic JSON writes, shared capital allocation, candle watchdogs, fee-aware backtest metrics, and extensive mocked tests. However, unresolved-order handling can still duplicate orders, persistence cannot guarantee recovery across a crash, and several reporting/validation paths overstate what the evidence establishes.

Prioritize reliable execution and trustworthy net results before increasing strategy complexity or expanding live symbols.

Validation performed:

- `.venv/bin/python -m pytest tests/crypto tests/shared -q`: **591 passed in 19.55 seconds**.
- Additional isolated checks with mocked exchanges and temporary state reproduced three failures: duplicate submission after failed reconciliation; partial SELL closes the entire position; mixed capital caps produce negative available cash.
- Reviewed saved September 13 research and September 18 shadow reports. Did not rerun historical downloads or reconcile the live account; current account state and profitability are not verified.

## 1. P0 — Unknown submission outcomes can cause duplicate orders

Evidence: `bot/execution/live_executor.py:1093`, `:1168`, `:1530`, `:1699`.

`_find_untracked_entry_order()` returns `None` both when there is no matching order and when open/closed-order lookup fails. `_place_limit_order()` interprets that result as permission to submit a market fallback. A request timeout can occur after the original order was accepted.

**Reproduced:** mock limit submission raises `ccxt.RequestTimeout`; reconciliation raises `ccxt.NetworkError`; recorded submissions are `['limit', 'market']`. The second order is submitted while the first outcome is unknown. This does not prove a duplicate occurred in production; it proves the code permits one.

Direct market submissions and market fallbacks also lack the limit path's client-order identifier, and generic CCXT errors become REJECTED even when they represent unknown outcomes.

Implement:

- Explicit outcomes: confirmed rejected, pending/unknown, partially filled, filled, canceled. Never equate a timeout or failed lookup with confirmed rejection.
- Persist an order intent and unique client identifier before submission, using exchange capabilities correctly. An identifier is a reconciliation key, not an assumed exchange-wide exactly-once guarantee.
- Reserve funds while an intent is unresolved; block conflicting submissions across ticks and restarts. Reconcile orders and individual trades until the outcome is known.
- Cover all submission paths, including urgent exits and market fallbacks. Keep unknown exits distinct from confirmed failed exits before rearming protection.

Acceptance: accepted-but-response-lost plus unavailable lookups produces exactly one submission; restart preserves reservation; later discovery records the fill once; partial fills followed by additional fills record only new quantities.

Official reference: [CCXT timeout handling](https://github.com/ccxt/ccxt/wiki/Manual#requesttimeout) recommends checking order state and balances after a create-order timeout. [Kraken client order identifiers](https://docs.kraken.com/api/blog/cl-ord-id/) support tracing requests.

## 2. P0 — Native stop cancellation forgets unresolved protection

Evidence: `bot/execution/live_executor.py:745`, `:765`, `:1460`.

`_cancel_native_stop()` catches every exception, then clears and saves the stop identifier and trigger fields anyway. A timeout does not establish that the stop was canceled. The caller can then submit a SELL or attempt replacement protection without knowing whether the original stop remains active or filled.

Implement confirmed cancellation/reconciliation before clearing identity. Account for fills during cancellation; sell only the reconciled residual. Persist unknown cancellation state and prevent duplicate protective orders. On a rejected exit, restore protection only after determining what orders and inventory actually remain. Retain trailing-stop parameters needed for recovery.

Acceptance: cancellation timeout with a still-open stop retains its ID; stop fill during cancellation updates holdings and ledger exactly once; rejected exit restores exactly one protective order for remaining inventory.

## 3. P1 — Fill ledger and portfolio updates are not crash-consistent

Evidence: `bot/execution/live_executor.py:960`, `:1804`; `bot/main.py:963`; `bot/data/trade_log.py:22`, `:58`.

Executor JSON state is saved before the main loop writes the SQLite fill. Between those operations a crash can leave holdings updated without a fill record. `_save_state()` logs persistence failure but allows execution to continue. The fill table has no exchange order/trade IDs or uniqueness constraint, preventing reliable idempotent replay. Exchange order IDs exist on `Order` but are discarded by the normal logging call.

Implement a durable order/fill journal and transactional local bookkeeping. Make JSON snapshots rebuildable from the journal, or define an explicit recovery protocol. Store exchange trade ID, order ID, client ID, actual execution timestamp, strategy/run identifier and fee provenance. Reconcile on startup and periodically, not only during the next signal. Block new entries if accounting cannot be durably recorded; keep a deliberate recovery path for risk-reducing exits.

Acceptance: injected crashes before/after submission, fill receipt, ledger commit and snapshot writing recover to the same holdings/cash/fills; repeated reconciliation creates no duplicates. External holdings remain distinguishable from bot inventory.

## 4. P1 — Live performance reporting still ignores fees

Evidence: `live_comparison.py:24`, `:81`; `bot/data/trade_log.py:117`; `bot/portfolio/position_manager.py:89`.

The backtest metrics now allocate entry and exit fees, but `live_comparison._load_fills()` does not load fee columns and `_compute_metrics()` uses gross `pnl`. `TradeLog.summary()` does the same. The comparison baseline is hardcoded to an August result, preceding the September fee-metric correction and later exit-model changes. Its “Sharpe” uses trade P&L differences rather than regularly sampled account returns, so it is not comparable with the backtest statistic.

Implement one shared, tested closed-position accounting component for live reports and backtests. Include both sides' fees, proportional fee allocation on partial exits, base/third-currency fees and explicit treatment of missing fee data. Distinguish gross P&L, net realized P&L and marked account equity. Load versioned baseline artifacts instead of constants. Compute comparable risk metrics from a defined, regularly sampled equity series, or relabel/remove the incompatible statistic.

Acceptance: a price-profitable trade that loses after fees is a net loss everywhere; partial exits conserve total fees; incomplete/unknown-basis inventory is excluded or flagged; baseline config/engine mismatch is visible. Also fix `TradeLog.recent()`'s hardcoded column list, which currently drops fee columns from `SELECT *` results.

## 5. P1 — Mixed slot caps can overcommit capital

Evidence: `bot/portfolio/capital_pool.py:131` onward.

`slot_cash_for()` bounds explicit per-symbol caps by remaining capital, but its default branch returns the equal-share amount without that bound. `can_open_position()` checks slot count, not remaining cash.

**Reproduced:** total cash 100, two slots, BTC cap 80, SOL with no override. Allocate BTC then SOL: allocations total 130 and `available_cash == -30`.

Implement remaining-cash bounds for every sizing branch, including fee reserves. Separate reserving funds for an order from allocating confirmed holdings. Make execution use the actual reservation amount; verify main-loop executor cash refreshes obey it.

Acceptance: mixed capped/uncapped allocations never exceed available capital in any allocation order; failed/unknown orders release or retain reservations correctly; zero-cash pools do not admit an entry simply because a slot is free. This is a configuration-dependent bug, not evidence the current pool is overdrawn.

## 6. P1 when enabled — Partial take-profit liquidates the full live position

Evidence: `bot/execution/live_executor.py:1406`; `bot/main.py:2910`.

The caller supplies a partial quantity, but `LiveExecutor.execute()` overwrites every SELL quantity with the full position.

**Reproduced in dry-run:** buy 0.002 BTC, request SELL 0.001 BTC, receive filled quantity 0.002 BTC with zero inventory remaining. The caller subsequently calls `recover_long()` despite that full liquidation. The feature defaults to disabled, so this finding is conditional on enabling partial TP.

Implement an explicit full-close API or honor a validated requested SELL quantity capped by available bot inventory. After a partial fill preserve original cost basis, trailing state, capital allocation and a protective stop for the residual. Only recover LONG when inventory actually remains.

Acceptance: 50% sell leaves exactly 50%; overlarge request cannot oversell; full sell releases the slot; minimum-order/dust handling is explicit; live and simulated executors follow the same quantity contract.

## 7. P1 — Fresh market data and successful startup sync are not entry prerequisites everywhere

Evidence: `bot/main.py:2760`, `:3360`; `bot/execution/live_executor.py:238`, `:290`.

On ticker failure, the fixed-roster loop reuses `last_price` and continues. The candle watchdog verifies candle progress, which does not establish fresh executable prices. The dynamic ranked path already has a price-refresh check; that protection is not universal. Startup cash-sync failure falls back to configured starting cash and does not expose a persistent readiness flag from the executor.

Implement timestamped quote/account snapshots and explicit execution readiness. Block new BUYs on stale/invalid prices, incomplete holdings reconciliation or unknown funds. Do not replace a missing account balance with tradable fictional cash. Maintain separately defined exit/protection behavior during outages. Detect candle gaps and backfill missing completed bars before producing another signal; `_fetch_completed_candle()` currently fetches only two rows.

Acceptance: fresh candles plus failed tickers cannot authorize an entry; restart with balance failure cannot buy; recovery lifts the block only after successful sync; missed bars produce the same indicator state as uninterrupted processing. Reject NaN, infinity, nonpositive and out-of-order input values.

## 8. P1 — Backtest timing and portfolio realism still need work

Evidence: `bot/backtest/engine.py:224`, `:284`, `:313`; `dynamic_universe_backtest.py:1`.

- Signals consume the current candle's close and can fill at that same close. That is an optimistic execution assumption for a bot acting only after candle completion.
- Partial TP is processed before stop-loss using the same candle's high/low. If both levels were touched, OHLC alone cannot establish which came first; favorable partial profit can be booked before a stop even when the stop may have happened first.
- The recent trailing-stop fix avoids using a newly raised stop against an earlier open. It also delays that stop's effect until the next bar. That is a documented approximation, not a reconstruction of live intrabar trailing behavior.
- The expanded-universe script averages independent single-symbol runs. It explicitly does not model shared capital, ranking or slot contention.

Implement configurable next-bar executable entry fills and explicit latency/spread/fee assumptions. Use lower-timeframe replay for ambiguous exits where available; otherwise document and test conservative ordering and sensitivity bounds. Build a timestamp-interleaved portfolio simulator before claiming dynamic-universe portfolio performance. Preserve point-in-time universe snapshots going forward; a current list cannot reconstruct historical eligibility.

Acceptance: no fill before its information is available; bars touching both TP and SL follow a stated deterministic policy; all assets share one cash pool; simultaneous signals reproduce live ranking and reservations. Compare old/new results and invalidate dependent validation artifacts.

## 9. P1 — Validation identity does not cover the full trading behavior

Evidence: `bot/strategy/fingerprint.py:19`; `validate_symbol.py:156`; `walkforward.py:28`; `shadow_signal.py:463`.

The strategy fingerprint hashes three strategy/indicator files, excluding active configuration, exit engine, execution assumptions, risk/sizing and universe behavior. Identical hashes can therefore refer to materially different systems. The 5,000/3,000/1,000 candle validation windows are overlapping trailing samples, not three independent out-of-sample tests. Repeatedly tuning against the same held-out period also consumes its value as a holdout.

The shadow report says a failing PF with clean signal fidelity means “variance, not strategy failure.” That conclusion is unjustified: correct implementation can execute a strategy whose edge has disappeared or never existed. September 18's 200/200 signal matches establish agreement for comparable signals only, not profitability or complete execution fidelity.

Implement separate strategy, execution-model and full-run fingerprints, including normalized nonsecret config, data checksum/range, fee model and versioned engine. Preserve a research-trial registry and frozen, chronological evaluation protocol. Label overlapping windows as robustness checks. Require fresh forward evidence after material changes; report uncertainty and sample size rather than treating a fixed small trade count as proof. Replace the variance claim with an inconclusive-result statement and investigate both execution and economic edge.

Acceptance: changing exit/fee/sizing assumptions invalidates the relevant validation; cosmetic report edits do not invalidate behavior unnecessarily; future data cannot influence earlier selections; reports explicitly identify development, holdout and forward samples.

## 10. P2 — Reduce blocking work and test actual loop behavior

Evidence: `bot/main.py` is 3,790 lines; `bot/execution/live_executor.py` is 1,839 lines. Limit-chase defaults allow four 120-second attempts, excluding network delays (`config.py:155`). Symbols are processed sequentially. Existing native stops help during delays, but other software exits and trailing updates can wait behind a chase. Some main-loop tests assert source strings rather than exercising behavior.

After fixing order lifecycle, extract bounded operations for quote refresh, order reconciliation, exits, entries and valuation. Give exits/reconciliation priority and use nonblocking pending-order progression with explicit budgets. Avoid adding concurrency before capital/order ownership is safe. Introduce deterministic loop integration tests with fake clock, exchange and temporary storage. Track quote age, protection state, unresolved-order age, reconciliation discrepancy and execution latency in health reporting.

Acceptance: one symbol's entry chase cannot postpone another symbol's exit beyond a defined bound; protection is checked after restart and failures; integration tests assert outcomes rather than source spelling; the existing suite remains green.

## Strategy improvement: evidence first

The saved `logs/dynamic_universe_backtest_20260913.md` reports:

| Symbol | Pinned net PF | Rolling net PF |
|---|---:|---:|
| BTC | 0.82 | 1.17 |
| SOL | 0.94 | 1.05 |
| ETH | 0.42 | 0.31 |
| XRP | 0.55 | 0.57 |

These are saved historical results using Binance USDT proxies and the model available when generated, not fresh results from this audit. Later execution-model edits mean they need regeneration under a versioned corrected engine. They show fragile fee-adjusted evidence and no support in those runs for adding ETH/XRP to this strategy. They do not establish future performance.

Once accounting and simulation are corrected, ask Claude Code to:

1. Measure actual maker/taker mix, fees, spread, order latency and implementation shortfall. Persist signal candle close, decision-time quote and execution-time reference separately; candle movement and execution slippage are different quantities.
2. Compare pullback and breakout modes independently, by symbol and regime, on training data only. Record all experiments and test simple predeclared changes on untouched data.
3. Report net expectancy, net PF, turnover, exposure, drawdown, tail losses, time underwater and cash/buy-and-hold benchmarks, with uncertainty and clearly matched capital assumptions.
4. Stress fees, spreads, slippage, missed fills, outages and stop gaps. Evaluate lower turnover or maker policies only if measured economics justify them; maker orders are not guaranteed to fill.
5. Continue forward paper measurement using frozen settings. Treat insufficient evidence as insufficient evidence; do not loosen loss limits or add indicators merely to generate more trades.

## Suggested Claude Code handoff

> Read CRYPTO_BOT_REVIEW_2026-09-18.md and current repository instructions. Recheck findings against HEAD, then implement separate reviewable changes in this order: (1) durable order outcomes/reconciliation and native-stop cancellation; (2) crash-consistent ledger and net live reporting; (3) capital bounds, partial SELL contract and fresh-data/readiness gates; (4) realistic backtest execution and versioned validation; (5) loop extraction and observability. Start each bug fix with the behavioral regression cases specified in the review. Preserve current trading mode and configuration unless explicitly tasked to change them. Do not place live orders or promote a strategy as part of these fixes. Run the crypto/shared suite and relevant new tests, and report which historical results need regeneration. Prefer measured execution and validation improvements before strategy tuning.
