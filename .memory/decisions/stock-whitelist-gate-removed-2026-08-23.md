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
