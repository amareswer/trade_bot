---
name: index
description: "Map of .memory/ topic files — load a topic only when it comes up"
metadata:
  type: project
---

**Note:** the authoritative current-state docs are the repo's `CLAUDE.md` +
`CLAUDE_HISTORY.md`. This `.memory/` tree holds the deepest decision trails.
`progress/current.md` is stale (last real update 2026-08-06) — don't rely on it.

## Layer / subsystem notes (root of .memory/)
- `execution_layer.md` — LiveExecutor: native stops, limit-chase, fill recording, deadlock fix
- `risk_layer.md` — RiskManager tiers, breakers
- `state_machine.md` — TradingStateMachine, cooldowns
- `position_manager.md` — PositionManager
- `stock_bot.md` — stock-bot subsystem state
- `project_trade_bot.md` — crypto-bot subsystem state
- `security_audit.md`, `feature_plan.md`, `feedback_workflow.md`

## decisions/
- `timeframe-4h-validated.md` — 1h/day-trading FAILED walk-forward; 4h is the only validated live TF
- `swing-1d-validated.md` — swing book (since retired, FAST_ENABLED=false)
- `multi-symbol-validation.md` — SOL promoted; SYN/PUMP/LINK validation-ready, deposit + FX-layer blocked
- `fee-structure.md` — maker/taker assumptions, round-trip cost model
- `strategy-search-2026-08-28.md` — 3 alt strategies (mean-reversion / grid-DCA / momentum) ALL FAILED to beat a passive hold; search concluded
- `mean-reversion-experiment-2026-08-28.md` — mean-reversion detail (FAILED both bots)
- `strategy-selectivity-2026-09-02.md` — trend strategy is NOT too selective; every loosening degrades the OOS edge. NO modification
- `crypto-buy-overlays-2026-09-02.md` — Fear&Greed gate + redundant regime gate REMOVED; MTF daily-trend gate KEPT
- `2026-08-18-missed-buy-signal.md` — a real BTC BUY vetoed by the MTF gate (not a bug); the gate-logging fix that followed
- `stock-whitelist-gate-removed-2026-08-23.md` — RULE_WHITELIST no longer gates stock-bot BUYs (user request)
- `livetradinggate-gate-repair-2026-08-20.md` — IBKR readiness Gates 1–3, code-enforced
- `amd-whitelist-investigation-2026-08-20.md` — AMD Gate 1 small-sample noise (now 16/16 PASS)
- `silent-degradation-sweep-2026-08-27.md` — maker-fallback + MTF fail-open alerting
- `stock-bot-stability.md`, `stock-offline-audit-2026-08-20.md` — stock-bot hardening passes
- `telegram-control.md` — two-way Telegram command poller design + shared-token constraint
- `expert-practices-benchmark.md` — DSR/CSCV deferral rationale
- `known-gaps.md` — chronological bug log (20 entries; #19 TSX .TO orders, #20 display BrokenPipeError — both 2026-09-02)
