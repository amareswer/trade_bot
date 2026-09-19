"""
Strategy fingerprint: stable SHA-256 over behavior-defining source files.

Only files that directly determine trade decisions are hashed — entry/exit
logic and indicator calculations. Tooling (fingerprint.py itself), package
init files, and utility modules are excluded so that non-behavioral edits
don't invalidate a passing walk-forward.

Hashed file list is printed alongside the hash so scope is always auditable.
"""
import hashlib
import os


# Explicit list of behavior-defining files relative to the project root.
# Update this list whenever a new module that affects trade decisions is added.
# Do NOT include fingerprint.py, __init__.py, or other non-behavioral files.
_BEHAVIOR_FILES: tuple[str, ...] = (
    "bot/strategy/indicator_strategy.py",
    "bot/strategy/threshold_strategy.py",
    "bot/indicators/indicators.py",
)


def compute_strategy_hash(verbose: bool = False) -> str:
    """
    SHA-256 over the explicit list of behavior-defining files.

    Returns the first 16 hex characters (64 bits — ample for a dev-workflow
    guard, not a security primitive).

    If verbose=True, prints the hashed file list to stdout so the scope is
    auditable without reading source.
    """
    project_root = os.path.dirname(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    )

    h = hashlib.sha256()
    if verbose:
        print("Strategy fingerprint — hashing:")
    for rel_path in _BEHAVIOR_FILES:
        abs_path = os.path.join(project_root, rel_path)
        if not os.path.exists(abs_path):
            raise FileNotFoundError(
                f"Behavior file not found: {abs_path}\n"
                f"Update _BEHAVIOR_FILES in bot/strategy/fingerprint.py"
            )
        h.update(rel_path.encode())   # filename change also changes the hash
        with open(abs_path, "rb") as fh:
            h.update(fh.read())
        if verbose:
            print(f"  {rel_path}")

    result = h.hexdigest()[:16]
    if verbose:
        print(f"Hash: {result}")
    return result


def hashed_files() -> tuple[str, ...]:
    """Return the list of files included in the fingerprint (for display)."""
    return _BEHAVIOR_FILES


# ---------------------------------------------------------------------------
# Execution-layer fingerprint (2026-09-18 review finding, P1-9)
#
# compute_strategy_hash() above answers "is the entry/exit SIGNAL logic
# identical" — it deliberately excludes execution assumptions, sizing, and
# risk-gate config, so two runs sharing that hash can still behave
# differently in practice (different order type, different ATR-sizing
# setting, different risk thresholds). This section adds a SEPARATE,
# purely additive fingerprint for that execution/risk layer, and a combined
# "full run" identity — neither changes compute_strategy_hash()'s existing
# return value, format, or any of its callers (stamp_strategy.py,
# LiveTradingGate Gate 1, logs/validated_strategy_hash all keep working
# exactly as before this addition).
#
# Deliberately NOT attempted here (disclosed limitation, not hidden): a
# data checksum/range component (hashing the actual OHLCV data a backtest
# ran against) and a versioned-engine component (bot/backtest/engine.py's
# own revision) — both are real, additional identity dimensions the review
# calls for, but adding them usefully means deciding where/how backtest
# reports would carry and display a third+fourth hash, which touches
# reporting surfaces beyond this module's scope; tracked as follow-up.
# ---------------------------------------------------------------------------

_EXECUTION_BEHAVIOR_FILES: tuple[str, ...] = (
    "bot/execution/live_executor.py",
    "bot/portfolio/capital_pool.py",
    "bot/risk/risk_manager.py",
)


def compute_execution_hash(config_snapshot: "dict | None" = None, verbose: bool = False) -> str:
    """
    SHA-256 over execution/risk-layer behavior-defining files, plus an
    optional caller-supplied snapshot of normalized non-secret config
    values that affect trade OUTCOMES without living in the hashed files
    themselves — e.g. {"order_type": "limit", "atr_sizing_enabled": True,
    "max_slippage_pct": 0.01, "risk_max_position_pct": 0.20}. Keys are
    sorted before hashing so the result never depends on dict ordering.
    Pass nothing to hash only the files.

    Returns the first 16 hex characters, same convention as
    compute_strategy_hash().
    """
    project_root = os.path.dirname(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    )

    h = hashlib.sha256()
    if verbose:
        print("Execution fingerprint — hashing:")
    for rel_path in _EXECUTION_BEHAVIOR_FILES:
        abs_path = os.path.join(project_root, rel_path)
        if not os.path.exists(abs_path):
            raise FileNotFoundError(
                f"Behavior file not found: {abs_path}\n"
                f"Update _EXECUTION_BEHAVIOR_FILES in bot/strategy/fingerprint.py"
            )
        h.update(rel_path.encode())
        with open(abs_path, "rb") as fh:
            h.update(fh.read())
        if verbose:
            print(f"  {rel_path}")

    if config_snapshot:
        normalized = ",".join(f"{k}={config_snapshot[k]!r}" for k in sorted(config_snapshot))
        h.update(normalized.encode())
        if verbose:
            print(f"  config: {normalized}")

    result = h.hexdigest()[:16]
    if verbose:
        print(f"Execution hash: {result}")
    return result


def execution_hashed_files() -> tuple[str, ...]:
    """Return the list of files included in the execution fingerprint."""
    return _EXECUTION_BEHAVIOR_FILES


def compute_full_run_fingerprint(config_snapshot: "dict | None" = None) -> str:
    """
    Combined identity of BOTH the strategy signal logic AND the execution/
    risk layer (+ an optional config snapshot). Two runs sharing this value
    are behaviorally identical in every dimension this module currently
    fingerprints; two runs sharing only compute_strategy_hash() may still
    trade differently in practice. Does not replace either individual hash
    — report all three where space allows, per the review's own guidance
    that cosmetic report edits should not invalidate behavior unnecessarily
    while a real config/execution change should be visible.
    """
    strat_hash = compute_strategy_hash()
    exec_hash  = compute_execution_hash(config_snapshot)
    h = hashlib.sha256()
    h.update(strat_hash.encode())
    h.update(exec_hash.encode())
    return h.hexdigest()[:16]
