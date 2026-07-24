"""
Hermetic tests for bot/exchanges/retry.py's fetch_with_retry.

2026-07-24: two live BUY signals (2026-07-06, 2026-07-15) were lost after a
single transient Kraken network error on the order-book depth fetch cascaded
straight into a market-order fallback (one incident became an unrecorded
fill, the other a genuine non-fill). Same unretried-single-call pattern also
sat on the live price and candle fetches (bot/main.py), which logged
"price fetch failed" / "live candle fetch error" on a transient blip.
fetch_with_retry adds a short retry before any of those call sites give up.
"""
import pytest
from unittest.mock import patch

from bot.exchanges.retry import fetch_with_retry


def test_succeeds_immediately_without_retrying():
    calls = {"n": 0}

    def ok():
        calls["n"] += 1
        return "result"

    with patch("bot.exchanges.retry.time.sleep") as mock_sleep:
        result = fetch_with_retry(ok, label="TEST:ok")

    assert result == "result"
    assert calls["n"] == 1
    mock_sleep.assert_not_called()


def test_retries_on_failure_and_can_still_succeed():
    calls = {"n": 0}

    def flaky():
        calls["n"] += 1
        if calls["n"] < 2:
            raise ConnectionError("network blip")
        return "recovered"

    with patch("bot.exchanges.retry.time.sleep") as mock_sleep:
        result = fetch_with_retry(flaky, label="TEST:flaky")

    assert result == "recovered"
    assert calls["n"] == 2
    mock_sleep.assert_called_once_with(2.0)


def test_raises_last_exception_after_exhausting_attempts():
    calls = {"n": 0}

    def always_fails():
        calls["n"] += 1
        raise ConnectionError(f"fail {calls['n']}")

    with patch("bot.exchanges.retry.time.sleep"):
        with pytest.raises(ConnectionError, match="fail 3"):
            fetch_with_retry(always_fails, attempts=3, label="TEST:dead")

    assert calls["n"] == 3


def test_custom_attempts_and_delay_are_respected():
    sleep_calls = []

    def always_fails():
        raise TimeoutError("boom")

    with patch("bot.exchanges.retry.time.sleep", side_effect=lambda s: sleep_calls.append(s)):
        with pytest.raises(TimeoutError):
            fetch_with_retry(always_fails, attempts=2, delay_s=1.5, label="TEST:custom")

    assert sleep_calls == [1.5]   # only sleeps between attempts, not after the last one
