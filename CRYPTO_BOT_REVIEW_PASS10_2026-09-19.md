# Crypto bot review — pass 10, September 19, 2026

Reviewed the supplied staged changes to `bot/execution/live_executor.py` and `tests/crypto/test_crash_consistency_harness.py` on top of HEAD `d4bb1bd`. These changes were not a new commit in the visible log. The staged pass-9 report was also present. No implementation, settings, HALT state, or live orders changed during this review.

## Verification

`.venv/bin/python -m pytest tests/crypto tests/shared -q`: **769 passed in 18.53s**. Full-repository tests, strategy fingerprint, and pinned backtest were not independently rerun in this pass.

Two findings below were independently reproduced. Executor tests used the repository's stateful FakeExchange, actual LiveExecutor constructors, temporary state files, mocked exchange construction/configuration, and disabled Telegram sends. The reporting reproduction used the actual discovered-BUY consumer, real PositionManager/state machine/capital pool, real temporary SQLite TradeLog, and the test suite's fake protection executor. No live exchange calls or messages were sent.

## 1. P0 — Discovery time is still confused with execution time, replaying synchronized fills as live trades

Locations: `bot/execution/live_executor.py:1138` (`_record_live_stop_execution_delta`), `:1290` live/cash-free dispatch, `:1590` queued retries, and `:1926` (`_adopt_untracked_stop`).

The new `apply_as_live_fill` flag chooses accounting according to **which caller finally obtains the order**, not whether the execution was included in the balance synchronization. A fill that happened before startup can remain unreadable during startup and become readable on the next tick. Calling its recovery with `live=True` applies the already-synchronized cash and inventory effects a second time.

### Reproduction A: terminal offline fill, delayed successful lookup

1. Seed $1,000 cash, 0.002 BTC, an $85,000 basis, and tracked stop `O1` at $78,000; save state.
2. While offline, let `O1` fill 0.001 BTC at $78,000, fee $0.16, and become terminal. Exchange now has $1,077.84 and 0.001 BTC.
3. Restart with final-order lookup raising `ccxt.RequestTimeout`. Startup correctly synchronizes the balances above and queues the unknown historical outcome.
4. Restore healthy lookup and call `reconcile_pending_orders()`. No exchange execution occurs after startup.

| Result | Correct / exchange | Actual executor |
|---|---:|---:|
| Cash | $1,077.84 | **$1,155.68** |
| BTC inventory | 0.001 | **0** |
| New execution after startup | None | Returns a live 0.001 SELL |

The residual holding is now missing from executor state. Passing the returned SELL to the normal downstream consumer would likewise treat a historical fill as a new reduction of already-synchronized inventory.

### Reproduction B: adoption transfers the same stale accounting baseline into a live cursor

Start with the same partial fill, but keep `O1` open. During the first restart, return an empty open-order listing and time out direct lookup, queuing historical recovery. On a second restart, let open-order discovery work while the queued direct lookup still times out.

The new adoption code removes the queue entry and seeds active progress with its frozen **zero** filled baseline, although startup balances already include the historical 0.001 fill. A subsequent successful `_cancel_native_stop()` reads that old fill and books it through live accounting.

Observed again: executor **$1,155.68 / 0 BTC**, exchange **$1,077.84 / 0.001 BTC**. The queue is empty, so the duplicate-owner bug is fixed, but transferring ownership without reconciling the accounting boundary introduces this double application.

**Required change:** Track whether recovered economic effects are included in the authoritative balance checkpoint independently from whether the fill has been journaled and delivered. Preserve this distinction during queued recovery and adoption. The current single cumulative baseline conflates those obligations. A caller-mode boolean alone cannot determine when the exchange movement happened. If the order cannot be reconciled with a trustworthy checkpoint, keep the discrepancy explicit and reconcile it before treating it as fresh trading activity.

Do not simply make all retries cash-free again: that would restore pass 9's missed post-startup cash/inventory changes. Do not simply seed adoption from raw exchange progress: that would consume historical executions without recovering their journal/P&L.

**Acceptance matrix:**

- Execution before startup, lookup succeeds only on a later live tick.
- Execution after startup, lookup succeeds on that tick.
- One order contains both pre-startup and post-startup deltas.
- Fee-only corrections before and after the checkpoint.
- Adoption while direct historical lookup still fails, followed by cancel/replacement.
- Repeated restarts/crashes around each transition.

Assert cash, inventory, realized P&L, execution journal, downstream state, and SQLite rows together. A replayed historical execution must be reportable without selling already-reconciled inventory again.

## 2. P1 — Recovered BUY protection can write its SELL before the BUY, overstating net performance

Locations: `bot/main.py:1469` (`_process_discovered_buy_fill`), its protection/discovered-SELL handling before `trade_log.log_fill(side="BUY")`; `live_comparison.py:83` fill ordering and `_compute_live_metrics` entry-fee allocation.

This is an additional finding in nearby integration code, not a claim that the staged executor edits introduced it.

The recovered BUY consumer first updates PositionManager, then synchronizes protection and processes any SELL discovered during that operation. It writes the BUY to TradeLog only afterward. If the new/resized protection immediately executes, SQLite receives SELL then BUY, although their economic order was BUY then SELL. The live report loads rows by insertion ID and allocates entry fees in that order, so it cannot charge the late-written BUY fee to the earlier row representing its exit.

**Reproduction with actual consumer and temporary SQLite:**

- Recovered BUY: quantity 1, price $100, fee $0.80.
- Fake protection executor returns an immediate full SELL: quantity 1, price $101, fee $0.40.
- Invoke `_process_discovered_buy_fill()` with real PositionManager/state machine/capital pool and TradeLog.
- Load the resulting SQLite fills and call `_compute_live_metrics()`.

Actual rows: `SELL ($1 gross P&L, $0.40 fee)` followed by `BUY ($0.80 fee)`.

Actual report: **+$0.60 net P&L, 100% win rate, $0.80 unallocated BUY fees**, despite the position being flat. Correct round-trip net P&L is **−$0.20**, win rate **0%**, with no unallocated entry fee.

**Required change:** Preserve causal execution ordering in the durable ledger. Commit the recovered BUY record before consuming protection-generated SELL records, or use a durable sequencing scheme that both normal writes and restart replay honor. Do not delay necessary protection merely to repair reporting order. Insertion ordering and replay must agree; timestamps generated during recovery alone are not a reliable execution sequence.

**Acceptance:** Real SQLite plus the actual BUY/SELL consumers for immediate full and partial protective exits, with entry fees, and crashes/replay around both inserts. Verify net P&L/PF/win rate and that a fully closed position leaves no entry fee unallocated.

## Recommended next pass

Fix the balance-checkpoint/ledger distinction before adding further caller-specific switches. Then add integration coverage for causal event ordering through the real ledger. These reproduce gaps outside the current three regression tests; passing the previous scenarios does not establish the complementary cases.

Keep HALT and the existing resumption gate unchanged. No profitability validation was performed or implied by this execution/accounting review.
