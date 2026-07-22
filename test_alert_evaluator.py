"""
Hermetic tests for AlertEvaluator's EARNINGS_SOON handling and the
held-position source (live executor snapshot vs static PORTFOLIO tracker).

2026-07-21 incident #1: an EARNINGS_SOON Telegram alert fired HIGH priority
for T (AT&T) — a watchlist symbol the user does not hold. The message gave
no indication that (a) nothing was owned and (b) new entries were already
auto-blocked by the earnings blackout (stock_bot/main.py
_is_earnings_blackout) — so it read as an urgent, actionable ping with
nothing to act on. Fix: priority + message now depend on whether the symbol
is actually held.

2026-07-21 incident #2 (found while investigating #1): "is this symbol
held" was answered ONLY from the static PORTFOLIO tracker (personal
holdings declared in stock_bot/.env, e.g. CM.TO:4:41.15) — the bot's own
real IBKR position (bare "CM", opened by the rule strategy) was invisible
to every "held" check (PORTFOLIO_SELL, RSI_OVERBOUGHT, RSI_OVERSOLD, and
the new EARNINGS_SOON logic), because it was never in that static list.
Fix: evaluate() now accepts the live executor's positions_snapshot() and
prefers it over the static tracker when given (same "executor takes
precedence over static tracker" rule main.py already applies elsewhere).
"""
from __future__ import annotations

from datetime import date, timedelta
from types import SimpleNamespace

from stock_bot.alerts.alert import AlertType
from stock_bot.alerts.evaluator import AlertEvaluator
from stock_bot.portfolio.tracker import PortfolioTracker


def _scan_result(symbol: str, days_to_earnings: int, price: float = 22.25) -> SimpleNamespace:
    earnings = SimpleNamespace(next_earnings_date=date.today() + timedelta(days=days_to_earnings))
    research = SimpleNamespace(earnings=earnings)
    return SimpleNamespace(
        symbol=symbol, company_name=symbol, price=price, currency="USD",
        rsi=None, trend=None, macd_note=None, research=research, verdict=None,
        source="watchlist",
    )


def test_earnings_soon_not_held_stays_medium_and_says_no_action():
    tracker = PortfolioTracker("")   # empty portfolio — nothing held
    ev = AlertEvaluator(tracker)
    alerts = ev.evaluate([_scan_result("T", days_to_earnings=1)])

    earnings_alerts = [a for a in alerts if a.alert_type == AlertType.EARNINGS_SOON]
    assert len(earnings_alerts) == 1
    a = earnings_alerts[0]
    assert a.priority == "MEDIUM"          # not HIGH -> never reaches Telegram
    assert "not held" in a.message.lower()
    assert "no action needed" in a.message.lower()


def test_earnings_soon_held_within_1_day_stays_high():
    tracker = PortfolioTracker("CM:10:117.82")
    ev = AlertEvaluator(tracker)
    alerts = ev.evaluate([_scan_result("CM", days_to_earnings=1)])

    earnings_alerts = [a for a in alerts if a.alert_type == AlertType.EARNINGS_SOON]
    assert len(earnings_alerts) == 1
    a = earnings_alerts[0]
    assert a.priority == "HIGH"            # actionable — reaches Telegram
    assert "you hold this position" in a.message.lower()


def test_earnings_soon_held_beyond_1_day_stays_medium():
    tracker = PortfolioTracker("CM:10:117.82")
    ev = AlertEvaluator(tracker)
    alerts = ev.evaluate([_scan_result("CM", days_to_earnings=3)])

    earnings_alerts = [a for a in alerts if a.alert_type == AlertType.EARNINGS_SOON]
    assert len(earnings_alerts) == 1
    assert earnings_alerts[0].priority == "MEDIUM"


def test_earnings_soon_uses_live_executor_position_not_static_tracker():
    # Static PORTFOLIO tracker knows nothing about "CM" (only CM.TO, a
    # different symbol) — but the live executor snapshot holds it. The live
    # snapshot must win: EARNINGS_SOON should treat CM as held.
    tracker = PortfolioTracker("CM.TO:4:41.15")   # unrelated static holding
    ev = AlertEvaluator(tracker)
    live = {"CM": (10.0, 117.82)}                 # real IBKR position
    alerts = ev.evaluate([_scan_result("CM", days_to_earnings=1)], held_positions=live)

    earnings_alerts = [a for a in alerts if a.alert_type == AlertType.EARNINGS_SOON]
    assert len(earnings_alerts) == 1
    a = earnings_alerts[0]
    assert a.priority == "HIGH"
    assert "you hold this position" in a.message.lower()


def test_earnings_soon_falls_back_to_static_tracker_when_no_live_snapshot():
    # No held_positions passed (e.g. no executor) — old static-tracker-only
    # behavior must still work unchanged.
    tracker = PortfolioTracker("CM.TO:4:41.15")
    ev = AlertEvaluator(tracker)
    alerts = ev.evaluate([_scan_result("CM.TO", days_to_earnings=1)])

    earnings_alerts = [a for a in alerts if a.alert_type == AlertType.EARNINGS_SOON]
    assert len(earnings_alerts) == 1
    assert earnings_alerts[0].priority == "HIGH"


if __name__ == "__main__":
    import sys
    failures = 0
    for t in [
        test_earnings_soon_not_held_stays_medium_and_says_no_action,
        test_earnings_soon_held_within_1_day_stays_high,
        test_earnings_soon_held_beyond_1_day_stays_medium,
        test_earnings_soon_uses_live_executor_position_not_static_tracker,
        test_earnings_soon_falls_back_to_static_tracker_when_no_live_snapshot,
    ]:
        try:
            t()
            print(f"PASS {t.__name__}")
        except AssertionError as e:
            failures += 1
            print(f"FAIL {t.__name__}: {e}")
    sys.exit(1 if failures else 0)
