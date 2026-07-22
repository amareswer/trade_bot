"""
Paper trading report generator.

Reads paper_trades.csv and paper_state.json — no network calls, no yfinance.
Prints a complete trading session summary.
"""
from __future__ import annotations

import csv
import json
import os
from datetime import datetime

_STOCK_BOT_DIR   = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_TRADES_CSV      = os.path.join(_STOCK_BOT_DIR, "paper_trades.csv")
_STATE_JSON      = os.path.join(_STOCK_BOT_DIR, "paper_state.json")
_FAST_CSV        = os.path.join(_STOCK_BOT_DIR, "fast_trades.csv")
_FAST_STATE_JSON = os.path.join(_STOCK_BOT_DIR, "fast_validator_state.json")
_IBKR_CSV        = os.path.join(_STOCK_BOT_DIR, "ibkr_trades.csv")
_IBKR_STATE_JSON = os.path.join(_STOCK_BOT_DIR, "ibkr_state.json")

_COLS = [
    "timestamp", "symbol", "side", "shares",
    "price", "total_value", "cash_remaining", "reason", "confidence",
]


def _read_trades(csv_path: str) -> list[dict]:
    trades: list[dict] = []
    if not os.path.exists(csv_path):
        return trades
    with open(csv_path, "r", encoding="utf-8", newline="") as f:
        reader = csv.reader(f)
        for row in reader:
            if not row:
                continue
            if row[0].strip().lower() == "timestamp":
                continue
            try:
                datetime.strptime(row[0].strip()[:19], "%Y-%m-%d %H:%M:%S")
            except (ValueError, IndexError):
                continue
            d: dict = {}
            for i, col in enumerate(_COLS):
                d[col] = row[i].strip() if i < len(row) else ""
            try:
                d["shares"]     = float(d["shares"])     if d["shares"]     else 0.0
                d["price"]      = float(d["price"])      if d["price"]      else 0.0
                d["total_value"] = float(d["total_value"]) if d["total_value"] else 0.0
                d["confidence"] = int(float(d["confidence"])) if d["confidence"] else 0
            except (ValueError, TypeError):
                d["shares"]     = 0.0
                d["price"]      = 0.0
                d["total_value"] = 0.0
                d["confidence"] = 0
            trades.append(d)
    return trades


def read_position_book(
    paper_csv: str = _TRADES_CSV,
    ibkr_csv:  str = _IBKR_CSV,
) -> list[dict]:
    """
    The position book's full trade history: sim-paper fills plus IBKR-paper
    fills, merged in timestamp order. The Phase A 30-trade gate counts the
    strategy's trades regardless of which executor filled them — the sim book
    was closed flat before the 2026-07-17 switch, so pairs never straddle
    executors.
    """
    trades = _read_trades(paper_csv) + _read_trades(ibkr_csv)
    trades.sort(key=lambda t: t.get("timestamp", ""))
    return trades


def load_active_book_state() -> dict:
    """
    Position-book account state for the ACTIVE executor (STOCK_EXECUTOR env,
    set inside the bot process): sim → paper_state.json as-is; ibkr →
    synthesized from ibkr_state.json + ibkr_trades.csv (cash = live snapshot
    the executor persists in ibkr_state.json each scan cycle, falling back to
    the last fill's cash_remaining; positions = unpaired BUYs — no TWS call
    from here). Same dict shape
    either way: {cash, starting_cash, realized_pnl, positions, last_updated}.
    unified_dashboard.py has its own copy of this logic because it runs as a
    subprocess without the bot's env and must read stock_bot/.env itself.
    """
    if os.getenv("STOCK_EXECUTOR", "paper").strip().lower() != "ibkr":
        return _read_state(_STATE_JSON)
    ibkr = _read_state(_IBKR_STATE_JSON)
    if not ibkr:
        return {}
    trades = _read_trades(_IBKR_CSV)
    _, open_pos = _pair_trades(trades)
    starting = float(ibkr.get("starting_cash", 0.0) or 0.0)
    cash = float(ibkr.get("cash", 0.0) or 0.0)
    if cash <= 0:
        cash = float(trades[-1].get("cash_remaining") or 0.0) if trades else starting
    return {
        "cash":          cash,
        "starting_cash": starting,
        "realized_pnl":  float(ibkr.get("realized_pnl", 0.0) or 0.0),
        "positions":     {
            sym: {"shares": p["shares"], "avg_cost": p["avg_cost"]}
            for sym, p in open_pos.items()
        },
        "last_updated":  ibkr.get("last_updated"),
        "executor":      "ibkr",
        "account":       ibkr.get("account", ""),
    }


