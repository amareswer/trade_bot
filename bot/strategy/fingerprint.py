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
