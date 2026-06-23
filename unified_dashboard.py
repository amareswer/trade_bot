"""
Unified dashboard generator — crypto bot + stock bot side by side.

Usage:
    python unified_dashboard.py           # generate once and exit
    python unified_dashboard.py --watch   # regenerate every 30s (Ctrl+C to stop)
"""
from __future__ import annotations

import json
import sys
import time
from datetime import datetime
from pathlib import Path

# ── Paths (all relative to project root) ─────────────────────────────────────
CRYPTO_STATE_PATH   = "logs/live_state.json"
STOCK_STATE_PATH    = "stock_bot/paper_state.json"
OUTPUT_PATH         = "unified_dashboard.html"

CRYPTO_DASHBOARD    = "./dashboard.html"
STOCK_DASHBOARD     = "./stock_dashboard.html"

REFRESH_INTERVAL_S  = 30


# ── State loaders ─────────────────────────────────────────────────────────────

def _load_json(path: str) -> dict | None:
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


# ── Formatting helpers ────────────────────────────────────────────────────────

def _pnl_span(value: float, fmt: str = "+.2f") -> str:
    cls = "pos" if value >= 0 else "neg"
    sign = "+" if value >= 0 else ""
    return f'<span class="{cls}">{sign}{value:{fmt[1:]}}</span>'


def _fmt_ts(ts: str | None) -> str:
    if not ts:
        return "—"
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        return dt.strftime("%Y-%m-%d %H:%M:%S UTC")
    except Exception:
        return ts


# ── Section builders ──────────────────────────────────────────────────────────

def _crypto_section(state: dict | None) -> str:
    if state is None:
        return """
        <div class="bot-card offline">
          <div class="offline-label">Crypto bot offline</div>
          <div class="offline-sub">logs/live_state.json not found or unreadable</div>
        </div>"""

    symbol       = state.get("symbol", "—")
    cash         = float(state.get("cash", 0))
    position     = float(state.get("position", 0))
    cost_basis   = float(state.get("cost_basis", 0))
    realized_pnl = float(state.get("realized_pnl", 0))
    fees_paid    = float(state.get("fees_paid", 0))
    saved_at     = _fmt_ts(state.get("saved_at"))

    base = symbol.split("/")[0] if "/" in symbol else "crypto"

    return f"""
        <div class="bot-card">
          <div class="bot-header">
            <span class="bot-title">Crypto Bot</span>
            <span class="bot-symbol">{symbol}</span>
          </div>
          <div class="kv-grid">
            <div class="kv-row"><span class="kv-key">Cash</span>
              <span class="kv-val">${cash:,.2f}</span></div>
            <div class="kv-row"><span class="kv-key">Position</span>
              <span class="kv-val">{position:.6f} {base}</span></div>
            <div class="kv-row"><span class="kv-key">Cost basis</span>
              <span class="kv-val">${cost_basis:,.2f}</span></div>
            <div class="kv-row"><span class="kv-key">Realized P&amp;L</span>
              <span class="kv-val">{_pnl_span(realized_pnl)}</span></div>
            <div class="kv-row"><span class="kv-key">Fees paid</span>
              <span class="kv-val">${fees_paid:.4f}</span></div>
            <div class="kv-row"><span class="kv-key">Saved at</span>
              <span class="kv-val muted">{saved_at}</span></div>
          </div>
          <a class="dash-link" href="{CRYPTO_DASHBOARD}">Open full dashboard →</a>
        </div>"""


def _positions_table(positions: dict) -> str:
    if not positions:
        return '<p class="muted" style="margin-top:8px">No open positions</p>'

    rows = ""
    for sym, pos in positions.items():
        shares   = float(pos.get("shares", 0))
        avg_cost = float(pos.get("avg_cost", 0))
        rows += f"""
            <tr>
              <td>{sym}</td>
              <td>{shares:,.0f}</td>
              <td>${avg_cost:,.2f}</td>
              <td class="muted">—</td>
            </tr>"""

    return f"""
          <table>
            <thead>
              <tr>
                <th>Symbol</th><th>Shares</th><th>Avg Cost</th><th>Current P&amp;L</th>
              </tr>
            </thead>
            <tbody>{rows}
            </tbody>
          </table>"""


