---
name: fee-structure
description: Real Kraken fee is 0.8% taker; limit BUY saves ~0.64% per round trip; backtest fee corrected
metadata:
  type: project
---

Decision: limit orders for BUY, market for SELL
Date: 2026-06-22
Final: yes

**Fee facts:**
- Kraken taker fee: 0.80% per side = 1.60% per round trip
- Kraken maker fee: ~0.16% per side
- Limit BUY + market SELL = ~0.96% per round trip (saving 0.64%)
- At 61 trades / 2 years: saves ~$17–20 on $100 capital (proportional at higher capital)

**Backtest fee was wrong until 2026-06-22:**
.env had BACKTEST_FEE_PCT=0.001 (0.1%). All prior PF numbers were optimistic.
Corrected to BACKTEST_FEE_PCT=0.008. Backtest at 0.8% fee: PF 1.78, return -22.68%.
With limit orders the return is estimated near-breakeven on the same trade count.

**Implementation:**
- ORDER_TYPE=limit in .env
- live_executor.py: BUY places limit bid at price * 0.998 (0.2% below market = passive/maker)
- SELL always market regardless of ORDER_TYPE (guaranteed exit)
- Poll window: 9s (range(1,10)) — enough time for passive bid to fill on 4h candle

**How to apply:**
Never change ORDER_TYPE=market for BUY without re-running fee math.
Never apply limit orders to SELL — exit must always be guaranteed.
