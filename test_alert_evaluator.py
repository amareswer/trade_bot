"""
Hermetic tests for AlertEvaluator's EARNINGS_SOON handling and its
live-executor-only held-position source.

2026-07-21 incident #1: an EARNINGS_SOON Telegram alert fired HIGH priority
for T (AT&T) — a watchlist symbol the user does not hold. The message gave
no indication that (a) nothing was owned and (b) new entries were already
auto-blocked by the earnings blackout (stock_bot/main.py
_is_earnings_blackout) — so it read as an urgent, actionable ping with
nothing to act on. Fix: priority + message now depend on whether the symbol
is actually held.

2026-07-21 incident #2 (found while investigating #1): "is this symbol
held" was answered ONLY from a static, hand-typed PORTFOLIO-env tracker
(stock_bot/.env) — the bot's own real IBKR position (bare "CM", opened by
the rule strategy) was invisible to every "held" check (PORTFOLIO_SELL,
RSI_OVERBOUGHT, RSI_OVERSOLD, and the new EARNINGS_SOON logic), because it
was never in that static list. Fixed by sourcing "held" from the live
executor's positions_snapshot() instead.

2026-07-22: the static PORTFOLIO-env tracker fallback was removed entirely
(stock_bot/portfolio/tracker.py) — it had gone fully dormant once an
executor became mandatory (2026-07-17) and was never read otherwise.
AlertEvaluator now takes no constructor argument; held_positions passed to
evaluate() (or None, when there's no executor) is the only source of truth.
"""
from __future__ import annotations

from datetime import date, timedelta
from types import SimpleNamespace

from stock_bot.alerts.alert import AlertType
from stock_bot.alerts.evaluator import AlertEvaluator


def _scan_result(symbol: str, days_to_earnings: int, price: float = 22.25) -> SimpleNamespace:
    earnings = SimpleNamespace(next_earnings_date=date.today() + timedelta(days=days_to_earnings))
    research = SimpleNamespace(earnings=earnings)
    return SimpleNamespace(
        symbol=symbol, company_name=symbol, price=price, currency="USD",
        rsi=None, trend=None, macd_note=None, research=research, verdict=None,
        source="watchlist",
    )


def test_earnings_soon_not_held_stays_medium_and_says_no_action():
    ev = AlertEvaluator()
    alerts = ev.evaluate([_scan_result("T", days_to_earnings=1)], held_positions={})

    earnings_alerts = [a for a in alerts if a.alert_type == AlertType.EARNINGS_SOON]
    assert len(earnings_alerts) == 1
    a = earnings_alerts[0]
    assert a.priority == "MEDIUM"          # not HIGH -> never reaches Telegram
    assert "not held" in a.message.lower()
    assert "no action needed" in a.message.lower()


def test_earnings_soon_held_within_1_day_stays_high():
    ev = AlertEvaluator()
    live = {"CM": (10.0, 117.82)}          # real IBKR position
    alerts = ev.evaluate([_scan_result("CM", days_to_earnings=1)], held_positions=live)

    earnings_alerts = [a for a in alerts if a.alert_type == AlertType.EARNINGS_SOON]
    assert len(earnings_alerts) == 1
    a = earnings_alerts[0]
    assert a.priority == "HIGH"            # actionable — reaches Telegram
    assert "you hold this position" in a.message.lower()


def test_earnings_soon_held_beyond_1_day_stays_medium():
    ev = AlertEvaluator()
    live = {"CM": (10.0, 117.82)}
    alerts = ev.evaluate([_scan_result("CM", days_to_earnings=3)], held_positions=live)

    earnings_alerts = [a for a in alerts if a.alert_type == AlertType.EARNINGS_SOON]
    assert len(earnings_alerts) == 1
    assert earnings_alerts[0].priority == "MEDIUM"


def test_earnings_soon_no_executor_means_not_held():
    # held_positions=None (no executor running, PAPER_TRADING_ENABLED=false)
    # — nothing can be "held" in that mode, so this must behave exactly like
    # the not-held case, never HIGH.
    ev = AlertEvaluator()
    alerts = ev.evaluate([_scan_result("CM", days_to_earnings=1)], held_positions=None)

    earnings_alerts = [a for a in alerts if a.alert_type == AlertType.EARNINGS_SOON]
    assert len(earnings_alerts) == 1
    assert earnings_alerts[0].priority == "MEDIUM"


if __name__ == "__main__":
    import sys
    failures = 0
    for t in [
        test_earnings_soon_not_held_stays_medium_and_says_no_action,
        test_earnings_soon_held_within_1_day_stays_high,
        test_earnings_soon_held_beyond_1_day_stays_medium,
        test_earnings_soon_no_executor_means_not_held,
    ]:
        try:
            t()
            print(f"PASS {t.__name__}")
        except AssertionError as e:
            failures += 1
            print(f"FAIL {t.__name__}: {e}")
    sys.exit(1 if failures else 0)
