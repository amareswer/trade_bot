"""
Unified tabbed dashboard — crypto bot + stock bot + portfolio.

Usage:
    python unified_dashboard.py           # generate once and exit
    python unified_dashboard.py --watch   # regenerate every 30s (Ctrl+C to stop)

Tabs:
    Crypto    → embeds dashboard.html (written by bot/dashboard/renderer.py)
    Stocks    → embeds stock_dashboard.html (written by stock_bot/dashboard/renderer.py)
    Portfolio → inline summary from logs/live_state.json + stock_bot/paper_state.json

Tab selection is saved in localStorage — auto-refresh does not lose your spot.
"""
from __future__ import annotations

import csv
import glob
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

CRYPTO_STATE_GLOB    = "logs/live_state_*.json"   # per-symbol files (current bot)
CRYPTO_STATE_LEGACY  = "logs/live_state.json"     # pre-multi-symbol — fallback only
STOCK_STATE_PATH     = "stock_bot/paper_state.json"
IBKR_STATE_PATH      = "stock_bot/ibkr_state.json"
IBKR_TRADES_PATH     = "stock_bot/ibkr_trades.csv"
KRAKEN_HOLDINGS_PATH = "logs/kraken_holdings.json"
RISK_STATE_PATH      = "logs/risk_state.json"
HALT_FLAG_PATH       = "logs/HALT"
OUTPUT_PATH          = "unified_dashboard.html"
REFRESH_S            = 30
STALE_AFTER_H        = 48   # state older than this is flagged STALE


# ── State helpers ─────────────────────────────────────────────────────────────

def _load_json(path: str) -> dict | None:
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def _stock_executor_type() -> str:
    """STOCK_EXECUTOR from stock_bot/.env — 'paper' (sim) or 'ibkr'."""
    try:
        from dotenv import dotenv_values
        raw = dotenv_values("stock_bot/.env").get("STOCK_EXECUTOR", "") or ""
    except Exception:
        raw = ""
    return raw.strip().lower() or "paper"


def _load_stock_state() -> dict | None:
    """
    State dict for the stock card/positions table, shaped like
    paper_state.json: {cash, starting_cash, realized_pnl, positions,
    last_updated}. When STOCK_EXECUTOR=ibkr the live account is IBKR —
    synthesize the same shape from ibkr_state.json + ibkr_trades.csv
    (cash = last fill's cash_remaining; positions = unpaired BUYs), since
    this subprocess can't ask TWS. Falls back to the sim book otherwise.
    """
    if _stock_executor_type() != "ibkr":
        return _load_json(STOCK_STATE_PATH)
    ibkr = _load_json(IBKR_STATE_PATH)
    if ibkr is None:
        return None
    try:
        from stock_bot.analysis.paper_report import _pair_trades, _read_trades
        trades = _read_trades(IBKR_TRADES_PATH)
        _, open_pos = _pair_trades(trades)
    except Exception:
        trades, open_pos = [], {}
    starting = float(ibkr.get("starting_cash", 0.0) or 0.0)
    cash = (
        float(trades[-1].get("cash_remaining") or 0.0) if trades else starting
    )
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


def _whitelist() -> list[str]:
    """Active symbols from .env UNIVERSE_WHITELIST (comma-separated)."""
    try:
        from dotenv import dotenv_values
        raw = dotenv_values(".env").get("UNIVERSE_WHITELIST", "") or ""
    except Exception:
        raw = os.getenv("UNIVERSE_WHITELIST", "")
    return [s.strip() for s in raw.split(",") if s.strip()]


def _load_crypto_states() -> tuple[dict[str, dict], dict[str, dict]]:
    """Read per-symbol live state files. Returns (active, retired) keyed by
    symbol — active = in UNIVERSE_WHITELIST, retired = leftover slot files.
    Falls back to the legacy single-file path if no per-symbol files exist."""
    states: dict[str, dict] = {}
    for path in sorted(glob.glob(CRYPTO_STATE_GLOB)):
        data = _load_json(path)
        if data and data.get("symbol"):
            states[data["symbol"]] = data
    if not states:
        legacy = _load_json(CRYPTO_STATE_LEGACY)
        if legacy and legacy.get("symbol"):
            states[legacy["symbol"]] = legacy
    wl = _whitelist()
    active  = {s: d for s, d in states.items() if not wl or s in wl}
    retired = {s: d for s, d in states.items() if s not in active}
    return active, retired


def _hours_old(ts: str | None) -> float | None:
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - dt).total_seconds() / 3600
    except Exception:
        return None


def read_live_signals() -> dict:
    import csv
    try:
        with open("logs/live_signals.csv", encoding="utf-8", newline="") as f:
            last: dict = {}
            for row in csv.DictReader(f):
                sym = (row.get("symbol") or "").strip()
                if sym:
                    last[sym] = row
        return last
    except Exception:
        return {}


def _fmt_ts(ts: str | None) -> str:
    if not ts:
        return "—"
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        return dt.strftime("%Y-%m-%d %H:%M UTC")
    except Exception:
        return ts


# ── HTML micro-helpers ────────────────────────────────────────────────────────

def _pnl(v: float) -> str:
    col = "#3fb950" if v >= 0 else "#f85149"
    s   = "+" if v >= 0 else ""
    return f'<span style="color:{col};font-weight:600">{s}${v:,.2f}</span>'


def _kv(key: str, val: str) -> str:
    return (
        f'<div style="display:flex;justify-content:space-between;align-items:baseline;'
        f'border-bottom:1px solid #21262d;padding:6px 0">'
        f'<span style="font-size:12px;color:#8b949e">{key}</span>'
        f'<span style="font-size:13px;font-weight:600;color:#e6edf3;text-align:right">{val}</span>'
        f'</div>'
    )


def _stat_block(label: str, val: str, sub: str = "") -> str:
    sub_html = f'<div style="font-size:11px;color:#8b949e;margin-top:3px">{sub}</div>' if sub else ""
    return (
        f'<div style="background:#161b22;border:1px solid #30363d;border-radius:8px;padding:16px 20px">'
        f'<div style="font-size:10px;color:#8b949e;text-transform:uppercase;'
        f'letter-spacing:.06em;margin-bottom:6px">{label}</div>'
        f'<div style="font-size:24px;font-weight:700;color:#e6edf3">{val}</div>'
        f'{sub_html}'
        f'</div>'
    )


# ── Portfolio tab sections ────────────────────────────────────────────────────

