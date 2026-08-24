---
name: multi-symbol-validation
description: "Symbol ranking, fee constraint, and expansion decisions from 2026-06-11 multi-symbol backtest. 2026-08-20 addendum: live investigation into BTC/CAD's 7-week zero-BUY drought confirms and extends the original 'BTC is weak now' finding — MTF daily-trend veto data, blocked-gate distribution, real price characterization. 2026-08-24 addenda: (1) SOL's ATR×2.0 OOS-HOLDS result re-confirmed with dollar-risk-capped position sizing applied; (2) fill-frequency reality check quantifies the 'BTC/CAD 15 fills' gate as an unexamined default, not a calculated one; (3) that gate REMOVED as a precondition for SOL/other new-symbol promotion — SOL now blocked on capital alone; (4) capital threshold itself corrected from $500 (wrong — Stage-3 scale-up figure) to $100 (correct — Stage-1 new-symbol figure), live balance checked ($153.39 CAD, 0 BTC), and a CapitalPool architecture constraint (no per-symbol slot cap) flagged as unresolved."
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

**Conclusion — restated explicitly so this isn't misread (updated same day, see the two
addenda directly below):** precondition #5 (renumbered — see the removal addendum) is
satisfied, both generically (already was, since 2026-07-21) and now specifically for SOL's
ATR-stop combination (new as of 2026-08-24). **This did not, by itself, unblock SOL** — at the
time this section was first written, capital AND the BTC/CAD live-fill precondition were both
still separately unmet. The BTC/CAD live-fill precondition was removed later the same day (see
below) after the fill-frequency investigation found it was never a deliberate calculation.
Capital (~$146 CAD available vs. $500 required) is now SOL's sole remaining unmet
precondition. No `.env` or `UNIVERSE_WHITELIST` change was made at any point in this file's
history.

## Fill-frequency reality check — 2026-08-24 (why the BTC/CAD 15-fill precondition got removed, below)

Investigated whether the 15-fill/PF≥1.2 capital gate — used both for BTC/CAD's own $100→$250
capital-scaling decision AND, separately, as a precondition gating any *other* symbol's
promotion to `UNIVERSE_WHITELIST` — is calibrated to reality. Read-only; this section is the
evidence base the removal decision (next section) is built on.

**Time-between-trades, current canonical config (31 trades, PF 2.19, hash
`b30f2f9e769c8d41`), full 5000-candle history, 2024-07-16 → 2026-08-18:**
- Median gap: **18.9 days**. Mean gap: **25.4 days** (pulled up by a long right tail —
  clustered, not evenly spaced). Longest observed dry spell: **94.7 days**.
- Full sorted gap list (days): 4.8, 5.5, 8.0, 8.7, 8.7, 8.7, 10.2, 10.3, 10.5, 13.0, 13.3,
  13.8, 14.7, 18.2, 18.7, 19.2, 19.5, 19.7, 21.2, 23.3, 24.7, 27.2, 32.7, 34.0, 35.5, 46.3,
  58.0, 65.0, 75.2, 94.7.

**Per-window frequency (5000/4000/3000/2000/1000-candle splits) — declining, not stable:**

| Window | Real span | Trades | PF | Avg freq |
|---|---|---|---|---|
| 5000c | 2.28yr | 31 | 2.19 | 26.9 d/trade |
| 4000c | 1.82yr | 23 | 2.00 | 29.0 d/trade |
| 3000c | 1.37yr | 14 | 2.93 | 35.6 d/trade |
| 2000c | 0.91yr | 8 | 2.48 | **41.6 d/trade** |
| 1000c | 0.45yr | 5 | 3.49 | 33.2 d/trade (thin, 5 trades) |

PF holds up fine across every window — this is not an edge-quality problem. It's a frequency
problem, and frequency has gotten *worse* in more recent windows (26.9 → 41.6 d/trade from
full-history to the most recent statistically-meaningful window), not better.

**Live data:** `logs/trades.db` — only 8 fills ever, all clustered 2026-06-12 → 2026-06-27 (3
completed round-trips in 15 days). Zero fills since. As of 2026-08-24, **65 days have elapsed
since the last live entry with zero progress toward fill #1** of the next 15 — longer than the
previously-documented "7-week drought" (which used a different start reference) and still
unresolved. No second historical live drought exists to compare it against — the bot's entire
live fill history is 15 days long, so the backtest's own per-window data above is the only
available source for "is this normal," and it says yes: recent-window gaps of 35-42 days
easily produce a 65+ day stretch on a bad draw.

**Realistic time-to-15-fills, using the most recent relevant window instead of the optimistic
full-history average:**

