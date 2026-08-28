---
name: amd-whitelist-investigation-2026-08-20
description: AMD failed LiveTradingGate Gate 1's walk-forward on 2026-08-20, verdict was small-sample noise from one thin 250d window. RESOLVED 2026-08-28 — re-run gives 16/16 PASS, AMD passes (the failing window's 3rd trade aged out → excluded as low-sample). AMD stays in RULE_WHITELIST, Gate 1 now 16/16. Closed.
metadata:
  type: project
---

**Why:** [[livetradinggate-gate-repair-2026-08-20]]'s Gate 1 fix produced its first real
result on 2026-08-20 (16 RULE_WHITELIST symbols, ~4m45s run): 15/16 PASS, **AMD FAIL**. This
file is the dedicated investigation of that one failure, kept separate from the gate-mechanism
file since it's about a specific symbol's status, not the gate machinery — re-check this
before deciding anything about AMD's whitelist status in a future session.

**How to apply:** if `stock_backtest.py` fails AMD again (or keeps failing it), read this
first — it has the exact evidence bar to compare against. Don't re-derive from scratch.

---

## The failure, precisely

From `logs/stock_backtest_latest.json` (run 2026-08-20T15:41:50Z):

| Window | Trades | PF | Net P&L | Verdict |
|---|---|---|---|---|
| full (all history) | 16 | 1.55 | +$269.11 | PASS |
| 750d | 8 | 2.48 | +$284.27 | PASS |
| 500d | 5 | 1.43 | +$61.28 | PASS |
| **250d** | **3** | **0.75** | **−$23.47** | **FAIL** |

Driven entirely by the 250d window: 1 win (~$72) + 2 losses (~$48 each), sitting right at
`MIN_TRADES_FOR_VERDICT=3` (the tool's own floor for a window to count toward the verdict at
all). Every longer window — including the full 16-trade history — passes comfortably, two of
them strongly.

## Why this reads as noise, not a real edge failure

1. **Real AMD price action over this exact window (2025-08-22→2026-08-20, fetched fresh via
   yfinance, not from any log) is a +176% rally** ($167.76→$463.21, 284% high-low range) —
   stair-step shaped: sharp trending bursts (May 2026 ADX mean 50.0, June 33.8 — genuinely
   parabolic) punctuated by consolidation (Sept 2025, Dec–Jan, March, July–Aug 2026 all
   showing many sub-18 ADX readings). This is structurally the kind of move a pullback/
   breakout strategy (Mode A/B) gets whipsawed at the edges of — it doesn't chase an
   already-overextended move (`max_ema_spread_pct` exists specifically for that), and pullback
   entries during a violent trend can get stopped out on a sharp shake before the trend
   resumes. Consistent with this window's 66.7% SL-exit rate. This is a DIFFERENT failure
   mode from the BTC/CAD drought investigation — that was a genuinely flat/ranging market with
   correctly zero signals; this is a strategy that traded, and traded well historically on
   this symbol, hitting an unlucky 3-trade patch.
2. **3 trades is far below any sample size this codebase treats as trustworthy for a PF
   signal.** The crypto bot's own capital-scaling gate requires 15+ live trades before
   treating PF as anything but variance (CLAUDE.md: *"A failing PF with clean fidelity means
   variance, not strategy failure — extend the window"*). This is a fifth of that floor.
3. **History confirms this is new and hasn't moved:** AMD passed cleanly through
   2026-07-10/07-15 (250d window had only 2 low-sample-excluded trades then). First failed
   2026-08-01, once a 3rd recent trade pushed the window past the exclusion threshold. Between
   the 2026-08-01 and 2026-08-20 runs, **AMD generated zero new trades** — the exact same
   3-trade snapshot persisted unchanged for 3 weeks — while the 500d window's PF swung from
   1.06 (would itself have failed) to 1.43 (comfortably passes) purely from one older trade
   rolling out of that window's boundary between runs. Direct evidence of small-sample
   window-boundary instability, not a stable worsening trend.

## Verdict — AMD NOT removed, monitor only

Decided 2026-08-20: **do not remove AMD from `RULE_WHITELIST`** based on this result. Gate 1
itself is working exactly as designed (no small-sample exception built in, so FAIL is the
mechanically correct output given its criteria) — but the underlying claim "AMD doesn't
belong in the whitelist" isn't well-supported by the evidence. Nothing here resembles the
stable, worsening pattern behind a real removal (e.g. UBER's documented decayed edge, or
LINK/DOGE's permanent crypto-side exclusion — see [[multi-symbol-validation]] for that
lineage's shape of a genuine, well-evidenced removal).

**Re-check trigger:** re-run `stock_backtest.py` in a few weeks and look specifically at
whether AMD's 250d window has accumulated more trades (5-10+, not 3). If it's *still* failing
once the window holds a real sample, that would be genuine signal worth acting on — until
then this is "keep watching, don't force it," the same standing posture this file's sibling
memory already applies to the crypto capital gate.

No config or whitelist changes made as part of this investigation.

---

## Re-check 2026-08-28 — AMD now PASSES, exactly as predicted

Re-ran `stock_backtest.py` over all 16 RULE_WHITELIST symbols. **16/16 PASS, AMD verdict PASS.**

| Window | 2026-08-20 | 2026-08-28 |
|---|---|---|
| full | 16 tr, PF 1.55 | 16 tr, PF 1.55 (**identical** — zero new AMD trades in the interim) |
| 750d | 8 tr, PF 2.48 | 8 tr, PF 2.48 (identical) |
| 500d | 5 tr, PF 1.43 | 5 tr, PF 1.43 (identical) |
| **250d** | **3 tr, PF 0.75 → FAIL** | **2 tr, PF 1.70, `low_sample=true` → EXCLUDED from verdict** |

The single failing window's 3rd trade aged out past the 250-day boundary between runs. With
only 2 trades it's now below `MIN_TRADES_FOR_VERDICT` and doesn't count toward the verdict at
all — exactly the "small-sample window-boundary instability" this file's #3 point called out.
Nothing about AMD's actual edge changed (the full 16-trade history is byte-identical). The
re-check trigger ("5-10+ trades in the 250d window") was NOT met — the window got *smaller*,
not larger — but the practical outcome (AMD off the failing list) is the same.

**Verdict: closed. AMD stays in RULE_WHITELIST. Gate 1 is now 16/16.** No further monitoring
needed unless a future run flips it back — in which case the same "is the 250d window a real
sample now?" question applies before acting.
