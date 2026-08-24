"""
Tests for the post-whitelist review checkpoint tracker (stock_bot/analysis/
checkpoint_tracker.py, added 2026-08-23) — reporting/aggregation only, mirrors
the trigger conditions documented in .memory/decisions/
stock-whitelist-gate-removed-2026-08-23.md ("Post-whitelist review checkpoint").
"""
from __future__ import annotations

from datetime import datetime, timedelta

from stock_bot.analysis.checkpoint_tracker import (
    compute_checkpoint_status,
    ORIGINAL_SYMBOLS,
    WHITELIST_REMOVED_DATE,
    ROUND_TRIP_TRIGGER,
)


def _trade(ts: str, symbol: str, side: str, price: float, reason: str = "", confidence: int = 0) -> dict:
    """A trade dict in the same shape ConfidenceBandTracker.load_trades() returns."""
    return {
        "timestamp": ts, "symbol": symbol, "side": side, "shares": 1.0,
        "price": price, "total_value": price, "cash_remaining": 0.0,
        "reason": reason, "confidence": confidence,
    }


_CUTOFF = datetime.strptime(WHITELIST_REMOVED_DATE, "%Y-%m-%d")


def _round_trip(day: int, symbol: str, entry: float, exit_: float, reason: str = "RULE BUY", conf: int = 60):
    """One BUY+SELL pair `day` days on/after WHITELIST_REMOVED_DATE (day=0 = on the cutoff)."""
    d       = _CUTOFF + timedelta(days=day)
    ts_buy  = d.strftime("%Y-%m-%d") + " 09:30:00"
    ts_sell = d.strftime("%Y-%m-%d") + " 15:00:00"
    return [
        _trade(ts_buy,  symbol, "BUY",  entry, reason=reason, confidence=conf),
        _trade(ts_sell, symbol, "SELL", exit_, reason="RULE SELL"),
    ]


def test_no_trades_gives_zero_progress_not_triggered():
    status = compute_checkpoint_status(trades=[])
    assert status.round_trip_count == 0
    assert status.round_trip_target == ROUND_TRIP_TRIGGER
    assert status.progress_pct == 0.0
    assert status.triggered is False
    assert status.trigger_reasons == []


def test_original_symbols_excluded_from_the_count():
    trades = []
    for sym in ("MRNA", "AMD", "RY", "PLTR", "RY.TO"):
        trades += _round_trip(0, sym, 100, 110)
    status = compute_checkpoint_status(trades=trades)
    assert status.round_trip_count == 0          # none of these count toward non-original
    assert status.original_n == 5


def test_pre_cutoff_trades_excluded_even_on_non_original_symbol():
    # entry_date before WHITELIST_REMOVED_DATE — must not count
    trades = [
        _trade("2026-08-22 09:30:00", "NVDA", "BUY",  100, reason="RULE BUY", confidence=60),
        _trade("2026-08-22 15:00:00", "NVDA", "SELL", 110),
    ]
    status = compute_checkpoint_status(trades=trades)
    assert status.round_trip_count == 0


def test_on_cutoff_date_counts():
    trades = _trade("2026-08-23 09:30:00", "NVDA", "BUY", 100, reason="RULE BUY", confidence=60), \
             _trade("2026-08-23 15:00:00", "NVDA", "SELL", 110)
    status = compute_checkpoint_status(trades=list(trades))
    assert status.round_trip_count == 1


def test_below_sample_size_never_triggers_even_with_bad_results():
    trades = []
    for i in range(14):   # one short of the 15-trade trigger
        trades += _round_trip(i, "NVDA", 100, 90, reason="RULE BUY | ai=BUY60")   # all losers
    status = compute_checkpoint_status(trades=trades)
    assert status.round_trip_count == 14
    assert status.triggered is False


def test_win_rate_gap_triggers_review():
    trades = []
    # 15 non-original round-trips, all losers (0% win rate)
    for i in range(15):
        trades += _round_trip(i, "NVDA", 100, 90)
    # original-symbol round-trips in the same window, all winners (100% win rate)
    for i in range(5):
        trades += _round_trip(i, "MRNA", 100, 110)

    status = compute_checkpoint_status(trades=trades)
    assert status.round_trip_count == 15
    assert status.non_original_win_pct == 0.0
    assert status.original_win_pct == 100.0
    assert status.triggered is True
    assert any("win rate" in r for r in status.trigger_reasons)


