"""
Repo-wide pytest safety fixtures.

Root cause of the 2026-07-29 "unexplained second LiveExecutor instance"
investigation: several tests construct a real LiveExecutor (which builds a
real TelegramAlerter from the live cfg singleton in __init__) without ever
mocking Telegram. With TELEGRAM_ENABLED=true in .env, any test that walks a
code path calling alerter.error()/.fill()/etc. — e.g.
test_live_executor.py::test_sync_cash_falls_back_on_error, which
deliberately makes fetch_balance() raise to test the fallback path — fired
a genuine outbound POST to https://api.telegram.org using the real bot
token, every time the suite ran. No new process, nothing in
logs/trade_bot.log (that handler is only attached inside bot.main.run(),
which pytest never calls), so it looked exactly like a rogue instance.

This autouse, session-wide fixture patches TelegramAlerter._send — the one
method that actually calls requests.post() — to a no-op for every test in
the suite, so no future test can ever make a real outbound Telegram call
even if it forgets to mock Telegram itself. Tests that need to assert on
what WOULD have been sent are unaffected: they locally
patch.object(alerter_instance, "_send") (an instance-level patch), which
shadows this class-level default for the duration of their own `with`
block, then reverts back to this no-op afterward — see
test_crash_hardening.py and test_crypto_telegram.py for the existing
pattern this doesn't change.
"""
import pytest


def _noop_send(self, text: str) -> None:
    pass


@pytest.fixture(autouse=True)
def _block_real_telegram_sends(monkeypatch):
    from bot.alerts.telegram import TelegramAlerter
    monkeypatch.setattr(TelegramAlerter, "_send", _noop_send)
