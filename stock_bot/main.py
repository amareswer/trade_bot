"""
Stock bot — Phase 4 entry point.

Advisory only. No orders, no execution, no real money.
Loops through the watchlist every LOOP_INTERVAL seconds, prints
a terminal summary per symbol, and writes stock_dashboard.html.

Run from the repo root:
    python -m stock_bot.main
"""
from __future__ import annotations

import logging
import sys
import time
from datetime import datetime

from stock_bot.config import load
from stock_bot.data.price_feed import fetch_candles
from stock_bot.data.universe  import StockUniverse
from stock_bot.data.screener  import StockScreener
from stock_bot.indicators.indicators import (
    adx   as calc_adx,
    macd  as calc_macd,
    rsi   as calc_rsi,
    trend as calc_trend,
)
from stock_bot.research.aggregator  import fetch_research, ResearchReport, COMPANY_NAMES
from stock_bot.research.fear_greed   import fetch_fear_greed
from stock_bot.research.google_trends import fetch_market_trends
from stock_bot.ai.ai_engine         import AIEngine
from stock_bot.ai.verdict           import AIVerdict
from stock_bot.dashboard.renderer   import DashboardRenderer, ScanResult
from stock_bot.portfolio.tracker    import PortfolioTracker
from stock_bot.alerts.evaluator     import AlertEvaluator
from stock_bot.alerts.notifier      import AlertNotifier
from stock_bot.execution.paper      import StockPaperExecutor
from stock_bot.execution.base       import OrderStatus

from colorama import Fore, Style, init as _colorama_init
_colorama_init(autoreset=True)

# ---------------------------------------------------------------------------
# Logging — file only at INFO, stderr at WARNING
# ---------------------------------------------------------------------------
import os as _os
_LOG_DIR = _os.path.join(_os.path.dirname(_os.path.dirname(__file__)), "logs")
_os.makedirs(_LOG_DIR, exist_ok=True)

_fh = logging.FileHandler(_os.path.join(_LOG_DIR, "stock_bot.log"))
_fh.setLevel(logging.INFO)
_fh.setFormatter(logging.Formatter("%(asctime)s %(name)s %(levelname)s %(message)s"))

_ch = logging.StreamHandler(sys.stderr)
_ch.setLevel(logging.WARNING)

logging.getLogger().handlers.clear()
logging.getLogger().setLevel(logging.INFO)
logging.getLogger().addHandler(_fh)
logging.getLogger().addHandler(_ch)

# Silence yfinance/urllib noise in the terminal
logging.getLogger("yfinance").setLevel(logging.WARNING)
logging.getLogger("urllib3").setLevel(logging.WARNING)
logging.getLogger("peewee").setLevel(logging.WARNING)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------

_TREND_ICON = {"BULLISH": "▲", "BEARISH": "▼", "NEUTRAL": "—"}
_RSI_WARN   = 70.0
_RSI_OVER   = 30.0


def _fmt_rsi(val: float | None) -> str:
    if val is None:
        return "RSI=  — "
    flag = "⚠" if val >= _RSI_WARN or val <= _RSI_OVER else " "
    return f"RSI={val:4.1f}{flag}"


def _fmt_macd(result: tuple | None) -> str:
    if result is None:
        return "MACD=—                "
    m, s, h = result
    hist_sign = "+" if h >= 0 else ""
    return f"MACD={m:+6.2f}  sig={s:+6.2f}  hist={hist_sign}{h:.2f}"


def _fmt_adx(val: float | None) -> str:
    if val is None:
        return "ADX=  — "
    label = "trending" if val >= 25 else "ranging "
    return f"ADX={val:4.1f} {label}"


_SIGNAL_ICON = {"BUY": "✅ BUY", "SELL": "❌ SELL", "HOLD": "⏸  HOLD"}
_AI_PREFIX   = "                   "   # aligns continuation lines under the signal


