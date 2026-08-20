"""
Confidence band accuracy tracker + Live Trading Gate for the stock bot.

ConfidenceBandTracker — reads paper_trades.csv, pairs BUY→SELL round trips,
and reports whether the AI confidence score predicts profitable outcomes.

LiveTradingGate — four-gate check-list surfaced on the dashboard and in the
weekly email as a live-trading readiness indicator. Call print_gate_status()
to see current state.

DISPLAY-ONLY as of 2026-08-20 — deliberately not wired into IBKRExecutor or
IBKR_ALLOW_LIVE. Whether/how to make a PASS status actually block or gate a
live-trading switch (most likely mirroring IBKRExecutor's existing
allow_live "refuse to start" pattern in stock_bot/execution/ibkr.py) is an
open, deferred decision — see CLAUDE.md and .memory/decisions/ for the
2026-08-20 gate-repair session that fixed what these gates measure without
yet deciding whether they enforce anything.

Gate definitions corrected 2026-08-20 (see the same session's investigation
first, then this fix pass):
  Gate 1 — was validating a stale (2026-06-29), disconnected strategy config
           via the dead stock_bot/backtest.py tool. Now reads
           logs/stock_backtest_latest.json, written by the CURRENT
           walk-forward tool (stock_backtest.py / stock_bot/backtest/
           engine.py, which imports bot/strategy/indicator_strategy.py
           directly — the same module rules.py's live signal path uses, so
           this gate now checks the strategy the bot actually runs) against
           the CURRENT RULE_WHITELIST symbols, not a hardcoded AAPL/SPY pair.
  Gate 2 — was checking fast_trades.csv, the retired swing/fast book
           (FAST_ENABLED=false, frozen since 2026-07-22, structurally could
           never reach its own pass threshold again). Repurposed to AI
           confidence-band edge: reads the same active position book Gate 3
           reads, but asks whether MED/HIGH-confidence AI calls are actually
           predictive — a genuine second signal (AI-advisory-path
           calibration), independent of whether the rules-based strategy
           itself has edge, not a duplicate of Gate 3.
  Gate 3 — threshold raised from 5 round-trips (far below any real
           readiness signal) to the already-documented "Stock Phase A gate"
           / "IBKR live go-live" bar in CLAUDE.md's Roadmap: >=30 completed
           round-trips, PF>=1.2, win rate>=30%, all three required. Label
           corrected from "Swing paper (daily)" (stale — this reads the
           active Mode A/B position book, not the retired swing book).
  Gate 4 — unchanged (infrastructure importability smoke check).
"""
from __future__ import annotations

import csv
import json
import os
from datetime import datetime

# ─────────────────────────────── paths ────────────────────────────────────────

_STOCK_BOT_DIR   = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_PROJECT_ROOT    = os.path.dirname(_STOCK_BOT_DIR)

_TRADES_CSV      = os.path.join(_STOCK_BOT_DIR, "paper_trades.csv")
_IBKR_CSV        = os.path.join(_STOCK_BOT_DIR, "ibkr_trades.csv")
# Fixed path stock_backtest.py (project root) writes on every run — see that
# file's module docstring. NOT stock_bot/backtest_results.json (the old
# path) — that was written by the dead stock_bot/backtest.py tool and is no
# longer read by anything in this file.
_LATEST_BACKTEST_JSON = os.path.join(_PROJECT_ROOT, "logs", "stock_backtest_latest.json")

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
_GATE1_QUORUM        = "all"   # every current RULE_WHITELIST symbol must PASS
_GATE2_MIN_TRADES    = 10      # MED/HIGH-confidence round-trips (mirrors recommendation()'s own bar)
_GATE2_MIN_WIN_PCT   = 55.0    # percent — mirrors recommendation()'s "AI HAS EDGE" threshold
_GATE3_MIN_TRADES    = 30      # CLAUDE.md "Stock Phase A gate" / "IBKR live go-live" bar
_GATE3_MIN_PF        = 1.2
_GATE3_MIN_WIN_PCT   = 30.0    # percent

_STATUS_DISPLAY = {
    "PASS":    "PASS    ✓",
    "FAIL":    "FAIL    ✗",
    "PENDING": "PENDING  ",
    "NOT_RUN": "NOT_RUN  ",
}


