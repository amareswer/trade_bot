"""
HTML dashboard renderer.

Writes a single self-contained dashboard.html after every tick.
The page auto-refreshes at the configured interval — open once, stays current.

write() is the only public function.
"""
from __future__ import annotations

import os
from datetime import datetime
from typing import Optional

DEFAULT_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(__file__))),
    "dashboard.html",
)

# ---------------------------------------------------------------------------
# Public
# ---------------------------------------------------------------------------

def write(
    path:            str,
    exchange:        str,
    symbol:          str,
    strategy:        str,
    tick:            int,
    price:           float,
    signal:          str,
    rsi:             Optional[float],
    trend:           Optional[str],
    state:           str,
    cooldown:        int,
    last_trade:      str,
    cash:            float,
    position:        float,
    avg_entry:       float,
    unrealized_pnl:  float,
    realized_pnl:    float,
    total_value:     float,
    fills:           list[dict],   # [{time, side, qty, price, total, pnl}]
    tick_log:        list[dict],   # [{tick, time, price, signal, rsi, trend, state, reason}]
    refresh_s:       int = 30,
) -> None:
    html = _render(
        exchange=exchange, symbol=symbol, strategy=strategy,
        tick=tick, price=price, signal=signal,
        rsi=rsi, trend=trend, state=state,
        cooldown=cooldown, last_trade=last_trade,
        cash=cash, position=position, avg_entry=avg_entry,
        unrealized_pnl=unrealized_pnl, realized_pnl=realized_pnl,
        total_value=total_value,
        fills=fills, tick_log=tick_log, refresh_s=refresh_s,
    )
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(html)


# ---------------------------------------------------------------------------
# Internal
# ---------------------------------------------------------------------------

def _fmt_pnl(v: float) -> str:
    sign  = "+" if v >= 0 else ""
    color = "#3fb950" if v >= 0 else "#f85149"
    return f'<span style="color:{color}">{sign}${v:,.2f}</span>'


def _signal_badge(s: str) -> str:
    colors = {"BUY": "#3fb950", "SELL": "#f85149", "HOLD": "#8b949e"}
    c = colors.get(s, "#8b949e")
    return f'<span class="badge" style="background:{c}22;color:{c};border:1px solid {c}55">{s}</span>'


def _state_badge(s: str) -> str:
    colors = {"IDLE": "#8b949e", "LONG": "#3fb950", "COOLDOWN": "#d29922"}
    c = colors.get(s, "#8b949e")
    return f'<span class="badge" style="background:{c}22;color:{c};border:1px solid {c}55">{s}</span>'


def _trend_color(t: Optional[str]) -> str:
    if t == "BULLISH": return "#3fb950"
    if t == "BEARISH": return "#f85149"
    return "#8b949e"


def _rsi_color(r: Optional[float]) -> str:
    if r is None:  return "#8b949e"
    if r > 70:     return "#f85149"
    if r < 30:     return "#3fb950"
    return "#e3b341"


