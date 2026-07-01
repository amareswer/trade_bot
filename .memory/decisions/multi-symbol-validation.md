---
name: multi-symbol-validation
description: "Symbol ranking, fee constraint, and expansion decisions from 2026-06-11 multi-symbol backtest"
metadata:
  type: project
---

**Date:** 2026-06-11
**Method:** 4h backtest, frozen strategy params, Binance data, 5000 candles (full history ~27mo) + 1250 candles (recent regime Nov25–Jun26), both at 0.1% and 0.8% fee.

## Symbol rankings

| Symbol | PF full-hist | PF recent | Verdict |
|---|---|---|---|
| ETH/USDT | 1.20 | 1.22 | **First expansion choice** — most stable across regimes |
| BNB/USDT | 1.44 | 1.34 | Watchlist — strong now, but was inflated by bull era |
| SOL/USDT | 1.07 | 1.33 | Watchlist — improving, but historically marginal |
| BTC/USDT | 1.27 | 0.45 | Weakest current-regime symbol — same dates ETH=1.22 |
| LINK/USDT | 0.95 | 0.92 | **Permanently excluded** — never profitable at any window |

**Why ETH over SOL/BNB:** SOL and BNB look strong in the recent 7-month window, but that is the exact window we're live-trading in — recency-chasing. ETH's PF has barely moved (1.20→1.22) across 27 months vs 7 months, indicating strategy signal quality is regime-insensitive on ETH. That's the right property for an expansion symbol.

**Why BTC is weak now:** BTC/CAD Nov25–Jun26 has very high EMA spread rejection (~69%) and low ADX. The trend-following signal simply isn't finding structure in BTC's recent price action. The strategy works; BTC is the wrong instrument right now.

**LINK is structurally excluded:** PF below 1.0 in both full-history and recent regime, at both fee rates. Not a fee or regime problem — the strategy's EMA/RSI/ADX combination finds no edge in LINK. Do not add LINK to any multi-symbol expansion.

## Fee constraint

Everything net-negative at 0.8% fee, regardless of signal quality (even SOL PF 1.33 → net −2.40%). Everything except BTC and LINK net-positive at 0.1%. Fee rate is the binding constraint at $100 CAD capital, not strategy quality.

**Fee levers (Kraken):**
- Maker orders (limit orders) → 0.40% vs 0.80% taker (maker rate confirmed Jun 14 live fill)
- Volume tier (30-day volume ≥ $50k USD → 0.14%)
- BTC/USD vs BTC/CAD — CAD pairs may carry FX surcharge; pending raw fee-dict from next fill
- Do NOT switch to Binance — unavailable in Canada

**Why:** 2026-06-11 first live fill returned 0.80% actual fee vs 0.26% modeled. Cause not yet confirmed (fee-dict logging added; next fill will reveal the raw ccxt response).

## Decisions

**Do not change symbols until fee fix is proven.**
Running ETH on Kraken at 0.80% would still be net-negative. The fee is the problem, not the symbol. Confirm the fee-dict structure and find a path below 0.20% before any expansion.

**How to apply:** When planning multi-symbol expansion, lead with ETH. Do not include LINK. Treat SOL/BNB as second phase after ETH is validated live. Never add a symbol just because it performed well in the recent backtest window — check full-history PF first.
