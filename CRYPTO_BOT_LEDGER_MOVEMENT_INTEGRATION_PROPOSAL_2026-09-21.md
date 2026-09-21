# Crypto bot — ledger-movement integration proposal (2026-09-21)

**Status: PROPOSAL ONLY, v4 (revised three times same day — v2 fixed four v1 gaps, v3 fixed
four gaps found in v2, v4 fixes three contract issues found in v3 itself, including a real
self-contradiction: v3's schema comment excluded trade-type ledger rows from the very "ledger-
only, authoritative" fold that same revision's Section 2.4 was proposing). Sections marked
"(v4)" were corrected this pass; earlier version tags are unchanged since their own revision.**
No production file is changed by this document.
`bot/main.py`, `bot/accounting/reconciliation.py`, `bot/accounting/four_way.py`,
`bot/accounting/store.py`, and `bot/backtest/metrics.py` are all untouched. `logs/HALT`
remains engaged. The profitability gate (`CRYPTO_BOT_REVIEW_2026-09-12`'s review-deadline
decision) is completely independent of everything below and is not affected by it either way.

## 0. What this closes, and what it explicitly does not

This month's offline work (`bot/accounting/asset_movement_analysis.py`, built and hardened
across six same-day review passes, 54 tests) proved — using the account's own real BTC/CAD
and SOL/CAD history — three things production reconciliation cannot currently see:

1. A real BTC deposit (0.00037766 BTC, 2026-06-26T12:43:35Z) explains a shortfall the
   production `engine.causal_order()` correctly refuses to resolve on trades alone.
2. Four real SOL staking-reward ledger entries explain the SOL balance-agreement gap down to
   the exact digit (`0.0000035758`).
3. Trade `TDCRFZ-MWTNB-2NVHO6`'s ledger shows a fee genuinely settled in BTC quantity while
   `observed_trades.fee_currency` reports CAD — a **reporting valuation, not a real cash
   deduction** — and naive P&L math silently discards the fee-consumed quantity's real
   economic cost unless corrected (a conservation bug found and fixed in the offline module
   itself this same day).

None of this is wired into the live reconciliation cycle today. **This is the bounded next
step: a concrete proposal for how it could be, without weakening any existing guarantee** —
not a request to implement it, and not a request to let any of this touch a trading decision.

## 1. The three separations this integration must preserve

### a) Exchange asset balances
Ground truth, unchanged: `ExchangeAdapter.fetch_balance_total(asset)`, already used by
`_reconcile_scope()`'s bootstrap and balance-check paths. This proposal adds no second way to
read a balance and does not touch this function.

### b) Bot-owned inventory — corrected description (v2)
**The first draft of this proposal mischaracterized the current state.** Checked directly:
`_scope_trades()` (`reconciliation.py:492`) calls `store.load_observed_trades(conn, symbol)`
(`store.py:268`), which is a bare `SELECT ... FROM observed_trades WHERE symbol = ?` — **no
join against `trade_fill_links`, no fill-linkage filter at all.** Today's fold is over *every*
observed trade for a symbol, bot-executed or not. There is no existing "bot-owned inventory"
number to preserve as-is; the honest starting point is that this concept doesn't exist yet.

Worse, fill-linkage alone would not be sufficient even if the loader used it: the real BTC
incident is direct proof. `TEYLVF-3GXRC-N6RME4` **is** linked to a bot fill (`fill_id=9` in
`trade_fill_links`) and still consumed inventory that came from an external deposit, not from
any bot-executed BUY. Execution attribution (this trade has a fill row) and inventory
provenance (which lot a sale actually drew from) are different questions — the first doesn't
answer the second.

