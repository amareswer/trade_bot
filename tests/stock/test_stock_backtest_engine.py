"""Stock backtest engine — execution mechanics with scripted signals.

The strategy is injectable, so these tests verify the ENGINE's honesty
guarantees independent of strategy logic:
- next-open fills (never fill on the signal candle's close)
- intra-candle SL/TP with gap handling, SL-before-TP pessimism
- slippage on both fills, IBKR round-trip commission in net P&L
- trade_start_idx gating (walk-forward windows)
- end_of_data force-close excluded from stats
"""

from datetime import datetime, timedelta

from bot.strategy.threshold_strategy import Signal
from stock_bot.backtest.engine import (
    StockBacktestConfig,
    run_symbol,
)
from stock_bot.data.price_feed import Candle


class ScriptedStrategy:
    """Returns a pre-planned signal per candle index."""
    def __init__(self, signals: dict[int, Signal]):
        self._signals = signals
        self._i = -1

    def evaluate(self, candle) -> Signal:
        self._i += 1
        return self._signals.get(self._i, Signal.HOLD)


def mk_candles(bars: list[tuple[float, float, float, float]]) -> list[Candle]:
    """bars = [(open, high, low, close), ...] with daily timestamps."""
    t0 = datetime(2026, 1, 1)
    return [
        Candle(timestamp=t0 + timedelta(days=i), open=o, high=h, low=l,
               close=c, volume=1_000_000)
        for i, (o, h, l, c) in enumerate(bars)
    ]


def _cfg(**kw) -> StockBacktestConfig:
    return StockBacktestConfig(
        notional=kw.get("notional", 1_000.0),
        slippage_bps=kw.get("slippage_bps", 0),
        stop_loss_pct=kw.get("stop_loss_pct", 0.05),
        take_profit_pct=kw.get("take_profit_pct", 0.15),
    )


def test_buy_fills_next_open_not_signal_close():
    candles = mk_candles([
        (100, 101, 99, 100),   # 0: BUY signal on close
        (102, 103, 101, 102),  # 1: entry fills at open=102
        (102, 103, 101, 102),  # 2: SELL signal
        (104, 105, 103, 104),  # 3: exit fills at open=104
    ])
    strat = ScriptedStrategy({0: Signal.BUY, 2: Signal.SELL})
    res = run_symbol("TEST", candles, _cfg(), strategy=strat)
    assert res.n_trades == 1
    t = res.completed[0]
    assert t.entry_price == 102.0   # next open, NOT 100 (signal close)
    assert t.exit_price == 104.0
    assert t.exit_reason == "strategy"


def test_stop_loss_intra_candle():
    candles = mk_candles([
        (100, 101, 99, 100),   # 0: BUY
        (100, 101, 99, 100),   # 1: entry @ 100 → SL = 95
        (99, 100, 94, 96),     # 2: low 94 touches 95 → SL fill at 95
    ])
    strat = ScriptedStrategy({0: Signal.BUY})
    res = run_symbol("TEST", candles, _cfg(), strategy=strat)
    t = res.completed[0]
    assert t.exit_reason == "sl"
    assert t.exit_price == 95.0


def test_gap_down_fills_at_open_not_stop_price():
    candles = mk_candles([
        (100, 101, 99, 100),   # 0: BUY
        (100, 101, 99, 100),   # 1: entry @ 100 → SL = 95
        (90, 92, 88, 91),      # 2: gaps to 90, below the 95 stop
    ])
    strat = ScriptedStrategy({0: Signal.BUY})
    res = run_symbol("TEST", candles, _cfg(), strategy=strat)
    t = res.completed[0]
    assert t.exit_reason == "sl"
    assert t.exit_price == 90.0     # the gap open — never the fantasy 95 fill


def test_take_profit_intra_candle():
    candles = mk_candles([
        (100, 101, 99, 100),    # 0: BUY
        (100, 101, 99, 100),    # 1: entry @ 100 → TP = 115
        (110, 116, 109, 114),   # 2: high 116 crosses 115 → TP fill at 115
    ])
    strat = ScriptedStrategy({0: Signal.BUY})
    res = run_symbol("TEST", candles, _cfg(), strategy=strat)
    t = res.completed[0]
    assert t.exit_reason == "tp"
    assert abs(t.exit_price - 115.0) < 1e-9


