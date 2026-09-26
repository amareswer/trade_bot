"""Secret redaction in logs (2026-09-26): the live Telegram bot token was
written to logs/trade_bot.log 30 times via requests' exception text, which
includes the request URL (https://api.telegram.org/bot<token>/getUpdates)."""
import inspect
import logging

import requests

from bot.alerts import redact as redact_mod
from bot.alerts.redact import REDACTED, RedactingFormatter, redact

FAKE_TOKEN = "1234567890:AAHfakeTokenForTestsOnly_abcdefghijk"
URL = f"https://api.telegram.org/bot{FAKE_TOKEN}/getUpdates?timeout=25"


def _http_error() -> requests.HTTPError:
    # The exact message shape requests produces (and that leaked live).
    return requests.HTTPError(f"502 Server Error: Bad Gateway for url: {URL}")


def test_redacts_telegram_token_from_request_exception_text():
    out = redact(_http_error())
    assert FAKE_TOKEN not in out
    assert f"bot{REDACTED}/getUpdates" in out


def test_redacts_any_secret_env_value(monkeypatch):
    monkeypatch.setenv("KRAKEN_API_SECRET", "s3cr3t-kraken-value-XYZ123")
    out = redact("signing failed with s3cr3t-kraken-value-XYZ123 attached")
    assert "s3cr3t-kraken-value-XYZ123" not in out
    assert REDACTED in out


def test_short_or_non_secret_env_values_are_left_alone(monkeypatch):
    monkeypatch.setenv("SOME_TOKEN", "abc")          # too short to be a real secret
    monkeypatch.setenv("SYMBOL", "BTC/CAD-longer-value")   # not a secret-named key
    assert redact("abc BTC/CAD-longer-value 12:30:45") == "abc BTC/CAD-longer-value 12:30:45"


def test_formatter_scrubs_args_and_traceback():
    fmt = RedactingFormatter("%(levelname)s %(message)s")
    try:
        raise _http_error()
    except requests.HTTPError:
        import sys
        rec = logging.LogRecord("t", logging.WARNING, __file__, 1,
                                "getUpdates failed: %s", (URL,), sys.exc_info())
    out = fmt.format(rec)
    assert FAKE_TOKEN not in out
    assert "Traceback" in out            # the traceback itself is still there, just scrubbed


def test_poller_getupdates_failure_never_logs_the_token(caplog, monkeypatch):
    """End-to-end reproduction of the live leak path."""
    from bot.alerts.telegram_control import TelegramCommandPoller

    poller = TelegramCommandPoller(bot_token=FAKE_TOKEN, chat_id="1", handlers={},
                                   error_backoff_s=0)

    def _boom(**_):
        raise _http_error()
    monkeypatch.setattr(poller, "_get_updates", lambda offset, timeout: _boom())

    with caplog.at_level(logging.WARNING):
        poller.poll_once()

    assert "getUpdates failed" in caplog.text
    assert FAKE_TOKEN not in caplog.text


def test_retry_warning_is_redacted(caplog):
    from bot.exchanges.retry import fetch_with_retry

    def _fail():
        raise _http_error()

    with caplog.at_level(logging.WARNING):
        try:
            fetch_with_retry(_fail, attempts=2, delay_s=0, label="Telegram send")
        except requests.HTTPError:
            pass
    assert "attempt 1/2" in caplog.text
    assert FAKE_TOKEN not in caplog.text


def test_both_bots_install_the_redacting_formatter():
    import bot.main as crypto_main
    import stock_bot.main as stock_main
    for mod in (crypto_main, stock_main):
        src = inspect.getsource(mod._setup_logging)
        assert "RedactingFormatter" in src
        assert "logging.Formatter(" not in src, "a plain Formatter would bypass redaction"


def test_redact_never_raises():
    class _Bad:
        def __str__(self):
            raise ValueError("no")
    assert "redaction failed" in redact(_Bad())
    assert redact_mod.redact(None) == "None"
