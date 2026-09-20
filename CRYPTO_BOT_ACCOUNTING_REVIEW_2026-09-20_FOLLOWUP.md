# Accounting and migration review — 2026-09-20

Scope: current migration, backup, live observation, matching, and ledger verification. This is not a full security audit or profitability assessment. Production code, live databases, HALT, and git history were not modified. Existing user edits to claude.md were left alone.

Verification: 26 existing tests passed across test_accounting_migration.py, test_migrate_legacy_fills_cli.py, test_accounting_live_observe.py, and test_accounting_four_way.py. Additional offline reproductions used disposable SQLite databases and the existing FakeExchangeAdapter. No exchange calls.

## P1: Migration group linking still has an internal commit boundary

Locations: bot/accounting/migration.py:77 and bot/accounting/store.py:157.

The outer `with conn` in the existing-fill matching branch calls `upsert_observed_trade`, which enters its own `with conn`. SQLite connection contexts are not nested savepoints. On inserting the second observed trade, that helper commits the first trade's link too. An exception immediately after the second upsert leaves a partial group durable.

Reproduced: a quantity-2 legacy fill matched to two quantity-1 trades. Injecting the exception after the second real upsert leaves only T1 linked. Re-running migration reports `linked=0 blocked=0 orphans_backfilled=0` and still leaves only T1 linked. The fill is excluded by `unlinked_fills`, and T2's existing source is live, so recovery skips it.

Fix: a single transaction owner for all observations, links, and markers in a matched group. Helpers called inside it must not commit. Test failure after the second actual upsert, close/reopen, and retry. The earlier orphan-backfill recovery fix addresses a different branch.

## P1: Backup and dry-run copies are not SQLite snapshots

Locations: migrate_legacy_fills.py:55 and :70.

Both paths use `shutil.copyfile` on the main database file. Committed SQLite WAL transactions can remain in the companion WAL file, and concurrent writes also make raw copying unsafe.

Reproduced with a temporary WAL-mode database kept open: after creating a table and committing a row, the copied main file raises `OperationalError: no such table: x`. This establishes the missing safeguard; it does not assert that the live database currently uses WAL.

Fix: use SQLite's connection backup API for both paths, with unique non-overwriting names and an integrity check. Test committed WAL contents and active-writer snapshots. Do not endorse the live-apply restore point until this is fixed.

## P1: Live observation assigns an order's executions to the wrong partial-fill row

Location: bot/accounting/live_observe.py:45.

The observer filters by order and symbol but does not use its side/quantity parameters to constrain the matched executions. Every visible unlinked execution of that order is assigned to the supplied fill row. One exchange order may have multiple local delta-fill rows.

Reproduced: a real SQLite fill row with quantity 1 and an order containing two quantity-1 executions. Calling observe_fill with quantity=1 links both T1 and T2 to that row. A subsequent row can no longer claim its execution. Per-trade commits also retain the partial-group crash risk in this path.

Fix: reconcile order execution groups against the local fill deltas with quantity/cost/fee conservation, durable allocation, and atomic group writes. If allocation is ambiguous or visibility incomplete, keep it pending and expose the delivery gap. Test multiple local rows for one order, delayed visibility, and mid-group failure.

## P1: Ledger verification accepts the incorrect links

Location: bot/accounting/four_way.py:34.

The verifier compares link existence with marker existence, without checking the linked fill's economics. In the preceding quantity-1-versus-quantity-2 reproduction it returned `ok=True`. The `double_represented_fill_ids` field is not populated by this function.

Fix: validate referenced fill existence, trade ownership uniqueness, and aggregate quantity/cost/fee conservation under the declared correction policy, plus P&L where applicable. Report unresolved delivery explicitly. Tests must corrupt amounts or link allocation while leaving markers intact.

## P1: “Exact conservation” matching omits cost and known order identity

Locations: bot/accounting/engine.py:402 and :436.

UnlinkedFill drops the row's price and order_id. The matcher checks only quantity and fee within a time window. Reproduced: a quantity-1 legacy BUY priced at 100 with zero fee accepts a sole candidate priced at 900, cost 900, and a different order ID, returning blocked=False.

Fix: require notional/cost conservation and compatible fee currency; enforce order identity when present. Legacy rows without an order ID still require economic conservation. Retain ambiguity blocking. Test same quantity/fee with different prices and conflicting known order IDs.

## Decision

Keep HALT. Fix these bounded defects and prove them with offline regressions before running or approving a migration. Passing the software checks remains separate from security sign-off, shadow-run evidence, and the existing profitability gate.
