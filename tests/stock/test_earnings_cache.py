"""
Hermetic tests for the earnings-fetch failure cache TTL
(stock_bot/research/earnings.py).

2026-07-23 incident: a transient yfinance fetch failure (NVDA, RY — both
fetched cleanly moments later on manual retest) was cached identically to a
successful "no earnings scheduled" result, for the same 24h TTL. Since the
earnings blackout safety feature (blocks BUY within N days of earnings)
depends entirely on next_earnings_date being populated, one transient blip
silently disabled that protection for a full day. Fix: failures get a much
shorter TTL (1h) so a retry happens soon instead of being locked in.
"""
import threading
import time as real_time
from unittest.mock import patch

import stock_bot.research.earnings as earnings_mod
from stock_bot.research.earnings import fetch_earnings, EarningsInfo


def _reset_cache():
    earnings_mod._earnings_cache.clear()


def test_failure_is_retried_after_failure_ttl_not_full_ttl(monkeypatch):
    _reset_cache()
    clock = [1_000_000.0]
    monkeypatch.setattr(earnings_mod.time, "time", lambda: clock[0])

    with patch.object(earnings_mod, "fetch_with_retry", return_value=None) as mock_fetch:
        info1 = fetch_earnings("NVDA")
        assert info1.next_earnings_date is None
        assert mock_fetch.call_count == 1

        # Well past the short failure TTL, still well under the full 24h TTL —
        # must retry, not serve the stale cached failure.
        clock[0] += earnings_mod._EARNINGS_FAILURE_TTL + 1
        fetch_earnings("NVDA")
        assert mock_fetch.call_count == 2


def test_failure_is_not_retried_before_failure_ttl_expires(monkeypatch):
    _reset_cache()
    clock = [2_000_000.0]
    monkeypatch.setattr(earnings_mod.time, "time", lambda: clock[0])

    with patch.object(earnings_mod, "fetch_with_retry", return_value=None) as mock_fetch:
        fetch_earnings("RY")
        assert mock_fetch.call_count == 1

        clock[0] += earnings_mod._EARNINGS_FAILURE_TTL - 1
        fetch_earnings("RY")
        assert mock_fetch.call_count == 1   # still cached, no re-fetch yet


def test_success_is_cached_for_the_full_ttl_not_the_short_one(monkeypatch):
    _reset_cache()
    clock = [3_000_000.0]
    monkeypatch.setattr(earnings_mod.time, "time", lambda: clock[0])

    fake_calendar = {"Earnings Date": []}
    with patch.object(
        earnings_mod, "fetch_with_retry",
        return_value=(object(), fake_calendar, None),
    ) as mock_fetch:
        fetch_earnings("PLTR")
        assert mock_fetch.call_count == 1

        # Past the short failure TTL but well under the full 24h TTL — a
        # successful result must NOT be evicted early.
        clock[0] += earnings_mod._EARNINGS_FAILURE_TTL + 1
        fetch_earnings("PLTR")
        assert mock_fetch.call_count == 1   # still cached — success uses the long TTL


def test_concurrent_fetches_are_serialized_by_the_lock():
    # 2026-07-23: earnings.py was the one of three yfinance call sites with no
    # lock, unlike price_feed.py (_yf_download_lock) and fast_validator.py
    # (_yf_lock) — both already serialize for the same reason. main.py's
    # research phase fetches up to 5 symbols concurrently
    # (ThreadPoolExecutor max_workers=5); this proves _yf_lock actually
    # prevents concurrent yf.Ticker access, not just that it exists.
    _reset_cache()
    concurrent_count = [0]
    max_concurrent = [0]
    count_lock = threading.Lock()

    class _FakeTicker:
        def __init__(self, symbol):
            with count_lock:
                concurrent_count[0] += 1
                max_concurrent[0] = max(max_concurrent[0], concurrent_count[0])
            real_time.sleep(0.05)   # hold the "network call" window open
            with count_lock:
                concurrent_count[0] -= 1

        @property
        def calendar(self):
            return {}

        @property
        def earnings_dates(self):
            return None

    with patch.object(earnings_mod.yf, "Ticker", _FakeTicker):
        threads = [
            threading.Thread(target=fetch_earnings, args=(f"SYM{i}",))
            for i in range(5)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

    assert max_concurrent[0] == 1, (
        f"expected fetches to be serialized (max 1 concurrent), "
        f"got {max_concurrent[0]} concurrent yf.Ticker constructions"
    )


if __name__ == "__main__":
    import sys
    failures = 0
    for t in [
        test_failure_is_retried_after_failure_ttl_not_full_ttl,
        test_failure_is_not_retried_before_failure_ttl_expires,
        test_success_is_cached_for_the_full_ttl_not_the_short_one,
        test_concurrent_fetches_are_serialized_by_the_lock,
    ]:
        try:
            if t is test_concurrent_fetches_are_serialized_by_the_lock:
                t()
                print(f"PASS {t.__name__}")
                continue
            # Manual monkeypatch shim for standalone execution
            class _MP:
                def setattr(self, obj, name, val): setattr(obj, name, val)
            t(_MP())
            print(f"PASS {t.__name__}")
        except AssertionError as e:
            failures += 1
            print(f"FAIL {t.__name__}: {e}")
    sys.exit(1 if failures else 0)
