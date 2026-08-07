"""Unit tests for RiskManager — all checks, no network."""

import json
import os
import tempfile
from datetime import date, timedelta

from bot.execution.executor import PaperExecutor, OrderStatus
from bot.risk.risk_manager import RiskManager, RiskConfig, BlockReason, _utc_today
from bot.strategy.threshold_strategy import Signal


# ── SL/TP bypass pattern ──────────────────────────────────────────────────────
# The intra-candle SL/TP path in bot/main.py calls executor.execute() directly
# instead of going through risk.evaluate(). This test verifies that pattern:
# even if the risk gate would block the SELL, the executor still fills the order.

def test_sl_tp_bypasses_risk_gate_in_halt():
    """SL/TP exits execute directly even when risk manager is in manual halt state."""
    ex   = PaperExecutor("BTC/CAD", quantity=0.01, starting_cash=1_000.0)
    risk = RiskManager(RiskConfig(halt=True))

    sl_price = 95_000.0

    # Confirm the risk gate would block SELL in halt state
    gate_result = risk.evaluate(Signal.SELL, sl_price, ex.portfolio, ex.portfolio.position)
    assert not gate_result, "Risk gate should block SELL when halt=True"
    assert gate_result.block_reason == BlockReason.HALT

    # SL/TP path bypasses risk gate — executor.execute() is called directly
    order = ex.execute(Signal.SELL, sl_price, quantity=ex.portfolio.position)
    assert order is not None
    assert order.status == OrderStatus.FILLED

    risk.record_fill()   # fill is still recorded for accounting
    assert risk.fills_today == 1


def _make(cash=100_000, qty=1.0, **cfg):
    executor = PaperExecutor("BTC/USDT", quantity=qty, starting_cash=cash)
    risk     = RiskManager(RiskConfig(**cfg))
    return executor, risk


# ── Check 1: manual halt ──────────────────────────────────────────────────

def test_halt_blocks_all_signals():
    ex, risk = _make(halt=True)
    for sig in (Signal.BUY, Signal.SELL):
        result = risk.evaluate(sig, 74_000, ex.portfolio, 1.0)
        assert not result
        assert result.block_reason == BlockReason.HALT

def test_halt_does_not_block_hold():
    ex, risk = _make(halt=True)
    result = risk.evaluate(Signal.HOLD, 74_000, ex.portfolio, 1.0)
    assert result.approved   # HOLD always passes

def test_resume_lifts_halt():
    ex, risk = _make(halt=True)
    risk.resume()
    result = risk.evaluate(Signal.BUY, 74_000, ex.portfolio, 0.001)
    assert result.approved


# ── Kill switch (sticky) — added 2026-08-07 breaker-tiering upgrade ────────

def test_kill_switch_trips_and_blocks_buy():
    ex, risk = _make(cash=10_000, max_drawdown_pct=0.50, weekly_loss_limit_pct=0.50, kill_switch_pct=0.20)
    risk.evaluate(Signal.HOLD, 100, ex.portfolio, 1.0)   # seeds peak at $10k
    ex.portfolio.cash = 7_500   # 25% down — past the 20% kill switch
    result = risk.evaluate(Signal.BUY, 100, ex.portfolio, 0.001)
    assert not result
    assert result.block_reason == BlockReason.KILL_SWITCH
    assert risk.kill_switch_tripped

def test_kill_switch_never_blocks_sell():
    ex, risk = _make(cash=10_000, max_drawdown_pct=0.50, weekly_loss_limit_pct=0.50, kill_switch_pct=0.20)
    risk.evaluate(Signal.HOLD, 100, ex.portfolio, 1.0)
    ex.portfolio.cash = 7_500
    result = risk.evaluate(Signal.SELL, 100, ex.portfolio, 1.0)
    assert result.approved

def test_kill_switch_is_sticky_survives_recovery():
    ex, risk = _make(cash=10_000, max_drawdown_pct=0.50, weekly_loss_limit_pct=0.50, kill_switch_pct=0.20)
    risk.evaluate(Signal.HOLD, 100, ex.portfolio, 1.0)
    ex.portfolio.cash = 7_500
    tripped = risk.evaluate(Signal.BUY, 100, ex.portfolio, 0.001)
    assert not tripped
    ex.portfolio.cash = 10_000   # equity fully recovers back to the peak...
    result = risk.evaluate(Signal.BUY, 100, ex.portfolio, 0.001)
    # ...but the kill switch stays tripped regardless — sticky, not auto-lifting.
    assert not result
    assert result.block_reason == BlockReason.KILL_SWITCH