def _print_verdict(verdict: AIVerdict) -> None:
    """Print the AI verdict block (3 lines)."""
    icon = _SIGNAL_ICON.get(verdict.signal, verdict.signal)

    if verdict.confidence >= 70:
        conf_col = Fore.GREEN
    elif verdict.confidence >= 50:
        conf_col = Fore.YELLOW
    else:
        conf_col = Style.DIM

    conf_str = f"{conf_col}{verdict.confidence}%{Style.RESET_ALL}"
    print(f"  🤖 AI ({verdict.trading_style:<8}):  {icon}  | Confidence: {conf_str}")

    if verdict.target_price is not None or verdict.stop_loss is not None:
        parts = []
        if verdict.target_price is not None:
            parts.append(f"Target: ${verdict.target_price:,.2f}")
        if verdict.stop_loss is not None:
            parts.append(f"Stop: ${verdict.stop_loss:,.2f}")
        print(f"  {_AI_PREFIX}{' | '.join(parts)}")

    if verdict.reasoning:
        print(f"  {_AI_PREFIX}\"{verdict.reasoning[:300]}\"")


def _print_research(report: ResearchReport) -> None:
    """Print the research block for one symbol (4 lines)."""
    # News
    if report.news:
        headlines = " · ".join(f'"{n.title[:60]}"' for n in report.news[:3])
        print(f"  📰 News ({len(report.news)}):  {headlines}")
    else:
        print("  📰 News:      no headlines found")

    # News sentiment
    s = report.sentiment
    if s.post_count > 0:
        print(f"  💬 Sentiment:  {s.label} (score: {s.score:+.2f}) | {s.post_count} headline{'s' if s.post_count != 1 else ''} scored")
    else:
        print("  💬 Sentiment:  no headlines to score")

    # Earnings
    e = report.earnings
    next_str  = str(e.next_earnings_date) if e.next_earnings_date else "unknown"
    print(f"  📅 Earnings:  Next: {next_str} | Last: {e.earnings_note}")


def _scan_symbol(
    symbol:     str,
    cfg,
    screener:   StockScreener | None = None,
    force_scan: bool = False,
) -> dict | None:
    """Fetch candles, run screener, print one-line indicator summary, return data for AI."""
    candles = fetch_candles(symbol, interval=cfg.interval, lookback_days=cfg.lookback_days)
    if candles is None:
        print(f"  {symbol} — no data available (market may be closed)")
        return None

    closes = [c.close for c in candles]
    highs  = [c.high  for c in candles]
    lows   = [c.low   for c in candles]
    price  = closes[-1]

    rsi_val   = calc_rsi(closes)
    trend_val = "NEW IPO" if len(closes) < 21 else calc_trend(closes)
    adx_val   = calc_adx(highs, lows, closes)
    macd_val  = calc_macd(closes)

    # Screener: skip AI on stocks with no momentum signal.
    # force_scan=True bypasses the screener (used for cfg.watchlist symbols).
    if not force_scan and screener is not None and not screener.screen(symbol, candles):
        print(f"  {symbol:<10}  ${price:>10,.2f}  — no signal (screened out)")
        return None

    icon = _TREND_ICON.get(trend_val, "—")
    print(
        f"  {symbol:<10}  ${price:>10,.2f}"
        f"  {_fmt_rsi(rsi_val)}"
        f"  {icon} {trend_val:<8}"
        f"  {_fmt_adx(adx_val)}"
        f"  {_fmt_macd(macd_val)}"
    )
    logger.info(
        "%s  price=%.2f  rsi=%s  trend=%s  adx=%s  macd=%s",
        symbol, price,
        f"{rsi_val:.1f}" if rsi_val else "n/a",
        trend_val,
        f"{adx_val:.1f}" if adx_val else "n/a",
        f"{macd_val[0]:+.2f}" if macd_val else "n/a",
    )

    return {
        "last_candle": candles[-1],
        "rsi":         rsi_val,
        "trend":       trend_val,
        "adx":         adx_val,
        "macd":        macd_val,
    }


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