def test_sl_wins_when_both_touchable_same_candle():
    candles = mk_candles([
        (100, 101, 99, 100),    # 0: BUY
        (100, 101, 99, 100),    # 1: entry @ 100
        (100, 120, 90, 110),    # 2: touches both SL 95 and TP 115 → SL (pessimistic)
    ])
    strat = ScriptedStrategy({0: Signal.BUY})
    res = run_symbol("TEST", candles, _cfg(), strategy=strat)
    assert res.completed[0].exit_reason == "sl"


def test_slippage_applied_both_ways():
    candles = mk_candles([
        (100, 101, 99, 100),
        (100, 101, 99, 100),    # entry open 100 → fill 100.10 (10 bps)
        (100, 101, 99, 100),    # SELL signal
        (100, 101, 99, 100),    # exit open 100 → fill 99.90
    ])
    strat = ScriptedStrategy({0: Signal.BUY, 2: Signal.SELL})
    res = run_symbol("TEST", candles, _cfg(slippage_bps=10), strategy=strat)
    t = res.completed[0]
    assert abs(t.entry_price - 100.10) < 1e-9
    assert abs(t.exit_price - 99.90) < 1e-9
    assert t.gross_pnl < 0          # flat market loses to slippage — by design


def test_commission_reduces_net_pnl():
    candles = mk_candles([
        (100, 101, 99, 100),
        (100, 101, 99, 100),    # entry @ 100, 10 shares
        (100, 101, 99, 100),    # SELL
        (100, 101, 99, 100),    # exit @ 100 — gross 0
    ])
    strat = ScriptedStrategy({0: Signal.BUY, 2: Signal.SELL})
    res = run_symbol("AAPL", candles, _cfg(), strategy=strat)
    t = res.completed[0]
    assert t.gross_pnl == 0.0
    assert t.commission >= 2.0      # IBKR US minimum $1 per side
    assert t.net_pnl == -t.commission


def test_trade_start_idx_blocks_early_entries():
    candles = mk_candles([(100, 101, 99, 100)] * 6)
    strat = ScriptedStrategy({0: Signal.BUY, 3: Signal.BUY})
    res = run_symbol("TEST", candles, _cfg(), trade_start_idx=2, strategy=strat)
    # signal at idx 0 is before the window → ignored; idx 3 fires, fills at idx 4
    assert len(res.trades) == 1
    assert res.trades[0].entry_ts == candles[4].timestamp


def test_end_of_data_close_excluded_from_stats():
    candles = mk_candles([
        (100, 101, 99, 100),    # BUY
        (100, 101, 99, 100),    # entry — never exits
        (101, 102, 100, 101),
    ])
    strat = ScriptedStrategy({0: Signal.BUY})
    res = run_symbol("TEST", candles, _cfg(), strategy=strat)
    assert len(res.trades) == 1
    assert res.trades[0].exit_reason == "end_of_data"
    assert res.n_trades == 0        # completed-only stats
    assert res.profit_factor == 0.0


def test_no_reentry_while_position_open():
    candles = mk_candles([(100, 101, 99, 100)] * 8)
    strat = ScriptedStrategy({0: Signal.BUY, 2: Signal.BUY, 3: Signal.BUY})
    res = run_symbol("TEST", candles, _cfg(), strategy=strat)
    assert len(res.trades) == 1     # extra BUYs while long are ignored


def test_pf_and_win_rate_math():
    # Two trades: +10% winner then SL loser
    candles = mk_candles([
        (100, 101, 99, 100),     # 0 BUY
        (100, 101, 99, 100),     # 1 entry @100
        (108, 111, 107, 110),    # 2 SELL signal
        (110, 111, 109, 110),    # 3 exit @110 → +$100 on 10 sh
        (100, 101, 99, 100),     # 4 BUY
        (100, 101, 99, 100),     # 5 entry @100
        (96, 97, 94, 95),        # 6 SL @95 → -$50
    ])
    strat = ScriptedStrategy({0: Signal.BUY, 2: Signal.SELL, 4: Signal.BUY})
    res = run_symbol("TEST", candles, _cfg(), strategy=strat)
    assert res.n_trades == 2
    assert res.win_rate == 50.0
    assert res.sl_exit_rate == 50.0
    wins, losses = 100.0 - res.completed[0].commission, 50.0 + res.completed[1].commission
    assert abs(res.profit_factor - wins / losses) < 1e-9