def test_kill_switch_persists_across_restart():
    ex = PaperExecutor("BTC/USDT", quantity=1.0, starting_cash=10_000)
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "risk_state.json")
        cfg = RiskConfig(max_drawdown_pct=0.50, weekly_loss_limit_pct=0.50, kill_switch_pct=0.20)
        risk = RiskManager(cfg, state_path=path)
        risk.evaluate(Signal.HOLD, 100, ex.portfolio, 1.0)
        ex.portfolio.cash = 7_500
        risk.evaluate(Signal.BUY, 100, ex.portfolio, 0.001)   # trips it
        assert risk.kill_switch_tripped

        restarted = RiskManager(cfg, state_path=path)
        assert restarted.kill_switch_tripped   # survives restart — did NOT auto-clear

def test_kill_switch_takes_priority_over_max_drawdown():
    # Both tiers trip simultaneously — the more severe kill switch must be
    # the reported block_reason, not the (also-tripped) halt tier.
    ex, risk = _make(cash=10_000, max_drawdown_pct=0.05, kill_switch_pct=0.10, weekly_loss_limit_pct=0.50)
    risk.evaluate(Signal.HOLD, 100, ex.portfolio, 1.0)
    ex.portfolio.cash = 8_500   # 15% down — past both the 5% halt and 10% kill switch
    result = risk.evaluate(Signal.BUY, 100, ex.portfolio, 0.001)
    assert not result
    assert result.block_reason == BlockReason.KILL_SWITCH


# ── Max drawdown — halt tier, NOT sticky (previously untested in isolation) ─

def test_max_drawdown_blocks_when_exceeded():
    ex, risk = _make(cash=10_000, max_drawdown_pct=0.05, weekly_loss_limit_pct=0.50, kill_switch_pct=0.50)
    risk.evaluate(Signal.HOLD, 100, ex.portfolio, 1.0)   # seeds peak at $10k
    ex.portfolio.cash = 9_000    # 10% down — past the 5% halt
    result = risk.evaluate(Signal.BUY, 100, ex.portfolio, 0.001)
    assert not result
    assert result.block_reason == BlockReason.MAX_DRAWDOWN

def test_max_drawdown_not_sticky_recovers():
    ex, risk = _make(cash=10_000, max_drawdown_pct=0.05, weekly_loss_limit_pct=0.50, kill_switch_pct=0.50)
    risk.evaluate(Signal.HOLD, 100, ex.portfolio, 1.0)
    ex.portfolio.cash = 9_000
    tripped = risk.evaluate(Signal.BUY, 100, ex.portfolio, 0.001)
    assert not tripped
    ex.portfolio.cash = 10_000   # recovers back to peak
    result = risk.evaluate(Signal.BUY, 100, ex.portfolio, 0.001)
    assert result.approved   # not sticky — auto-lifts, unlike the kill switch


# ── Weekly loss limit — added 2026-08-07 ────────────────────────────────────

def test_weekly_loss_blocks_when_exceeded():
    ex, risk = _make(cash=10_000, max_drawdown_pct=0.50, kill_switch_pct=0.50, weekly_loss_limit_pct=0.05)
    risk.evaluate(Signal.HOLD, 100, ex.portfolio, 1.0)   # seeds week_open at $10k
    ex.portfolio.cash = 9_000    # 10% down — past the 5% weekly limit
    result = risk.evaluate(Signal.BUY, 100, ex.portfolio, 0.001)
    assert not result
    assert result.block_reason == BlockReason.WEEKLY_LOSS

def test_weekly_loss_allows_within_limit():
    ex, risk = _make(
        cash=10_000, max_drawdown_pct=0.50, kill_switch_pct=0.50,
        weekly_loss_limit_pct=0.05, daily_loss_limit_pct=0.50,
    )
    risk.evaluate(Signal.HOLD, 100, ex.portfolio, 1.0)
    ex.portfolio.cash = 9_600    # 4% down — within the 5% limit
    result = risk.evaluate(Signal.BUY, 100, ex.portfolio, 0.001)
    assert result.approved