| Scenario | Basis | Time |
|---|---|---|
| Best case | Full-history average, 26.9 d/trade | ~13.5 months (1.1yr) |
| Typical case | Most recent 11-month window, 41.6 d/trade | **~20.5 months (1.7yr)** |
| Worst case | Longest observed dry spell (94.7d) as sustained pace | ~3.9 years |

**Origin of "15"/"PF≥1.2" — searched CLAUDE.md, CLAUDE_HISTORY.md, every `.memory/decisions/
*.md` file, and git log/blame on both. No derivation found tying "15" to BTC/CAD's own
expected trade frequency or any other specific calculation.** It's used identically across
multiple unrelated gates in this codebase (this capital gate, the stock bot's post-whitelist
review checkpoint sample-size trigger, various other thresholds) — reads as a general
"minimum sample before a PF/win-rate reading means anything" convention, not something
calculated per-gate. "PF ≥ 1.2" is even more pervasive (screen gate, walk-forward gate,
capital gate, stock-bot Phase A gate, IBKR gate) — a project-wide house standard, not derived
per-gate either. The one piece of related reasoning that *does* exist: a 2026-07-24
investigation stated "0/15 reflects a strategy that trades roughly every 1–3 weeks (consistent
with backtest frequency)" — written to *explain* the already-existing 15, not to derive it,
and that 1–3-week assumption is directly contradicted by the per-window data above (30–42
days/trade recently, not 7–21).

## BTC/CAD 15-fill precondition removed from new-symbol promotion — 2026-08-24 (decoupling decision)

**Decision:** the "BTC/CAD live gates met: ≥15 fills + live PF ≥ 1.2" precondition (CLAUDE.md,
"Preconditions for any USD pair promotion", was item #2) is **removed** as a requirement for
promoting SOL or any other independently-validated symbol. This is a **deliberate correction
to an unexamined default, not a loosening of standards.** The evidentiary bar itself — PF ≥ 1.2
walk-forward validated across train/validation, exactly as strict as before — is completely
unchanged and still applies to every remaining precondition (screen PASS, capital ≥$500,
documented FX handling, fresh full walk-forward on the current strategy hash, SL-distance
sizing). What was removed is a *coupling*: requiring an unrelated symbol's (BTC/CAD's) own
live trade count to clear 15 fills before a *different*, independently-proven symbol could be
promoted.

**Why this coupling didn't hold up, per the reality check above:** it added no evidence about
SOL's own edge — SOL clears the real bar on its own merits (walk-forward PF ≥ 1.2 across
train/validation, with proper ATR-risk sizing, confirmed 2026-08-24, see the section above).
Requiring BTC/CAD to also hit 15 fills only tied SOL's promotion timeline to BTC/CAD's
unrelated trade frequency — and that frequency turns out to be 35–42 days/trade in the most
recent, most relevant window, making 15 fills a realistic 1.1–1.7+ year wait with 65 days
already elapsed and zero progress. No record of "15" being deliberately calculated for this
purpose was found anywhere in this project's documented history — it was a reused round
number, asserted once and retroactively rationalized after the fact, not a deliberate
risk-sizing decision tied to SOL's (or any other symbol's) own promotion readiness.

**What this does NOT do:** SOL/CAD is **not** added to `UNIVERSE_WHITELIST` or any live config
by this decision. No `.env` change was made. **Capital (~$146 CAD available vs. $500 required)
is now the sole unmet precondition for SOL/CAD.** Every other precondition (screen PASS,
capital threshold itself, FX documentation, fresh walk-forward at promotion time, SL-distance
sizing) is completely unchanged in substance — only renumbered in CLAUDE.md now that item #2
is gone (old #3→#2, #4→#3, #5→#4, #6→#5).

**Other gates checked for coupling, none require changes:**
- `stock_bot/analysis/checkpoint_tracker.py`'s `ROUND_TRIP_TRIGGER = 15` — a separate,
  independent hardcoded constant for the stock bot's post-whitelist review checkpoint. Its own
  comment says it "mirrors the crypto bot's 15-fill capital-gate convention" (same number,
  chosen by analogy) but there is no code or doc coupling — changing/removing the crypto
  precondition does not touch this constant's value or behavior. Left as-is.
- `shadow_signal.py` / `unified_dashboard.py`'s "15-fill" mentions — these are about
  **BTC/CAD's own separate $100→$250 capital-*scaling*** gate (BTC/CAD earning more of its
  *own* capital based on its *own* track record), not the new-symbol-promotion coupling this
  task removed. Different mechanism, correctly untouched, unaffected.
