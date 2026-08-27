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


# Same shape of incident as the Telegram one above, found 2026-08-05: adding
# the settlement/FX tax-record CSV to StockPaperExecutor/IBKRExecutor meant
# every test file's local `sandbox` fixture needed a NEW monkeypatch line
# for it — four pre-existing fixtures (test_stock_breaker.py, test_fx_sizing.py,
# test_stock_position_mark_refresh.py, test_ibkr_executor.py) predated that
# CSV and were never updated, so every suite run silently appended fake
# RY/CM.TO/KO test rows into the REAL stock_bot/paper_trades_settlement.csv
# and stock_bot/ibkr_trades_settlement.csv — exactly the "a test forgets to
# sandbox X, real files get corrupted" pattern the Telegram fixture above
# already exists to prevent for outbound messages.
#
# This is the same fix, generalized: redirect the file-path module globals
# to a session-default tmp location for EVERY test automatically, so a
# future new persisted-file addition can't repeat this by omission. A test's
# own local `sandbox` fixture (if it requests `tmp_path` itself) still wins
# for that test — monkeypatch applies fixtures in dependency order, so a
# test-specific override set up after this one simply replaces it — this is
# only the fallback for tests that don't set their own.
@pytest.fixture(autouse=True)
def _block_real_stock_bot_file_writes(tmp_path, monkeypatch):
    import stock_bot.execution.paper as _paper_mod
    import stock_bot.execution.ibkr as _ibkr_mod

    for _mod, _prefix in ((_paper_mod, "paper"), (_ibkr_mod, "ibkr")):
        for _attr in ("_STATE_JSON", "_TRADES_CSV", "_SETTLEMENT_CSV"):
            if hasattr(_mod, _attr):
                monkeypatch.setattr(_mod, _attr, str(tmp_path / f"{_prefix}_{_attr}.default"))
        if hasattr(_mod, "_RESET_FLAG"):
            monkeypatch.setattr(_mod, "_RESET_FLAG", str(tmp_path / f"{_prefix}_reset.default"))

    # stock_bot/main.py persists the daily top-movers universe here; redirect it
    # so a test exercising that path can't touch the real file (same rationale
    # as the settlement-CSV redirect above).
    try:
        import stock_bot.main as _main_mod
        monkeypatch.setattr(_main_mod, "_MOVERS_STATE_FILE",
                            str(tmp_path / "universe_movers.default.json"))
    except Exception:
        pass
