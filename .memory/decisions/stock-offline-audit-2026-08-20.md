---
name: stock-offline-audit-2026-08-20
description: 2026-08-20 read-only audit of stock_bot/indicators/indicators.py and stock_bot/backtest.py for the same lookahead/self-referential-baseline bug class found and fixed in the crypto bot — result: clean, no bugs found, but two scope corrections to the original premise
metadata:
  type: project
---

**Why:** the crypto bot's [[expert-practices-benchmark]] 2026-08-19/20 addendum found and
fixed a self-referential-ATR-baseline bug in `bot/strategy/indicator_strategy.py`
(`_classify_regime()` folding the current bar's ATR into its own comparison baseline before
comparing). That fix didn't need to touch the stock bot because `stock_bot/strategy/rules.py`
imports `IndicatorStrategy` directly from `bot/strategy/indicator_strategy.py` — it inherited
the fix automatically. But `stock_bot/indicators/indicators.py` +
`stock_bot/backtest.py` are a **separate, independent reimplementation** that was never
checked against the same bug class. This session did that check.

**How to apply:** if the same bug class ever needs re-checking elsewhere in this repo (a
third independent indicator implementation surfaces, or one of these gets refactored), this
is the checklist that was actually run, not just a "looks fine" verdict.

---

## What was checked

1. **`stock_bot/indicators/indicators.py`, all 8 functions** (`sma`, `ema`, `_ema_series`,
   `rsi`, `macd`, `adx`, `atr`, `trend`, `regime`) — read in full. For each: lookahead
   (does computing a value for index i ever use data beyond i?), the specific
   self-referential-baseline pattern (a rolling history a new value gets folded into before
   being compared against), and incremental/stateful drift (does anything persist across
   separate calls?).
2. **`stock_bot/backtest.py`'s `compute_indicators()`** — the `closes[:i+1]`/`highs[:i+1]`/
   `lows[:i+1]` growing-slice loop that feeds every indicator call per bar.
3. **The SPY regime path** — `_fetch_spy_regimes()`/`_spy_regime_at()` — same growing-slice
   discipline for market-wide regime, plus the backward-only weekend/holiday gap-bridging in
   `_spy_regime_at`.
4. **Live call sites** — traced where `stock_bot/main.py` actually calls into this module
   (not assumed from the file's own docstring), since the original task framing described it
   as offline-only and that turned out to be only partly true (see corrections below).

## Result: clean

- **No lookahead.** Every slice boundary checked resolves to `[:i+1]` — inclusive of the
  current bar, nothing beyond. Verified algebraically for `macd()`'s fast/slow EMA alignment
  (`aligned_fast[j]` and `slow_s[j]` both correspond to `prices[slow_period-1+j]` — exact, not
  off-by-one) and by hand for `adx()`/`atr()`'s Wilder-smoothing minimum-valid-length edge
  case (seed slice and continuation slice partition the TR/±DM list with no gap or overlap at
  the boundary — the specific "off-by-one at array edges" class flagged as most likely to hide
  a real bug here).
- **No self-referential-baseline bug.** This is the important structural finding, not just an
  absence-of-evidence claim: **none of the 8 functions carry any state across separate
  calls** — every one is a pure, full recompute from whatever list is passed in each time. The
  crypto bot's bug required a *persisted rolling history* object (`self._atr_history`) that a
  new value got appended to before being compared against its own mean. No such object exists
  anywhere in `stock_bot/indicators/indicators.py` — the bug class is structurally impossible
  here, not merely unencountered.
- **No incremental/stateful drift.** Confirms item 1's third check directly — every function
  is a full stateless recompute per call (same shape as the crypto bot's own indicator
  engine), so there's no way for a backtest replay and a live call to diverge from
  accumulated state.

## Two scope corrections to the original task framing

1. **`stock_bot/indicators/indicators.py`'s `regime()` is live, not offline-only.**
   `stock_bot/main.py:1038` calls it every scan cycle on freshly-fetched SPY closes, feeding
   `_regime_ok`, which gates live BUYs (shared with the VIX-crisis gate). Traced and confirmed
   clean per above, so this doesn't change the "no bug" verdict — but it means this module was
   higher-stakes to audit than "an offline backtest engine" implied, and is worth remembering
   next time this area gets touched. `rsi`/`trend`/`adx`/`macd` from the same module are also
   called live every cycle but only feed the console/log indicator line — display only, not a
   decision path (the actual rule-based trigger is `IndicatorStrategy`, already-fixed,
   imported by `rules.py`; confirmed via a code comment in `stock_bot/main.py` that explicitly
   distinguishes the two).

2. **`stock_bot/backtest.py` (the file named in the original task) is dead tooling.** Zero
   importers anywhere in the codebase (verified via grep) — a standalone CLI only. There are
   *two separate backtest engines* in this repo: this file (unused, own indicator
   implementations, the one audited here), and `stock_bot/backtest/engine.py` (a different
   file, in the `stock_bot/backtest/` package) which the real gating tool — root-level
   `stock_backtest.py` — actually uses. `engine.py` imports `bot/strategy/indicator_strategy.py`
   directly, so it was never exposed to this bug class in the first place and didn't need
   auditing. `stock_bot/backtest.py`'s `--walkforward` output (`backtest_results.json`) does
   feed `LiveTradingGate.check_gate1()` in `stock_bot/analysis/accuracy_tracker.py`, but every
   call site of that (`stock_bot/main.py`'s dashboard render, `stock_bot/alerts/notifier.py`'s
   weekly email) is display-only — an IBKR-paper-to-live human readiness indicator, never
   wired into automated trade execution.

## Outcome (confirmed with user 2026-08-20)

Documentation-only close-out — no code logic changed, since nothing needed fixing:
- `stock_bot/backtest.py`'s docstring corrected (was stale, claimed indicator-pipeline parity
  with the live bot that stopped being true when `rules.py` switched to
  `bot/strategy/indicator_strategy.py`) — now states plainly it's legacy/unused and points to
  the real gate.
- `CLAUDE.md` gained a new "Stock bot `regime()` live-gating + offline-audit note" section
  (right after VIX crisis mode, which already references the same `_regime_ok` flag) covering
  both corrections above plus the audit result.
- This file records the full checklist + evidence for future reference.

Full suite re-run after the docstring edit: no regressions (docs-only change, as expected).
