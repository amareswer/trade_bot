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

import json
import sys
import time
from datetime import datetime
from pathlib import Path

CRYPTO_STATE_PATH   = "logs/live_state.json"
STOCK_STATE_PATH    = "stock_bot/paper_state.json"
KRAKEN_HOLDINGS_PATH = "logs/kraken_holdings.json"
OUTPUT_PATH         = "unified_dashboard.html"
REFRESH_S           = 30


# ── State helpers ─────────────────────────────────────────────────────────────

def _load_json(path: str) -> dict | None:
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
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

def _combined_stats(crypto: dict | None, stock: dict | None) -> str:
    crypto_cash  = float(crypto.get("cash", 0))          if crypto else 0.0
    crypto_basis = float(crypto.get("cost_basis", 0))    if crypto else 0.0
    crypto_pos   = float(crypto.get("position", 0))      if crypto else 0.0
    crypto_rpnl  = float(crypto.get("realized_pnl", 0))  if crypto else 0.0
    crypto_fees  = float(crypto.get("fees_paid", 0))     if crypto else 0.0

    stock_cash   = float(stock.get("cash", 0))           if stock else 0.0
    stock_rpnl   = float(stock.get("realized_pnl", 0))   if stock else 0.0
    stock_pos    = stock.get("positions", {})             if stock else {}
    stock_pv     = sum(
        float(p.get("shares", 0)) * float(p.get("avg_cost", 0))
        for p in stock_pos.values()
    )

    crypto_pv  = crypto_basis if crypto_pos > 0 else 0.0
    total      = crypto_cash + crypto_pv + stock_cash + stock_pv
    total_rpnl = crypto_rpnl + stock_rpnl

    rpnl_col = "#3fb950" if total_rpnl >= 0 else "#f85149"
    rpnl_s   = "+" if total_rpnl >= 0 else ""

    return (
        f'<div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));'
        f'gap:12px;margin-bottom:24px">'
        + _stat_block(
            "Combined Capital", f"${total:,.2f}",
            "crypto + stock · crypto uses cost basis as proxy"
        )
        + _stat_block(
            "Realized P&L",
            f'<span style="color:{rpnl_col}">{rpnl_s}${total_rpnl:,.2f}</span>',
            "crypto (live) + stock (paper)"
        )
        + _stat_block(
            "Crypto Fees Paid", f"${crypto_fees:.4f}",
            "Kraken taker + CAD pair surcharge"
        )
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


def _fmt_price(p: float) -> str:
    return f"${p:,.4f}" if p < 1 else f"${p:,.2f}"


def _fmt_balance(b: float) -> str:
    if b < 0.01:
        return f"{b:.6f}"
    if b < 1:
        return f"{b:.5f}"
    return f"{b:,.2f}"


def _kraken_holdings_card(bot_cash: float) -> str:
    holdings = _load_json(KRAKEN_HOLDINGS_PATH)
    if not holdings:
        return ""

    TH = ('style="text-align:left;padding:6px 10px;font-size:10px;color:#8b949e;'
          'font-weight:600;text-transform:uppercase;letter-spacing:.05em;'
          'border-bottom:1px solid #30363d;white-space:nowrap"')
    TD = "padding:7px 10px;border-bottom:1px solid #21262d;font-size:12px;white-space:nowrap"

    rows = ""
    total_value = 0.0

    for asset, info in holdings.items():
        balance   = float(info.get("balance", 0))
        avg_price = float(info.get("avg_price", 0))
        cost_val  = balance * avg_price
        current   = _fetch_kraken_price_raw(asset)

        if current is not None:
            cur_val  = balance * current
            pnl      = cur_val - cost_val
            pnl_pct  = pnl / cost_val * 100 if cost_val else 0.0
            pnl_col  = "#3fb950" if pnl >= 0 else "#f85149"
            pnl_s    = "+" if pnl >= 0 else ""
            cur_str  = _fmt_price(current)
            val_str  = f"${cur_val:,.2f}"
            pnl_str  = f'<span style="color:{pnl_col}">{pnl_s}${pnl:,.2f}</span>'
            pct_str  = f'<span style="color:{pnl_col}">{pnl_s}{pnl_pct:.1f}%</span>'
            total_value += cur_val
        else:
            cur_str = "—"
            val_str = f"${cost_val:,.2f}"
            pnl_str = "—"
            pct_str = "—"
            total_value += cost_val

        rows += (
            f"<tr>"
            f'<td style="{TD};color:#c9d1d9"><strong>{asset}</strong></td>'
            f'<td style="{TD};color:#c9d1d9">{_fmt_balance(balance)}</td>'
            f'<td style="{TD};color:#c9d1d9">{_fmt_price(avg_price)}</td>'
            f'<td style="{TD};color:#c9d1d9">{cur_str}</td>'
            f'<td style="{TD};color:#c9d1d9">{val_str}</td>'
            f'<td style="{TD}">{pnl_str}</td>'
            f'<td style="{TD}">{pct_str}</td>'
            f"</tr>"
        )

    total_combined = bot_cash + total_value

    return (
        '<div style="margin-top:14px;background:#0d1117;border:1px solid #30363d;'
        'border-radius:6px;overflow:hidden;overflow-x:auto">'
        '<div style="padding:8px 12px;border-bottom:1px solid #30363d;font-size:10px;'
        'color:#8b949e;text-transform:uppercase;letter-spacing:.05em;font-weight:600">'
        'Kraken Holdings'
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
        f'Bot Cash: <strong style="color:#e6edf3">${bot_cash:,.2f}</strong>'
        f' + Holdings: <strong style="color:#e6edf3">${total_value:,.2f}</strong>'
        f' = Total: <strong style="color:#3fb950">${total_combined:,.2f}</strong>'
        '</div>'
        '</div>'
    )


def _crypto_card(state: dict | None) -> str:
    if state is None:
        return (
            '<div class="pf-card offline">'
            '<div>⚡ Crypto bot offline</div>'
            '<div style="font-size:11px;color:#8b949e;margin-top:4px">'
            'logs/live_state.json not found</div>'
            '</div>'
        )

    symbol   = state.get("symbol", "—")
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

    holding_row = _kv("Position", f"{position:.6f} {base}") if position > 0 else _kv("Position", "Flat")
    basis_row   = _kv("Cost basis", f"${basis:,.2f}") if position > 0 else ""

    return (
        '<div class="pf-card">'
        '<div class="pf-card-header">'
        '<span class="pf-card-title">⚡ Crypto Bot</span>'
        f'<span class="pf-card-badge" style="background:#1f6feb22;color:#58a6ff;border-color:#1f6feb55">LIVE · {symbol}</span>'
        '</div>'
        + _kv("Live Price", f"<strong>{live_price}</strong>")
        + _signals_section(read_live_signals())
        + _kraken_holdings_card(cash)
        + _kv("Cash", f"${cash:,.2f} CAD")
        + holding_row
        + basis_row
        + _kv("Realized P&L", _pnl(rpnl))
        + _kv("Fees paid", f"${fees:.4f}")
        + _kv("Total value", f"${total:,.2f}")
        + _kv("Last saved", saved)
        + "</div>"
    )


def _stock_card(state: dict | None) -> str:
    if state is None:
        return (
            '<div class="pf-card offline">'
            '<div>📈 Stock bot offline</div>'
            '<div style="font-size:11px;color:#8b949e;margin-top:4px">'
            'stock_bot/paper_state.json not found</div>'
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

    return (
        '<div class="pf-card">'
        '<div class="pf-card-header">'
        '<span class="pf-card-title">📈 Stock Bot</span>'
        '<span class="pf-card-badge" style="background:#7c8cf822;color:#7c8cf8;border-color:#7c8cf855">PAPER</span>'
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

    rows = ""
    for sym, pos in positions.items():
        shares   = float(pos.get("shares", 0))
        avg_cost = float(pos.get("avg_cost", 0))
        rows += (
            f"<tr>"
            f"<td {td}><strong>{sym}</strong></td>"
            f"<td {td}>{shares:,.0f}</td>"
            f"<td {td}>${avg_cost:,.2f}</td>"
            f'<td {td} style="color:#8b949e">—</td>'
            f"</tr>"
        )

    return (
        '<div style="background:#161b22;border:1px solid #30363d;border-radius:8px;'
        'overflow:hidden;overflow-x:auto;margin-top:16px">'
        '<div style="padding:10px 14px;border-bottom:1px solid #30363d;font-size:11px;'
        'color:#8b949e;text-transform:uppercase;letter-spacing:.05em;font-weight:600">'
        "📦 Stock Paper Positions"
        "</div>"
        '<table style="width:100%;border-collapse:collapse">'
        "<thead><tr>"
        f"<th {th}>Symbol</th>"
        f"<th {th}>Shares</th>"
        f"<th {th}>Avg Cost</th>"
        f"<th {th}>Live P&L</th>"
        f"</tr></thead>"
        f"<tbody>{rows}</tbody>"
        "</table>"
        "</div>"
    )


def _portfolio_tab_html(crypto: dict | None, stock: dict | None) -> str:
    return (
        '<div style="padding:24px 20px;max-width:1000px;margin:0 auto">'
        '<div style="margin-bottom:24px">'
        '<div style="font-size:18px;font-weight:700;color:#e6edf3;margin-bottom:4px">'
        "Portfolio Overview"
        "</div>"
        '<div style="font-size:12px;color:#8b949e">'
        "Crypto live (Kraken) · Stocks paper ($1,000 account)"
        "</div>"
        "</div>"
        + _combined_stats(crypto, stock)
        + '<div style="display:grid;grid-template-columns:1fr 1fr;gap:16px;margin-bottom:24px">'
        + _crypto_card(crypto)
        + _stock_card(stock)
        + "</div>"
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

def _build_html(crypto: dict | None, stock: dict | None) -> str:
    now       = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    portfolio = _portfolio_tab_html(crypto, stock)

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
    crypto = _load_json(CRYPTO_STATE_PATH)
    stock  = _load_json(STOCK_STATE_PATH)
    html   = _build_html(crypto, stock)
    Path(OUTPUT_PATH).write_text(html, encoding="utf-8")
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"[{ts}] unified_dashboard.html written")


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
