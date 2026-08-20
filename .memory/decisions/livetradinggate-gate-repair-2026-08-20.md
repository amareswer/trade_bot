---
name: livetradinggate-gate-repair-2026-08-20
description: Investigation + fix of two structurally broken gates in stock_bot's LiveTradingGate (accuracy_tracker.py) — Gate 1 was validating a stale/disconnected strategy config, Gate 2 was checking a permanently-retired book. Both repaired 2026-08-20. Enforcement (wiring into IBKRExecutor) — RESOLVED same day, second pass: hard block on Gates 1-3, Gate 4 excluded.
metadata:
  type: project
---

**Why:** `LiveTradingGate` is the stock bot's paper→live readiness indicator, shown on the
dashboard and in the weekly email. An investigation (same day) found two of its four gates
were checking things that could never meaningfully pass or fail — not "close to ready," but
structurally broken. This file records what was found, what was fixed, and — deliberately —
what was NOT decided (enforcement).

**How to apply:** if this gate's status ever looks confusing again, or before deciding to wire
it into `IBKRExecutor`, read this first. See also [[project_trade_bot]] for the stock bot's
broader architecture.

---

## What was found (investigation, before any fix)

- **Gate 1** read `stock_bot/backtest_results.json`, written by `stock_bot/backtest.py
  --walkforward` — confirmed **dead tooling, zero importers**, in the same day's separate
  offline-code audit. Last run 2026-06-29 (~7.5 weeks stale at investigation time), and its
  recorded config (`sl_pct=0.05, tp_pct=0.15, adx_min=15.0, rsi_max=75.0`) doesn't match what's
  actually live — `stock_bot/strategy/rules.py`'s `build_indicator_config()`
  (`adx_threshold=18.0, min_ema_spread_pct=0.004`, validated via the REAL walk-forward tool,
  `stock_backtest.py`/`stock_bot/backtest/engine.py`, on 2026-07-10). Gate 1 PASSed by
  validating a strategy the bot doesn't run. Its hardcoded symbol pair, `("AAPL", "SPY")`,
  wasn't even in `RULE_WHITELIST` — while MSFT, which *is* whitelisted, was explicitly
  excluded in a stale comment.
- **Gate 2** read `fast_trades.csv` — the retired swing/fast book. `FAST_ENABLED=false`,
  frozen since 2026-07-22, last row's exit reason literally
  `MANUAL_CLOSE_SWING_BOOK_RETIRED`. Structurally could never reach its 20-trade minimum
  again — the "1 gate remaining" dashboard status wasn't "almost there," it was permanently
  stuck.
- **Gate 3** (`paper_trades.csv`+`ibkr_trades.csv`, the real active Mode A/B position book)
  was the one gate genuinely current and meaningful — but its bar, 5 round-trips, was far
  below any real readiness signal (crypto's own capital-scaling gate: 15 fills, PF≥1.2,
  shadow-match≥95%, fee/slippage on-spec).
- Checked whether the crypto bot's own capital gate is code-enforced anywhere, as a possible
  precedent for hard-blocking this one — **it isn't**. Pure documentation + `shadow_signal.py`
  (informational) + human discipline. This codebase's consistent pattern across both bots is
  "compute rigorously, surface clearly, human decides" (see `rescreen.py`'s own docstring:
  *"THIS SCRIPT NEVER CHANGES A WHITELIST... the decision remains a human checkpoint"*) — not
  automatic enforcement. Making `LiveTradingGate` hard-blocking would be a new pattern here,
  not an extension of an existing one.

## What was fixed (this session, 2026-08-20, discussed and confirmed before building)

