---
name: multi-symbol-validation
description: "Symbol ranking, fee constraint, and expansion decisions from 2026-06-11 multi-symbol backtest. 2026-08-20 addendum: live investigation into BTC/CAD's 7-week zero-BUY drought confirms and extends the original 'BTC is weak now' finding — MTF daily-trend veto data, blocked-gate distribution, real price characterization. 2026-08-24 addendum: SOL's ATR×2.0 OOS-HOLDS result re-confirmed with dollar-risk-capped position sizing applied — precondition #6 (SL-distance-based sizing) specifically exercised for SOL for the first time; SOL remains blocked on capital and BTC/CAD's own fill-count gate regardless."
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

## SOL/CAD SL-distance-based sizing precondition — 2026-08-24 (built AND validated, still blocked on unrelated preconditions)

**Task framing corrected first — important.** The request that triggered this session
described SL-distance-based position sizing as "the unmet precondition" for SOL/other
ATR-stop symbols. That's stale: `config.calc_trade_qty_atr_risk()` — the exact standard
formula (`position_size = risk_budget / stop_distance`, `min()`-capped against flat notional
so a wider stop sizes DOWN, never up) — was already built generically and confirmed
symbol-agnostic on **2026-07-21**, and has been **live for BTC/CAD since 2026-07-17**
(`ATR_SIZING_ENABLED=true`). No new sizing logic was written this session. CLAUDE.md's
"Preconditions for any USD pair promotion" list #6 already said this before today; today's
work didn't change that fact, it exercised it against SOL specifically for the first time.

**What was actually missing:** the mechanism existed, but had never been *combined* with
SOL's own 2026-07-17 ATR×2.0 OOS-HOLDS result. Read `atr_oos_validation.py` (the script that
produced that HOLDS result) and confirmed it calls `bot.backtest.engine.run()` without
`atr_risk_sizing=True` — the engine has supported this flag since 2026-07-17 too (implements
the identical formula), the script just never passed it. So SOL's validated ATR-stop edge had
only ever been tested with flat notional sizing — the exact gap the original task description
was worried about, just one level more specific than "the sizing mechanism doesn't exist."

**Fix: added an opt-in `ATR_RISK_SIZING` env flag to `atr_oos_validation.py`** (default off —
a bare re-run reproduces the original 2026-07-17 methodology unchanged), wiring the existing
engine parameter through. No change to `bot/strategy/*`, `config.py`, or any live `.env` value.

**BTC/CAD regression check (first, before touching SOL):**
- Canonical fingerprint (`EXCHANGE=binance SYMBOL=BTC/USDT python backtest.py`) reproduced
  **exactly**: 31 trades, PF 2.19, hash `b30f2f9e769c8d41` — this command already runs with
  `atr_risk_sizing=True` baked in via `engine_kwargs_from_cfg` (reads `ATR_SIZING_ENABLED`
  from live `.env`), so this alone is the honest "does BTC's already-sizing-inclusive live
  behavior still reproduce" check.
- Same-window OOS split, sizing on vs. off (isolating the sizing effect from data drift):
  TRAIN PF 1.77→1.73, VALIDATION PF 3.61→4.14 — same trade counts, same win/SL rates, sizing
  barely perturbs BTC's numbers either direction. Consistent with BTC's ATR-implied stop
  distance sitting close to its flat-notional baseline most of the time (same pattern found
  for most of the stock bot's "PASS" symbols in the 2026-08-24 AMD sizing investigation —
  sizing only bites hard when a symbol's realized volatility runs well above the baseline).

**SOL/USDT test — still HOLDS with sizing applied:**
- Same-window comparison (2024-05-13 → 2026-08-24, matching the current live data, not the
  exact 2026-07-17 window — some natural edge decay visible in both sized and unsized variants
  vs. the original report, expected with 5 more weeks of data): unsized TRAIN PF 1.27 /
  VALIDATION PF 1.38 → **sized TRAIN PF 1.32 / VALIDATION PF 1.46**, same trade count (37/27
  train, 26/21 validation) in both. Sizing did not clip SOL's edge — if anything it improved
  PF slightly here, since SOL's higher-ATR entries getting sized down apparently didn't
  disproportionately hit its winning trades.
- **Both TRAIN and VALIDATION clear the PF≥1.2 gate, but narrowly** — this is not a wide-margin
  pass. Full detail: `logs/atr_oos_SOL_2.0_sized_20260824.md` (also
  `logs/atr_oos_BTC_2.0_sized_20260824.md`, `logs/atr_oos_SOL_2.0_20260824.md` for the
  same-day unsized comparison baseline).

**Conclusion — restated explicitly so this isn't misread:** precondition #6 is satisfied, both
generically (already was, since 2026-07-21) and now specifically for SOL's ATR-stop
combination (new as of today). **This does not unblock SOL.** Preconditions #2 (BTC/CAD's own
live gate: ≥15 fills + PF≥1.2, currently 0/15 — BTC/CAD has never traded since this precondition
list was written) and #3 (capital ≥$500 CAD for the new symbol slot, currently ~$146 available)
remain separately, entirely unmet, and neither this session's work nor the OOS HOLDS result
changes either of them. No `.env` or `UNIVERSE_WHITELIST` change was made.

## Decisions

**Do not change symbols until fee fix is proven.**
Running ETH on Kraken at 0.80% would still be net-negative. The fee is the problem, not the symbol. Confirm the fee-dict structure and find a path below 0.20% before any expansion.

**How to apply:** When planning multi-symbol expansion, lead with ETH. Do not include LINK. Treat SOL/BNB as second phase after ETH is validated live. Never add a symbol just because it performed well in the recent backtest window — check full-history PF first.