def _combined_stats(active: dict[str, dict], stock: dict | None) -> str:
    crypto_cash = sum(float(d.get("cash", 0)) for d in active.values())
    crypto_pv   = sum(
        float(d.get("cost_basis", 0))
        for d in active.values() if float(d.get("position", 0)) > 0
    )
    crypto_rpnl = sum(float(d.get("realized_pnl", 0)) for d in active.values())
    crypto_fees = sum(float(d.get("fees_paid", 0)) for d in active.values())

    stock_cash  = float(stock.get("cash", 0))          if stock else 0.0
    stock_rpnl  = float(stock.get("realized_pnl", 0))  if stock else 0.0
    stock_pos   = stock.get("positions", {})            if stock else {}
    stock_pv    = sum(
        float(p.get("shares", 0)) * float(p.get("avg_cost", 0))
        for p in stock_pos.values()
    )

    swing_data  = _load_json("stock_bot/fast_validator_state.json") or {}
    swing_cash  = float(swing_data.get("cash",         0))
    swing_rpnl  = float(swing_data.get("realized_pnl", 0))
    swing_pv    = sum(
        float(p.get("shares", 0)) * float(p.get("entry_price", 0))
        for p in swing_data.get("positions", [])
    )

    total      = crypto_cash + crypto_pv + stock_cash + stock_pv + swing_cash + swing_pv
    total_rpnl = crypto_rpnl + stock_rpnl + swing_rpnl

    rpnl_col = "#3fb950" if total_rpnl >= 0 else "#f85149"
    rpnl_s   = "+" if total_rpnl >= 0 else ""

    return (
        f'<div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));'
        f'gap:12px;margin-bottom:24px">'
        + _stat_block(
            "Bot-Managed Capital", f"${total:,.2f}",
            "crypto slots + position book + swing book · positions at cost/entry"
        )
        + _stat_block(
            "Realized P&L",
            f'<span style="color:{rpnl_col}">{rpnl_s}${total_rpnl:,.2f}</span>',
            "crypto (live) + position book (paper) + swing book (paper)"
        )
        + _stat_block(
            "Crypto Fees Paid", f"${crypto_fees:.4f}",
            "active slots · Kraken maker/taker"
        )
        + "</div>"
    )


def _file_age_h(path: str) -> float | None:
    try:
        return (time.time() - os.path.getmtime(path)) / 3600
    except Exception:
        return None


def _heartbeat_blocks() -> str:
    """Green/amber/red bot-alive indicators based on log freshness."""
    def block(label: str, path: str, green_h: float, amber_h: float, idle_note: str) -> str:
        age = _file_age_h(path)
        if age is None:
            return _stat_block(label, '<span style="color:#f85149">● no log</span>', path)
        if age < green_h:
            val, sub = '<span style="color:#3fb950">● alive</span>', f"last activity {age*60:.0f} min ago"
        elif age < amber_h:
            val, sub = '<span style="color:#d29922">● quiet</span>', f"last activity {age:.1f}h ago — {idle_note}"
        else:
            val, sub = '<span style="color:#f85149">● down?</span>', f"no activity for {age:.0f}h — check the bot"
        return _stat_block(label, val, sub)

    return (
        block("Crypto Bot", "logs/trade_bot.log", 2, 8, "normal between candles")
        + block("Stock Bot", "logs/stock_bot.log", 2, 80, "normal on weekends/holidays")
    )


# Gate window opens when the live bot went back on the validated fixed-SL
# config (2026-06-22 21:24 UTC — see CLAUDE.md, ATR SL drift incident).
# Earlier fills (older configs, kraken_backfill reconstructions, the Jun 27
# external-holdings incident) are not evidence about the current strategy.
_GATE_START = "2026-06-22T21:24"


def _read_gate_stats() -> dict:
    """Capital-gate inputs: completed round trips, NET-of-fee live PF, shadow match.

    PF gross of fees is misleading at this capital level — fees dwarf gross
    P&L (backtest PF 1.79 coexists with a negative net return for the same
    reason). Each SELL's pnl is reduced by its own fee plus an equal share of
    the window's BUY fees.
    """
    out = {"sells": 0, "pf": None, "wins": 0, "losses": 0, "shadow": None,
           "shadow_age_d": None, "realized": 0.0}
    try:
        import sqlite3
        con = sqlite3.connect("logs/trades.db")
        sell_rows = con.execute(
            "SELECT pnl, COALESCE(fee_cost, 0), COALESCE(signal_reason, '')"
            " FROM fills WHERE side='SELL' AND quantity > 0 AND pnl IS NOT NULL"
            " AND timestamp >= ?"
            " AND (notes IS NULL OR (notes NOT LIKE '%phantom%'"
            "                        AND notes NOT LIKE '%backfill%'))",
            (_GATE_START,),
        ).fetchall()
        buy_fees = con.execute(
            "SELECT COALESCE(SUM(fee_cost), 0) FROM fills"
            " WHERE side='BUY' AND timestamp >= ?"
            " AND (notes IS NULL OR notes NOT LIKE '%backfill%')",
            (_GATE_START,),
        ).fetchone()[0]
        con.close()

        # Partial TPs realize P&L (counted in PF) but are not completed
        # round trips (excluded from the 15-fill gate count).
        out["sells"] = sum(1 for _, _, reason in sell_rows if reason != "partial_tp")
        buy_fee_share = (buy_fees or 0.0) / len(sell_rows) if sell_rows else 0.0
        net_pnls = [pnl - fee - buy_fee_share for pnl, fee, _ in sell_rows]
        gross_p = sum(p for p in net_pnls if p > 0)
        gross_l = -sum(p for p in net_pnls if p < 0)
        out["wins"]     = sum(1 for p in net_pnls if p > 0)
        out["losses"]   = sum(1 for p in net_pnls if p < 0)
        out["realized"] = sum(net_pnls)
        if gross_l > 0:
            out["pf"] = gross_p / gross_l
        elif gross_p > 0:
            out["pf"] = float("inf")
    except Exception:
        pass
    try:
        import re
        reports = sorted(glob.glob("logs/shadow_report_*.md"))
        if reports:
            text = open(reports[-1], encoding="utf-8").read()
            m = re.search(r"Match rate[^0-9]*([0-9]+(?:\.[0-9]+)?)\s*%", text)
            if m:
                out["shadow"] = float(m.group(1))
            d = re.search(r"shadow_report_(\d{8})\.md", reports[-1])
            if d:
                report_day = datetime.strptime(d.group(1), "%Y%m%d").date()
                # Reports are named by UTC date — can be "tomorrow" local
                # in the evening; clamp so a fresh report never shows -1d.
                out["shadow_age_d"] = max(
                    0, (datetime.now().date() - report_day).days
                )
    except Exception:
        pass
    return out