**Gate 1 — rewired to the current strategy.** `stock_backtest.py` now writes
`logs/stock_backtest_latest.json` (fixed path, always overwritten, alongside its existing
dated `.md` report — no new CLI flag) on every run: per-symbol verdict + per-window
trades/win_rate/profit_factor/net_pnl/sl_exit_rate, using `bot/atomic_json.atomic_write_json`
for the same atomic-write guarantee the rest of this codebase's persisted state uses.
`check_gate1()` reads this file and requires **every** symbol in the current
`RULE_WHITELIST` (`cfg.rule_whitelist_str`) to show `verdict: PASS` — trusting
`stock_backtest.py`'s own computed verdict rather than re-deriving a threshold a second time
(the two-copies-of-one-threshold drift class this codebase has been bitten by before — see
`bot/backtest/params.py`'s docstring on the crypto side). Old `_GATE_SYMBOLS`/`_GATE1_MIN_PASS`
hardcoding removed entirely.

Quorum decision (asked, not assumed): **all** RULE_WHITELIST symbols must pass, not a
percentage. First real run under the new logic: 16 symbols, ~4m45s, **15/16 PASS, AMD FAIL**.
AMD's failure investigated separately same day — see [[amd-whitelist-investigation-2026-08-20]]:
small-sample noise (one thin 3-trade recent window), not a genuine edge failure. AMD NOT
removed from `RULE_WHITELIST`; re-check once that window holds more trades.

**Gate 2 — repurposed to AI confidence-band edge.** Reads the same active book Gate 3 reads
(`paper_trades.csv`+`ibkr_trades.csv`), not the retired fast book. Checks ≥10 completed
MED/HIGH-confidence (80+) round-trips with a ≥55% win rate — mirroring
`ConfidenceBandTracker.recommendation()`'s own "AI HAS EDGE" threshold, implemented as a
structured check (not string-parsing that method's return value, so the gate stays testable
independent of its exact wording). This is a genuinely different question from Gate 3: is the
AI's confidence score calibrated, independent of whether the rules-based strategy itself has
edge — not a duplicate signal.

Chosen over the other two options discussed (retire Gate 2 entirely; repurpose to a stricter
threshold on the same book Gate 3 already reads) because it's a real, complementary,
already-built-but-orphaned signal (`ConfidenceBandTracker` had zero gate wiring before this),
not a duplicate of Gate 3's question.

**Gate 3 — raised to the documented Phase A bar.** ≥30 completed round-trips, PF≥1.2, win
rate≥30%, **all three required** — CLAUDE.md's own Roadmap already stated this exact number
twice ("Stock Phase A gate" and "IBKR live go-live" entries) without it ever being
implemented; reused verbatim rather than inventing a new threshold. Below 30 trades: still
`PENDING` with a progress count, unchanged pattern. Label corrected from "Swing paper
(daily)" (stale, conflated with the retired book by name even though the code always read the
position book) to "Position book (live)".

**Enforcement — RESOLVED, same day, second pass (hard block, Gates 1-3, Gate 4 excluded).**
`IBKRExecutor.__init__()` (`stock_bot/execution/ibkr.py`) now extends its existing
`port in _LIVE_PORTS and not allow_live` guard: when a live port is requested with
`allow_live=True`, it also calls `LiveTradingGate().evaluate()` and raises `ValueError` (same
exception type/pattern as the pre-existing guard) naming every gate that isn't `PASS`, before
any TWS connection is attempted. Gate 4 (infrastructure importability) was explicitly asked
about and excluded — a broken smoke-test import (an unrelated code-hygiene signal, not a
trading-readiness one) shouldn't block someone otherwise cleared to go live. The check only
fires inside the `allow_live=True` branch; paper-mode construction (the default) never
evaluates the gate at all — confirmed by a test that makes `LiveTradingGate.evaluate()` raise
if called and shows paper mode still succeeds.

This closes the "deliberately deferred" item from the same day's first pass. The reasoning
that made enforcement a genuinely open question then (no code-enforced precedent anywhere
else in this codebase — see "What was found" above) still stands as *context* for why this
was worth asking rather than assuming; the answer, once asked, was a clean hard block matching
`IBKRExecutor`'s own existing pattern, not a new mechanism.

## Tests

`tests/stock/test_accuracy_tracker.py` (new file, 18 cases, first pass — what the gates
measure): Gate 1 — missing/malformed JSON (fail-safe, doesn't raise), all-whitelist-symbols-
pass, one-symbol-fail, symbol-missing-from-latest-run, non-whitelist-symbol-in-JSON-ignored,
empty-whitelist. Gate 2 — pending below minimum, pass/fail on the win-rate threshold,
LOW/PRE-confidence trades excluded from the tally, structural guard confirming zero remaining
reference to `_FAST_TRADES_CSV`. Gate 3 — pending below 30, all-three-criteria PASS, and both
directions of "2 of 3 criteria pass but still FAIL" (enough trades + win rate but PF<1.2;
enough trades + PF but win rate<30%) — the specific edge case requested to prove the gate
doesn't loosen to a majority-vote. Suite 580→598.

`tests/stock/test_ibkr_executor.py` (+7, second pass — enforcement): all-Gates-1-3-pass
succeeds, Gate-4-fail still succeeds (confirms exclusion), single-gate FAIL blocks with
`ValueError` naming it, PENDING blocks the same as FAIL (not-proven-ready isn't ready), error
message names only the actually-failing gate(s) — not ones that passed, blocked before any
FakeIB connection attempt (`fake._connected` stays False), and paper mode (`allow_live=False`)
never even calls `LiveTradingGate.evaluate()` (patched to raise `AssertionError` if invoked,
confirms zero effect on the common case). Suite 598→605, all passing, no regressions.

See also [[expert-practices-benchmark]] for the DSR/CSCV-adjacent reasoning about this
codebase's general "human checkpoint, not automatic enforcement" pattern — the context that
made the enforcement question worth asking explicitly in the first pass, even though the
answer here ended up being a hard block, a deliberate departure from that general pattern for
this specific, higher-stakes gate (real brokerage capital, not a position-sizing tier).
