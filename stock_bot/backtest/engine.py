"""Stock backtest engine — replays the rule-based strategy over daily candles.

Purpose: give the stock bot the same discipline the crypto bot has — no rule
trades paper (let alone live) money until it has shown edge on history.

Execution model (deliberately honest for daily bars):
- Signals are evaluated on a candle's CLOSE; entries/strategy exits fill at the
  NEXT candle's OPEN (you cannot trade a close you haven't seen finish).
- Stop-loss / take-profit are checked intra-candle against each candle's
  low/high from the entry candle onward. Gap-through fills at the open
  (worse than the stop for SL, better for TP). When both SL and TP are
  touchable in one candle, SL wins (pessimistic).
- Every fill pays slippage (bps, same knob as the paper executor) and every
  round trip pays the IBKR fixed commission model (same function the paper
  expectancy report uses — backtest and live report share one cost model).

The strategy itself is the crypto bot's validated IndicatorStrategy (Mode A/B
pullback + breakout) — imported, never modified, so crypto strategy hash
659d1c03987b72fd is untouched.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

from bot.strategy.indicator_strategy import IndicatorStrategy, IndicatorConfig
from bot.strategy.threshold_strategy import Signal
from bot.data.historical_feed import Candle as StrategyCandle
from stock_bot.analysis.paper_report import _round_trip_commission
from stock_bot.indicators.indicators import atr as _calc_atr

logger = logging.getLogger(__name__)


@dataclass
class BacktestTrade:
    symbol:      str
    entry_ts:    datetime
    exit_ts:     datetime
    entry_price: float      # fill price incl. slippage
    exit_price:  float      # fill price incl. slippage
    shares:      int
    commission:  float      # full round-trip commission
    exit_reason: str        # "sl" | "tp" | "strategy" | "end_of_data"

    @property
    def gross_pnl(self) -> float:
        return (self.exit_price - self.entry_price) * self.shares

    @property
    def net_pnl(self) -> float:
        return self.gross_pnl - self.commission

    @property
    def net_pct(self) -> float:
        cost_basis = self.entry_price * self.shares
        return (self.net_pnl / cost_basis * 100.0) if cost_basis else 0.0


@dataclass
class BacktestResult:
    symbol:          str
    trades:          list[BacktestTrade]
    candles_total:   int
    trade_start_idx: int

    @property
    def completed(self) -> list[BacktestTrade]:
        """Trades with a real exit — end-of-data force-closes excluded from stats."""
        return [t for t in self.trades if t.exit_reason != "end_of_data"]

    @property
    def n_trades(self) -> int:
        return len(self.completed)

    @property
    def win_rate(self) -> float:
        c = self.completed
        return (sum(1 for t in c if t.net_pnl > 0) / len(c) * 100.0) if c else 0.0

    @property
    def profit_factor(self) -> float:
        """Net-of-cost PF. inf when there are wins and zero losses."""
        wins   = sum(t.net_pnl for t in self.completed if t.net_pnl > 0)
        losses = -sum(t.net_pnl for t in self.completed if t.net_pnl <= 0)
        if losses == 0:
            return float("inf") if wins > 0 else 0.0
        return wins / losses

    @property
    def sl_exit_rate(self) -> float:
        c = self.completed
        return (sum(1 for t in c if t.exit_reason == "sl") / len(c) * 100.0) if c else 0.0

    @property
    def total_net_pnl(self) -> float:
        return sum(t.net_pnl for t in self.completed)


@dataclass
class StockBacktestConfig:
    """Cost + risk knobs. Strategy parameters live in IndicatorConfig."""
    notional:      float = 1_000.0   # $ per trade (fixed slice, no compounding)
    slippage_bps:  int   = 15        # per fill, same as PAPER_SLIPPAGE_BPS
    stop_loss_pct: float = 0.05      # same as PAPER_STOP_LOSS_PCT
    take_profit_pct: float = 0.15    # same as PAPER_TAKE_PROFIT_PCT
    indicator: IndicatorConfig = field(default_factory=IndicatorConfig)
    # ATR-based stop distance (added 2026-08-23, mirrors PAPER_ATR_SIZING_ENABLED's
    # paired stop-distance override in stock_bot/main.py / calc_shares_atr_risk).
    # None (default) = unchanged flat stop_loss_pct behavior, identical to before
    # this field existed. When set, each trade's SL distance is
    # min(ATR(14) * atr_sl_mult / entry_price, atr_sl_cap) computed ONCE at entry
    # fill (never repriced mid-trade — matches the live executor's own
    # once-at-BUY _atr_stop_pct, not a rolling trailing stop) and falls back to
    # the flat stop_loss_pct when ATR is unavailable (thin history at entry),
    # exactly like the live sizing path's own fallback.
    atr_sl_mult:   Optional[float] = None
    atr_sl_cap:    float = 0.50      # sanity cap — never a stop wider than 50%,
                                      # same cap the live executor applies


def run_symbol(
    symbol: str,
    candles: list,                    # stock_bot Candle or any OHLCV w/ timestamp
    cfg: StockBacktestConfig,
    trade_start_idx: int = 0,
    strategy=None,                    # injectable for tests; default = IndicatorStrategy
) -> BacktestResult:
    """Replay one symbol. Trades may only OPEN at index >= trade_start_idx —
    earlier candles still feed the strategy (warm indicators, no cold-start
    artifacts), which is how walk-forward sub-windows are produced."""
    if strategy is None:
        strategy = IndicatorStrategy(cfg.indicator)
    slip = cfg.slippage_bps / 10_000.0

    # Precomputed once for the optional ATR-stop path — cheap, only actually
    # read (via a slice up to the entry index) when a position fills and
    # cfg.atr_sl_mult is set.
    highs  = [c.high  for c in candles] if cfg.atr_sl_mult else None
    lows   = [c.low   for c in candles] if cfg.atr_sl_mult else None
    closes = [c.close for c in candles] if cfg.atr_sl_mult else None

    trades: list[BacktestTrade] = []
    in_pos        = False
    entry_price   = 0.0
    entry_ts: Optional[datetime] = None
    shares        = 0
    entry_sl_pct  = cfg.stop_loss_pct   # this trade's SL distance — flat by
                                         # default, overridden per-entry below
                                         # when ATR-stop mode is active
    pending_entry = False
    pending_exit  = False

    def close_trade(exit_px: float, ts: datetime, reason: str) -> None:
        nonlocal in_pos, pending_exit
        commission = _round_trip_commission(symbol, shares)
        trades.append(BacktestTrade(
            symbol=symbol, entry_ts=entry_ts, exit_ts=ts,
            entry_price=entry_price, exit_price=exit_px * (1 - slip),
            shares=shares, commission=commission, exit_reason=reason,
        ))
        in_pos = False
        pending_exit = False

    for i, c in enumerate(candles):
        # ── 1. Fill pending orders at this candle's OPEN ──────────────────
        if pending_entry:
            pending_entry = False
            fill = c.open * (1 + slip)
            n = int(cfg.notional / fill) if fill > 0 else 0
            if n > 0:
                in_pos, entry_price, entry_ts, shares = True, fill, c.timestamp, n
                entry_sl_pct = cfg.stop_loss_pct   # flat default; ATR override below
                if cfg.atr_sl_mult:
                    # ATR computed from candles up to and including the fill
                    # candle — same "known at entry, never repriced" semantics
                    # as the live executor's once-at-BUY _atr_stop_pct.
                    atr_val = _calc_atr(highs[:i + 1], lows[:i + 1], closes[:i + 1], period=14)
                    if atr_val and atr_val > 0 and fill > 0:
                        entry_sl_pct = min((atr_val * cfg.atr_sl_mult) / fill, cfg.atr_sl_cap)
        elif pending_exit and in_pos:
            close_trade(c.open, c.timestamp, "strategy")

        # ── 2. Intra-candle SL/TP (SL first — pessimistic) ────────────────
        if in_pos:
            sl_price = entry_price * (1 - entry_sl_pct)
            tp_price = entry_price * (1 + cfg.take_profit_pct)
            if c.low <= sl_price:
                close_trade(min(c.open, sl_price), c.timestamp, "sl")
            elif c.high >= tp_price:
                close_trade(max(c.open, tp_price), c.timestamp, "tp")

        # ── 3. Evaluate strategy on this candle's close ───────────────────
        sig = strategy.evaluate(StrategyCandle(
            timestamp=c.timestamp, open=c.open, high=c.high,
            low=c.low, close=c.close, volume=c.volume,
        ))

        if not in_pos and not pending_entry and sig == Signal.BUY and i >= trade_start_idx:
            pending_entry = True
        elif in_pos and sig == Signal.SELL:
            pending_exit = True

    if in_pos:
        last = candles[-1]
        close_trade(last.close, last.timestamp, "end_of_data")

    return BacktestResult(
        symbol=symbol, trades=trades,
        candles_total=len(candles), trade_start_idx=trade_start_idx,
    )
