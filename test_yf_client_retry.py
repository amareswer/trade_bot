"""
Hermetic tests for fetch_with_retry's generic-exception retry
(stock_bot/data/yf_client.py).

2026-07-23: non-rate-limit exceptions (e.g. "Fetch failed X:earnings:
['Earnings Date']") previously got ZERO retries — one glitch and the whole
fetch gave up immediately, even though manual retesting during the live
incident showed these same fetches succeeding within seconds moments later.
Only YFRateLimitError got the escalating-backoff retry treatment. Fix: any
exception now gets retried too, with a short fixed delay (not the long
rate-limit backoff, since the server isn't actually throttling).
"""
from unittest.mock import patch

from yfinance.exceptions import YFRateLimitError

from stock_bot.data.yf_client import fetch_with_retry


def test_generic_exception_is_retried_and_can_still_succeed():
    calls = {"n": 0}

    def flaky():
        calls["n"] += 1
        if calls["n"] < 2:
            raise KeyError("['Earnings Date']")
        return "ok"

    with patch("stock_bot.data.yf_client.time.sleep"):
        result = fetch_with_retry(flaky, label="TEST:earnings")

    assert result == "ok"
    assert calls["n"] == 2


def test_generic_exception_gives_up_after_max_attempts():
    calls = {"n": 0}

    def always_fails():
        calls["n"] += 1
        raise KeyError("['Earnings Date']")

    with patch("stock_bot.data.yf_client.time.sleep"):
        result = fetch_with_retry(always_fails, label="TEST:earnings", max_attempts=3)

    assert result is None
    assert calls["n"] == 3


def test_generic_exception_retry_uses_a_short_delay_not_the_rate_limit_backoff():
    sleep_calls = []

    def flaky():
        raise KeyError("boom")

    with patch("stock_bot.data.yf_client.time.sleep", side_effect=lambda s: sleep_calls.append(s)):
        fetch_with_retry(flaky, label="TEST:x", max_attempts=2)

    # Rate-limit delays default to [5, 15, 30] — a generic exception must NOT
    # wait that long, it should use the short fixed retry delay.
    assert sleep_calls == [2]   # _GENERIC_RETRY_DELAY_S


def test_rate_limit_error_behavior_is_unchanged():
    # Rate limits must still use the escalating backoff ladder, not the new
    # short generic-exception delay — this fix must not blur the two paths.
    calls = {"n": 0}

    def rate_limited():
        calls["n"] += 1
        raise YFRateLimitError()

    sleep_calls = []
    with patch("stock_bot.data.yf_client.time.sleep", side_effect=lambda s: sleep_calls.append(s)), \
         patch("stock_bot.data.yf_client.trip_circuit_breaker") as mock_trip:
        result = fetch_with_retry(rate_limited, label="TEST:rl", max_attempts=3, delays=[5, 15, 30])

    assert result is None
    assert calls["n"] == 3
    assert sleep_calls == [5, 15]   # unchanged escalating backoff, not the short delay
    mock_trip.assert_called_once()


if __name__ == "__main__":
    import sys
    failures = 0
    for t in [
        test_generic_exception_is_retried_and_can_still_succeed,
        test_generic_exception_gives_up_after_max_attempts,
        test_generic_exception_retry_uses_a_short_delay_not_the_rate_limit_backoff,
        test_rate_limit_error_behavior_is_unchanged,
    ]:
        try:
            t()
            print(f"PASS {t.__name__}")
        except AssertionError as e:
            failures += 1
            print(f"FAIL {t.__name__}: {e}")
    sys.exit(1 if failures else 0)
