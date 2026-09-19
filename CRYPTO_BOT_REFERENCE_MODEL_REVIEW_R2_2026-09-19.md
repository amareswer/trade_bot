# Offline reference model, second review — September 19, 2026

The reference-model suite passes: **21 tests in 2.60s**. Reviewed the new watermark, migration predicate, real transaction rollback injection, DB-reopen reconstruction, fee corrections, and ledger verification. Additional reproductions used only synthetic data and temporary SQLite. No production code/settings, live API calls, or HALT changes were made.

## Progress established

The new rollback injection executes inside a real transaction, and the new reopening tests improve the previous simulated restart coverage. Migration and normal replay now share a representation predicate. Synthetic fee revision now moves exchange cash. These directly address material weaknesses from the previous review; retain those tests.

Three remaining issues limit the claims that may be made about readiness and reconstruction.

## 1. An aged watermark is conditional on a visibility bound, not confirmed coverage

Locations: `execution_accounting_reference_model.py:352` compute_safe_watermark, `:397` is_watermark_confirmed, and readiness evaluation.

The code correctly documents that trades hidden longer than the safety margin can still be lost. The user-facing claim that permanent loss/false readiness is fixed is therefore too broad. Also, a watermark computed as now minus margin immediately passes is_watermark_confirmed: it is not independent confirmation that unseen events surfaced.

**Reproduced:** opening cash 1,000; hidden BUY 1 at 100 at timestamp 1000, hidden SELL 1 at 100 at 1001, no fees. At now=10000 with margin=5000, visible pagination is empty/complete, balance consistency passes, computed watermark=5000, and readiness returns **True**. Revealing those trades later cannot recover them using an exclusive since=5000 query.

This is the acknowledged out-of-margin case, not a claim the code violates its stated bounded-delay assumption. The missing requirement is evidence that the assumed bound holds for the actual retrieval source, including outages and backfills. Until then call this conditionally aged/provisional, not confirmed completeness. Specify periodic historical audits or another justified completeness protocol and the behavior when a late record violates the assumption. Add this counterexample as a limitation test; do not merely increase the margin until it passes.

## 2. Restart position/P&L reconstruction ignores durable fee corrections

Locations: model `:685` record_fee_revision; `:732` load_observed_trades; `:741` recover_position.

The correction is persisted separately, but load_observed_trades reads only each trade's original fee_cost and recover_position folds that unchanged payload. Making the fake exchange's cash movement real does not make reconstructed accounting incorporate it.

**Reproduced using a close/reopen:** persist BUY 1 at 100 and SELL 1 at 101, originally zero fees. Write their ledger rows, record a revision setting the SELL fee to 2, then close and reopen the DB. recover_position returns **+1 realized P&L**, while the correct result is **−1**. verify_ledger_delivery_consistency still returns True.

Required: apply effective fee revisions exactly once in recovered accounting, with currency-aware attribution and a clear original-payload/audit policy. Entry-fee revisions must update remaining basis and realized P&L for already-closed portions. Test partial holdings, repeated identical revisions, correction replay, and a real DB restart—not only a balance check supplied manually with fee_correction_deltas.

## 3. Ledger verification checks identity presence, not economic/delivery consistency

Location: model `:756`, verify_ledger_delivery_consistency.

The function verifies native-row XOR legacy-link presence, written markers, and that observed trades can be ordered. It does not compare stored quantity/cost/fee/P&L, validate legacy aggregate conservation here, or compare the reconstructed result with an independent delivered state.

**Reproduced:** after the valid BUY/SELL setup above, modify the SELL fills row to quantity=99 and pnl=999 while preserving its exec_key. The verifier still returns **True**. This synthetic inconsistency demonstrates exactly what the checker does not establish; it is not a claim production code currently performs that update.

Required: either name/report this narrowly as a representation check or extend it to the claimed economic invariant. Validate canonical fields and conserved legacy totals, effective correction accounting, and orphan/excess ledger rows. Include a separate comparison to reconstructed/delivered position and P&L. Matching identities are necessary but insufficient for the third readiness property.

## Next step

Continue this same isolated model. Fix correction-aware reconstruction and strengthen or narrow the verifier. Make the watermark's explicit assumption a visible part of readiness evidence rather than an automatically satisfied confirmation flag. These are bounded prototype improvements; no live integration, permission change, or HALT change is warranted yet.
