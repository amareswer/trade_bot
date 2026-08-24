---
name: swing-1d-validated
description: "1d swing strategy walk-forward results and next steps — SL/TP re-derived 2026-08-24 (now 3%/20%, was 4%/25%); still PARTIAL not PASS on Val_2 sample size"
metadata:
  type: project
---

1d swing strategy — SL=4% TP=25% ADX=18 RSI_FILTER=true originally validated via walk-forward
on 2026-06-23; SL/TP **re-derived to 3%/20% on 2026-08-24** against current strategy code
(ADX=18/RSI_FILTER=true/cooldown=3/fee=0.8% unchanged — see the 2026-08-24 sections below for
why). **Current status is PARTIAL, not PASS** — read the 2026-08-24 update before trusting
anything above this line; it's kept for historical record only.

**Why:** Full OOS validation before any capital allocation — confirmed edge is not a bull-market artifact.

**How to apply:** Do NOT activate with real capital yet, and do NOT start the paper observation
either until a walk-forward actually clears all 3 windows — it hasn't, twice now (2026-08-24).
See the 2026-08-24 sections below for current state and what's still open.

## Walk-forward results, ORIGINAL — 2026-06-23 (BTC/USDT 1d, fee=0.8%, cash=$10k)

**Superseded — does not reproduce on current strategy code, see 2026-08-24 below. Kept for
historical record only.**

| Period           | Candles | Trades | PF   | Return% | MaxDD% | Verdict |
|------------------|---------|--------|------|---------|--------|---------|
| Train 2017–2022  | 1963    | 29     | 2.67 | +8.35%  | -3.76% | PASS    |
| Val_1 2023–mid24 | 547     | 8      | 2.30 | +1.50%  | -1.41% | PASS    |
| Val_2 mid24–now  | 723     | 5      | 1.54 | +0.06%  | -2.21% | PASS    |

**Conclusion (2026-06-23, superseded):** VALIDATED — edge holds out-of-sample across all regimes.

## Active config — ORIGINAL, 2026-06-23 (superseded, see 2026-08-24 below)
- SL=4%  TP=25%  ADX=18  RSI_FILTER=true  cooldown=3  fast_ema=9  slow_ema=21
- fee=0.8%  cash=$10k  risk_per_trade=10%  regime_enabled=true (this field no longer exists in
  `bot.backtest.engine.run()`'s signature — see 2026-08-24 dead-key fix below)

## Status (2026-06-23, superseded — see 2026-08-24 update below)
- Paper-trade candidate (not live) as of 2026-06-23
- Val_2 only 5 trades — low count, watch for stability as more 1d signals accumulate
- Script: `python swing_walkforward.py` (reproduces results, does not touch .env)

---

## 2026-08-24 update — paper observation never started; re-validation now PARTIAL, not PASS

**Task:** determine whether the 4-week paper-trade observation (the documented next step above)
had ever been run, and start it if not.

**Finding 1 — never started, no scaffold existed.** Searched `.memory/`, `CLAUDE_HISTORY.md`,
`bot/main.py`, `.env`/`.env.example`, and the whole repo for anything resembling a live swing
paper loop, state file, or trade log. Nothing — `swing_walkforward.py`/`swing_backtest.py`/
`swing_atr_walkforward.py` are one-shot batch scripts only, no persistence, no loop.
`ops/crontab.txt` (empty by design — cron was abandoned on this machine 2026-07-14, macOS TCC
blocks it from `~/Desktop`) confirms even a naive scheduled-job attempt was never viable here.
This wasn't abandoned mid-run; there was simply nothing built yet.

**Finding 2 — `swing_walkforward.py`'s FIXED dict no longer ran at all.** Before building
anything, tried to reproduce the original 2026-06-23 numbers as a sanity check. 5 of `FIXED`'s
keys (`regime_enabled`, `bb_period`, `bb_std_dev`, `mr_rsi_oversold`, `mr_rsi_overbought`) are
no longer accepted by `bot.backtest.engine.run()` — a `TypeError`, not a values mismatch;
leftovers from an older engine signature (an older regime on/off flag, a mean-reversion mode
that no longer exists). Removed them from `swing_walkforward.py`'s `FIXED` dict (the 6 real
trading params this strategy is defined by — SL/TP/ADX/RSI-filter/cooldown/fee — are untouched,
byte-identical to 2026-06-23).

**Finding 3 — even after that fix, the walk-forward no longer reproduces the 2026-06-23
result, and no longer PASSes.** Same underlying data (Train/Val_1 candle counts match the
original exactly — 1963/547 — ruling out a data-fetch difference) but materially different
trade counts and PF once re-run today:

| Period | 2026-06-23 (original) | 2026-08-24 (re-run) |
|--------|------------------------|------------------------|
| Train  | 29 trades, PF 2.67, PASS | 21 trades, PF 2.99, PASS |
| Val_1  | 8 trades, PF 2.30, PASS  | 5 trades, PF 2.08, PASS |
| Val_2  | 5 trades, PF 1.54, PASS  | **3 trades, PF 3.06, FAIL** (too few trades to judge — the script's own `_verdict()` requires ≥5 regardless of PF) |

`swing_walkforward.py`'s own final verdict, printed by the (now-fixed) script itself:
**"PARTIAL: Edge degraded in recent regime. Do not activate."**

Root cause: `bot/strategy/indicator_strategy.py` has genuinely changed since 2026-06-23 — known
candidates already on record elsewhere in this repo: the 2026-07-20 Mode A/B entry-parameter
wiring fix, and the 2026-08-20 self-referential-ATR-regime-baseline fix (which alone moved the
*4h* strategy's trade count 32→31). Per this repo's own existing rule (CLAUDE.md's "Validation
Discipline": a strategy-code change invalidates prior validation until walk-forward is re-run) —
**the 2026-06-23 conclusion ("VALIDATED — edge holds out-of-sample") no longer holds.** Status
is now correctly **PARTIAL**, not PASS: Train and Val_1 still clear the bar; Val_2 doesn't have
enough trades to judge either way (3, not the 5 required) — not proven to have failed, just not
proven to still hold.

**Decision, confirmed with the user at each step before proceeding:**
1. Confirmed: re-validate on current code before deciding whether to build anything (not decided
   unilaterally — the user chose this over "start anyway, flag the gap" or "hold off entirely").
2. Given the PARTIAL result, **the paper-trade observation was NOT started** — the user's own
   prior instruction was explicit: start only if the re-validation still PASSes.

**What was built anyway, and is ready whenever a future re-validation actually passes:**
`swing_paper_trade.py` (repo root) — a standalone paper-trading loop for the 1d swing strategy,
modeled on `stock_bot/fast_validator.py`'s isolation pattern (own state `logs/swing_state.json`,
own trade log `logs/swing_trades.csv`, never touches `logs/live_state_BTC_CAD.json`/
`logs/risk_state.json`/`trades.db`, never imports `bot/main.py`). Design: re-runs
`bot.backtest.engine.run()` — the exact code path `swing_walkforward.py` already uses — against
freshly fetched Binance BTC/USDT 1d candles once/day (sleeps to the next UTC-midnight+buffer,
not a busy loop), using `FIXED` imported directly from `swing_walkforward.py` (never re-typed,
so it can't silently drift from whatever IS validated at the time it's eventually started).
New fills are detected by timestamp comparison against the last one already logged — robust to
the data fetcher's rolling window eventually dropping old candles. Verified working end-to-end
(`--once` mode, real Binance fetch, real engine run) before this write-up; its own test-run
output (39 completed round-trips, the full backtest history since 2018) was then deleted from
`logs/swing_state.json`/`logs/swing_trades.csv` rather than left in place, since it reflects
historical backtest fills, not real forward paper-trading data, and leaving it would misrepresent
the observation as already having run. It sits inert until manually started — not started by
this session, no scheduled task references it, does not run automatically at any point.

**Not done, and deliberately not decided here:** whether/how to get Val_2 to a judgeable sample
size (wait for more live BTC data, shrink the window, or accept a longer combined recent-period
check) is a fresh decision for whenever this is revisited — not resolved as part of this pass.

**Verification:** full test suite 629 passed (unchanged — no existing tests touch
`swing_walkforward.py`/`swing_paper_trade.py`, consistent with every other standalone
research/validation script in this repo, e.g. `stock_backtest.py`/`validate_atr_sizing.py`,
none of which have dedicated pytest coverage either). `bot/strategy/*` and
`build_indicator_config()` untouched — confirmed via `git diff --stat` (empty) and
`bot.strategy.fingerprint.compute_strategy_hash()` (`b30f2f9e769c8d41`, unchanged). The live 4h
bot itself (`bot/main.py`) was not touched at all. Full narrative: CLAUDE_HISTORY.md, 2026-08-24
entry.

---

## 2026-08-24 update (second pass, same day) — SL/TP re-derived against current strategy code; still PARTIAL, not PASS

**Task:** the update above found the strategy's original 2026-06-23 SL/TP (4%/25%) no longer
PASSes cleanly on current code — but that pass reused the STALE 2026-06-23 values as-given,
which is exactly the assumption that broke. This pass re-derives SL/TP from scratch against
current `bot/strategy/indicator_strategy.py`, then re-validates the result.

**Step 1 — `swing_backtest.py` had the identical dead-key issue `swing_walkforward.py` had.**
Same 5 keys (`regime_enabled`, `bb_period`, `bb_std_dev`, `mr_rsi_oversold`,
`mr_rsi_overbought`) — confirmed by running it first (`TypeError`), then removed only those
from its `FIXED` dict too. Sweep ranges (6 SL/TP combinations, unchanged from 2026-06-23) and
all other logic in the script untouched.

**Step 2 — fresh SL/TP sweep on current code, full result table:**

| SL% | TP% | Trades | Win% | PF | MaxDD% | Return% | Verdict |
|-----|-----|--------|------|-----|--------|---------|---------|
| 2%  | 10% | 58     | 24.1% | 1.59 | -6.26% | -4.11% | PASS |
| 3%  | 15% | 47     | 27.7% | 1.89 | -3.40% | +1.57% | PASS |
| **3%** | **20%** | **42** | **26.2%** | **2.34** | **-4.56%** | **+5.83%** | **PASS (best PF)** |
| 4%  | 20% | 40     | 32.5% | 2.21 | -4.83% | +6.79% | PASS |
| 4%  | 25% | 39     | 33.3% | 2.29 | -4.83% | +7.40% | PASS (old default, now 2nd) |
| 5%  | 25% | 38     | 34.2% | 1.90 | -5.17% | +5.34% | PASS |

All 6 candidates now PASS the sweep's own gate (PF≥1.3, trades≥10) — a genuine improvement
over the 2026-06-23 sweep, which had several MARGINAL results at much higher trade counts
(e.g. 2%/10% was 83 trades/PF 1.30/MARGINAL back then, now 58 trades/PF 1.59/PASS) — consistent
with the strategy becoming pickier (fewer, higher-quality entries) since the 2026-07-20/
2026-08-20 fixes, not just noisier. **New best: SL=3%/TP=20% (PF=2.34, 42 trades)** — the old
SL=4%/TP=25% default is now second-best (PF=2.29), a real, non-trivial change in the winning
config, not a tie. ADX/RSI-filter/cooldown/fee left unchanged — no reason tied to the known
strategy-code changes (Mode A/B wiring, ATR-regime-baseline fix) was found to revisit those;
only SL/TP (pure exit parameters, most directly downstream of how the strategy's
now-different price paths get walked) were re-derived.

**Step 3 — updated `swing_walkforward.py`'s `FIXED['stop_loss_pct']`/`['take_profit_pct']` to
3%/20%** (the new winner) and re-ran the full 3-window walk-forward:

| Period | Candles | Trades | PF | Return% | MaxDD% | Verdict |
|--------|---------|--------|-----|---------|--------|---------|
| Train 2017–2022  | 1963 | 22 | 2.48 | +3.57% | -4.56% | PASS |
| Val_1 2023–mid24 | 547  | 5  | 4.35 | +2.26% | -1.43% | PASS |
| Val_2 mid24–now  | 785  | **3** | 3.28 | +0.89% | -1.11% | **FAIL** (< 5-trade minimum) |

Script's own verdict: **"PARTIAL: Edge degraded in recent regime. Do not activate."** — same
overall status as before re-deriving, but now on the correct (re-derived, not stale) params.

**Val_2's shortfall is SL/TP-independent — confirmed, not assumed.** The old SL=4%/TP=25%
walk-forward (previous pass, same day) also produced exactly 3 Val_2 trades. Entry frequency
in this window is governed by the ADX≥18/RSI-filter/Mode-A/B *entry* logic, not by the SL/TP
*exit* parameters this sweep varies — so no candidate in the swept range would plausibly clear
5 trades in Val_2. Per the task's explicit instruction, this is reported plainly and NOT
worked around by shrinking the window, extending Val_2's end date, or lowering the 5-trade bar.
**Stopped here, as instructed.**

**`swing_paper_trade.py`'s FIXED import — no code change needed.** It imports `FIXED` directly
from `swing_walkforward.py` (never re-typed), so it already reflects SL=3%/TP=20% automatically
— confirmed by direct import check. Its module docstring was updated to state the current
status accurately (built, not started, reads live SL/TP from `swing_walkforward.py`) rather
than the previous pass's now-stale framing. **Still not started** — the walk-forward is still
PARTIAL, and the user's standing condition (start only on a clean PASS) hasn't been met by
either the stale-param or the re-derived-param attempt.

**What remains genuinely open, not decided here (per the task's explicit instruction not to
force this):** whether/how to eventually get Val_2 to a judgeable sample size — waiting for
more live BTC data to accumulate, revisiting the window boundaries as a deliberate future
decision (not a today's-workaround), or accepting the strategy stays unvalidated for
paper-trading purposes until entry frequency naturally increases.

**Verification:** full test suite **629 passed**, unchanged (same reasoning as the prior
entry — no test touches these standalone scripts). `bot/strategy/*` and
`build_indicator_config()` untouched (`git diff --stat` empty on both), `bot/main.py`
untouched, strategy hash `b30f2f9e769c8d41` confirmed unchanged. Files touched this pass:
`swing_backtest.py` (dead-key fix only), `swing_walkforward.py` (dead-key fix from the prior
pass + SL/TP values updated to the re-derived winner + 3 stale hardcoded "SL=4%/TP=25%"
display strings made dynamic), `swing_paper_trade.py` (docstring only, no logic change). Full
narrative: CLAUDE_HISTORY.md, 2026-08-24 second entry.
