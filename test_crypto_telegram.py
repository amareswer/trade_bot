"""
Hermetic tests for TelegramAlerter.fill()'s reason line (bot/alerts/telegram.py).

2026-07-21 audit: every crypto fill alert included price/qty/P&L but never WHY
the trade happened (strategy signal vs stop-loss vs take-profit vs trailing
stop vs partial take-profit) — the reason was computed and logged to
trade_log at every call site in bot/main.py but silently dropped before
reaching Telegram. The stock bot's notifier.fill() already had a reason
line; this brings the crypto bot's alerter.fill() to parity.
"""
from unittest.mock import patch

from bot.alerts.telegram import TelegramAlerter


def _fill_text(t: TelegramAlerter, **kwargs) -> str:
    """Call fill() with the send thread patched to run inline, return the sent text."""
    with patch("threading.Thread") as MockThread:
        class _InlineThread:
            def __init__(self, target, args, daemon):
                self._target, self._args = target, args
            def start(self):
                self._target(*self._args)
        MockThread.side_effect = _InlineThread
        with patch.object(t, "_send") as mock_send:
            t.fill(**kwargs)
            mock_send.assert_called_once()
            return mock_send.call_args[0][0]


def test_fill_includes_reason_line_when_given():
    t = TelegramAlerter("123:fake", "42", enabled=True)
    text = _fill_text(
        t, side="SELL", symbol="BTC/CAD", quantity=0.001, price=90000.0,
        total_value=90.0, pnl=-0.5, exchange="kraken",
        reason="stop-loss hit — cutting the loss",
    )
    assert "Reason: stop-loss hit — cutting the loss" in text


def test_fill_omits_reason_line_when_absent():
    t = TelegramAlerter("123:fake", "42", enabled=True)
    text = _fill_text(
        t, side="BUY", symbol="BTC/CAD", quantity=0.001, price=90000.0,
        total_value=90.0, exchange="kraken",
    )
    assert "Reason:" not in text


if __name__ == "__main__":
    import sys
    failures = 0
    for t in [
        test_fill_includes_reason_line_when_given,
        test_fill_omits_reason_line_when_absent,
    ]:
        try:
            t()
            print(f"PASS {t.__name__}")
        except AssertionError as e:
            failures += 1
            print(f"FAIL {t.__name__}: {e}")
    sys.exit(1 if failures else 0)
