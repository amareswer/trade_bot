# Crypto bot — execution accounting design (revision 2, 2026-09-19)

Revises the original version of this document after
`CRYPTO_BOT_EXECUTION_ACCOUNTING_DESIGN_REVIEW_2026-09-19.md` found six blocking gaps in it
(checkpoint-race soundness, conflated recovery obligations, ordering/correction handling,
incorrect CCXT retrieval facts, incomplete accounting scope, and an under-specified blocked
state). **No code was changed to produce this revision.** `logs/HALT` is unchanged — still
engaged since 2026-09-12. No live exchange calls were made; every CCXT claim below was
re-verified directly against the installed `ccxt==4.5.56` source in this repo's `.venv`
(`inspect.getsource`), not assumed or carried over from the prior draft. Where something
cannot be settled from source alone (Kraken ledger `refid` semantics, whether a trade's own
fee is ever revised post-hoc, the live API key's actually-granted permission set), §11 says so
explicitly rather than asserting it.

This revision does not re-litigate what the review confirmed was right (the general direction
of per-execution evidence, canonical order ownership, explicit blocking, and joint
verification) — it replaces every part the review found unsound.

---

## 1. Corrected CCXT/Kraken retrieval facts (source-verified)

Read directly from `inspect.getsource(ccxt.kraken.fetch_my_trades)`,
`ccxt.kraken.fetch_order_trades`, `ccxt.kraken.fetch_ledger`, and `ccxt.kraken.parse_trade` in
the installed package:

- **`fetch_my_trades(symbol, since, limit, params)`** calls `privatePostTradesHistory`. Kraken
  returns `result.trades` as a **dict keyed by the trade's own Kraken transaction id**
  (e.g. `"GJ3NYQ-XJRTF-THZABF"`). CCXT injects that dict key as `trade['id']` *before*
  `parse_trade` runs (`trades[ids[i]]['id'] = ids[i]`). `parse_trade`'s private-record branch
  then does `id = self.safe_string_2(trade, 'id', 'postxid')` — since `'id'` is always already
  present after that injection, the id ccxt returns is **that Kraken trade txid**, not the
  separate numeric `trade_id` field some raw responses also carry, and the `postxid` fallback
  is effectively dead code on this path. (The prior draft said the opposite — "falls back to
  `postxid`" — that was wrong; corrected here.) This txid *is* a genuine unique per-trade
  identifier suitable for idempotency; the caveat is about which field it comes from, not
  whether one exists.
- **`fetch_order_trades(id, symbol, since, limit, params)`** is labeled `emulated` in
  capabilities, but its `id` positional argument is **never referenced in the function body**.
  It requires `params['trades']` — an array of *already-known* trade id strings — and batches
  them (20 per call, `options.fetchOrderTrades.batchSize`) through **`privatePostQueryTrades`**,
  a different Kraken endpoint from `TradesHistory`. It cannot answer "give me order X's
  trades" from the order id alone. **Consequence for this design: `fetch_order_trades()` is not
  usable as the retrieval mechanism.** The only viable path is `fetch_my_trades(symbol,
  since=...)` (account/symbol-scoped), with each returned trade grouped client-side by its own
  `order` field (ccxt's unified name for Kraken's `ordertxid`, confirmed in `parse_trade`:
  `orderId = self.safe_string(trade, 'ordertxid')`).
- **No automatic pagination.** `fetch_my_trades` converts `since` (ms) to Kraken's `start`
  (unix seconds, **exclusive** per Kraken's own documented semantics), calls
  `privatePostTradesHistory` exactly **once**, and passes whatever that single response
  contains to `parse_trades(..., since, limit)` — `limit` only trims the already-fetched batch,
  it does not tell Kraken to return more or fewer records, and does not trigger a second call.
  Kraken's own response includes a `count` (total matching records) and accepts an `ofs`
  request parameter for paging through it (visible as a commented-out key in the ccxt source);
  ccxt's unified wrapper does not drive that loop. **Consequence: any caller that needs
  guaranteed-complete coverage (a full historical backfill, or a window that could plausibly
  exceed one page) must implement its own `ofs`-incrementing loop against `params`, comparing
  accumulated records fetched against the response's own `count` before considering the window
  covered.** The prior draft's "low trade frequency means pagination isn't a practical
  concern" was true only for steady-state incremental syncs, not for the one-time migration
  backfill in §5.4, which walks full account history — that claim is withdrawn as stated.
- **`fetchLedger`/`fetchLedgerEntry` are supported** (`ccxt.kraken().has['fetchLedger'] is
  True`), calling `privatePostLedgers` (Kraken's `/Ledgers` endpoint). Each entry carries `id`
  (the Kraken ledger-entry key), `refid`, `time`, `type` (`trade` / `deposit` / `withdrawal` /
  `transfer` / `margin` / `adjustment` / ...), `asset`, `amount` (signed delta), `fee`, and —
  critically — **`balance`: the resulting balance of that asset immediately after this specific
  entry.** This is a fundamentally stronger primitive than `fetch_balance()` +
  `fetch_my_trades()` read separately: each ledger row states its own post-event balance, so
  there is no race window to reason about between two independent REST calls. See §3 for how
  this changes the design, and §11 for what is not yet confirmed about it (particularly whether
  the live API key currently has the required permission — `reconcile_ledger.py`'s own report
  text already notes it does not, per §11).

---

## 2. The checkpoint-race problem (review §1) — resolved as an accounting identity, not an inclusion proof

The review's counterexample is correct and decisive: `fetch_balance()` and `fetch_my_trades()`
are two independent REST calls with no atomicity. A trade executing between them can be
"in the trades response but not yet in the balance response" (mark-as-covered is wrong) or "in
the balance response but not yet visible in the trades response" (the complementary failure).
**No single pair of reads can prove inclusion.** The fix is to stop trying to prove inclusion
per trade and instead maintain a running **accounting identity**, verified every checkpoint:

```
previous_confirmed_balance + Σ(known trade deltas since previous checkpoint)
   + Σ(known external movements since previous checkpoint, §7.2)
   == freshly read balance
                                                              (within a market-precision tolerance, §6.3)
```

- If the identity holds: every currently-known trade (and external movement) is confirmed
  explained by this balance read. Advance the checkpoint; mark those trades
  `balance_confirmed`.
- If it does **not** hold: there is a residual the currently-known trade/movement set does not
  explain — exactly the review's counterexample (a trade executed in the race window, not yet
  visible to `fetch_my_trades`). **Do not guess which direction it resolved.** Leave the
  affected symbol/asset `reconciliation_blocked` (§8) and retry next cycle. A trade that was
  genuinely missed becomes visible on a subsequent `fetch_my_trades` call (Kraken's
  `TradesHistory` is a durable, append-only history — nothing disappears, it only becomes
  visible later), at which point the identity re-checks and resolves. This never mis-attributes
  a trade to the wrong side of the boundary because it never attributes an unexplained residual
  to anything — it stays visibly unexplained until it either resolves or is investigated.

This works under two explicit assumptions, stated so they can be tested adversarially rather
than assumed sound:

1. **Append-only, immutable trade history** — a trade, once it appears via `fetch_my_trades`,
   never disappears or changes economic identity (id/side/price/amount) on a later read. (Fee
   *may* be a partial exception — see §6.2.)
2. **No balance-affecting event exists that is invisible to both `fetch_my_trades` and the
   §7.2 external-movement checks** (deposits/withdrawals). This assumption is exactly what
   Tier A (below) removes the need for.

### Two tiers

**Tier A — preferred, requires the Kraken API key's "Query Ledger Entries" permission.**
`fetch_ledger()` returns each balance-affecting event *with its own resulting balance already
attached* — no race window, no separate identity check needed; a ledger entry's `balance`
field is authoritative by construction for that asset as of that entry. `refid`/`type=trade`
entries are cross-referenced against `fetch_my_trades` for order attribution and reporting
detail. **This tier is not yet confirmed usable**: `reconcile_ledger.py`'s own existing report
text (`reconcile_ledger.py:351`-`353`) already states "requires 'Query Ledger Entries'
permission — currently not granted to the API key," and `CLAUDE.md`'s documented Kraken key
setup does not list it among the enabled permissions. **This is a human decision, not a code
change**: enabling it on the live key is a precondition for Tier A and should be evaluated
(and explicitly accepted or declined) before implementation, not assumed.

**Tier B — fallback, usable under current permissions.** The accounting-identity protocol
above, using `fetch_balance()` + `fetch_my_trades()` + `fetch_deposits()`/`fetch_withdrawals()`
(§7.2). Weaker than Tier A (it can only detect "something is unexplained," never distinguish
*why* without further reads), but sound under the two stated assumptions, and it fails closed
(blocks) rather than fails silently whenever those assumptions are not met. **This is the tier
the design should assume as the baseline for implementation**, with Tier A as a strict
upgrade if the permission question is resolved affirmatively.

---

## 3. Durable progress model — per trade, not per order, with distinct obligations

The review is correct that one `applied_trade_ids` set conflated at least four different
facts. Restated as genuinely separate, independently-recoverable obligations, tracked **per
trade id** (an order aggregates a set of these, it is no longer the unit of record):

| Obligation | What it means | Where it lives | How it's recovered after a crash |
|---|---|---|---|
| **Observed** | The trade's immutable execution payload (id, order id, symbol, side, price, amount, cost, fee, fee currency, exchange timestamp) has been fetched and durably captured | New SQLite table `observed_trades` (§3.1) | Re-fetch via `fetch_my_trades`; already-observed rows are a no-op (trade id is the primary key) |
| **Balance-confirmed** | This trade's economic effect is covered by a checkpoint that passed the §2 identity check (or, Tier A, is directly attached to a `fetch_ledger` entry) | `observed_trades.checkpoint_id` (nullable) | Re-run the §2 identity check; a trade already `observed` but not yet `balance_confirmed` is exactly what a restart should re-attempt, never re-derive from a scalar cap |
| **Ledger-written** | A `fills` row exists for this trade | `observed_trades.ledger_written_at` (nullable), set in the **same SQLite transaction** that inserts the `fills` row | If `ledger_written_at IS NULL` for an `observed` row, insert `fills` (keyed `exec_key=trade_id`) and set it — one atomic `with conn:` block closes the "SQLite plus a separate JSON write is not one transaction" gap the review raised, because both writes now live in the *same* SQLite database |
| **Delivered** (to `PositionManager`/state machine/capital pool) | In-memory trading state reflects this trade | **Not persisted at all** | `PositionManager` is rebuilt at every startup by folding `observed_trades` in timestamp order (§3.2) — "delivered" becomes a property that is always recomputed, never a durable bit that itself needs crash recovery |

This directly answers the review's "distinct obligations" demand without multiplying flags
unnecessarily: three of the four obligations get a durable marker; the fourth is designed away
by making the in-memory consumer purely derived, which was already this document's original
stated intent for `PositionManager` (§2.4 of the prior draft) — it just wasn't followed through
into the recovery mechanics.

### 3.1 Schema

```sql
CREATE TABLE observed_trades (
    trade_id            TEXT PRIMARY KEY,   -- exchange trade id (Kraken TradesHistory key)
    order_id             TEXT NOT NULL,      -- exchange order id (Kraken ordertxid)
    symbol                TEXT NOT NULL,
    side                  TEXT NOT NULL,
    price                 REAL NOT NULL,
    amount                REAL NOT NULL,
    cost                  REAL NOT NULL,
    fee_cost              REAL NOT NULL,
    fee_currency          TEXT NOT NULL,
    exchange_timestamp    TEXT NOT NULL,     -- ISO, from the trade's own `time`/`timestamp`
    observed_at           TEXT NOT NULL,     -- ISO, wall-clock when THIS process first saw it
    checkpoint_id         TEXT,              -- NULL until balance-confirmed (§2)
    ledger_written_at     TEXT               -- NULL until the matching `fills` row is committed
)
```

Lives in `trades.db`, alongside `fills`/`fee_adjustments` — one database, one transactional
boundary for the two obligations that must be atomic together (ledger-written + the `fills`
insert). The existing per-symbol JSON state file keeps only what is legitimately
process-local/ephemeral: `last_synced_cursor` per order (the `since`/`ofs` position for the
next `fetch_my_trades` call) and a short-lived in-flight submission record — never a second
copy of trade economics.

### 3.2 PositionManager reconstruction (replaces "delivered" as a persisted flag)

At startup, after `_sync_position`'s existing external-holdings/opening-balance determination
(§7.1) establishes the bot-managed starting quantity, `PositionManager` folds every
`observed_trades` row *for that symbol newer than the opening baseline's own cutoff* in
`(exchange_timestamp, trade_id)` order (§6.1 for the tie rule). This makes "did PositionManager
already see this trade" not a question that needs its own answer — it is recomputed from
scratch every time, which is only affordable because `observed_trades` is a small,
symbol-scoped table at this bot's trade frequency; if that ever stops being true, an
explicit snapshot-plus-replay-from-snapshot scheme would be the next step, not attempted here.

### 3.3 Migration / bootstrap (review §2, last point)

Existing `fills` rows carry synthetic UUID `exec_key`s (`bot/execution/executor.py:86`,
`exec_key: str = field(default_factory=lambda: str(uuid.uuid4()))`), not trade ids — introducing
`observed_trades` must not re-insert the same historical economics under the new scheme.
Bootstrap procedure:

1. Backfill `observed_trades` from a full-history `fetch_my_trades` pull (paginated per §1),
   for every symbol ever traded.
2. For each fetched trade, attempt to match it to an existing `fills` row using the *same*
   timestamp/side/symbol/amount proximity matching `reconcile_ledger.py` already implements
   (`reconcile_ledger.py:155`-`177`) — reuse that function rather than writing a second matcher.
3. A match: insert the `observed_trades` row with `ledger_written_at` set to the **existing**
   row's timestamp (linking backward), and do **not** insert a new `fills` row — the historical
   economics are already recorded once.
4. No match (an orphan Kraken trade, same category `reconcile_ledger.py` already calls out):
   insert `observed_trades` **and** a fresh `fills` row together, exactly like a live
   ledger-write, with a clear `source` marker distinguishing a migration backfill from a live
   fill.
5. This step runs once, offline, against a copy or with the live bot stopped — it is
   implementation work for a future pass, not something this document performs.

---

## 4. Ordering, ties, and late corrections (review §3)

- **`live_comparison.py:83`'s `ORDER BY id`** assumes insertion order equals economic order —
  false the moment a delayed-visibility historical trade is discovered after a later trade was
  already journaled (exactly PASS-10's original finding, now generalized). **Required
  companion change** (not made here, flagged for the implementation pass): every reader that
  currently relies on `fills` insertion order for chronology (`live_comparison.py`'s
  `_compute_live_metrics`, and any other consumer found at implementation time) must sort by
  `(timestamp, id)` — or, once `observed_trades` exists, by `(exchange_timestamp, trade_id)` —
  not by SQLite's autoincrement id.
- **Same-timestamp ties**: Kraken's trade `time` field carries sub-second precision (observed
  in the installed ccxt docstring example: `1710429248.3052235`), which makes an exact tie
  between two *different* trades unlikely but not impossible (and impossible to rule out for
  trades on different orders). Tie-break deterministically on the trade id string
  (lexicographic) — **explicitly a determinism aid, not a claim about causal precedence**, per
  the review's caution that opaque ids are not presumed chronological. Nothing in this design
  depends on the tie-break's direction being economically meaningful, only on it being stable.
- **Late arrivals**: a trade whose own timestamp is earlier than trades already `ledger_written`
  can still be *observed* later (a delayed-visibility read). Its `fills` row is inserted with
  its own `exchange_timestamp` (already the practice for replay — `TradeLog.log_fill`'s
  `timestamp` override parameter exists for exactly this), which is what makes the `ORDER BY
  timestamp` fix above necessary and sufficient — insertion order no longer needs to match
  economic order once every reader sorts by the row's own timestamp field instead of assuming
  it.
- **Late corrections to an already-observed trade**: the review is right that trade-id
  uniqueness would silently discard a corrected payload for the *same* trade id. Two cases:
  - *Fee.* The existing `fee_adjustments` mechanism (`TradeLog.log_fee_adjustment`,
    `bot/data/trade_log.py:184`-`232`) already exists precisely because `fetch_order()`'s
    cumulative fee has been observed to settle after an initial read. **Open question, not yet
    resolved by source inspection** (§11): does a Kraken *trade's own* fee (from
    `fetch_my_trades`) ever change on a later read of the same trade id, or is the existing
    fee-settlement lag purely an artifact of the *order-level* cumulative field lagging behind
    trade-level data that was already final? If the latter, moving to trade-level data may
    reduce or eliminate the need for fee_adjustment events going forward — but this must not be
    assumed; the mechanism stays as a documented fallback either way. If a freshly-fetched
    trade payload for an already-`observed` trade id shows a **different** fee than the stored
    immutable payload, that is not a duplicate to silently skip — it is a correction, recorded
    via `log_fee_adjustment(order_id=..., adjustment_id=f"{trade_id}:fee_correction:<n>")`,
    distinct from the trade-id-keyed `fills` uniqueness guard.
  - *Anything else changing* (price/amount/side on an already-observed trade id) would indicate
    a genuine data-integrity problem, not a normal correction — treat as `reconciliation_blocked`
    and alert loudly, never silently overwrite an immutable payload.
- **Terminal order status is not a fee-finality signal.** `fetch_order().status == "closed"`
  says the order stopped accepting new fills; it says nothing about whether every trade under
  it has had its *fee* finalized. Fee-correction checking (above) runs independent of order
  terminal status.

---

## 5. Precision and tolerance (review §3, last sentence)

A single flat epsilon (the prior draft's blanket `1e-8`) is wrong for a quantity check applied
across cash and fees too. Use market metadata, not a hardcoded constant:

- **Quantity tolerance**: derived from `exchange.markets[symbol]['precision']['amount']`
  (ccxt's normalized per-market precision) — half of one increment, not an arbitrary constant.
- **Cost/cash tolerance**: derived from the *quote* currency's own precision
  (`markets[symbol]['precision']['price']` / the quote currency's minimum unit — CAD is
  effectively 2 decimal places, unlike BTC's much finer amount precision) — never reuse the
  quantity epsilon for a cash comparison.
- **Fee tolerance**: fee currency can differ per trade in principle even though this bot's
  actual Kraken spot fills have consistently shown quote-currency fees; use the trade's own
  reported `fee.currency` (from `parse_trade`, already read correctly at
  `bot/execution/live_executor.py:1325`) to select the right precision, rather than assuming.

---

## 6. Accounting scope — opening basis, external movements, shared cash (review §5)

This design does **not** invent a new opening-balance/ownership concept. `_sync_position`
(`bot/execution/live_executor.py:889`-`~1070`) already establishes, at every startup, which
quantity is bot-managed versus "external holdings" (deposits, manual trades, a different
session) via the existing `ADOPT_EXTERNAL_HOLDINGS` flag and `_EXTERNAL_THRESHOLD` guard — that
remains the sole authority for the **opening** boundary. This design's checkpoint-fold (§2)
only reconciles **changes** from whatever `_sync_position` already established forward; it does
not re-derive ownership from zero.

- **External movements during operation** (not just at startup): a CAD deposit mid-session
  would otherwise show up as an "unexplained residual" under the §2 identity check. Tier B must
  additionally pull `fetch_deposits()`/`fetch_withdrawals()` (already used by
  `reconcile_ledger.py:134`, `:46`-`59` for exactly this purpose) for the checkpoint window and
  subtract/add their amounts before concluding a residual is genuinely unexplained. Tier A
  (ledger) makes this unnecessary — deposits/withdrawals appear as their own `type` in the same
  ledger stream as trades, one source instead of three independently-paginated ones.
- **Shared quote cash across symbols**: `CapitalPool` (`bot/portfolio/capital_pool.py`) already
  models ONE shared cash pool split into per-symbol slots — the exchange has exactly one
  quote-currency (CAD) balance, not one per traded symbol. **The identity check and the §10
  joint verification must therefore run cash reconciliation at the account level** (one check
  per quote currency, comparing the exchange balance against `CapitalPool`'s aggregate view —
  sum of allocated slot cash plus free pool cash) — never per-symbol for cash. Base-asset
  inventory (BTC, SOL, ...) stays per-symbol, since each traded symbol owns a distinct asset.
- **`total` vs. `free`/`used`**: reconciliation (this document, throughout) always uses
  `fetch_balance()`'s `total` — the actual quantity owned, matching what trade history sums to,
  regardless of how much of it is currently reserved by a resting native stop order (the
  documented 2026-08-15 native-stop mechanism reserves 100% of the base asset while a stop
  rests — `CLAUDE.md`, "Native exchange-side stop-loss"). `free`/`used` matter for a *separate*
  concern — "can a new order be placed right now" — already handled elsewhere in the execution
  path (order-placement capacity checks) and explicitly out of scope for this accounting
  design.

---

## 7. Blocking rules, revised (review §6)

Two distinct block flags, not one:

- **`symbol_reconciliation_blocked[symbol]`** — set when the §2 identity check (or Tier A
  ledger check) fails to fully explain that symbol's **base-asset** inventory. Blocks new BUYs
  for that symbol only.
- **`account_cash_reconciliation_blocked`** — set when the account-level **quote-currency**
  identity check (§6, shared cash) fails to fully explain the shared cash balance. **Blocks new
  BUYs for every symbol** drawing on that pool, not just one — an unreconciled shared-cash
  residual can misprice sizing for any symbol, so no symbol's own clean record makes it safe to
  trade from an unreconciled pool.
- **Exits are never blocked by either flag** (unchanged from the original draft) — but sizing
  an exit under either blocked state must re-fetch `fetch_balance()`'s `total` for the base
  asset **immediately before** placing the protective order, rather than trusting a
  possibly-stale locally-derived quantity. Reconciliation-blocked prevents advancing *derived
  accounting/journal state* on ambiguous evidence; it must never prevent refreshing the
  exchange-authoritative quantity used to size an actual exit.
- **§10's joint verification "pass" condition is corrected**: blocked symbols or a blocked
  account-cash state are **explicit FAIL conditions for a resumption check**, reported by name
  and reason — never folded into a silent "aside" category that a summary pass/fail could
  obscure. A resumption-readiness verdict is PASS only with zero blocked flags and zero
  unexplained residuals.

---

## 8. Revised scenario matrix

Supersedes the original draft's matrix — retains its restart/fill-pattern/storage-failure axes,
adds rows the review's specific counterexamples require, and removes the false claim that any
row proves inclusion rather than identity-convergence.

| # | Scenario | Expected authoritative behavior | Status |
|---|---|---|---|
| 1 | Trade executes strictly between a `fetch_my_trades` read and the following `fetch_balance` read (review's own counterexample) | §2 identity check finds an unexplained residual for that checkpoint; symbol stays/goes `reconciliation_blocked`; resolves automatically once the trade becomes visible on a later `fetch_my_trades` and the identity re-checks clean | **New — the specific case the v1 design got wrong**; no test exists, none is meaningful until the identity-check protocol is implemented |
| 2 | Same as #1, but the trade is a **deposit**, not a trade | Tier B: identity resolves once `fetch_deposits`/`fetch_withdrawals` for the window is pulled and its amount explains the residual. Tier A: resolves natively, single ledger read | **New** |
| 3 | Kraken's `Query Ledger Entries` permission is not granted (current state) | Design runs Tier B only; Tier A code paths (if implemented) detect the missing permission via a specific API error and fall back, never silently retrying forever | **New — matches actual current key permissions per §11** |
| 4 | A one-time migration backfill needs more trades than fit one `TradesHistory` page | Explicit `ofs`-paginated loop continues until accumulated records equal the response's own `count`; a page-count safety cap prevents unbounded looping against a corrupted `count` | **New — no pagination loop exists anywhere in this codebase today** (confirmed: no `ofs` reference in `bot/execution/live_executor.py` or `reconcile_ledger.py`) |
| 5 | Two trades share the exact same `time` value | Tie-broken deterministically on trade id string; replay is stable across runs; no claim of causal ordering is made anywhere downstream | **New** |
| 6 | A trade already `ledger_written` is re-fetched with a **different fee** than first observed | Recorded as a `fee_adjustments` correction keyed `f"{trade_id}:fee_correction:<n>"`, never silently discarded by the trade-id uniqueness guard, never silently overwritten in `observed_trades`' immutable payload either | **New** |
| 7 | A trade already `ledger_written` is re-fetched with a **different price or amount** | Not treated as a correction — flagged `reconciliation_blocked` and alerted as a data-integrity anomaly | **New** |
| 8 | An unreconciled shared-cash residual exists while every individual symbol's own base-asset check is clean | `account_cash_reconciliation_blocked` blocks BUYs for **all** symbols, not just the one that happened to be checked first | **New — directly the review §6 gap** |
| 9 | A `reconciliation_blocked` symbol needs a protective exit | Exit sizes off a **fresh** `fetch_balance().total` read at exit time, ignoring any stale locally-derived quantity; the block itself is never bypassed for the *decision* to exit, only for the *sizing* input | **New** |
| 10 | Migration backfill trade matches an existing UUID-keyed `fills` row | No duplicate `fills` row inserted; `observed_trades` links backward to the existing row via matched timestamp/side/amount (reusing `reconcile_ledger.py`'s existing matcher) | **New — required before any live trade-id migration** |
| 11–17 | *(retained from the original draft's rows 1, 2, 5, 8, 10, 11/12, 13 — single-boundary/constant-price recovery, multiple concurrent unresolved orders, adoption merges, SQLite/state-file storage-failure injection, 3+ chained restarts)* | Unchanged expectations | Unchanged status — see the prior revision for the full per-row detail; those rows described real, still-valid gaps in the *old* scalar-cap mechanism and remain useful acceptance criteria once restated against `observed_trades` instead of `checkpoint_qty_cap` |

---

## 9. Joint verification procedure, revised

Corrects the original §6: cash is checked **once, at the account level**, not per symbol;
blocked states are explicit fail conditions, not set aside; Tier A/B are both accommodated.

1. **Cash (account level, once per quote currency).** Read `fetch_balance().total[quote]`. Run
   the §2 identity check (Tier B) or read the latest `fetch_ledger` balance entry (Tier A)
   against `CapitalPool`'s aggregate view (allocated slot cash + free pool cash) plus every
   `observed_trades` cash-affecting row plus deposits/withdrawals since the last confirmed
   checkpoint. Any residual → `account_cash_reconciliation_blocked`, reported by amount and
   currency, not silently absorbed.
2. **Base-asset inventory (per traded symbol).** Read `fetch_balance().total[base]`. Run the
   same identity check restricted to that symbol's own `observed_trades`, starting from
   whatever `_sync_position`'s existing opening-boundary determination established. Any
   residual → `symbol_reconciliation_blocked[symbol]`.
3. **`observed_trades` vs. `fills`.** Every `observed_trades` row with `ledger_written_at IS
   NOT NULL` must have exactly one matching `fills` row (`exec_key = trade_id`); every row with
   it `NULL` must not — this checks the atomic-write invariant from §3.1 itself, not the
   exchange relationship, and should never fail if the SQLite transaction boundary in §3.1 is
   implemented correctly. A failure here is a code bug, not an exchange-data problem, and
   should be distinguished as such in the report.
4. **`PositionManager` vs. `observed_trades` fold.** Recompute the §3.2 fold independently of
   whatever `PositionManager` currently holds in memory and diff. Any difference means the
   in-memory state was not actually rebuilt from the ledger at last startup (a wiring bug, not
   an exchange-reconciliation problem) — report distinctly from steps 1–2.
5. **Verdict.** PASS only with zero blocked flags (step 1, step 2) and zero diffs (steps 3–4).
   Any blocked/diff condition is reported by name, symbol/currency, and reason — never
   aggregated into a single boolean that could hide *which* thing is unresolved.
6. Run this: once, manually, before any implementation of §2–§8 lands; as an automated
   pre-resumption gate before `logs/HALT` is ever lifted (additional to, not a replacement for,
   the existing 2026-09-12 fresh-out-of-sample walk-forward requirement); periodically
   thereafter via the existing daily health digest (`bot/main.py._maybe_send_health_digest()`).

---

## 10. Table-driven protocol tests before joint checks (review's recommended item 5)

Per the review's explicit sequencing ask, the implementation order should test the §2
identity-check protocol and the §3 durable-progress model **in isolation** — synthetic
balance/trade/deposit sequences exercising the assumptions stated in §2 directly, including
adversarial violations of them (a trade that "disappears" between reads, a residual that never
resolves, a deposit that arrives out of order) — before wiring them into the four-way joint
check in §9, which should only ever be exercised on top of protocols already independently
proven sound.

---

## 11. Explicitly unverified — needs live/sandbox confirmation before implementation

Stated plainly rather than assumed, matching the review's own standard for its CCXT findings
("no live capability/permission guarantees were tested"):

- **Kraken ledger `refid` join semantics.** Whether a `type=trade` ledger entry's `refid`
  matches a trade's own id, its order id, or something else entirely was not determined from
  source alone — `fetch_ledger`'s docstring/comments do not specify it, and no live call was
  made. Needed before Tier A can attribute a ledger entry to a specific `observed_trades` row.
- **Whether a Kraken trade's own fee (via `fetch_my_trades`) is ever revised after first
  observation**, versus the fee lag being purely an order-level (`fetch_order()`) aggregation
  artifact. Affects whether §6.2's fee-correction path is a common case or a rare edge case —
  either way the mechanism stays, but this changes how much weight to put on testing it.
- **The live API key's actual currently-granted permission set.** `CLAUDE.md`'s documented
  setup and `reconcile_ledger.py`'s own report text both indicate "Query Ledger Entries" is not
  granted, but neither is a live permission query — worth confirming directly (e.g. Kraken's
  account security page) before deciding whether Tier A is realistically reachable soon or a
  longer-term upgrade.
- **Whether `fetch_my_trades`'s single-page response, at this account's actual historical
  volume, in fact exceeds one page during the §3.3 migration backfill.** Assumed possible and
  designed for (§1, §8 row 4); not confirmed to actually occur.

---

## 12. Out of scope for this document

- No code in `bot/execution/live_executor.py`, `bot/main.py`, `reconcile_ledger.py`,
  `bot/data/trade_log.py`, or any test file was changed.
- `logs/HALT` untouched — still engaged since 2026-09-12.
- No live exchange calls were made.
- No profitability, promotion, or resumption decision is implied. Per the existing 2026-09-12
  review-deadline policy, resumption still requires its own fresh out-of-sample walk-forward,
  independent of and in addition to this accounting work.
- Whether to pursue the Tier A ledger-permission grant is a human decision, not resolved here.

## 13. Recommended sequencing

1. Resolve §11's open verification items (at minimum: confirm the live key's actual permission
   set; decide whether to pursue Tier A).
2. Implement `observed_trades` (§3.1) and the migration backfill (§3.3), reusing
   `reconcile_ledger.py`'s existing trade-matching logic rather than duplicating it.
3. Implement the §2 identity-check protocol (Tier B baseline, Tier A if §11/pursued) with the
   §10 isolated protocol tests, including adversarial violations of its stated assumptions.
4. Wire `PositionManager` reconstruction (§3.2) and the ordering fix to `live_comparison.py`
   (§4) as required companions, not optional follow-ups.
5. Implement the revised blocking rules (§7) and re-run the existing PASS-5…PASS-11 regression
   suite plus the §8 new scenario rows.
6. Extend `reconcile_ledger.py` into the §9 four-way automated check.
7. Only then revisit any `logs/HALT` decision — separate, explicit, human, unaffected by this
   document.
