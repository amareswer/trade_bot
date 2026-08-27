---
name: multi-symbol-validation
description: "Symbol ranking, fee constraint, and expansion decisions from 2026-06-11 multi-symbol backtest. 2026-08-20 addendum: live investigation into BTC/CAD's 7-week zero-BUY drought confirms and extends the original 'BTC is weak now' finding — MTF daily-trend veto data, blocked-gate distribution, real price characterization. 2026-08-24 addenda: (1) SOL's ATR×2.0 OOS-HOLDS result re-confirmed with dollar-risk-capped position sizing applied; (2) fill-frequency reality check quantifies the 'BTC/CAD 15 fills' gate as an unexamined default, not a calculated one; (3) that gate REMOVED as a precondition for SOL/other new-symbol promotion — SOL now blocked on capital alone; (4) capital threshold itself corrected from $500 (wrong — Stage-3 scale-up figure) to $100 (correct — Stage-1 new-symbol figure), live balance checked ($153.39 CAD, 0 BTC), and a CapitalPool architecture constraint (no per-symbol slot cap) flagged as unresolved; (5) same-day follow-up: CapitalPool per-symbol slot caps BUILT (code capability, not live-wired) and the $100 Stage-1 placeholder replaced with SOL's real researched minimum (~$110-$334 CAD bare / ~$165-$501 with the bot's own safety-margin guard, volatility-dependent, from real Kraken SOL/CAD minimums + the live ATR-risk sizer) — current $153.39 balance doesn't clear it alongside BTC's $77; (6) same-day follow-up: rescreen.py's 'automated monthly USD re-screen' claim was FALSE (code never passed SCREEN_QUOTE=USD) — fixed with a real USD leg (measured load impact first, ~7min added), plus an unrelated live _alert() bug found and fixed (wrong cfg attribute path, silently killed every rescreen Telegram alert)."
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

## CapitalPool per-symbol slot caps + Kraken SOL/CAD real minimum-viable slot — 2026-08-24 (follow-up session, same day)

Two pieces of work, requested together: (1) give `CapitalPool` the ability to hold different
slot caps per symbol (code capability only, not wired into live `.env`), and (2) replace the
"$100 Stage-1" placeholder from the correction directly above with SOL's actual researched
minimum, using Kraken's real order minimums the same way `LiveExecutor`'s existing 2026-07-30
min-size guard does.

### CapitalPool per-symbol caps (code, not live config)

