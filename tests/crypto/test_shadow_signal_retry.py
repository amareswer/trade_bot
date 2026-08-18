"""
Retry resilience for shadow_signal.py's Kraken OHLCV fetch (added 2026-08-05).

Bug: a single transient Kraken hiccup in shadow_replay()'s fetch_ohlcv call
had no retry — it wasted the whole day's shadow audit (report came back "0
comparable candles" / "N/A" for a day that likely would have passed cleanly
otherwise; see logs/shadow_report_20260805.md). Fixed by wrapping the fetch
in the same fetch_with_retry helper already used for live candle/ticker/
depth fetches elsewhere (bot/exchanges/retry.py, already covered by
test_kraken_retry.py — these tests only prove shadow_signal.py actually
uses it, not the retry mechanism itself).
"""
from unittest.mock import MagicMock

import shadow_signal


def _fake_ohlcv_rows(n: int) -> list[list[float]]:
    base_ms = 1_700_000_000_000
    return [
        [base_ms + i * 14_400_000, 100.0, 101.0, 99.0, 100.5, 10.0]
        for i in range(n)
    ]


def test_transient_failure_recovers_via_retry(monkeypatch):
    monkeypatch.setattr("bot.exchanges.retry.time.sleep", lambda *_: None)
    exchange = MagicMock()
    exchange.fetch_ohlcv.side_effect = [
        Exception("kraken GET https://api.kraken.com/0/public/Assets"),
        _fake_ohlcv_rows(5),
    ]
    shadow_signal.shadow_replay("BTC/CAD", exchange)
    assert exchange.fetch_ohlcv.call_count == 2   # first call failed, retry succeeded


def test_persistent_failure_still_returns_empty_after_exhausting_retries(monkeypatch, capsys):
    monkeypatch.setattr("bot.exchanges.retry.time.sleep", lambda *_: None)
    exchange = MagicMock()
    exchange.fetch_ohlcv.side_effect = Exception("kraken GET .../Assets")
    result = shadow_signal.shadow_replay("BTC/CAD", exchange)
    assert result == []
    assert exchange.fetch_ohlcv.call_count > 1   # retried, not a single silent failure
    assert "fetch error" in capsys.readouterr().out


def test_first_attempt_success_does_not_retry(monkeypatch):
    exchange = MagicMock()
    exchange.fetch_ohlcv.return_value = _fake_ohlcv_rows(5)
    shadow_signal.shadow_replay("BTC/CAD", exchange)
    assert exchange.fetch_ohlcv.call_count == 1