def _gate_tracker_section() -> str:
    """Scoreboard for the $100 → $250 capital gate: 15 fills + PF ≥ 1.2 + shadow ≥ 95%."""
    g = _read_gate_stats()
    fills_needed = 15

    pct = min(100, int(g["sells"] / fills_needed * 100))
    bar = (
        f'<div style="background:#21262d;border-radius:4px;height:8px;margin-top:8px">'
        f'<div style="background:#1f6feb;height:8px;border-radius:4px;width:{pct}%"></div></div>'
    )
    fills_block = _stat_block(
        "Gate 1 · Round Trips",
        f'{g["sells"]} / {fills_needed}' + bar,
        f'{g["wins"]}W / {g["losses"]}L · realized ${g["realized"]:+.2f} net of fees'
        f' · since {_GATE_START[:10]} (validated config)',
    )

    if g["pf"] is None:
        pf_val, pf_sub = "—", "needs closed trades with recorded P&L"
    else:
        pf_s   = "∞" if g["pf"] == float("inf") else f'{g["pf"]:.2f}'
        pf_col = "#3fb950" if (g["pf"] >= 1.2) else "#d29922"
        pf_val = f'<span style="color:{pf_col}">{pf_s}</span>'
        pf_sub = "target ≥ 1.2 over ≥15 fills — NET of fees (small sample — direction only)"
    pf_block = _stat_block("Gate 2 · Live PF (net)", pf_val, pf_sub)

    if g["shadow"] is None:
        sh_val, sh_sub = "—", "run python shadow_signal.py"
    else:
        sh_col = "#3fb950" if g["shadow"] >= 95 else "#f85149"
        age = g["shadow_age_d"]
        # A stale report must not look like a fresh one — the daily audit
        # silently failed for a week before this indicator existed.
        if age is None:
            age_html, age_sub = "", "report date unknown"
        elif age <= 1:
            age_html, age_sub = "", f"report {age}d old"
        else:
            age_col = "#d29922" if age <= 3 else "#f85149"
            age_html = (f' <span style="color:{age_col};font-size:12px">'
                        f'⚠ {age}d old</span>')
            age_sub = f"STALE — daily audit hasn't run in {age} days"
        sh_val = f'<span style="color:{sh_col}">{g["shadow"]:.1f}%</span>{age_html}'
        sh_sub = f"target ≥ 95% — strategy fidelity · {age_sub}"
    sh_block = _stat_block("Gate 3 · Shadow Match", sh_val, sh_sub)

    return (
        '<div style="margin-bottom:8px;font-size:11px;color:#8b949e;'
        'text-transform:uppercase;letter-spacing:.05em;font-weight:600">'
        'Capital Gate — $100 → $250 requires all three</div>'
        '<div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));'
        'gap:12px;margin-bottom:24px">'
        + fills_block + pf_block + sh_block
        + "</div>"
    )


def _paper_book_stats(*csv_paths: str) -> tuple[int, float | None, float | None]:
    """(completed trades, net PF, net win rate %) across one or more stock-bot
    trade CSVs (position book = sim paper + IBKR paper, merged by timestamp).
    Reuses paper_report's pairing + net-of-commission math — no duplicate logic."""
    try:
        from stock_bot.analysis.paper_report import (
            _expectancy_stats, _pair_trades, _read_trades,
        )
        trades: list[dict] = []
        for p in csv_paths:
            trades += _read_trades(p)
        trades.sort(key=lambda t: t.get("timestamp", ""))
        pairs, _ = _pair_trades(trades)
        stats = _expectancy_stats(pairs)
        if stats is None:
            return 0, None, None
        return stats["n"], stats["net_pf"], stats["net_win_rate"]
    except Exception:
        return 0, None, None


def _book_gates_section() -> str:
    """Gates at a glance — every book's trade-count progress and net PF side
    by side (roadmap item C). Detail stays in each book's own card below."""
    def block(label: str, n: int, need: int, pf: float | None,
              wr: float | None, sub: str) -> str:
        if pf is None:
            pf_html = '<span style="color:#8b949e">PF —</span>'
        else:
            pf_s   = "∞" if pf == float("inf") else f"{pf:.2f}"
            pf_col = "#3fb950" if pf >= 1.2 else "#d29922"
            pf_html = f'<span style="color:{pf_col}">PF {pf_s}</span>'
        wr_s = f" · WR {wr:.0f}%" if wr is not None else ""
        pct = min(100, int(n / need * 100)) if need else 0
        bar = (
            f'<div style="background:#21262d;border-radius:4px;height:6px;margin-top:6px">'
            f'<div style="background:#1f6feb;height:6px;border-radius:4px;width:{pct}%"></div></div>'
        )
        return _stat_block(label, f"{n} / {need} · {pf_html}{wr_s}{bar}", sub)

    g = _read_gate_stats()
    wins, losses = g["wins"], g["losses"]
    crypto_wr = wins / (wins + losses) * 100 if (wins + losses) else None

    pos_n, pos_pf, pos_wr = _paper_book_stats("stock_bot/paper_trades.csv", IBKR_TRADES_PATH)
    swi_n, swi_pf, swi_wr = _paper_book_stats("stock_bot/fast_trades.csv")

    return (
        '<div style="margin-bottom:8px;font-size:11px;color:#8b949e;'
        'text-transform:uppercase;letter-spacing:.05em;font-weight:600">'
        'Gates at a Glance — trades toward each book\'s gate · PF net of costs</div>'
        '<div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));'
        'gap:12px;margin-bottom:24px">'
        + block("Crypto · Live", g["sells"], 15, g["pf"], crypto_wr,
                "BTC/CAD · gate: PF ≥ 1.2 + shadow ≥ 95%")
        + block(
            "Position Book · IBKR" if _stock_executor_type() == "ibkr"
            else "Position Book · Paper",
            pos_n, 30, pos_pf, pos_wr,
            "daily candles · gate: PF ≥ 1.2, WR ≥ 30%")
        + block("Swing Book · Paper", swi_n, 30, swi_pf, swi_wr,
                "1h · 48h max hold · gate: PF ≥ 1.2, WR ≥ 30%")
        + "</div>"
    )


def _pnl_trend_section() -> str:
    """Daily realized P&L bars from trades.db (crypto live only — stock
    realized P&L lives in the Gates strip and position-book card)."""
    daily: dict[str, float] = {}
    try:
        import sqlite3
        con = sqlite3.connect("logs/trades.db")
        rows = con.execute(
            "SELECT substr(timestamp, 1, 10), SUM(pnl) FROM fills"
            " WHERE side='SELL' AND pnl IS NOT NULL AND quantity > 0"
            " GROUP BY substr(timestamp, 1, 10) ORDER BY 1"
        ).fetchall()
        con.close()
        daily = {d: (p or 0.0) for d, p in rows}
    except Exception:
        pass
    if not daily:
        return ""

    max_abs = max(abs(v) for v in daily.values()) or 1.0
    bars = ""
    for day, v in list(daily.items())[-14:]:
        h   = max(4, int(abs(v) / max_abs * 48))
        col = "#3fb950" if v >= 0 else "#f85149"
        bars += (
            f'<div style="display:flex;flex-direction:column;align-items:center;gap:4px">'
            f'<div style="font-size:9px;color:{col}">{v:+.2f}</div>'
            f'<div style="width:26px;height:{h}px;background:{col};border-radius:3px;'
            f'align-self:center;margin-top:{48-h}px"></div>'
            f'<div style="font-size:9px;color:#8b949e">{day[5:]}</div>'
            f'</div>'
        )
    return (
        '<div style="background:#161b22;border:1px solid #30363d;border-radius:8px;'
        'padding:14px 16px;margin-bottom:24px">'
        '<div style="font-size:11px;color:#8b949e;text-transform:uppercase;'
        'letter-spacing:.05em;font-weight:600;margin-bottom:10px">'
        'Realized P&L by Day (crypto live only · stock P&L in Gates strip above)</div>'
        f'<div style="display:flex;gap:10px;align-items:flex-end">{bars}</div>'
        '</div>'
    )