`bot/portfolio/capital_pool.py` gained an optional `slot_caps: dict[str, float]` constructor
param and a new `slot_cash_for(symbol)` method, alongside the original `slot_cap`/`slot_cash`
(unchanged, still the shared default). A symbol absent from `slot_caps` falls straight through
to the old shared computation — numerically identical, confirmed by dedicated tests and by the
unchanged strategy fingerprint (`b30f2f9e769c8d41`, 31 trades, PF 2.19 — this is a
`bot/portfolio/`-only change, no `bot/strategy/*` touched). When a per-symbol cap IS set, that
symbol's target is its own cap (not an equal pool division), further bounded by whatever cash
isn't already committed to other open slots — so an under-capitalized pool degrades instead of
over-committing (first-allocated symbol gets priority; a later one gets whatever's left), and
a pool whose caps sum to less than total capital leaves the surplus idle rather than force-
splitting it. `config.py` gained a matching `MAX_SLOT_CASH_CAD_<BASE>` env-var scan (e.g.
`MAX_SLOT_CASH_CAD_SOL=45`), falling back entirely to the existing shared `MAX_SLOT_CASH_CAD`
when unset — an `.env` with only the old single value is untouched. `bot/main.py`'s pool-init
block now builds a per-symbol dict from this and seeds each executor's initial cash via
`slot_cash_for()`; the single-shared-cap startup log line is byte-identical to before when no
per-symbol override is configured (which is the case today — nothing added to `.env` this
session). Tests: `tests/crypto/test_capital_pool.py`, +10 (no-override backward compat,
single-symbol-dict matches old single-shared-cap exactly, untouched-symbol falls back to
shared default, two-symbols-both-fit, insufficient-total-so-second-gets-remainder, pre-
allocation order-dependence documented, zero-means-uncapped-per-symbol, property readable,
negative-cap validation, release-then-reallocate cycle). Suite 629→639.

### Kraken SOL/CAD real minimum-viable slot size

Queried live (`ccxt.kraken().load_markets()`, same mechanism `LiveExecutor._lookup_amt_min()`/
`_validate_order()` already use for the 2026-07-30 min-size guard): **SOL/CAD `amount.min =
0.06 SOL`** (~$7.88 CAD notional at the price checked, $131.26), **`cost.min = $1.00 CAD`**
(not binding — the amount minimum is always the larger constraint here). Trivial numbers on
their own — the real constraint is what the LIVE sizing formula actually produces at small
slot sizes.

**The binding constraint is `calc_trade_qty_atr_risk()` interacting with SOL's own volatility,
not the exchange minimum itself.** Live formula (config.py): `qty = min(base_qty, cash ×
RISK_PER_TRADE_PCT × STOP_LOSS_PCT / (ATR × ATR_SL_MULT))` — with live values
`RISK_PER_TRADE_PCT=0.10`, `STOP_LOSS_PCT=0.015`, `ATR_SL_MULT=2.0`, the ATR-risk term is
almost always the binding (smaller) one, confirmed numerically (`base_qty` came out
3–4× larger than the ATR-capped qty at every cash level tested). Solving for the slot cash
needed to clear `amount.min` (0.06 SOL), across SOL/CAD's real last 30 four-hour candles
(`ccxt.fetch_ohlcv`, ~5 days, ATR(14) via `bot/indicators/indicators.py`'s own `atr()` —
Wilder-smoothed, growing-window, same function the live bot calls):

| Scenario (SOL/CAD, 4h candles) | ATR(14) | Slot cash to clear `amount.min` (bare) | Slot cash to also clear the 1.5× `MIN_SIZE_SAFETY_MARGIN` guard |
|---|---|---|---|
| Calmest of last 30 candles | 1.3725 | $109.80 | $164.70 |
| 30-candle mean | 2.9956 | $239.64 | $359.46 |
| Latest reading (at check time) | 4.0384 | $323.07 | $484.61 |
| Most volatile of last 30 candles | 4.1715 | $333.72 | $500.58 |

`MIN_SIZE_SAFETY_MARGIN=1.5` (`bot/execution/live_executor.py`, default) is the bot's own
pre-trade early-warning threshold — a computed qty under 1.5× `amount.min` fires a Telegram
alert before the order is even sent, distinct from the outright exchange rejection at `qty <
amount.min` itself. Both bars are reported since "reliably avoids SIZE_SKIP/min-order
rejection" reasonably means clearing the warning too, not just squeaking past the hard floor.

**Conclusion: a $100 slot (the corrected Stage-1 figure from the earlier correction above)
would SIZE_SKIP or trigger the min-size warning on essentially every normal-to-current-
volatility SOL/CAD 4h candle, not just occasionally** — $100 only clears the *bare* floor at
the calmest reading observed in the last 5 days, and doesn't clear the safety-margin guard at
any reading in that window. The Stage-1 $100 rule is still the correct GENERAL new-symbol
starting point (untouched for symbols whose own ATR is a smaller fraction of price) — this is
a SOL-specific finding about how SOL's own volatility interacts with the ATR-risk sizer, not a
change to the general rule.

**Notable, worth flagging plainly:** the earlier-corrected "$500" (wrongly cited as SOL's
threshold before being fixed to $100) turns out to sit close to the upper end of the
*real*, volatility-driven range found here (~$334–$501 depending on which bar). Coincidence,
not vindication — $500 was still the wrong number for the wrong reason (a misapplied
Capital Sizing Rules stage, not a derived sizing-math result) — but worth naming so "$100" and
"$500" aren't both treated as equally-wrong guesses; one of them happens to land in the right
neighborhood for the wrong reason.

**Fee cross-check, using the identical logic already documented for BTC
(`.memory/decisions/fee-structure.md`):** Kraken charges pure percentage-of-notional fees with
no fixed per-trade dollar floor (confirmed — `ccxt` market dict carries `maker`/`taker` rates
only, no minimum-fee field; the $1 `cost.min` is an order-size floor, not a fee floor). Round-
trip fee drag is therefore constant as a % of trade value regardless of dollar slot size — a
$150 slot and a $500 slot pay the identical ~1.20% (0.40% maker BUY + 0.80% taker SELL, per the
documented BUY-limit/SELL-market policy) round trip, proportionally. This means SOL is **not**
"fee-strangled" at small slot sizes the way the original 2026-06-11 screen found other alts to
be — that finding was about the *strategy's own edge* being too thin to survive fee drag at
any size, a different failure mode from a sizing/exchange-minimum mismatch. Confirms this
directly: SOL's own OOS-validated result (TRAIN PF 1.32 / VALIDATION PF 1.46, addendum above)
already used `BACKTEST_FEE_PCT=0.008` applied per side (`atr_oos_validation.py`'s own default,
≈1.6% modeled round-trip) — harsher than the real ~1.20% live figure — and still cleared
PF≥1.2. Fee drag is not SOL's constraint; the ATR-risk-sizing/exchange-minimum interaction
above is.

**Live capital re-check, same session:** Kraken CAD balance unchanged since the earlier
correction, **$153.39 CAD total, 0 BTC held**. Against the real SOL range found here
(~$110–$334 bare, ~$165–$501 with safety margin) plus preserving BTC's $77, **$153.39 does not
clear the low end of the bare range while also leaving BTC intact** ($77 + $110 = $187 needed
at minimum, $153.39 available — $33.61 short even at the *calmest* observed reading; the gap
widens to $170–$381 short at more typical/current volatility). No code or live config was
changed to act on this — reporting only, per the task.

**How to apply:** this range is volatility-derived, not a fixed constant — SOL's ATR moves
meaningfully week to week (1.37 to 4.17 observed in just the last 5 days here). Re-run the
same check (`ccxt.fetch_ohlcv('SOL/CAD', '4h')` + `bot.indicators.indicators.atr()` +
`config.calc_trade_qty_atr_risk()`) close to any actual promotion decision rather than trusting
these exact dollar figures indefinitely.

## rescreen.py: USD leg added, closing a real automation gap — 2026-08-24 (later same day)

A "what's missing, verify" pass found CLAUDE.md's Roadmap item J ("USD symbol re-screen —
Automated monthly via `rescreen.py`") was **false**: `rescreen.py` called `screen_universe.py`
with no env override, and `screen_universe.py` defaults `SCREEN_QUOTE` to `CAD` — the USD side
had been manual-only since the last real USD screen, 2026-07-16 (confirmed via
`comm`/`git blame`-style reasoning against the actual code, not by trusting the doc). Load
impact was measured before implementing anything (per explicit task instruction not to ship
silently): `screen_universe.py`'s Kraken calls are 2 total regardless of quote currency or
candidate count (`load_markets()` + `fetch_tickers()`, negligible); the real cost is Binance
OHLCV fetches for the walk-forward step, measured empirically at ~28s per candidate that
clears the liquidity gate (`SCREEN_SYMBOLS=ETH/CAD,LTC/CAD` timing run). At the default
`SCREEN_MAX_CANDIDATES=15`, that's up to ~7 extra minutes for the USD leg — the CAD leg today
is nearly free (~5-10s) since almost every CAD candidate is already excluded/decided, so this
roughly triples total monthly job runtime (~5min → ~12min including the stock leg's ~4m45s),
still well under the existing 2400s per-leg subprocess timeout and running once a month off
the trading tick loop. Verdict: acceptable, proceeded.

**Fix:** `rescreen.py`'s `sections` list now carries a 4th element (`extra_env`), with a new
`("crypto-usd", "screen_universe.py", _crypto_usd_whitelist(), {"SCREEN_QUOTE": "USD"})` entry
reusing the exact same report-building loop the CAD/stocks legs already use — so the USD
section's format is identical by construction, not a parallel reimplementation. New
`_crypto_usd_whitelist()` filters `UNIVERSE_WHITELIST` for `/USD`-suffixed entries (empty
today — nothing USD is live-whitelisted — so every USD PASS surfaces as a NEW QUALIFIER, never
a decay, which is the correct signal). New `RESCREEN_SKIP_USD` env flag, symmetric with the
existing `RESCREEN_SKIP_CRYPTO`/`RESCREEN_SKIP_STOCKS`. Same "never auto-changes a whitelist"
rule applies identically — confirmed no code path touches `UNIVERSE_WHITELIST` or the USD
Expansion preconditions list.

**Bonus fix, found while in this file:** `_alert()` read `cfg.telegram_bot_token`/
`telegram_chat_id`/`telegram_enabled` directly — those fields live under `cfg.alerts.*`
(`AlertConfig`), not flat on `AppConfig`. Confirmed via the 2026-08-01 rescreen run's own
`logs/rescreen.log`, which shows the exact `AttributeError` this caused, silently caught by
`_alert()`'s own try/except and reduced to a console line nobody reads (this runs as an
unattended monthly subprocess). Every attention-worthy rescreen result — exactly the runs
where the Telegram push matters most — had been silently failing to alert. The monthly
markdown report itself was unaffected (a completely separate code path); only the Telegram
push was dead. Fixed the same way `_crypto_whitelist()`'s own `cfg.universe_whitelist` →
`cfg.universe.universe_whitelist` bug had already been silently fixed at some earlier,
undocumented point (confirmed by reading the current source — that one's already correct).

**Tests:** new file `tests/crypto/test_rescreen.py` (this script's first-ever test coverage),
11 cases — `_crypto_usd_whitelist()` behavior + a regression check that `_crypto_whitelist()`
is unaffected, USD leg env-override wiring, USD results landing correctly in the report, CAD
leg regression check (report/whitelist-comparison unchanged), `RESCREEN_SKIP_USD`, a USD
gate-failure report case, and the `_alert()` bugfix (both a no-`AttributeError` check and a
call-args check against `cfg.alerts.*`). One test-hygiene fix along the way: `_alert()`'s real
`time.sleep(5)` (daemon-thread hand-off margin, pointless in a test where
`TelegramAlerter._send` is already a no-op) was costing ~5s per test that triggered it —
patched `time.sleep` in the relevant tests, cutting the file from ~31s to <1s.

Suite 647→658. Strategy hash reconfirmed unchanged (`b30f2f9e769c8d41`, 31 trades, PF 2.19) —
`rescreen.py` and its test file only, no `bot/strategy/*` touched. CLAUDE.md's Roadmap item J
and the "USD Expansion → Re-screen triggers" section (which previously contradicted each
other — one said manual, one said automated) both corrected to describe what the code now
actually does. Full detail: CLAUDE_HISTORY.md.

## Decisions

**Do not change symbols until fee fix is proven.**
Running ETH on Kraken at 0.80% would still be net-negative. The fee is the problem, not the symbol. Confirm the fee-dict structure and find a path below 0.20% before any expansion.

**How to apply:** When planning multi-symbol expansion, lead with ETH. Do not include LINK. Treat SOL/BNB as second phase after ETH is validated live. Never add a symbol just because it performed well in the recent backtest window — check full-history PF first.

## SOL/CAD PROMOTED to live trading — 2026-08-25

The capital gap flagged throughout this file (BTC's $77 + SOL's ~$110–$334/$165–$501
range vs. the $153.39 balance on hand) closed via a real $400 CAD deposit. Verified live
against Kraken (`check_kraken_balance.py`, new read-only `fetch_balance()` script, no
orders/state writes) rather than trusting the arithmetic: **$553.39 CAD total**, matching
$153.39 + $400 exactly.

All remaining preconditions were re-checked same-day, not assumed from the 2026-08-24 result:
- **Walk-forward** re-run fresh (`SYMBOL=SOL/USDT ATR_MULT=2.0 ATR_RISK_SIZING=true python
  atr_oos_validation.py`): TRAIN PF 1.32 / VALIDATION PF 1.46, both ≥1.2 — identical to
  2026-08-24 (strategy code unchanged in between), but run again per the "a pass on an older
  hash doesn't count" rule rather than reused. Report: `logs/atr_oos_SOL_2.0_sized_20260825.md`.
- **FX-conversion precondition (#3)** confirmed N/A, not just skipped — queried
  `ccxt.kraken().load_markets()` directly: `SOL/CAD` is `quote: CAD`, a real direct spot
  market, same as `BTC/CAD`. No USD leg, no conversion cost, nothing to document.

**Live config applied:**
```
UNIVERSE_WHITELIST=BTC/CAD,SOL/CAD
MAX_SLOT_CASH_CAD_SOL=376        # BTC's $77 shared-cap fallback untouched
MAX_CONCURRENT_POSITIONS=2       # was 1 — required, or CapitalPool.allocate() blocks the
                                  # second slot regardless of the per-symbol cap existing
STARTING_CASH=553.39             # inert for real live trading (LiveExecutor._sync_cash()
                                  # overrides with the real balance) but kept accurate per
                                  # the existing "raise together" hard rule
MONITOR_SYMBOLS=BTC/CAD,SOL/CAD  # regime_monitor.py
```
`$376` sits inside the researched safe range with room to spare (clears the bare exchange
minimum across the full observed volatility range; clears the 1.5× safety-margin guard
except at the single most-volatile reading on record) while leaving ~$100 of the $553.39
uncommitted as buffer, rather than capping SOL at the full $476.39 available.

Verified before treating this as done: `CapitalPool(total_capital=553.39, max_concurrent=2,
slot_cap=77.0, slot_caps={'SOL/CAD': 376.0})` gives BTC/CAD → $77.00, SOL/CAD → $376.00
independently (SOL's slot doesn't shrink once BTC's is allocated) — matches the intended
per-symbol-cap semantics, not a coincidence of the numbers.

**One casualty, fixed same session:** `tests/crypto/test_capital_pool.py::
test_slot_caps_by_base_ignores_unrelated_keys` assumed no `MAX_SLOT_CASH_CAD_<BASE>` key
exists in the real environment — true until this promotion, false after. Not a code bug in
`_slot_caps_by_base()` itself, a test-isolation gap it happened to expose; fixed with an
explicit `monkeypatch.delenv("MAX_SLOT_CASH_CAD_SOL")` in that one test. Suite count
unchanged (666), full run reconfirmed green after the fix.

**Both symbols now start their live-fill capital gates independently and from zero.**
SOL/CAD has 0/15 fills toward its own $376→ next-tier gate, same three-criteria bar as
BTC/CAD (live PF≥1.2, shadow match≥95%, fee/slippage on-spec) — not a shared counter with
BTC/CAD's own 0/15.

**What was deliberately NOT touched:** `regime_monitor.py`'s `MONITOR_WATCHLIST` (XRP/CAD
stays watchlist-only), `RiskManager` config (already symbol-generic — shared aggregate
breakers + per-symbol position-size cap, confirmed via `bot/main.py`'s single global
`RiskManager` instance covering all executors, no per-symbol construction needed), and SYN
(OOS-validated at ATR×2.0 same as SOL, but no capital research or live-config action was
taken for it in this session — still just a documented candidate).

## SYN/USD — groundwork done, NOT promoted — 2026-08-25 (same day, later)

User asked to prepare the next candidate ahead of an actual decision. Same three-step process
as SOL, research only — no `.env`/`UNIVERSE_WHITELIST` change, no live config touched.

**1. Fresh walk-forward, current strategy hash** (`SYMBOL=SYN/USDT ATR_MULT=2.0
ATR_RISK_SIZING=true python atr_oos_validation.py`, same script/methodology as SOL):
```
TRAIN      ATRx2.0    trades=20   win=55%  PF=1.75  SL=25%  → PASS
VALIDATION ATRx2.0    trades=21   win=57%  PF=1.75  SL=29%  → PASS
HOLDS — validation PF 1.75 >= 1.2 (train was 1.75)
```
Stronger and more stable than SOL's result (PF 1.32→1.46, SL 48%/52%) — SYN's SL-exit rate is
roughly half SOL's. Flat-SL comparison run in the same pass FAILED both windows (83%/77%
SL-exit) — consistent with the original 2026-07 screen finding, confirms ATR stop is doing
real work here, not a fluke. Report: `logs/atr_oos_SYN_2.0_sized_20260825.md`.

**2. FX-conversion precondition — genuinely applies here (unlike SOL/CAD).** `SYN/USD` is
real, active, spot on Kraken (`ccxt.load_markets()` confirmed) — but this account holds CAD,
not USD (`check_kraken_balance.py`: CAD + ETH only, no USD). Kraken carries a direct `USD/CAD`
spot market, maker AND taker both **0.20%** (confirmed live via `load_markets()`, not the
assumed figure from memory) — this is the actual conversion cost, one-way. A CAD→USD
conversion at position-open time (and USD→CAD again if capital is ever pulled back) is a
real, one-time ~0.20%-per-leg cost, separate from and on top of the existing ~1.2%
round-trip trading fee. Ongoing USD-denominated P&L would need its own tracking, separate
from the CAD-denominated BTC/SOL book — not yet built, not hard, just not done.

**3. Capital-sizing — SYN needs MORE than SOL did, not less.** Kraken's real SYN/USD minimum:
`amount.min = 60 SYN` (~$6.60 USD notional at the $0.11 price checked), `cost.min = $0.50`
(not binding, same pattern as SOL). SYN's low unit price means the ATR-risk sizer's dollar
cap has to buy proportionally more units to clear that 60-unit floor. Solved across SYN/USD's
real last 30 4h candles (Kraken `fetch_ohlcv` + `bot.indicators.indicators.atr()`, same
method as SOL):

| Scenario (SYN/USD, 4h) | ATR(14) | Slot to clear `amount.min` (bare) | + 1.5× safety margin |
|---|---|---|---|
| Calmest of last 30 | 0.00312 | $249.93 | $374.90 |
| 30-candle mean | 0.00449 | $359.43 | $539.14 |
| Latest reading | 0.00366 | $292.64 | $438.95 |
| Most volatile of last 30 | 0.00574 | $459.56 | $689.33 |

**Fee cross-check:** `atr_oos_validation.py`'s own `BACKTEST_FEE_PCT=0.008`/side (≈1.6% round
trip, harsher than the real ~1.2% live figure) was already baked into the PF=1.75 result
above — same conclusion as SOL, fee drag is not the constraint, sizing/exchange-minimum is.

**Flag worth surfacing now, not burying:** live 24h volume checked at the same time —
**$49,371 USD**, sitting essentially AT the $50,000/day liquidity gate this whole framework
uses, not comfortably above it the way SOL/BTC are. This is a single point-in-time ticker
read, not a trend — could be a quiet day, could be real decay since the original June 2026
screen (which required clearing $50k to even get walk-forwarded). Re-check this specifically,
not just the ATR/volatility numbers, before ever treating SYN as ready — a symbol hovering at
the liquidity floor is a different kind of risk than the capital-sizing gap SOL had.

**Bottom line: SYN/USD is NOT promoted, and capital doesn't support it right now regardless.**
BTC ($77) + SOL ($376) already commit $453 of the $553.39 balance — only ~$100 uncommitted,
against a $250-$690 need depending on volatility/margin comfort. Needs its own deposit,
same as SOL did, before this becomes actionable — not something to trade off against SOL's
existing capital. Everything above is groundwork for a future decision, not a promotion.

## Post-promotion multi-symbol audit + fixes — 2026-08-25 (same day, evening)

Read-only audit of the newly-exercised multi-symbol path first (user request), fixes applied
after (second explicit request). **Trading-path mechanics all verified correct** — BUY sizing
uses per-symbol slot cash ($376 for SOL, not the shared $77 cap), tick loop iterates all
symbols, risk gates are per-slot/per-symbol where they should be and aggregate where they
should be, CapitalPool allocate/release wired on fills, shadow audit + dashboard +
in-bot regime monitor all pick up the whitelist dynamically. Findings fixed:

1. **`live_comparison.py` stale baseline** — its hardcoded `_BASELINE` still carried the
   2026-06-19 result (58 trades, PF 1.79) through four subsequent strategy-hash changes.
   Updated to the current canonical fingerprint (31 trades, PF 2.19, win 38.7%, max DD
   −1.74%, return −0.08%, validated 2026-08-20, hash `b30f2f9e769c8d41`) — numbers
   reproduced by actually running `EXCHANGE=binance SYMBOL=BTC/USDT python backtest.py`
   the same evening, not copied from the doc.
2. **`live_comparison.py` symbol blending** — it aggregated ALL fills in `trades.db`
   against the BTC-only baseline. Now filters to the baseline's base asset (`--base`
   override available), printing what was excluded. Smoke-run immediately proved the point
   retroactively: 2 old DOGE/CAD fills (from before DOGE was blocked) had been silently
   blending into the "BTC" comparison all along — now correctly excluded, 8/10 fills compared.
3. **`regime_monitor.py` standalone missed `.env`** — no `load_dotenv()`, so a bare
   `python regime_monitor.py` fell back to the BTC/CAD default regardless of
   MONITOR_SYMBOLS in `.env` (the in-bot subprocess path was never affected — it passes
   symbols explicitly). Added `load_dotenv()`; standalone run confirmed both symbols now
   monitored. Stale "BTC/CAD" docstring fixed too.
4. **Dashboard "Gates at a Glance" label** hardcoded "BTC/CAD" — now built from
   `_whitelist()` dynamically.
5. **`.env` `MAX_SLOT_CASH_CAD` comment** still described the "$100 capital / 1 slot" world
   — rewritten for the per-symbol-override reality.

**Known-stale, needs a restart to fix (bot holds it in memory):** `logs/risk_state.json`
`week_open_value=77.0` predates the $400 deposit — the weekly-loss breaker is inert until
Monday's ISO-week rollover (a >5% weekly loss would be measured against $77, not $453).
`peak_value`/`day_open_value` self-correct (peak on first evaluate, day_open at UTC
midnight); `week_open_value` does NOT until Monday. Fix requires stop-bot → patch file →
start-bot sequencing because any in-flight save (fills, midnight rollover) rewrites the
file from stale memory. Left for the user to run (they handle restarts). Not urgent while
both positions are flat.

**By-design, not fixed:** first SOL BUY at recent volatility will fire the 1.5×
min-size-margin Telegram warning (qty ~0.07 SOL vs 0.06 min — above the hard floor, below
the margin) — matches the sizing research exactly, order still places.

Suite re-run after all fixes: 666 passed. No `bot/strategy/*` touched — no walk-forward
needed.

## SYN/USD liquidity re-check — 2026-08-26 (mixed result, not resolved)

Re-read SYN/USD's Kraken ticker on request. Volume concern from 2026-08-25 (sitting right at
the $50k floor, $49,371) looks resolved on this reading — $99,389, comfortably above. But
spread now fails narrowly, 0.179% vs the 0.15% max, which wasn't flagged as a risk before —
single point-in-time ticker reads, one metric improved while a different one became the
blocker. Net effect: still not clean on liquidity, just for a different reason than
yesterday. Capital gap ($250-$690 vs ~$100 uncommitted) and the FX-conversion build are
unchanged either way.