def test_pf_gap_triggers_review():
    trades = []
    # non-original: small wins, one huge loss -> PF well under 1.0
    for i in range(14):
        trades += _round_trip(i, "NVDA", 100, 100.5)
    trades += _round_trip(14, "NVDA", 100, 50)   # one big loser drags PF down
    # original: healthy PF >= 1.2
    for i in range(5):
        trades += _round_trip(i, "AMD", 100, 130)

    status = compute_checkpoint_status(trades=trades)
    assert status.round_trip_count == 15
    assert status.non_original_pf < 1.0
    assert status.original_pf >= 1.2
    assert status.triggered is True
    assert any("PF" in r for r in status.trigger_reasons)


def test_ai_agreement_gap_triggers_review():
    trades = []
    # 8 AI-agree trades, all winners
    for i in range(8):
        trades += _round_trip(i, "NVDA", 100, 110, reason="RULE BUY | ai=BUY70")
    # 7 AI-disagree trades, all losers
    for i in range(8, 15):
        trades += _round_trip(i, "NVDA", 100, 90, reason="RULE BUY | ai=HOLD40")

    status = compute_checkpoint_status(trades=trades)
    assert status.round_trip_count == 15
    assert status.ai_agree_n == 8
    assert status.ai_disagree_n == 7
    assert status.ai_agree_win_pct == 100.0
    assert status.ai_disagree_win_pct == 0.0
    assert status.triggered is True
    assert any("AI-agree" in r for r in status.trigger_reasons)


def test_ai_agreement_gap_does_not_trigger_on_a_lopsided_small_sample():
    """Sample-size guard added 2026-08-24: 3 disagree trades (all losers) vs.
    12 agree trades (all winners) clears the old, too-thin 3-per-side floor
    and shows a huge (100pp) win-rate gap — but 3 is too few trades to be a
    real comparison, and must NOT contribute to a trigger. Reproduces the
    concrete "1 disagree vs 14 agree"-style scenario flagged in review."""
    trades = []
    for i in range(12):
        trades += _round_trip(i, "NVDA", 100, 110, reason="RULE BUY | ai=BUY70")   # all winners
    for i in range(12, 15):
        trades += _round_trip(i, "NVDA", 100, 90, reason="RULE BUY | ai=HOLD40")   # all losers

    status = compute_checkpoint_status(trades=trades)
    assert status.round_trip_count == 15
    assert status.ai_agree_n == 12
    assert status.ai_disagree_n == 3
    assert status.ai_agree_win_pct == 100.0
    assert status.ai_disagree_win_pct == 0.0        # a real 100pp gap...
    assert not any("AI-agree" in r for r in status.trigger_reasons)   # ...but too thin to count
    assert status.triggered is False                # no original-symbol trades in this
                                                      # scenario either, so nothing else fires


def test_healthy_population_does_not_trigger_at_15():
    # 15 non-original round-trips, evenly good, no gap vs original, no AI split issue
    trades = []
    for i in range(15):
        trades += _round_trip(i, "NVDA", 100, 110, reason="RULE BUY | ai=BUY70")
    for i in range(5):
        trades += _round_trip(i, "MRNA", 100, 110)

    status = compute_checkpoint_status(trades=trades)
    assert status.round_trip_count == 15
    assert status.triggered is False
    assert status.trigger_reasons == []


def test_ai_none_and_untagged_reasons_excluded_from_agree_disagree_split():
    trades = []
    trades += _round_trip(0, "NVDA", 100, 110, reason="RULE BUY | ai=NONE")
    trades += _round_trip(1, "NVDA", 100, 110, reason="BUY 70% LONGTERM")   # no ai= tag at all
    status = compute_checkpoint_status(trades=trades)
    assert status.round_trip_count == 2
    assert status.ai_agree_n == 0
    assert status.ai_disagree_n == 0


def test_progress_pct_caps_at_100():
    trades = []
    for i in range(20):   # well past the 15-trade target
        trades += _round_trip(i, "NVDA", 100, 105)
    status = compute_checkpoint_status(trades=trades)
    assert status.round_trip_count == 20
    assert status.progress_pct == 100.0


def test_symbol_classification_matches_original_symbols_constant():
    assert ORIGINAL_SYMBOLS == frozenset({"MRNA", "AMD", "RY", "RY.TO", "PLTR"})


def test_whitelist_removed_date_matches_documented_date():
    assert WHITELIST_REMOVED_DATE == "2026-08-23"
