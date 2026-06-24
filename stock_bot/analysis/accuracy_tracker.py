"""
Confidence band accuracy tracker for the stock bot AI signal system.

Reads paper_trades.csv, pairs BUY→SELL round trips, and reports
whether the AI confidence score actually predicts profitable outcomes.

The AI confidence score IS the primary strategy signal — indicators are
context fed to the AI, not signal generators. Validating accuracy by
confidence band measures whether the AI has genuine edge.

Gate for live trading: 80+ confidence band win% >= 55%, trades >= 10.
"""
from __future__ import annotations

import csv
import os
from datetime import datetime

_TRADES_CSV = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "paper_trades.csv",
)

_BAND_NAMES = {
    "HIGH": "90–100",
    "MED":  "80–89",
    "LOW":  "70–79",
    "PRE":  "<70 / no conf",
}

_COLS = [
    "timestamp", "symbol", "side", "shares",
    "price", "total_value", "cash_remaining", "reason", "confidence",
]


def _confidence_band(confidence: int) -> str:
    if confidence >= 90:
        return "HIGH"
    if confidence >= 80:
        return "MED"
    if confidence >= 70:
        return "LOW"
    return "PRE"


class ConfidenceBandTracker:
    """Tracks and reports AI signal accuracy per confidence band."""

    def load_trades(self, csv_path: str | None = None) -> list[dict]:
        """
        Read paper_trades.csv. Returns list of trade dicts.
        Handles missing confidence column (older trades without it get confidence=0).
        """
        path = csv_path or _TRADES_CSV
        trades: list[dict] = []
        if not os.path.exists(path):
            return trades

        with open(path, "r", encoding="utf-8", newline="") as f:
            reader = csv.reader(f)
            for row in reader:
                if not row:
                    continue
                # Skip header row if present
                if row[0].strip().lower() == "timestamp":
                    continue
                # Skip rows that don't look like timestamps
                try:
                    datetime.strptime(row[0].strip()[:19], "%Y-%m-%d %H:%M:%S")
                except (ValueError, IndexError):
                    continue

                row_dict: dict = {}
                for i, col in enumerate(_COLS):
                    row_dict[col] = row[i].strip() if i < len(row) else ""

                try:
                    row_dict["shares"]     = float(row_dict["shares"])     if row_dict["shares"]     else 0.0
                    row_dict["price"]      = float(row_dict["price"])      if row_dict["price"]      else 0.0
                    row_dict["confidence"] = int(float(row_dict["confidence"])) if row_dict["confidence"] else 0
                except (ValueError, TypeError):
                    row_dict["shares"]     = 0.0
                    row_dict["price"]      = 0.0
                    row_dict["confidence"] = 0

                trades.append(row_dict)

        return trades

    def pair_trades(self, trades: list[dict]) -> list[dict]:
        """
        Pair each BUY with its subsequent SELL for the same symbol (FIFO).
        Returns list of completed round-trips. Unpaired BUYs (open positions) are skipped.

        Each returned dict has:
          symbol, entry_date, exit_date, entry_price, exit_price,
          shares, pnl, pnl_pct, confidence, exit_reason, hold_days
        """
        open_buys: dict[str, list[dict]] = {}
        pairs: list[dict] = []

        for t in trades:
            sym  = t.get("symbol", "").upper()
            side = t.get("side", "").upper()
            if not sym or not side:
                continue

            if side == "BUY":
                open_buys.setdefault(sym, []).append(t)
            elif side == "SELL":
                queue = open_buys.get(sym, [])
                if not queue:
                    continue  # orphan SELL — no matching BUY
                buy = queue.pop(0)

                entry_price = buy["price"]
                exit_price  = t["price"]
                shares      = buy["shares"]
                pnl         = round((exit_price - entry_price) * shares, 2)
                pnl_pct     = (
                    round((exit_price - entry_price) / entry_price * 100, 2)
                    if entry_price > 0 else 0.0
                )

                try:
                    entry_dt  = datetime.strptime(buy["timestamp"][:19], "%Y-%m-%d %H:%M:%S")
                    exit_dt   = datetime.strptime(t["timestamp"][:19],   "%Y-%m-%d %H:%M:%S")
                    hold_days = max(0, (exit_dt - entry_dt).days)
                except (ValueError, KeyError):
                    hold_days = 0

                pairs.append({
                    "symbol":      sym,
                    "entry_date":  buy["timestamp"][:10],
                    "exit_date":   t["timestamp"][:10],
                    "entry_price": entry_price,
                    "exit_price":  exit_price,
                    "shares":      shares,
                    "pnl":         pnl,
                    "pnl_pct":     pnl_pct,
                    "confidence":  buy["confidence"],
                    "exit_reason": t.get("reason", ""),
                    "hold_days":   hold_days,
                })

        return pairs

    def band_report(self, pairs: list[dict]) -> str:
        """
        Group completed trades by confidence band and print a summary table.
        Returns the formatted string.

        Verdict logic per band (requires trades >= 5):
          EDGE  — win% >= 55%
          WEAK  — win% >= 45%
          NOISE — win% <  45% or trades < 5 (need more data)
        """
        bands: dict[str, list[dict]] = {"HIGH": [], "MED": [], "LOW": [], "PRE": []}
        for p in pairs:
            bands[_confidence_band(p["confidence"])].append(p)

        sep   = "─" * 76
        lines = [
            "╔══════════════════════════════════════════════════════════════════════════╗",
            "║  CONFIDENCE BAND ACCURACY REPORT                                        ║",
            "╚══════════════════════════════════════════════════════════════════════════╝",
            "",
            f"  {'Band':<8} {'Range':<16} {'Trades':>6} {'Win%':>6} {'Avg PnL%':>9} {'Avg Hold':>9}  Verdict",
            f"  {sep}",
        ]

        all_pairs: list[dict] = []
        for band_key in ("HIGH", "MED", "LOW", "PRE"):
            ps        = bands[band_key]
            range_str = _BAND_NAMES[band_key]
            n         = len(ps)

            if n == 0:
                lines.append(f"  {band_key:<8} {range_str:<16} {n:>6} {'—':>6} {'—':>9} {'—':>9}  NO DATA")
                continue

            wins     = sum(1 for p in ps if p["pnl_pct"] > 0)
            win_pct  = wins / n * 100
            avg_pnl  = sum(p["pnl_pct"] for p in ps) / n
            avg_hold = sum(p["hold_days"] for p in ps) / n

            if band_key == "PRE":
                verdict = "PRE-TRACKER"
            elif n < 5:
                verdict = "NEED MORE DATA"
            elif win_pct >= 55:
                verdict = "EDGE"
            elif win_pct >= 45:
                verdict = "WEAK"
            else:
                verdict = "NOISE"

            lines.append(
                f"  {band_key:<8} {range_str:<16} {n:>6} {win_pct:>5.1f}% {avg_pnl:>+8.1f}% {avg_hold:>8.1f}d  {verdict}"
            )
            all_pairs.extend(ps)

        n_all = len(all_pairs)
        lines.append(f"  {sep}")
        if n_all > 0:
            wins_all     = sum(1 for p in all_pairs if p["pnl_pct"] > 0)
            win_pct_all  = wins_all / n_all * 100
            avg_pnl_all  = sum(p["pnl_pct"] for p in all_pairs) / n_all
            avg_hold_all = sum(p["hold_days"] for p in all_pairs) / n_all
            lines.append(
                f"  {'AGGREGATE':<8} {'ALL':<16} {n_all:>6} {win_pct_all:>5.1f}% {avg_pnl_all:>+8.1f}% {avg_hold_all:>8.1f}d"
            )
        else:
            lines.append(f"  {'AGGREGATE':<8} {'ALL':<16} {'0':>6} {'—':>6} {'—':>9} {'—':>9}")

        lines.append("")
        return "\n".join(lines)

    def recommendation(self, pairs: list[dict]) -> str:
        """Return a one-line actionable recommendation based on accuracy data."""
        total = len(pairs)
        if total < 15:
            needed = 15 - total
            return f"INSUFFICIENT DATA: need {needed} more completed trades (have {total})"

        low_trades = [p for p in pairs if _confidence_band(p["confidence"]) == "LOW"]
        med_high   = [p for p in pairs if _confidence_band(p["confidence"]) in ("MED", "HIGH")]

        low_win = (
            sum(1 for p in low_trades if p["pnl_pct"] > 0) / len(low_trades) * 100
            if low_trades else 0.0
        )
        mh_win = (
            sum(1 for p in med_high if p["pnl_pct"] > 0) / len(med_high) * 100
            if med_high else 0.0
        )
        all_win = sum(1 for p in pairs if p["pnl_pct"] > 0) / total * 100

        if low_trades and low_win < 45 and med_high and mh_win >= 55:
            return "RAISE MIN_CONFIDENCE to 80: low-confidence trades dragging results"
        if med_high and len(med_high) >= 10 and mh_win >= 55:
            return f"AI HAS EDGE: 80+ confidence win rate {mh_win:.0f}% — consider live trading"
        if all_win < 50:
            return "NO EDGE DETECTED: AI accuracy below 50% across all bands"
        return f"TRACKING: {total} completed trades, {all_win:.0f}% win rate — continue accumulating"
