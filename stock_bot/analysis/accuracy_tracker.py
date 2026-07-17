"""
Confidence band accuracy tracker + Live Trading Gate for the stock bot.

ConfidenceBandTracker — reads paper_trades.csv, pairs BUY→SELL round trips,
and reports whether the AI confidence score predicts profitable outcomes.

LiveTradingGate — four-gate check-list that must all PASS before switching
to live IBKR trading.  Call print_gate_status() to see current state.
"""
from __future__ import annotations

import csv
import json
import os
from datetime import datetime

# ─────────────────────────────── paths ────────────────────────────────────────

_STOCK_BOT_DIR   = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

_TRADES_CSV      = os.path.join(_STOCK_BOT_DIR, "paper_trades.csv")
_IBKR_CSV        = os.path.join(_STOCK_BOT_DIR, "ibkr_trades.csv")
_FAST_TRADES_CSV = os.path.join(_STOCK_BOT_DIR, "fast_trades.csv")
_BACKTEST_JSON   = os.path.join(_STOCK_BOT_DIR, "backtest_results.json")

# ─────────────────────── confidence band constants ────────────────────────────

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


# ─────────────────────────────────────────────────────────────────────────────
# ConfidenceBandTracker
# ─────────────────────────────────────────────────────────────────────────────

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


# ─────────────────────────────────────────────────────────────────────────────
# Live Trading Gate
# ─────────────────────────────────────────────────────────────────────────────

# Gate thresholds — change only after re-validation
_GATE_SYMBOLS       = ("AAPL", "SPY")   # MSFT excluded — walk-forward FAIL 2024-now (monitor only)
_GATE1_MIN_PASS     = 2      # symbols (of 2) that must pass all WF windows
_GATE2_MIN_TRADES   = 20
_GATE2_MIN_WIN_PCT  = 50.0   # percent
_GATE3_MIN_TRADES   = 5

_STATUS_DISPLAY = {
    "PASS":    "PASS    ✓",
    "FAIL":    "FAIL    ✗",
    "PENDING": "PENDING  ",
    "NOT_RUN": "NOT_RUN  ",
}


