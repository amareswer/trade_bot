---
name: timeframe-4h-validated
description: 1h timeframe rejected (PF 0.49); 4h locked as the only validated live timeframe
metadata:
  type: project
---

Decision: 4h candles only for live trading
Date: 2026-06-22
Final: yes — do not reopen without full re-validation

**Why:**
1h backtest (Nov 2025–Jun 2026, 5000 candles): PF 0.49, return -25.42%, zero TPs fired.
TP of 10% never triggers on 1h candles — BTC doesn't move 10% per hour under normal conditions.
RSI attribution flipped vs 4h: winners had LOWER RSI than losers (opposite of expected).
Strategy parameters (TP, RSI, ADX) were tuned for 4h multi-day momentum moves.

4h backtest (Mar 2024–Jun 2026, 5000 candles, corrected 0.8% fee): PF 1.78, 61 trades.
5 walk-forward windows all PF > 1.0.

**How to apply:**
CANDLE_MINUTES must always be 240 on live bot. Any proposal to run 1h requires a
full re-sweep of TP/ADX/RSI for that timeframe before touching live config.