**Revised design, corrected a second time (v3) — a v2 error found by a further review:** v2's
own fix conflated two genuinely independent questions by proposing to convert every *unlinked*
trade into an unknown-cost-basis deposit/withdrawal. That is wrong in both directions:
- An **unlinked** trade (no `trade_fill_links` row — e.g. a manual BUY placed directly through
  Kraken's own UI, not by the bot) still has a real, exchange-reported `price`/`cost`/`fee_cost`
  — its **cost basis is exactly as known as a bot trade's own**. The only thing actually
  unknown is whether the *bot* decided to make it. Silently downgrading it to "unknown cost"
  would throw away real, trustworthy evidence for no reason.
- Conversely, a **linked** trade can still consume inventory of unresolved provenance — this is
  the literal real BTC incident (`TEYLVF-3GXRC-N6RME4` IS fill-linked and still drew from the
  external deposit's lot). Fill-linkage says something about the SALE event, not about which
  physical units it happened to draw from.

**Three independent axes, tracked separately, never collapsed into one:**
1. **Execution attribution** (per trade) — does a `trade_fill_links` row exist
   (`store.is_ledger_represented`)? Bot-initiated, or not.
2. **Cost-basis availability** (per lot, at acquisition) — is there a real, exchange-reported
   `price`/`cost`/`fee_cost` for this quantity's acquisition? True for **any** real trade
   (linked or not); false only for a deposit, reward, or other externally-credited inflow with
   no recorded acquisition price at all.
3. **Lot provenance at consumption** (per sale, per unit consumed) — when a SELL draws from the
   FIFO lot queue, which lot(s) does it actually draw from, and what is *their* status on axes
   1 and 2?

Concretely: an unlinked manual BUY is fed into the fold with its **real, unmodified**
`cost`/`fee_cost` (axis 2 = known), tagged `bot_attributed=False` (axis 1); a deposit is fed in
with `cost_per_unit=None` (axis 2 = unknown) and is trivially `bot_attributed=False` (axis 1,
there is no trade at all); a bot-linked SELL's `known_qty`/`unknown_qty` split (axis 3) already
correctly reflects *cost-basis* provenance of the consumed units today — extending it to also
carry each consumed unit's `bot_attributed` flag (from whichever lot it came from) gives the
ownership answer directly, entirely independent of whether the *consuming* trade itself was
bot-linked. This is a genuinely new capability `asset_movement_analysis.py` needs (tagging each
`_Lot` with `bot_attributed: bool` alongside its existing `cost_per_unit`, and propagating that
tag through FIFO consumption into `SellAttribution`) — identified here, not yet designed in
full or implemented.

**`unknown_basis_qty_remaining` must not be relabeled "external inventory."** It answers axis 2
only (do we know the acquisition cost) — it is not an ownership statement. A held unit could be
cost-basis-unknown (from a deposit) yet the question "is this bot-owned" doesn't even apply to
it (it was never bought by anyone); a held unit from an unlinked manual BUY is cost-basis-known
yet non-bot-attributed. The integration's summary must present at least three separate figures
per asset — **bot-attributed quantity**, **non-bot-attributed-but-known-cost quantity**, and
**unknown-cost-basis quantity** — never compressing axis 1 and axis 2 into a single label.

`PositionManager` / `LiveExecutor`'s own live-tracked position (the number actually used for
sizing and risk checks) is completely untouched by any of this — this integration is read-only
/ reporting-only, and does not change what those components consider "the position" for
trading purposes.

### c) Known vs. unresolved cost basis and P&L
`asset_movement_analysis.py`'s `PnlAvailability` / `SellAttribution` (with its
`known_qty` / `unknown_qty` / `unmatched_qty` and the same three-way split now also applied to
fee-consumed quantity) already makes this distinction rigorously, offline. Production's
`bot/backtest/metrics.py` has no such concept — it computes a number or the strategy simply
isn't evaluated. **This proposal introduces a new, separate reporting concept
("external-inventory-adjusted P&L availability") that never touches `metrics.py` or any
PF/win-rate gate.** The existing, already-validated profitability floors (PF ≥ 1.2 net of
fees, etc.) keep meaning exactly what they mean today: bot-trade-only performance.

## 2. Proposed architecture (concrete, not implemented)

### 2.1 New schema — additive only, evidence-preserving (v2)
**A single `REAL amount` column, as drafted in v1, loses evidence a real reconciliation needs.**
Kraken's own ledger rows carry, as raw decimal strings: a signed `amount`, a separate `fee`
(itself asset-denominated, per the real `TDCRFZ` finding), a running `balance` after the entry,
and two DIFFERENT identifiers (`id`, the ledger-entry's own key, e.g. `LGBTCK-TWYWU-NDYU7J`; and
`refid`, which for a trade-type entry equals the trade_id, but for a deposit is an internal
Kraken reference — e.g. `FTYl6Qu-zyZCpaCJTTzXa3kWQ8gIM9` — that has **no relationship at all**
to the on-chain txid `fetch_deposits` reports for the SAME real event, `bde604f...c5c6bb`,
confirmed by direct inspection this session). Storing only a rounded float `amount` discards
every one of these, and the `scripts/ledger_reconciliation_audit.py` Decimal-exact reconciliation
this session built would not be reproducible from it.

```sql
CREATE TABLE ledger_movements (
    ledger_id TEXT NOT NULL,          -- Kraken's own ledger-entry id (e.g. "LGBTCK-TWYWU-NDYU7J")
    reference_id TEXT NOT NULL,       -- Kraken's `refid` — for a trade, equals its trade_id;
                                       -- for a deposit/withdrawal/reward, an internal Kraken
                                       -- reference with NO relationship to fetch_deposits' txid
    account_id TEXT NOT NULL,         -- see 2.3 — every row is scoped to a specific account/key
    type TEXT NOT NULL,               -- 'deposit' | 'withdrawal' | 'reward' | 'trade' — trade-type
                                       -- ledger rows are STORED AND COUNTED here on equal footing
                                       -- with every other type (v4 fix — an earlier draft said
                                       -- these were corroboration-only, which would have made the
                                       -- "ledger-only, authoritative" quantity check in 2.4 blind
                                       -- to the dominant source of real balance change: trades)
    asset TEXT NOT NULL,
    amount_raw TEXT NOT NULL,         -- Kraken's raw decimal STRING, never a float — parsed with
                                       -- decimal.Decimal only, exactly as the audit script does
    fee_raw TEXT NOT NULL,            -- always present, '0' when none — same asset as `asset`
    balance_raw TEXT NOT NULL,        -- Kraken's own running balance after this entry
    exchange_timestamp TEXT NOT NULL, -- the settlement/ledger timestamp (see below — NOT
                                       -- necessarily the same instant fetch_deposits reports)
    observed_at TEXT NOT NULL
    -- no `source` column: EVERY row in this table comes from fetch_ledger — see the
    -- corrected dedup discussion below for why deposit/withdrawal-history rows are
    -- never inserted here at all, only into the separate corroboration table.
    , PRIMARY KEY (account_id, ledger_id)
);

CREATE TABLE deposit_withdrawal_history (
    -- Corroboration only — never read by the fold or by run_asset_movement_check's
    -- quantity math. See the dedup discussion below for why this can't safely be
    -- merged with ledger_movements by amount/time matching.
    account_id TEXT NOT NULL,
    source TEXT NOT NULL,             -- 'deposit_history' | 'withdrawal_history'
    external_id TEXT NOT NULL,        -- fetch_deposits'/fetch_withdrawals' own id/txid
    asset TEXT NOT NULL,
    amount_raw TEXT NOT NULL,
    exchange_timestamp TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    PRIMARY KEY (account_id, source, external_id)
);
```

