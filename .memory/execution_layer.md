---
name: execution-layer
description: "PaperExecutor + LiveExecutor design — Order lifecycle, Portfolio state, P&L tracking, fees, state persistence"
metadata:
  type: project
---

Two executors share an identical interface. `main.py` selects between them via `cfg.exchange.live_trading`.

## LiveExecutor — `bot/execution/live_executor.py`

Built 2026-06-07. Hardened 2026-06-09/10. Places real market orders on Kraken via ccxt.

**Activation:** `LIVE_TRADING=true` in `.env`. Use `DRY_RUN=true` first for candle-by-candle validation.

**All hardening items complete (as of 2026-06-09):**

| Feature | Method | Notes |
|---|---|---|
| Balance sync | `_sync_cash()` | Fetches `free[quote]` from exchange on init; warns if currency absent; falls back to `STARTING_CASH` |
| State persistence | `_save_state()` / `_load_state()` | JSON at `logs/live_state.json`; saves cash, position, cost_basis, realized_pnl, fees_paid; symbol mismatch guard on load |
| Order validation | `_validate_order()` | Checks `limits.amount.min` and `limits.cost.min` from `load_markets()`; runs in dry-run too; rejection message states minimum viable trade in quote currency |
| Market data | `load_markets()` at init | Public endpoint; fail-fast in live mode if unavailable |
| Order polling | `fetch_order` × 3 polls | After `create_order`; uses last `filled` amount on timeout (partial fill); loud WARNING if not closed |
| Fee deduction | From `raw['fee']` | Deducts if `fee.currency == quote`; logs WARNING and skips if currency mismatch; raw fee-dict logged at WARNING for audit |
| Order history | `_fills` / `_rejects` lists | `filled_orders()` and `rejected_orders()` return real history |
| Fee tracking | `_fees_paid` | Accumulates session fees; persisted in state file; exposed as `fees_paid` property |
| Reset | `reset()` | Restores `_starting_cash`, clears lists |
| Imports | from `executor.py` | `Order`, `OrderSide`, `OrderStatus`, `Portfolio` — no enum type mismatch with main.py |

**Startup init order:**
1. Build ccxt exchange instance
2. `load_markets()` — public endpoint; fail-fast in live mode
3. `_load_state()` — restore position/cost_basis/fees_paid from disk (dry-run also loads)
4. `_sync_cash()` — override cash with real exchange balance (live only); warns if drift > $0.50

**Test coverage:** `test_live_executor.py` — 11 tests, all mocked exchange.

**Known fee finding (2026-06-11):** Actual Kraken fee on first fill was 0.80% (not 0.26% modeled). Raw fee-dict logging added; next fill will reveal true structure. Likely BTC/CAD FX surcharge on top of 0.26% taker.

