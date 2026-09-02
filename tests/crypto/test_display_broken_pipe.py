"""
A broken stdout pipe must never crash the bot from cosmetic console output.

Regression: 2026-09-02 a `BrokenPipeError` raised inside `display.warmup()`'s
print (parent process killed mid-warmup) propagated all the way out of
`bot.main.run()` as a FATAL CRASH + Telegram alert. Console output is purely
decorative — the file log handler is independent — so `bot/display.py` now
routes every print through a wrapper that swallows `BrokenPipeError` / `OSError`.
"""
import sys

import pytest

from bot import display


class _BrokenStdout:
    def write(self, *a):
        raise BrokenPipeError(32, "Broken pipe")

    def flush(self, *a):
        raise BrokenPipeError(32, "Broken pipe")


@pytest.fixture
def broken_stdout(monkeypatch):
    monkeypatch.setattr(sys, "stdout", _BrokenStdout())


def test_warmup_does_not_raise_on_broken_pipe(broken_stdout):
    display.warmup(1, 1, 302, 88_000.0)   # the exact call site from the incident


def test_all_hot_path_display_calls_survive_broken_pipe(broken_stdout):
    display.next_candle(88_000.0, 5, "2h 14m")
    display.fill("BUY", 0.001, "BTC/CAD", 88_000.0, 88.0, None)
    display.fill("SELL", 0.001, "BTC/CAD", 90_000.0, 90.0, 2.0)
    display.reject("insufficient cash")
    display.separator()
    display.state_line("IDLE", 0, "—")


def test_wrapper_swallows_generic_oserror(monkeypatch):
    def _boom(*a, **k):
        raise OSError("stdout gone")

    monkeypatch.setattr(display, "_builtin_print", _boom)
    display.separator()   # must not raise


def test_normal_print_still_reaches_stdout(capsys):
    display.reject("test reason")
    out = capsys.readouterr().out
    assert "REJECTED" in out and "test reason" in out
