"""
Stock bot analysis CLI.

Usage:
  python stock_analysis.py                      # accuracy report only
  python stock_analysis.py --csv path/to.csv   # custom CSV path
  python stock_analysis.py --report             # paper report + accuracy report
"""
from __future__ import annotations

import argparse
import os
import sys

_ROOT = os.path.dirname(os.path.abspath(__file__))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

_TRADES_CSV = os.path.join(_ROOT, "stock_bot", "paper_trades.csv")
_IBKR_CSV   = os.path.join(_ROOT, "stock_bot", "ibkr_trades.csv")
_STATE_JSON = os.path.join(_ROOT, "stock_bot", "paper_state.json")


def _run_accuracy(csv_path: str | None) -> None:
    from stock_bot.analysis.accuracy_tracker import ConfidenceBandTracker
    tracker = ConfidenceBandTracker()
    if csv_path is None:
        # Default = full position book across the 2026-07-17 executor switch
        trades = tracker.load_trades(_TRADES_CSV) + tracker.load_trades(_IBKR_CSV)
        trades.sort(key=lambda t: t.get("timestamp", ""))
    else:
        trades = tracker.load_trades(csv_path)
    pairs   = tracker.pair_trades(trades)
    print(tracker.band_report(pairs))
    rec = tracker.recommendation(pairs)
    print(f"  RECOMMENDATION: {rec}")
    print()


def _run_paper_report() -> None:
    from stock_bot.analysis.paper_report import generate_report
    print(generate_report())


def main() -> None:
    parser = argparse.ArgumentParser(description="Stock bot paper trading analysis")
    parser.add_argument("--csv",    default=None, help="Path to a trades CSV (default: paper + IBKR merged)")
    parser.add_argument("--report", action="store_true",  help="Include full paper trading report")
    args = parser.parse_args()

    if args.report:
        _run_paper_report()

    _run_accuracy(args.csv)


if __name__ == "__main__":
    main()
