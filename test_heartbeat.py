"""
Heartbeat pings (bot/alerts/heartbeat.py) — hermetic, no network.

Covers: URL-off behavior, success/failure paths never raising, and the
healthy_fn gate (the stock bot uses it to stop TWS-check pings while the
IBKR connection is down — a pinging heartbeat during an outage would keep
the monitor quiet about a real problem).
"""
from unittest.mock import MagicMock, patch

from bot.alerts import heartbeat


class _FakeResponse:
    def __init__(self, status=200):
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def test_ping_empty_url_returns_false():
    assert heartbeat.ping("") is False


def test_ping_success():
    with patch.object(heartbeat.urllib.request, "urlopen", return_value=_FakeResponse(200)):
        assert heartbeat.ping("https://hc-ping.com/fake") is True


def test_ping_http_error_status_returns_false():
    with patch.object(heartbeat.urllib.request, "urlopen", return_value=_FakeResponse(500)):
        assert heartbeat.ping("https://hc-ping.com/fake") is False


def test_ping_network_error_never_raises():
    with patch.object(heartbeat.urllib.request, "urlopen", side_effect=OSError("no route")):
        assert heartbeat.ping("https://hc-ping.com/fake") is False


def test_beat_skips_ping_when_unhealthy():
    with patch.object(heartbeat, "ping") as mock_ping:
        assert heartbeat.beat("https://x", healthy_fn=lambda: False) is False
        mock_ping.assert_not_called()


def test_beat_pings_when_healthy():
    with patch.object(heartbeat, "ping", return_value=True) as mock_ping:
        assert heartbeat.beat("https://x", healthy_fn=lambda: True) is True
        mock_ping.assert_called_once()


def test_beat_healthy_fn_error_counts_as_unhealthy():
    def boom():
        raise RuntimeError("broken health check")

    with patch.object(heartbeat, "ping") as mock_ping:
        assert heartbeat.beat("https://x", healthy_fn=boom) is False
        mock_ping.assert_not_called()


def test_start_thread_disabled_on_empty_url():
    assert heartbeat.start_heartbeat_thread("") is None
    assert heartbeat.start_heartbeat_thread("   ") is None
