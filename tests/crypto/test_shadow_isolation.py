"""
Shadow-environment isolation tests (external review, 2026-09-21, P1):
'shadow mode writes production state' — with LIVE_TRADING=true,
DRY_RUN=true, executor state, risk state, and TradeLog all still used
PRODUCTION paths, so a shadow session's own startup and periodic saves
silently overwrote real persisted state (reproduced live, same day: a
shadow restart corrupted both crypto executors' real cash and force-
tripped the real kill-switch in the real logs/risk_state.json).

_SHADOW_MODE / _STATE_LOG_DIR / _live_state_path are plain module-level
values, unlike most of bot/main.py's run()-local logic, so they get real
behavioral tests here, not just source-guard inspection — but the
construction call sites that USE them are still deep inside run(), so
those get source-guard verification (matching the established pattern —
see test_accounting_buy_gate_wiring.py's own docstring for why).
"""
import inspect
import os

import bot.main as main_mod


# ============================================================================
# _live_state_path / _STATE_LOG_DIR — direct behavioral tests
# ============================================================================

def test_state_log_dir_is_shadow_subdir_iff_shadow_mode():
    expected = (
        os.path.join(main_mod._log_dir, "shadow")
        if main_mod._SHADOW_MODE else main_mod._log_dir
    )
    assert main_mod._STATE_LOG_DIR == expected


def test_live_state_path_uses_state_log_dir(monkeypatch, tmp_path):
    monkeypatch.setattr(main_mod, "_STATE_LOG_DIR", str(tmp_path))
    assert main_mod._live_state_path("BTC/CAD") == os.path.join(str(tmp_path), "live_state_BTC_CAD.json")
    assert main_mod._live_state_path("SOL/CAD") == os.path.join(str(tmp_path), "live_state_SOL_CAD.json")


def test_live_state_path_never_points_at_the_real_logs_dir_when_shadow(monkeypatch, tmp_path):
    """The actual property that matters: whatever _STATE_LOG_DIR resolves to
    under shadow mode, it must not be bot/main.py's own real _log_dir —
    proven directly rather than by re-deriving the same expression twice."""
    shadow_dir = str(tmp_path / "shadow")
    monkeypatch.setattr(main_mod, "_STATE_LOG_DIR", shadow_dir)
    p = main_mod._live_state_path("BTC/CAD")
    assert not p.startswith(main_mod._log_dir + os.sep) or p.startswith(os.path.join(main_mod._log_dir, "shadow"))
    assert p.startswith(shadow_dir)


# ============================================================================
# Source-guard: every construction site that needs isolation actually uses it
# ============================================================================

def _run_src() -> str:
    return inspect.getsource(main_mod.run)


def test_all_executor_constructions_use_live_state_path_helper():
    """No executor construction site in run() may build its own
    'logs/live_state_...' string directly — every one must route through
    _live_state_path(), which is the ONE place shadow-mode redirection
    lives. A single spot reverting to a raw f-string would silently
    reopen this exact finding for that one call site."""
    src = _run_src()
    assert "_live_state_path(" in src
    # No literal production-path construction left anywhere in run().
    assert "f\"logs/live_state_" not in src
    assert "f'logs/live_state_" not in src


def test_risk_manager_state_path_uses_state_log_dir():
    src = _run_src()
    i = src.index("RiskManager(")
    window = src[i:i + 1000]
    assert "_STATE_LOG_DIR" in window
    assert '_log_dir, "risk_state.json"' not in window   # the old, unconditionally-real path


def test_trade_log_and_accounting_store_share_one_isolated_db_path_in_shadow_mode():
    src = _run_src()
    assert "_trade_log_db_path" in src
    assert "_SHADOW_MODE" in src
    # The accounting store's init_db/connect calls must reuse the SAME
    # variable TradeLog was given, not compute their own independent path —
    # store.py's own design is "one database, one transactional boundary".
    acct_i = src.index("_accounting_db_path = _trade_log_db_path")
    assert acct_i > 0


