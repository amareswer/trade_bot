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
_STATE_JSON = os.path.join(_ROOT, "stock_bot", "paper_state.json")


def _run_accuracy(csv_path: str) -> None:
    from stock_bot.analysis.accuracy_tracker import ConfidenceBandTracker
    tracker = ConfidenceBandTracker()
    trades  = tracker.load_trades(csv_path)
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
    parser.add_argument("--csv",    default=_TRADES_CSV, help="Path to paper_trades.csv")
    parser.add_argument("--report", action="store_true",  help="Include full paper trading report")
    args = parser.parse_args()

    if args.report:
        _run_paper_report()

    _run_accuracy(args.csv)


if __name__ == "__main__":
    main()
