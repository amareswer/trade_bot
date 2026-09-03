---
name: per-symbol-exit-2026-09-03
description: BTC take-profit raised 10%→20% (per-symbol exit config); SOL keeps 10%. Strategy hash unchanged.
metadata:
  type: project
---

Date: 2026-09-03. Follows the 2026-09-02 exit-logic research ([[strategy-selectivity-2026-09-02]]
covers the entry side; this is the exit side).

## Decision

**`TAKE_PROFIT_PCT_BTC=0.20`** in `.env`. BTC's flat 10% take-profit was capping its trend
winners. SOL keeps the shared 10% TP (every wider-TP / trailing variant made SOL worse —
choppier price action).

## Why 20% (not 15% / trailing / off)

- TP20 beats the flat TP10 in **both** walk-forward windows: BTC/USDT TRAIN PF 1.20→1.37,
  VALIDATION 2.78→3.41. Also 5 of 6 rolling windows tested. The TRAIN improvement (older
  data) is the robustness signal — TP20 is not just fitting the recent trending regime.
- TP15 was *worse* than TP10 in the TRAIN window (1.15 vs 1.20).
- An 8% trailing stop was strongest of all (VAL PF 3.73) but needs a live SL-priority fix
  first (see "deferred" below) — not done.
- Pinned window (2024-03→2026-06) dips 1.94→1.87: that window ends just before the strong
  trend period TP20 targets. Sampling artifact; still clears the ≥1.72 fingerprint floor.

## Mechanism

`config._exit_overrides_by_base()` scans `TAKE_PROFIT_PCT_<BASE>` /
`TRAILING_STOP_PCT_<BASE>` / `TRAILING_STOP_ACTIVATION_PCT_<BASE>` (same `_<BASE>` pattern as
`MAX_SLOT_CASH_CAD_<BASE>`). `BacktestConfig.exit_params_for(symbol)` merges the override
over the shared keys. Routed through **both** `bot/main.py` (`_ep` per per-symbol loop
iteration) **and** `engine_kwargs_from_cfg(cfg, symbol=)` — live exits always match the
symbol's walk-forward.

Side fixes: `backtest.py` `--stop_loss`/`--take_profit` now default `None` (were clobbering
the per-symbol resolution with the shared value); `validate_symbol.py` / `screen_universe.py`
pass `symbol=` so a screened candidate resolves exit params for ITS base.

## Not affected

Strategy hash `b30f2f9e769c8d41` — UNCHANGED (exit params are `cfg.backtest` / `engine.run`,
not the hashed strategy files). New BTC fingerprint: pinned 27 / PF 1.87, rolling ~29 / 2.46.
SOL walk-forward re-confirmed identical (1.49 / 1.98). Suite 864→875 (+11
`tests/crypto/test_exit_overrides.py`). Crypto bot needs a restart to load it.

## Deferred

Live SL-priority divergence: the engine applies stops as trail(if armed) → ATR → fixed (one
only); `bot/main.py`'s `_ic_sl` ORs the ATR level with the 1.5% fixed SL so the tighter can
fire. Pre-existing live/backtest mismatch. Doesn't affect this TP change (TP is separate) but
blocks the trailing-stop option. Its own careful change.

Full trail: `CLAUDE_HISTORY.md` "Per-symbol take-profit — IMPLEMENTED 2026-09-03".
