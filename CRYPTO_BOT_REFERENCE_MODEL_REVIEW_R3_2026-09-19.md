# Reference model review R3 — September 19, 2026

Ran the reference-model suite: **27 passed in 5.97s**. Full crypto/shared results were not independently rerun this pass. Additional reproductions used temporary SQLite and injected retrieval results only. No production changes, exchange calls, or HALT changes.

The correction-aware reconstruction now produces the correct −$1 result for the earlier example, and native-row value corruption is detected. The conditional wording for watermark evidence is also more accurate. Two compositional gaps remain before production integration.

## 1. An incomplete audit is reported as clean

Location: `execution_accounting_reference_model.py:432`, audit_historical_window.

The function ignores `coverage.complete` and its failure reason. It searches only the returned partial trades for unknown IDs, then returns violated=False if none are found. Readiness's documented conversion, `historical_audit_clean=not audit.violated`, therefore upgrades an inconclusive audit to clean evidence.

Reproduction: inject `CoverageResult(False, [], 2, 0, 'reported total drifted')` from retrieve_with_coverage_proof. audit_historical_window returns `AuditResult(violated=False, newly_discovered_trade_ids=[])`; its result is consumed as clean. An unstable/truncated read containing only already-known IDs has the same problem. This directly exercises a failure state that the retrieval API already supports.

Required: return distinct clean, violated, and incomplete/failed outcomes, retaining coverage reason and symbol/window evidence. Clean requires successful coverage for the requested window. Readiness must not strengthen its evidence on an incomplete audit. Test count drift, truncation with only known records, retrieval exceptions, and wrong-window audit results. Continue to describe even a successful audit as evidence at that observation time, not proof of unbounded future visibility.

## 2. A valid fee correction makes ledger verification fail with no repair path

Locations: record_fee_revision, load_observed_trades, write_ledger_rows, and verify_ledger_delivery_consistency.

Reconstruction now uses the latest fee, but the existing fills row remains at its original fee and P&L. The verifier compares that frozen row directly to the corrected trade/fold. Ordinary replay skips the row because its identity is already represented, so replay cannot close the discrepancy.

Reproduction: commit/write BUY 1 at 100 and SELL 1 at 101, both initially fee-free. Verification initially returns True. Record a valid SELL fee revision to 2, load the corrected trades, and run ordinary ledger replay. Recovered P&L is correctly **−1**, but stored SELL remains **fee 0 / P&L +1** and verification remains **False**.

This is a fail-closed convergence gap rather than silent false readiness. A valid correction should be representable coherently without manual SQL edits. Choose one policy: immutable base execution rows plus a correction-aware effective ledger view, or transactionally refreshed derived fee/P&L projections with an immutable audit trail. Compare equivalent representations in verification. Apply the same policy to legacy-linked aggregates and entry-fee corrections affecting later SELLs and remaining basis.

Acceptance: valid correction → replay/rebuild → DB close/reopen → verifier True and correct P&L; repeat the correction without duplication; inject a crash during materialization if projections are updated. Genuine unrelated corruption must continue to fail.

## Next step

Keep working in this prototype. Add these two composed lifecycle tests instead of another redesign. Current progress supports the architecture direction, not live integration yet. HALT and profitability gates remain unchanged.
