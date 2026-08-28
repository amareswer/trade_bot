"""
Tests for the SL/TP watcher's INFO-level "N/M positions priced" audit log
(added 2026-08-06).

Context: a 2026-08-05 yfinance outage (fetch_candles/yf.download, "possibly
delisted" for real tickers) broke the main scan loop for a full trading
day. Whether the SL/TP watcher's separate get_live_price() path (fast_info,
a different yfinance endpoint, independent 30s thread) was also blind
during that window couldn't be confirmed after the fact — per-symbol
failures only logged at debug, which the file handler doesn't capture.

This log line is the fix: an always-visible INFO summary every watcher
tick, so a future outage leaves direct evidence instead of requiring
after-the-fact code-path inference.

Also the first behavioral test coverage for _check_open_positions_sl_tp
itself — it had none before this.
"""
from types import SimpleNamespace
from unittest.mock import MagicMock

import stock_bot.main as main_mod


def _executor(positions: dict[str, tuple[float, float]]) -> MagicMock:
    ex = MagicMock()
    ex.positions_snapshot.return_value = positions
    ex.sell.return_value = SimpleNamespace(status=main_mod.OrderStatus.FILLED)
    del ex.get_position_stop_pct   # hasattr(executor, "get_position_stop_pct") is False
    return ex


def _cfg(stop_loss_pct=0.05, take_profit_pct=0.50) -> SimpleNamespace:
    return SimpleNamespace(paper_stop_loss_pct=stop_loss_pct, paper_take_profit_pct=take_profit_pct)


def test_logs_full_pricing_success(monkeypatch, caplog):
    monkeypatch.setattr(main_mod, "get_live_price", lambda sym: {"RY": 211.0, "CM": 119.0}[sym])
    ex = _executor({"RY": (4.0, 210.8), "CM": (7.0, 118.84)})
    with caplog.at_level("INFO", logger="stock_bot.main"):
        main_mod._check_open_positions_sl_tp(ex, _cfg())
    assert "SL/TP check: 2/2 positions priced" in caplog.text


def test_logs_partial_pricing_failure(monkeypatch, caplog):
    prices = {"RY": 211.0, "CM": None}   # CM fetch failed
    monkeypatch.setattr(main_mod, "get_live_price", lambda sym: prices[sym])
    ex = _executor({"RY": (4.0, 210.8), "CM": (7.0, 118.84)})
    with caplog.at_level("INFO", logger="stock_bot.main"):
        main_mod._check_open_positions_sl_tp(ex, _cfg())
    assert "SL/TP check: 1/2 positions priced" in caplog.text


def test_logs_total_outage_as_zero_of_n(monkeypatch, caplog):
    # The scenario this whole fix exists for: every get_live_price() call
    # fails (matches the 2026-08-05 outage), but the watcher itself keeps
    # running — 0/N must be visible in the log, not silently absent.
    monkeypatch.setattr(main_mod, "get_live_price", lambda sym: None)
    ex = _executor({"RY": (4.0, 210.8), "CM": (7.0, 118.84)})
    with caplog.at_level("INFO", logger="stock_bot.main"):
        main_mod._check_open_positions_sl_tp(ex, _cfg())
    assert "SL/TP check: 0/2 positions priced" in caplog.text
    ex.sell.assert_not_called()   # no price -> no trigger decision, correctly conservative


def test_no_log_when_no_open_positions(monkeypatch, caplog):
    monkeypatch.setattr(main_mod, "get_live_price", lambda sym: 100.0)
    ex = _executor({})
    with caplog.at_level("INFO", logger="stock_bot.main"):
        main_mod._check_open_positions_sl_tp(ex, _cfg())
    assert "SL/TP check" not in caplog.text


def test_zero_share_positions_excluded_from_count(monkeypatch, caplog):
    monkeypatch.setattr(main_mod, "get_live_price", lambda sym: 211.0)
    ex = _executor({"RY": (4.0, 210.8), "GHOST": (0.0, 50.0)})
    with caplog.at_level("INFO", logger="stock_bot.main"):
        main_mod._check_open_positions_sl_tp(ex, _cfg())
    assert "SL/TP check: 1/1 positions priced" in caplog.text


