---
name: swing-1d-validated
description: "1d swing strategy SL=4% TP=25% walk-forward results and next steps"
metadata:
  type: project
---

1d swing strategy (SL=4% TP=25% ADX=18 RSI_FILTER=true) validated via walk-forward on 2026-06-23.

**Why:** Full OOS validation before any capital allocation — confirmed edge is not a bull-market artifact.

**How to apply:** Do NOT activate with real capital yet. Run 4-week paper observation alongside
live 4h bot before promoting. See [[progress-current]] for next steps.

## Walk-forward results (BTC/USDT 1d, fee=0.8%, cash=$10k)

| Period           | Candles | Trades | PF   | Return% | MaxDD% | Verdict |
|------------------|---------|--------|------|---------|--------|---------|
| Train 2017–2022  | 1963    | 29     | 2.67 | +8.35%  | -3.76% | PASS    |
| Val_1 2023–mid24 | 547     | 8      | 2.30 | +1.50%  | -1.41% | PASS    |
| Val_2 mid24–now  | 723     | 5      | 1.54 | +0.06%  | -2.21% | PASS    |

**Conclusion:** VALIDATED — edge holds out-of-sample across all regimes.

## Active config (swing_walkforward.py FIXED dict)
- SL=4%  TP=25%  ADX=18  RSI_FILTER=true  cooldown=3  fast_ema=9  slow_ema=21
- fee=0.8%  cash=$10k  risk_per_trade=10%  regime_enabled=true

## Status
- Paper-trade candidate (not live) as of 2026-06-23
- Val_2 only 5 trades — low count, watch for stability as more 1d signals accumulate
- Script: `python swing_walkforward.py` (reproduces results, does not touch .env)
