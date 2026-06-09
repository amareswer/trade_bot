"""Unit tests for RiskManager — all checks, no network."""

from bot.execution.executor import PaperExecutor, OrderStatus
from bot.risk.risk_manager import RiskManager, RiskConfig, BlockReason
from bot.strategy.threshold_strategy import Signal


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
    # max_drawdown_pct=0.50 ensures the drawdown check (Check 2) does not fire
    # before the daily loss check (Check 4) when cash is drained by 10%.
    ex, risk = _make(cash=10_000, daily_loss_limit_pct=0.05, max_drawdown_pct=0.50)
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
    ]
    for t in tests:
        t()
        print(f"  PASS  {t.__name__}")
    print(f"\nAll {len(tests)} tests passed.")