def _stock_section(state: dict | None) -> str:
    if state is None:
        return """
        <div class="bot-card offline">
          <div class="offline-label">Stock bot offline</div>
          <div class="offline-sub">stock_bot/paper_state.json not found or unreadable</div>
        </div>"""

    cash         = float(state.get("cash", 0))
    realized_pnl = float(state.get("realized_pnl", 0))
    last_updated = _fmt_ts(state.get("last_updated"))
    positions    = state.get("positions", {})

    return f"""
        <div class="bot-card">
          <div class="bot-header">
            <span class="bot-title">Stock Bot</span>
            <span class="bot-symbol">Paper</span>
          </div>
          <div class="kv-grid">
            <div class="kv-row"><span class="kv-key">Cash</span>
              <span class="kv-val">${cash:,.2f}</span></div>
            <div class="kv-row"><span class="kv-key">Realized P&amp;L</span>
              <span class="kv-val">{_pnl_span(realized_pnl)}</span></div>
            <div class="kv-row"><span class="kv-key">Open positions</span>
              <span class="kv-val">{len(positions)}</span></div>
            <div class="kv-row"><span class="kv-key">Last updated</span>
              <span class="kv-val muted">{last_updated}</span></div>
          </div>
          {_positions_table(positions)}
          <a class="dash-link" href="{STOCK_DASHBOARD}">Open full dashboard →</a>
        </div>"""


def _combined_section(crypto: dict | None, stock: dict | None) -> str:
    crypto_cash    = float(crypto.get("cash", 0))          if crypto else 0.0
    crypto_pos     = float(crypto.get("position", 0))      if crypto else 0.0
    crypto_basis   = float(crypto.get("cost_basis", 0))    if crypto else 0.0
    crypto_pnl     = float(crypto.get("realized_pnl", 0))  if crypto else 0.0
    crypto_fees    = float(crypto.get("fees_paid", 0))      if crypto else 0.0

    stock_cash     = float(stock.get("cash", 0))           if stock  else 0.0
    stock_pnl      = float(stock.get("realized_pnl", 0))   if stock  else 0.0

    # Proxy for crypto position value: use total cost_basis if holding
    crypto_pos_value = crypto_basis if crypto_pos > 0 else 0.0
    total_value      = crypto_cash + crypto_pos_value + stock_cash
    total_pnl        = crypto_pnl + stock_pnl
    total_fees       = crypto_fees

    crypto_note = " (cost basis proxy)" if crypto_pos > 0 else ""

    return f"""
        <div class="summary-card">
          <div class="summary-title">Combined Summary</div>
          <div class="summary-grid">
            <div class="summary-item">
              <div class="summary-label">Total Paper Value</div>
              <div class="summary-value">${total_value:,.2f}<span class="muted" style="font-size:11px"> {crypto_note}</span></div>
            </div>
            <div class="summary-item">
              <div class="summary-label">Total Realized P&amp;L</div>
              <div class="summary-value">{_pnl_span(total_pnl)}</div>
            </div>
            <div class="summary-item">
              <div class="summary-label">Total Fees Paid</div>
              <div class="summary-value">${total_fees:.4f}</div>
            </div>
          </div>
        </div>"""


# ── HTML assembler ────────────────────────────────────────────────────────────

