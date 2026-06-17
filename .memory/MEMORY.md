# Memory Index

- [Core](core.md) — project summary, phase, stack, critical rules, working style (always load first)
- [Progress](progress/current.md) — current stage and what is in progress
- [Preferences](preferences/user.md) — working style: discuss before building, update memory after every session
- [Trade Bot Project](project_trade_bot.md) — full architecture, module map, config knobs, run commands
- [Feature Plan](feature_plan.md) — all planned/deferred/built features with decisions (update every session)
- [Feedback: Workflow](feedback_workflow.md) — discuss before building; note every change/plan in feature_plan
- [Risk Layer](risk_layer.md) — RiskManager 5-check approval gate, daily state, SELL bypass rules, config
- [Execution Layer](execution_layer.md) — PaperExecutor + LiveExecutor, Order lifecycle, Portfolio, P&L model, fees_paid, state persistence, all hardening complete
- [Multi-Symbol Validation](decisions/multi-symbol-validation.md) — ETH=first expansion, LINK=excluded, BTC=weak current regime, fee gating constraint, expansion decisions
- [Live Loop Bugs](errors/live-loop-bugs.md) — 4 bugs fixed Jun10–12: SL/TP gap, dashboard staleness, position-size rounding drift, root logger level
- [State Machine](state_machine.md) — IDLE/LONG/COOLDOWN states, position-aware filtering, dedup, cooldown config
- [Position Manager](position_manager.md) — weighted avg entry, realized/unrealized PnL, trade history, wiring pattern
- [Security Audit](security_audit.md) — 2026-05-28 audit findings, all fixes applied, pending key rotation
- [Stock Bot](stock_bot.md) — stock_bot/ phases 1–3 built; AI multi-provider (OpenRouter/Ollama local/cloud); Phase 4 dashboard next
