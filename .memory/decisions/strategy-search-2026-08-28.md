---
name: strategy-search-2026-08-28
description: A 3-strategy search (mean-reversion, grid/DCA, cross-sectional momentum) for a SECOND live strategy on either bot, 2026-08-28/29. None cleared the bar. Read before proposing any new trading strategy — the consistent finding is that beating a passive diversified hold net of costs is hard.
metadata:
  type: project
---

**Why:** during a quiet trading stretch the user asked, repeatedly, "are there other ways to
trade / can the bots be more active / what methods are we missing." This is the consolidated
record of the strategy search that followed. Read it before building or proposing another
trading strategy for either bot.

**How to apply:** all three strategies below were built as hermetic research scripts (no live
code, strategy hash unchanged), pre-registered parameters, walk-forward / out-of-sample
tested against the project's own bar. If a future session wants to add a strategy, the bar
is the same and the prior results are the baseline to beat.

---

## Results

| Strategy | Script | Verdict |
|---|---|---|
| Mean reversion (Bollinger/RSI, buy the dip) | `mean_reversion_experiment.py` (crypto), `stock_mean_reversion_experiment.py` (stock) | **FAILED both bots.** Crypto: PF 0.30–0.36, every window loses (1.6% Kraken fees swamp small reversions). Stock: long-only 0/16 (barely trades), long+short 1/16 (chance). The fee hypothesis was disproven — the entry has no edge. |
| Grid / DCA | `grid_dca_experiment.py` (crypto) | Did not clear; research scaffolding only, never fully concluded but no config passed. |
| Cross-sectional momentum (6-1, top-10, monthly rebalance) | `stock_momentum_experiment.py` | **FAILED, but closest.** Validation CAGR +43.8% vs SPY +21.3% (real, ~2x), beats SPY on Sharpe (1.42 vs 1.28) — but loses to equal-weight-hold-all on Sharpe (1.42 vs 1.49) and drawdown 26–33% > 1.1x SPY. Regime filter fixes drawdown but drops Sharpe below SPY. |

## The consistent finding

**Beating a simple diversified passive hold, net of costs, is hard.** Momentum has a genuine
return premium but the turnover cost + concentration risk cancel it on a risk-adjusted
basis. Mean reversion and grid have no premium at all. This is exactly the premise of the
[[project_trade_bot]] two-bucket policy — the wealth engine is a broad diversified index
hold (Bucket 1, outside the bots); the bots (Bucket 2) are a capped, gate-controlled
experiment, not the way to grow money.

Also relevant: testing 3+ strategies and keeping the best is the multiple-testing bias the
DSR/CSCV discussion in [[expert-practices-benchmark]] covers. PLTR "passing" 1-of-16 in the
stock mean-reversion run was that in action — a chance false positive, correctly not acted on.

## Where the value actually is (not new strategies)

1. **Uptime** — VPS migration for the crypto bot (ready, ~2h). Downtime directly = missed
   signal. Deferred by the user 2026-08-28 ("not right now") but still the top lever.
2. **Cost/slippage measurement** — blocked on trade volume. Both bots have < 15 fills; there
   is nothing to measure or tune yet.
3. **The ATR-sizing decision** (stock) — built, off, needs one call from the user.

## Scripts + reports

- `logs/mean_reversion_experiment_20260828.md`, `logs/stock_mean_reversion_experiment_20260828.md`,
  `logs/stock_momentum_experiment_20260829.md`
- `.memory/decisions/mean-reversion-experiment-2026-08-28.md` (fuller mean-reversion writeup)
- Tests: `tests/crypto/test_mean_reversion_experiment.py` (20),
  `tests/stock/test_stock_mean_reversion_experiment.py` (20),
  `tests/stock/test_stock_momentum_experiment.py` (14) — all hermetic, no `*/strategy/*` touched.