def test_weekly_loss_resets_on_new_week():
    ex, risk = _make(cash=10_000, max_drawdown_pct=0.50, kill_switch_pct=0.50, weekly_loss_limit_pct=0.05)
    risk.evaluate(Signal.HOLD, 100, ex.portfolio, 1.0, candle_date=date(2026, 8, 3))   # Monday
    ex.portfolio.cash = 9_000
    tripped = risk.evaluate(Signal.BUY, 100, ex.portfolio, 0.001, candle_date=date(2026, 8, 3))
    assert not tripped
    assert tripped.block_reason == BlockReason.WEEKLY_LOSS
    # Next ISO week — baseline resets fresh off the current (still-down) value,
    # so the same balance is no longer "down" relative to its own new week-open.
    result = risk.evaluate(Signal.BUY, 100, ex.portfolio, 0.001, candle_date=date(2026, 8, 10))
    assert result.approved


# ── Non-blocking drawdown-warning tier — added 2026-08-07 ───────────────────

def test_drawdown_status_reports_warning_flag():
    ex, risk = _make(
        cash=10_000, drawdown_warning_pct=0.03,
        max_drawdown_pct=0.50, weekly_loss_limit_pct=0.50, kill_switch_pct=0.50,
    )
    risk.evaluate(Signal.HOLD, 100, ex.portfolio, 1.0)   # seeds peak at $10k
    assert not risk.drawdown_status(10_000)["warning"]
    status = risk.drawdown_status(9_600)   # 4% down — past the 3% warning threshold
    assert status["warning"]
    assert abs(status["drawdown_pct"] - 0.04) < 1e-9

def test_drawdown_status_never_blocks_trades():
    # Purely informational — evaluate() must still approve a BUY while the
    # warning tier is active. Only halt/kill-switch/drawdown-halt/weekly-loss
    # actually block.
    ex, risk = _make(
        cash=10_000, drawdown_warning_pct=0.03,
        max_drawdown_pct=0.50, weekly_loss_limit_pct=0.50, kill_switch_pct=0.50,
        daily_loss_limit_pct=0.50,
    )
    risk.evaluate(Signal.HOLD, 100, ex.portfolio, 1.0)
    ex.portfolio.cash = 9_600
    assert risk.drawdown_status(9_600)["warning"]
    result = risk.evaluate(Signal.BUY, 100, ex.portfolio, 0.001)
    assert result.approved


# ── Check 2: daily trade cap ──────────────────────────────────────────────

def test_daily_trade_cap_blocks_after_limit():
    ex, risk = _make(max_trades_per_day=2)
    risk.record_fill()
    risk.record_fill()
    result = risk.evaluate(Signal.BUY, 74_000, ex.portfolio, 0.001)
    assert not result
    assert result.block_reason == BlockReason.DAILY_TRADE_CAP

def test_daily_trade_cap_allows_up_to_limit():
    ex, risk = _make(max_trades_per_day=3)
    risk.record_fill()
    risk.record_fill()
    result = risk.evaluate(Signal.BUY, 74_000, ex.portfolio, 0.001)
    assert result.approved   # 2 fills, limit is 3 — still OK

# ── Check 3: daily loss limit ─────────────────────────────────────────────

def test_daily_loss_limit_blocks_when_exceeded():
    # max_drawdown_pct/weekly_loss_limit_pct=0.50 ensure the drawdown and
    # weekly-loss checks (Checks 2-4) do not fire before the daily loss check
    # (Check 6) when cash is drained by 10%.
    ex, risk = _make(
        cash=10_000, daily_loss_limit_pct=0.05,
        max_drawdown_pct=0.50, weekly_loss_limit_pct=0.50,
    )
    # Seed the day-open value at full $10k
    risk.evaluate(Signal.HOLD, 100, ex.portfolio, 1.0)
    # Manually drain portfolio cash to simulate a loss > 5%
    ex.portfolio.cash = 9_000    # 10% loss → exceeds daily_loss_limit (5%)
    result = risk.evaluate(Signal.BUY, 100, ex.portfolio, 0.01)
    assert not result
    assert result.block_reason == BlockReason.DAILY_LOSS