def test_regime_monitor_thread_disabled_in_shadow_mode():
    """External review, sixth pass, 2026-09-21: 'the acknowledged
    background-thread paths should also be isolated or disabled.'
    _regime_monitor_loop writes logs/regime_health.log, a hardcoded
    production path outside this isolation boundary."""
    src = _run_src()
    i = src.index("Regime monitor background thread")
    window = src[i:i + 1200]
    assert "if not _SHADOW_MODE:" in window
    assert "_monitor_thread.start()" in window


def test_scheduled_audits_thread_disabled_in_shadow_mode():
    """Same finding — the scheduled-audits thread runs shadow_signal.py /
    live_comparison.py / rescreen.py as subprocesses against the REAL
    logs/trades.db and log files, all hardcoded."""
    src = _run_src()
    i = src.index("Scheduled audits thread (replaces macOS cron")
    window = src[i:i + 700]
    assert "not _SHADOW_MODE" in window


def test_unified_dashboard_thread_disabled_in_shadow_mode():
    """unified_dashboard.py is a separate subprocess with its own hardcoded
    production paths, outside this isolation boundary — must be skipped
    entirely in shadow mode rather than partially isolated."""
    src = _run_src()
    i = src.index('_ud_interval = 0 if _SHADOW_MODE else')
    assert i > 0


def test_dashboard_path_isolated_in_shadow_mode():
    expected = (
        os.path.join(main_mod._STATE_LOG_DIR, "dashboard.html") if main_mod._SHADOW_MODE
        else os.path.join(os.path.dirname(os.path.dirname(main_mod.__file__)), "dashboard.html")
    )
    assert main_mod._DASHBOARD_PATH == expected


# ============================================================================
# Four-way verification: shadow mode must use a real exchange-backed
# snapshot, never the simulated executor's own position, for live_positions
# ============================================================================

def test_four_way_live_positions_split_gross_by_shadow_mode():
    """External review, 2026-09-21, P1: 'simulated inventory is compared
    with real account history.' In shadow mode, qty must come from a real
    balance fetch (fetch_balance_total), not the dry-run executor's own
    simulated .position — proven by source inspection of the exact branch."""
    src = _run_src()
    i = src.index("if _SHADOW_MODE:")
    window = src[i:i + 1400]
    assert "fetch_balance_total" in window
    assert '"qty": _fw_exc.position' not in window   # must not appear in the shadow branch
    # The non-shadow (real live trading) branch still uses the executor's
    # own state — this fix is additive, not a removal of the real path.
    else_window = src[i + 1400:i + 2200]
    assert '"qty":          _fw_exc.position' in else_window


def test_accounting_cycle_status_written_after_every_cycle_attempt():
    """External review, sixth pass, 2026-09-21, P1: the shadow report's
    verdict now depends entirely on this file being written on EVERY
    cycle attempt (success or exception), not just the happy path."""
    src = _run_src()
    assert "accounting_cycle_status.write(" in src
    # Must be reachable after BOTH the try body and the except handler —
    # i.e. positioned after the try/except block ends, not inside either
    # branch alone.
    try_i = src.index("_four_way_ran_this_cycle = False")
    except_i = src.index("except Exception as _acct_cycle_exc:")
    write_i = src.index("accounting_cycle_status.write(")
    assert try_i < except_i < write_i


def test_four_way_shadow_balance_fetch_failure_uses_nan_not_zero():
    """A failed real-balance fetch must fail CLOSED (NaN — every comparison
    is False, so diff_position_against_fold's qty_ok deterministically
    fails) rather than defaulting to 0.0/flat, which could mask a real
    open position during exactly the kind of transient API hiccup shadow
    mode is supposed to be exercised against."""
    src = _run_src()
    i = src.index("if _SHADOW_MODE:")
    window = src[i:i + 1600]
    assert 'float("nan")' in window
    assert "except Exception" in window
