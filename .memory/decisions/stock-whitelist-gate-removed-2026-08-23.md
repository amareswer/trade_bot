---
name: stock-whitelist-gate-removed-2026-08-23
description: RULE_WHITELIST removed as a gate on stock_bot rule-based BUYs (2026-08-23) — user requested full-universe trading without per-symbol backtest validation as an entry precondition. Rule signal + scan-universe membership + risk tiers are now the only gates.
metadata:
  type: project
---

**Decision:** `stock_bot/main.py`'s `_rule_buy` no longer requires
`symbol.upper() in RULE_WHITELIST`. A rule-based BUY now fires on `rule_v.signal == "BUY"
and rule_v.warmed_up` alone, for any symbol reached by that cycle's scan universe.

**Why:** explicit user request — wants full-universe trading, not gated by a per-symbol
`stock_backtest.py` walk-forward pass. This deliberately reverses the whitelist-gating
discipline used everywhere else in this codebase (see [[project_trade_bot]]'s XRP/CAD
incident — a stale-validation symbol traded live for weeks before being caught, which is
exactly the failure mode this change re-opens for the stock bot). The user made this call
knowingly; it was not proposed by Claude.

**How to apply:** if asked to re-add a whitelist-style gate to stock bot BUYs, or if a
symbol trades that "shouldn't have edge," check here first — this is by design now, not a
regression. Do not "fix" it without the user asking to reverse this decision.

---

## What changed
- `stock_bot/main.py`: removed the local `_rule_whitelist` set, the whitelist condition in
  `_rule_buy`, the "(not in RULE_WHITELIST — no entry)" console note, and the
  `rule_whitelisted` field passed to `ScanResult`.
- `stock_bot/dashboard/renderer.py`: removed `ScanResult.rule_whitelisted`; both the
  "📐 Rule Signals" strip (`_rule_summary_html`) and the per-symbol card's "📐 Rules:" tag
  now show "→ buying" / "→ will buy" for any BUY signal (the summary strip still shows the
  pre-existing SIZE_SKIP note when `buy_alloc` can't cover 1 share — that's a sizing
  constraint, unrelated, and was left alone).

## What did NOT change (confirmed, not assumed)
- `stock_bot/config.py`'s `RULE_WHITELIST` env var loading (`rule_whitelist_str`) is still
  there and still required — `LiveTradingGate.check_gate1()`
  (`stock_bot/analysis/accuracy_tracker.py`, see [[livetradinggate-gate-repair-2026-08-20]])
  reads it directly to validate every whitelisted symbol against the latest
  `stock_backtest.py` run, as part of the code-enforced IBKR live-trading readiness check in
  `IBKRExecutor.__init__()`. That gate is about live-account readiness, not paper BUY entry
  — untouched, still enforced.
- `bot/strategy/*` and `build_indicator_config()` — not touched. Strategy fingerprint
  `b30f2f9e769c8d41` unaffected, no walk-forward re-run needed.
- Crypto bot (`bot/`, root `config.py`) — completely unaffected. `UNIVERSE_WHITELIST=BTC/CAD`
  and its walk-forward-gated re-entry rules are untouched; that whitelist discipline still
  applies to crypto.
- All five stock-bot risk-gate tiers (`PAPER_DAILY_LOSS_PCT`, `PAPER_WEEKLY_LOSS_PCT`,
  `PAPER_DRAWDOWN_HALT_PCT`, `PAPER_KILL_SWITCH_PCT`, sector-concentration/correlation/
  macro-blackout/VIX-crisis gates) in `stock_bot/execution/` — untouched, still the real
  safety net on every new entry post-change.

## What now gates a stock-bot BUY (post-change)
1. `rule_signal()` says BUY and is warmed up (unchanged Mode A/B logic in
   `stock_bot/strategy/rules.py` / `bot/strategy/indicator_strategy.py`).
