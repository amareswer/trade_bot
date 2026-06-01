# Memory Index

> Files live in `memory/` inside the project root.
> Add `memory/` to `.gitignore` to keep private, or commit to share with the team.

- [Trade Bot Project](memory/project_trade_bot.md) — full architecture, module map, config knobs, and run commands
- [Feature Plan](memory/feature_plan.md) — all planned/deferred/built features with decisions and designs (update every session)
- [Feedback: Workflow](memory/feedback_workflow.md) — discuss before building; note every change/plan in feature_plan
- [Execution Layer](memory/execution_layer.md) — PaperExecutor, Order lifecycle, Portfolio, P&L model (weighted avg), rejection rules
- [Risk Layer](memory/risk_layer.md) — RiskManager 4-check approval gate, daily state, config, usage pattern
- [State Machine](memory/state_machine.md) — IDLE/LONG/COOLDOWN states, position-aware filtering, dedup, cooldown config
- [Position Manager](memory/position_manager.md) — weighted avg entry, realized/unrealized PnL, trade history, wiring pattern
- [Security Audit](memory/security_audit.md) — 2026-05-28 audit findings, all fixes applied, pending key rotation
