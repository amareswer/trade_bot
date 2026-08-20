---
name: expert-practices-benchmark
description: 2026-08-18 web research on how experts run crypto trading bots, benchmarked against this codebase's actual practices — what already matches/exceeds, two real gaps, decision not to implement either right now. 2026-08-19 addendum vs Freqtrade adds a third candidate (lookahead/recursive-bias check tooling) — resolved 2026-08-20: no lookahead bug, but a related self-referential-baseline bug was found and fixed.
metadata:
  type: project
---

Researched 2026-08-18 (web search, 4 queries: risk management, common retail-bot failure
modes, walk-forward/overfitting prevention, capital scaling, infra/uptime). Full source list
in session transcript. Purpose: sanity-check this project's practices against outside
consensus, not to chase every idea found.

**Why:** avoid re-researching from scratch next time "are we doing this right" comes up;
records the comparison verdict and the explicit decision not to act on the two gaps found.
**How to apply:** if reconsidering VPS migration or statistical overfitting tooling, read this
first — both were already evaluated and consciously deferred, not overlooked.

---

## Already matches or exceeds expert practice — no action needed

- **Position sizing / stop-loss:** standard advice is 1%-of-capital risk + mandatory exit on
  every trade ("bot cannot rely on the next signal to bail you out"). This project's ATR
  sizing (~0.15% real dollar risk/trade) plus the native exchange-side stop-loss backstop
  (added 2026-08-15, protects against the bot process itself being down) goes *beyond* typical
  retail bots, which usually rely on a single software-only SL that dies with the process.
- **Circuit breakers/kill switches:** advice is "pause on extreme volatility, API failure,
  repeated losses — settings only matter if the system is running to enforce them." This
  project's 4-tier breaker (HALT → kill-switch → drawdown-halt → weekly-loss → daily-loss →
  position-size) + candle-watchdog circuit breaker + manual HALT flag is more layered than
  the median setup described in the research.
- **Walk-forward validation:** called "the gold standard... strategies must prove themselves
  repeatedly rather than succeed in one lucky backtest." This project's hard rule (any
  `bot/strategy/*` change invalidates the fingerprint until a fresh 3-window walk-forward
  passes, see CLAUDE.md "Validation Discipline") already enforces exactly this.
- **Capital scaling:** advice is start small, scale only after weeks of stable performance
  across varying conditions, never on a streak. This project's 15-fill/30-fill gates (PF ≥ 1.2
  **and** shadow-match-rate ≥ 95% **and** fee/slippage on-spec, explicitly barred from
  triggering after a winning streak) is a stricter multi-criteria bar than most sources
  described (which usually check win rate or PF alone).

## Two real gaps found — both already decided, neither implemented this pass

1. **Infrastructure uptime.** Research quantified what was already suspected: home/local
   setups run ~95% availability (~36h/month downtime) vs professional VPS ~99.99%+. This
   directly confirms [[project_trade_bot]] gap #7 (58% historical downtime, Mac sleep/manual
   stops, no auto-restart) and the same-day finding that a full machine shutdown had likely
   caused missed trading windows. **Decision (user, 2026-08-18): defer VPS migration until
   live performance is confirmed good — not implemented now.** `deploy/` already has
   `VPS_SETUP.md`/`trade_bot.service`/`deploy.sh` ready and unused whenever that's revisited.
2. **Overfitting statistical rigor.** Research surfaced Deflated Sharpe Ratio and
   Combinatorially Symmetric Cross-Validation (Probability of Backtest Overfitting) as
   institutional-grade defenses against multiple-testing bias — this project doesn't use
   either. **Judgment call, not implemented:** these exist to correct for inflated results
   from large parameter searches; this project keeps the strategy space deliberately simple
   and already walk-forward-validates every change, so the marginal value is low relative to
   the added complexity at current scale (single BTC/CAD symbol, small personal capital).
   Revisit only if the strategy search space grows materially (e.g., multi-parameter grid
   optimization across many symbols).

**Net conclusion:** no code changes made from this research pass. Both gaps were already
either decided (VPS) or assessed as premature (statistical overfitting tooling) rather than
overlooked — this file exists so that judgment doesn't need re-deriving from scratch.

---

## Addendum 2026-08-19: lookahead/recursive-bias check tooling (candidate, not implemented)