def _count_swing_completed() -> int:
    """Count completed round-trips (SELL rows) in fast_trades.csv."""
    try:
        path = "stock_bot/fast_trades.csv"
        if not os.path.exists(path):
            return 0
        with open(path, encoding="utf-8", newline="") as f:
            return sum(1 for row in csv.reader(f) if len(row) > 2 and row[2].upper() == "SELL")
    except Exception:
        return 0


def _fast_validator_card() -> str:
    """Swing book — 1h candles, 48h max hold, cash-tracked (separate from position book)."""
    data = _load_json("stock_bot/fast_validator_state.json")
    if not data:
        return ""

    positions    = data.get("positions", [])
    cash         = float(data.get("cash",          0))
    starting     = float(data.get("starting_cash", 0))
    realized_pnl = float(data.get("realized_pnl",  0))
    completed    = _count_swing_completed()

    ret_pct  = (cash - starting + realized_pnl) / starting * 100 if starting else 0.0
    ret_col  = "#3fb950" if ret_pct >= 0 else "#f85149"
    ret_s    = "+" if ret_pct >= 0 else ""

    gate_pct = min(100, int(completed / 30 * 100))
    gate_bar = (
        f'<div style="background:#21262d;border-radius:4px;height:6px;margin-top:6px">'
        f'<div style="background:#d29922;height:6px;border-radius:4px;width:{gate_pct}%"></div></div>'
    )

    pos_rows = "".join(
        _kv(
            f'{p.get("symbol", "?")} × {float(p.get("shares", 0)):g}sh',
            f'in @ ${float(p.get("entry_price", 0)):,.2f} · '
            f'SL ${float(p.get("sl_price", 0)):,.2f} · TP ${float(p.get("tp_price", 0)):,.2f}',
        )
        for p in positions
    ) or _kv("Open positions", "none")

    return (
        '<div class="pf-card" style="margin-bottom:24px">'
        '<div class="pf-card-header">'
        '<span class="pf-card-title">⚡ Swing Book</span>'
        '<span class="pf-card-badge" style="background:#d2992222;color:#d29922;'
        f'border-color:#d2992255">1h · 48h max · {len(positions)} open</span>'
        '</div>'
        + (
            _kv("Cash", f"${cash:,.2f}  (started ${starting:,.2f})")
            if starting else _kv("Cash", f"${cash:,.2f}")
        )
        + _kv("Realized P&L", _pnl(realized_pnl))
        + _kv(
            "Return",
            f'<span style="color:{ret_col}">{ret_s}{ret_pct:.1f}%</span>'
        )
        + _kv(
            "Phase A progress",
            f'{completed} / 30 completed trades{gate_bar}',
        )
        + pos_rows
        + _kv("Last updated", _fmt_ts(data.get("last_updated")))
        + "</div>"
    )


def _regime_card() -> str:
    """Latest regime-monitor verdict per symbol from logs/regime_health.log."""
    import re
    try:
        lines = open("logs/regime_health.log", encoding="utf-8").read().splitlines()
    except Exception:
        return ""
    latest: dict[str, str] = {}
    for ln in lines:
        m = re.match(r"([\d-]+ [\d:]+ UTC)\s+(\S+)\s+(.*)", ln)
        if m:
            latest[m.group(2)] = ln
    if not latest:
        return ""

    rows = ""
    for sym in sorted(latest):
        ln = latest[sym]
        ts = ln[:16]
        verdict = (re.search(r"verdict=(\S+)", ln) or [None, "?"])[1]
        col = {"EDGE": "#3fb950", "WARN": "#d29922", "DEGRADED": "#d29922"}.get(verdict, "#f85149")
        adx    = re.search(r"ADX=\s*([\d.]+)", ln)
        spread = re.search(r"spread=\s*([\d.]+)%", ln)
        vol    = re.search(r"vol_cad=([\d,]+)", ln)
        if adx and spread:
            detail = f"ADX {adx.group(1)} · spread {spread.group(1)}%"
        elif vol:
            detail = f"24h vol ${vol.group(1)} CAD (liquidity watch)"
        else:
            detail = ""
        rows += _kv(
            sym,
            f'<span style="color:{col};font-weight:600">{verdict}</span>'
            f' <span style="color:#8b949e;font-weight:400;font-size:11px">'
            f'{detail} · {ts}</span>',
        )
    return (
        '<div class="pf-card" style="margin-bottom:24px">'
        '<div class="pf-card-header">'
        '<span class="pf-card-title">📡 Regime Monitor (latest reading)</span>'
        '</div>'
        + rows
        + "</div>"
    )


def _ops_status_section() -> str:
    """Kill-switch + risk-breaker state strip (files written by the live bot)."""
    halted = os.path.exists(HALT_FLAG_PATH)
    halt_val = (
        '<span style="color:#f85149">🛑 HALTED</span>' if halted
        else '<span style="color:#3fb950">● trading enabled</span>'
    )
    halt_sub = "rm logs/HALT to resume" if halted else "touch logs/HALT to kill-switch"

    risk = _load_json(RISK_STATE_PATH)
    if risk:
        fills_by_sym = risk.get("fills_by_symbol") or {}
        fills = int(risk.get("fills_today", 0))
        fills_sub = (
            " · ".join(f"{s} {n}" for s, n in fills_by_sym.items())
            if fills_by_sym else f"UTC day {risk.get('today', '—')}"
        )
        peak = float(risk.get("peak_value") or 0.0)
        dov  = risk.get("day_open_value")
        peak_sub = f"day open ${float(dov):,.2f}" if dov else "day open unset"
        fills_val = str(fills)
        peak_val  = f"${peak:,.2f}"
    else:
        fills_val, fills_sub = "—", "logs/risk_state.json not written yet"
        peak_val,  peak_sub  = "—", "restart-safe since 2026-07-03"

    return (
        '<div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));'
        'gap:12px;margin-bottom:24px">'
        + _heartbeat_blocks()
        + _stat_block("Kill-Switch", halt_val, halt_sub)
        + _stat_block("Fills Today", fills_val, fills_sub)
        + _stat_block("All-Time Peak (breaker)", peak_val, peak_sub)
        + "</div>"
    )


