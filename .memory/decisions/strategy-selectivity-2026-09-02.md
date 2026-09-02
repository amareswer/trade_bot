---
name: strategy-selectivity-2026-09-02
description: Audit of whether the crypto trend-following strategy is too selective — verdict NO modification; every loosening degrades the edge.
metadata:
  type: project
---

Date: 2026-09-02. Trigger: user (not a strategy person) asked for a fresh check on
whether the "only buy uptrends, sit out downtrends" design needs modification, prompted
by all 3 crypto symbols in a mild pullback (negative EMA spread) with the bot flat.

## Verdict: NO modification. The strategy is correctly (if anything slightly under-) selective.

## Evidence

**Fresh walk-forward** (train 2024, test 2025-02-22→present OOS), active config unchanged:

| | Train PF | OOS PF | Verdict |
|---|---|---|---|
| BTC/USDT | 1.20 | **2.78** | ✓ "genuine edge" |
| SOL/USDT | 1.49 | **1.98** | ✓ "genuine edge" |

Hash `b30f2f9e769c8d41` unchanged. Signal fidelity 100% (shadow reports). Note: backtest
*return* numbers are tiny (±0.5%) — ATR-risk-capped sizing compresses them; PF is the real
signal.

**Selectivity sweep** (`strategy_selectivity_sweep.py`, new research tool, wired into
nothing; `logs/strategy_selectivity_sweep_20260902.md`) on the OOS window:

- **EMA spread ↓ 0.2%:** BTC 19→28 trades but PF 2.78→1.60, return +0.5%→−1.8%, DD 2.4×
  deeper. SOL 26→31 trades, PF 1.98→1.26. Added trades are net losers.
- **Drop 200-EMA macro filter:** BTC +8 trades PF→1.82 (−return); SOL +17 trades PF→**1.15**.
  The filter is load-bearing.
- **ADX ↓ 12–15:** +2 trades each, PF slightly *down* (BTC 2.78→2.56). No benefit.
- **Tightening** spread to 0.8% *improves* everything (BTC PF 3.51, half the DD) —
  matches the WF attribution: winning entries averaged 0.89% spread vs 0.71% for losers.

**Attribution (BTC OOS):** winners held ~11 days median, losers ~2.4 days. Classic
trend-following — many small stop-outs, occasional big winners. To catch the winners you
must sit through the dead periods; that patience IS the edge.

## Why this is settled

Reinforces [[strategy-search-2026-08-28]] (3 alt strategies all failed to beat a passive
hold). Don't re-propose loosening ADX / EMA-spread / the 200-EMA filter without beating
these OOS numbers. The real lever for more activity is more validated symbols
([[multi-symbol-validation]] — deposit + FX-layer blocked), not this strategy.

Full narrative: `CLAUDE_HISTORY.md` → "Strategy selectivity audit — 2026-09-02".
