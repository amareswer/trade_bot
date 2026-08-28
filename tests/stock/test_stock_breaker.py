"""
Unit tests for the stock-bot daily-loss circuit breaker baseline.

Bug fixed 2026-07-04: after a restart with open positions, the session
baseline was cash-only while the drawdown calc used cash + position marks —
the breaker could never fire (current total always far above baseline).
Baseline now includes the mark value (avg_cost) of restored positions.

Change 2026-08-28: the daily-loss breaker is now anchored to the calendar
day (UTC), not process start. `_day_open_equity` / `_day_start_iso` are
persisted and rolled on a UTC date change — a restart mid-drawdown no longer
forgets the day's loss. Non-sticky: recomputed each call, so a mid-day
recovery above the threshold re-enables BUYs (matches the weekly tier and
the crypto RiskManager).

State/CSV paths are monkeypatched to a tmp dir so tests never touch the real
paper_state.json / paper_trades.csv (see .memory/core.md rule 10).
"""
import os

import pytest

import stock_bot.execution.paper as _paper_mod

import stock_bot.execution.paper as paper_mod
from stock_bot.execution.paper import StockPaperExecutor
from stock_bot.data.price_feed import _sector_cache


@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    """Redirect all paper-state files to tmp and stub the sector lookup."""
    monkeypatch.setattr(paper_mod, "_STATE_JSON", str(tmp_path / "state.json"))
    monkeypatch.setattr(paper_mod, "_TRADES_CSV", str(tmp_path / "trades.csv"))
    monkeypatch.setattr(paper_mod, "_RESET_FLAG", str(tmp_path / ".reset"))
    monkeypatch.setattr(paper_mod, "_SETTLEMENT_CSV", str(tmp_path / "settlement.csv"))
    _sector_cache["TEST"] = "other"
    return tmp_path


def test_fresh_session_baseline_is_cash(sandbox):
    ex = StockPaperExecutor(starting_cash=1000.0)
    # Not seeded from __init__'s avg_cost-only mark — the first live-priced
    # refresh (or breaker check) anchors it.
    assert ex._day_open_equity is None
    assert ex._open_position_value == 0.0

    ex.refresh_position_marks({})
    assert ex._day_open_equity == 1000.0
    assert ex._day_start_iso == ex._current_day_iso()


def test_breaker_fires_after_restart_with_open_positions(sandbox):
    # Session 1: buy, run one scan cycle (seeds the day baseline), persist.
    ex = StockPaperExecutor(starting_cash=1000.0)
    ex.set_daily_loss_limit(0.03)
    ex.set_slippage_bps(0)
    order = ex.buy("TEST", 10, 50.0, reason="test")
    assert order.status.value == "FILLED"
    ex.refresh_position_marks({"TEST": 50.0})       # a real cycle -> day_open=$1000
    ex.save_state()

    # Session 2 (same-day restart): the day-open baseline must be restored
    # from disk ($1000), not re-based to the current lower equity.
    ex2 = StockPaperExecutor(starting_cash=1000.0)
    ex2.set_daily_loss_limit(0.03)
    assert ex2._day_open_equity == pytest.approx(1000.0)
    assert ex2._day_start_iso == ex2._current_day_iso()
    assert ex2._open_position_value > 0   # position marked at avg_cost, not 0

    # Price drops 14% → portfolio down ~7% from the $1000 baseline.
    # Under the old _session_start_value baseline (re-based to ~$1000 on
    # restart from live marks) this still tripped; the point here is the
    # baseline is the *persisted* $1000, not silently reset.
    ex2._update_position_value({"TEST": 43.0})
    assert ex2._is_daily_loss_tripped()


def test_breaker_silent_within_limit_after_restart(sandbox):
    ex = StockPaperExecutor(starting_cash=1000.0)
    ex.buy("TEST", 10, 50.0, reason="test")
    ex.save_state()

    ex2 = StockPaperExecutor(starting_cash=1000.0)
    ex2.set_daily_loss_limit(0.03)
    ex2._update_position_value({"TEST": 48.5})   # ~1.6% portfolio drawdown
    assert not ex2._is_daily_loss_tripped()


