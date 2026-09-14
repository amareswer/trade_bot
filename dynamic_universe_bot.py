"""
⚠️ DEPRECATED FOR PERFORMANCE EVALUATION (2026-09-13) — DO NOT USE FOR THAT.
============================================================================
This standalone runner has been SUPERSEDED by a live-engine integration
directly inside bot/main.py (see CLAUDE.md "Dynamic Crypto Universe"
section, and bot/main.py's _sync_dynamic_universe / _admit_dynamic_symbol /
_execute_approved_signal / _execute_ranked_dynamic_buys). A follow-up
review of THIS file's own run_cycle() found five confirmed correctness
bugs that make anything it produced unsuitable as evidence of how the
strategy performs:
  1. A successful fill never called sm.on_fill() — the state machine stayed
     IDLE forever, silently suppressing every later SELL signal.
  2. No stop-loss/take-profit execution path at all — only the raw
     strategy BUY/SELL signal was ever acted on; intra-candle SL/TP
     (the ONLY real exit path in the live bot) was completely missing.
  3. LiveExecutor(dry_run=True) here modeled zero fees and no slippage,
     silently overstating any P&L this runner produced.
  4. Restart recovery restored executor positions but never re-registered
     them with the capital pool, so a restart could let a new symbol
     wrongly claim a slot already held by a recovered position.
  5. risk.record_fill() was never called, so the daily/per-symbol trade
     counters this bot's own risk gates depend on never incremented.
None of these bugs were carried into the bot/main.py integration — see its
own tests (tests/crypto/test_dynamic_live_integration.py) proving each of
the five is fixed there. This file is kept only so its recorded run
history/logs (logs/dynamic_universe_*.log, logs/dynamic_paper_state/,
logs/dynamic_universe_backtest_*.md) are not lost — do not delete them, and
do not run this file expecting a meaningful P&L number. It still cannot
place a real order (dry_run stays hardcoded True below) and remains safe
to execute for pure code-reading/inspection purposes, but its OUTPUT is
not evidence of anything.
============================================================================

Dynamic universe PAPER-mode runner — broadly screens active spot pairs on the
configured exchange, scans many, trades few, and NEVER places a real order.

Safety by construction, not by convention:
  - Every executor is a LiveExecutor constructed with dry_run=True — hardcoded
    below, not read from any config flag, so there is no .env combination
    that turns this into live trading.
  - State files live under logs/dynamic_paper_state/ — entirely separate
    from the live bot's logs/live_state_BTC_CAD.json / _SOL_CAD.json. This
    script never opens, reads, or writes those files.
  - This script never touches logs/HALT. The live BTC/CAD + SOL/CAD bot's
    pause is independent of this paper system by design — pausing live
    trading does not (and should not) stop paper research.
  - Capital is an isolated paper bankroll (DYNAMIC_STARTING_CASH_CAD),
    unrelated to the real Kraken account balance.

Preserves the existing validated strategy unchanged (IndicatorStrategy via
bot.main.build_strategy()) — this system changes WHICH symbols get a chance
to signal and HOW MANY can hold a position at once, not the entry/exit rules
themselves. That is what lets an evaluation attribute any performance
difference to the universe-expansion change, not a strategy change.

Usage (paper mode only — see CLAUDE.md "Dynamic universe" section):
    DYNAMIC_UNIVERSE_ENABLED=true .venv/bin/python dynamic_universe_bot.py
    .venv/bin/python dynamic_universe_bot.py --once      # single cycle, for tests/inspection
    .venv/bin/python dynamic_universe_bot.py --dashboard-only   # just dump the current dashboard JSON
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from datetime import datetime, timezone as _tz

import ccxt

from config import cfg
from bot.dynamic.eligibility import DynamicUniverseScreener, ScreenResult
from bot.dynamic.lifecycle import DynamicSymbolManager, SymbolHandle
from bot.dynamic.ranking import RankableSignal, rank_buy_signals
from bot.execution.live_executor import LiveExecutor
from bot.execution.executor import OrderStatus
from bot.portfolio.capital_pool import CapitalPool
from bot.portfolio.position_manager import PositionManager
from bot.risk.risk_manager import RiskManager
from bot.state.trade_state import TradingStateMachine
from bot.strategy.threshold_strategy import Signal
from bot.main import build_strategy, _warmup_strategy, _fetch_completed_candle

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
    handlers=[
        logging.FileHandler(os.path.join("logs", "dynamic_universe_bot.log")),
        logging.StreamHandler(sys.stdout),
    ],
)
logger = logging.getLogger(__name__)

_DASHBOARD_JSON = os.path.join("logs", "dynamic_universe_dashboard.json")
_TIMEFRAME = "4h"   # matches the only validated live timeframe (CLAUDE.md "No day-trading")


class DynamicRunnerContext:
    """Holds everything one process needs across cycles. A thin class, not a
    dataclass, so it can carry live objects (exchange client, RiskManager)
    without dataclass equality/repr machinery getting in the way."""

    def __init__(self):
        d = cfg.dynamic
        if not d.enabled:
            logger.warning(
                "DYNAMIC_UNIVERSE_ENABLED is false — running anyway since this "
                "script was invoked directly, but note it is a no-op inside "
                "bot/main.py's live process either way (fully separate code path)."
            )
        self.cfg_dynamic = d
        self.exchange = ccxt.__dict__[cfg.exchange.exchange.lower()]({
            "enableRateLimit": True,
            "timeout": 15_000,
        })
        self.screener = DynamicUniverseScreener(d)
        self.capital_pool = CapitalPool(
            total_capital=d.starting_cash_cad,
            max_concurrent=d.max_concurrent_positions,
        )
        self.risk = RiskManager(state_path=os.path.join("logs", "dynamic_risk_state.json"))
        self.manager = DynamicSymbolManager(
            make_strategy=build_strategy,
            warmup_strategy=lambda strat, ex, tf, sym: _warmup_strategy(strat, ex, tf, symbol=sym),
            make_state_machine=lambda: TradingStateMachine(cooldown_ticks=cfg.risk.cooldown_ticks),
            make_position_manager=PositionManager,
            make_executor=self._make_executor,
        )
        self.last_screen: ScreenResult | None = None
        self.last_screen_at: float = 0.0
        self.quote_volume_by_symbol: dict[str, float] = {}
        self.cycle_count = 0
        self.fills: list[dict] = []
        self.blocked: dict[str, str] = {}   # symbol -> gate that blocked its BUY this cycle

    def _make_executor(self, symbol: str, state_path: str) -> LiveExecutor:
        return LiveExecutor(
            exchange_id   = cfg.exchange.exchange,
            symbol        = symbol,
            api_key       = "",     # public-data only — dry_run never places a real order
            api_secret    = "",
            starting_cash = 0.0,    # funded lazily on first successful capital_pool.allocate()
            dry_run       = True,   # HARDCODED — see module docstring
            state_path    = state_path,
        )

    def slot_cash_estimate(self) -> float:
        return self.cfg_dynamic.starting_cash_cad / self.cfg_dynamic.max_concurrent_positions

    def account_value(self) -> float:
        # available_cash already excludes allocated slots; add each
        # allocated slot's executor cash + current position value back in.
        total = self.capital_pool.available_cash
        for sym in self.capital_pool.allocated_symbols:
            handle = self.manager.get(sym)
            if handle is None:
                continue
            price = getattr(handle, "_last_price", 0.0) or handle.executor.avg_entry or 0.0
            total += handle.executor.cash + handle.executor.position * price
        return total


def refresh_universe_if_due(ctx: DynamicRunnerContext) -> None:
    age_h = (time.time() - ctx.last_screen_at) / 3600
    if ctx.last_screen is not None and age_h < ctx.cfg_dynamic.refresh_hours:
        return
    logger.info("dynamic universe: refreshing candidate universe …")
    result = ctx.screener.discover(ctx.exchange, slot_cash=ctx.slot_cash_estimate())
    ctx.last_screen = result
    ctx.last_screen_at = time.time()
    ctx.quote_volume_by_symbol = {c.symbol: c.quote_volume for c in result.eligible if c.quote_volume}
    logger.info(
        "dynamic universe: %d eligible, %d rejected (stale=%s)",
        len(result.eligible), len(result.rejected), result.stale,
    )
    _write_dashboard_json(ctx)


def run_cycle(ctx: DynamicRunnerContext) -> None:
    ctx.cycle_count += 1
    ctx.blocked = {}
    refresh_universe_if_due(ctx)

    eligible_symbols = set(ctx.last_screen.eligible_symbols) if ctx.last_screen else set()

    # Admit every eligible candidate (idempotent — cheap after the first
    # warmup) so it can be ticked and, if it signals BUY, ranked alongside
    # every other simultaneous signal. Admission is NOT the same as getting
    # capital — see point 3/5 of the design: scanning stays separate from
    # position limits, enforced below via capital_pool, not by restricting
    # which symbols get a strategy instance.
    for sym in eligible_symbols:
        ctx.manager.admit(sym, ctx.exchange, _TIMEFRAME)

    # Retire anything flat that has fallen out of eligibility. Symbols still
    # holding a position are untouched regardless of eligibility.
    retired = ctx.manager.sync_to_candidates(eligible_symbols)
    if retired:
        logger.info("dynamic universe: retired (flat, no longer eligible): %s", retired)

    buy_candidates: list[RankableSignal] = []
    price_by_symbol: dict[str, float] = {}

    for sym, handle in list(ctx.manager.items()):
        candle, new_ts = _fetch_completed_candle(ctx.exchange, handle.last_ts_ms, _TIMEFRAME, symbol=sym)
        if candle is None:
            continue
        handle.last_ts_ms = new_ts
        price = candle.close
        price_by_symbol[sym] = price
        handle._last_price = price   # used by account_value(); not persisted

        if not handle.strategy.is_warmed_up:
            continue

        raw_signal = handle.strategy.evaluate(candle)
        filtered_signal, _reason = handle.sm.filter_signal(raw_signal)
        handle.sm.tick()

        if filtered_signal == Signal.SELL and handle.executor.position > 1e-9:
            qty = handle.executor.position
            approval = ctx.risk.evaluate(
                Signal.SELL, price, handle.executor.portfolio, qty,
                account_value=ctx.account_value(), symbol=sym,
            )
            if approval:
                order = handle.executor.execute(Signal.SELL, price, quantity=qty)
                if order is not None and order.status == OrderStatus.FILLED:
                    ctx.capital_pool.release(sym, handle.executor.cash)
                    ctx.fills.append(_fill_record(sym, order, "SELL"))
            else:
                ctx.blocked[sym] = "risk_manager"

        elif filtered_signal == Signal.BUY and handle.executor.position <= 1e-9:
            if not ctx.capital_pool.can_open_position(sym):
                ctx.blocked[sym] = "capital_pool"
            else:
                buy_candidates.append(RankableSignal(
                    symbol=sym,
                    adx=handle.strategy.last_adx,
                    quote_volume=ctx.quote_volume_by_symbol.get(sym),
                ))

        try:
            handle.executor._save_state()
        except Exception as exc:
            logger.warning("dynamic universe: state save failed [%s]: %s", sym, exc)

    # Rank simultaneous BUY signals; admit into free slots highest-priority
    # first. Ranking uses only information already computed this cycle — no
    # future/aggregate data.
    for sym in rank_buy_signals(buy_candidates):
        if not ctx.capital_pool.can_open_position(sym):
            ctx.blocked[sym] = "capital_pool"
            continue
        handle = ctx.manager.get(sym)
        price = price_by_symbol[sym]
        allocated = ctx.capital_pool.allocate(sym)
        if allocated <= 0:
            continue
        handle.executor._portfolio.cash = allocated

        qty = cfg.calc_trade_qty(allocated, price)
        if cfg.strategy.atr_sizing_enabled and cfg.strategy.atr_sl_mult > 0:
            from bot.indicators.indicators import atr as _atr_fn
            atr_val = _atr_fn(
                list(handle.strategy._highs), list(handle.strategy._lows),
                list(handle.strategy._closes), cfg.strategy.atr_period,
            )
            if atr_val:
                qty = cfg.calc_trade_qty_atr_risk(
                    allocated, price, atr_val, cfg.strategy.atr_sl_mult,
                    cfg.backtest.stop_loss_pct or 0.015,
                )
        qty = min(qty, (allocated * 0.98) / price) if price > 0 else 0.0
        qty = round(qty, 6)

        approval = ctx.risk.evaluate(
            Signal.BUY, price, handle.executor.portfolio, qty,
            account_value=ctx.account_value(), symbol=sym,
        )
        if not approval:
            ctx.blocked[sym] = "risk_manager"
            ctx.capital_pool.release(sym, allocated)   # cancel the reservation — no pnl, slot freed
            handle.executor._portfolio.cash = 0.0
            continue

        order = handle.executor.execute(Signal.BUY, price, quantity=qty)
        if order is None or order.status != OrderStatus.FILLED:
            ctx.capital_pool.release(sym, allocated)   # rejected — return the reserved slot
            handle.executor._portfolio.cash = 0.0
        else:
            ctx.fills.append(_fill_record(sym, order, "BUY"))
        try:
            handle.executor._save_state()
        except Exception as exc:
            logger.warning("dynamic universe: state save failed [%s]: %s", sym, exc)

    ctx.risk.mark_valuation(ctx.account_value())
    _write_dashboard_json(ctx)
    logger.info(
        "dynamic universe cycle %d: %d admitted, %d open positions, %d fills so far",
        ctx.cycle_count, len(ctx.manager.active_symbols),
        len(ctx.manager.open_position_symbols()), len(ctx.fills),
    )


def _fill_record(symbol: str, order, side: str) -> dict:
    return {
        "symbol": symbol,
        "side": side,
        "price": order.price,
        "quantity": order.quantity,
        "fee_cost": order.fee_cost,
        "timestamp": (order.filled_at or datetime.now(_tz.utc)).isoformat(),
    }


def _write_dashboard_json(ctx: DynamicRunnerContext) -> None:
    """Snapshot for unified_dashboard.py's dynamic-universe card. A plain
    JSON file, not HTML — the dashboard renderer reads and formats it."""
    try:
        screen = ctx.last_screen
        payload = {
            "generated_at": datetime.now(_tz.utc).isoformat(),
            "enabled": ctx.cfg_dynamic.enabled,
            "cycle_count": ctx.cycle_count,
            "discovered": len(screen.all_candidates()) if screen else 0,
            "eligible": screen.eligible_symbols if screen else [],
            "rejected": [
                {"symbol": c.symbol, "reasons": c.reasons} for c in (screen.rejected if screen else [])
            ][:50],
            "admitted": ctx.manager.active_symbols,
            "open_positions": ctx.manager.open_position_symbols(),
            "blocked_this_cycle": ctx.blocked,
            "paper_cash_available": ctx.capital_pool.available_cash,
            "paper_total_capital": ctx.capital_pool.total_capital,
            "paper_account_value": ctx.account_value(),
            "fills_count": len(ctx.fills),
            "recent_fills": ctx.fills[-20:],
            "screen_stale": screen.stale if screen else True,
        }
        os.makedirs(os.path.dirname(_DASHBOARD_JSON), exist_ok=True)
        with open(_DASHBOARD_JSON, "w") as f:
            json.dump(payload, f, indent=2, default=str)
    except Exception as exc:
        logger.warning("dynamic universe: dashboard snapshot write failed: %s", exc)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--once", action="store_true", help="Run a single cycle and exit (inspection/testing).")
    parser.add_argument("--dashboard-only", action="store_true", help="Just print the current dashboard JSON and exit.")
    parser.add_argument("--interval", type=int, default=900, help="Seconds between cycles (default 900 = 15min).")
    args = parser.parse_args()

    if args.dashboard_only:
        if os.path.exists(_DASHBOARD_JSON):
            print(open(_DASHBOARD_JSON).read())
        else:
            print("{}")
        return

    ctx = DynamicRunnerContext()
    recovered = ctx.manager.restart_recovery(ctx.exchange, _TIMEFRAME)
    if recovered:
        logger.info("dynamic universe: restart recovery re-admitted %s", recovered)

    print(
        f"\n  DYNAMIC UNIVERSE — PAPER MODE (dry_run=True, hardcoded)\n"
        f"  Paper bankroll: ${ctx.cfg_dynamic.starting_cash_cad:.2f} CAD"
        f" / {ctx.cfg_dynamic.max_concurrent_positions} concurrent slots\n"
        f"  Quote currencies scanned: {ctx.cfg_dynamic.quote_list}\n"
        f"  This process never places a real order and never touches live state.\n",
        flush=True,
    )

    if args.once:
        run_cycle(ctx)
        return

    while True:
        try:
            run_cycle(ctx)
        except Exception:
            logger.exception("dynamic universe: cycle failed — continuing")
        time.sleep(args.interval)


if __name__ == "__main__":
    main()
