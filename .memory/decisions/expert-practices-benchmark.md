---
name: expert-practices-benchmark
description: 2026-08-18 web research on how experts run crypto trading bots, benchmarked against this codebase's actual practices — what already matches/exceeds, two real gaps, decision not to implement either right now. 2026-08-19 addendum vs Freqtrade adds a third candidate (lookahead/recursive-bias check tooling), also not implemented.
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