# ---------------------------------------------------------------------------
# Calendar-day anchoring (2026-08-28) — the core of the "session-lifetime ->
# calendar-day" change. `_current_day_iso` is a staticmethod so a test can
# pin "today" without touching the wall clock.
# ---------------------------------------------------------------------------

def _pin_day(monkeypatch, iso: str) -> None:
    monkeypatch.setattr(_paper_mod.StockPaperExecutor, "_current_day_iso",
                        staticmethod(lambda: iso))


def test_daily_baseline_persists_across_same_day_restart(sandbox, monkeypatch):
    _pin_day(monkeypatch, "2026-08-28")
    ex = StockPaperExecutor(starting_cash=1000.0)
    ex.set_daily_loss_limit(0.03)
    ex.set_slippage_bps(0)
    ex.buy("TEST", 10, 50.0, reason="test")
    ex.refresh_position_marks({"TEST": 50.0})        # cycle 1 -> day_open=$1000
    ex.refresh_position_marks({"TEST": 45.0})        # price crashes 10% -> total $950
    assert ex._is_daily_loss_tripped()              # 5% > 3%
    ex.save_state()

    # Restart on the SAME day: the $1000 baseline is restored, so the still-
    # depressed equity is still a >3% drawdown -> still blocked. (Old
    # behaviour re-based the baseline on every restart -> drawdown reset -> a
    # restart mid-bad-day silently re-enabled BUYs.)
    ex2 = StockPaperExecutor(starting_cash=1000.0)
    ex2.set_daily_loss_limit(0.03)
    assert ex2._day_open_equity == pytest.approx(1000.0)
    ex2.refresh_position_marks({"TEST": 45.0})
    assert ex2._is_daily_loss_tripped()
    order = ex2.buy("TEST2", 1, 10.0, reason="test")
    assert order.status.value == "REJECTED"
    assert "Daily loss limit" in order.reject_reason


def test_daily_baseline_rolls_and_unblocks_on_new_day(sandbox, monkeypatch):
    _pin_day(monkeypatch, "2026-08-28")
    ex = StockPaperExecutor(starting_cash=1000.0)
    ex.set_daily_loss_limit(0.03)
    ex.set_slippage_bps(0)
    ex.buy("TEST", 10, 50.0, reason="test")
    ex.refresh_position_marks({"TEST": 50.0})        # day_open=$1000
    ex.refresh_position_marks({"TEST": 45.0})        # total $950
    assert ex._is_daily_loss_tripped()              # tripped on 2026-08-28
    ex.save_state()

    # New UTC day, price still $45: the first cycle rolls the baseline to the
    # live mark-to-market total ($950), so the drawdown from the new day-open
    # is 0 -> BUYs allowed again.
    _pin_day(monkeypatch, "2026-08-29")
    ex2 = StockPaperExecutor(starting_cash=1000.0)
    ex2.set_daily_loss_limit(0.03)
    ex2.refresh_position_marks({"TEST": 45.0})
    assert ex2._day_open_equity == pytest.approx(950.0)
    assert ex2._day_start_iso == "2026-08-29"
    assert not ex2._is_daily_loss_tripped()


def test_daily_breaker_is_not_sticky_intraday(sandbox, monkeypatch):
    _pin_day(monkeypatch, "2026-08-28")
    ex = StockPaperExecutor(starting_cash=1000.0)
    ex.set_daily_loss_limit(0.03)
    ex.set_slippage_bps(0)
    ex.buy("TEST", 10, 50.0, reason="test")
    ex.refresh_position_marks({"TEST": 50.0})        # day_open=$1000

    ex.refresh_position_marks({"TEST": 45.0})        # 5% down -> tripped
    assert ex._is_daily_loss_tripped()
    ex.refresh_position_marks({"TEST": 49.0})        # recovers to 1% down
    assert not ex._is_daily_loss_tripped()           # not sticky -> re-enabled