2. The risk-gate tiers above.
3. **Scan-universe membership — now the only remaining filter on which symbols ever reach
   `rule_signal()` at all.** `cycle_symbols` (`stock_bot/main.py`, `run()`) =
   `watchlist_symbols` (`cfg.watchlist`, user `.env` `WATCHLIST`) + `top_movers`
   (`StockUniverse.get_universe()` → S&P500+TSX60 → `.pre_filter(..., cfg.universe_size,
   ...)`, capped at `UNIVERSE_SIZE` (default 20/day), only when `UNIVERSE_ENABLED=true`,
   default false) + currently-held symbols, deduped. Any non-watchlist symbol also has to
   clear `StockScreener.screen()` in `_fetch_symbol_data()` — a liquidity/momentum filter
   (min price, RSI extreme, recent MACD cross, or a large single-candle move), not a
   strategy-edge validation. `.TO` (TSX) symbols stay permanently advisory-only regardless —
   CIRO DMR 3200 blocks API orders on Canadian exchanges, unrelated to this change.

Test suite: 605 passed, unchanged count (no test referenced `rule_whitelisted`/
`_rule_whitelist`, confirmed by grep before editing). Full detail: CLAUDE_HISTORY.md,
dated entry 2026-08-23.

---

## Follow-up hardening pass (2026-08-23, same day)

Four defensive changes made after the gate removal above, plus this section — the AI
shadow-vote review criteria. See CLAUDE_HISTORY.md's 2026-08-23 hardening entry for full
detail on all of them (screener filter, sizing audit, threshold audit, gate audit). Summary:

1. **In-distribution ATR%/liquidity filter** added to `stock_bot/data/screener.py` —
   rejects a symbol whose ATR% or avg $ volume is far outside the range observed on the 4
   backtested-PASS symbols (MRNA/AMD/RY.TO/PLTR), only for symbols not already in
   `watchlist_set` (held positions and configured watchlist symbols are exempt, same scoping
   as the pre-existing screener). Rejection reason surfaces on the dashboard (new "🔬
   Screened Out" section), not just logged.
2. **Position sizing by volatility** — audited, walk-forward run, NOT enabled.
   `stock_bot/config.py`'s `calc_shares_atr_risk()` already implements "size ∝ 1/ATR%
   relative to a baseline, capped at the flat default" but was gated off
   (`PAPER_ATR_SIZING_ENABLED=false`). Reported real example sizes (MRNA/AMD/GM); user then
   asked to run the walk-forward CLAUDE.md already requires before enabling this live (it also
   swaps the SL trigger to ATR×2.0, not just position size). Built `validate_atr_sizing.py`
   (new standalone script — deliberately not a flag on `stock_backtest.py`, whose
   `logs/stock_backtest_latest.json` output `LiveTradingGate.check_gate1()` depends on) and a
   matching optional ATR-stop mode in `stock_bot/backtest/engine.py`
   (`StockBacktestConfig.atr_sl_mult`, default `None` = unchanged prior behavior). **Result:
   14/16 RULE_WHITELIST symbols PASS at ATR×2.0, but AMD and KO FAIL** — AMD is a genuine
   regression (PASSED under the original flat-5% backtest, fails full-window PF 1.05 < 1.2
   here). `PAPER_ATR_SIZING_ENABLED` left off. Full table: `logs/
   stock_backtest_atr_validation_20260823.md`. See [[project_trade_bot]]/CLAUDE_HISTORY.md
   for the complete writeup and options going forward (exclude AMD/KO, try a smaller mult,
   or leave sizing flat).
3. **Kill-switch/drawdown thresholds** — audited, NOT changed, per explicit instruction.
   `PAPER_DAILY_LOSS_PCT=0.03` (2026-06-19, original), `PAPER_WEEKLY_LOSS_PCT=0.05`,
   `PAPER_DRAWDOWN_HALT_PCT=0.15`, `PAPER_KILL_SWITCH_PCT=0.20` (all three 2026-08-05,
   commit `7d7c90fc`) — confirmed via `git blame`, matches CLAUDE.md exactly, no drift.
4. **Sector/correlation gate audit** — both confirmed genuinely generic/dynamic, not
   hardcoded to the original whitelist. `get_sector()` (`stock_bot/data/price_feed.py`) is a
   live yfinance `Ticker(sym).info` lookup for any symbol, cached; the correlation gate
   (`stock_bot/risk/correlation.py` + `_check_correlation_gate` in `main.py`) is pure Pearson
   math over candle closes already fetched for whatever `executor.positions_snapshot()`
   actually holds — no symbol list anywhere in either path. No gap found.

### Post-whitelist review checkpoint — AI shadow-vote + risk-threshold adequacy (new decision, 2026-08-23)

**Why this exists:** `stock_bot/main.py`'s rule-BUY path already logs an "AI shadow vote"
(`reason += f" | ai={verdict.signal}{verdict.confidence}"`) on every trade, with an existing
code comment: *"After ~30 trades, compare outcomes where the AI agreed vs disagreed — if
agreement is predictive, the AI earns veto power."* That plan was written before
RULE_WHITELIST was removed and doesn't distinguish backtested-PASS symbols from the newly
opened universe. This section adds that distinction, as a documented, dated decision made
BEFORE any bad outcome — not decided retroactively after one (the user's explicit ask).

The same checkpoint also covers whether the kill-switch/drawdown thresholds still fit.
`PAPER_DAILY_LOSS_PCT=0.03` (set 2026-06-19), `PAPER_WEEKLY_LOSS_PCT=0.05`,
`PAPER_DRAWDOWN_HALT_PCT=0.15`, `PAPER_KILL_SWITCH_PCT=0.20` (all three set 2026-08-05,
commit `7d7c90fc`) were sized for a 4-symbol validated universe (MRNA, AMD, RY, PLTR) —
the opened-up universe changes the risk profile these thresholds were originally calibrated
against, so their continued adequacy deserves the same look rather than a separate one.
This is one unified review moment, not two independent triggers: entry-quality (is the AI
shadow vote predictive on non-backtested symbols) and loss-limit adequacy (are the existing
thresholds still sized right for a wider, less-vetted symbol set) are two questions asked at
the same checkpoint, from the same trade sample, at the same time.

**Trigger for this review (both entry-quality and loss-limit adequacy — reinstating a lighter
validation gate is one possible outcome; retuning the risk thresholds is another; neither is
automatic):**
- **Sample size: ≥15 completed round-trips** on the non-backtested-symbol population
  specifically (i.e., rule BUYs on any symbol NOT in {MRNA, AMD, RY.TO, PLTR} — the only 4
  that ever passed a `stock_backtest.py` walk-forward). 15 mirrors the crypto bot's own
  15-fill live capital-gate threshold (CLAUDE.md, "Capital gate evaluation") — an existing
  convention in this codebase for "enough trades to start drawing conclusions," reused here
  rather than inventing a new number.
- **AND at least one of:**
  - Win rate on non-backtested-symbol trades is **≥15 percentage points below** win rate on
    backtested-symbol trades over the same window, OR
  - PF on non-backtested-symbol trades is **< 1.0** while backtested-symbol PF over the same
    window stays ≥ 1.2 (the existing Gate 3 bar), OR
  - AI-disagreement trades on non-backtested symbols underperform AI-agreement trades by a
    wide margin (the existing "~30 trades, agreed vs disagreed" comparison above, evaluated
    early — at 15 trades — specifically for this population, not deferred to 30).
- **What "review" means:** at the same checkpoint, (1) re-evaluate whether to reinstate a
  lighter validation gate for non-backtested symbols (e.g., a single-window
  `stock_backtest.py` PASS instead of the full 4-window RULE_WHITELIST bar, or an
  ATR%/liquidity-scaled position size floor), AND (2) re-evaluate whether
  `PAPER_DAILY_LOSS_PCT`/`PAPER_WEEKLY_LOSS_PCT`/`PAPER_DRAWDOWN_HALT_PCT`/
  `PAPER_KILL_SWITCH_PCT` still fit a wider, less-vetted symbol set — review, not necessarily
  change; the thresholds may well turn out still adequate. Both are decisions to make WITH
  the user at that point — not a rule to auto-revert RULE_WHITELIST removal or auto-tighten
  the risk thresholds on its own trigger. No code currently computes the entry-quality split
  automatically; it would need a query against `paper_trades.csv`/`ibkr_trades.csv` filtered
  by symbol ∉ {MRNA,AMD,RY.TO,PLTR}, which does not exist yet — flagged for whenever this
  trigger condition is actually reached, not built preemptively.