**Cross-representation deduplication — corrected a second time (v3): fuzzy matching is unsafe
even with a single candidate.** v2 proposed matching a `deposit_history` row to a `ledger` row
by same-asset + same-amount (to tick size) + within ±1 hour, and treating a lone match as
confirmed. A further review is right that this is unsafe on its own terms: **a single visible
candidate is not proof of identity.** Two genuinely distinct real deposits of the identical
amount within an hour of each other — unlikely, but not impossible, and exactly the kind of
coincidence this codebase's own discipline elsewhere (`match_legacy_fill`'s exact-conservation
requirement) refuses to paper over — would be silently merged into one, permanently losing a
real movement with no trace it ever happened.

**Corrected design: the ledger (`fetch_ledger`) is the sole source of truth for COUNTED
movements. `fetch_deposits`/`fetch_withdrawals` are never inserted as their own
`ledger_movements` rows at all** — this doesn't just reduce the matching risk, it eliminates
the matching problem: there is only one authoritative event per `(account_id, ledger_id)`,
identified by Kraken's own key, never inferred from amount/time proximity. Deposit/withdrawal-
history records are retained purely as **corroboration**, stored in a separate, clearly-labeled
`deposit_withdrawal_history` table (same raw-decimal-string discipline as 2.1's main table,
its own primary key on `(account_id, source, external_id)`) and used only for cross-checking
narrative (e.g. the BTC deposit's two-timestamp story) — never counted in the fold, never
merged with a ledger row by inference.

**If ledger evidence is unavailable, report incomplete evidence — never invent a ledger ID or
a settlement balance.** When the account's "Query ledger entries" permission is off (as it was
for most of this session) or a `fetch_ledger` call fails, `ledger_movements` simply has no new
rows for that window. `run_asset_movement_check` (2.4) must report this explicitly as
"incomplete evidence — ledger unavailable" rather than falling back to
`deposit_withdrawal_history` as if it were an equivalent-quality substitute; that table lacks
`balance_raw` and, per the paragraph above, cannot be safely counted on its own.