def _signals_section(signals: dict) -> str:
    if not signals:
        return (
            '<div style="margin-top:14px;padding:10px 14px;background:#0d1117;'
            'border:1px solid #30363d;border-radius:6px;font-size:11px;color:#8b949e;'
            'font-style:italic">No candle closes yet — waiting for signals</div>'
        )

    TH = ('style="text-align:left;padding:6px 10px;font-size:10px;color:#8b949e;'
          'font-weight:600;text-transform:uppercase;letter-spacing:.05em;'
          'border-bottom:1px solid #30363d;white-space:nowrap"')
    TD = 'padding:7px 10px;border-bottom:1px solid #21262d;font-size:12px;white-space:nowrap'

    rows = ""
    for sym in sorted(signals):
        row = signals[sym]
        try:
            price_str = f"${float(row.get('close', 0)):,.2f}"
        except Exception:
            price_str = row.get("close", "—")
        try:
            rsi = f"{float(row.get('rsi', 0)):.1f}"
        except Exception:
            rsi = row.get("rsi", "—")
        try:
            adx = f"{float(row.get('adx', 0)):.1f}"
        except Exception:
            adx = row.get("adx", "—")
        signal = (row.get("signal") or "—").strip()
        reason = (row.get("reason") or "—").strip()
        sig_color = {"BUY": "#3fb950", "SELL": "#f85149"}.get(signal.upper(), "#8b949e")
        rows += (
            f'<tr>'
            f'<td style="{TD};color:#c9d1d9"><strong>{sym}</strong></td>'
            f'<td style="{TD};color:#c9d1d9">{price_str}</td>'
            f'<td style="{TD};color:#c9d1d9">{rsi}</td>'
            f'<td style="{TD};color:#c9d1d9">{adx}</td>'
            f'<td style="{TD};color:{sig_color};font-weight:600">{signal}</td>'
            f'<td style="{TD};color:#8b949e">{reason}</td>'
            f'</tr>'
        )

    return (
        '<div style="margin-top:14px;background:#0d1117;border:1px solid #30363d;'
        'border-radius:6px;overflow:hidden;overflow-x:auto">'
        '<div style="padding:8px 12px;border-bottom:1px solid #30363d;font-size:10px;'
        'color:#8b949e;text-transform:uppercase;letter-spacing:.05em;font-weight:600">'
        'Active Signals'
        '</div>'
        '<table style="width:100%;border-collapse:collapse">'
        '<thead><tr>'
        f'<th {TH}>Symbol</th>'
        f'<th {TH}>Price</th>'
        f'<th {TH}>RSI</th>'
        f'<th {TH}>ADX</th>'
        f'<th {TH}>Signal</th>'
        f'<th {TH}>Reason</th>'
        '</tr></thead>'
        f'<tbody>{rows}</tbody>'
        '</table>'
        '</div>'
    )


def _fetch_crypto_price(symbol: str) -> str:
    try:
        import urllib.request
        pair = symbol.replace("/", "").replace("BTC", "XBT")
        url = f"https://api.kraken.com/0/public/Ticker?pair={pair}"
        with urllib.request.urlopen(url, timeout=3) as r:
            data = json.loads(r.read())
            result = data.get("result", {})
            if result:
                ticker = list(result.values())[0]
                return f"${float(ticker['c'][0]):,.2f}"
    except Exception:
        pass
    return "—"


def _fetch_kraken_price_raw(asset: str) -> float | None:
    try:
        import urllib.request
        sym = asset.replace("BTC", "XBT")
        pair = f"{sym}CAD"
        url = f"https://api.kraken.com/0/public/Ticker?pair={pair}"
        with urllib.request.urlopen(url, timeout=3) as r:
            data = json.loads(r.read())
            result = data.get("result", {})
            if result:
                return float(list(result.values())[0]["c"][0])
    except Exception:
        pass
    return None


def _fetch_kraken_balances() -> dict[str, float] | None:
    """
    Authenticated fetch of REAL account balances (Kraken `total`, matching
    _sync_position / drift checks). Returns {asset: amount} for non-zero
    assets incl. 'CAD', or None when keys are missing or the fetch fails —
    caller falls back to the manual logs/kraken_holdings.json snapshot.
    """
    try:
        from dotenv import load_dotenv
        load_dotenv()
        key    = os.getenv("KRAKEN_API_KEY", "")
        secret = os.getenv("KRAKEN_API_SECRET", "")
        if not key or not secret:
            return None
        import ccxt
        ex  = ccxt.kraken({"apiKey": key, "secret": secret, "enableRateLimit": True})
        tot = ex.fetch_balance().get("total", {}) or {}
        return {a: float(v) for a, v in tot.items() if v and float(v) > 1e-12}
    except Exception:
        return None


def _fmt_price(p: float) -> str:
    return f"${p:,.4f}" if p < 1 else f"${p:,.2f}"


def _fmt_balance(b: float) -> str:
    if b < 0.01:
        return f"{b:.6f}"
    if b < 1:
        return f"{b:.5f}"
    return f"{b:,.2f}"


