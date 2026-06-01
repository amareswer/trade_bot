---
name: security-audit
description: "Security and correctness audit findings from 2026-05-28 — what was found, fixed, and still needs action"
metadata: 
  node_type: memory
  type: project
  originSessionId: 561ac2ba-311f-4840-9ce1-8792408e3e37
---

Full security and correctness review completed 2026-05-28.

**Why:** Pre-git-init hardening + code correctness before the bot is used more seriously.

**How to apply:** Before adding new modules, check this list for patterns to avoid (float equality, stale fallbacks, hardcoded secrets). Before `git init`, confirm `.gitignore` is in place.

---

## Still requires user action

- **Rotate OpenRouter API key** — the key `sk-or-v1-5ab3f...` was in plaintext in `.env` at time of audit. User must go to openrouter.ai → API Keys → revoke and regenerate. The new key should be placed back in `.env`.

---

## Fixed in this session (2026-05-28)

| Severity | File | Issue | Fix applied |
|---|---|---|---|
| CRITICAL | `.gitignore` (new) | No gitignore — `.env` would be committed on `git init` | Created `.gitignore` covering `.env`, `__pycache__`, `logs/`, `.venv/` |
| HIGH | `executor.py:207` | Cost basis overwritten on each BUY instead of weighted avg | Weighted avg: `(prev_cost + price * qty) / new_total` |
| HIGH | `simulated_executor.py` | SELL allowed position to go negative (no guard) | Added check: returns early with warning if `position < quantity` |
| MEDIUM | `price_feed.py` | Stale fallback price had no TTL — could trade on hours-old data | Added `_MAX_PRICE_AGE_S = 120` — raises error beyond 2 min |
| MEDIUM | `position_manager.py:76` | `== 0.0` float equality on accumulated float quantity | Replaced with `< 1e-9` epsilon check |
| MEDIUM | `executor.py:211` | `position < quantity` strict float comparison | Added `- 1e-9` tolerance |
| MEDIUM | `main.py` | Trade logs not persisted (CLAUDE.md requirement) | Added `logs/trade_bot.log` FileHandler at INFO level |
| LOW | `ai_engine.py:165` | AI prompt hardcoded `"BTC/USDT"` regardless of config | `_build_prompt()` and `advise()` now accept `symbol` param; `main.py` passes `SYMBOL` |

---

## Architectural notes from audit

- **Dual P&L tracking**: `Portfolio._cost_basis` (in executor) and `PositionManager._avg_entry` both track cost basis. They are now kept consistent (both use weighted avg). The display always uses `PositionManager` as the authoritative source.
- **State machine prevents most cost-basis multi-buy issues**: LONG state blocks consecutive BUYs, so the weighted-avg fix only matters if the state machine is ever bypassed.
- **AI cannot initiate trades**: `merge_signals()` enforces HOLD-stays-HOLD; AI can only downgrade BUY/SELL to HOLD, never upgrade HOLD.
