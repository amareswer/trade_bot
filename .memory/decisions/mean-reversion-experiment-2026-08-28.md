---
name: mean-reversion-experiment-2026-08-28
description: A Bollinger/RSI mean-reversion strategy was built and walk-forward tested as a candidate SECOND strategy for BOTH bots (crypto and stock). FAILED decisively on both. Not promoted. Read before re-proposing "trade more in ranging markets" / mean reversion / "buy the chop" for either bot.
metadata:
  type: project
---

**Why:** the live 4h strategy is trend-following and sits flat in ranging markets by
design (ADX >= 18 gate). The user, watching a quiet stretch, asked whether there are
other ways to trade — specifically whether the bots could be more active. Mean reversion
(buy oversold dips inside a range, exit on reversion to the mean) is the natural
complement — it trades exactly when the trend strategy doesn't. This is the record of
testing whether it actually has edge.

**How to apply:** if a future session proposes a mean-reversion / grid / "trade the chop"
strategy for crypto, read this first. The specific parameter set below failed; a materially
different construction could be retried, but the *failure mode* (stops + fees swamp the
small reversions) is structural and likely to recur.

---

## What was built (research only, nothing live)

`mean_reversion_experiment.py` + `tests/crypto/test_mean_reversion_experiment.py` (20 tests).
Hermetic, standalone — no `bot/strategy/*`, no `.env`, no `bot/main.py`, no live executor,
strategy hash `b30f2f9e769c8d41` unchanged. Same discipline as `grid_dca_experiment.py`:
parameters fixed in source BEFORE any result was seen.

**Strategy (pre-registered):**
- Regime: ADX(14) < 20 (ranging — the inverse of the live ADX>=18 gate)
- Entry (long only, Kraken spot): close below lower Bollinger(20, 2.0σ) AND RSI(14) < 35
- Exit: close >= middle band (target) / -4% stop below entry (intra-candle vs low) /
  18-bar time stop / 1-bar cooldown after exit
- Fee 0.8%/side (the real live Kraken finding — 1.6% round trip), $1000 fixed notional

**Bar:** PF >= 1.2 in every window that reached >= 10 trades, across 5000/3000/1000
trailing 4h candles — the same shape as the symbol-promotion / capital gate.

## Result — FAILED, both symbols, decisively

| Symbol | 5000c | 3000c | 1000c |
|---|---|---|---|
| BTC/USDT | PF **0.30** (20 tr, ret -28.7%, SL-exit 30%) | PF **0.32** (14 tr, -20.0%) | PF 0.18 (4 tr) |
| SOL/USDT | PF **0.36** (18 tr, ret -39.4%, SL-exit 61%) | PF 0.73 (9 tr) | PF 0.15 (2 tr) |

Win rate ~50% (BTC) but PF ~0.3 — the wins are small (a few % reversion to the mean), the
losses are ~4% (the stop) plus 1.6% round-trip fees. "Picking up pennies in front of a
steamroller," and the steamroller wins. Same shape as the June 2026 USD alt screen's
finding for the trend strategy: entry has no edge net of realistic costs.

Report: `logs/mean_reversion_experiment_20260828.md`.

## Verdict

**Not promoted. Not worth pursuing this construction.** Re-run only if the parameters or the
fee assumption change for a documented reason. The complementary-regime idea is sound in
principle (the two strategies would trade in near-mutually-exclusive ADX regimes) but this
implementation has negative edge — trading more here would lose money, which is exactly the
question the experiment was built to answer.

Related: [[multi-symbol-validation]] (same fee-drag failure mode on alts),
`grid_dca_experiment.py` (the sibling "does a non-trend strategy clear our bar" research,
also inconclusive/unpromoted), CLAUDE.md "Standing Policies — Investment philosophy" (the
bots are a capped, gate-controlled experiment, not the wealth engine).

---

## Stock re-test — same day, ALSO FAILED

The user asked the "other ways to trade" question for the stock bot too. Hypothesis: the
crypto failure was fee-driven (1.6% round trip); IBKR's cost is ~0.2-0.4% + shorting is
available, so maybe it clears on stocks. Built `stock_mean_reversion_experiment.py` (+20
tests) — daily candles, 16 US `RULE_WHITELIST` symbols, IBKR `_round_trip_commission` + 15
bps, optional SHORT leg, gated by `stock_backtest.py`'s own criteria.

**Result: long-only 0/16, long+short 1/16.**
- Long-only: nearly every symbol fails "< 10 trades full window" — ADX<20 + oversold +
  below-band is rare on daily large-cap bars. The one that trades (AMD, 11) has PF 0.88.
- Long+short: only PLTR passes (PF 2.00/2.14/1.68). 1-of-16 at a PF≥1.2 bar ≈ chance — a
  multiple-testing false positive. SL-exit 74-86% on MRNA/AMD (shorting into a 2024-26
  uptrend → stopped out). Rest PF 0.3-0.9.

**The fee hypothesis is disproven.** Lower cost didn't help — the mean-reversion *entry* has
no edge, full stop. On crypto it manifested as fees eating small wins; on stocks as the
strategy not trading (long) or getting stopped out (short). **Rejected on both bots.**
Report: `logs/stock_mean_reversion_experiment_20260828.md`. Suite 799→819.

**PLTR note:** do NOT promote a strategy on the strength of one symbol out of a screened
batch clearing a threshold — that's the exact selection bias
[[expert-practices-benchmark]]'s DSR/CSCV discussion covers. If mean reversion is ever
revisited, PLTR's lone pass is not evidence.
