# Trading Bot Research Log
BTC/USDT · 4H · Binance · 10,000 candles (2021-11-21 → 2026-06-14)

---

> ## ⚠️ SUPERSEDED — do not treat this file as current
>
> **Confirmed 2026-08-24** (during the 2026-08-18 missed-BUY-signal investigation — see
> `.memory/decisions/2026-08-18-missed-buy-signal.md`): this file is a standalone research
> pass (created 2026-06-14, last edited 2026-07-05, per `git log --follow`) that was **never
> wired into the live system**. Its "FROZEN — do not change without new research" config block
> below (notably `ADX_THRESHOLD=25.0`, `STOP_LOSS_PCT=0.03`, `TAKE_PROFIT_PCT=0.06`) does not
> match — and, as far as the documented project history shows, never matched — any version of
> what has actually run live. It also predates two strategy-code changes that materially affect
> signal generation (Mode A/B entry-parameter wiring, 2026-07-20; the self-referential
> ATR-regime-baseline fix, 2026-08-20), so even its own backtest numbers can no longer be
> reproduced against current code.
>
> **The actual validated/live config is documented in `CLAUDE.md`'s "Active .env settings" and
> "Current Live Configuration" sections** — currently `ADX_THRESHOLD=18.0` (chosen by a
> documented sweep of 18/25/30/35 in `CLAUDE_HISTORY.md`, around 2026-06-27–07-02, which
> predates this file's own last edit) — and cross-checked directly against the live `.env` and
> `config.cfg` at investigation time. Kept here, not deleted, as a historical record of a
> research pass that was run but not adopted.

---

## Final System Classification

```
BTC regime-trend capture system
with fixed-volatility assumptions
and medium-frequency mean-reversion exits
```

Not: universal crypto strategy, predictive indicator system, momentum system.
Not: broken — it is precisely characterised.

---

## Validated Configuration (FROZEN — do not change without new research)

```dotenv
EXCHANGE=kraken
SYMBOL=BTC/CAD
STOP_LOSS_PCT=0.03
TAKE_PROFIT_PCT=0.06
REGIME_EMA_PERIOD=200
REGIME_EMA_SLOPE_FILTER=False
RSI_FILTER_ENABLED=True
ADX_THRESHOLD=25.0
COOLDOWN_TICKS=6
```

### Full-cycle performance (10,000 candles BTC/USDT, 2021–2026)
- Profit factor:  1.41
- Sharpe:         0.42
- Total return:   +3.20%
- Max drawdown:   -1.92%
- Trades:         86
- Worst trade:    -$0.31

### Bear period (2021-11 → 2023-05, with regime filter)
- Profit factor:  1.04
- Sharpe:         -0.14
- Return:         -0.33%

### Bull period (2024-03 → 2026-06)
- Profit factor:  1.49
- Sharpe:         0.73
- Return:         +3.69%

---

## Confirmed Findings

| # | Finding | Evidence | Status |
|---|---|---|---|
| 1 | 3% SL better than 2% and 4% | PF peaked at 3% in monotonic sensitivity test | ✅ Confirmed |
| 2 | 6% TP better than 4% and no TP | PF 1.49 at 6% vs 1.41 at 4% vs 1.06 at no TP | ✅ Confirmed |
| 3 | EMA200 regime filter essential | Bear PF 0.81→1.04, full cycle Sharpe -0.79→+0.42 | ✅ Confirmed |
| 4 | RSI+ADX act as noise suppressors | Removing them: PF 1.41→1.11, Sharpe 0.42→-0.04 | ✅ Confirmed |
| 5 | Fixed TP is load-bearing | Removing TP: PF 1.49→1.06, return went negative | ✅ Confirmed |
| 6 | System survives bear markets only with regime filter | Without filter: bear PF 0.81. With: 1.04 | ✅ Confirmed |
| 7 | EMA200 slope filter hurts performance | PF 1.41→1.18, Sharpe 0.42→0.06 — removes early entries | ❌ Rejected |
| 8 | Strategy generalises to ETH/USDT | ETH PF 0.63, Sharpe -0.86 — system loses money | ❌ Rejected |

---

## Key Structural Insights

### 1. Attribution ≠ Filter Value
RSI and ADX show weak winner/loser separation in attribution analysis but removing them destroys
the edge. They prevent bad entries from being taken, not identify good ones.

### 2. The edge is early regime entry, not trend confirmation
- Slope filter (confirmation): hurt performance — removes early profitable entries
- Regime filter (direction):   essential — defines which environment to trade
- The system rewards correct regime identification + early entry, not patience

### 3. Fixed TP defines the profit window
The edge is captured in a bounded 6% profit window. Winners that run past 6% tend to
give back gains before the strategy signal exits. The TP is not capping profits — it is
locking in drift before mean reversion.

### 4. Time-in-trade is the strongest structural signal
Winner hold time consistently 60-170% higher than loser hold time across all tests.
Winners survive and develop. Losers decay quickly. This is survivorship, not prediction.

### 5. BTC-specific, not universal crypto
The system implicitly encodes BTC's volatility and trend structure:
- 3% SL fits BTC 4H noise distribution
- 6% TP matches BTC trend continuation amplitude
- EMA200 works because BTC trends are persistent and smooth
ETH violates all four assumptions simultaneously (faster whipsaws, more fake regime flips).

---

## Rejected Hypotheses

| Hypothesis | Test | Outcome | Decision |
|---|---|---|---|
| 2% SL is optimal | Tested 2%, 3%, 4% | 3% peaks monotonically | Rejected |
| Fixed TP is unnecessary | Removed TP entirely | PF and return went negative | Rejected |
| RSI+ADX add no value | Removed both filters | Performance degraded materially | Rejected |
| Bear market filter not needed | Tested without regime EMA | PF 0.81 in bear period | Rejected |
| EMA slope filter improves quality | Added slope > 0 condition | Removes early entries, hurts PF | Rejected |
| Strategy works on ETH | Same params on ETH/USDT | PF 0.63, deeply negative Sharpe | Rejected |

---

## Live Bot Status

- Running on:    Kraken BTC/CAD
- Cash:          ~$99.81 CAD
- Position:      None (IDLE)
- Regime:        BEAR (price ~$90k < EMA200 ~$96.7k, gap -6.6%)
- EMA velocity:  ~-$411/day (converging toward price)
- Est flip:      2-4 weeks
- Action:        WAITING — cash protected

### Monitoring
Run daily: `python3 check_regime.py`
Watch for: `Regime: BULL` → bot will start evaluating BUY signals

---

## Research Phase: COMPLETE

This version of the system is fully characterised. Further parameter tuning risks overfitting.

### Next evolution (future research phase — not now)
ATR-based volatility normalisation: replace fixed % SL/TP with ATR multiples.
This would make the system potentially cross-asset (ETH, SOL, etc).
Prerequisite: collect 2-3 months of live BTC/CAD data first to validate
that live behavior matches backtest expectations.

### Decision rule for reopening research
Only reopen if ONE of these occurs:
1. Live regime flips to BULL and first 5 trades show PF < 0.8 (strategy not working live)
2. Live trades accumulate to 20+ and metrics diverge materially from backtest
3. BTC market structure changes fundamentally (e.g. new regulatory regime)
---

## Session-Edge Experiment (2026-07-05)

Question: is the edge concentrated in specific entry sessions?
Two pre-registered hypotheses, coarse buckets, in-sample (2024-03-07→2026-06-20,
pinned canonical window) + OOS (2019–2021). Post-hoc trade removal.
Harness validated: baseline reproduced the fingerprint exactly (39 trades, PF 1.77).
Full report: logs/session_edge_experiment_20260705.md

| Hypothesis | In-sample (39 trades) | OOS (110 trades) | Verdict |
|---|---|---|---|
| H1 weekend entries underperform | blocked n=7 PF 0.03 vs kept PF 2.18 | blocked n=36 PF 1.47 vs kept PF 1.85 | **INSUFFICIENT DATA** — 7 < 8-trade floor; OOS weekend still profitable (PF 1.47), so the "block weekends" strength did not replicate |
| H2 overnight (00/04 UTC) entries underperform | blocked PF 1.96 vs kept 1.66 | blocked PF 2.14 vs kept 1.55 | **NOT SUPPORTED** — direction reversed: overnight entries are fine, even better. Hypothesis killed. |

Notes:
- The in-sample weekend number (PF 0.03 on 7 trades) is exactly the seductive
  small-sample mirage the pre-registered floor exists to catch. Both periods DO
  agree weekend < weekday directionally — logged as a watch item, below action
  threshold. Revisit only when the sample grows (live fills or a longer window);
  do NOT tune toward it.
- No live change. No strategy-file change. Hash 659d1c03987b72fd untouched.
- Next queued hypothesis (not yet run): volatility-regime conditioning —
  do winners concentrate in a measurable ATR/ADX band at entry?

---

## Volatility-Regime Experiment (2026-07-05)

Question: do winners concentrate in an ADX / ATR band at entry?
Pre-registered: H1 weak-trend (ADX 18–25 entries underperform), H2 excess-vol
(ATR% > 1.5% stop distance entries underperform). Same windows and criteria as
the session experiment. Baseline again reproduced the fingerprint (39 / PF 1.77).
Full report: logs/vol_regime_experiment_20260705.md

| Hypothesis | In-sample | OOS | Verdict |
|---|---|---|---|
| H1 weak-trend (ADX<25) | blocked n=18 PF 1.02 vs kept PF 2.48 — suggestive | REVERSED: blocked 1.86 vs kept 1.62 | **NOT SUPPORTED** — in-sample pattern did not survive OOS (ATR-alpha failure mode, caught again) |
| H2 excess-vol (ATR%>1.5%) | REVERSED in-sample: blocked 2.19 vs kept 1.62 | blocked 1.64 vs kept 2.17 | **NOT SUPPORTED** — high-ATR entries are fine on BTC |

Meta-conclusion after 3 experiments / 4 hypotheses (session ×2, regime ×2):
**BTC edge appears uniform** — it does not hide in a session or volatility
pocket. Good for robustness (no fragile conditioning), bad for free PF gains:
filter-hunting on BTC looks exhausted with current sample sizes. Remaining
levers, in order: live fill accumulation (capital gates), fee optimization
(already maximized), capital. Next research should target the actual failure
(alt entry quality), not further BTC conditioning.
