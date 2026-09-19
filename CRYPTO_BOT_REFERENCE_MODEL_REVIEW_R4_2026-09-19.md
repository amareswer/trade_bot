# Reference model review R4 — September 19, 2026

Verified **32 reference-model tests passed in 3.55s**. The broader reported 809-test run was not independently repeated. Reproductions below used only temporary SQLite and injected synthetic retrieval results. No production code, HALT, or live exchange access changed.

The earlier incomplete-with-no-new-records case now returns inconclusive correctly. Native fee/P&L projection refresh and its transaction rollback are substantive improvements. Two remaining cases require either correction or explicit readiness blocking; neither requires another architectural redesign.

## 1. Partial retrieval discards positive evidence of a historical omission

Location: audit_historical_window, early `if not coverage.complete` return.

An incomplete read cannot establish a clean audit, but it can establish a violation if a returned record is already an unknown trade inside the audited window. The function currently discards all partial trades before comparing IDs.

Reproduction: inject `CoverageResult(False, [Z], 2, 1, 'count drift')`, where Z is an unrecorded trade at timestamp 2000 and the audit window ends at 3000. Actual result is **inconclusive with an empty newly_discovered_trade_ids list**. Conversion yields None rather than False, so otherwise-passing readiness can remain conditionally ready despite concrete contrary evidence.

Required: inspect successfully retrieved records even if coverage is incomplete. An unknown trade in scope establishes violated; include the discovered IDs and retain the retrieval-incomplete reason. Incomplete with no observed violation remains inconclusive. Clean requires completeness and no violation. Test all three cases together.

## 2. Deferred legacy P&L is still allowed to pass economic verification

Locations: refresh_ledger_projection_for_corrections legacy loop and verify_ledger_delivery_consistency legacy checks.

The documented deferral of legacy P&L refresh is acceptable as a prototype boundary only if readiness reflects that boundary. Currently refreshing legacy fees makes conservation checks pass while stale realized P&L is not checked.

Reproduction: native BUY 1 at 100 with zero fee; migrated legacy SELL 1 at 101 with original zero fee and stored P&L +1. Record a SELL fee correction to 2 and refresh the projection. Actual legacy row becomes **fee 2, P&L +1**; correction-aware reconstruction gives **−1**; verification nevertheless returns **True**.

The defect is not that the prototype has a declared unsupported feature. It is that the economic-consistency result passes despite that known unresolved dimension.

Bounded options: (a) refresh/check legacy P&L under an explicit conserved aggregate policy, or (b) mark affected legacy projections unsupported/unresolved and make readiness fail until reconciled. Option (b) is sufficient for this prototype iteration and does not require expanding the legacy accounting design now. Include entry-fee corrections that change later legacy SELL P&L even if those SELLs' own fees did not change.

## Next step

Add these two cases and keep the current architecture. Preserve positive evidence from partial audits and make unsupported legacy economics visible to readiness. No production integration or HALT change follows from the passing prototype suite.
