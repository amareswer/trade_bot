---
name: crypto-buy-overlays-2026-09-02
description: Audit of the extra BUY gates layered on the validated crypto strategy in bot/main.py — Fear&Greed gate removed, regime re-check removed, MTF daily-trend gate kept.
metadata:
  type: project
---

Date: 2026-09-02. Trigger: user asked whether the accumulated crypto-bot rules earn
their keep and why the bot barely trades.

**Finding first:** over ~2 months / ~203 live 4h candles, BTC/CAD's *strategy* emitted BUY
exactly once (2026-08-18, MTF-vetoed). Every safety/risk gate (correlation, candle
watchdog, RiskManager 7 tiers, native stop, slippage) has blocked **zero** live trades.
The bot's inactivity is the core strategy correctly sitting out a low-ADX BTC 4h regime
(ADX 9–12 vs 18 required), not the overlays. See [[strategy-selectivity-2026-09-02]].

## Decision 1 — Fear&Greed / external-signal BUY gate: REMOVED

- Deleted `bot/signals/external_signals.py`, `config.ExternalSignalsConfig` / `cfg.signals`,
  `bot/main.py` §2d + `ext_gate` construction + import, `_BUY_BLOCK_REASONS["external_signal"]`,
  `EXT_FNG_*` / `EXT_FUNDING_*` .env keys.
- Reason: `mtf_overlay_backtest.py` showed the FNG>75 veto net-negative or wash in every
  window (2022–24 BTC PF 1.47→1.21; 2024–26 BTC +0.08 / SOL −0.19). **0 live vetoes ever.**
  Cost a third-party API dependency (alternative.me) + a fail-open bypass-alert path.
  Funding leg was already dead (Kraken is spot).
- Committed d3d3aed. User-approved.

## Decision 2 — Independent "regime gate" (main.py §2e): REMOVED

- It re-checked `ADX ≥ adx_threshold` AND `EMA spread ≥ min_ema_spread_pct` from the SAME
  `strategy.last_adx` / same closes deque that `IndicatorStrategy._trend_signal` already
  gates on → a strategy BUY had already cleared both → the block could never flip one.
- Its `"regime"` blocked-gate label also collided with the strategy's own 200-EMA filter,
  an ambiguity that cost time in [[2026-08-18-missed-buy-signal]].
- Strategy hash unchanged (not a `bot/strategy/` file). Committed a2b83ed.

## Decision 3 — MTF 1D-BEARISH veto (main.py §2c): KEPT

- Regime-dependent: helps a little in the 2022 bear (BTC PF 1.47→1.50, shallower DD),
  hurts in the 2024–26 bull/chop (BTC PF 2.10→1.48). Roughly a wash over a full cycle.
- Kept because it's genuine bear-regime protection doing its designed job, and it has
  vetoed only 1 live signal ever. A straight removal isn't justified; if it becomes a
  recurring "missed the move" complaint the fix is a design change (slower daily lookback,
  or veto only when the daily trend is *deteriorating*) with its own walk-forward.
- The strategy's own 200-EMA macro filter stays untouched — it IS in the validated
  fingerprint.

## Tooling added

- `bot/backtest/engine.py`: opt-in `mtf_daily_closes` / `fng_by_date` / `fng_bear_max`
  params. Default `None` → inert, fingerprint byte-identical (verified: pinned window
  30 trades / PF 1.94 / `b30f2f9e769c8d41`). Replays the live-only overlays on history.
- `mtf_overlay_backtest.py` (repo root, research tooling, wired into nothing).
- `tests/crypto/test_overlay_gates.py` (+10). Suite 846→856.

Full narrative: `CLAUDE_HISTORY.md` → "Crypto BUY-overlay audit — 2026-09-02".