def _read_state(state_path: str) -> dict:
    if not os.path.exists(state_path):
        return {}
    try:
        with open(state_path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}


def _pair_trades(trades: list[dict]) -> tuple[list[dict], dict[str, dict]]:
    """
    Returns (completed_pairs, open_positions).

    open_positions: {symbol: {"entry_date": str, "shares": float, "avg_cost": float}}
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
                continue
            buy         = queue.pop(0)
            entry_price = buy["price"]
            exit_price  = t["price"]
            shares      = buy["shares"]
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
                "shares":      shares,
                "entry_price": entry_price,
                "exit_price":  exit_price,
                "pnl":         round((exit_price - entry_price) * shares, 2),
                "pnl_pct":     pnl_pct,
                "exit_reason": t.get("reason", ""),
                "hold_days":   hold_days,
            })

    # Summarize remaining open buys
    open_positions: dict[str, dict] = {}
    for sym, queue in open_buys.items():
        if not queue:
            continue
        total_shares = sum(b["shares"] for b in queue)
        if total_shares > 0:
            avg_cost = sum(b["shares"] * b["price"] for b in queue) / total_shares
            open_positions[sym] = {
                "entry_date": queue[0]["timestamp"][:10],
                "shares":     total_shares,
                "avg_cost":   avg_cost,
            }

    return pairs, open_positions


def _round_trip_commission(symbol: str, shares: float) -> float:
    """
    IBKR Pro fixed-rate commission for a full round trip (entry + exit).
    Rates come from stock_bot/.env — never hardcode thresholds in code paths.
    Slippage is NOT modelled here: the paper executor already applies
    PAPER_SLIPPAGE_BPS to every fill price, so recorded fills include it.
    """
    if symbol.upper().endswith(".TO"):
        per_share = float(os.getenv("COMMISSION_PER_SHARE_CAD", "0.01"))
        minimum   = float(os.getenv("COMMISSION_MIN_CAD",       "1.00"))
    else:
        per_share = float(os.getenv("COMMISSION_PER_SHARE_USD", "0.005"))
        minimum   = float(os.getenv("COMMISSION_MIN_USD",       "1.00"))
    return 2 * max(minimum, shares * per_share)


def _expectancy_stats(pairs: list[dict]) -> dict | None:
    """
    Net-of-commission expectancy over completed pairs.
    Returns None when there are no completed pairs.
    """
    if not pairs:
        return None
    net_pnls: list[float] = []
    net_pcts: list[float] = []
    for p in pairs:
        commission = _round_trip_commission(p["symbol"], p["shares"])
        net        = p["pnl"] - commission
        pos_value  = p["entry_price"] * p["shares"]
        net_pnls.append(net)
        net_pcts.append(net / pos_value * 100 if pos_value > 0 else 0.0)

    n        = len(net_pnls)
    wins     = [x for x in net_pnls if x > 0]
    losses   = [x for x in net_pnls if x < 0]
    gross_w  = sum(wins)
    gross_l  = abs(sum(losses))

    # Trades-per-week pace from the exit-date span
    try:
        exit_dates = sorted(
            datetime.strptime(p["exit_date"], "%Y-%m-%d") for p in pairs
        )
        span_days = max(1, (exit_dates[-1] - exit_dates[0]).days)
        per_week  = n / span_days * 7
    except (ValueError, KeyError):
        per_week = None

    return {
        "n":              n,
        "expectancy_usd": sum(net_pnls) / n,
        "expectancy_pct": sum(net_pcts) / n,
        "net_pf":         (gross_w / gross_l) if gross_l > 0
                          else (float("inf") if gross_w > 0 else None),
        "net_win_rate":   len(wins) / n * 100,
        "trades_per_week": per_week,
    }


def generate_report(
    csv_path:        str = _TRADES_CSV,
    state_path:      str = _STATE_JSON,
    fast_csv_path:   str = _FAST_CSV,
    ibkr_csv_path:   str = _IBKR_CSV,
    ibkr_state_path: str = _IBKR_STATE_JSON,
) -> str:
    """
    Build and return the full paper trading report as a string.
    No network calls. No yfinance. Pure file reads.
    """
    trades = read_position_book(csv_path, ibkr_csv_path)
    state  = _read_state(state_path)
    pairs, open_pos = _pair_trades(trades)

    # ── Account info ──────────────────────────────────────────────────────────
    # When the IBKR executor is active its state file exists: the live account
    # is IBKR (starting cash from state; current cash from the live snapshot
    # the executor persists in ibkr_state.json each scan cycle, falling back
    # to the last fill's cash_remaining — the report makes no network calls so
    # it cannot ask TWS itself).
    # Realized P&L stays the combined book: sim realized + IBKR realized.
    ibkr_state   = _read_state(ibkr_state_path)
    ibkr_trades  = _read_trades(ibkr_csv_path)
    current_cash   = state.get("cash", None)
    starting_cash  = state.get("starting_cash", None)
    realized_pnl   = state.get("realized_pnl", None)
    account_label  = ""
    if ibkr_state:
        starting_cash = ibkr_state.get("starting_cash", starting_cash)
        current_cash = float(ibkr_state.get("cash", 0.0) or 0.0)
        if current_cash <= 0:
            if ibkr_trades:
                current_cash = float(ibkr_trades[-1].get("cash_remaining") or 0.0)
            else:
                current_cash = starting_cash
        realized_pnl = (
            float(state.get("realized_pnl", 0.0) or 0.0)
            + float(ibkr_state.get("realized_pnl", 0.0) or 0.0)
        )
        account_label = f"  [IBKR paper {ibkr_state.get('account', '')}]"

    # Fall back: infer starting_cash from first BUY if state file is missing
    if starting_cash is None and trades:
        first_buy = next((t for t in trades if t["side"].upper() == "BUY"), None)
        if first_buy:
            starting_cash = first_buy["total_value"] + float(first_buy.get("cash_remaining", 0) or 0)

    # Count buys/sells
    n_buys  = sum(1 for t in trades if t["side"].upper() == "BUY")
    n_sells = sum(1 for t in trades if t["side"].upper() == "SELL")
    n_total = n_buys + n_sells

    # Realized P&L from state; fall back to summing pairs
    if realized_pnl is None:
        realized_pnl = sum(p["pnl"] for p in pairs)

    realized_pct = (
        realized_pnl / starting_cash * 100
        if starting_cash and starting_cash > 0 else 0.0
    )

    now_str = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")
    width   = 54
    sep     = "─" * width

    lines = [
        "═" * width,
        "  STOCK BOT — PAPER TRADING REPORT",
        f"  Generated: {now_str}",
        "═" * width,
        "",
        "  POSITION BOOK  (daily candles · multi-day holds · sized in $)" + account_label,
        f"  {sep}",
    ]

    if starting_cash is not None:
        lines.append(f"  Starting cash:    ${starting_cash:>10,.2f}")
    else:
        lines.append(f"  Starting cash:    {'N/A':>10}")

    if current_cash is not None:
        lines.append(f"  Current cash:     ${current_cash:>10,.2f}")
    else:
        lines.append(f"  Current cash:     {'N/A':>10}")

    open_syms = ", ".join(open_pos.keys()) if open_pos else "none"
    lines.append(f"  Open positions:   {len(open_pos)} ({open_syms})")

    pnl_sign = "+" if realized_pnl >= 0 else ""
    lines.append(
        f"  Realized P&L:     ${realized_pnl:>+10,.2f}  ({realized_pct:+.1f}%)"
    )
    lines.append(f"  Total trades:     {n_total} ({n_buys} buys, {n_sells} sells)")
    lines.append("")

    # ── Completed round-trips ─────────────────────────────────────────────────
    lines += [
        "  COMPLETED ROUND-TRIPS",
        f"  {sep}",
    ]

    if pairs:
        hdr = f"  {'Symbol':<8} {'Entry':<12} {'Exit':<12} {'Shares':>7} {'Entry$':>8} {'Exit$':>8} {'PnL%':>7}  Result"
        lines.append(hdr)
        lines.append(f"  {sep}")
        for p in pairs:
            result  = "WIN" if p["pnl_pct"] > 0 else ("EVEN" if p["pnl_pct"] == 0 else "LOSS")
            pnl_str = f"{p['pnl_pct']:+.1f}%"
            lines.append(
                f"  {p['symbol']:<8} {p['entry_date']:<12} {p['exit_date']:<12}"
                f" {p['shares']:>7.0f} ${p['entry_price']:>7,.2f} ${p['exit_price']:>7,.2f}"
                f" {pnl_str:>7}  {result}"
            )
    else:
        lines.append("  No completed round-trips yet.")

    lines.append("")

    # ── Open positions ────────────────────────────────────────────────────────
    lines += [
        "  OPEN POSITIONS",
        f"  {sep}",
    ]
    if open_pos:
        lines.append(f"  {'Symbol':<8} {'Entry':<12} {'Shares':>7} {'Avg Cost':>10}  (no live price — file only)")
        lines.append(f"  {sep}")
        for sym, pos in open_pos.items():
            lines.append(
                f"  {sym:<8} {pos['entry_date']:<12} {pos['shares']:>7.0f} ${pos['avg_cost']:>9,.2f}"
            )
    else:
        lines.append("  No open positions.")

    lines.append("")

    # ── Summary stats ─────────────────────────────────────────────────────────
    lines += [
        "  SUMMARY STATS",
        f"  {sep}",
    ]
    n_complete = len(pairs)
    win_rate   = 0.0
    pf         = 0.0
    if n_complete > 0:
        wins      = [p for p in pairs if p["pnl_pct"] > 0]
        losses    = [p for p in pairs if p["pnl_pct"] < 0]
        win_rate  = len(wins) / n_complete * 100
        gross_win = sum(p["pnl"] for p in wins)
        gross_los = abs(sum(p["pnl"] for p in losses))
        pf        = gross_win / gross_los if gross_los > 0 else float("inf")
        best      = max(pairs, key=lambda p: p["pnl_pct"])
        worst     = min(pairs, key=lambda p: p["pnl_pct"])
        avg_hold  = sum(p["hold_days"] for p in pairs) / n_complete

        lines.append(f"  Completed trades:  {n_complete}")
        lines.append(f"  Win rate:          {win_rate:.1f}%")
        lines.append(f"  Profit factor:     {pf:.2f}" if pf != float("inf") else "  Profit factor:     ∞ (no losses)")
        lines.append(f"  Best trade:        {best['pnl_pct']:+.1f}% ({best['symbol']})")
        lines.append(f"  Worst trade:       {worst['pnl_pct']:+.1f}% ({worst['symbol']})")
        lines.append(f"  Avg hold (days):   {avg_hold:.1f}")
    else:
        lines.append(f"  Completed trades:  0")
        lines.append(f"  Win rate:          —")
        lines.append(f"  Profit factor:     —")

    lines.append("")

    # ── Expectancy (net of costs) ─────────────────────────────────────────────
    # THE number that converts this paper book into an income projection:
    # expectancy $/trade × trades/week = weekly income at current sizing.
    lines += [
        "  EXPECTANCY — NET OF COMMISSIONS",
        f"  {sep}",
    ]
    exp = _expectancy_stats(pairs)
    if exp:
        lines.append(f"  Per-trade net $:   ${exp['expectancy_usd']:>+8,.2f}  (IBKR Pro fixed rates; slippage already in fills)")
        lines.append(f"  Per-trade net %:   {exp['expectancy_pct']:>+8.2f}%  of position value")
        if exp["net_pf"] is None:
            lines.append("  Net profit factor: —")
        elif exp["net_pf"] == float("inf"):
            lines.append("  Net profit factor: ∞ (no net losses)")
        else:
            lines.append(f"  Net profit factor: {exp['net_pf']:>8.2f}")
        lines.append(f"  Net win rate:      {exp['net_win_rate']:>8.1f}%")
        if exp["trades_per_week"] is not None:
            weekly = exp["expectancy_usd"] * exp["trades_per_week"]
            lines.append(f"  Pace:              {exp['trades_per_week']:>8.1f} trades/week")
            lines.append(f"  Projected income:  ${weekly:>+8,.2f}/week at current sizing")
        if exp["n"] < 30:
            lines.append(f"  ⚠ {exp['n']} trades — direction only, not statistically reliable (need 30)")
    else:
        lines.append("  No completed round-trips — expectancy unknown.")
        lines.append("  This number IS the product of the paper phase; nothing")
        lines.append("  can be projected until trades complete.")

    lines.append("")

    # ── Swing book (1h candles, 48h max hold, cash-tracked) ──────────────────
    fast_pairs, fast_open = _pair_trades(_read_trades(fast_csv_path))
    fast_state = _read_state(_FAST_STATE_JSON)
    f_cash          = fast_state.get("cash",          None)
    f_starting_cash = fast_state.get("starting_cash", None)
    f_realized_pnl  = fast_state.get("realized_pnl",  None)

    lines += [
        "  SWING BOOK  (1h candles · 48h max hold · sized in $)",
        f"  {sep}",
    ]

    if f_starting_cash:
        lines.append(f"  Starting cash:    ${f_starting_cash:>10,.2f}")
    if f_cash is not None:
        lines.append(f"  Current cash:     ${f_cash:>10,.2f}")
    if f_realized_pnl is not None and f_starting_cash:
        pct = f_realized_pnl / f_starting_cash * 100
        lines.append(f"  Realized P&L:     ${f_realized_pnl:>+10,.2f}  ({pct:+.1f}%)")

    if fast_pairs:
        f_n    = len(fast_pairs)
        f_wins = [p for p in fast_pairs if p["pnl"] > 0]
        f_loss = [p for p in fast_pairs if p["pnl"] < 0]
        f_wr   = len(f_wins) / f_n * 100
        f_gw_usd = sum(p["pnl"] for p in f_wins)
        f_gl_usd = abs(sum(p["pnl"] for p in f_loss))
        f_pf_usd = (f_gw_usd / f_gl_usd) if f_gl_usd > 0 else float("inf")
        f_gw_pct = sum(p["pnl_pct"] for p in f_wins)
        f_gl_pct = abs(sum(p["pnl_pct"] for p in f_loss))
        f_pf_pct = (f_gw_pct / f_gl_pct) if f_gl_pct > 0 else float("inf")
        f_exp_pct = sum(p["pnl_pct"] for p in fast_pairs) / f_n

        f_exp_stats = _expectancy_stats(fast_pairs)

        lines.append(f"  Completed:         {f_n}   open: {len(fast_open)}")
        lines.append(f"  Win rate:          {f_wr:.1f}%")
        lines.append(f"  Profit factor:     {'∞' if f_pf_usd == float('inf') else f'{f_pf_usd:.2f}'} (gross $) · "
                     f"{'∞' if f_pf_pct == float('inf') else f'{f_pf_pct:.2f}'} (gross %)")
        lines.append(f"  Expectancy:        {f_exp_pct:+.2f}% per trade")
        if f_exp_stats:
            lines.append(f"  Net expectancy:    ${f_exp_stats['expectancy_usd']:>+8,.2f}/trade · "
                         f"{f_exp_stats['expectancy_pct']:>+6.2f}%  (net of IBKR commissions)")
            lines.append(f"  Net PF:            {f_exp_stats['net_pf']:.2f}")
            lines.append(f"  Pace:              {f_exp_stats['trades_per_week']:.1f} trades/week")
            if f_n < 30:
                lines.append(f"  ⚠ {f_n} trades — direction only, not statistically reliable (need 30)")
    else:
        lines.append(f"  Completed: 0   open: {len(fast_open)} — no closed trades yet.")

    # ── Phase A gates ─────────────────────────────────────────────────────────
    lines.append(f"  {sep}")
    lines.append("  PHASE A GATE STATUS")
    lines.append(f"  {sep}")

    # Position book gate (30 trades, PF ≥ 1.2, WR ≥ 30%)
    pos_pf = pf if n_complete > 0 else 0.0
    pos_wr = win_rate if n_complete > 0 else 0.0
    if n_complete >= 30 and pos_pf >= 1.2 and pos_wr >= 30.0:
        pos_gate = "✓ PASS"
    elif n_complete == 0:
        pos_gate = f"NEED DATA (0 / 30 trades)"
    else:
        reasons = []
        if n_complete < 30:
            reasons.append(f"{n_complete}/30 trades")
        if pos_pf < 1.2:
            reasons.append(f"PF {pos_pf:.2f} < 1.2")
        if pos_wr < 30.0:
            reasons.append(f"WR {pos_wr:.1f}% < 30%")
        pos_gate = "TRACKING  (" + " · ".join(reasons) + ")"
    lines.append(f"  Position book:  {pos_gate}")

    # Swing book gate (30 trades, PF ≥ 1.2, WR ≥ 30%)
    if fast_pairs:
        swing_pf = f_pf_usd
        swing_wr = f_wr
        swing_n  = f_n
    else:
        swing_pf = 0.0
        swing_wr = 0.0
        swing_n  = 0

    if swing_n >= 30 and swing_pf >= 1.2 and swing_wr >= 30.0:
        sw_gate = "✓ PASS"
    elif swing_n == 0:
        sw_gate = "NEED DATA (0 / 30 trades)"
    else:
        sw_reasons = []
        if swing_n < 30:
            sw_reasons.append(f"{swing_n}/30 trades")
        if swing_pf < 1.2:
            sw_reasons.append(f"PF {swing_pf:.2f} < 1.2" if swing_pf != float("inf") else "")
        if swing_wr < 30.0:
            sw_reasons.append(f"WR {swing_wr:.1f}% < 30%")
        sw_reasons = [r for r in sw_reasons if r]
        sw_gate = "TRACKING  (" + " · ".join(sw_reasons) + ")"
    lines.append(f"  Swing book:     {sw_gate}")

    lines.append("═" * width)
    lines.append("")

    return "\n".join(lines)