def _kraken_holdings_card(bot_cash: float) -> str:
    """
    Real Kraken account: live authenticated balances when API keys are
    available; falls back to the manual logs/kraken_holdings.json snapshot
    otherwise. The manual file only supplies avg-cost for P&L — it is NOT
    the source of what the account holds (it went stale Jun 26 and kept
    showing assets sold in the Jun 27 incident).
    """
    cost_ref = _load_json(KRAKEN_HOLDINGS_PATH) or {}
    balances = _fetch_kraken_balances()

    if balances is not None:
        cad_cash = balances.pop("CAD", 0.0)
        assets   = balances
        title    = "Kraken Account (live)"
    elif cost_ref:
        cad_cash = None
        assets   = {a: float(i.get("balance", 0)) for a, i in cost_ref.items()}
        title    = "Kraken Holdings (manual snapshot — may be stale)"
    else:
        return ""

    TH = ('style="text-align:left;padding:6px 10px;font-size:10px;color:#8b949e;'
          'font-weight:600;text-transform:uppercase;letter-spacing:.05em;'
          'border-bottom:1px solid #30363d;white-space:nowrap"')
    TD = "padding:7px 10px;border-bottom:1px solid #21262d;font-size:12px;white-space:nowrap"

    rows = ""
    total_value = 0.0

    for asset, balance in sorted(assets.items()):
        avg_price = float(cost_ref.get(asset, {}).get("avg_price", 0))
        cost_val  = balance * avg_price
        current   = _fetch_kraken_price_raw(asset)
        avg_str   = _fmt_price(avg_price) if avg_price > 0 else "—"

        if current is not None:
            cur_val = balance * current
            cur_str = _fmt_price(current)
            val_str = f"${cur_val:,.2f}"
            total_value += cur_val
            if avg_price > 0:
                pnl     = cur_val - cost_val
                pnl_pct = pnl / cost_val * 100 if cost_val else 0.0
                pnl_col = "#3fb950" if pnl >= 0 else "#f85149"
                pnl_s   = "+" if pnl >= 0 else ""
                pnl_str = f'<span style="color:{pnl_col}">{pnl_s}${pnl:,.2f}</span>'
                pct_str = f'<span style="color:{pnl_col}">{pnl_s}{pnl_pct:.1f}%</span>'
            else:
                pnl_str = "—"
                pct_str = "—"
        else:
            cur_str = "—"
            val_str = f"${cost_val:,.2f}" if avg_price > 0 else "—"
            pnl_str = "—"
            pct_str = "—"
            total_value += cost_val

        rows += (
            f"<tr>"
            f'<td style="{TD};color:#c9d1d9"><strong>{asset}</strong></td>'
            f'<td style="{TD};color:#c9d1d9">{_fmt_balance(balance)}</td>'
            f'<td style="{TD};color:#c9d1d9">{avg_str}</td>'
            f'<td style="{TD};color:#c9d1d9">{cur_str}</td>'
            f'<td style="{TD};color:#c9d1d9">{val_str}</td>'
            f'<td style="{TD}">{pnl_str}</td>'
            f'<td style="{TD}">{pct_str}</td>'
            f"</tr>"
        )

    if not rows:
        rows = (
            f'<tr><td colspan="7" style="{TD};color:#8b949e">'
            "no crypto assets held</td></tr>"
        )

    if cad_cash is not None:
        # Live mode: account cash is real; slot cash is the bot's share of it.
        total_combined = cad_cash + total_value
        footer = (
            f'Account cash: <strong style="color:#e6edf3">${cad_cash:,.2f} CAD</strong>'
            f' (bot slot uses <strong style="color:#e6edf3">${bot_cash:,.2f}</strong>)'
            f' + Holdings: <strong style="color:#e6edf3">${total_value:,.2f}</strong>'
            f' = Total: <strong style="color:#3fb950">${total_combined:,.2f}</strong>'
        )
    else:
        total_combined = bot_cash + total_value
        footer = (
            f'Bot Cash: <strong style="color:#e6edf3">${bot_cash:,.2f}</strong>'
            f' + Holdings: <strong style="color:#e6edf3">${total_value:,.2f}</strong>'
            f' = Total: <strong style="color:#3fb950">${total_combined:,.2f}</strong>'
        )

    return (
        '<div style="margin-top:14px;background:#0d1117;border:1px solid #30363d;'
        'border-radius:6px;overflow:hidden;overflow-x:auto">'
        '<div style="padding:8px 12px;border-bottom:1px solid #30363d;font-size:10px;'
        'color:#8b949e;text-transform:uppercase;letter-spacing:.05em;font-weight:600">'
        f'{title}'
        '</div>'
        '<table style="width:100%;border-collapse:collapse">'
        '<thead><tr>'
        f'<th {TH}>Asset</th>'
        f'<th {TH}>Balance</th>'
        f'<th {TH}>Avg Cost</th>'
        f'<th {TH}>Current</th>'
        f'<th {TH}>Value CAD</th>'
        f'<th {TH}>P&amp;L CAD</th>'
        f'<th {TH}>P&amp;L %</th>'
        '</tr></thead>'
        f'<tbody>{rows}</tbody>'
        '</table>'
        '<div style="padding:10px 12px;border-top:1px solid #30363d;font-size:12px;color:#8b949e">'
        f'{footer}'
        '</div>'
        '</div>'
    )


def _crypto_card(symbol: str, state: dict) -> str:
    """One card per active crypto slot."""
    cash     = float(state.get("cash", 0))
    position = float(state.get("position", 0))
    basis    = float(state.get("cost_basis", 0))
    rpnl     = float(state.get("realized_pnl", 0))
    fees     = float(state.get("fees_paid", 0))
    saved    = _fmt_ts(state.get("saved_at"))
    base     = symbol.split("/")[0] if "/" in symbol else "crypto"

    pos_val    = basis if position > 0 else 0.0
    total      = cash + pos_val
    live_price = _fetch_crypto_price(symbol)

    age = _hours_old(state.get("saved_at"))
    if age is not None and age > STALE_AFTER_H:
        badge = (
            f'<span class="pf-card-badge" style="background:#f8514922;color:#f85149;'
            f'border-color:#f8514955">STALE · {age/24:.0f}d old</span>'
        )
    else:
        badge = (
            f'<span class="pf-card-badge" style="background:#1f6feb22;color:#58a6ff;'
            f'border-color:#1f6feb55">LIVE · {symbol}</span>'
        )

    holding_row = _kv("Position", f"{position:.6f} {base}") if position > 0 else _kv("Position", "Flat")
    basis_row   = _kv("Cost basis", f"${basis:,.2f}") if position > 0 else ""

    return (
        '<div class="pf-card">'
        '<div class="pf-card-header">'
        f'<span class="pf-card-title">⚡ {symbol}</span>'
        f'{badge}'
        '</div>'
        + _kv("Live Price", f"<strong>{live_price}</strong>")
        + _kv("Slot cash", f"${cash:,.2f} CAD")
        + holding_row
        + basis_row
        + _kv("Realized P&L", _pnl(rpnl))
        + _kv("Fees paid", f"${fees:.4f}")
        + _kv("Slot value", f"${total:,.2f}")
        + _kv("Last saved", saved)
        + "</div>"
    )


def _crypto_offline_card() -> str:
    return (
        '<div class="pf-card offline">'
        '<div>⚡ Crypto bot offline</div>'
        '<div style="font-size:11px;color:#8b949e;margin-top:4px">'
        'no logs/live_state_*.json found</div>'
        '</div>'
    )


def _retired_slots_note(retired: dict[str, dict]) -> str:
    """Leftover state files for symbols no longer in UNIVERSE_WHITELIST.
    Their cash sits in the shared Kraken account — shown but not counted
    as bot-managed capital."""
    if not retired:
        return ""
    items = " · ".join(
        f"{sym} (${float(d.get('cash', 0)):,.2f}, "
        f"saved {_fmt_ts(d.get('saved_at')).split(' ')[0]})"
        for sym, d in retired.items()
    )
    return (
        '<div style="margin:0 0 24px;padding:10px 14px;background:#0d1117;'
        'border:1px solid #30363d;border-radius:6px;font-size:11px;color:#8b949e">'
        f'<strong style="color:#c9d1d9">Retired slots</strong> (not in whitelist, '
        f'not counted — cash remains in the Kraken account): {items}'
        '</div>'
    )


def _stock_card(state: dict | None) -> str:
    if state is None:
        return (
            '<div class="pf-card offline">'
            '<div>📈 Stock bot offline</div>'
            '<div style="font-size:11px;color:#8b949e;margin-top:4px">'
            'no executor state file found (paper_state.json / ibkr_state.json)</div>'
            '</div>'
        )

    cash      = float(state.get("cash", 0))
    rpnl      = float(state.get("realized_pnl", 0))
    positions = state.get("positions", {})
    starting  = float(state.get("starting_cash", 1000))
    updated   = _fmt_ts(state.get("last_updated"))

    pos_val = sum(
        float(p.get("shares", 0)) * float(p.get("avg_cost", 0))
        for p in positions.values()
    )
    total   = cash + pos_val
    ret_pct = (total - starting) / starting * 100 if starting else 0.0
    ret_col = "#3fb950" if ret_pct >= 0 else "#f85149"
    ret_s   = "+" if ret_pct >= 0 else ""

    if state.get("executor") == "ibkr":
        acct  = state.get("account", "")
        badge = f"IBKR PAPER{' · ' + acct if acct else ''}"
    else:
        badge = "PAPER"

    return (
        '<div class="pf-card">'
        '<div class="pf-card-header">'
        '<span class="pf-card-title">📈 Stock Bot</span>'
        f'<span class="pf-card-badge" style="background:#7c8cf822;color:#7c8cf8;border-color:#7c8cf855">{badge}</span>'
        '</div>'
        + _kv("Cash", f"${cash:,.2f}")
        + _kv("Open positions", str(len(positions)))
        + _kv("Position value (est.)", f"${pos_val:,.2f}")
        + _kv("Realized P&L", _pnl(rpnl))
        + _kv(
            "Total value",
            f'${total:,.2f} <span style="color:{ret_col};font-size:12px">{ret_s}{ret_pct:.1f}%</span>',
        )
        + _kv("Starting cash", f"${starting:,.2f}")
        + _kv("Last updated", updated)
        + "</div>"
    )


