---
name: multi-symbol-validation
description: "Symbol ranking, fee constraint, and expansion decisions from 2026-06-11 multi-symbol backtest. 2026-08-20 addendum: live investigation into BTC/CAD's 7-week zero-BUY drought confirms and extends the original 'BTC is weak now' finding — MTF daily-trend veto data, blocked-gate distribution, real price characterization."
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

## BTC/CAD live signal drought — 2026-08-20 investigation (extends "Why BTC is weak now")

The 2026-06-11 finding above ("very high EMA spread rejection ~69%, low ADX... BTC is the
wrong instrument right now") held up under a full live-data investigation two months later:
BTC/CAD produced **zero live BUY signals for 7 weeks straight** (2026-06-30 → 2026-08-20).
Verdict: genuinely choppy/unfavorable regime, not a misconfigured gate or mechanical fault —
confirmed, not assumed, against `logs/live_signals.csv`, real independently-fetched Kraken
price data, the candle-watchdog/risk-breaker logs, and the strategy source itself.

**Blocked-gate distribution (144 BUY-considered-then-blocked candles, `logs/live_signals.csv`,
2026-07-02 → 2026-08-20):** ADX<18 threshold 52 (36%), "regime" 43 (30%, of which 36 =
`REGIME_EMA_PERIOD=200` macro filter, 5 = genuine VOLATILE/ATR-spike, 2 unresolved),
EMA_spread 24 (17%), MACD 22 (15%), trend 2, RSI 1. The binding gate **shifts week to week**
with measured ADX — high-ADX weeks get blocked by `regime`/EMA_spread/MACD, low-ADX weeks get
blocked by ADX itself — consistent with genuine chop rotating which single condition fails,
not one gate stuck misfiring.

**Real price action (fetched fresh from Kraken, independent of the bot's own logs), June 1 →
Aug 20:** net move **−2.5%** despite a **22.8% high-low range** — textbook ranging signature.
Walk-forward ADX(14): mean 26.8, but 22% of all readings below the 18 threshold, weekly means
swinging 18.7–36.9. Not dead-flat, not cleanly trending either — exactly the kind of stretch a
trend-following, walk-forward-validated strategy is expected to sit out.

**The one MTF (multi-timeframe) daily-trend veto, 2026-08-18 12:00 UTC:** the strategy's ONLY
raw BUY signal in the entire 7-week window (price $90,042, valid 4h Mode A/B setup, price
+0.59% *above* the 200-EMA so the regime_ema macro filter did NOT block it, `regime=TRENDING`
not VOLATILE) was vetoed by the separate MTF daily-trend gate in `bot/main.py`:
`MTF gate [BTC/CAD]: BUY suppressed — daily trend BEARISH`. The 4h leg turned bullish inside a
daily chart still reading bearish — the gate did exactly its documented job (filtering a
probable false start within a larger range), not a bug. Worth remembering as the one concrete
moment anything got close, but it's one candle out of hundreds — doesn't change the "genuinely
unfavorable regime" verdict on its own.

**Mechanically clean throughout:** zero candle-watchdog stale-feed events in either direction
(feed never went stale — confirmed via log grep, not assumed). No risk-manager breaker ever
engaged (`halt=False` at every restart across the window, `kill_switch_tripped: false`,
`peak_value` unchanged at $77 — no drawdown to speak of, consistent with zero trades). The
known Kraken auth outage (2026-08-11→15, already documented elsewhere) only affected
authenticated balance/position sync, never the public candle feed signals are generated from,
and didn't overlap the one real BUY attempt (which was Aug 18, after that incident resolved).

**How to apply:** if BTC/CAD goes quiet again for an extended stretch, this is the checklist
that was actually run (blocked-gate tabulation from `live_signals.csv` + independent price
refetch + watchdog/breaker log grep) — re-run it before assuming either "the strategy is
broken" or "nothing to see here." A shifting blocked-gate bottleneck tracking real measured
ADX is the signature of genuine chop; a gate stuck on one label regardless of market
conditions would be the signature of an actual misconfiguration.

## Decisions

**Do not change symbols until fee fix is proven.**
Running ETH on Kraken at 0.80% would still be net-negative. The fee is the problem, not the symbol. Confirm the fee-dict structure and find a path below 0.20% before any expansion.

**How to apply:** When planning multi-symbol expansion, lead with ETH. Do not include LINK. Treat SOL/BNB as second phase after ETH is validated live. Never add a symbol just because it performed well in the recent backtest window — check full-history PF first.
