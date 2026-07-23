"""
Hermetic test for the per-source research-fetch timeout
(stock_bot/research/aggregator.py).

2026-07-23: fetch_earnings now retries through fetch_with_retry (up to 3
attempts + a 2s delay between each, after that day's yf_client.py fix made
generic exceptions retry too) — a failing fetch can legitimately take close
to the old blanket 15s budget before giving up, where it used to fail almost
instantly. That turned a single underlying failure into two separate log
lines ("Research fetch timed out" on top of "Fetch failed ... after 3
attempts") for the same event. Fix: earnings gets a wider timeout (45s);
news (which doesn't use fetch_with_retry — feedparser, not yfinance) is
unaffected and stays at 15s. This test confirms the right value reaches each
source without waiting out any real timeout.
"""
from unittest.mock import patch

from stock_bot.research.aggregator import fetch_research
from stock_bot.research.earnings import EarningsInfo


def test_earnings_gets_a_wider_timeout_than_news():
    captured_timeouts = {}

    class _FakeFuture:
        def __init__(self, value):
            self._value = value

        def result(self, timeout=None):
            captured_timeouts[timeout] = captured_timeouts.get(timeout, 0) + 1
            return self._value

    class _FakeExecutor:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def submit(self, fn, *args):
            if fn.__name__ == "fetch_news":
                return _FakeFuture([])
            return _FakeFuture(EarningsInfo())

    with patch("stock_bot.research.aggregator.ThreadPoolExecutor", lambda max_workers: _FakeExecutor()), \
         patch("stock_bot.research.aggregator.fetch_fear_greed", return_value=None):
        fetch_research("TEST", fear_greed_data=object())

    assert 15 in captured_timeouts   # news — unaffected, unchanged
    assert 45 in captured_timeouts   # earnings — widened by the 2026-07-23 fix


if __name__ == "__main__":
    import sys
    try:
        test_earnings_gets_a_wider_timeout_than_news()
        print("PASS test_earnings_gets_a_wider_timeout_than_news")
        sys.exit(0)
    except AssertionError as e:
        print(f"FAIL: {e}")
        sys.exit(1)
