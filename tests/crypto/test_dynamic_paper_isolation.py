"""
Source guards proving dynamic_universe_bot.py cannot become a live-trading
path and cannot touch the live BTC/CAD + SOL/CAD bot's state or halt flag.

These are text-level guards (same idiom this repo already uses for the TSX
rule-buy block, the MTF-gate alert, etc.) rather than behavioral tests,
because the property being proven IS about the source text: there must be
no code path, no env var, no config combination that flips dry_run to False
or points state at the live files. A behavioral test could only prove "it
didn't happen this run" — this proves "it cannot happen at all".
"""
import re

_SRC = open("dynamic_universe_bot.py").read()

# Strip the module docstring (which legitimately documents "never touches
# logs/HALT") so the guard below checks actual code, not documentation text.
_DOCSTRING_MATCH = re.match(r'^""".*?"""', _SRC, re.DOTALL)
_CODE_ONLY = _SRC[_DOCSTRING_MATCH.end():] if _DOCSTRING_MATCH else _SRC


def test_dry_run_is_hardcoded_true_not_from_config():
    """The dry_run kwarg passed to LiveExecutor must be the literal True,
    never a cfg.* lookup or an env-derived value — otherwise some future
    .env combination could turn this into a live-order path."""
    m = re.search(r"dry_run\s*=\s*(\S+),", _SRC)
    assert m is not None, "could not find the dry_run kwarg at all"
    assert m.group(1).strip() == "True", (
        f"dry_run is set from {m.group(1)!r}, not a hardcoded True — "
        "this could allow real orders under some config"
    )


def test_no_reference_to_halt_flag():
    """This paper system must be independent of the live bot's pause —
    pausing live trading must not accidentally pause (or be silently gated
    by) paper research, and this script's CODE (not just its docstring)
    must never engage, clear, or check logs/HALT."""
    assert "HALT" not in _CODE_ONLY


def test_no_reference_to_live_state_files():
    """Must never open/write the real live-trading state files."""
    assert "live_state_BTC" not in _CODE_ONLY
    assert "live_state_SOL" not in _CODE_ONLY
    assert "live_state_{" not in _CODE_ONLY and "live_state_%s" not in _CODE_ONLY


def test_state_paths_use_isolated_directory():
    assert "dynamic_paper_state" in _SRC


def test_api_credentials_are_empty_not_read_from_env():
    """A paper-only screener/executor needs no real API keys — passing them
    anyway would be an unnecessary way for a real secret to end up wired
    into a path that constructs a live-capable ccxt client."""
    m = re.search(r"api_key\s*=\s*\"([^\"]*)\"", _SRC)
    assert m is not None
    assert m.group(1) == ""