## screen_universe.py engine-kwargs drift bug — found + fixed 2026-08-26 (surfaced PUMP/USD)

Full root-cause trail is in `CLAUDE_HISTORY.md` (search "screen_universe.py's own
engine-kwargs drift bug") — not duplicated here. Summary relevant to this file: while
re-verifying DOGE/XRP/ETH/PEPE/XDC/LINK/SYN symbol-by-symbol via `validate_symbol.py`, a
cross-check full-universe run of `screen_universe.py` (CAD + USD legs) disagreed with
`validate_symbol.py` on LINK/USD's verdict. Root cause: `screen_universe.py` had been
hand-listing its own `engine.run()` kwargs instead of using the shared
`engine_kwargs_from_cfg()` builder, missing `macd_enabled`, all 7 Mode A/B entry params, and
`atr_risk_sizing`/`atr_sizing_baseline_sl_pct` — validating a more permissive strategy shape
than what's actually live, for every run since 2026-07-20. Traced impact: no past promotion
decision was ever made on the buggy result (CAD leg's only 2 candidates fail liquidity before
reaching walk-forward; the USD leg's automation didn't exist until 2026-08-24 and hadn't fired
once before this fix). Fixed to use the shared builder; added `screen_universe.py` to
`test_validation_scripts_use_the_builder()` (`tests/crypto/test_engine_params.py`) so a 5th
script can't reopen this gap silently. Re-running the USD screen before/after the fix: LINK/USD
flipped PASS→FAIL (the false positive that surfaced the bug), PENGU/USD flipped FAIL→PASS —
confirms a genuine two-different-strategies difference, not a one-directional bug. PUMP/USD
passed cleanly both times and is the one fresh candidate carried forward — sized below.

## PUMP/USD — found + capital-sized, NOT promoted — 2026-08-26 (same day, later)

Surfaced by the `screen_universe.py` fix above, not from a targeted search. Walk-forward on
current strategy code (post-fix, correct kwargs): **PASSES cleanly** — 5000c PF 2.04 (20
trades), 3000c PF 2.04 (20 trades), 1000c PF 2.14 (13 trades), SL-exit rate 20% (well under
the 70% cap, and notably lower than SOL's 48-52% or SYN's 25-29%). Kraken liquidity
(`ccxt.load_markets()`/`fetch_ticker`, checked live): **$6,213,623 USD/day volume, 0.041%
spread** — both comfortably clean, unlike SYN/USD's borderline case. `amount.min = 2200
PUMP` (~$10.63 notional at $0.0048/unit), `cost.min = $0.50` (not binding).

**Capital-sizing — needs MORE than either SOL or SYN did, and by a wide margin.** Same method
as SOL/SYN: solved `calc_trade_qty_atr_risk()`'s dollar-risk cap for the slot cash needed to
clear `amount.min`, across PUMP/USD's real last 30 4h candles (Kraken `fetch_ohlcv` + `bot.
indicators.indicators.atr()`, period 14):

| Scenario (PUMP/USD, 4h) | ATR(14) | Slot to clear `amount.min` (bare) | + 1.5× safety margin |
|---|---|---|---|
| Calmest of last 30 | 0.000268 | $785.67 | $1,178.51 |
| 30-candle mean | 0.000313 | $917.16 | $1,375.73 |
| Latest reading | 0.000268 | $785.67 | $1,178.51 |
| Most volatile of last 30 | 0.000368 | $1,078.99 | $1,618.48 |

Why so much larger than SOL ($110-$334) or SYN ($250-$690): PUMP's exchange minimum is a
large *unit count* (2200) against a tiny per-unit price — the ATR-risk sizer's dollar cap has
to buy proportionally far more units to clear that floor than either SOL or SYN's minimums
required. Fee cross-check: same `BACKTEST_FEE_PCT=0.008`/side already baked into the PF=2.04
result above (harsher than the real ~1.2% live figure) — fee drag is not the constraint here
either, same conclusion as SOL/SYN.

**Bottom line: PUMP/USD is NOT promoted.** Against the ~$100 CAD currently uncommitted (BTC
$77 + SOL $376 = $453 of the $553.39 balance), even the calmest-case bare minimum ($785.67) is
~8x what's available — a substantially bigger capital ask than SYN's already-parked
$250-$690 gap. Ranking among the three open USD candidates by capital-actionability today:
SOL (promoted, done) > SYN (parked, gap ~$150-$590) > PUMP (parked, gap ~$685-$1,518) — despite
PUMP having the cleanest liquidity and lowest SL-exit rate of the three. Everything above is
groundwork for a future deposit decision, not a promotion. No `.env`/`UNIVERSE_WHITELIST`
change made.