def test_daily_baseline_seeds_on_new_day_while_process_stays_up(sandbox, monkeypatch):
    # No restart — the UTC date ticks over mid-run. The next scan cycle must
    # roll the baseline to the current live total.
    _pin_day(monkeypatch, "2026-08-28")
    ex = StockPaperExecutor(starting_cash=1000.0)
    ex.set_daily_loss_limit(0.03)
    ex.set_slippage_bps(0)
    ex.buy("TEST", 10, 50.0, reason="test")
    ex.refresh_position_marks({"TEST": 44.0})        # total $940, 6% down -> tripped
    assert ex._is_daily_loss_tripped()

    _pin_day(monkeypatch, "2026-08-29")
    ex.refresh_position_marks({"TEST": 44.0})        # same equity, new day
    assert ex._day_open_equity == pytest.approx(940.0)
    assert ex._day_start_iso == "2026-08-29"
    assert not ex._is_daily_loss_tripped()


# ---------------------------------------------------------------------------
# Weekly loss / drawdown-from-peak breaker tiers (added 2026-08-05).
# 0% slippage throughout so the $50 -> $X mark maps to a clean drawdown %:
# starting_cash=1000, buy 10 @ $50 -> cash=$500, position marked at fill
# price -> total stays exactly $1000 (peak) right after the fill.
# ---------------------------------------------------------------------------

def _open_1000_position(sandbox) -> StockPaperExecutor:
    ex = StockPaperExecutor(starting_cash=1000.0)
    ex.set_slippage_bps(0)
    ex.set_daily_loss_limit(0.99)   # isolate the new tiers from the (tighter) daily breaker
    ex.set_weekly_loss_limit(0.05)
    ex.set_drawdown_limits(0.10, 0.15, 0.20)
    order = ex.buy("TEST", 10, 50.0, reason="test")
    assert order.status.value == "FILLED"
    assert ex._peak_equity == pytest.approx(1000.0)
    return ex


def test_weekly_loss_blocks_buy_without_tripping_halt_or_kill(sandbox):
    ex = _open_1000_position(sandbox)
    ex._update_position_value({"TEST": 44.0})   # total=$940, 6% down — between 5% and 15%
    assert ex._is_weekly_loss_tripped()
    assert not ex._is_drawdown_halted()
    assert not ex._is_kill_switch_tripped()

    order = ex.buy("TEST2", 1, 10.0, reason="test")
    assert order.status.value == "REJECTED"
    assert "Weekly loss limit" in order.reject_reason


def test_drawdown_halt_blocks_buy_and_auto_lifts_on_recovery(sandbox):
    ex = _open_1000_position(sandbox)
    ex._update_position_value({"TEST": 33.0})   # total=$830, 17% down — between 15% and 20%
    order = ex.buy("TEST2", 1, 10.0, reason="test")
    assert order.status.value == "REJECTED"
    assert "Drawdown halt" in order.reject_reason
    assert not ex._is_kill_switch_tripped()     # 17% < 20% kill threshold

    # Recovery: back above the halt threshold — not sticky, must auto-lift.
    ex._update_position_value({"TEST": 50.0})   # back to $1000, 0% down
    assert not ex._is_drawdown_halted()


def test_kill_switch_blocks_buy_never_blocks_sell(sandbox):
    ex = _open_1000_position(sandbox)
    ex._update_position_value({"TEST": 20.0})   # total=$700, 30% down — past 20% kill threshold

    buy_order = ex.buy("TEST2", 1, 10.0, reason="test")
    assert buy_order.status.value == "REJECTED"
    assert "KILL SWITCH" in buy_order.reject_reason

    sell_order = ex.sell("TEST", 10, 20.0, reason="test")
    assert sell_order.status.value == "FILLED"