def test_daily_loss_limit_allows_within_limit():
    ex, risk = _make(cash=10_000, daily_loss_limit_pct=0.05)
    risk.evaluate(Signal.HOLD, 100, ex.portfolio, 1.0)
    ex.portfolio.cash = 9_600    # 4% loss — within limit
    result = risk.evaluate(Signal.BUY, 100, ex.portfolio, 0.001)
    assert result.approved

# ── Check 4: max position size ────────────────────────────────────────────

def test_position_size_blocks_oversized_buy():
    # Portfolio = $10k, max_position_pct = 2%  →  max position value = $200
    # Buying 1 BTC at $74k would be 740% of portfolio → blocked
    ex, risk = _make(cash=10_000, max_position_pct=0.02)
    result = risk.evaluate(Signal.BUY, 74_000, ex.portfolio, 1.0)
    assert not result
    assert result.block_reason == BlockReason.POSITION_SIZE

def test_position_size_allows_small_buy():
    # $10k portfolio, max 2% = $200. Buying 0.001 BTC @ $74k = $74 → 0.74% → OK
    ex, risk = _make(cash=10_000, max_position_pct=0.02)
    result = risk.evaluate(Signal.BUY, 74_000, ex.portfolio, 0.001)
    assert result.approved

def test_position_size_does_not_apply_to_sell():
    # SELL should never be blocked by position-size check
    ex, risk = _make(cash=10_000, max_position_pct=0.02)
    ex.portfolio.position = 10.0   # large position already held
    result = risk.evaluate(Signal.SELL, 74_000, ex.portfolio, 1.0)
    assert result.approved

# ── Multi-symbol: per-symbol trade cap + aggregate account breakers ───────

def test_per_symbol_trade_cap_is_isolated():
    ex, risk = _make(max_trades_per_day=2)
    risk.record_fill("BTC/CAD")
    risk.record_fill("BTC/CAD")
    blocked = risk.evaluate(Signal.BUY, 100, ex.portfolio, 0.001, symbol="BTC/CAD")
    assert not blocked
    assert blocked.block_reason == BlockReason.DAILY_TRADE_CAP
    # A different symbol still has its full daily budget
    allowed = risk.evaluate(Signal.BUY, 100, ex.portfolio, 0.001, symbol="XRP/CAD")
    assert allowed.approved


def test_account_value_drives_daily_loss_breaker():
    # Slot portfolio is flat, but the aggregate account is down 10% — the
    # daily-loss breaker must fire on the account, not the slot.
    ex, risk = _make(
        cash=10_000, daily_loss_limit_pct=0.05,
        max_drawdown_pct=0.50, weekly_loss_limit_pct=0.50,
    )
    risk.evaluate(Signal.HOLD, 100, ex.portfolio, 1.0, account_value=10_000.0)
    result = risk.evaluate(Signal.BUY, 100, ex.portfolio, 0.001, account_value=9_000.0)
    assert not result
    assert result.block_reason == BlockReason.DAILY_LOSS


def test_position_size_check_stays_per_slot():
    # Position sizing is per-slot even when a large aggregate account value is
    # supplied — a $77 slot must not size positions off a $10k account.
    ex, risk = _make(cash=100, max_position_pct=0.20, daily_loss_limit_pct=0.50)
    result = risk.evaluate(
        Signal.BUY, 100, ex.portfolio, 0.5,   # $50 = 50% of the $100 slot
        account_value=10_000.0,
    )
    assert not result
    assert result.block_reason == BlockReason.POSITION_SIZE


def test_per_symbol_fills_persist_across_restart():
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "risk_state.json")
        risk = RiskManager(RiskConfig(), state_path=path)
        risk.record_fill("BTC/CAD")
        risk.record_fill("BTC/CAD")
        risk.record_fill("XRP/CAD")

        restarted = RiskManager(RiskConfig(), state_path=path)
        assert restarted.fills_today == 3
        assert restarted.fills_today_for("BTC/CAD") == 2
        assert restarted.fills_today_for("XRP/CAD") == 1
        assert restarted.fills_today_for("ETH/CAD") == 0