**Native trailing-stop fix (2026-08-19):** `sync_protective_stop(stop_price, trailing_pct=None)` now
takes an optional `trailing_pct` — when set, places a Kraken `trailing-stop` order
(`params={"trailingPercent": "X.XXXX"}`, ccxt derives the ordertype) instead of the static
`stopLossPrice` order. Why: `ss['native_stop_price']` (bot/main.py) was set once at BUY-fill
and never updated as the software trailing stop's `trail_peak` rose — the native backstop
under-protected exactly when there was profit to protect. **Scoped narrowly**: only matters
when `ss['atr_sl'] == 0` (ATR SL otherwise always wins `_trail_sl_level` in bot/main.py, so a
flat native stop already mirrors it exactly) — with the current live config (`ATR_SL_MULT=2.0`
always set), this path is dormant; `TRAILING_STOP_PCT=0` in `.env` too. Kraken trailing-stop
orders track the peak server-side once placed — no repeated repricing calls needed, only a
one-shot static→trailing swap the instant `trail_peak` arms (`bot/main.py`, guarded by new
`ss['native_stop_is_trailing']` flag). Quantity changes (partial TP, partial fill) still
cancel/replace (`_resync_native_stop()` helper in bot/main.py) — Kraken has no in-place volume
amend via `create_order`; a trailing re-place restarts the tracked peak from the price at
re-placement (accepted precision loss, same as the static order's own per-resize snapshot).
`native_stop_is_trailing` is persisted in the state file — LiveExecutor's own copy was always
correctly restored from disk + reconciled against Kraken on restart; it's `bot/main.py`'s
*separate* `symbol_state` copy that had the gap (see 2026-08-20 entry below, now closed).
Tests: `test_live_executor.py`, 7 new cases (trailing placement param, priority-over-static,
dry-run no-op, cancel, resync-on-quantity-change, failure-alert, state persist/restore across
restart).

**Restart-seeding gap CLOSED 2026-08-20** (flagged, deliberately unfixed, right above). Root
cause was narrower than first described: `LiveExecutor`'s own `native_stop_order_id`/
`native_stop_price`/`native_stop_is_trailing` were ALREADY correctly reconciled against
Kraken's real open orders on every restart via `_verify_resting_stop_on_startup()` (built into
the original 2026-08-07 feature) — the actual gap was that `bot/main.py`'s *separate*
`symbol_state` (`ss`) copy of the same two fields was never re-seeded from it, always
defaulting to `None`/`False` post-restart. Confirmed the concrete risk mechanistically:
`_resync_native_stop(ss)` (fires on partial TP / a partial fill on an urgent SL/TP exit)
trusts `ss`'s stale copy — with both fields at their defaults it calls
`sync_protective_stop(None)`, which unconditionally cancels whatever's really resting
(`_cancel_native_stop()` always runs first) and then places nothing, since neither
`stop_price` nor `trailing_pct` is set. Real naked-position risk, not theoretical. Fix: new
`_seed_native_stop_state(executor) -> (price, is_trailing)` in `bot/main.py` (same
extract-for-testability pattern as `_evaluate_drift`/`_update_auth_health`), a pure read of
the executor's own already-reconciled public properties (added `native_stop_price` as a new
public property alongside the existing `native_stop_is_trailing`) — no new network calls, no
recomputation, wired into the existing "Restart recovery" loop right where `pm`/`sm` already
get seeded. Deliberately mirrors whatever's ACTUALLY resting verbatim rather than recomputing
from `avg_entry`/ATR, matching the pre-existing "still-open saved order kept as-is" decision.
**Also found+fixed same pass:** `_verify_resting_stop_on_startup()` never checked for MORE
than one stop-type order resting (only ever confirmed/cleared its own single tracked id) — now
also scans the same already-fetched `fetch_open_orders()` result for `descr.ordertype` in
`{stop-loss, trailing-stop}` (the two ordertypes this bot ever places; the unified ccxt `type`
field is unreliable here since Kraken's `stop-loss` maps to unified `'market'`), and alerts
loudly via Telegram if more than one is found — deliberately does not try to auto-resolve by
picking one. **Deliberately NOT fixed, flagged for later:** (1) whether a still-resting
order's quantity matches the position's actual post-restart size — separate, adjacent gap;
(2) adopting an untracked-but-real resting order when the state file's own id is
missing/lost — `_verify_resting_stop_on_startup()` still only confirms/clears its own tracked
id in the no-tracked-id branch, found during design work but deliberately scoped out rather
than expanding an already-reviewed, approved plan mid-implementation. Tests:
`test_live_executor.py` (+3: multi-stop-order alert, unrelated-order no-false-positive,
`native_stop_price` property), `test_drift_escalation.py` (+4: `_seed_native_stop_state()` —
static/trailing/naked/mismatched-trusted-verbatim). Suite 536→543. No `bot/strategy/*`
touched — hash unchanged, confirmed via `compute_strategy_hash()`, no walk-forward needed.
**`trailingPercent` param verified same day, redone as literal proof after a review correctly
called out that the first pass was prose-with-citations, not actual evidence:**
`verify_kraken_trailing_stop_param.py` (repo root) runs the REAL installed ccxt 4.5.56
(asserts version, no network calls) and proves `params={"trailingPercent": "2.0000"}` →
`{'ordertype': 'trailing-stop', 'price': '+2.0000%', 'trigger': 'last', ...}` — re-run it,
don't just trust this note. Literal source excerpt + that exact runtime output are embedded
in `_place_native_trailing_stop()`'s docstring (`bot/execution/live_executor.py`). Cross-checked
against Kraken's own AddOrder REST docs the same way (ordertype enum includes `trailing-stop`,
price format matches exactly, no spot/margin restriction). ccxt's "*margin only*" docstring
label isn't code-enforced — same label sits on the already-working-live `stopLossPrice` param.
No fix needed.

**Real Kraken server round-trip done same day, PASS — the one remaining zero-risk check.**
`verify_kraken_trailing_stop_live_validate.py` (repo root, DIFFERENT risk profile than the
script above: real authenticated API call, guarded behind an explicit `--i-understand-...`
flag, not pytest/CI, not for casual re-runs) called Kraken's real AddOrder with
`params={"trailingPercent": "2.0000", "validate": "true"}` — note the **string** `'true'`,
not Python bool `True`: `urlencode_nested()` (this endpoint's POST-body encoder) has no
bool→string normalization, so `True` would've serialized as literal `validate=True` and risked
Kraken not recognizing it as truthy (ccxt's own kraken.py hardcodes lowercase strings for the
identical reason on `reduce_only`/`post_only`). Sized at the real `MAX_SLOT_CASH_CAD=$77` cap
→ 0.000806 BTC. Kraken's response: `id: None` (nothing executed) + description
`'sell 0.00080 XBTCAD @ trailing stop -2.0000%'` — fully accepted, well-formed. Both halves
of verification (ccxt-source-level and real-server-level) are now PASS. This does not need
re-running absent a ccxt/Kraken API change.

---

## PaperExecutor — `bot/execution/executor.py`

Paper trading engine. No real money, no exchange calls.

**How to apply:** Used when `LIVE_TRADING=false`. `simulated_executor.py` is the old simple version — don't wire it into main.

## Key types

| Type | Role |
|---|---|
| `Order` | Immutable record: id, symbol, side, qty, price, status, timestamps, total_value, reject_reason |
| `OrderStatus` | `PENDING → FILLED or REJECTED` (also `CANCELLED`) |
| `OrderSide` | `BUY / SELL` |
| `Portfolio` | cash, position (units), realized_pnl, `_cost_basis`, `unrealized_pnl()`, `total_value()` |

## Rejection rules (inside executor, before risk layer)
- BUY: rejected if cash < order.total_value (PaperExecutor) or exchange error / validation fail (LiveExecutor)
- SELL: rejected if position ≤ 0
- HOLD: returns `None`, no order created

## P&L model
- `realized_pnl`: credited on each SELL as `(sell_price - cost_basis) * qty`
- `unrealized_pnl(price)`: `(price - cost_basis) * position`
- Cost basis: **weighted average** — `(prev_cost + new_price * qty) / new_total_qty`
- `PositionManager` is the authoritative display P&L source; `Portfolio._cost_basis` kept consistent

## Float safety
- SELL position check uses `position < qty - 1e-9` (not strict `<`) to handle float drift
- `PositionManager` uses `< 1e-9` instead of `== 0.0` when zeroing quantity/avg_entry