`exchange_timestamp` in the main `ledger_movements` table is therefore unambiguously the
**ledger's own** timestamp — the only timestamp this design ever counts. `fetch_deposits`
reported `2026-06-26T12:43:35Z` for the real BTC deposit; the ledger's own `transaction`-type
row for the identical event says `2026-06-26T13:02:42Z` — confirmed, this session, to be
different real timestamps for the same underlying event, which is exactly why only one of them
(the ledger's) is ever treated as authoritative.

**Reward normalization, made explicit (v1 left this implicit):** a `reward` row's usable
quantity is **`amount_raw - fee_raw`**, not `amount_raw` alone — confirmed this session by
walking Kraken's own running `balance_raw` field across all 4 real SOL staking entries in exact
Decimal arithmetic and finding it matches only when the fee is subtracted (e.g. entry 1:
`0.0000051019 - 0.0000015305 = 0.0000035714`, exactly Kraken's own reported post-entry balance).
`ledger_reconciliation.py` (2.4) must apply this subtraction before constructing the
`LedgerMovement` passed to `analyze_with_asset_movements()`'s `rewards` parameter — the analyzer
itself has no fee concept for rewards and must not be handed the gross figure.

No change to `observed_trades`, `fills`, `trade_fill_links`, or `checkpoints`.

### 2.2 New observation step — split by table, per 2.1's corrected source-of-truth rule (v3)
A new `bot/accounting/ledger_observe.py`, mirroring `live_observe.py`'s own discipline:
- `observe_ledger_entries(exchange, conn, account_id, asset)` — the counted path. Requires
  `fetch_ledger` (i.e. the "Query ledger entries" permission); upserts into `ledger_movements`
  keyed on `(account_id, ledger_id)`, identical re-observation a silent no-op, a **different**
  payload for the same key raises rather than overwrites. If `fetch_ledger` is unavailable or
  fails, this function returns an explicit "ledger unavailable" status — it never falls back to
  the other path below to fill the gap.
- `observe_deposit_withdrawal_history(exchange, conn, account_id, asset)` — the corroboration-
  only path. Calls `fetch_deposits`/`fetch_withdrawals`, upserts into the separate
  `deposit_withdrawal_history` table, same no-op/raise-on-conflict discipline keyed on
  `(account_id, source, external_id)`. Always safe to run regardless of ledger availability,
  since it is never counted.

Both gated behind a new `cfg.accounting.ledger_movements_enabled` flag, default `False` — inert
until explicitly turned on, mirroring `cfg.accounting.enabled`'s own existing pattern.

### 2.3 Base-currency-fee corrections stay human-reviewed, not auto-inferred — now account-scoped (v2)
The cross-currency-fee check built this session (`scripts/ledger_reconciliation_audit.py`)
only ever reports "strongly suggests one real fee... not proven beyond this arithmetic check."
**This proposal does not promote that arithmetic check into an automatic correction.** v1's
mapping was keyed by bare `trade_id` alone — a `trade_id` is only unique *within* a Kraken
account, so a correction keyed this way could silently misapply if this code were ever pointed
at a second account/key. Corrected: scoped by `account_id`, and each entry must cite the
specific ledger record(s) that were the actual evidence, not just a report filename:

```python
# Each entry requires a human to have reviewed the ledger cross-check evidence
# (scripts/ledger_reconciliation_audit.py's own report) before adding it here.
KNOWN_BASE_CURRENCY_FEE_CORRECTIONS: dict[str, dict[str, "FeeCorrection"]] = {
    "kraken:trade_bot_local": {   # account_id — see 2.1's ledger_movements.account_id
        "TDCRFZ-MWTNB-2NVHO6": FeeCorrection(
            qty=0.00000044,
            evidence_ledger_id="LIWJL4-OX4PC-GRQUZZ",      # the BTC-leg ledger row itself
            evidence_cad_leg_ledger_id="LSJAV5-ADXIN-BMVOO6",  # the corroborating CAD-leg row
            reviewed_at="2026-09-21",
            report="logs/ledger_reconciliation_audit_20260921.md",
        ),
    },
}
```

This is a deliberate scope limit, not an oversight: auto-inferring "this fee was really
base-currency-settled" from a same-trade arithmetic match is exactly the class of unproven
inference this whole review chain has been careful never to silently trust.

### 2.4 New reconciliation step — inputs, scope, and numeric authority (v3)
**v1 left `run_asset_movement_check(conn, asset)` with no stated source for a timestamped
closing balance or verified pagination evidence, and reused a symbol-scoped trade loader
without addressing that an asset can span more than one trading pair. v2 fixed the scoping and
proposed a source for each input, but left the history/balance-alignment question underspecified
and silently reused a float-based function while promising Decimal-only arithmetic — both fixed
below.**

`bot/accounting/ledger_reconciliation.py::run_asset_movement_check(conn, exchange, account_id, asset)`:
1. **Trade retrieval is asset-scoped, across every relevant pair, not one hardcoded symbol.**
   Query `observed_trades` for `symbol LIKE '{asset}/%'` (matching
   `scripts/asset_movement_discrepancy_report.py`'s own existing pattern), covering every quote
   currency this account has ever traded that base against — not just the live `UNIVERSE_
   WHITELIST` pair. Every trade retrieved this way is tagged with its execution-attribution
   axis (`bot_attributed = is_ledger_represented(trade_id)`, Section 1(b)) — **an unlinked trade
   is still passed to the attribution analysis with its own real `cost`/`fee_cost` intact, never
   converted into a synthetic deposit/withdrawal.**
2. **Asset reconstruction happens ONCE, never split by quote currency — corrected (v4), and a
   real self-contradiction with point 1 fixed.** v3 said point 1 includes ALL trades (linked or
   not, tagged by attribution) but then this point said to "group the asset-scoped, **fill-
   linked** trades by quote currency" — reintroducing exactly the restriction point 1 had just
   removed. Beyond the wording contradiction, the underlying idea was also structurally wrong:
   BTC bought with CAD and later sold for USDT is **the same BTC inventory** — splitting into
   independent per-quote-currency folds would either sever that connection (a CAD-bought lot
   becomes invisible to a USDT-quoted sale that actually consumed it) or double-count a shared
   deposit across groups to make each group's own balance close. Quantity and lot-provenance
   reconstruction must happen **once**, across every trade for the asset regardless of quote
   currency:
   - This is already true, structurally, for the authoritative Decimal quantity check (point 4)
     once point 1's fix to the schema takes effect: ledger `trade`-type rows carry only the
     ASSET-side signed amount, with no quote-currency dimension at all — a BTC-side ledger row
     looks identical whether the trade was against CAD or USDT. The Decimal fold is naturally
     single, asset-wide, and quote-currency-agnostic; there was never really a need to split it.
   - The **attribution analysis** (`analyze_with_asset_movements()`) is where the real gap is:
     its own `_validate_assets_and_currencies` requires a single quote currency across the whole
     call, because its P&L math cannot mix CAD and USDT dollar amounts. That constraint is
     correct for P&L, but the SAME call also does lot-provenance tracing (`known_qty`/
     `unknown_qty`/ownership), and that tracing needs the FULL cross-currency trade sequence to
     be correct — exactly the connection a per-quote-currency split would sever. **This reveals
     a genuine capability gap, not something this proposal claims to have solved**: the
     analyzer as it exists today cannot correctly trace lot provenance across a mixed-quote-
     currency history at all. Flagged here as a further required extension (alongside the
     `bot_attributed` tagging and unlinked-trade handling from Section 1(b)): the analyzer needs
     a mode that reconstructs lot provenance across ALL trades in one pass regardless of quote
     currency, while computing P&L only for the portions where quote currency stays single
     end-to-end, and marking any sale whose consumed lots span more than one quote currency
     (without conversion evidence) as P&L-unavailable specifically for that reason — not
     attempted here, not yet designed in full.
3. **History/balance-snapshot boundary and opening balance — corrected further (v4).** v3 fixed
   using a separate live balance call for the primary quantity check, but left the opening
   balance, per-transition verification, and tie-break/overlap rules unspecified. All four
   addressed:
   - **Opening balance is never assumed to be zero.** The earliest entry `reconcile_quantity_
     decimal` sees for an asset is only the earliest entry *this account's ledger currently
     reports* — pagination completeness (count matches) proves nothing was missed from what
     Kraken currently returns, but says nothing about whether Kraken's own retention window, or
     the query parameters used, could exclude a genuinely earlier balance the account started
     with. The check requires an explicit opening checkpoint: either (a) a verified zero — the
     earliest entry's own `balance_raw` minus its `amount`/`fee` equals exactly zero AND there is
     documented, human-confirmed evidence this really is the asset's first-ever activity on the
     account (e.g. a dated note cross-referencing the account's own creation/funding date), or
     (b) absent that evidence, the check proceeds but is labeled explicitly
     `opening_balance_verified=False` and its overall status is never reported as a clean,
     unqualified pass on that basis alone — a "reconciles from an unverified zero" result is a
     materially weaker claim than a verified one and must read as such.
   - **Every transition is checked, not only the final total.** `reconcile_quantity_decimal`
     performs the exact per-row walk `scripts/ledger_reconciliation_audit.py` already does
     (`running_total_after_this_row == this_row.balance_raw`, checked at every row) — a chain
     with two offsetting errors that happen to cancel by the last entry must still fail, not
     pass because the final number coincidentally matches.
   - **Equal timestamps get a stated, deterministic tie-break.** Two ledger entries sharing an
     identical `exchange_timestamp` (Kraken can process more than one event per second) are
     ordered by `ledger_id` as a secondary sort key — arbitrary but deterministic and
     reproducible, the same kind of stated-not-proven convention `_chronological_order`'s own
     tie-break already uses in the offline analyzer, disclosed the same way.
   - **Overlapping retrieval windows are handled by re-walking the full persisted set, not just
     new rows, using the existing safety-margin discipline.** `observe_ledger_entries`'s `since`
     watermark should never advance past `now - safety_margin_ms`, mirroring
     `engine.compute_safe_watermark`'s existing rationale exactly (a real event can be reported
     by the exchange with a short delay after its own timestamp) — an overlapping re-fetch is
     therefore expected, not an edge case, and the idempotent upsert (2.2) already makes
     re-inserting an already-seen `ledger_id` a no-op. `reconcile_quantity_decimal` itself always
     re-walks the FULL persisted `ledger_movements` set for the asset on each run, never only
     the rows fetched in the current cycle — so a late-arriving entry with an earlier effective
     timestamp than a previously-identified "closing entry" is correctly incorporated into the
     walk the next time it runs, not silently missed.
   - **Two statuses, published separately, and an inconclusive currentness check blocks the
     overall pass — restated explicitly per this review.** `chain_consistent` (every transition
     checks out, subject to the opening-balance caveat above) and `current_as_of_now` (`True` /
     `False` / `inconclusive`, from the bounded-retry check below) are reported as two distinct
     fields, never collapsed into one. A consistent chain is not proof the ledger is current — a
     real movement could exist that simply hasn't been fetched yet. **The overall
     `run_asset_movement_check` verdict is a pass only when BOTH `chain_consistent=True` AND
     `current_as_of_now=True`; if `current_as_of_now` is `inconclusive`, the overall verdict is
     never a pass, regardless of how clean `chain_consistent` is.**
   - **The bounded retry for currentness**: compares the last ledger entry's `balance_raw`
     against a fresh `fetch_balance_total(asset)` call. Disagreement doesn't necessarily mean a
     real problem — it may simply mean new activity happened after the last fetched entry.
     Resolution: re-run the pagination-proof fetch for entries after the previous last one; if
     new entries appear, re-walk the extended chain (per-transition, as above) and repeat the
     comparison; retry at most `N` times (proposed `N=3`, tunable). If the two still disagree
     after `N` attempts, `current_as_of_now=inconclusive` — never silently folded into `True` or
     `False`. This loop is not proven to always converge (continuous, faster-than-fetch account
     activity could exhaust `N`) — an accepted, disclosed limit, not something this design claims
     to solve.
4. **Decimal/reuse contradiction — resolved (v3).** v2 promised "Decimal-only arithmetic,
   matching the audit script's discipline" for storage (2.1) while also proposing to reuse
   `analyze_with_asset_movements()` **unmodified** — a function whose entire numeric contract is
   `float`, operating on `observed_trades`' own float-typed `price`/`amount`/`cost`/`fee_cost`
   columns. Those two claims cannot both be true of the same result. Resolved by splitting the
   check into two components with two clearly labeled, different numeric contracts, not one:
   - **The authoritative quantity reconciliation** is a NEW function, `ledger_reconciliation.
     reconcile_quantity_decimal(ledger_movements_rows)` — built the same way `scripts/
     ledger_reconciliation_audit.py`'s own per-row walk already works: every `amount_raw`/
     `fee_raw`/`balance_raw` parsed as `decimal.Decimal`, every transition checked (point 3),
     against a stated or explicitly-unverified opening balance (point 3). It returns
     `chain_consistent` and, via the bounded-retry step, `current_as_of_now` — the two
     separately-published statuses point 3 defines — and does not depend on `observed_trades`'
     float columns at all, only on the ledger's own Decimal-exact figures.
   - **`asset_movement_analysis.analyze_with_asset_movements()`, called verbatim and
     unmodified, is relabeled a separately-labeled "attribution and cost-basis analysis"** —
     valuable for its `SellAttribution` / ownership / `PnlAvailability` breakdown, but its own
     `final_qty`/numeric outputs are float-based and explicitly NOT presented as the
     authoritative reconciliation figure. Any float-precision noise at this boundary (e.g. a
     result of `4e-16` where the Decimal check shows exact `0`) is documented as expected
     float-arithmetic behavior, not a discrepancy to chase. Upgrading the analyzer's own numeric
     contract to Decimal is a legitimate future improvement — deliberately out of scope here,
     since it would mean re-deriving and re-testing its entire fold, not a small addition.
5. Looks up `base_currency_fee_qty` from `KNOWN_BASE_CURRENCY_FEE_CORRECTIONS[account_id]`
   (2.3) — never computed live — for use by the attribution analysis (point 4's second
   component) only; the Decimal quantity check (point 4's first component) needs no fee
   correction lookup at all, since it already walks the ledger's own `fee_raw` field directly.
6. Writes the result to `logs/asset_movement_status_<ASSET>.json` **under the same
   `_STATE_LOG_DIR` shadow-mode redirection `bot/main.py` already applies to every other
   accounting artifact** (i.e. `logs/shadow/asset_movement_status_<ASSET>.json` when
   `_SHADOW_MODE` is true) — using the same two-phase (`in_progress` marker, then outcome)
   write discipline `bot/accounting/cycle_status.py` already established. Never written into
   `checkpoints` or `observed_trades`, and **never read by `_accounting_enabled`'s existing
   BUY-blocking check.** Purely observational in this pass; gating BUYs on it is explicitly a
   separate, later proposal this one does not make. **This distinction stays explicit
   throughout: this observer may fully explain a balance while the existing trading gate
   (HALT + the unmet profitability floor) remains blocked regardless — the two are
   independent, and nothing in this integration lifts or bears on the latter.**
7. Runs on the SAME `reconcile_interval_s` cadence as the existing accounting block, as an
   ADDITIONAL step after `run_cycle()` — its own try/except boundary, so a failure here can
   never affect the existing, already-validated accounting cycle's own success/failure.

### 2.5 Presentation — corrected numbers, correct location (v2, refined v3)
**v1's proposed dashboard numbers were misleading in two ways, and its location was wrong for
where this actually runs first.**

1. **Cumulative flow was proposed where a current-holdings figure was needed.** "Net deposits +
   rewards − withdrawals" is a *flow* total across all of history — it does not mean "how much
   externally-sourced inventory is still held." The real BTC case proves this directly: the
   deposit was 0.00037766 BTC, but almost all of it was consumed by a subsequent sell within a
   day — the actual *remaining* externally-sourced inventory is close to zero, not the deposit's
   full amount. The correct stock figure, per Section 1(b)'s v3 correction, is the currently-held
   quantity whose lots trace to a deposit or reward specifically — **not** every unlinked-trade
   lot, since an unlinked trade retains its own real, known cost basis and must not be counted
   as "unknown-cost external inventory." (This means `unknown_basis_qty_remaining` as it exists
   in the analyzer *today* is close but not exactly this figure once the unlinked-trade handling
   from Section 1(b) is implemented — it would need to report unknown-cost-basis quantity only,
   which after that fix is exactly deposit/reward-sourced holdings, nothing more.) The cumulative
   flow total is still worth showing, but only ever labeled and positioned as a separate,
   historical figure — never in the same visual slot as "currently held."
2. **Neither `unmatched_qty` nor the analyzer's own `BalanceAgreement.diff` is the authoritative
   residual — `reconcile_quantity_decimal()`'s `chain_consistent` / `current_as_of_now` pair is
   (v3, terms aligned v4).** `unmatched_qty` (plus `fee_consumed_unmatched_qty`) reports one
   *source* of unexplained quantity from the float-based attribution analysis. The analyzer's
   own `BalanceAgreement.diff` is a broader, still-float-based measure of the SAME analysis's
   disagreement with a supplied balance. Per 2.4's resolved design, the actual authoritative
   picture is `reconcile_quantity_decimal()`'s two published statuses — `chain_consistent` (every
   ledger transition checks out, subject to its own opening-balance disclosure) and
   `current_as_of_now` (`True`/`False`/`inconclusive`) — shown as the primary result, with the
   analyzer's `unmatched_qty`/`BalanceAgreement.diff` offered only as supporting, float-based
   diagnostic detail from the separate attribution analysis. Presentation must never show a
   single combined "reconciled" checkmark when `current_as_of_now` is `inconclusive`, even if
   `chain_consistent` is clean.
3. **The unified dashboard is not where this validates first.** `bot/main.py` already sets
   `_ud_interval = 0` (disabled) whenever `_SHADOW_MODE` is true — confirmed by reading the
   current code — and Section 4's shadow-mode validation run is exactly the condition where
   that applies. A `unified_dashboard.py` card is real future work for once this reaches live
   production mode, but it will not render at all during the shadow acceptance run this
   proposal requires first. `logs/asset_movement_status_<ASSET>.json` (2.4, point 6) is
   therefore the actual interface during validation — written under `_STATE_LOG_DIR` exactly
   like every other accounting artifact in shadow mode — and the acceptance fixtures in
   Section 3 are checked by reading that file directly, not by looking at a dashboard.

## 3. Acceptance fixtures (the plan; not yet built) — expanded a third time (v4)

Using the evidence already captured and version-controlled in this session's own artifacts:

- **BTC fixture** — the real BTC ledger chain (10 `trade`-type entries + the 1 `deposit`-type
  entry, `ledger_id="LGBTCK-TWYWU-NDYU7J"`, 0.00037766 BTC — **trade entries included in the
  Decimal fold per the v4 fix**, not treated as corroboration-only) + the reviewed
  `TDCRFZ-MWTNB-2NVHO6` correction (2.3) for the separate attribution analysis. Expected:
  `causal_order()` on `observed_trades` alone still returns `None` (unchanged — this fixture
  must prove the EXISTING safety behavior is untouched); `reconcile_quantity_decimal()` reports
  `chain_consistent=True` (every one of the 11 transitions checks out, not just the final total)
  and, given a matching fresh balance read, `current_as_of_now=True`.
- **SOL fixture** — the real SOL ledger chain (2 `trade`-type entries + 4 `staking`-type
  entries, each reward normalized per 2.1's `amount_raw - fee_raw` rule). Expected:
  `reconcile_quantity_decimal()` reports `chain_consistent=True`, `current_as_of_now=True`
  against the real closing balance `0.0000035758`; the separate attribution analysis reports
  `pnl_availability.available=True` (no unknown-basis sale in this round trip).
- **Known-cost manual (unlinked) BUY fixture (Section 1(b))** — a synthetic BUY trade with a
  real `price`/`cost`/`fee_cost` and NO `trade_fill_links` row. Expected: the attribution
  analysis folds it with its own real cost basis intact (`cost_basis: known`), tagged
  `bot_attributed=False` — proving an unlinked trade is never downgraded to unknown-cost.
- **Ownership-unresolved fixture (Section 1(b))** — the real `TEYLVF-3GXRC-N6RME4` sale, which
  IS `trade_fill_links`-linked, consuming a lot whose provenance is the external deposit.
  Expected: the consumed quantity's `bot_attributed` flag (from the DEPOSIT lot it actually drew
  from, not from the consuming trade's own link status) is `False`.
- **Two-equal-sized-deposits fixture (Section 2.1)** — two GENUINELY DISTINCT real-shaped ledger
  entries (different `ledger_id`s, same asset, identical `amount_raw`, within the same hour).
  Expected: BOTH are counted as separate `ledger_movements` rows and BOTH contribute to the
  chain walk — proving the ledger-id-keyed design cannot merge two real, distinct events the way
  an amount/time fuzzy match could.
- **Ledger-unavailable fixture (Section 2.1)** — `fetch_ledger` raising (simulating the
  permission being disabled). Expected: `ledger_movements` gains no rows, and
  `run_asset_movement_check` reports "incomplete evidence — ledger unavailable" — never a
  fallback to `deposit_withdrawal_history`, never a fabricated `ledger_id` or `balance_raw`.
- **Unverified-opening-balance fixture (new, Section 2.4 point 3, v4)** — a synthetic chain
  whose earliest entry's own arithmetic implies a zero start, but with no corroborating
  inception evidence supplied. Expected: `reconcile_quantity_decimal()` proceeds but reports
  `opening_balance_verified=False`, and the overall `run_asset_movement_check` status is NOT
  presented as an unqualified clean pass on that basis alone.
- **Per-transition-not-just-total fixture (new, Section 2.4 point 3, v4)** — a synthetic chain
  with two offsetting errors (e.g. one entry's `balance_raw` overstated, a later one understated
  by the same amount) such that the FINAL total happens to match. Expected:
  `chain_consistent=False`, with the specific failing row(s) identified — proving the walk
  checks every transition, not only whether the last number lines up.
- **Equal-timestamp tie-break fixture (new, Section 2.4 point 3, v4)** — two synthetic ledger
  entries sharing an identical `exchange_timestamp`. Expected: the walk orders them
  deterministically by `ledger_id` and produces the same result on repeated runs — proving the
  tie-break is a stated, reproducible convention, not accidental input-order dependence.
- **Overlapping-retrieval-window fixture (new, Section 2.4 point 3, v4)** — `observe_ledger_
  entries` run twice with deliberately overlapping `since` windows (mirroring
  `compute_safe_watermark`'s own safety-margin rationale). Expected: the second run's re-fetch
  of already-seen `ledger_id`s is a no-op; a genuinely late-arriving entry with an earlier
  effective timestamp than a previously-assumed "closing entry" is correctly incorporated
  because `reconcile_quantity_decimal` always re-walks the FULL persisted set, not just newly
  fetched rows.
- **Movement-between-history-and-balance-reads fixture (Section 2.4 point 3)** — a paginated
  fetch completes with a closing ledger entry at balance X; a new ledger entry is then injected
  (simulating real activity in the gap) before the currentness check's fresh balance read runs,
  showing X+delta. Expected: the bounded retry converges within `N` attempts here — and a SECOND
  variant where entries keep arriving faster than the retry budget allows must report
  `current_as_of_now=inconclusive`, which per point 3's rule must ALSO make the overall verdict
  not-a-pass even though `chain_consistent=True` throughout.
- **Precision-loss-at-the-analyzer-boundary fixture (Section 2.4 point 4)** — the same real BTC
  evidence run through BOTH components: `reconcile_quantity_decimal()` must show an exact
  `Decimal` zero diff, while the separately-labeled attribution analysis
  (`analyze_with_asset_movements()`, float-based) is allowed to show a nonzero
  float-precision-scale residual (e.g. `abs(final_qty) < 1e-9`) without that being treated as a
  contradiction — proving the two components' outputs have correctly different numeric
  authority.
- **Cross-currency lot-continuity fixture (new, Section 2.4 point 2, v4)** — a synthetic BUY
  against CAD followed by a SELL of the same units against USDT. Expected:
  `reconcile_quantity_decimal()` (ledger-only, quote-currency-agnostic by construction) reflects
  the correct single continuous position, unaffected; the attribution analysis, run against the
  full cross-currency trade sequence, must NOT silently sever the lot connection or duplicate a
  shared deposit — and, per point 2's disclosed capability gap, the analyzer as it exists today
  is expected to either report this specific sale's P&L as unavailable (mixed-currency-cost
  lot) or to require the not-yet-designed extension described in point 2 before this fixture can
  fully pass; this fixture's job is to prove the gap is caught and reported, not silently
  mishandled, until that extension exists.
- **Restart fixture** — run `run_asset_movement_check` once, terminate the process, restart it
  against the SAME sqlite file, run again. Expected: byte-identical results from both
  components, zero new rows inserted into `ledger_movements` for already-seen
  `(account_id, ledger_id)` keys (relies on the primary-key upsert from 2.1/2.2, already proven
  idempotent at the pure-function level by the offline module's own
  `test_repeated_calls_are_reproducible_a_restart_replaying_evidence_is_safe`).
- **Duplicate-event fixture** — invoke `observe_ledger_entries` twice with overlapping windows
  (simulating two overlapping polls). Expected: the second call's upsert of an already-seen
  `(account_id, ledger_id)` with an **identical** payload is a silent no-op; a **conflicting**
  payload for the same key (defensive case — should never happen from a real exchange) raises
  rather than silently overwriting, exactly mirroring `asset_movement_analysis._dedup_by_id`'s
  existing, tested contract.

## 4. Staged rollout — shadow first, always

1. Build `ledger_movements` + `ledger_observe.py` + `ledger_reconciliation.py` +
   `known_fee_corrections.py` as new, inert files — **no call site added to `bot/main.py` in
   this step.**
2. Point them at `logs/shadow/trades.db` — the SAME isolated database this session's shadow-mode
   work already established (see `deploy/PAPER_SHADOW_RUNBOOK.md`) — and run them manually via a
   standalone script, in the same style as `scripts/ledger_reconciliation_audit.py`, against
   real shadow-mode account activity across at least one real restart. This is the load-bearing
   validation step: it must demonstrate clean, repeatable, restart-safe results against a real
   (if isolated) account before anything touches the production path. Per 2.5, this validation
   is read from `logs/shadow/asset_movement_status_<ASSET>.json` directly — the unified
   dashboard is disabled in shadow mode and will show nothing during this step.
3. Only after that shadow run is clean: wire `run_asset_movement_check()` into `bot/main.py`'s
   existing accounting block, still behind its own off-by-default flag, still not read by
   `_accounting_enabled`'s BUY-blocking logic.
4. A **separate, later proposal** — not this one — would be required before this check is
   ever allowed to influence a trading decision (BUY-blocking, sizing, or the profitability
   gate). This document does not request that, and building it is out of scope here.

## 5. Explicitly out of scope

- No automatic inference of base-currency-fee corrections from live arithmetic — human-reviewed
  only (2.3).
- No change to `bot/backtest/metrics.py`, any PF/win-rate gate, or the review-deadline decision.
- No change to `PositionManager` / `LiveExecutor`'s own live position tracking or sizing.
- No BUY-blocking change of any kind.
- No production file touched by writing this proposal. `logs/HALT` untouched. No live order,
  no state mutation, no git commit made in producing this document.

**Standing distinction, restated once more because it must never blur:** even in a future
where this observer fully and cleanly explains every BTC/SOL balance, that is a statement
about *evidence*, not about *permission to trade*. `logs/HALT` and the unmet net-of-fees
profitability floor (`CLAUDE.md`'s review-deadline decision) are the trading gate, and they
stay independent of this integration's result in every version of this proposal, including
after full production rollout.