# Price cache: {symbol: [price_or_None, fetched_at_epoch]}. Regeneration can
# run every 30-60s — without a TTL that means a yfinance call per symbol per
# cycle, which Yahoo rate-limits within minutes. Failures are cached too so a
# rate-limited symbol isn't retried every cycle. Persisted to a file because
# the generator now runs as a short-lived subprocess from the bot — an
# in-memory cache would be empty on every run.
STOCK_PRICE_TTL_S      = 900   # 15 min — plenty fresh for a paper dashboard
STOCK_PRICE_CACHE_PATH = "logs/stock_price_cache.json"


def _load_price_cache() -> dict:
    try:
        with open(STOCK_PRICE_CACHE_PATH, encoding="utf-8") as f:
            raw = json.load(f)
        return {k: [v[0], float(v[1])] for k, v in raw.items()}
    except Exception:
        return {}


def _save_price_cache(cache: dict) -> None:
    try:
        os.makedirs(os.path.dirname(STOCK_PRICE_CACHE_PATH), exist_ok=True)
        with open(STOCK_PRICE_CACHE_PATH, "w", encoding="utf-8") as f:
            json.dump(cache, f)
    except Exception:
        pass


def _fetch_stock_prices(symbols: list[str]) -> dict[str, float]:
    """Live prices via the stock bot's own guarded feed, TTL-cached to disk.
    Returns only symbols with a known price — the table falls back to
    avg-cost marks for the rest."""
    prices: dict[str, float] = {}
    if not symbols:
        return prices
    try:
        from stock_bot.data.price_feed import latest_price
    except Exception:
        return prices
    cache = _load_price_cache()
    now = time.time()
    dirty = False
    for sym in symbols:
        cached = cache.get(sym)
        if cached is not None and now - cached[1] < STOCK_PRICE_TTL_S:
            if cached[0] is not None:
                prices[sym] = float(cached[0])
            continue
        price: float | None = None
        try:
            p = latest_price(sym)
            if p and p > 0:
                price = float(p)
        except Exception:
            price = None
        cache[sym] = [price, now]
        dirty = True
        if price is not None:
            prices[sym] = price
    if dirty:
        _save_price_cache(cache)
    return prices


def _stock_positions_table(state: dict | None) -> str:
    if state is None:
        return ""
    positions = state.get("positions", {})
    if not positions:
        return (
            '<p style="color:#8b949e;font-size:12px;font-style:italic;'
            'margin-top:8px">No open stock positions</p>'
        )

    th = (
        'style="text-align:left;padding:7px 12px;font-size:10px;color:#8b949e;'
        'font-weight:600;text-transform:uppercase;letter-spacing:.05em;'
        'border-bottom:1px solid #30363d;white-space:nowrap"'
    )
    td = (
        'style="padding:8px 12px;border-bottom:1px solid #21262d;'
        'font-size:12px;color:#c9d1d9;white-space:nowrap"'
    )

    live = _fetch_stock_prices(list(positions.keys()))

    rows = ""
    total_upnl = 0.0
    have_any_live = False
    for sym, pos in positions.items():
        shares   = float(pos.get("shares", 0))
        avg_cost = float(pos.get("avg_cost", 0))
        cur      = live.get(sym)
        if cur is not None and avg_cost > 0:
            have_any_live = True
            mkt_val  = shares * cur
            upnl     = mkt_val - shares * avg_cost
            upnl_pct = (cur - avg_cost) / avg_cost * 100
            total_upnl += upnl
            col   = "#3fb950" if upnl >= 0 else "#f85149"
            s     = "+" if upnl >= 0 else ""
            cur_s = f"${cur:,.2f}"
            val_s = f"${mkt_val:,.2f}"
            pnl_s = f'<span style="color:{col}">{s}${upnl:,.2f} ({s}{upnl_pct:.1f}%)</span>'
        else:
            cur_s = "—"
            val_s = f"${shares * avg_cost:,.2f} (cost)"
            pnl_s = '<span style="color:#8b949e">—</span>'
        rows += (
            f"<tr>"
            f"<td {td}><strong>{sym}</strong></td>"
            f"<td {td}>{shares:,.0f}</td>"
            f"<td {td}>${avg_cost:,.2f}</td>"
            f"<td {td}>{cur_s}</td>"
            f"<td {td}>{val_s}</td>"
            f"<td {td}>{pnl_s}</td>"
            f"</tr>"
        )

    footer = ""
    if have_any_live:
        col = "#3fb950" if total_upnl >= 0 else "#f85149"
        s   = "+" if total_upnl >= 0 else ""
        footer = (
            '<div style="padding:10px 12px;border-top:1px solid #30363d;'
            'font-size:12px;color:#8b949e">'
            f'Unrealized P&amp;L: <strong style="color:{col}">{s}${total_upnl:,.2f}</strong>'
            '</div>'
        )

    _tbl_title = (
        "📦 Stock Positions — IBKR paper"
        if state.get("executor") == "ibkr" else "📦 Stock Paper Positions"
    )
    return (
        '<div style="background:#161b22;border:1px solid #30363d;border-radius:8px;'
        'overflow:hidden;overflow-x:auto;margin-top:16px">'
        '<div style="padding:10px 14px;border-bottom:1px solid #30363d;font-size:11px;'
        'color:#8b949e;text-transform:uppercase;letter-spacing:.05em;font-weight:600">'
        f"{_tbl_title}"
        "</div>"
        '<table style="width:100%;border-collapse:collapse">'
        "<thead><tr>"
        f"<th {th}>Symbol</th>"
        f"<th {th}>Shares</th>"
        f"<th {th}>Avg Cost</th>"
        f"<th {th}>Live</th>"
        f"<th {th}>Value</th>"
        f"<th {th}>Unrealized P&L</th>"
        f"</tr></thead>"
        f"<tbody>{rows}</tbody>"
        "</table>"
        f"{footer}"
        "</div>"
    )


