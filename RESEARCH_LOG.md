# Trading Bot Research Log
BTC/USDT · 4H · Binance · 10,000 candles (2021-11-21 → 2026-06-14)

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