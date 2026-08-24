"""
Post-whitelist review checkpoint tracker.

Added 2026-08-23. Tracks progress toward the review checkpoint documented in
.memory/decisions/stock-whitelist-gate-removed-2026-08-23.md: once rule-based
BUYs on symbols outside the 4 originally backtest-PASSed ones (MRNA, AMD, RY,
PLTR) accumulate >=15 completed round-trips AND show a material win-rate/PF/
AI-agreement gap vs. those 4, that's the trigger to review — NOT automatically
change — the validation gate and/or the risk thresholds sized for the old
4-symbol universe.

REPORTING/AGGREGATION ONLY. This module reads paper_trades.csv/ibkr_trades.csv
(via ConfidenceBandTracker's existing BUY->SELL pairing, stock_bot/analysis/
accuracy_tracker.py — no new log, no duplicated pairing logic) and computes a
status snapshot. It never places, blocks, or modifies a trade, never touches a
risk-gate value, and setting `triggered=True` here changes nothing about how
the bot trades — it only makes the checkpoint visible on the dashboard instead
of requiring manual log digging.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional

from stock_bot.analysis.accuracy_tracker import ConfidenceBandTracker, _IBKR_CSV

# The 4 symbols that passed a stock_backtest.py walk-forward before
# RULE_WHITELIST stopped gating BUYs (logs/stock_backtest_20260710.md).
# "RY" is the actual live/tradeable NYSE cross-listing (RULE_WHITELIST holds
# "RY", not "RY.TO" — TSX symbols are permanently API-blocked, see CLAUDE.md);
# "RY.TO" is included defensively for any legacy/manual row using that ticker.
ORIGINAL_SYMBOLS = frozenset({"MRNA", "AMD", "RY", "RY.TO", "PLTR"})

# The date RULE_WHITELIST stopped gating rule-based BUYs — see CLAUDE_HISTORY.md.
WHITELIST_REMOVED_DATE = "2026-08-23"

# Trigger thresholds — mirror .memory/decisions/stock-whitelist-gate-removed-2026-08-23.md
# ("Post-whitelist review checkpoint") verbatim where that doc gives a number.
ROUND_TRIP_TRIGGER             = 15     # mirrors the crypto bot's 15-fill capital-gate convention
WIN_RATE_GAP_TRIGGER_PP        = 15.0   # percentage points
NON_ORIGINAL_PF_FAIL_THRESHOLD = 1.0
ORIGINAL_PF_HEALTHY_THRESHOLD  = 1.2    # the existing Gate 3 bar
# The decision doc's third condition ("AI-disagreement trades... underperform
# AI-agreement trades by a wide margin") doesn't give a number. Operationalized
# here at the same order of magnitude as the win-rate-gap trigger above — a
# documented choice made in this module, not derived from the decision doc's
# own text, and re-check that doc if this ever needs to move.
AI_AGREEMENT_GAP_TRIGGER_PP = 20.0
_MIN_TRADES_FOR_SPLIT       = 3   # below this a group's win rate is noise, not a comparison
                                   # (gates the PF-gap condition below — NOT the AI-agreement
                                   # split, which has its own, separate minimum)
# Minimum trades required on EACH side (agree AND disagree) before the
# AI-agreement gap can contribute to a trigger. Deliberately separate from
# _MIN_TRADES_FOR_SPLIT above: at n=3 per side, win rate is quantized in 33%
# steps and a 20pp gap can arise from pure noise (e.g. 2/3 vs 1/3 wins = a
# clean 33pp swing with zero real signal). 5 matches the existing precedent
# in this codebase — ConfidenceBandTracker.band_report()'s own "NEED MORE
# DATA" cutoff (accuracy_tracker.py) is n<5 — and stays reachable within the
# 15-30 total non-original round-trips expected near-term, even after
# excluding untagged/ai=NONE trades from the split. Added 2026-08-24 after
# an explicit sample-size review; confirmed with the user before implementing.
_MIN_TRADES_FOR_AI_SPLIT    = 5

_AI_TAG_RE = re.compile(r"ai=([A-Z]+)(\d*)")


def _parse_ai_tag(entry_reason: str) -> Optional[str]:
    """Parse the " | ai=BUY60" / " | ai=HOLD40" / " | ai=NONE" shadow-vote tag
    stock_bot/main.py appends to a RULE BUY's reason string. Returns the AI's
    signal ("BUY"/"HOLD"/"SELL") or None when there's no tag (older trades
    predating the shadow vote, a non-RULE-BUY entry like "BUY 70% LONGTERM",
    or an explicit "ai=NONE" meaning the AI verdict was unavailable that
    cycle)."""
    if not entry_reason:
        return None
    m = _AI_TAG_RE.search(entry_reason)
    if not m:
        return None
    signal = m.group(1)
    return None if signal == "NONE" else signal


def _win_rate(pairs: list[dict]) -> float:
    n = len(pairs)
    return (sum(1 for p in pairs if p["pnl_pct"] > 0) / n * 100.0) if n else 0.0


def _profit_factor(pairs: list[dict]) -> float:
    wins   = sum(p["pnl"] for p in pairs if p["pnl"] > 0)
    losses = -sum(p["pnl"] for p in pairs if p["pnl"] <= 0)
    if losses == 0:
        return float("inf") if wins > 0 else 0.0
    return wins / losses


@dataclass
class CheckpointStatus:
    round_trip_count:     int
    round_trip_target:    int   = ROUND_TRIP_TRIGGER
    non_original_win_pct: float = 0.0
    non_original_pf:      float = 0.0
    original_n:            int  = 0
    original_win_pct:     float = 0.0
    original_pf:           float = 0.0
    ai_agree_n:            int  = 0
    ai_agree_win_pct:      float = 0.0
    ai_disagree_n:          int  = 0
    ai_disagree_win_pct:   float = 0.0
    triggered:             bool = False
    trigger_reasons:        list = field(default_factory=list)

    @property
    def progress_pct(self) -> float:
        if self.round_trip_target <= 0:
            return 0.0
        return min(100.0, self.round_trip_count / self.round_trip_target * 100.0)


def compute_checkpoint_status(trades: Optional[list[dict]] = None) -> CheckpointStatus:
    """
    Build a CheckpointStatus snapshot.

    trades: pre-loaded, already-parsed trade rows (as returned by
    ConfidenceBandTracker.load_trades()) — pass this in tests. None (the
    default) loads paper_trades.csv + ibkr_trades.csv for real, the same
    combined source LiveTradingGate's Gate 2/Gate 3 already use.
    """
    tracker = ConfidenceBandTracker()
    if trades is None:
        trades = tracker.load_trades() + tracker.load_trades(_IBKR_CSV)
        trades.sort(key=lambda t: t.get("timestamp", ""))
    pairs = tracker.pair_trades(trades)

    since        = [p for p in pairs if p["entry_date"] >= WHITELIST_REMOVED_DATE]
    non_original = [p for p in since if p["symbol"] not in ORIGINAL_SYMBOLS]
    original     = [p for p in since if p["symbol"] in ORIGINAL_SYMBOLS]

    n        = len(non_original)
    non_win  = _win_rate(non_original)
    non_pf   = _profit_factor(non_original)
    orig_win = _win_rate(original)
    orig_pf  = _profit_factor(original)

    agree, disagree = [], []
    for p in non_original:
        signal = _parse_ai_tag(p.get("entry_reason", ""))
        if signal == "BUY":
            agree.append(p)
        elif signal in ("HOLD", "SELL"):
            disagree.append(p)

    agree_win    = _win_rate(agree)
    disagree_win = _win_rate(disagree)

    status = CheckpointStatus(
        round_trip_count     = n,
        non_original_win_pct = non_win,
        non_original_pf      = non_pf,
        original_n           = len(original),
        original_win_pct     = orig_win,
        original_pf          = orig_pf,
        ai_agree_n           = len(agree),
        ai_agree_win_pct     = agree_win,
        ai_disagree_n        = len(disagree),
        ai_disagree_win_pct  = disagree_win,
    )

    if n >= ROUND_TRIP_TRIGGER:
        reasons: list[str] = []
        if original and (orig_win - non_win) >= WIN_RATE_GAP_TRIGGER_PP:
            reasons.append(
                f"win rate {non_win:.0f}% is {orig_win - non_win:.0f}pp below "
                f"original-symbol win rate {orig_win:.0f}%"
            )
        if (n >= _MIN_TRADES_FOR_SPLIT and non_pf < NON_ORIGINAL_PF_FAIL_THRESHOLD
                and original and orig_pf >= ORIGINAL_PF_HEALTHY_THRESHOLD):
            reasons.append(
                f"PF {non_pf:.2f} < {NON_ORIGINAL_PF_FAIL_THRESHOLD:.1f} while "
                f"original-symbol PF holds at {orig_pf:.2f}"
            )
        if (len(agree) >= _MIN_TRADES_FOR_AI_SPLIT and len(disagree) >= _MIN_TRADES_FOR_AI_SPLIT
                and abs(agree_win - disagree_win) >= AI_AGREEMENT_GAP_TRIGGER_PP):
            reasons.append(
                f"AI-agree win rate {agree_win:.0f}% vs AI-disagree win rate "
                f"{disagree_win:.0f}% — {abs(agree_win - disagree_win):.0f}pp gap"
            )
        if reasons:
            status.triggered = True
            status.trigger_reasons = reasons

    return status