def run() -> None:
    cfg = load()
    cfg.log_startup()

    # ── Universe / watchlist setup ────────────────────────────────────────────
    watchlist_symbols = list(cfg.watchlist)

    if cfg.universe_enabled:
        _universe = StockUniverse(refresh_hours=cfg.universe_refresh_hours)
        raw_symbols      = _universe.get_universe()
        universe_symbols = _universe.pre_filter(raw_symbols, cfg.universe_size)
        _universe_refreshed_at = time.time()
    else:
        _universe        = None
        universe_symbols = []
        _universe_refreshed_at = 0.0

    all_symbols = list(dict.fromkeys(watchlist_symbols + universe_symbols))

    screener = StockScreener() if cfg.screener_enabled else None

    # Initialise components once at startup
    ai_engine = AIEngine() if cfg.ai_enabled else None
    renderer  = DashboardRenderer(loop_interval=cfg.loop_interval)
    tracker   = PortfolioTracker(cfg.portfolio)
    evaluator = AlertEvaluator(tracker)
    notifier  = AlertNotifier(cfg)
    executor  = StockPaperExecutor(cfg.paper_starting_cash) if cfg.paper_trading_enabled else None

    print()
    print("  Stock Bot — Phase 6  (indicators + research + AI + dashboard + alerts + paper trading)")
    print(f"  My Watchlist : {', '.join(watchlist_symbols)}")
    if cfg.universe_enabled:
        print(f"  Universe     : S&P500 + TSX60 → top {cfg.universe_size} movers")
        print(f"  Top Movers   : {', '.join(universe_symbols)}")
    print(f"  Screener  : {'enabled' if screener else 'disabled'}")
    print(f"  Interval  : {cfg.interval}   Lookback: {cfg.lookback_days}d   Loop: {cfg.loop_interval}s")
    ai_status = "enabled" if (ai_engine and ai_engine.enabled) else "disabled"
    from stock_bot.ai.ai_engine import _MODEL as _AI_MODEL
    print(f"  AI engine : {ai_status}  (model: {_AI_MODEL})")
    if executor:
        print(f"  Paper trading: ON  cash=${cfg.paper_starting_cash:,.2f}  risk={cfg.paper_risk_pct*100:.0f}%/trade  min_conf={cfg.paper_min_confidence}%")
    print(f"  Dashboard : file://{_os.path.abspath('stock_dashboard.html')}")
    print()

    tick = 0
    try:
      while True:
        tick += 1
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        # Both fetched once per cycle and shared across all symbols
        fear_greed_data      = fetch_fear_greed()
        market_trends_score  = fetch_market_trends()

        print(f"  ── Scan #{tick:04d}  {now} {'─' * 30}")
        print(f"  😨 Market: Fear & Greed {fear_greed_data.score} — {fear_greed_data.label}  |  📈 Trends: {market_trends_score}/100")
        print(f"  {'Symbol':<10}  {'Price':>10}  {'RSI':^7}  {'Trend':<10}  {'ADX':^13}  MACD")
        print(f"  {'─'*10}  {'─'*10}  {'─'*7}  {'─'*10}  {'─'*13}  {'─'*30}")

        # Refresh universe watchlist when its TTL has elapsed
        if cfg.universe_enabled and _universe is not None:
            elapsed_h = (time.time() - _universe_refreshed_at) / 3600
            if elapsed_h >= cfg.universe_refresh_hours:
                raw_symbols      = _universe.get_universe()
                universe_symbols = _universe.pre_filter(raw_symbols, cfg.universe_size)
                all_symbols      = list(dict.fromkeys(watchlist_symbols + universe_symbols))
                _universe_refreshed_at = time.time()
                print(f"  Universe refreshed: {len(universe_symbols)} new movers")
                print(f"  My Watchlist : {', '.join(watchlist_symbols)}")
                print(f"  Top Movers   : {', '.join(universe_symbols)}")

        scan_results: list[ScanResult] = []

        for symbol in all_symbols:
            verdict: AIVerdict | None = None
            try:
                force_scan  = symbol in cfg.watchlist
                symbol_data = _scan_symbol(symbol, cfg, screener, force_scan=force_scan)

                if symbol_data is not None:
                    report = fetch_research(symbol, fear_greed_data=fear_greed_data, market_trends_score=market_trends_score)
                    _print_research(report)

                    if not cfg.ai_enabled:
                        print("  🤖 AI: disabled (AI_ENABLED=false in stock_bot/.env)")
                    elif ai_engine:
                        indicators = {
                            "rsi":         symbol_data["rsi"],
                            "trend":       symbol_data["trend"],
                            "adx":         symbol_data["adx"],
                            "macd_line":   symbol_data["macd"][0] if symbol_data["macd"] else None,
                            "macd_signal": symbol_data["macd"][1] if symbol_data["macd"] else None,
                        }
                        verdict = ai_engine.analyze(
                            symbol, symbol_data["last_candle"], indicators, report
                        )
                        _print_verdict(verdict)
                    else:
                        print("  🤖 AI: unavailable — check credentials in .env")

                    # ── Paper trading execution ───────────────────────────────
                    if executor is not None and verdict is not None:
                        px   = symbol_data["last_candle"].close
                        sig  = verdict.signal
                        cur  = "CAD" if symbol.upper().endswith(".TO") else "USD"
                        if sig == "BUY" and verdict.confidence >= cfg.paper_min_confidence:
                            if executor.position(symbol) == 0:
                                # Size on total portfolio value (cash + positions at avg cost)
                                snap     = executor.positions_snapshot()
                                pos_val  = sum(sh * co for sh, co in snap.values())
                                tot_val  = executor.cash + pos_val
                                alloc    = tot_val * cfg.paper_risk_pct
                                shares   = round(alloc / px, 4) if px > 0 else 0
                                if shares > 0:
                                    reason = f"BUY {verdict.confidence}% {verdict.trading_style}"
                                    order  = executor.buy(symbol, shares, px, reason=reason)
                                    if order.status == OrderStatus.FILLED:
                                        total = round(shares * px, 2)
                                        print(f"  📄 PAPER BUY:  {symbol}  {shares:.4f} shares @ ${px:,.2f} = ${total:,.2f}")
                                        print(f"                 Cash remaining: ${executor.cash:,.2f}")
                                    else:
                                        need = round(shares * px, 2)
                                        print(f"  📄 REJECTED:   {symbol}  insufficient cash")
                                        print(f"                 (need ${need:,.2f}  have ${executor.cash:,.2f})")
                        elif sig == "SELL":
                            held = executor.position(symbol)
                            if held > 0:
                                avg  = executor.avg_cost(symbol)
                                reason = f"SELL {verdict.confidence}% {verdict.trading_style}"
                                order  = executor.sell(symbol, held, px, reason=reason)
                                if order.status == OrderStatus.FILLED:
                                    proceeds = round(held * px, 2)
                                    trade_pnl = round((px - avg) * held, 2)
                                    pnl_pct   = round((px - avg) / avg * 100, 1) if avg else 0.0
                                    pnl_s     = "+" if trade_pnl >= 0 else ""
                                    print(f"  📄 PAPER SELL: {symbol}  {held:.4f} shares @ ${px:,.2f} = ${proceeds:,.2f}")
                                    print(f"                 Realized P&L: {pnl_s}${trade_pnl:,.2f} ({pnl_s}{pnl_pct}%)")
                                    print(f"                 Cash remaining: ${executor.cash:,.2f}")

                    # Build ScanResult for dashboard
                    macd_note: str | None = None
                    if symbol_data["macd"]:
                        ml, ms, _ = symbol_data["macd"]
                        if abs(ml - ms) < 0.001 * max(abs(ml), abs(ms), 0.01):
                            macd_note = "flat"
                        elif ml > ms:
                            macd_note = "bullish cross"
                        else:
                            macd_note = "bearish cross"

                    if verdict is None:
                        verdict = AIVerdict(
                            symbol=symbol, signal="HOLD", confidence=0,
                            target_price=None, stop_loss=None,
                            reasoning="AI disabled", trading_style="SWING",
                            timestamp=datetime.now(),
                        )

                    scan_results.append(ScanResult(
                        symbol       = symbol,
                        company_name = COMPANY_NAMES.get(symbol, symbol.split(".")[0]),
                        price        = symbol_data["last_candle"].close,
                        currency     = "CAD" if symbol.upper().endswith(".TO") else "USD",
                        rsi          = symbol_data["rsi"],
                        trend        = symbol_data["trend"],
                        macd_note    = macd_note,
                        research     = report,
                        verdict      = verdict,
                        source       = "watchlist" if symbol in cfg.watchlist else "universe",
                    ))

            except Exception as exc:
                print(f"  {symbol:<10}  ERROR: {exc}")
                logger.warning("scan failed for %s: %s", symbol, exc)
            print(f"  {'─' * 70}")

        # Build portfolio summary — paper executor takes precedence over static tracker
        portfolio_summary = None
        paper_summary     = None
        try:
            if executor is not None:
                portfolio_summary = executor.build_summary(scan_results)
                paper_summary     = executor.build_paper_summary(scan_results)
                executor.log_state({r.symbol: r.price for r in scan_results})
            else:
                portfolio_summary = tracker.build_summary(scan_results)
        except Exception as exc:
            logger.warning("Portfolio build failed: %s", exc)

        # End-of-cycle paper portfolio summary
        if executor is not None:
            try:
                price_map_cycle = {r.symbol: r.price for r in scan_results}
                unr     = executor.unrealized_pnl(price_map_cycle)
                rea     = executor.realized_pnl()
                tv      = executor.total_value(price_map_cycle)
                ret_pct = (tv - executor.starting_cash) / executor.starting_cash * 100 if executor.starting_cash else 0.0
                open_syms = list(executor.positions_snapshot().keys())
                unr_s   = "+" if unr >= 0 else ""
                rea_s   = "+" if rea >= 0 else ""
                ret_s   = "+" if ret_pct >= 0 else ""
                syms_str = ", ".join(open_syms) if open_syms else "none"
                print(f"  {'─' * 44}")
                print(f"  📄 Paper Portfolio Summary")
                print(f"  💵 Cash:           ${executor.cash:>10,.2f}")
                print(f"  📦 Open positions: {len(open_syms)} ({syms_str})")
                print(f"  📈 Unrealized P&L: {unr_s}${unr:,.2f}")
                print(f"  ✅ Realized P&L:   {rea_s}${rea:,.2f}")
                print(f"  💼 Total Value:    ${tv:>10,.2f}  ({ret_s}{ret_pct:.2f}%)")
                print(f"  {'─' * 44}")
            except Exception as exc:
                logger.warning("Paper summary print failed: %s", exc)

        # Evaluate and deliver alerts
        alerts = []
        try:
            alerts = evaluator.evaluate(scan_results)
            notifier.notify(alerts)
            logger.info("Alerts: %d triggered this cycle", len(alerts))
        except Exception as exc:
            logger.warning("Alert evaluation/notification failed: %s", exc)

        # Write dashboard
        try:
            renderer.render(scan_results, fear_greed_data, portfolio_summary, alerts, paper=paper_summary)
        except Exception as exc:
            logger.warning("Dashboard render failed: %s", exc)

        print()
        time.sleep(cfg.loop_interval)
    except KeyboardInterrupt:
        print("\n⛔ Stock Bot stopped. Goodbye!")


if __name__ == "__main__":
    run()