def _fmt_pf(pf: float) -> str:
    return "inf" if pf == float("inf") else f"{pf:.2f}"


class LiveTradingGate:
    """
    Four-gate live trading readiness check. DISPLAY-ONLY — see module
    docstring for the deferred enforcement decision.

    Gate 1 — Backtest walk-forward (logs/stock_backtest_latest.json):
              Every symbol in the CURRENT RULE_WHITELIST must have
              verdict PASS in the latest stock_backtest.py run — the same
              tool (and same bot/strategy/indicator_strategy.py module) the
              live rules engine actually runs.

    Gate 2 — AI confidence-band edge (paper_trades.csv + ibkr_trades.csv):
              >= 10 completed MED/HIGH-confidence round-trips with a
              >= 55% win rate — is the AI's confidence score actually
              predictive, independent of the rules engine's own edge.

    Gate 3 — Position book, live (paper_trades.csv + ibkr_trades.csv):
              >= 30 completed round-trips, PF >= 1.2, win rate >= 30% —
              all three required. Matches CLAUDE.md's documented
              "Stock Phase A gate" / "IBKR live go-live" bar.

    Gate 4 — Infrastructure:
              AI signal memory importable; TSX price-audit function importable.

    Statuses:  PASS | FAIL | PENDING | NOT_RUN
      PENDING — not enough data yet (gate not failed, just needs more trades)
      NOT_RUN — gate 1 only, when logs/stock_backtest_latest.json has never
                been written (stock_backtest.py never run)
    """

    # ── Gate 1: backtest walk-forward ─────────────────────────────────────────

    def check_gate1(self) -> dict:
        if not os.path.exists(_LATEST_BACKTEST_JSON):
            return {
                "status":        "NOT_RUN",
                "detail":        "logs/stock_backtest_latest.json missing — run: .venv/bin/python stock_backtest.py",
                "passing_count": 0,
                "total_count":   0,
            }

        try:
            with open(_LATEST_BACKTEST_JSON, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError) as exc:
            return {"status": "FAIL", "detail": f"stock_backtest_latest.json unreadable ({exc})"}

        try:
            from stock_bot.config import load as _load_stock_config
            whitelist = sorted({
                s.strip().upper()
                for s in _load_stock_config().rule_whitelist_str.split(",")
                if s.strip()
            })
        except Exception as exc:
            return {"status": "FAIL", "detail": f"could not read RULE_WHITELIST: {exc}"}

        if not whitelist:
            return {"status": "FAIL", "detail": "RULE_WHITELIST is empty — nothing to validate"}

        run_at  = (data.get("run_at") or "")[:10]
        sym_map = {r["symbol"].upper(): r for r in data.get("results", [])}

        passing: list[str] = []
        failing: list[str] = []
        missing: list[str] = []
        for sym in whitelist:
            r = sym_map.get(sym)
            if r is None:
                missing.append(sym)
            elif r.get("verdict") == "PASS":
                passing.append(sym)
            else:
                failing.append(sym)

        run_note = f"  (run {run_at})" if run_at else ""
        total    = len(whitelist)

        if len(passing) == total:
            return {
                "status":        "PASS",
                "detail":        f"{len(passing)}/{total} RULE_WHITELIST symbols pass{run_note}",
                "passing_count": len(passing),
                "total_count":   total,
            }
        problems = []
        if failing:
            problems.append(f"failing: {', '.join(failing)}")
        if missing:
            problems.append(f"not in latest run: {', '.join(missing)}")
        return {
            "status":        "FAIL",
            "detail":        (
                f"{len(passing)}/{total} RULE_WHITELIST symbols pass{run_note}"
                f" — {'; '.join(problems)}"
            ),
            "passing_count": len(passing),
            "total_count":   total,
        }

    # ── Gate 2: AI confidence-band edge ───────────────────────────────────────

    def check_gate2(self) -> dict:
        """Reads the same active position book Gate 3 reads (NOT
        fast_trades.csv — that book is retired, FAST_ENABLED=false, frozen
        since 2026-07-22). Mirrors ConfidenceBandTracker.recommendation()'s
        own MED/HIGH-band threshold (structured here instead of parsing its
        return string, so this gate stays testable independent of that
        method's exact wording)."""
        tracker = ConfidenceBandTracker()
        trades  = tracker.load_trades() + tracker.load_trades(_IBKR_CSV)
        trades.sort(key=lambda t: t.get("timestamp", ""))
        pairs    = tracker.pair_trades(trades)
        med_high = [p for p in pairs if _confidence_band(p["confidence"]) in ("MED", "HIGH")]
        n        = len(med_high)
        win_pct  = (sum(1 for p in med_high if p["pnl_pct"] > 0) / n * 100) if n > 0 else 0.0

        if n < _GATE2_MIN_TRADES:
            return {
                "status":  "PENDING",
                "detail":  (
                    f"{n} / {_GATE2_MIN_TRADES} MED/HIGH-confidence round-trips"
                    f"  ({win_pct:.1f}% win rate, need {_GATE2_MIN_WIN_PCT:.0f}%)"
                ),
                "trades":  n,
                "win_pct": win_pct,
            }
        if win_pct >= _GATE2_MIN_WIN_PCT:
            return {
                "status":  "PASS",
                "detail":  f"{n} MED/HIGH-confidence round-trips  {win_pct:.1f}% win rate",
                "trades":  n,
                "win_pct": win_pct,
            }
        return {
            "status":  "FAIL",
            "detail":  (
                f"{n} MED/HIGH-confidence round-trips  {win_pct:.1f}% win rate"
                f" (need {_GATE2_MIN_WIN_PCT:.0f}%)"
            ),
            "trades":  n,
            "win_pct": win_pct,
        }

    # ── Gate 3: position book, live ───────────────────────────────────────────

    def check_gate3(self) -> dict:
        tracker = ConfidenceBandTracker()
        # Position book spans the 2026-07-17 executor switch: sim-era fills in
        # paper_trades.csv plus IBKR-era fills in ibkr_trades.csv.
        trades  = tracker.load_trades() + tracker.load_trades(_IBKR_CSV)
        trades.sort(key=lambda t: t.get("timestamp", ""))
        pairs   = tracker.pair_trades(trades)
        n       = len(pairs)

        if n < _GATE3_MIN_TRADES:
            return {
                "status": "PENDING",
                "detail": f"{n} / {_GATE3_MIN_TRADES} round-trips",
                "pairs":  n,
            }

        wins         = sum(1 for p in pairs if p["pnl_pct"] > 0)
        win_pct      = wins / n * 100
        total_wins   = sum(p["pnl"] for p in pairs if p["pnl"] > 0)
        total_losses = -sum(p["pnl"] for p in pairs if p["pnl"] <= 0)
        pf           = (
            (total_wins / total_losses) if total_losses > 0
            else (float("inf") if total_wins > 0 else 0.0)
        )
        pf_ok  = pf >= _GATE3_MIN_PF
        win_ok = win_pct >= _GATE3_MIN_WIN_PCT

        if pf_ok and win_ok:
            return {
                "status":  "PASS",
                "detail":  f"{n} round-trips  PF={_fmt_pf(pf)}  win rate={win_pct:.1f}%",
                "pairs":   n,
                "pf":      pf,
                "win_pct": win_pct,
            }
        problems = []
        if not pf_ok:
            problems.append(f"PF {_fmt_pf(pf)} < {_GATE3_MIN_PF}")
        if not win_ok:
            problems.append(f"win rate {win_pct:.1f}% < {_GATE3_MIN_WIN_PCT:.0f}%")
        return {
            "status":  "FAIL",
            "detail":  f"{n} round-trips but {', '.join(problems)}",
            "pairs":   n,
            "pf":      pf,
            "win_pct": win_pct,
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
            {"gate": 1, "description": "Backtest walk-forward (current strategy)", **self.check_gate1()},
            {"gate": 2, "description": "AI confidence-band edge",                  **self.check_gate2()},
            {"gate": 3, "description": "Position book (live)",                     **self.check_gate3()},
            {"gate": 4, "description": "Infrastructure",                           **self.check_gate4()},
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
                "gate3_min_pf":      _GATE3_MIN_PF,
                "gate3_min_win_pct": _GATE3_MIN_WIN_PCT,
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
        1   Backtest walk-forward (current strategy)  PASS    ✓  16/16 RULE_WHITELIST symbols pass…
        2   AI confidence-band edge        PENDING    6 / 10 MED/HIGH-confidence round-trips …
        3   Position book (live)           PENDING    12 / 30 round-trips
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
