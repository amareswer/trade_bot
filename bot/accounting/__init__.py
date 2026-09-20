"""
Execution-accounting reconciliation layer (crypto bot).

Design: CRYPTO_BOT_EXECUTION_ACCOUNTING_DESIGN_2026-09-19.md, revised after
CRYPTO_BOT_EXECUTION_ACCOUNTING_DESIGN_REVIEW_2026-09-19.md and
CRYPTO_BOT_EXECUTION_ACCOUNTING_DESIGN_REVIEW_R2_2026-09-19.md. The algorithms
here were first proven, adversarially, against a synthetic exchange in
execution_accounting_reference_model.py (repo root) across four review
passes (PASS-3 .. R4) — this package is that same reasoning, adapted to the
real trades.db schema and a real ccxt.kraken adapter, wired additively into
the live bot.

Package layout:
    store.py          — SQLite schema (observed_trades, checkpoints,
                         trade_fill_links) added to the EXISTING trades.db
                         alongside `fills`/`fee_adjustments`; no separate DB.
    engine.py          — pure accounting algorithms (coverage proof,
                          watermark, balance identity, causal order,
                          position fold, checkpoint commit, recovery,
                          legacy-migration matching, four-way verification).
                          Exchange-agnostic; takes an ExchangeAdapter Protocol.
    kraken_adapter.py  — real ccxt.kraken implementation of that protocol,
                          including TRUE ofs-paginated fetch_my_trades
                          (ccxt's unified fetch_my_trades does not paginate
                          or expose Kraken's own `count` — see the module
                          docstring and design §1).
    reconciliation.py  — the per-cycle orchestration: pull → observe → check
                          → checkpoint → block/unblock. This is what
                          bot/main.py calls periodically.
    live_observe.py    — synchronous, per-fill observation: right after an
                          ordinary live fill is confirmed and logged, look up
                          its REAL exchange trade id(s) by order_id (exact,
                          not ambiguous — the order_id is already known) and
                          link them. This is the common case; reconciliation
                          cycles handle whatever this misses (e.g. a
                          broker-triggered native-stop fill, a fetch delay).
    migration.py        — one-time historical backfill (§3.3 / item 8):
                          conservation-based matching, blocks ambiguous rows.

Nothing in this package is imported by bot/main.py or bot/execution/
live_executor.py unless cfg.accounting.enabled is True — see AccountingConfig
in config.py. logs/HALT is never read or written here.
"""