def test_none_executor_is_a_noop(caplog):
    with caplog.at_level("INFO", logger="stock_bot.main"):
        main_mod._check_open_positions_sl_tp(None, _cfg())   # must not raise
    assert "SL/TP check" not in caplog.text


# ── Basic behavioral coverage (previously untested) ──────────────────────

def test_stop_loss_triggers_sell(monkeypatch):
    monkeypatch.setattr(main_mod, "get_live_price", lambda sym: 200.0)   # -5.1% from 210.8
    ex = _executor({"RY": (4.0, 210.8)})
    main_mod._check_open_positions_sl_tp(ex, _cfg(stop_loss_pct=0.05))
    ex.sell.assert_called_once()
    assert ex.sell.call_args.kwargs["reason"] == "STOP_LOSS_HIT"


def test_take_profit_triggers_sell(monkeypatch):
    monkeypatch.setattr(main_mod, "get_live_price", lambda sym: 350.0)   # +66% from 210.8
    ex = _executor({"RY": (4.0, 210.8)})
    main_mod._check_open_positions_sl_tp(ex, _cfg(take_profit_pct=0.50))
    ex.sell.assert_called_once()
    assert ex.sell.call_args.kwargs["reason"] == "TAKE_PROFIT_HIT"


def test_no_trigger_within_bounds_does_not_sell(monkeypatch):
    monkeypatch.setattr(main_mod, "get_live_price", lambda sym: 211.0)   # +0.1%, well within bounds
    ex = _executor({"RY": (4.0, 210.8)})
    main_mod._check_open_positions_sl_tp(ex, _cfg())
    ex.sell.assert_not_called()


# ── rejected SL/TP exit — previously SILENT (2026-08-27) ─────────────────────

def test_rejected_sl_tp_exit_logs_error_and_feeds_stuck_detector(monkeypatch, caplog):
    from bot.alerts.stuck_loop import StuckLoopDetector
    monkeypatch.setattr(main_mod, "get_live_price", lambda sym: 200.0)   # stop hit
    ex = _executor({"RY": (4.0, 210.8)})
    ex.sell.return_value = SimpleNamespace(
        status=main_mod.OrderStatus.REJECTED, reject_reason="Insufficient position",
    )
    alerts = []
    det = StuckLoopDetector(alerts.append, threshold=2)
    with caplog.at_level("ERROR"):
        for _ in range(2):
            main_mod._check_open_positions_sl_tp(ex, _cfg(stop_loss_pct=0.05), None, det)
    assert "SL/TP EXIT REJECTED RY" in caplog.text       # was silent before
    assert len(alerts) == 1 and "sl_tp_exit:RY" in alerts[0]


def test_filled_sl_tp_exit_resets_the_stuck_streak(monkeypatch):
    from bot.alerts.stuck_loop import StuckLoopDetector
    monkeypatch.setattr(main_mod, "get_live_price", lambda sym: 200.0)
    ex = _executor({"RY": (4.0, 210.8)})
    det = StuckLoopDetector(lambda _m: None, threshold=2)
    ex.sell.return_value = SimpleNamespace(
        status=main_mod.OrderStatus.REJECTED, reject_reason="x")
    main_mod._check_open_positions_sl_tp(ex, _cfg(stop_loss_pct=0.05), None, det)
    ex.sell.return_value = SimpleNamespace(status=main_mod.OrderStatus.FILLED)
    main_mod._check_open_positions_sl_tp(ex, _cfg(stop_loss_pct=0.05), None, det)
    assert det.snapshot() == {}                          # success cleared it


def test_stuck_detector_wired_into_run():
    import inspect
    src = inspect.getsource(main_mod.run)
    assert "StuckLoopDetector(" in src
    assert "stuck_detector.record(" in src               # scan-loop buy/sell
    assert "_check_open_positions_sl_tp(executor, cfg, notifier, stuck_detector)" in src