_CSS = """
    *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
    body {
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", monospace;
      background: #0d1117; color: #e6edf3; font-size: 13px;
      padding: 24px 20px; min-height: 100vh;
    }
    a { color: #58a6ff; text-decoration: none; }
    a:hover { text-decoration: underline; }

    /* Header */
    .page-header {
      display: flex; align-items: baseline; justify-content: space-between;
      flex-wrap: wrap; gap: 8px; margin-bottom: 28px;
      border-bottom: 1px solid #30363d; padding-bottom: 16px;
    }
    .page-title  { font-size: 20px; font-weight: 700; color: #e6edf3; }
    .page-sub    { font-size: 12px; color: #8b949e; margin-top: 3px; }
    .page-ts     { font-size: 12px; color: #8b949e; }

    /* Two-column bot grid */
    .bots-grid {
      display: grid;
      grid-template-columns: 1fr 1fr;
      gap: 20px; margin-bottom: 20px;
    }
    @media (max-width: 720px) {
      .bots-grid { grid-template-columns: 1fr; }
    }

    /* Bot card */
    .bot-card {
      background: #161b22; border: 1px solid #30363d;
      border-radius: 10px; padding: 20px;
      display: flex; flex-direction: column; gap: 14px;
    }
    .bot-header {
      display: flex; align-items: center; justify-content: space-between;
    }
    .bot-title  { font-size: 14px; font-weight: 700; color: #e6edf3;
                   text-transform: uppercase; letter-spacing: .05em; }
    .bot-symbol { font-size: 12px; font-weight: 600; color: #58a6ff;
                   background: #1f2937; padding: 2px 8px; border-radius: 10px; }

    /* Key-value grid */
    .kv-grid { display: flex; flex-direction: column; gap: 6px; }
    .kv-row  { display: flex; justify-content: space-between; align-items: baseline;
                border-bottom: 1px solid #21262d; padding-bottom: 5px; }
    .kv-row:last-child { border-bottom: none; padding-bottom: 0; }
    .kv-key  { font-size: 12px; color: #8b949e; }
    .kv-val  { font-size: 13px; font-weight: 600; color: #e6edf3; text-align: right; }

    /* Offline card */
    .offline { border-color: #30363d; justify-content: center; align-items: center;
                min-height: 120px; opacity: 0.6; }
    .offline-label { font-size: 15px; font-weight: 600; color: #8b949e; }
    .offline-sub   { font-size: 11px; color: #6e7681; margin-top: 4px; }

    /* Positions table */
    table { width: 100%; border-collapse: collapse; margin-top: 4px; }
    th {
      text-align: left; font-size: 11px; color: #8b949e;
      text-transform: uppercase; letter-spacing: .04em;
      padding: 5px 8px; border-bottom: 1px solid #30363d;
    }
    td { padding: 6px 8px; font-size: 12px; border-bottom: 1px solid #21262d; }
    tr:last-child td { border-bottom: none; }

    /* Dashboard link */
    .dash-link {
      font-size: 12px; color: #58a6ff; margin-top: auto;
      padding-top: 4px;
    }

    /* Combined summary */
    .summary-card {
      background: #161b22; border: 1px solid #30363d;
      border-radius: 10px; padding: 20px;
    }
    .summary-title {
      font-size: 12px; font-weight: 700; color: #8b949e;
      text-transform: uppercase; letter-spacing: .05em; margin-bottom: 16px;
    }
    .summary-grid {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(160px, 1fr));
      gap: 16px;
    }
    .summary-item  { }
    .summary-label { font-size: 11px; color: #8b949e; text-transform: uppercase;
                      letter-spacing: .04em; margin-bottom: 4px; }
    .summary-value { font-size: 22px; font-weight: 700; color: #e6edf3; }

    /* Colours */
    .pos   { color: #3fb950; }
    .neg   { color: #f85149; }
    .muted { color: #8b949e; }
"""


def _build_html(crypto: dict | None, stock: dict | None) -> str:
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta http-equiv="refresh" content="{REFRESH_INTERVAL_S}">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>APEX TRADER — Unified Dashboard</title>
  <style>{_CSS}
  </style>
</head>
<body>

  <div class="page-header">
    <div>
      <div class="page-title">APEX TRADER</div>
      <div class="page-sub">Unified Dashboard</div>
    </div>
    <div class="page-ts">Updated: {now} &nbsp;·&nbsp; auto-refresh {REFRESH_INTERVAL_S}s</div>
  </div>

  <div class="bots-grid">
    {_crypto_section(crypto)}
    {_stock_section(stock)}
  </div>

  {_combined_section(crypto, stock)}

</body>
</html>"""


# ── Entry point ───────────────────────────────────────────────────────────────

def generate() -> None:
    crypto = _load_json(CRYPTO_STATE_PATH)
    stock  = _load_json(STOCK_STATE_PATH)
    html   = _build_html(crypto, stock)
    Path(OUTPUT_PATH).write_text(html, encoding="utf-8")
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"Dashboard updated: {ts}")


def main() -> None:
    watch = "--watch" in sys.argv
    if watch:
        print(f"Watching — regenerating every {REFRESH_INTERVAL_S}s. Ctrl+C to stop.")
        try:
            while True:
                generate()
                time.sleep(REFRESH_INTERVAL_S)
        except KeyboardInterrupt:
            print("\nStopped.")
    else:
        generate()


if __name__ == "__main__":
    main()