Re-run of the 2026-08-18 benchmark pass, one day later — no major shift, but comparison
against Freqtrade surfaced one specific tool class not previously considered.

**Finding:** Freqtrade ships `lookahead-analysis` and `recursive-analysis` CLI commands —
narrow, cheap checks for two concrete bug classes: (1) a strategy indicator accidentally
using future-bar data during backtest, (2) an indicator whose calculation differs between
a fresh recalculation and a rolling/incremental one (recursive formula drift), which can
silently produce different values live vs. in backtest.

**Why this is distinct from the DSR/CSCV gap already deferred in this file:** that gap is
about correcting for bias from *searching many parameters* (multiple-testing bias). This
is about catching a strategy that backtests well because it's *structurally cheating* —
different bug class, cheaper to check, not a duplicate ask.

**Status:** not implemented. Candidate for a future audit pass — check whether
`bot/strategy/indicator_strategy.py`'s EMA/RSI/ADX calculations use only closed candles
(no forward-looking window) and whether incremental (`_closes`/`_highs`/`_lows` deque)
values match a full recompute over the same window at any given point. If confirmed clean
by inspection, note it here as verified-by-design rather than building new tooling for it.

**Also surfaced, not gaps:** Freqtrade's two-way Telegram control (pause/resume/status
from chat, not just alerts) and its explicit exchange-diversification advice — both
consistent with existing deliberate scope (single-exchange Kraken, alert-only Telegram),
not new problems.

---

## Resolution 2026-08-20: lookahead check done — no bug found; one related bug found and fixed

Did the audit pass this addendum proposed. Two separate questions, two separate answers:

**1. Lookahead (future-bar data used during backtest) — NOT FOUND, verified by design.**
`IndicatorStrategy.evaluate()` computes every indicator (`calc_rsi`, `calc_ema`, `calc_adx`,
`calc_atr`) as a full stateless recompute over `list(self._closes)`/`_highs`/`_lows` — plain
Python lists sliced from `deque`s that only ever have the *current* candle's OHLC appended
immediately before those calls, once per `evaluate()` invocation. No indicator function
receives or indexes anything beyond what's already in that snapshot. There is no
forward-looking window anywhere in the calculation path — confirmed by direct inspection of
`bot/indicators/indicators.py` and every call site in `evaluate()`, not just re-asserted from
the addendum's framing.

**2. Recursive/incremental-vs-recompute drift — NOT APPLICABLE as originally framed, but a
real self-referential-baseline bug was found in the adjacent regime-classification code.**
Every indicator here (RSI/EMA/ADX/ATR) is already a fresh full recompute each candle, not an
incremental rolling update — so the "does incremental drift from recompute" question Freqtrade's
`recursive-analysis` checks for doesn't apply to this codebase's indicator layer at all.
However, the deep-verification pass this addendum prompted found a **different, real** bug one
level up, in how the regime classifier *uses* the recomputed ATR: `evaluate()` appended the
current candle's ATR to `self._atr_history` (a 20-entry rolling window) BEFORE
`_classify_regime()` compared that same ATR against the window's mean — the VOLATILE-regime
threshold was being judged against a baseline the current spike had already been folded into
(self-inclusion bias, ~1/20th of the spike's own pull on the mean). Not lookahead (nothing
future was touched) and not recursive drift (the ATR value itself was always correctly
recomputed) — a third, narrower bug class: a rolling statistic computed AFTER the value it's
being compared against had already been added to it.

**Fix:** moved the `self._atr_history.append(atr_val)` call to strictly after
`_classify_regime()` consumes `atr_val`, so the comparison is always against the prior (up to
20) candles only. `bot/strategy/indicator_strategy.py`. Full detail, walk-forward result, and
new hash: CLAUDE.md "Canonical strategy fingerprint" section (hash `659d1c03987b72fd` →
`b30f2f9e769c8d41`, BTC/USDT backtest 32 trades/PF 1.72 → 31 trades/PF 2.19, walk-forward
PASS both windows PF ≥ 1.0). Tests: `tests/shared/test_indicators.py`, 2 new — one a direct
contract check on `_classify_regime()`, one a mutation-verified regression test driving real
`evaluate()` end to end (confirmed to fail under the pre-fix ordering, not just pass under the
fix). Suite 534→536.

**Addendum's original candidate item is now closed** — worth revisiting only if the strategy
gains genuinely incremental/stateful indicator math in the future (none exists today).