def _portfolio_tab_html(
    active: dict[str, dict], retired: dict[str, dict], stock: dict | None
) -> str:
    crypto_cards = (
        "".join(_crypto_card(sym, st) for sym, st in active.items())
        if active else _crypto_offline_card()
    )
    active_cash = sum(float(d.get("cash", 0)) for d in active.values())

    stock_cash = float(stock.get("cash", 0)) if stock else 0.0
    stock_pv   = sum(
        float(p.get("shares", 0)) * float(p.get("avg_cost", 0))
        for p in (stock.get("positions", {}) if stock else {}).values()
    )
    _stock_kind = (
        "Stocks IBKR paper" if stock and stock.get("executor") == "ibkr"
        else "Stocks paper"
    )
    stock_label = (
        f"{_stock_kind} (${stock_cash + stock_pv:,.0f} account)"
        if stock else _stock_kind
    )

    return (
        '<div style="padding:24px 20px;max-width:1000px;margin:0 auto">'
        '<div style="margin-bottom:24px">'
        '<div style="font-size:18px;font-weight:700;color:#e6edf3;margin-bottom:4px">'
        "Portfolio Overview"
        "</div>"
        '<div style="font-size:12px;color:#8b949e">'
        f"Crypto live (Kraken) · {stock_label}"
        "</div>"
        "</div>"
        + _combined_stats(active, stock)
        + _ops_status_section()
        + _gate_tracker_section()
        + _book_gates_section()
        + _pnl_trend_section()
        + '<div style="display:grid;grid-template-columns:1fr 1fr;gap:16px;margin-bottom:24px">'
        + crypto_cards
        + _stock_card(stock)
        + "</div>"
        + _regime_card()
        + _fast_validator_card()
        + _retired_slots_note(retired)
        + _signals_section(read_live_signals())
        + _kraken_holdings_card(active_cash)
        + _stock_positions_table(stock)
        + "</div>"
    )


# ── CSS ───────────────────────────────────────────────────────────────────────

_CSS = """
    *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
    html, body { height: 100%; }
    body {
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", monospace;
      background: #0d1117; color: #e6edf3; font-size: 13px;
    }

    /* Fixed tab bar */
    .tab-bar {
      position: fixed; top: 0; left: 0; right: 0; height: 48px;
      display: flex; align-items: center; gap: 6px; padding: 0 16px;
      background: #161b22; border-bottom: 1px solid #30363d; z-index: 100;
    }
    .tab-btn {
      padding: 5px 18px; border-radius: 6px; border: 1px solid #30363d;
      background: transparent; color: #8b949e; cursor: pointer;
      font-size: 13px; font-weight: 600; font-family: inherit;
    }
    .tab-btn:hover { border-color: #58a6ff; color: #e6edf3; }
    .tab-btn.active { background: #1f6feb; border-color: #1f6feb; color: #fff; }
    .tab-spacer { flex: 1; }
    .tab-ts { font-size: 11px; color: #8b949e; white-space: nowrap; }

    /* Tab content areas — fill below tab bar */
    .tab-content {
      display: none;
      position: fixed; top: 48px; left: 0; right: 0; bottom: 0;
    }
    .tab-content.active { display: block; }

    /* Iframe tabs: iframe fills the area */
    .iframe-tab iframe { width: 100%; height: 100%; border: none; display: block; }

    /* Portfolio tab: scrollable */
    .portfolio-tab { overflow-y: auto; }

    /* Portfolio cards */
    .pf-card {
      background: #161b22; border: 1px solid #30363d;
      border-radius: 10px; padding: 18px;
    }
    .pf-card.offline {
      display: flex; flex-direction: column; justify-content: center;
      align-items: center; min-height: 120px; opacity: .6; color: #8b949e;
    }
    .pf-card-header {
      display: flex; align-items: center; justify-content: space-between;
      margin-bottom: 12px;
    }
    .pf-card-title { font-size: 14px; font-weight: 700; color: #e6edf3; }
    .pf-card-badge {
      font-size: 11px; font-weight: 600; padding: 2px 9px;
      border-radius: 10px; border: 1px solid;
    }

    @media (max-width: 680px) {
      .tab-ts { display: none; }
      .tab-btn { padding: 5px 10px; font-size: 12px; }
    }
"""


# ── JS ────────────────────────────────────────────────────────────────────────

_JS = """
  <script>
    function showTab(name) {
      document.querySelectorAll('.tab-content').forEach(function(el) {
        el.classList.remove('active');
      });
      document.querySelectorAll('.tab-btn').forEach(function(el) {
        el.classList.remove('active');
      });
      var content = document.getElementById('tab-' + name);
      if (content) content.classList.add('active');
      var btn = document.querySelector('[data-tab="' + name + '"]');
      if (btn) btn.classList.add('active');
      try { localStorage.setItem('activeTab', name); } catch(e) {}
    }

    (function() {
      var saved = 'crypto';
      try { saved = localStorage.getItem('activeTab') || 'crypto'; } catch(e) {}
      showTab(saved);
    })();
  </script>
"""


# ── HTML assembler ────────────────────────────────────────────────────────────

def _build_html(
    active: dict[str, dict], retired: dict[str, dict], stock: dict | None
) -> str:
    now       = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    portfolio = _portfolio_tab_html(active, retired, stock)

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta http-equiv="refresh" content="{REFRESH_S}">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>APEX TRADER</title>
  <style>{_CSS}</style>
</head>
<body>

  <div class="tab-bar">
    <button class="tab-btn" data-tab="crypto"    onclick="showTab('crypto')">⚡ Crypto</button>
    <button class="tab-btn" data-tab="stocks"    onclick="showTab('stocks')">📈 Stocks</button>
    <button class="tab-btn" data-tab="portfolio" onclick="showTab('portfolio')">💼 Portfolio</button>
    <div class="tab-spacer"></div>
    <span class="tab-ts">Updated {now} · auto-refresh {REFRESH_S}s</span>
  </div>

  <div id="tab-crypto" class="tab-content iframe-tab">
    <iframe src="dashboard.html" title="Crypto Bot Dashboard"></iframe>
  </div>

  <div id="tab-stocks" class="tab-content iframe-tab">
    <iframe src="stock_dashboard.html" title="Stock Bot Dashboard"></iframe>
  </div>

  <div id="tab-portfolio" class="tab-content portfolio-tab">
    {portfolio}
  </div>

{_JS}
</body>
</html>"""


# ── Entry point ───────────────────────────────────────────────────────────────

def generate() -> None:
    active, retired = _load_crypto_states()
    stock = _load_stock_state()
    html  = _build_html(active, retired, stock)
    Path(OUTPUT_PATH).write_text(html, encoding="utf-8")
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"[{ts}] unified_dashboard.html written "
          f"({len(active)} active crypto slot(s), {len(retired)} retired)")


def main() -> None:
    watch = "--watch" in sys.argv
    if watch:
        print(f"Watching — regenerating every {REFRESH_S}s. Ctrl+C to stop.")
        try:
            while True:
                generate()
                time.sleep(REFRESH_S)
        except KeyboardInterrupt:
            print("\nStopped.")
    else:
        generate()


if __name__ == "__main__":
    main()
