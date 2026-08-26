"""
HTML dashboard renderer.

Writes a single self-contained dashboard.html after every tick, combining
ALL live crypto symbols onto one page (added 2026-08-26, when SOL/CAD joined
BTC/CAD live — the page used to be single-symbol only, and SOL/CAD had zero
visibility on it despite holding a real position). One shared page shell
(title/style/exchange header) wraps one full content block per symbol,
stacked in order.
The page auto-refreshes at the configured interval — open once, stays current.

write_multi() is the primary public function (multi-symbol, current call
site: bot/main.py). write() is a thin single-symbol convenience wrapper
around it (symbols=[one dict]) — kept for any future single-symbol caller,
not used by the live bot today.
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

def write_multi(
    path:            str,
    exchange:        str,
    strategy:        str,
    tick:            int,
    symbols:         list[dict],   # one dict per symbol — see _render_symbol_block() for keys
    refresh_s:       int   = 30,
    live_trading:    bool  = False,
    dry_run:         bool  = False,
) -> None:
    """Render ALL symbols onto one page. `symbols` is a list of dicts, each
    with the same per-symbol keys write()'s single-symbol signature used to
    take directly: symbol, price, signal, rsi, trend, state, cooldown,
    last_trade, cash, position, avg_entry, unrealized_pnl, realized_pnl,
    total_value, fills, tick_log, candle_log, stop_loss_pct, take_profit_pct,
    fees_paid, rsi_filter_enabled, volume_k. Order in the list is the order
    rendered on the page — callers control symbol ordering (e.g. active
    symbol first)."""
    html = _render_page(
        exchange=exchange, strategy=strategy, tick=tick,
        refresh_s=refresh_s, live_trading=live_trading, dry_run=dry_run,
        symbols=symbols,
    )
    os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(html)


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
    fills:           list[dict],       # [{time, side, qty, price, total, pnl}]
    tick_log:        list[dict],       # [{tick, time, price, signal, rsi, trend, state, reason}]
    candle_log:      list[dict] = None,# [{ts, close, rsi, adx, trend, spread, signal, action, reason}]
    refresh_s:       int   = 30,
    live_trading:    bool  = False,
    dry_run:         bool  = False,
    stop_loss_pct:      float = 0.0,
    take_profit_pct:    float = 0.0,
    fees_paid:          float = 0.0,
    rsi_filter_enabled: bool  = True,
    volume_k:           float = 0.0,
) -> None:
    """Single-symbol convenience wrapper around write_multi() — not used by
    the live bot (which always has >=1 symbol via write_multi()), kept for
    any future single-symbol caller."""
    write_multi(
        path=path, exchange=exchange, strategy=strategy, tick=tick,
        refresh_s=refresh_s, live_trading=live_trading, dry_run=dry_run,
        symbols=[{
            "symbol": symbol, "price": price, "signal": signal, "rsi": rsi,
            "trend": trend, "state": state, "cooldown": cooldown,
            "last_trade": last_trade, "cash": cash, "position": position,
            "avg_entry": avg_entry, "unrealized_pnl": unrealized_pnl,
            "realized_pnl": realized_pnl, "total_value": total_value,
            "fills": fills, "tick_log": tick_log, "candle_log": candle_log or [],
            "stop_loss_pct": stop_loss_pct, "take_profit_pct": take_profit_pct,
            "fees_paid": fees_paid, "rsi_filter_enabled": rsi_filter_enabled,
            "volume_k": volume_k,
        }],
    )


# ---------------------------------------------------------------------------
# Helpers
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
    if r is None: return "#8b949e"
    if r > 70:    return "#f85149"
    if r < 30:    return "#3fb950"
    return "#e3b341"


def _pct_color(pct: float, warn: float = 1.0, danger: float = 0.5) -> str:
    """Green when far, yellow when within warn%, red when within danger%."""
    if pct <= danger: return "#f85149"
    if pct <= warn:   return "#d29922"
    return "#3fb950"


def _render_symbol_block(**kw) -> str:
    """One symbol's full content block: mini-header, position-protection
    panel, metric cards, state/indicator/regime row, candle table, fills
    table, tick log table. No <html>/<head>/<body> — that's shared page
    shell, built once by _render_page() regardless of symbol count."""
    rsi_str   = f"{kw['rsi']:.1f}" if kw['rsi'] is not None else "—"
    trend_str = kw['trend'] or "—"
    pos_str   = f"{kw['position']:.6f}" if kw['position'] > 0 else "—"
    entry_str = f"${kw['avg_entry']:,.2f}" if kw['avg_entry'] > 0 else "—"
    rsi_col   = _rsi_color(kw['rsi'])
    trend_col = _trend_color(kw['trend'])
    cd_str    = f"({kw['cooldown']} left)" if kw['cooldown'] > 0 else ""

    # ── Position protection panel ──────────────────────────────────────────
    pos_panel = ""
    if kw['position'] > 0 and kw['avg_entry'] > 0:
        entry     = kw['avg_entry']
        price     = kw['price']
        sl_pct    = kw['stop_loss_pct']
        tp_pct    = kw['take_profit_pct']

        if sl_pct > 0 and tp_pct > 0:
            sl_level = entry * (1 - sl_pct)
            tp_level = entry * (1 + tp_pct)

            pct_above_sl = (price - sl_level) / price * 100
            pct_below_tp = (tp_level - price) / price * 100

            sl_col = _pct_color(pct_above_sl, warn=1.0, danger=0.5)

            # Progress bar: where is price between SL and TP?
            total_range  = tp_level - sl_level
            bar_fill_pct = max(0.0, min(100.0, (price - sl_level) / total_range * 100))
            entry_mark   = max(0.0, min(100.0, (entry - sl_level) / total_range * 100))

            # Bar color: red zone (left), green zone (right), entry marker
            pos_panel = f"""
  <!-- Position protection panel -->
  <div class="section" style="margin-bottom:20px;border-color:#30363d">
    <h2>Open Position — Protection Levels</h2>
    <div style="display:grid;grid-template-columns:1fr 1fr 1fr;gap:12px;margin-bottom:16px">

      <div class="info-card">
        <div class="info-row">
          <span class="info-key">Entry</span>
          <span class="info-val">${entry:,.2f}</span>
        </div>
        <div class="info-row">
          <span class="info-key">Stop-loss</span>
          <span class="info-val" style="color:#f85149">${sl_level:,.2f}</span>
        </div>
        <div class="info-row">
          <span class="info-key">Take-profit</span>
          <span class="info-val" style="color:#3fb950">${tp_level:,.2f}</span>
        </div>
        <div class="info-row">
          <span class="info-key">Current</span>
          <span class="info-val" style="color:#e6edf3">${price:,.2f}</span>
        </div>
        <div class="info-row">
          <span class="info-key">Move from entry</span>
          <span class="info-val" style="color:{'#3fb950' if price >= entry else '#f85149'}">{(price - entry) / entry * 100:+.2f}%</span>
        </div>
      </div>

      <div class="info-card" style="border-color:{sl_col}66">
        <div class="info-row">
          <span class="info-key">Stop Loss ({sl_pct*100:.0f}%)</span>
          <span class="info-val">${sl_level:,.2f}</span>
        </div>
        <div class="info-row">
          <span class="info-key">Distance to SL</span>
          <span class="info-val" style="color:{sl_col}">{pct_above_sl:+.2f}%</span>
        </div>
        <div class="info-row">
          <span class="info-key">Trigger below</span>
          <span style="color:#8b949e;font-size:11px">${sl_level:,.2f}</span>
        </div>
      </div>

      <div class="info-card" style="border-color:#3fb95066">
        <div class="info-row">
          <span class="info-key">Take Profit ({tp_pct*100:.0f}%)</span>
          <span class="info-val">${tp_level:,.2f}</span>
        </div>
        <div class="info-row">
          <span class="info-key">Distance to TP</span>
          <span class="info-val" style="color:#3fb950">{pct_below_tp:+.2f}%</span>
        </div>
        <div class="info-row">
          <span class="info-key">Trigger above</span>
          <span style="color:#8b949e;font-size:11px">${tp_level:,.2f}</span>
        </div>
      </div>

    </div>
    <!-- SL → price → TP bar -->
    <div style="position:relative;height:20px;border-radius:4px;overflow:hidden;background:#21262d">
      <!-- SL zone: left portion in red tint -->
      <div style="position:absolute;left:0;top:0;height:100%;width:{entry_mark:.1f}%;background:linear-gradient(to right,#f8514922,#f8514911)"></div>
      <!-- TP zone: right portion in green tint -->
      <div style="position:absolute;left:{entry_mark:.1f}%;top:0;height:100%;width:{100-entry_mark:.1f}%;background:linear-gradient(to right,#3fb95011,#3fb95022)"></div>
      <!-- Current price marker -->
      <div style="position:absolute;left:calc({bar_fill_pct:.1f}% - 2px);top:0;width:4px;height:100%;background:#e6edf3;border-radius:2px"></div>
      <!-- Entry marker -->
      <div style="position:absolute;left:calc({entry_mark:.1f}% - 1px);top:0;width:2px;height:100%;background:#8b949e66"></div>
    </div>
    <div style="display:flex;justify-content:space-between;margin-top:4px;font-size:10px;color:#8b949e">
      <span>SL ${sl_level:,.0f}</span>
      <span style="color:#e6edf3">▲ current ${price:,.0f}</span>
      <span>TP ${tp_level:,.0f}</span>
    </div>
  </div>"""

    # ── Regime stats from candle_log ───────────────────────────────────────
    candle_log = kw['candle_log']
    n_candles  = len(candle_log)
    last_adx   = candle_log[-1]['adx']    if candle_log else None
    last_spread= candle_log[-1]['spread'] if candle_log else None

    if n_candles > 0:
        reasons = [c.get('reason', '') or '' for c in candle_log]
        n_adx    = sum(1 for r in reasons if r.startswith('ADX'))
        n_spread = sum(1 for r in reasons if 'EMA spread' in r)
        n_rsi    = sum(1 for r in reasons if r.startswith('RSI'))
        n_trend  = sum(1 for r in reasons if 'trend' in r)
        n_action = sum(1 for c in candle_log if c.get('action', 'HOLD') not in ('HOLD',))
        adx_pct    = n_adx    / n_candles * 100
        spread_pct = n_spread / n_candles * 100
        action_pct = n_action / n_candles * 100

        adx_str    = f"{last_adx:.1f}" if last_adx is not None else "—"
        spread_str = f"{last_spread:.3f}%" if last_spread is not None else "—"
        adx_col    = "#3fb950" if last_adx and last_adx >= 20 else "#d29922" if last_adx else "#8b949e"
        spread_col = "#f85149" if last_spread and last_spread > 0.8 else "#d29922" if last_spread and last_spread > 0.4 else "#3fb950"

        regime_card = f"""
    <div class="info-card">
      <div style="font-size:11px;color:#8b949e;text-transform:uppercase;letter-spacing:.05em;margin-bottom:6px">
        Regime ({n_candles} candles)
      </div>
      <div class="info-row">
        <span class="info-key">ADX</span>
        <span class="info-val" style="color:{adx_col}">{adx_str}</span>
      </div>
      <div class="info-row">
        <span class="info-key">EMA Spread</span>
        <span class="info-val" style="color:{spread_col}">{spread_str}</span>
      </div>
      <div class="info-row">
        <span class="info-key">ADX filtered</span>
        <span style="color:#8b949e;font-size:12px">{adx_pct:.0f}%</span>
      </div>
      <div class="info-row">
        <span class="info-key">Spread filtered</span>
        <span style="color:#8b949e;font-size:12px">{spread_pct:.0f}%</span>
      </div>
      <div class="info-row">
        <span class="info-key">Actionable</span>
        <span style="color:{'#3fb950' if action_pct > 10 else '#8b949e'};font-size:12px">{action_pct:.0f}%</span>
      </div>
      <div class="info-row">
        <span class="info-key">Volume filter</span>
        <span style="color:{'#3fb950' if kw['volume_k'] > 0 else '#8b949e'};font-size:12px">{'k=' + f"{kw['volume_k']:.1f}" if kw['volume_k'] > 0 else 'OFF'}</span>
      </div>
    </div>"""
    else:
        adx_str = last_spread_str = "—"
        regime_card = f"""
    <div class="info-card">
      <div style="font-size:11px;color:#8b949e;text-transform:uppercase;letter-spacing:.05em;margin-bottom:6px">Regime</div>
      <div style="color:#8b949e;font-size:12px">No candle data yet</div>
      <div class="info-row" style="margin-top:6px">
        <span class="info-key">Volume filter</span>
        <span style="color:{'#3fb950' if kw['volume_k'] > 0 else '#8b949e'};font-size:12px">{'k=' + f"{kw['volume_k']:.1f}" if kw['volume_k'] > 0 else 'OFF'}</span>
      </div>
    </div>"""

    # ── Fees / P&L ─────────────────────────────────────────────────────────
    fees_paid   = kw['fees_paid']
    realized    = kw['realized_pnl']
    net_pnl     = realized - fees_paid
    fees_str    = f'<span style="color:#f85149">-${fees_paid:,.4f}</span>' if fees_paid > 0 else '<span style="color:#8b949e">$0.00</span>'
    net_str     = _fmt_pnl(net_pnl)

    # ── Fills table rows ───────────────────────────────────────────────────
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

    # ── Candle evaluations table (last 10) ─────────────────────────────────
    if candle_log:
        candle_rows = ""
        for c in reversed(list(candle_log)[-10:]):
            rsi_c    = _rsi_color(c.get('rsi'))
            rsi_disp = f'<span style="color:{rsi_c}">{c["rsi"]}</span>' if c.get('rsi') is not None else "—"
            adx_disp = f'{c["adx"]}' if c.get('adx') is not None else "—"
            tr_col   = _trend_color(c.get('trend'))
            sp_val   = c.get('spread', 0)
            sp_col   = "#f85149" if sp_val > 0.8 else "#d29922" if sp_val > 0.4 else "#8b949e"
            act      = c.get('action', '—')
            act_col  = "#3fb950" if act == "BUY" else "#f85149" if act == "SELL" else "#8b949e"
            reason_disp = f'<span style="color:#8b949e;font-size:10px">{c.get("reason","")}</span>' if c.get("reason") else ""
            candle_rows += f"""
            <tr>
              <td style="color:#8b949e;font-size:11px">{c['ts']} UTC</td>
              <td style="color:#e6edf3">${c['close']:,.2f}</td>
              <td>{rsi_disp}</td>
              <td style="color:#8b949e">{adx_disp}</td>
              <td style="color:{tr_col}">{c.get('trend','—')}</td>
              <td style="color:{sp_col}">{sp_val:.3f}%</td>
              <td>{_signal_badge(c.get('signal','HOLD'))}</td>
              <td style="color:{act_col};font-weight:600">{act}{reason_disp}</td>
            </tr>"""
        candle_section = f"""
  <div class="section">
    <h2>Last {min(10, len(candle_log))} Candle Evaluations</h2>
    <table>
      <thead>
        <tr>
          <th>Candle Close</th><th>Price</th><th>RSI</th><th>ADX</th>
          <th>Trend</th><th>Spread</th><th>Signal</th><th>Action / Reason</th>
        </tr>
      </thead>
      <tbody>{candle_rows}</tbody>
    </table>
  </div>"""
    else:
        candle_section = ""

    # ── Tick log rows ──────────────────────────────────────────────────────
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

    return f"""
  <!-- ═══════════════ {kw['symbol']} ═══════════════ -->
  <div class="symbol-block">

  <!-- Symbol header -->
  <div class="header" style="margin-bottom:16px">
    <div>
      <div class="header-title" style="font-size:20px">{kw['symbol']}</div>
      <div class="header-sub">{len(kw['fills'])} fill(s) today</div>
    </div>
    <div style="text-align:right">
      <div style="font-size:26px;font-weight:700;color:#e6edf3">${kw['price']:,.2f}</div>
      <div style="font-size:11px;color:#8b949e;margin-top:2px">Last price</div>
    </div>
  </div>

  {pos_panel}

  <!-- Metric cards -->
  <div class="cards">
    <div class="card">
      <div class="card-label">Cash</div>
      <div class="card-value">${kw['cash']:,.2f}</div>
    </div>
    <div class="card">
      <div class="card-label">Position</div>
      <div class="card-value" style="font-size:18px">{pos_str}</div>
      <div class="card-sub">avg entry {entry_str}</div>
    </div>
    <div class="card">
      <div class="card-label">Unrealized P&amp;L</div>
      <div class="card-value" style="font-size:18px">{_fmt_pnl(kw['unrealized_pnl'])}</div>
    </div>
    <div class="card">
      <div class="card-label">Realized P&amp;L</div>
      <div class="card-value" style="font-size:18px">{_fmt_pnl(realized)}</div>
      <div class="card-sub">fees {fees_str} &nbsp;·&nbsp; net {net_str}</div>
    </div>
    <div class="card">
      <div class="card-label">Total Value</div>
      <div class="card-value">${kw['total_value']:,.2f}</div>
    </div>
  </div>

  <!-- State / indicators / regime row -->
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
        <span class="info-key">Trend</span>
        <span class="info-val" style="color:{trend_col}">{trend_str}</span>
      </div>
      <div class="info-row">
        <span class="info-key">ADX</span>
        <span class="info-val" style="color:{'#3fb950' if last_adx and last_adx >= 20 else '#d29922' if last_adx else '#8b949e'}">{f"{last_adx:.1f}" if last_adx is not None else "—"}</span>
      </div>
      <div class="info-row">
        <span class="info-key">EMA Spread</span>
        <span class="info-val" style="color:{'#f85149' if last_spread and last_spread > 0.8 else '#d29922' if last_spread and last_spread > 0.4 else '#8b949e'}">{f"{last_spread:.3f}%" if last_spread is not None else "—"}</span>
      </div>
      <div class="info-row">
        <span class="info-key">RSI filter</span>
        <span class="info-val" style="color:{'#3fb950' if kw['rsi_filter_enabled'] else '#d29922'}">{'ON' if kw['rsi_filter_enabled'] else 'OFF'}</span>
      </div>
      <div class="info-row">
        <span class="info-key">Fills today</span>
        <span class="info-val">{len(kw['fills'])}</span>
      </div>
    </div>
    {regime_card}
  </div>

  {candle_section}

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

  <!-- Recent ticks -->
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

  </div><!-- /symbol-block -->"""


def _render_page(*, exchange, strategy, tick, refresh_s, live_trading, dry_run, symbols: list[dict]) -> str:
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    symbol_names = ", ".join(s["symbol"] for s in symbols) or "—"

    # ── Mode badge (bot-wide, not per-symbol) ───────────────────────────────
    if live_trading and not dry_run:
        mode_badge = '<span class="badge" style="background:#f8514933;color:#f85149;border:1px solid #f8514966;font-size:13px">● LIVE</span>'
        mode_text  = "Live Trading"
        pulse_cls  = " pulse-red"
    elif dry_run:
        mode_badge = '<span class="badge" style="background:#d2992233;color:#d29922;border:1px solid #d2992266;font-size:13px">◌ DRY RUN</span>'
        mode_text  = "Dry Run"
        pulse_cls  = ""
    else:
        mode_badge = '<span class="badge" style="background:#8b949e22;color:#8b949e;border:1px solid #8b949e44">PAPER</span>'
        mode_text  = "Paper Trading"
        pulse_cls  = ""

    symbol_blocks = "".join(_render_symbol_block(**s) for s in symbols)

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta http-equiv="refresh" content="{refresh_s}">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>Trade Bot — {symbol_names}</title>
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
      grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));
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
      grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
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
    .pulse-red {{ background: #f85149; }}
    @keyframes pulse {{
      0%, 100% {{ opacity: 1; }}
      50%       {{ opacity: 0.3; }}
    }}
    .symbol-block {{ margin-bottom: 40px; padding-bottom: 8px; border-bottom: 2px solid #21262d; }}
    .symbol-block:last-of-type {{ border-bottom: none; margin-bottom: 0; }}
  </style>
</head>
<body>

  <!-- Shared page header (bot-wide, not per-symbol) -->
  <div class="header">
    <div>
      <div class="header-title">
        <span class="pulse{pulse_cls}"></span>
        {exchange.capitalize()} &nbsp;·&nbsp; {len(symbols)} symbol{'s' if len(symbols) != 1 else ''}
        &nbsp;&nbsp;{mode_badge}
      </div>
      <div class="header-sub">{mode_text} &nbsp;·&nbsp; Strategy: {strategy} &nbsp;·&nbsp; Tick #{tick:,} &nbsp;·&nbsp; {symbol_names}</div>
    </div>
  </div>

  {symbol_blocks}

  <div class="footer">Auto-refreshes every {refresh_s}s &nbsp;·&nbsp; Last updated {now}</div>

</body>
</html>"""