- `screen_universe.py` (line ~478) — **does** restate the exact removed precondition
  ("Promotion to UNIVERSE_WHITELIST requires: 1. BTC/CAD live gates met (≥15 fills, live PF ≥
  1.2)...") as a hardcoded string in its generated report's footer notes. This is pure
  informational text — no code path checks BTC/CAD's fill count programmatically anywhere in
  this repo (confirmed via grep; the whole "USD pair promotion precondition" list has never
  been code-enforced, only documented). Leaving it means a future `screen_universe.py` run
  will print a stale policy statement until someone updates that string — flagged here,
  **not fixed**, since it's not "directly and necessarily coupled" (nothing breaks) and this
  task's stated scope for adjacent files was report-only.

## Capital threshold correction — 2026-08-24 (later same day): SOL's precondition was citing the wrong stage

**The mistake:** CLAUDE.md's "Preconditions for any USD pair promotion" #2 said SOL needed
"Capital ≥ $500 CAD available for the new symbol slot." $500 is the **Stage-3 scale-up**
threshold from Capital Sizing Rules above ($250→$500, requires 30 completed *live* trades on
that specific symbol at PF≥1.3 sustained over the last 20) — a bar for a symbol that has
already been trading live and proven itself twice over. SOL has never traded live at all. A
brand-new symbol starts at **Stage 1: $100 CAD**, the same starting point BTC/CAD itself used
before ever earning its way to a larger allocation. The doc had conflated "capital required to
scale an already-successful live symbol up" with "capital required to open a new symbol's
first slot" — two different gates in Capital Sizing Rules, mistakenly treated as one.

**How this happened:** traced back through the 2026-08-24 "BTC/CAD 15-fill precondition
removed" session (above) — that session correctly identified the *fill-count* coupling as an
unexamined default and removed it, but carried the *dollar figure* forward unquestioned from
whatever precondition #2 already said, rather than checking it against Capital Sizing Rules'
actual stage definitions. Same failure shape as the fill-count issue it was fixing right next
to: a number reused across a doc edit without re-deriving it from source, just one level
downstream this time.

**Correction applied:** CLAUDE.md precondition #2 now reads $100 (Stage-1), with an explicit
note that a new symbol's slot must not come at the expense of BTC/CAD's existing
`MAX_SLOT_CASH_CAD=77` allocation — that's a separate constraint from the dollar threshold
itself (see the live-capital check below for why the two interact).

**Live capital check, same day:** real Kraken CAD balance (`fetch_balance()`, read-only, not
the ~$146 figure this file and CLAUDE.md had been carrying from an earlier memory) is
**$153.39 CAD total, $153.39 free, 0 BTC held** — the account is currently fully flat (matches
the 2026-07-17 full-BTC-sale entry elsewhere in memory; nothing has been bought since). At the
now-corrected $100 Stage-1 threshold, $153.39 clears the dollar figure alone — but preserving
BTC/CAD's current $77 slot **at the same time** as opening a full $100 SOL slot requires
**$177 total** ($77 + $100), which $153.39 does not reach. Shortfall: **$23.61**.

**A separate, more binding constraint found in the same check — not a doc bug, an architecture
fact:** `CapitalPool` (`bot/portfolio/capital_pool.py`), as actually coded and as instantiated
in `bot/main.py` (`_pool_total = _first_exec.cash` for live trading, one shared `slot_cap` from
`MAX_SLOT_CASH_CAD`), gives every symbol slot the **same** `slot_cash = min(total /
max_concurrent, slot_cap)` — there is no per-symbol cap. It cannot express "BTC gets $77, SOL
gets $100" simultaneously; both symbols always get an equal split (up to the one shared cap).
So even with ≥$177 total capital, hitting the literal "BTC=$77, SOL=$100" split described in
this session's task would require either a code change (a per-symbol cap dict, not built) or
accepting that both slots come out equal instead (e.g. $177 total ÷ 2 = $88.50 each — not
$77/$100). This is a real constraint to weigh when choosing how to split capital, independent
of whether the $100-vs-$500 threshold is now correct. Flagged for a decision, not resolved
here — no code or config was changed in this pass.

**How to apply:** before citing a dollar capital requirement for a symbol anywhere in this
project, check which Capital Sizing Rules stage it actually refers to (Stage 1 $100 new-slot
entry vs. Stage 2 $250 vs. Stage 3 $500 scale-up) rather than assuming a round number seen
nearby is the right one — the same "reused number, not re-derived" failure mode as the
fill-count correction directly above this section.

## Decisions

**Do not change symbols until fee fix is proven.**
Running ETH on Kraken at 0.80% would still be net-negative. The fee is the problem, not the symbol. Confirm the fee-dict structure and find a path below 0.20% before any expansion.

**How to apply:** When planning multi-symbol expansion, lead with ETH. Do not include LINK. Treat SOL/BNB as second phase after ETH is validated live. Never add a symbol just because it performed well in the recent backtest window — check full-history PF first.
