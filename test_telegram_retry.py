"""
Hermetic tests for TelegramAlerter._send()'s retry behavior (bot/alerts/telegram.py).

2026-08-17: a known-gaps audit (.memory/decisions/known-gaps.md gap #17) found the crypto
bot's api.telegram.org sends failing with DNS resolution errors 1,320 times since 2026-08-05
(including a ~13h continuous stretch), with the old single-attempt _send silently dropping
whatever alert was in flight. _send now wraps its POST in the same fetch_with_retry helper
already used for Kraken calls (bot/exchanges/retry.py) — these tests confirm a transient
failure now recovers instead of being dropped on the first blip, a persistent failure still
degrades to a warning-only no-raise (never crashes the trading loop), and a healthy send
still only calls requests.post once (no needless retries/delay on the common path).
"""
from unittest.mock import MagicMock, patch

from bot.alerts.telegram import TelegramAlerter

# conftest.py's autouse `_block_real_telegram_sends` fixture monkeypatches
# TelegramAlerter._send to a no-op for every test in the suite (2026-07-29
# safety net — see conftest.py's docstring). That's exactly right for every
# other test, but this file exists specifically to test _send's own retry
# logic, so it needs the REAL implementation. Capturing the function object
# here, at module-import time, happens before any fixture runs — the
# reference is unaffected by the per-test monkeypatch that follows.
_real_send = TelegramAlerter._send


def _ok_response():
    resp = MagicMock()
    resp.ok = True
    return resp


def test_send_succeeds_first_try_without_retrying():
    t = TelegramAlerter("123:fake", "42", enabled=True)
    with patch("requests.post", return_value=_ok_response()) as mock_post, \
         patch("bot.exchanges.retry.time.sleep") as mock_sleep:
        _real_send(t, "hello")

    mock_post.assert_called_once()
    mock_sleep.assert_not_called()


def test_send_retries_and_recovers_from_transient_dns_failure():
    calls = {"n": 0}

    def flaky_post(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] < 2:
            raise ConnectionError("Failed to resolve 'api.telegram.org'")
        return _ok_response()

    t = TelegramAlerter("123:fake", "42", enabled=True)
    with patch("requests.post", side_effect=flaky_post), \
         patch("bot.exchanges.retry.time.sleep") as mock_sleep:
        _real_send(t, "hello")   # must not raise

    assert calls["n"] == 2
    mock_sleep.assert_called_once()


def test_send_gives_up_after_max_attempts_without_raising():
    calls = {"n": 0}

    def always_fails(*args, **kwargs):
        calls["n"] += 1
        raise ConnectionError("Failed to resolve 'api.telegram.org'")

    t = TelegramAlerter("123:fake", "42", enabled=True)
    with patch("requests.post", side_effect=always_fails), \
         patch("bot.exchanges.retry.time.sleep"):
        _real_send(t, "hello")   # must not raise — fails silently per class contract

    assert calls["n"] == 3   # fetch_with_retry's default attempts


if __name__ == "__main__":
    import sys
    failures = 0
    for fn in [
        test_send_succeeds_first_try_without_retrying,
        test_send_retries_and_recovers_from_transient_dns_failure,
        test_send_gives_up_after_max_attempts_without_raising,
    ]:
        try:
            fn()
            print(f"PASS {fn.__name__}")
        except AssertionError as e:
            failures += 1
            print(f"FAIL {fn.__name__}: {e}")
    sys.exit(1 if failures else 0)
