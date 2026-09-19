# Offline reference-model review — September 19, 2026

Reviewed `execution_accounting_reference_model.py` and its test file. Ran the reference-model suite: **15 passed in 1.54s**. Additional reproductions used only the standalone model and temporary SQLite. No production implementation, configuration, HALT state, or live exchange access changed.

## Decision

Continue the offline prototype. Its isolation, transactional schema, separate readiness fields, and conservative rejection of ambiguous same-symbol ordering are useful. The current tests demonstrate selected examples; they do not yet prove history completeness, migration/replay composition, or restart convergence. No further production changes are warranted from these results yet.

## 1. Coverage of visible records is still reported as complete economic history

Locations: model `:177` (`fetch_my_trades_page`), `:293` (`retrieve_with_coverage_proof`), `:756` (`assess_readiness`).

The fake's reported_total counts only visible records. Matching that count proves pagination exhausted the visible result, not that all relevant executions were visible. The offsetting-events test explicitly accepts complete coverage for an empty visible window and does not test readiness while the hidden events exist.

**Reproduced:** deposit opening CAD 1,000; execute a hidden BUY of one BTC at CAD 100 at timestamp 1000, then a hidden SELL of one BTC at CAD 100 at 1001, zero fees. Actual cash and inventory return to their opening values. Retrieval returns zero trades with complete=True; balance consistency passes. Passing True for ledger_delivery_ok (the empty visible ledger is internally consistent) produces **“ready: coverage complete, balance consistent, ledger/delivery consistent”**, although two executions are missing.

After revealing those trades, fetching with `since_ms=1001` still returns an empty complete result: the exclusive cursor permanently excludes them. This is what happens if a caller advances a checkpoint/window boundary after the apparent success. The reference model has no durable orchestration preventing that advance.

**Next test/contract:** distinguish visible-page exhaustion from verified coverage of an economic window. Introduce an explicit evidence/unknown state rather than claiming completeness from a count alone. Test delayed older trades and equal-boundary timestamps after cursor advancement, with overlap and ID deduplication. A durable watermark/completeness guarantee must come from a justified source protocol, not the fake's private knowledge or balance equality.

Kraken start exclusivity alone is not a reason to remove replay overlap. The real installed CCXT wrapper also converts millisecond since to integer seconds and then applies parsing/filtering. The prototype's exact millisecond-exclusive predicate is a simplifying contract, not proof of end-to-end adapter behavior.

## 2. Migration links are ignored by normal ledger replay

Locations: model `:525` (`write_ledger_rows`), `:641` (`apply_migration_link`).

Migration correctly retains a legacy UUID-keyed row and records a link for each underlying trade. But write_ledger_rows checks only `fills.exec_key == trade_id`; it neither checks legacy_links nor the migration's ledger-written marker. The two otherwise useful functions do not compose idempotently.

**Reproduced:** insert a legacy BUY row for quantity 1 at 100; migrate one matching trade T into observed_trades/legacy_links; then call write_ledger_rows for T. The fills table becomes **[('legacy', 1.0), ('T', 1.0)]**. One execution is represented twice.

**Next test/fix:** define a single ledger-representation predicate supporting both native rows and conserved legacy mappings. Use it in ordinary replay and verification. Add migration → checkpoint → ordinary replay → reconnect/replay tests. Also enforce global uniqueness of trade allocation across different legacy rows; individual subset matching alone cannot establish that separate migration results do not consume the same execution.

## 3. The crash/restart tests do not exercise the claimed persistence boundaries

Locations: model `:489` (`commit_checkpoint`); tests `test_checkpoint_commit_failure_leaves_no_partial_state` and the three-restart scenario.

`fail_before_commit=True` returns before entering the SQL transaction. Zero rows afterward establishes that an early return made no writes; it does not test rollback after checkpoint insertion or after one of several observed-trade inserts.

The three-restart test uses the same connection, caller-held objects, and manually supplied since values throughout. Its final balance assertion checks SyntheticExchange's arithmetic. It does not close/reopen SQLite, recover the cursor/checkpoint from disk, rebuild a position from persisted data, or compare the rebuilt cash/P&L with the exchange. All fills are BUYs, so missing persisted entry basis cannot be detected by later realized-P&L assertions.

**Next tests:** inject failure after the first SQL mutation, after an observed-trade insertion, after ledger insertion but before acknowledgment, and after commit before caller acknowledgment. Reopen the DB and reconstruct state using only persisted records. Use a BUY before restart and partial/full SELLs afterward, varying prices and fees. Compare reconstructed cash, inventory, realized P&L, ledger representation and readiness. Advance the durable retrieval cursor only with the transaction that establishes its coverage.

## Additional limits to label honestly

- assess_readiness accepts ledger_delivery_ok from its caller; there is no implemented joint ledger/delivery verifier behind that argument yet. Exposing three booleans is useful but does not independently compute three proofs.
- SyntheticExchange.revise_trade_fee changes its trade payload without changing its cash balance or ledger. The existing fee test proves correction-row deduplication, not economic convergence after a real fee movement.
- The arithmetic remains float/SQLite REAL, with tolerances including 1e-12 in balance checking and 1e-6 in migration. Documentation calling these comparisons exact should be narrowed or the reference arithmetic changed to decimal units. Base-currency fee effects are also not modeled.

## Bounded next step

Extend this same prototype; do not restart the design or wire it into LiveExecutor yet. Add the three counterexamples above, implement a durable recovery driver and actual ledger-consistency checker, and state the evidence required for history completeness. Keep successful existing tests as regression examples, while replacing the vacuous failure injection with failures inside real transactions. HALT remains unchanged.