def _render(**kw) -> str:
    now        = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    rsi_str    = f"{kw['rsi']:.1f}" if kw['rsi'] is not None else "—"
    trend_str  = kw['trend'] or "—"
    pos_str    = f"{kw['position']:.4f}" if kw['position'] > 0 else "—"
    entry_str  = f"${kw['avg_entry']:,.2f}" if kw['avg_entry'] > 0 else "—"
    rsi_col    = _rsi_color(kw['rsi'])
    trend_col  = _trend_color(kw['trend'])
    cd_str     = f"({kw['cooldown']} left)" if kw['cooldown'] > 0 else ""

    # ── Fills table rows ───────────────────────────────────────────────
    if kw['fills']:
        fill_rows = ""
        for f in reversed(kw['fills']):
            side_col = "#3fb950" if f['side'] == "BUY" else "#f85149"
            pnl_cell = _fmt_pnl(f['pnl']) if f.get('pnl') is not None else '<span style="color:#8b949e">—</span>'
            fill_rows += f"""
            <tr>
              <td>{f['time']}</td>
              <td style="color:{side_col};font-weight:600">{f['side']}</td>
              <td>{f['qty']}</td>
              <td>${f['price']:,.2f}</td>
              <td>${f['total']:,.2f}</td>
              <td>{pnl_cell}</td>
            </tr>"""
    else:
        fill_rows = '<tr><td colspan="6" style="color:#8b949e;text-align:center">No fills yet</td></tr>'

    # ── Tick log rows ──────────────────────────────────────────────────
    tick_rows = ""
    for t in reversed(kw['tick_log'][-30:]):
        reason_cell = f'<span style="color:#8b949e;font-size:11px">{t["reason"]}</span>' if t['reason'] else ""
        rsi_c       = _rsi_color(t.get('rsi'))
        rsi_disp    = f'<span style="color:{rsi_c}">{t["rsi"]:.1f}</span>' if t.get('rsi') is not None else "—"
        tick_rows  += f"""
        <tr>
          <td style="color:#8b949e">#{t['tick']:04d}</td>
          <td style="color:#8b949e">{t['time']}</td>
          <td style="color:#e6edf3">${t['price']:>12,.2f}</td>
          <td>{rsi_disp}</td>
          <td style="color:{_trend_color(t.get('trend'))}">{t.get('trend') or '—'}</td>
          <td>{_signal_badge(t['signal'])}</td>
          <td>{_state_badge(t['state'])}</td>
          <td>{reason_cell}</td>
        </tr>"""

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta http-equiv="refresh" content="{kw['refresh_s']}">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>Trade Bot — {kw['symbol']}</title>
  <style>
    *, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}
    body {{
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", monospace;
      background: #0d1117; color: #e6edf3; font-size: 13px;
      padding: 20px;
    }}
    h2 {{ font-size: 14px; color: #8b949e; font-weight: 600;
          text-transform: uppercase; letter-spacing: .05em; margin-bottom: 12px; }}
    .header {{
      display: flex; align-items: center; justify-content: space-between;
      margin-bottom: 24px;
    }}
    .header-title {{ font-size: 18px; font-weight: 700; color: #e6edf3; }}
    .header-sub   {{ font-size: 12px; color: #8b949e; margin-top: 3px; }}
    .badge {{
      display: inline-block; padding: 2px 10px; border-radius: 12px;
      font-size: 12px; font-weight: 700; letter-spacing: .04em;
    }}
    .cards {{
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(160px, 1fr));
      gap: 12px; margin-bottom: 24px;
    }}
    .card {{
      background: #161b22; border: 1px solid #30363d;
      border-radius: 8px; padding: 16px;
    }}
    .card-label {{ font-size: 11px; color: #8b949e; text-transform: uppercase;
                   letter-spacing: .05em; margin-bottom: 6px; }}
    .card-value {{ font-size: 22px; font-weight: 700; color: #e6edf3; }}
    .card-sub   {{ font-size: 11px; color: #8b949e; margin-top: 4px; }}
    .state-row {{
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
      gap: 12px; margin-bottom: 24px;
    }}
    .info-card {{
      background: #161b22; border: 1px solid #30363d;
      border-radius: 8px; padding: 14px 16px;
      display: flex; flex-direction: column; gap: 6px;
    }}
    .info-row {{ display: flex; justify-content: space-between; align-items: center; }}
    .info-key {{ color: #8b949e; font-size: 12px; }}
    .info-val {{ font-size: 13px; font-weight: 600; color: #e6edf3; }}
    .section {{
      background: #161b22; border: 1px solid #30363d;
      border-radius: 8px; padding: 16px; margin-bottom: 20px;
      overflow-x: auto;
    }}
    table {{ width: 100%; border-collapse: collapse; }}
    th {{
      text-align: left; padding: 6px 10px;
      color: #8b949e; font-size: 11px; font-weight: 600;
      text-transform: uppercase; letter-spacing: .04em;
      border-bottom: 1px solid #30363d;
    }}
    td {{
      padding: 7px 10px; border-bottom: 1px solid #21262d;
      font-size: 12px; color: #c9d1d9;
    }}
    tr:last-child td {{ border-bottom: none; }}
    tr:hover td {{ background: #1c2128; }}
    .footer {{ color: #484f58; font-size: 11px; margin-top: 16px; text-align: right; }}
    .pulse {{ display: inline-block; width: 8px; height: 8px; border-radius: 50%;
              background: #3fb950; margin-right: 6px;
              animation: pulse 2s ease-in-out infinite; }}
    @keyframes pulse {{
      0%, 100% {{ opacity: 1; }}
      50%       {{ opacity: 0.3; }}
    }}
  </style>
</head>
<body>

  <!-- Header -->
  <div class="header">
    <div>
      <div class="header-title">
        <span class="pulse"></span>
        {kw['exchange'].capitalize()} &nbsp;·&nbsp; {kw['symbol']}
      </div>
      <div class="header-sub">Paper Trading &nbsp;·&nbsp; Strategy: {kw['strategy']} &nbsp;·&nbsp; Tick #{kw['tick']:,}</div>
    </div>
    <div style="text-align:right">
      <div style="font-size:26px;font-weight:700;color:#e6edf3">${kw['price']:,.2f}</div>
      <div style="font-size:11px;color:#8b949e;margin-top:2px">Last price</div>
    </div>
  </div>

  <!-- Metric cards -->
  <div class="cards">
    <div class="card">
      <div class="card-label">Cash</div>
      <div class="card-value">${kw['cash']:,.0f}</div>
    </div>
    <div class="card">
      <div class="card-label">Position</div>
      <div class="card-value">{pos_str}</div>
      <div class="card-sub">avg entry {entry_str}</div>
    </div>
    <div class="card">
      <div class="card-label">Unrealized P&amp;L</div>
      <div class="card-value">{_fmt_pnl(kw['unrealized_pnl'])}</div>
    </div>
    <div class="card">
      <div class="card-label">Realized P&amp;L</div>
      <div class="card-value">{_fmt_pnl(kw['realized_pnl'])}</div>
    </div>
    <div class="card">
      <div class="card-label">Total Value</div>
      <div class="card-value">${kw['total_value']:,.0f}</div>
    </div>
  </div>

  <!-- State / indicators row -->
  <div class="state-row">
    <div class="info-card">
      <div class="info-row">
        <span class="info-key">State</span>
        <span>{_state_badge(kw['state'])} {cd_str}</span>
      </div>
      <div class="info-row">
        <span class="info-key">Signal</span>
        <span>{_signal_badge(kw['signal'])}</span>
      </div>
      <div class="info-row">
        <span class="info-key">Last trade</span>
        <span class="info-val" style="font-size:11px;color:#8b949e">{kw['last_trade']}</span>
      </div>
    </div>
    <div class="info-card">
      <div class="info-row">
        <span class="info-key">RSI (14)</span>
        <span class="info-val" style="color:{rsi_col}">{rsi_str}</span>
      </div>
      <div class="info-row">
        <span class="info-key">EMA Trend</span>
        <span class="info-val" style="color:{trend_col}">{trend_str}</span>
      </div>
      <div class="info-row">
        <span class="info-key">Fills today</span>
        <span class="info-val">{len(kw['fills'])}</span>
      </div>
    </div>
  </div>

  <!-- Trade history -->
  <div class="section">
    <h2>Trade History</h2>
    <table>
      <thead>
        <tr>
          <th>Time</th><th>Side</th><th>Qty</th>
          <th>Price</th><th>Total</th><th>P&amp;L</th>
        </tr>
      </thead>
      <tbody>{fill_rows}</tbody>
    </table>
  </div>

  <!-- Tick log -->
  <div class="section">
    <h2>Recent Ticks (last 30)</h2>
    <table>
      <thead>
        <tr>
          <th>#</th><th>Time</th><th>Price</th>
          <th>RSI</th><th>Trend</th><th>Signal</th><th>State</th><th>Reason</th>
        </tr>
      </thead>
      <tbody>{tick_rows}</tbody>
    </table>
  </div>

  <div class="footer">Auto-refreshes every {kw['refresh_s']}s &nbsp;·&nbsp; Last updated {now}</div>

</body>
</html>"""