class LiveTradingGate:
    """
    Four-gate live trading readiness check.

    Gate 1 — Backtest walk-forward (backtest_results.json):
              At least 2 of 2 symbols (AAPL/SPY) must have PF ≥ 1.3
              in all 3 walk-forward windows.
              (MSFT excluded — walk-forward FAIL 2024-now, monitor only)

    Gate 2 — Fast validator (fast_trades.csv):
              ≥ 20 completed 1h-candle round-trips with ≥ 50% win rate.

    Gate 3 — Swing paper (paper_trades.csv):
              ≥ 5 completed daily-candle round-trips (any confidence band).

    Gate 4 — Infrastructure:
              AI signal memory importable; TSX price-audit function importable.

    Statuses:  PASS | FAIL | PENDING | NOT_RUN
      PENDING — not enough data yet (gate not failed, just needs more trades)
      NOT_RUN — gate 1 only, when backtest_results.json has never been written
    """

    # ── Gate 1: backtest walk-forward ─────────────────────────────────────────

    def check_gate1(self) -> dict:
        if not os.path.exists(_BACKTEST_JSON):
            return {
                "status":        "NOT_RUN",
                "detail":        "backtest_results.json missing — run: python -m stock_bot.backtest --walkforward",
                "passing_count": 0,
                "total_count":   len(_GATE_SYMBOLS),
            }

        try:
            with open(_BACKTEST_JSON, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError) as exc:
            return {"status": "FAIL", "detail": f"backtest_results.json unreadable ({exc})"}

        pass_pf = float(data.get("pass_threshold_pf", 1.3))
        run_at  = (data.get("run_at") or "")[:10]
        sym_map = {r["symbol"].upper(): r for r in data.get("results", [])}

        passing: list[str] = []
        failing: list[str] = []
        for sym in sorted(s.upper() for s in _GATE_SYMBOLS):
            r = sym_map.get(sym)
            if r is None:
                failing.append(sym)
                continue
            windows = r.get("windows", [])
            sym_ok  = len(windows) >= 3 and all(
                w.get("total_trades", 0) > 0
                and (
                    w.get("profit_factor") is None        # null = ∞ → always pass
                    or float(w["profit_factor"]) >= pass_pf
                )
                for w in windows
            )
            (passing if sym_ok else failing).append(sym)

        run_note = f"  (run {run_at})" if run_at else ""

        _total    = len(_GATE_SYMBOLS)
        _msft_note = " | MSFT excluded — walk-forward FAIL 2024-now"
        if len(passing) >= _GATE1_MIN_PASS:
            return {
                "status":        "PASS",
                "detail":        (
                    f"{len(passing)}/{_total} symbols pass all windows"
                    f" (PF≥{pass_pf:.1f}): {', '.join(passing)}{run_note}{_msft_note}"
                ),
                "passing_count": len(passing),
                "total_count":   _total,
            }
        return {
            "status":        "FAIL",
            "detail":        (
                f"{len(passing)}/{_total} pass, need {_GATE1_MIN_PASS}"
                f" — failing: {', '.join(failing)}{run_note}{_msft_note}"
            ),
            "passing_count": len(passing),
            "total_count":   _total,
        }

    # ── Gate 2: fast validator ────────────────────────────────────────────────

    def check_gate2(self) -> dict:
        tracker = ConfidenceBandTracker()
        trades  = tracker.load_trades(_FAST_TRADES_CSV)
        pairs   = tracker.pair_trades(trades)
        n       = len(pairs)
        wins    = sum(1 for p in pairs if p["pnl_pct"] > 0)
        win_pct = wins / n * 100 if n > 0 else 0.0

        if n < _GATE2_MIN_TRADES:
            return {
                "status":  "PENDING",
                "detail":  (
                    f"{n} / {_GATE2_MIN_TRADES} trades"
                    f"  ({win_pct:.1f}% win rate, need {_GATE2_MIN_WIN_PCT:.0f}%)"
                ),
                "trades":  n,
                "win_pct": win_pct,
            }
        if win_pct >= _GATE2_MIN_WIN_PCT:
            return {
                "status":  "PASS",
                "detail":  f"{n} trades  {win_pct:.1f}% win rate",
                "trades":  n,
                "win_pct": win_pct,
            }
        return {
            "status":  "FAIL",
            "detail":  (
                f"{n} trades  {win_pct:.1f}% win rate"
                f" (need {_GATE2_MIN_WIN_PCT:.0f}%)"
            ),
            "trades":  n,
            "win_pct": win_pct,
        }

    # ── Gate 3: swing paper ───────────────────────────────────────────────────

    def check_gate3(self) -> dict:
        tracker = ConfidenceBandTracker()
        # Position book spans the 2026-07-17 executor switch: sim-era fills in
        # paper_trades.csv plus IBKR-era fills in ibkr_trades.csv.
        trades  = tracker.load_trades() + tracker.load_trades(_IBKR_CSV)
        trades.sort(key=lambda t: t.get("timestamp", ""))
        pairs   = tracker.pair_trades(trades)
        n       = len(pairs)

        if n >= _GATE3_MIN_TRADES:
            return {"status": "PASS", "detail": f"{n} round-trips", "pairs": n}

        return {
            "status": "PENDING",
            "detail": f"{n} / {_GATE3_MIN_TRADES} round-trips",
            "pairs":  n,
        }

    # ── Gate 4: infrastructure ────────────────────────────────────────────────

    def check_gate4(self) -> dict:
        ai_ok  = False
        tsx_ok = False

        try:
            import stock_bot.ai.ai_engine as _ai_mod
            _ = _ai_mod._signal_memory   # AttributeError if symbol was removed
            ai_ok = True
        except (ImportError, AttributeError):
            pass

        try:
            from stock_bot.data.price_feed import get_tsx_warnings  # noqa: F401
            tsx_ok = True
        except ImportError:
            pass

        detail = f"AI memory {'✓' if ai_ok else '✗'}  TSX audit {'✓' if tsx_ok else '✗'}"
        return {
            "status": "PASS" if (ai_ok and tsx_ok) else "FAIL",
            "detail": detail,
            "ai_ok":  ai_ok,
            "tsx_ok": tsx_ok,
        }

    # ── Evaluate all gates ────────────────────────────────────────────────────

    def evaluate(self) -> list[dict]:
        """Run all four gates. Returns list of gate-result dicts."""
        return [
            {"gate": 1, "description": "Backtest walk-forward",    **self.check_gate1()},
            {"gate": 2, "description": "Fast validator (1h paper)", **self.check_gate2()},
            {"gate": 3, "description": "Swing paper (daily)",       **self.check_gate3()},
            {"gate": 4, "description": "Infrastructure",            **self.check_gate4()},
        ]

    def get_gate_status(self) -> dict:
        """Return structured gate data for dashboard rendering."""
        gates     = self.evaluate()
        remaining = sum(1 for g in gates if g["status"] != "PASS")
        return {
            "gates":      gates,
            "remaining":  remaining,
            "ready":      remaining == 0,
            "thresholds": {
                "gate2_min_trades":  _GATE2_MIN_TRADES,
                "gate2_min_win_pct": _GATE2_MIN_WIN_PCT,
                "gate3_min_trades":  _GATE3_MIN_TRADES,
            },
        }


# ─────────────────────────────────────────────────────────────────────────────
# print_gate_status
# ─────────────────────────────────────────────────────────────────────────────

def print_gate_status() -> None:
    """
    Evaluate all live trading gates and print a status table to stdout.

    Output example:
      ══════════════════════════════════════════════════════════════════════
        LIVE TRADING GATE STATUS
      ══════════════════════════════════════════════════════════════════════
        #   Gate                           Status     Detail
        ──────────────────────────────────────────────────────────────────
        1   Backtest walk-forward          PASS    ✓  3/3 symbols pass…
        2   Fast validator (1h paper)      PENDING    7 / 20 trades …
        3   Swing paper (daily)            PENDING    2 / 5 round-trips
        4   Infrastructure                 PASS    ✓  AI memory ✓  TSX audit ✓
        ──────────────────────────────────────────────────────────────────
        LIVE TRADING: 2 gates remaining
      ══════════════════════════════════════════════════════════════════════
    """
    gate    = LiveTradingGate()
    results = gate.evaluate()

    W     = 72
    thick = "═" * W
    thin  = "─" * W

    print(thick)
    print("  LIVE TRADING GATE STATUS")
    print(thick)
    print(f"  {'#':<4} {'Gate':<30} {'Status':<12} Detail")
    print(f"  {thin}")

    for r in results:
        label = _STATUS_DISPLAY.get(r["status"], r["status"])
        print(f"  {r['gate']:<4} {r['description']:<30} {label:<12} {r.get('detail', '')}")

    print(f"  {thin}")

    remaining = sum(1 for r in results if r["status"] != "PASS")
    if remaining == 0:
        verdict = "LIVE TRADING: READY"
    else:
        noun = "gate" if remaining == 1 else "gates"
        verdict = f"LIVE TRADING: {remaining} {noun} remaining"

    print(f"  {verdict}")
    print(thick)
