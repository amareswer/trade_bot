# Historical fixture integration review — September 19, 2026

Ran the new fixture suite: **4 passed in 3.40s**. Read the fixture, test implementation, source Markdown reconciliation report, and the relevant historical rows through a read-only SQLite connection. No live API calls, production changes, or HALT changes.

## Verdict

Useful historical-data replay coverage, including real SQLite persistence and fee projection. This is not yet a test of the actual CCXT response adapter or an account-wide reconciliation proof. Keep the new tests, correct provenance, and strengthen the assertions before describing this milestone as complete.

## 1. Label the fixture as reconstructed and track provenance per field

The JSON was assembled from rounded report tables and local DB rows, not saved as a raw TradesHistory response. Its claim that only one field is synthesized is inaccurate:

- Six `ordertxid` values are the placeholder `unknown-not-captured`.
- `pair` uses normalized BTC/CAD, rather than demonstrating actual Kraken raw market-ID parsing.
- The primary trades_history fixture puts the local DB's erroneous zero fee into an exchange-shaped record. The source report's Kraken history already reports fee 0.4064. This represents a local-record repair, not evidence Kraken returned zero and subsequently revised the trade fee.
- The external-holdings trade is assigned a synthetic ID even though `logs/trades.db` row 9 notes contain the captured ID **TEYLVF-3GXRC-N6RME4**.
- Some values are rounded/reconstructed rather than exact source payloads: for example, first BUY cost 10.0423 in the fixture versus DB value 10.0423439. Row 9 quantity is 0.00037766 in the DB versus rounded 0.000378 in the fixture's incident record.

Required: field-level labels for captured, report-rounded, normalized, placeholder, and reconstructed values. Restore the actual external trade ID. Keep authoritative exchange-history fee and erroneous local ledger fee in separate fixture sections. The correction test can still exercise exactly those real numbers without pretending a local omission was an exchange fee revision. No new live capture is required to make this honest.

## 2. The custom parser does not verify CCXT adapter behavior

KrakenFixtureSource directly constructs SynTrade from JSON, assumes pair is already normalized, hardcodes CAD, and implements synthetic pagination/count semantics. No installed CCXT parsing, market normalization, request construction, or real response pagination is exercised. This is useful integration of a custom fixture parser with the reference model, but its expected representation and parser were authored together.

Next bounded test: feed independently defined Kraken-shaped payloads through the installed CCXT method with transport and market loading mocked offline. Compare its normalized output with the reference-model input. Explicitly test raw pair identifiers, returned IDs, precision/timestamps, and account-scoped page filtering. Unknown order IDs mean the current historical fixture cannot validate per-order grouping; use labeled synthetic IDs for that separate contract test if genuine IDs are unavailable.

## 3. Strengthen what the tests actually assert

The balance test says it proves an honest nonzero residual, but only asserts residual is not None and is a float. A broken implementation returning 0.0 and consistent=True would pass. For the current reconstructed fixture, independently computed values are expected_balance **100.22854**, actual_balance **154.1094**, residual **53.88086**. Assert the appropriate expected residual and `consistent is False`, then assert readiness is blocked under this incomplete account scenario. Update expected values if correcting provenance changes the fixture.

The full pipeline writes the account-wide July balance into a checkpoint containing six selected June BTC trades even though the account check is known not to reconcile. That is fine as a low-level persistence exercise, but must not be treated as a verified economic checkpoint. Add an orchestration-level assertion that this scenario cannot publish a balance-confirmed/ready checkpoint.

The external-holdings test establishes that a zero-opening-inventory fold rejects the extra SELL. It does not prove ownership classification or prevention of the original live sale; no ownership guard/production executor is called. Narrow the fixture's claim that an opening_snapshot mechanism would exclude the trade. The current test intentionally excludes it by construction in the success case.

## Next step

Correct fixture provenance, add exact negative-readiness assertions, and add one offline installed-CCXT adapter contract test. This is a bounded extension of the milestone, not a request for more live trading changes or another accounting redesign. The historical fee discrepancy and DB round-trip tests remain useful within their stated scope.
