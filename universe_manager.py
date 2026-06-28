#!/usr/bin/env python
"""
universe_manager.py — maintains the approved symbol list for the trading bot.

Runs the full validate_symbol.py pipeline on candidate symbols and writes
results to config/approved_symbols.json. The bot reads that file at startup
instead of UNIVERSE_WHITELIST in .env.

Commands:
  --candidates ADA,SOL,MATIC   validate new symbols and update the list
  --revalidate                 re-check every currently approved symbol
  --status                     print the current state of the approved list

Examples:
  python universe_manager.py --candidates ADA,SOL
  python universe_manager.py --candidates ETH --timeframe 1h
  python universe_manager.py --revalidate
  python universe_manager.py --status
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone

# ── Path to the registry file ─────────────────────────────────────────────────
_PROJECT_ROOT  = os.path.dirname(os.path.abspath(__file__))
_REGISTRY_PATH = os.path.join(_PROJECT_ROOT, "config", "approved_symbols.json")

# ── ANSI colours (same as validate_symbol.py) ─────────────────────────────────
_GR = "\033[32m"
_RD = "\033[31m"
_YL = "\033[33m"
_B  = "\033[1m"
_R  = "\033[0m"


def _ok(s):   return f"{_GR}{s}{_R}"
def _fail(s): return f"{_RD}{s}{_R}"
def _warn(s): return f"{_YL}{s}{_R}"
def _bold(s): return f"{_B}{s}{_R}"


# ── Registry I/O ──────────────────────────────────────────────────────────────

def _empty_registry() -> dict:
    return {
        "approved":     [],
        "watchlist":    [],
        "blocked":      [],
        "last_updated": None,
    }


def load_registry() -> dict:
    if not os.path.exists(_REGISTRY_PATH):
        return _empty_registry()
    with open(_REGISTRY_PATH) as f:
        return json.load(f)


def save_registry(registry: dict) -> None:
    os.makedirs(os.path.dirname(_REGISTRY_PATH), exist_ok=True)
    registry["last_updated"] = datetime.now(timezone.utc).isoformat()
    with open(_REGISTRY_PATH, "w") as f:
        json.dump(registry, f, indent=2)
    print(f"\n  Registry saved → {_REGISTRY_PATH}")


def _find_entry(registry: dict, base: str) -> tuple[str | None, int]:
    """Return (list_name, index) of where this base currency appears, or (None, -1)."""
    for lst in ("approved", "watchlist", "blocked"):
        for i, entry in enumerate(registry[lst]):
            if entry["base"] == base:
                return lst, i
    return None, -1


def _remove_from_all(registry: dict, base: str) -> None:
    for lst in ("approved", "watchlist", "blocked"):
        registry[lst] = [e for e in registry[lst] if e["base"] != base]


# ── Validation pipeline ───────────────────────────────────────────────────────

def validate_candidate(base: str, timeframe: str) -> dict:
    """
    Run the full pipeline for *base* and return a structured result dict.
    Imports validate_symbol functions directly (no subprocess).
    """
    # Import here so validate_symbol's logging.basicConfig doesn't fire at import
    from validate_symbol import check_liquidity, run_walkforward, decide_verdict

    bar = "─" * 60
    print(f"\n  {_bold(bar)}")
    print(f"  Validating {_bold(base)}  ({timeframe})")
    print(f"  {_bold(bar)}")

    liq     = check_liquidity(base)
    wf_rows = run_walkforward(base, timeframe)

    verdict, reasons = decide_verdict(liq, wf_rows)

    # Extract per-window PF safely
    pf_by_window: dict[str, float | None] = {}
    for row in wf_rows:
        pf_by_window[row["window"]] = round(row["pf"], 3)

    entry = {
        "base":          base,
        "symbol":        f"{base}/CAD",
        "verdict":       verdict,
        "validated_at":  datetime.now(timezone.utc).date().isoformat(),
        "timeframe":     timeframe,
        "pf_5000":       pf_by_window.get("5,000c"),
        "pf_3000":       pf_by_window.get("3,000c"),
        "pf_1000":       pf_by_window.get("1,000c"),
        "vol_cad":       round(liq["vol_cad"])    if liq.get("vol_cad")    else None,
        "spread_pct":    round(liq["spread_pct"] * 100, 4) if liq.get("spread_pct") else None,
        "pair_exists":   liq.get("pair_exists", False),
        "reasons":       reasons,
    }

    col = _ok if verdict == "APPROVED" else (_warn if verdict == "WATCHLIST" else _fail)
    print(f"\n  Result: {col(_bold(verdict))}")
    for r in reasons:
        print(f"    • {r}")

    return entry


# ── Registry update ───────────────────────────────────────────────────────────

def update_registry(registry: dict, entry: dict) -> str:
    """
    Place *entry* in the correct list. Remove it from all other lists first.
    Returns a short status string describing what changed.
    """
    base    = entry["base"]
    verdict = entry["verdict"]
    target  = verdict.lower()   # "approved" | "watchlist" | "blocked"

    prev_lst, _ = _find_entry(registry, base)
    _remove_from_all(registry, base)
    registry[target].append(entry)

    if prev_lst is None:
        return f"added to {target}"
    if prev_lst == target:
        return f"re-validated in {target} (no change)"
    return f"moved {prev_lst} → {target}"


# ── Status display ────────────────────────────────────────────────────────────

def print_status(registry: dict) -> None:
    bar = "═" * 62

    def pf_col(v):
        if v is None: return "  n/a"
        if v >= 1.2:  return f"{_GR}{v:5.2f}{_R}"
        if v >= 1.0:  return f"{_YL}{v:5.2f}{_R}"
        return f"{_RD}{v:5.2f}{_R}"

    def section(title, entries, col_fn):
        print(f"\n  {col_fn(_bold(title))}  ({len(entries)} symbol{'s' if len(entries)!=1 else ''})")
        if not entries:
            print(f"    (empty)")
            return
        print(f"  {'Symbol':>10}  {'PF-5k':>6}  {'PF-3k':>6}  {'PF-1k':>6}  "
              f"{'Vol(CAD)':>12}  {'Spread':>8}  {'Validated':>12}")
        print(f"  {'─'*10}  {'─'*6}  {'─'*6}  {'─'*6}  {'─'*12}  {'─'*8}  {'─'*12}")
        for e in entries:
            vol  = f"{e['vol_cad']:>11,.0f}" if e.get("vol_cad") else "         n/a"
            sp   = f"{e['spread_pct']:.4f}%" if e.get("spread_pct") else "    n/a"
            date = e.get("validated_at", "?")
            print(
                f"  {e['base']:>10}  "
                f"{pf_col(e.get('pf_5000'))}  "
                f"{pf_col(e.get('pf_3000'))}  "
                f"{pf_col(e.get('pf_1000'))}  "
                f"{vol}  {sp:>8}  {date:>12}"
            )

    ts = registry.get("last_updated", "never")
    print(f"\n  {_bold(bar)}")
    print(f"  {_bold('Approved Symbol Registry')}")
    print(f"  Last updated: {ts}")
    print(f"  {_bold(bar)}")

    section("APPROVED",  registry["approved"],  _ok)
    section("WATCHLIST", registry["watchlist"], _warn)
    section("BLOCKED",   registry["blocked"],   _fail)

    approved_syms = [e["symbol"] for e in registry["approved"]]
    print(f"\n  {_bold('Bot whitelist (approved only):')} {', '.join(approved_syms) or '(none)'}")
    print(f"  {_bold(bar)}\n")


# ── --candidates command ──────────────────────────────────────────────────────

def cmd_candidates(bases: list[str], timeframe: str) -> None:
    registry = load_registry()

    changes: list[tuple[str, str]] = []   # (base, status_string)
    for base in bases:
        try:
            entry  = validate_candidate(base, timeframe)
            status = update_registry(registry, entry)
            changes.append((base, status))
        except Exception as exc:
            print(f"\n  {_fail('ERROR')} validating {base}: {exc}")
            changes.append((base, f"error — {exc}"))

    save_registry(registry)

    print(f"\n  {_bold('── Summary ──────────────────────────────────')}")
    for base, status in changes:
        print(f"  {base:>8}  {status}")

    approved_syms = [e["symbol"] for e in registry["approved"]]
    print(f"\n  Active approved list: {', '.join(approved_syms) or '(none)'}")


# ── --revalidate command ──────────────────────────────────────────────────────

def cmd_revalidate(timeframe: str) -> None:
    registry   = load_registry()
    to_recheck = [e["base"] for e in registry["approved"]]

    if not to_recheck:
        print("\n  No approved symbols to revalidate.")
        return

    print(f"\n  Revalidating {len(to_recheck)} approved symbol(s): {', '.join(to_recheck)}")

    moved: list[str] = []
    for base in to_recheck:
        try:
            entry  = validate_candidate(base, timeframe)
            status = update_registry(registry, entry)
            if "moved" in status:
                moved.append(f"{base}: {status}")
        except Exception as exc:
            print(f"\n  {_fail('ERROR')} revalidating {base}: {exc}")

    save_registry(registry)

    if moved:
        print(f"\n  {_warn('Changes:')}")
        for m in moved:
            print(f"    • {m}")
    else:
        print(f"\n  {_ok('All approved symbols still pass — no changes.')}")

    approved_syms = [e["symbol"] for e in registry["approved"]]
    print(f"\n  Active approved list: {', '.join(approved_syms) or '(none)'}")


# ── Entry point ───────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Manage the approved symbol list for the trading bot.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument(
        "--candidates",
        metavar="SYM[,SYM...]",
        help="Comma-separated base currencies to validate (e.g. ADA,SOL,MATIC)",
    )
    group.add_argument(
        "--revalidate",
        action="store_true",
        help="Re-validate all currently approved symbols",
    )
    group.add_argument(
        "--status",
        action="store_true",
        help="Print the current registry without running any validation",
    )
    parser.add_argument(
        "--timeframe", default="4h",
        help="Backtest timeframe for walk-forward (default: 4h)",
    )

    args = parser.parse_args()

    if args.status:
        print_status(load_registry())
        return

    if args.candidates:
        bases = [b.strip().upper() for b in args.candidates.split(",") if b.strip()]
        if not bases:
            print("No symbols provided.")
            sys.exit(1)
        cmd_candidates(bases, args.timeframe)
        return

    if args.revalidate:
        cmd_revalidate(args.timeframe)


if __name__ == "__main__":
    main()