def test_kill_switch_stays_tripped_after_recovery_same_session(sandbox):
    # Sticky within a session: unlike the halt tier, equity recovering above
    # the kill-switch threshold must NOT silently clear it.
    ex = _open_1000_position(sandbox)
    ex._update_position_value({"TEST": 20.0})   # trips kill switch (30% down)
    assert ex._is_kill_switch_tripped()

    ex._update_position_value({"TEST": 50.0})   # fully recovers to $1000
    assert ex._is_kill_switch_tripped()          # still tripped


def test_kill_switch_persists_across_restart(sandbox):
    ex = _open_1000_position(sandbox)
    ex._update_position_value({"TEST": 20.0})   # 30% down — past the kill-switch threshold
    assert ex._is_kill_switch_tripped()          # evaluates + latches the sticky flag
    ex.save_state()

    ex2 = StockPaperExecutor(starting_cash=1000.0)
    assert ex2._kill_switch_tripped is True
    order = ex2.buy("TEST2", 1, 10.0, reason="test")
    assert order.status.value == "REJECTED"
    assert "KILL SWITCH" in order.reject_reason


def test_peak_equity_persists_across_restart(sandbox):
    ex = _open_1000_position(sandbox)
    ex._update_position_value({"TEST": 80.0})   # gain: total = $500 + $800 = $1300
    assert ex._peak_equity == pytest.approx(1300.0)
    ex.save_state()

    ex2 = StockPaperExecutor(starting_cash=1000.0)
    assert ex2._peak_equity == pytest.approx(1300.0)


def test_drawdown_status_warning_flag_tracks_threshold(sandbox):
    ex = _open_1000_position(sandbox)
    ex._update_position_value({"TEST": 50.0})   # at peak, 0% down
    assert ex.drawdown_status()["warning"] is False

    ex._update_position_value({"TEST": 39.0})   # total=$890, 11% down — past 10% warning
    status = ex.drawdown_status()
    assert status["warning"] is True
    assert status["drawdown_pct"] == pytest.approx(0.11, abs=0.001)


# ---------------------------------------------------------------------------
# Per-position ATR stop-loss override (opt-in ATR sizing, added 2026-08-05).
# ---------------------------------------------------------------------------

def test_position_stop_pct_defaults_to_baseline(sandbox):
    ex = StockPaperExecutor(starting_cash=1000.0)
    assert ex.get_position_stop_pct("TEST", 0.05) == 0.05


def test_position_stop_pct_override_and_persistence(sandbox):
    ex = StockPaperExecutor(starting_cash=1000.0)
    ex.buy("TEST", 10, 50.0, reason="test")
    ex.set_position_stop_pct("TEST", 0.08)
    assert ex.get_position_stop_pct("TEST", 0.05) == 0.08

    ex2 = StockPaperExecutor(starting_cash=1000.0)
    assert ex2.get_position_stop_pct("TEST", 0.05) == 0.08


def test_position_stop_pct_cleared_on_full_close(sandbox):
    ex = StockPaperExecutor(starting_cash=1000.0)
    ex.buy("TEST", 10, 50.0, reason="test")
    ex.set_position_stop_pct("TEST", 0.08)
    order = ex.sell("TEST", 10, 55.0, reason="test")
    assert order.status.value == "FILLED"
    assert ex.get_position_stop_pct("TEST", 0.05) == 0.05   # reverted to baseline


def test_position_stop_pct_survives_partial_close(sandbox):
    ex = StockPaperExecutor(starting_cash=1000.0)
    ex.buy("TEST", 10, 50.0, reason="test")
    ex.set_position_stop_pct("TEST", 0.08)
    order = ex.sell("TEST", 4, 55.0, reason="test")   # partial — position stays open
    assert order.status.value == "FILLED"
    assert ex.get_position_stop_pct("TEST", 0.05) == 0.08
