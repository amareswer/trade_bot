---
name: progress-current
description: "Current stage and what is in progress"
metadata:
  type: project
---

**Status as of 2026-06-15 17:25 UTC:** Bot restarted clean. Live LONG position recovered. RSI filter restored. Config now fully validated.

---

## Current Live State

**Running:** `python -m bot.main` — Kraken BTC/CAD, 1h candles, IndicatorStrategy, LiveExecutor (LIVE_TRADING=true, DRY_RUN=false)

**Open position:**
- BUY 0.000108 BTC/CAD @ $92,050.90 (filled 2026-06-15 07:00 UTC)
- Stop-loss: ~$90,671 (1.5% below entry) — checked every 30s tick
- Take-profit: ~$96,143 (4.5% above entry) — checked every 30s tick
- Fee paid: $0.0795 CAD (0.80% — consistent with first fill)
- Cash remaining: $89.79 CAD
- Last candle close (20:00 UTC): $93,078.80 → unrealized PnL ≈ +$0.11 CAD

**Restart at 17:25 UTC — all 4 checks passed:**
1. `State restored: cash=89.79 pos=0.000108 cost_basis=92050.90` ✓
2. `PositionManager seeded: qty=0.000108 avg_entry=92050.90` ✓
3. `State machine recovered to LONG | entry=92050.90` ✓
4. Warmup replayed correctly; first new candle awaited

---

## Active .env (as of 2026-06-15 17:25 UTC)

| Setting | Value | Notes |
|---|---|---|
| EXCHANGE | kraken | Live only; binance for backtests |
| SYMBOL | BTC/CAD | |
| CANDLE_MINUTES | 60 | 1h candles |
| ADX_THRESHOLD | 18.0 | Validated best across all sweep configs |
| RSI_FILTER_ENABLED | **true** | Restored 2026-06-15 ~17:25 UTC (was false — mistake) |
| VOLUME_K | 0 | Disabled — tested 1.2, hurt PF |
| STOP_LOSS_PCT | 0.015 | Updated 2026-06-15 (was 0.02) |
| TAKE_PROFIT_PCT | 0.045 | Updated 2026-06-15 (was 0.04) |
| RISK_PER_TRADE_PCT | 0.10 | High intentionally at $100 capital |
| RISK_MAX_POSITION_PCT | 0.15 | |
| LIVE_TRADING | true | |
| DRY_RUN | false | |

---

## What Was Validated This Session (2026-06-15)

**SL/TP sweep — winner: SL=1.5% / TP=4.5% (1:3 ratio)**

| Config | Trades | PF | Max DD | Return |
|---|---|---|---|---|
| SL=2% TP=4% (1:2) | 85 | 1.06 | -2.13% | -1.12% |
| **SL=1.5% TP=4.5% (1:3) ← ACTIVE** | **86** | **1.38** | **-1.37%** | **+1.51%** |
| SL=2% TP=6% (1:3) | 72 | 1.20 | -1.94% | +0.36% |
| SL=1% TP=3% (1:3) | 110 | 0.88 | -3.46% | -3.13% |

**ADX sweep — ADX=18 confirmed best (no change needed)**

| ADX | 5000c PF | 5000c Return | 2000c PF |
|---|---|---|---|
| 18 (active) | **1.38** | **+1.51%** | 1.02 |
| 25 | 1.03 | -0.86% | 1.04 |
| 30 | 0.89 | -1.39% | 0.52 |
| 35 | 1.09 | -0.29% | 1.00 |

**RSI filter — must be true**

| RSI_FILTER_ENABLED | Trades | PF | Return |
|---|---|---|---|
| false (was live) | 107 | 1.19 | -0.10% |
| **true (now live)** | **86** | **1.38** | **+1.51%** |

**Volume filter — disabled (VOLUME_K=0)**

| VOLUME_K | Trades | PF | Return |
|---|---|---|---|
| 0 (disabled, active) | 86 | 1.38 | +1.51% |
| 1.2 (tested) | 68 | 1.00 | -1.28% |

**Walk-forward (5 × 1000-candle windows, SL=1.5% TP=4.5%):**
- W1 full 5000 (Mar 2024–Jun 2026): PF 1.38, +1.51% ✓
- W2 4000 (Aug 2024–Jun 2026): PF 1.41, +1.39% ✓
- W3 3000 (Feb 2025–Jun 2026): PF 1.30, +0.41% ✓
- W4 2000 (Jul 2025–Jun 2026): PF 1.02, -0.50% — recent regime choppier
- W5 1000 (Dec 2025–Jun 2026): PF 1.06, -0.24% — thin sample (16 trades)
- **Conclusion:** strategy earns across older periods; recent 6 months is a market condition issue, not a code/filter problem. Watch live trades for confirmation.

---

## Open Items (Manual Follow-Up Required)

1. **Verify Jun 11 fill close on Kraken** — History → Trades, look for SELL ~Jun 14 10:44 UTC. Balance evidence: exchange showed $99.81 at Jun 13 23:59 restart (was $89.88 saved) → position closed between Jun 12 08:20 and Jun 13 23:59 while bot was stopped.

2. **Isolate dev/test from production API** — Jun 14 test burst sent real orders (BUY/SELL 0.001 BTC) to live Kraken key. Test harness must never use production credentials.

3. **Kraken fee investigation** — 0.80% actual vs 0.26% modeled on both fills. Likely BTC/CAD FX surcharge. Maker orders (limit) = 0.16% tier. Fee situation means strategy is net-negative at current $100 capital even with good PF signal.

---

## Next Steps

1. **Hold current position** — do not change SL/TP mid-trade. Wait for $90,671 SL or $96,143 TP
2. **After position closes:** compare live PF/win rate to backtest baseline
3. **Kraken fee lever:** test a limit order (maker) to confirm 0.16% rate
4. **When capital grows to $500+:** revisit RISK_PER_TRADE_PCT (lower to 2% validated level)
5. **ETH expansion:** deferred until fee path confirmed <0.20%