# ── State persistence: breakers survive restarts ──────────────────────────

def test_state_persists_across_restart():
    ex = PaperExecutor("BTC/USDT", quantity=1.0, starting_cash=10_000)
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "risk_state.json")

        risk = RiskManager(RiskConfig(), state_path=path)
        risk.evaluate(Signal.HOLD, 100, ex.portfolio, 1.0)   # seeds day_open + peak
        risk.record_fill()
        risk.record_fill()

        restarted = RiskManager(RiskConfig(), state_path=path)
        assert restarted.fills_today == 2
        assert restarted.peak_value == risk.peak_value
        assert restarted.day_open_value == risk.day_open_value


def test_stale_day_counters_reset_but_peak_survives():
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "risk_state.json")
        yesterday = _utc_today() - timedelta(days=1)
        with open(path, "w") as fh:
            json.dump({
                "today":          yesterday.isoformat(),
                "fills_today":    5,
                "day_open_value": 9_999.0,
                "peak_value":     12_345.0,
            }, fh)

        risk = RiskManager(RiskConfig(), state_path=path)
        assert risk.fills_today == 0            # daily counters reset on a new day
        assert risk.day_open_value is None      # re-seeded on first evaluate()
        assert risk.peak_value == 12_345.0      # all-time peak never resets


def test_corrupt_state_file_starts_fresh():
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "risk_state.json")
        with open(path, "w") as fh:
            fh.write("{not json")

        risk = RiskManager(RiskConfig(), state_path=path)
        assert risk.fills_today == 0
        assert risk.peak_value == 0.0


def test_no_state_path_never_writes():
    with tempfile.TemporaryDirectory() as tmp:
        risk = RiskManager(RiskConfig())
        risk.record_fill()
        assert os.listdir(tmp) == []   # stateless mode leaves no files


# ── Integration: approved trade flows through to executor ─────────────────

def test_approved_trade_executes_and_records_fill():
    ex, risk = _make(cash=100_000, max_position_pct=0.10, max_trades_per_day=5)
    approval = risk.evaluate(Signal.BUY, 74_000, ex.portfolio, 0.01)
    assert approval.approved
    order = ex.execute(Signal.BUY, 74_000)
    risk.record_fill()
    assert order.status == OrderStatus.FILLED
    assert risk.fills_today == 1


if __name__ == "__main__":
    tests = [
        test_halt_blocks_all_signals,
        test_halt_does_not_block_hold,
        test_resume_lifts_halt,
        test_daily_trade_cap_blocks_after_limit,
        test_daily_trade_cap_allows_up_to_limit,
        test_daily_loss_limit_blocks_when_exceeded,
        test_daily_loss_limit_allows_within_limit,
        test_position_size_blocks_oversized_buy,
        test_position_size_allows_small_buy,
        test_position_size_does_not_apply_to_sell,
        test_approved_trade_executes_and_records_fill,
        test_sl_tp_bypasses_risk_gate_in_halt,
        test_state_persists_across_restart,
        test_stale_day_counters_reset_but_peak_survives,
        test_corrupt_state_file_starts_fresh,
        test_no_state_path_never_writes,
        test_per_symbol_trade_cap_is_isolated,
        test_account_value_drives_daily_loss_breaker,
        test_position_size_check_stays_per_slot,
        test_per_symbol_fills_persist_across_restart,
        test_kill_switch_trips_and_blocks_buy,
        test_kill_switch_never_blocks_sell,
        test_kill_switch_is_sticky_survives_recovery,
        test_kill_switch_persists_across_restart,
        test_kill_switch_takes_priority_over_max_drawdown,
        test_max_drawdown_blocks_when_exceeded,
        test_max_drawdown_not_sticky_recovers,
        test_weekly_loss_blocks_when_exceeded,
        test_weekly_loss_allows_within_limit,
        test_weekly_loss_resets_on_new_week,
        test_drawdown_status_reports_warning_flag,
        test_drawdown_status_never_blocks_trades,
    ]
    for t in tests:
        t()
        print(f"  PASS  {t.__name__}")
    print(f"\nAll {len(tests)} tests passed.")
