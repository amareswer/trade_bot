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

---

## Addendum 2026-09-10 — 4th candidate: Donchian breakout (crypto). FAILED. Search still concluded.

User asked to try one more after an exhaustive crypto-universe screen came up empty.
`breakout_experiment.py` (new hermetic research script, pre-registered params, strategy hash
untouched): Donchian(20-bar entry / 10-bar trailing exit) + EMA(200) macro filter +
ADX(14)≥20, 2×ATR hard stop, **no fixed take-profit** (let the trailing stop decide) —
long-only, 0.8%/side fee, BTC/SOL primary + ETH/NEAR/ENA secondary.

**Result — the worst BTC/SOL result of any strategy tested:**
- BTC/USDT: 5000c PF 0.70, 3000c 0.43; OOS split TRAIN PF 1.22 (+6.6%) → **VALIDATION PF
  0.52 (−34%)** — textbook curve-fit collapse. Win rate 17–23% (failed breakouts stopped out).
- SOL/USDT: 5000c PF 0.61, 3000c 0.61; **VALIDATION PF 0.52 (−53%)**.
- ETH 0.76 / NEAR 0.77–0.87 — all fail.
- ENA: OOS PF 1.41 (+60%) — the multiple-testing false positive (27% win rate, one young
  hyper-volatile coin riding a historical mega-trend; not repeatable edge).

Breakout whipsaws hardest in exactly the ranging regime BTC has been stuck in — the low
prior was right. **Strategy search: now 5 shapes tested (live pullback + mean-reversion +
grid/DCA + cross-sectional momentum + breakout), 4 failed. Concluded, firmly.**

Same session also closed the surrounding questions: moving Kraken CAD→USD changes nothing
(fees are volume/holdings-based, not currency); no cheaper platform is available to a
Canadian retail account (NDAX spreads 0.4–0.5% on majors negate its low headline fee;
Binance/KuCoin/OKX are not legal in Canada; Coinbase/Gemini ≥ Kraken). The only real lever
left is the account growing to ~$10k → Kraken's holdings tier auto-cuts the taker fee
0.80%→0.38%. Full trail: `.memory/decisions/multi-symbol-validation.md` (2026-09-09
addendum) + auto-memory `project_crypto_usd_expansion_closed_2026-09-09`. Report:
`logs/breakout_experiment_20260910.md`. New file: `breakout_experiment.py` (no tests yet —
negative result; add the hermetic-test suite if it's ever kept as standing tooling).
