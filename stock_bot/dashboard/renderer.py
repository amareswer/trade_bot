"""
Stock bot HTML dashboard renderer — Phase 4 / 4.5 / 5.

Writes stock_dashboard.html to the repo root after every scan cycle.
Pure inline CSS, dark theme, vanilla JS countdown. No external dependencies.

Public API:
    ScanResult         — dataclass holding all per-symbol data for one cycle
    DashboardRenderer  — render(scan_results, fear_greed, portfolio, alerts) → writes the file
"""
from __future__ import annotations

import html
import logging
import os
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from stock_bot.ai.verdict          import AIVerdict
from stock_bot.data.price_feed     import get_sector
from stock_bot.research.aggregator import ResearchReport
from stock_bot.research.fear_greed import FearGreedData
from stock_bot.portfolio.tracker   import PortfolioPosition, PortfolioSummary, PaperSummary, PaperTrade
from stock_bot.alerts.alert        import Alert

logger = logging.getLogger(__name__)

_OUTPUT_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(__file__))),
    "stock_dashboard.html",
)

# ---------------------------------------------------------------------------
# Colours — GitHub dark palette
# ---------------------------------------------------------------------------
_BG         = "#0d1117"
_CARD_BG    = "#161b22"
_BORDER     = "#30363d"
_BORDER2    = "#21262d"
_TEXT       = "#e6edf3"
_MUTED      = "#8b949e"
_GREEN      = "#3fb950"
_YELLOW     = "#d29922"
_RED        = "#f85149"
_BLUE       = "#58a6ff"

_SIG_COLOR  = {"BUY": "#2ea043", "HOLD": "#6e7681", "SELL": "#f85149"}
_SIG_ICON   = {"BUY": "✅ BUY",  "HOLD": "⏸ HOLD",  "SELL": "❌ SELL"}
_SIG_ORDER  = {"BUY": 0,         "HOLD": 1,          "SELL": 2}

_TREND_ARROW = {"BULLISH": "↑", "BEARISH": "↓", "NEUTRAL": "→"}


# ---------------------------------------------------------------------------
# ScanResult — one per symbol per cycle
# ---------------------------------------------------------------------------

@dataclass
class ScanResult:
    symbol:       str
    company_name: str
    price:        float
    currency:     str                   # "CAD" (.TO) | "USD"
    rsi:          Optional[float]
    trend:        Optional[str]
    macd_note:    Optional[str]         # "bullish cross" | "bearish cross" | "flat"
    research:     ResearchReport
    verdict:      AIVerdict
    source:       str = "watchlist"     # "watchlist" | "universe"
    rule_verdict: object = None         # RuleVerdict — the trade trigger (rule mode)


# ---------------------------------------------------------------------------
# Inline helpers
# ---------------------------------------------------------------------------

def _e(text: str) -> str:
    """HTML-escape user-facing text to prevent XSS / broken HTML."""
    return html.escape(str(text))


def _rsi_color(rsi: Optional[float]) -> str:
    if rsi is None:
        return _MUTED
    if rsi < 30:
        return _GREEN    # oversold = opportunity
    if rsi > 70:
        return _RED      # overbought = caution
    return _YELLOW


def _conf_color(conf: int) -> str:
    if conf >= 70:
        return _GREEN
    if conf >= 50:
        return _YELLOW
    return "#6e7681"


def _is_ai_pending(verdict: AIVerdict) -> bool:
    """True when AI hasn't produced a real signal — confidence 0 + internal status reason."""
    return verdict.confidence == 0 and verdict.reasoning.startswith("AI ")


def _fg_label(score: int) -> str:
    if score <= 25:  return "Extreme Fear"
    if score <= 45:  return "Fear"
    if score <= 55:  return "Neutral"
    if score <= 75:  return "Greed"
    return "Extreme Greed"


def _fg_color(score: int) -> str:
    if score <= 25:  return _RED
    if score <= 45:  return "#e3623a"
    if score <= 55:  return _YELLOW
    if score <= 75:  return "#6fba4a"
    return _GREEN


# ---------------------------------------------------------------------------
# Section builders
# ---------------------------------------------------------------------------

def _css() -> str:
    return """
    *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
    body {
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", monospace;
      background: #0d1117; color: #e6edf3; font-size: 13px;
      padding: 16px 20px; max-width: 1400px; margin: 0 auto;
    }
    h2 {
      font-size: 11px; color: #8b949e; font-weight: 600;
      text-transform: uppercase; letter-spacing: .06em; margin-bottom: 10px;
    }
    a { color: #58a6ff; text-decoration: none; }
    .header {
      display: flex; justify-content: space-between; align-items: flex-start;
      margin-bottom: 14px; flex-wrap: wrap; gap: 8px;
    }
    .title { font-size: 20px; font-weight: 700; }
    .meta  { font-size: 11px; color: #8b949e; text-align: right; line-height: 1.7; }
    .fg-section { margin-bottom: 20px; }
    .fg-bar-wrap {
      position: relative; height: 14px; border-radius: 7px; overflow: visible;
      background: linear-gradient(to right, #f85149, #d29922 50%, #3fb950);
      margin-bottom: 6px;
    }
    .fg-marker {
      position: absolute; top: 50%; transform: translate(-50%, -50%);
      width: 20px; height: 20px; border-radius: 50%;
      background: #e6edf3; border: 3px solid #0d1117;
      box-shadow: 0 0 0 2px #8b949e;
    }
    .fg-labels {
      display: flex; justify-content: space-between;
      font-size: 10px; color: #8b949e;
    }
    .summary-grid {
      display: grid; grid-template-columns: repeat(3, 1fr);
      gap: 12px; margin-bottom: 20px;
    }
    .summary-card {
      background: #161b22; border: 1px solid #30363d;
      border-radius: 8px; padding: 14px 16px;
    }
    .summary-label  { font-size: 11px; color: #8b949e; margin-bottom: 4px; }
    .summary-count  { font-size: 30px; font-weight: 700; line-height: 1; margin-bottom: 6px; }
    .summary-syms   { font-size: 11px; color: #8b949e; }
    .top-picks-section { margin-bottom: 20px; }
    .picks-scroll {
      display: flex; gap: 8px; overflow-x: auto; padding-bottom: 6px;
    }
    .picks-scroll::-webkit-scrollbar { height: 4px; }
    .picks-scroll::-webkit-scrollbar-thumb { background: #30363d; border-radius: 2px; }
    .pick-pill {
      display: inline-flex; align-items: center; gap: 8px;
      background: #161b22; border: 1px solid #2ea043;
      border-radius: 20px; padding: 5px 14px;
      font-size: 12px; white-space: nowrap; cursor: default;
    }
    .pick-sym  { font-weight: 700; color: #e6edf3; }
    .pick-conf { color: #3fb950; font-weight: 600; }
    .pick-style{ color: #8b949e; font-size: 10px; }
    .no-picks  { color: #8b949e; font-size: 12px; font-style: italic; }
    .section-header {
      padding: 16px 20px 12px; margin: 24px 0 12px;
      border-radius: 8px; display: flex; flex-direction: column; gap: 4px;
    }
    .section-header.watchlist { background: #1c2333; }
    .section-header.movers    { background: #1c2820; }
    .section-title {
      display: flex; align-items: center; gap: 10px;
      font-size: 18px; font-weight: 600; color: #e6edf3;
    }
    .section-badge {
      font-size: 12px; font-weight: 400; color: #8b949e;
      background: #21262d; padding: 2px 8px; border-radius: 12px;
    }
    .section-sub { font-size: 12px; color: #8b949e; margin-left: 28px; }
    .cards-grid {
      display: grid;
      grid-template-columns: repeat(auto-fill, minmax(360px, 1fr));
      gap: 16px; margin-bottom: 24px;
    }
    .stock-card {
      background: #161b22; border: 1px solid #30363d;
      border-radius: 10px; overflow: hidden;
    }
    .watchlist-card { border-left: 3px solid #388bfd !important; }
    .mover-card     { border-left: 3px solid #2ea043 !important; }
    .screened-card {
      background: #161b22; border: 1px solid #21262d;
      border-radius: 6px; padding: 10px 16px; color: #8b949e;
      font-size: 13px; display: flex; align-items: center; gap: 12px;
    }
    .card-header {
      display: flex; justify-content: space-between; align-items: flex-start;
      padding: 12px 14px 8px; border-bottom: 1px solid #21262d;
    }
    .card-sym   { font-size: 17px; font-weight: 700; color: #e6edf3; }
    .card-co    { font-size: 11px; color: #8b949e; margin-top: 2px; }
    .card-price { text-align: right; }
    .card-price-val { font-size: 16px; font-weight: 700; color: #e6edf3; }
    .card-price-cur { font-size: 10px; color: #8b949e; margin-top: 1px; }
    .sig-badge {
      display: inline-block; padding: 3px 10px; border-radius: 12px;
      font-size: 11px; font-weight: 700; letter-spacing: .04em; margin-bottom: 4px;
    }
    .tech-row {
      display: flex; gap: 0; padding: 8px 14px;
      border-bottom: 1px solid #21262d; flex-wrap: wrap;
    }
    .tech-item {
      display: flex; align-items: center; gap: 4px;
      font-size: 12px; padding: 2px 10px 2px 0;
    }
    .tech-key { color: #8b949e; }
    .card-section { padding: 10px 14px; border-bottom: 1px solid #21262d; }
    .card-section:last-child { border-bottom: none; }
    .section-label {
      font-size: 10px; color: #8b949e; text-transform: uppercase;
      letter-spacing: .05em; margin-bottom: 6px; font-weight: 600;
    }
    .news-item {
      display: flex; gap: 6px; font-size: 11px; color: #8b949e;
      margin-bottom: 3px; line-height: 1.4;
    }
    .news-dot { color: #30363d; flex-shrink: 0; }
    .news-text { color: #c9d1d9; }
    .meta-row { font-size: 11px; color: #8b949e; margin-bottom: 3px; }
    .meta-row span { color: #c9d1d9; }
    .verdict-sig-row {
      display: flex; align-items: center; gap: 10px;
      margin-bottom: 8px;
    }
    .verdict-sig-text { font-size: 15px; font-weight: 700; }
    .conf-bar-wrap {
      flex: 1; height: 8px; background: #21262d;
      border-radius: 4px; overflow: hidden;
    }
    .conf-bar { height: 100%; border-radius: 4px; }
    .conf-pct { font-size: 13px; font-weight: 600; min-width: 36px; text-align: right; }
    .verdict-prices {
      display: flex; gap: 16px; font-size: 11px; margin-bottom: 8px;
    }
    .verdict-price-item { display: flex; gap: 4px; }
    .verdict-price-key { color: #8b949e; }
    .verdict-reasoning {
      font-size: 11px; color: #8b949e; line-height: 1.5;
      border-left: 2px solid #30363d; padding-left: 8px;
      font-style: italic;
    }
    footer {
      text-align: center; font-size: 11px; color: #484f58;
      padding: 12px 0; border-top: 1px solid #21262d; margin-top: 8px;
    }
    .portfolio-section { margin-bottom: 20px; }
    .portfolio-summary-bar {
      display: flex; gap: 24px; flex-wrap: wrap;
      padding: 12px 16px; background: #161b22;
      border: 1px solid #30363d; border-radius: 8px;
      margin-bottom: 12px;
    }
    .portfolio-summary-item { display: flex; flex-direction: column; gap: 2px; }
    .portfolio-summary-key {
      font-size: 10px; color: #8b949e;
      text-transform: uppercase; letter-spacing: .05em;
    }
    .portfolio-summary-val { font-size: 18px; font-weight: 700; }
    .portfolio-table-wrap {
      background: #161b22; border: 1px solid #30363d;
      border-radius: 8px; overflow: hidden; overflow-x: auto;
    }
    .portfolio-table { width: 100%; border-collapse: collapse; }
    .portfolio-table th {
      text-align: left; padding: 7px 10px;
      color: #8b949e; font-size: 10px; font-weight: 600;
      text-transform: uppercase; letter-spacing: .04em;
      border-bottom: 1px solid #30363d; white-space: nowrap;
    }
    .portfolio-table td {
      padding: 9px 10px; border-bottom: 1px solid #21262d;
      font-size: 12px; color: #c9d1d9; white-space: nowrap;
    }
    .portfolio-table tr:last-child td { border-bottom: none; }
    .portfolio-table tr:hover td { background: #1c2128; }
    .port-tag {
      font-size: 11px; color: #8b949e;
      padding: 5px 14px 6px; border-bottom: 1px solid #21262d;
      background: #0d111740;
    }
    .paper-section { margin-bottom: 20px; }
    .paper-label {
      display: inline-block; font-size: 10px; font-weight: 700;
      padding: 2px 7px; border-radius: 10px; vertical-align: middle;
      background: #2a2d45; color: #7c8cf8; border: 1px solid #3d4175;
      margin-left: 8px; letter-spacing: .04em;
    }
    .paper-summary-bar {
      display: flex; gap: 24px; flex-wrap: wrap;
      padding: 12px 16px; background: #161b22;
      border: 1px solid #3d4175; border-radius: 8px;
      margin-bottom: 12px;
    }
    .paper-summary-item { display: flex; flex-direction: column; gap: 2px; }
    .paper-summary-key {
      font-size: 10px; color: #8b949e;
      text-transform: uppercase; letter-spacing: .05em;
    }
    .paper-summary-val { font-size: 18px; font-weight: 700; }
    .paper-table-wrap {
      background: #161b22; border: 1px solid #3d4175;
      border-radius: 8px; overflow: hidden; overflow-x: auto; margin-bottom: 12px;
    }
    .paper-table { width: 100%; border-collapse: collapse; }
    .paper-table th {
      text-align: left; padding: 7px 10px;
      color: #8b949e; font-size: 10px; font-weight: 600;
      text-transform: uppercase; letter-spacing: .04em;
      border-bottom: 1px solid #3d4175; white-space: nowrap;
    }
    .paper-table td {
      padding: 9px 10px; border-bottom: 1px solid #21262d;
      font-size: 12px; color: #c9d1d9; white-space: nowrap;
    }
    .paper-table tr:last-child td { border-bottom: none; }
    .paper-table tr:hover td { background: #1c2128; }
    .paper-empty { color: #8b949e; font-size: 12px; font-style: italic; padding: 14px 0; text-align: center; }
    .alerts-section { margin-bottom: 20px; }
    .alert-row {
      padding: 10px 14px; border-radius: 6px;
      margin-bottom: 8px; border: 1px solid transparent;
    }
    .alert-row.high   { background: #3d1a1a; border-color: #f8514944; }
    .alert-row.medium { background: #2d2a1a; border-color: #d2992244; }
    .alert-header {
      display: flex; align-items: center; gap: 8px;
      font-size: 12px; font-weight: 700; margin-bottom: 4px;
    }
    .alert-sym   { color: #e6edf3; }
    .alert-type  { color: #8b949e; font-size: 10px; text-transform: uppercase; letter-spacing: .05em; }
    .alert-msg   { font-size: 11px; color: #c9d1d9; line-height: 1.4; }
    .alert-meta  { font-size: 10px; color: #8b949e; margin-top: 3px; }
    @media (max-width: 640px) {
      .summary-grid { grid-template-columns: 1fr; }
      .cards-grid   { grid-template-columns: 1fr; }
      .portfolio-summary-bar { gap: 12px; }
      body { padding: 10px; }
    }
    .gate-panel {
      background: #161b22; border: 1px solid #30363d;
      border-radius: 10px; padding: 16px 20px;
      margin-bottom: 24px;
    }
    .gate-panel-title {
      font-size: 11px; font-weight: 600; color: #8b949e;
      text-transform: uppercase; letter-spacing: .06em;
      margin-bottom: 12px;
    }
    .gate-rows { display: flex; flex-direction: column; gap: 10px; }
    .gate-row {
      display: grid;
      grid-template-columns: 20px 180px 80px 1fr;
      align-items: center; gap: 12px;
      padding: 8px 10px; background: #0d1117;
      border: 1px solid #21262d; border-radius: 6px;
    }
    .gate-num  { font-size: 11px; font-weight: 700; color: #8b949e; text-align: center; }
    .gate-desc { font-size: 12px; color: #e6edf3; }
    .gate-badge {
      display: inline-block; font-size: 10px; font-weight: 700;
      padding: 2px 8px; border-radius: 10px; letter-spacing: .04em;
      text-align: center; white-space: nowrap;
    }
    .gate-extra {
      display: flex; align-items: center; gap: 8px;
      font-size: 11px; color: #8b949e;
    }
    .gate-bar-wrap {
      flex: 1; height: 6px; background: #21262d;
      border-radius: 3px; overflow: hidden; min-width: 60px;
    }
    .gate-bar { height: 100%; border-radius: 3px; transition: width .3s; }
    .gate-bar-label { font-size: 11px; color: #8b949e; white-space: nowrap; }
    .gate-detail    { font-size: 11px; color: #8b949e; }
    .gate-banner {
      margin-top: 14px; padding: 9px 14px; border-radius: 6px;
      font-size: 13px; font-weight: 700; text-align: center;
    }
    .gate-banner-ready { background: #12261e; color: #3fb950; border: 1px solid #2ea04340; }
    .gate-banner-wait  { background: #1f1318; color: #f85149; border: 1px solid #f8514940; }
    @media (max-width: 640px) {
      .gate-row { grid-template-columns: 20px 1fr 70px; }
      .gate-extra { display: none; }
    }
"""


_MODE_META: dict[str, tuple[str, str, str]] = {
    "LIVE":        ("🟢", "LIVE TRADING",  "#3fb950"),
    "PRE_MARKET":  ("🌅", "PRE-MARKET",    "#d29922"),
    "AFTER_HOURS": ("🌙", "AFTER HOURS",   "#58a6ff"),
    "WEEKEND":     ("📅", "WEEKEND",       "#8b949e"),
}


def _header_html(
    now_str:       str,
    loop_interval: int,
    ai_stats:      dict | None = None,
    market_status: dict | None = None,
    loop_mode:     str         = "LIVE",
) -> str:
    import pytz as _tz
    eastern  = _tz.timezone("US/Eastern")
    now_et   = datetime.now(eastern)
    time_str = now_et.strftime("%I:%M%p EST").lstrip("0")

    mode_emoji, mode_label, mode_color = _MODE_META.get(loop_mode, ("🟢", "LIVE TRADING", "#3fb950"))
    mode_badge = (
        f'<span style="display:inline-flex;align-items:center;gap:6px;'
        f'background:{mode_color}18;border:1px solid {mode_color}55;'
        f'border-radius:6px;padding:3px 12px;font-size:13px;font-weight:700;color:{mode_color}">'
        f'{mode_emoji} {mode_label}&nbsp; <span style="font-weight:400;color:#8b949e;font-size:11px">{_e(time_str)}</span>'
        f'</span>'
    )

    ai_line = ""
    if ai_stats:
        nv    = ai_stats.get("nvidia",   0)
        fb    = ai_stats.get("fallback", 0)
        fl    = ai_stats.get("failed",   0)
        total = nv + fb + fl
        if total == 0 or (fl == 0 and fb == 0):
            dot = "🟢"
        elif fl > total // 2:
            dot = "🔴"
        else:
            dot = "🟡"
        parts = [f"{nv}✅"]
        if fb:
            parts.append(f"{fb}⚠️")
        if fl:
            parts.append(f"{fl}❌")
        ai_line = f'<br>🤖 nvidia_nim · {" ".join(parts)}'

    market_bar = ""
    if market_status is not None:
        def _mkt_badge(label: str, is_open: bool, holiday: str | None) -> str:
            if is_open:
                color = "#3fb950"
                status = "OPEN"
            elif holiday:
                color = "#d29922"
                status = _e(holiday)
            else:
                color = "#6e7681"
                status = "CLOSED"
            return (
                f'<span style="display:inline-flex;align-items:center;gap:5px;'
                f'background:#161b22;border:1px solid #30363d;border-radius:6px;'
                f'padding:3px 10px;font-size:11px;">'
                f'<span style="width:8px;height:8px;border-radius:50%;background:{color};display:inline-block"></span>'
                f'<span style="color:#8b949e">{label}</span>'
                f'<span style="color:{color};font-weight:600">{status}</span>'
                f'</span>'
            )
        us_badge = _mkt_badge("NYSE", market_status["us_open"], market_status["us_holiday"])
        ca_badge = _mkt_badge("TSX",  market_status["ca_open"], market_status["ca_holiday"])
        market_bar = f"""
  <div style="display:flex;gap:8px;margin-bottom:14px;flex-wrap:wrap;align-items:center;">
    <span style="font-size:11px;color:#8b949e;margin-right:4px;">Markets:</span>
    {us_badge}
    {ca_badge}
  </div>"""

    return f"""
  <div class="header">
    <div>
      <div class="title">📈 Stock Research Bot</div>
      <div style="margin-top:8px">{mode_badge}</div>
    </div>
    <div class="meta">
      Last updated: {_e(now_str)}<br>
      Next refresh: <span id="countdown">{loop_interval}s</span>{ai_line}
    </div>
  </div>
{market_bar}"""


def _fg_section_html(fg: FearGreedData) -> str:
    score     = max(0, min(100, fg.score))
    label     = _e(fg.label.title()) if fg.label else _fg_label(score)
    fg_col    = _fg_color(score)
    full_label = _fg_label(score)
    return f"""
  <div class="fg-section">
    <h2>🌡 Market Sentiment — CNN Fear &amp; Greed</h2>
    <div class="fg-bar-wrap">
      <div class="fg-marker" style="left:{score}%"></div>
    </div>
    <div class="fg-labels">
      <span>Extreme Fear</span><span>Fear</span>
      <span style="color:{fg_col};font-weight:600">{score} — {full_label}</span>
      <span>Greed</span><span>Extreme Greed</span>
    </div>
  </div>"""


def _rule_summary_html(
    results: list[ScanResult],
    held: Optional[set] = None,
    buy_alloc: Optional[float] = None,
) -> str:
    """The DECIDER strip — rule-based signals that actually trigger trades.
    Only rendered when at least one result carries a rule verdict.
    buy_alloc = current per-trade dollar allocation; a BUY whose share price
    exceeds it will SIZE_SKIP, so the label must not say "buying"."""
    held = held or set()
    with_rules = [r for r in results if r.rule_verdict is not None]
    if not with_rules:
        return ""

    groups: dict[str, list[str]] = {"BUY": [], "HOLD": [], "SELL": []}
    for r in with_rules:
        rv  = r.rule_verdict
        sig = rv.signal if rv.signal in groups else "HOLD"
        label = r.symbol
        if sig == "BUY":
            if buy_alloc is not None and 0 < buy_alloc < r.price:
                label += (
                    f" ⚠ signal valid, can't fill — 1 share ${r.price:,.0f}"
                    f" > ${buy_alloc:,.0f} allocation"
                )
            else:
                label += " → buying"
        elif sig == "SELL":
            label += " → exiting" if r.symbol.upper() in held else " (not held)"
        groups[sig].append(label)

    def box(sig: str, emoji: str) -> str:
        count = len(groups[sig])
        syms  = " · ".join(groups[sig]) if groups[sig] else "—"
        color = _SIG_COLOR.get(sig, _MUTED)
        return f"""
      <div class="summary-card" style="border-color:{color}66">
        <div class="summary-label">{emoji} {sig}</div>
        <div class="summary-count" style="color:{color}">{count}</div>
        <div class="summary-syms">{_e(syms)}</div>
      </div>"""

    return f"""
  <div style="margin:14px 0 4px;font-size:11px;color:#c9d1d9;font-weight:600;
       text-transform:uppercase;letter-spacing:.05em">
    📐 Rule Signals — backtested, these trigger trades
  </div>
  <div class="summary-grid">
    {box("BUY",  "🟢")}
    {box("HOLD", "⏸")}
    {box("SELL", "🔴")}
  </div>"""


def _summary_html(
    results: list[ScanResult],
    held: Optional[set] = None,
    exit_bars: Optional[dict] = None,
) -> str:
    """Signal summary cards. These are AI *opinions*, not orders — each entry
    shows confidence and, for SELL, whether the bot will actually act
    (needs: symbol held AND confidence over the exit bar / streak rule)."""
    held = held or set()
    groups: dict[str, list[str]] = {"BUY": [], "HOLD": [], "SELL": []}
    for r in results:
        sig  = r.verdict.signal if r.verdict.signal in groups else "HOLD"
        conf = r.verdict.confidence
        label = f"{r.symbol} {conf}%"
        if sig == "SELL":
            if r.symbol.upper() not in held:
                label += " (not held)"
            elif exit_bars and conf >= exit_bars.get("sell", 55):
                label += " → exiting"
            else:
                label += " (held · below exit bar)"
        elif sig == "BUY" and exit_bars and conf < exit_bars.get("buy", 65):
            label += " (below buy bar)"
        groups[sig].append(label)

    notes = {"BUY": "", "HOLD": "", "SELL": ""}
    if exit_bars:
        if exit_bars.get("rule_mode"):
            notes["BUY"] = "AI advisory only — BUYs come from backtested rules"
        else:
            notes["BUY"] = f"acts at ≥{exit_bars.get('buy', 65)}%"
        notes["SELL"] = (
            f"acts if held: ≥{exit_bars.get('sell', 55)}% or "
            f"{exit_bars.get('streak_cycles', 2)}×≥{exit_bars.get('streak_conf', 50)}% in a row"
        )

    def box(sig: str, emoji: str) -> str:
        count = len(groups[sig])
        syms  = " · ".join(groups[sig]) if groups[sig] else "—"
        color = _SIG_COLOR.get(sig, _MUTED)
        note  = notes.get(sig, "")
        note_html = (
            f'<div class="summary-syms" style="color:{_MUTED};font-size:10px;'
            f'margin-top:2px">{_e(note)}</div>'
        ) if note else ""
        return f"""
      <div class="summary-card" style="border-color:{color}33">
        <div class="summary-label">{emoji} {sig}</div>
        <div class="summary-count" style="color:{color}">{count}</div>
        <div class="summary-syms">{_e(syms)}</div>
        {note_html}
      </div>"""

    header = ""
    if exit_bars and exit_bars.get("rule_mode"):
        header = (
            '<div style="margin:14px 0 4px;font-size:11px;color:#8b949e;font-weight:600;'
            'text-transform:uppercase;letter-spacing:.05em">'
            '🤖 AI Advisory — opinions only, cannot open positions'
            '</div>'
        )
    return f"""
  {header}
  <div class="summary-grid">
    {box("BUY",  "🟢")}
    {box("HOLD", "⏸")}
    {box("SELL", "🔴")}
  </div>"""


def _top_picks_html(results: list[ScanResult]) -> str:
    picks = sorted(
        [r for r in results if r.verdict.signal == "BUY" and r.verdict.confidence >= 65],
        key=lambda r: -r.verdict.confidence,
    )
    if not picks:
        body = '<span class="no-picks">No strong buy signals this cycle</span>'
    else:
        pills = ""
        for r in picks:
            pills += f"""
        <div class="pick-pill">
          <span class="pick-sym">{_e(r.symbol)}</span>
          <span class="pick-conf">{r.verdict.confidence}%</span>
          <span class="pick-style">{_e(r.verdict.trading_style)}</span>
        </div>"""
        body = f'<div class="picks-scroll">{pills}</div>'

    return f"""
  <div class="top-picks-section">
    <h2>🔥 Top Picks This Cycle</h2>
    {body}
  </div>"""


def _action_hint(pos: PortfolioPosition) -> str:
    if pos.verdict is None:
        return "—"
    sig  = pos.verdict.signal
    gain = pos.gain_loss
    if sig == "BUY":
        return "Consider adding" if gain >= 0 else "Averaging down — caution"
    if sig == "HOLD":
        return "Hold position"
    if sig == "SELL":
        return "Consider taking profit" if gain >= 0 else "Consider cutting loss"
    return "—"


def _portfolio_section_html(summary: PortfolioSummary) -> str:
    gl_col = _GREEN if summary.total_gain_loss > 0 else _RED if summary.total_gain_loss < 0 else _MUTED
    gl_s   = "+" if summary.total_gain_loss >= 0 else ""

    summary_bar = f"""
    <div class="portfolio-summary-bar">
      <div class="portfolio-summary-item">
        <span class="portfolio-summary-key">Invested</span>
        <span class="portfolio-summary-val">${summary.total_invested:,.2f}</span>
      </div>
      <div class="portfolio-summary-item">
        <span class="portfolio-summary-key">Current Value</span>
        <span class="portfolio-summary-val">${summary.total_value:,.2f}</span>
      </div>
      <div class="portfolio-summary-item">
        <span class="portfolio-summary-key">Total P&amp;L</span>
        <span class="portfolio-summary-val" style="color:{gl_col}">{gl_s}${summary.total_gain_loss:,.2f} ({gl_s}{summary.total_gain_loss_pct:.1f}%)</span>
      </div>
    </div>"""

    rows = ""
    for p in summary.positions:
        p_gl_col = _GREEN if p.gain_loss > 0 else _RED if p.gain_loss < 0 else _MUTED
        p_gl_s   = "+" if p.gain_loss >= 0 else ""
        sig      = p.verdict.signal if p.verdict else "—"
        sig_col  = _SIG_COLOR.get(sig, _MUTED)
        action   = _action_hint(p)

        rows += f"""
        <tr>
          <td>
            <strong>{_e(p.symbol)}</strong>
            <span style="color:#8b949e;font-size:10px;margin-left:4px">{_e(p.currency)}</span>
          </td>
          <td>{p.shares:g}</td>
          <td>${p.avg_cost:,.2f}</td>
          <td>${p.current_price:,.2f}</td>
          <td style="color:{p_gl_col}">{p_gl_s}${p.gain_loss:,.2f}</td>
          <td style="color:{p_gl_col};font-weight:600">{p_gl_s}{p.gain_loss_pct:.1f}%</td>
          <td><span style="color:{sig_col};font-weight:600">{_e(sig)}</span></td>
          <td style="color:#8b949e;font-size:11px">{_e(action)}</td>
        </tr>"""

    return f"""
  <div class="portfolio-section">
    <h2>💼 My Portfolio</h2>
    {summary_bar}
    <div class="portfolio-table-wrap">
      <table class="portfolio-table">
        <thead>
          <tr>
            <th>Symbol</th><th>Shares</th><th>Avg Cost</th>
            <th>Current</th><th>P&amp;L $</th><th>P&amp;L %</th>
            <th>AI Signal</th><th>Action</th>
          </tr>
        </thead>
        <tbody>{rows}</tbody>
      </table>
    </div>
  </div>"""


def _stock_card_html(r: ScanResult, pos: Optional[PortfolioPosition] = None, extra_class: str = "") -> str:
    sig       = r.verdict.signal
    sig_color = _SIG_COLOR.get(sig, _MUTED)
    sig_icon  = _SIG_ICON.get(sig, sig)
    conf      = r.verdict.confidence
    conf_col  = _conf_color(conf)
    rsi_col   = _rsi_color(r.rsi)
    trend_arrow = _TREND_ARROW.get(r.trend or "", "—")
    _pending  = _is_ai_pending(r.verdict)

    rsi_str   = f"{r.rsi:.1f}" if r.rsi is not None else "—"
    trend_str = r.trend or "—"
    macd_str  = r.macd_note or "—"

    # ── Header ──────────────────────────────────────────────────
    is_new_ipo  = r.trend == "NEW IPO"
    ipo_badge   = (
        ' <span style="background:#7d3f0022;color:#e8832a;border:1px solid #e8832a55;'
        'border-radius:10px;padding:1px 7px;font-size:10px;font-weight:700;'
        'vertical-align:middle">🆕 NEW IPO</span>'
        if is_new_ipo else ""
    )
    _sig_badge = (
        '<span class="sig-badge" style="background:#21262d;color:#6e7681;border:1px solid #30363d">⏳ AI pending…</span>'
        if _pending else
        f'<span class="sig-badge" style="background:{sig_color}22;color:{sig_color};border:1px solid {sig_color}55">{_e(sig_icon)}</span>'
    )
    card_header = f"""
    <div class="card-header">
      <div>
        <div style="margin-bottom:2px">
          {_sig_badge}
        </div>
        <div class="card-sym">{_e(r.symbol)}{ipo_badge}</div>
        <div class="card-co">{_e(r.company_name)}</div>
      </div>
      <div class="card-price">
        <div class="card-price-val">${r.price:,.2f}</div>
        <div class="card-price-cur">{_e(r.currency)}</div>
      </div>
    </div>"""

    # ── Portfolio tag (shown when symbol is in portfolio) ────────
    if pos is not None:
        pos_gl_col = _GREEN if pos.gain_loss > 0 else _RED if pos.gain_loss < 0 else _MUTED
        pos_gl_s   = "+" if pos.gain_loss >= 0 else ""
        port_tag = (
            f'<div class="port-tag">💼 {pos.shares:g} shares @ ${pos.avg_cost:,.2f}'
            f' &nbsp;·&nbsp; P&amp;L: '
            f'<span style="color:{pos_gl_col};font-weight:600">'
            f'{pos_gl_s}${pos.gain_loss:,.2f} ({pos_gl_s}{pos.gain_loss_pct:.1f}%)'
            f'</span></div>'
        )
    else:
        port_tag = ""

    # ── Rule verdict tag — the trade trigger (rule mode) ─────────
    if r.rule_verdict is not None:
        rv       = r.rule_verdict
        rv_color = _SIG_COLOR.get(rv.signal, _MUTED)
        if rv.signal == "BUY":
            wl_note = " → will buy"
        elif rv.signal == "SELL":
            wl_note = " → exits if held"
        else:
            wl_note = ""
        rule_tag = (
            f'<div class="port-tag" style="border-left:2px solid {rv_color}">'
            f'📐 Rules: <span style="color:{rv_color};font-weight:700">{_e(rv.signal)}</span>'
            f'<span style="color:#8b949e">{_e(wl_note)}</span></div>'
        )
    else:
        rule_tag = ""

    # ── Technicals row ──────────────────────────────────────────
    macd_icon = "↑" if "bullish" in (macd_str or "") else "↓" if "bearish" in (macd_str or "") else "→"
    tech_row = f"""
    <div class="tech-row">
      <div class="tech-item">
        <span class="tech-key">RSI</span>
        <span style="color:{rsi_col};font-weight:600">{_e(rsi_str)}</span>
      </div>
      <div class="tech-item">
        <span class="tech-key">Trend</span>
        <span style="color:{'#3fb950' if trend_str=='BULLISH' else '#f85149' if trend_str=='BEARISH' else '#8b949e'};font-weight:600">{_e(trend_str)} {trend_arrow}</span>
      </div>
      <div class="tech-item">
        <span class="tech-key">MACD</span>
        <span style="color:{'#3fb950' if 'bull' in macd_str else '#f85149' if 'bear' in macd_str else '#8b949e'}">{macd_icon} {_e(macd_str)}</span>
      </div>
    </div>"""

    # ── News ────────────────────────────────────────────────────
    news_items = ""
    if r.research.news:
        for n in r.research.news[:3]:
            news_items += f"""
        <div class="news-item">
          <span class="news-dot">·</span>
          <span class="news-text">{_e(n.title[:90])}</span>
        </div>"""
    else:
        news_items = '<div class="news-item"><span class="news-text" style="color:#484f58">No headlines found</span></div>'

    news_section = f"""
    <div class="card-section">
      <div class="section-label">📰 News</div>
      {news_items}
    </div>"""

    # ── News Sentiment + Market Trends + Earnings ───────────────
    s = r.research.sentiment
    if s.post_count > 0:
        sent_col = _GREEN if s.label == "POSITIVE" else _RED if s.label == "NEGATIVE" else _YELLOW
        sent_str = f'<span style="color:{sent_col};font-weight:600">{_e(s.label)}</span> ({s.score:+.2f}) · {s.post_count} headlines'
    else:
        sent_str = '<span style="color:#484f58">No headlines to score</span>'

    mts        = r.research.market_trends_score if isinstance(r.research.market_trends_score, (int, float)) else 0
    trends_col = _GREEN if mts > 70 else _MUTED
    trends_str = f'<span style="color:{trends_col};font-weight:600">{mts}/100</span>'
    if mts > 70:
        trends_str += ' <span style="color:#e8832a">🔥 high interest</span>'

    e = r.research.earnings
    next_e = e.next_earnings_date.strftime("%b %d") if e.next_earnings_date else "unknown"
    earn_note = _e(e.earnings_note or "No data")

    meta_section = f"""
    <div class="card-section">
      <div class="meta-row">💬 News Sentiment: {sent_str}</div>
      <div class="meta-row">📈 Mkt Interest (7d): {trends_str}</div>
      <div class="meta-row">📅 Earnings: <span>{_e(next_e)}</span> · <span>{earn_note}</span></div>
    </div>"""

    # ── AI Verdict ────────────────────────────────────────────────
    if r.verdict.reasoning and r.verdict.confidence > 0:
        reasoning_html = f'<div class="verdict-reasoning">{_e(r.verdict.reasoning[:200])}</div>'
    elif r.verdict.confidence == 0:
        reasoning_html = f'<div class="verdict-reasoning" style="color:#484f58">{_e(r.verdict.reasoning or "AI unavailable")}</div>'
    else:
        reasoning_html = ""

    price_row = ""
    if r.verdict.target_price is not None or r.verdict.stop_loss is not None:
        parts = []
        if r.verdict.target_price is not None:
            parts.append(f'<div class="verdict-price-item"><span class="verdict-price-key">Target</span><span style="color:#3fb950;font-weight:600">${r.verdict.target_price:,.2f}</span></div>')
        if r.verdict.stop_loss is not None:
            parts.append(f'<div class="verdict-price-item"><span class="verdict-price-key">Stop</span><span style="color:#f85149;font-weight:600">${r.verdict.stop_loss:,.2f}</span></div>')
        price_row = f'<div class="verdict-prices">{"".join(parts)}</div>'

    if _pending:
        verdict_section = f"""
    <div class="card-section" style="background:#0d111799">
      <div class="section-label">🤖 AI Verdict · {_e(r.verdict.trading_style)}</div>
      <div class="verdict-sig-row">
        <span class="verdict-sig-text" style="color:#6e7681">⏳ AI pending…</span>
        <div class="conf-bar-wrap">
          <div class="conf-bar" style="width:0%;background:#30363d"></div>
        </div>
        <span class="conf-pct" style="color:#484f58">—</span>
      </div>
    </div>"""
    else:
        verdict_section = f"""
    <div class="card-section" style="background:#0d111799">
      <div class="section-label">🤖 AI Verdict · {_e(r.verdict.trading_style)}</div>
      <div class="verdict-sig-row">
        <span class="verdict-sig-text" style="color:{sig_color}">{_e(sig_icon)}</span>
        <div class="conf-bar-wrap">
          <div class="conf-bar" style="width:{conf}%;background:{conf_col}"></div>
        </div>
        <span class="conf-pct" style="color:{conf_col}">{conf}%</span>
      </div>
      {price_row}
      {reasoning_html}
    </div>"""

    cls = f"stock-card{' ' + extra_class if extra_class else ''}"
    return f'<div class="{cls}">{card_header}{rule_tag}{port_tag}{tech_row}{news_section}{meta_section}{verdict_section}</div>'


# ---------------------------------------------------------------------------
# Portfolio overview — unified real + paper positions with sector data
# ---------------------------------------------------------------------------

def _portfolio_overview_html(
    portfolio_summary: Optional[PortfolioSummary],
    paper_summary:     Optional[PaperSummary],
) -> str:
    """
    Unified table showing both real and paper positions side-by-side.
    Sector data is fetched live from yfinance (cached per session).
    Only rendered when at least one position exists across either account.
    """
    all_positions: list[dict] = []

    # Real positions — skip when paper_summary has positions (same executor source — avoids duplicates)
    if portfolio_summary and portfolio_summary.positions and not (paper_summary and paper_summary.positions):
        for p in portfolio_summary.positions:
            all_positions.append({
                "symbol":   p.symbol,
                "type":     "REAL",
                "shares":   p.shares,
                "avg_cost": p.avg_cost,
                "current":  p.current_price,
                "pnl":      p.gain_loss,
                "pnl_pct":  p.gain_loss_pct,
                "sector":   get_sector(p.symbol),
            })

    # Paper positions — PaperSummary.positions is also list[PortfolioPosition]
    if paper_summary and paper_summary.positions:
        for p in paper_summary.positions:
            all_positions.append({
                "symbol":   p.symbol,
                "type":     "PAPER",
                "shares":   p.shares,
                "avg_cost": p.avg_cost,
                "current":  p.current_price,
                "pnl":      p.gain_loss,
                "pnl_pct":  p.gain_loss_pct,
                "sector":   get_sector(p.symbol),
            })

    if not all_positions:
        return ""

    # REAL first, then PAPER; alpha within each group
    all_positions.sort(key=lambda x: (0 if x["type"] == "REAL" else 1, x["symbol"]))

    # Sector concentration map across all positions
    sector_counts: dict[str, int] = {}
    for p in all_positions:
        s = p["sector"]
        sector_counts[s] = sector_counts.get(s, 0) + 1

    # ── Table rows ────────────────────────────────────────────────────────────
    _th = (
        'style="text-align:left;padding:7px 10px;color:#8b949e;font-size:10px;'
        'font-weight:600;text-transform:uppercase;letter-spacing:.04em;'
        'border-bottom:1px solid #30363d;white-space:nowrap"'
    )
    _td = (
        'style="padding:9px 10px;border-bottom:1px solid #21262d;'
        'font-size:12px;color:#c9d1d9;white-space:nowrap"'
    )

    rows = ""
    for p in all_positions:
        if p["type"] == "REAL":
            badge = (
                '<span style="background:#1c3a5e;color:#58a6ff;'
                'padding:1px 7px;border-radius:10px;font-size:11px">REAL</span>'
            )
        else:
            badge = (
                '<span style="background:#3a2a00;color:#e3b341;'
                'padding:1px 7px;border-radius:10px;font-size:11px">PAPER</span>'
            )

        current_str = f"${p['current']:,.2f}" if p["current"] is not None else f'<span style="color:{_MUTED}">—</span>'

        if p["pnl"] is not None:
            pnl_col     = _GREEN if p["pnl"] >= 0 else _RED
            pnl_str     = f'<span style="color:{pnl_col}">${p["pnl"]:+.2f}</span>'
            pnl_pct_str = f'<span style="color:{pnl_col}">{p["pnl_pct"]:+.1f}%</span>'
        else:
            pnl_str     = f'<span style="color:{_MUTED}">—</span>'
            pnl_pct_str = f'<span style="color:{_MUTED}">—</span>'

        rows += f"""
        <tr>
          <td {_td}><strong>{_e(p['symbol'])}</strong></td>
          <td {_td} style="padding:9px 10px;border-bottom:1px solid #21262d;font-size:12px;color:{_MUTED};white-space:nowrap">{_e(p['sector'])}</td>
          <td {_td}>{badge}</td>
          <td {_td}>{p['shares']:g}</td>
          <td {_td}>${p['avg_cost']:,.2f}</td>
          <td {_td}>{current_str}</td>
          <td {_td}>{pnl_str}</td>
          <td {_td}>{pnl_pct_str}</td>
        </tr>"""

    # ── Totals footer ─────────────────────────────────────────────────────────
    real_value  = sum(p["current"] * p["shares"] for p in all_positions if p["type"] == "REAL"  and p["current"])
    paper_value = sum(p["current"] * p["shares"] for p in all_positions if p["type"] == "PAPER" and p["current"])
    combined    = real_value + paper_value

    tfoot = f"""
        <tr style="border-top:2px solid #30363d">
          <td colspan="8" style="padding:9px 10px;font-size:12px;color:{_MUTED}">
            Real:&nbsp;<span style="color:#58a6ff;font-weight:600">${real_value:,.2f} CAD</span>
            &nbsp;&nbsp;|&nbsp;&nbsp;
            Paper:&nbsp;<span style="color:#e3b341;font-weight:600">${paper_value:,.2f}</span>
            &nbsp;&nbsp;|&nbsp;&nbsp;
            Combined:&nbsp;<span style="color:{_TEXT};font-weight:600">${combined:,.2f}</span>
          </td>
        </tr>"""

    # ── Sector concentration warnings ─────────────────────────────────────────
    sector_html = ""
    for sector, count in sorted(sector_counts.items(), key=lambda x: -x[1]):
        if count >= 3:
            sector_html += f"""
      <div style="background:#3a2a00;border:1px solid #e3b34144;border-radius:6px;
                  padding:8px 12px;margin:8px 10px 10px;font-size:12px;color:#e3b341">
        ⚠️ High concentration: <strong>{_e(sector)}</strong> — {count} positions across real + paper holdings
      </div>"""
        elif count == 2:
            sector_html += f"""
      <div style="font-size:11px;color:{_MUTED};padding:4px 14px 8px">
        · {_e(sector)}: 2 positions
      </div>"""

    return f"""
  <div style="margin-bottom:20px">
    <h2>🗂 Portfolio Overview</h2>
    <div style="background:{_CARD_BG};border:1px solid {_BORDER};border-radius:8px;overflow:hidden">
      <div style="overflow-x:auto">
        <table style="width:100%;border-collapse:collapse">
          <thead>
            <tr>
              <th {_th}>Symbol</th>
              <th {_th}>Sector</th>
              <th {_th}>Type</th>
              <th {_th}>Shares</th>
              <th {_th}>Avg Cost</th>
              <th {_th}>Current</th>
              <th {_th}>P&amp;L</th>
              <th {_th}>P&amp;L %</th>
            </tr>
          </thead>
          <tbody>{rows}</tbody>
          <tfoot>{tfoot}</tfoot>
        </table>
      </div>{sector_html}
    </div>
  </div>"""


# ---------------------------------------------------------------------------
# Alerts panel
# ---------------------------------------------------------------------------

def _alerts_panel_html(alerts: list[Alert]) -> str:
    if not alerts:
        return ""

    high_first = sorted(alerts, key=lambda a: (0 if a.priority == "HIGH" else 1, a.symbol))
    rows = ""
    for a in high_first:
        css_cls  = "high" if a.priority == "HIGH" else "medium"
        icon     = "🔴" if a.priority == "HIGH" else "🟡"
        pri_str  = f'{icon} {a.priority}'
        time_str = a.timestamp.strftime("%H:%M:%S")
        conf_str = f" · {a.confidence}% conf" if a.confidence is not None else ""
        rows += f"""
    <div class="alert-row {css_cls}">
      <div class="alert-header">
        <span>{pri_str}</span>
        <span class="alert-type">{_e(a.alert_type.value)}</span>
        <span style="flex:1"></span>
        <span class="alert-sym">{_e(a.symbol)}</span>
      </div>
      <div class="alert-msg">{_e(a.message)}</div>
      <div class="alert-meta">${a.price:,.2f} {_e(a.currency)}{conf_str} · {_e(time_str)}</div>
    </div>"""

    return f"""
  <div class="alerts-section">
    <h2>🔔 Active Alerts ({len(alerts)})</h2>
    {rows}
  </div>"""


# ---------------------------------------------------------------------------
# Paper trading section
# ---------------------------------------------------------------------------

def _paper_section_html(paper: PaperSummary) -> str:
    sc   = paper.starting_cash
    tv   = paper.total_value
    ret  = tv - sc
    ret_pct = (ret / sc * 100) if sc else 0.0
    ret_s   = "+" if ret >= 0 else ""
    ret_col = _GREEN if ret >= 0 else _RED

    unr     = paper.unrealized_pnl
    unr_s   = "+" if unr >= 0 else ""
    unr_col = _GREEN if unr >= 0 else _RED

    rea     = paper.realized_pnl
    rea_s   = "+" if rea >= 0 else ""
    rea_col = _GREEN if rea >= 0 else _RED

    # ── Account summary bar ──────────────────────────────────────────────────
    n_pos = len(paper.positions)
    summary_bar = f"""
    <div class="paper-summary-bar">
      <div class="paper-summary-item">
        <span class="paper-summary-key">💵 Cash</span>
        <span class="paper-summary-val">${paper.cash:,.2f}</span>
      </div>
      <div class="paper-summary-item">
        <span class="paper-summary-key">📦 Open Positions</span>
        <span class="paper-summary-val">{n_pos}</span>
      </div>
      <div class="paper-summary-item">
        <span class="paper-summary-key">📈 Unrealized P&amp;L</span>
        <span class="paper-summary-val" style="color:{unr_col}">{unr_s}${unr:,.2f}</span>
      </div>
      <div class="paper-summary-item">
        <span class="paper-summary-key">✅ Realized P&amp;L</span>
        <span class="paper-summary-val" style="color:{rea_col}">{rea_s}${rea:,.2f}</span>
      </div>
      <div class="paper-summary-item">
        <span class="paper-summary-key">💼 Total Value</span>
        <span class="paper-summary-val" style="color:{ret_col}">${tv:,.2f} <span style="font-size:13px">({ret_s}{ret_pct:.2f}%)</span></span>
      </div>
    </div>"""

    # ── Open positions table ─────────────────────────────────────────────────
    if paper.positions:
        pos_rows = ""
        for p in paper.positions:
            gl_col = _GREEN if p.gain_loss > 0 else _RED if p.gain_loss < 0 else _MUTED
            gl_s   = "+" if p.gain_loss >= 0 else ""
            sig    = p.verdict.signal if p.verdict else "—"
            sig_col = _SIG_COLOR.get(sig, _MUTED)
            sig_icon = _SIG_ICON.get(sig, sig)
            pos_rows += f"""
          <tr>
            <td><strong>{_e(p.symbol)}</strong>
              <span style="color:#8b949e;font-size:10px;margin-left:4px">{_e(p.currency)}</span>
            </td>
            <td>{p.shares:g}</td>
            <td>${p.avg_cost:,.2f}</td>
            <td>${p.current_price:,.2f}</td>
            <td style="color:{gl_col};font-weight:600">{gl_s}${p.gain_loss:,.2f}</td>
            <td style="color:{gl_col};font-weight:600">{gl_s}{p.gain_loss_pct:.1f}%</td>
            <td><span style="color:{sig_col};font-weight:600">{_e(sig_icon)}</span></td>
          </tr>"""
        positions_html = f"""
    <div class="paper-table-wrap">
      <table class="paper-table">
        <thead><tr>
          <th>Symbol</th><th>Shares</th><th>Avg Cost</th>
          <th>Current</th><th>P&amp;L $</th><th>P&amp;L %</th><th>AI Signal</th>
        </tr></thead>
        <tbody>{pos_rows}</tbody>
      </table>
    </div>"""
    else:
        positions_html = '<div class="paper-empty">No open positions</div>'

    # ── Recent trades table ──────────────────────────────────────────────────
    if paper.recent_trades:
        trade_rows = ""
        for t in paper.recent_trades:
            side_col  = _GREEN if t.side == "BUY" else _RED
            ts        = t.timestamp.split(" ")[1] if " " in t.timestamp else t.timestamp
            trade_rows += f"""
          <tr>
            <td style="color:#8b949e">{_e(ts)}</td>
            <td><strong>{_e(t.symbol)}</strong></td>
            <td style="color:{side_col};font-weight:600">{_e(t.side)}</td>
            <td>{t.shares:g}</td>
            <td>${t.price:,.2f}</td>
            <td>${t.total_value:,.2f}</td>
            <td style="color:#8b949e">{_e(t.reason)}</td>
          </tr>"""
        trades_html = f"""
    <div style="margin-top:6px">
      <div style="font-size:11px;color:#8b949e;font-weight:600;text-transform:uppercase;
                  letter-spacing:.05em;margin-bottom:6px">Recent Trades</div>
      <div class="paper-table-wrap">
        <table class="paper-table">
          <thead><tr>
            <th>Time</th><th>Symbol</th><th>Side</th><th>Shares</th>
            <th>Price</th><th>Value</th><th>Reason</th>
          </tr></thead>
          <tbody>{trade_rows}</tbody>
        </table>
      </div>
    </div>"""
    else:
        trades_html = ""

    # Badge reflects the active executor: sim fills ("VIRTUAL") vs real order
    # routing on the IBKR paper API — the dashboard must show which one is live.
    if os.getenv("STOCK_EXECUTOR", "paper").strip().lower() == "ibkr":
        badge = "IBKR PAPER · real order routing, simulated money"
    else:
        badge = "VIRTUAL"
    return f"""
  <div class="paper-section">
    <h2>📄 Paper Trading <span class="paper-label">{_e(badge)}</span></h2>
    {summary_bar}
    {positions_html}
    {trades_html}
  </div>"""


# ---------------------------------------------------------------------------
# Trading readiness panel
# ---------------------------------------------------------------------------

def _readiness_panel_html(gate_status: Optional[dict]) -> str:
    if not gate_status:
        return ""

    gates      = gate_status.get("gates", [])
    remaining  = gate_status.get("remaining", 4)
    ready      = gate_status.get("ready", False)
    thresholds = gate_status.get("thresholds", {})

    t2_trades = int(thresholds.get("gate2_min_trades",  20))
    t2_win    = float(thresholds.get("gate2_min_win_pct", 50.0))
    t3_pairs  = int(thresholds.get("gate3_min_trades",  5))

    _STATUS_COL = {
        "PASS":    _GREEN,
        "FAIL":    _RED,
        "PENDING": _YELLOW,
        "NOT_RUN": _MUTED,
    }

    def _badge(status: str) -> str:
        col = _STATUS_COL.get(status, _MUTED)
        return (
            f'<span class="gate-badge"'
            f' style="background:{col}18;color:{col};border:1px solid {col}44">'
            f'{_e(status)}</span>'
        )

    def _progress_bar(current: int, total: int, color: str) -> str:
        pct = min(100, round(current / max(total, 1) * 100))
        return (
            f'<div class="gate-bar-wrap">'
            f'<div class="gate-bar" style="width:{pct}%;background:{color}"></div>'
            f'</div>'
            f'<span class="gate-bar-label">{current}/{total}</span>'
        )

    rows_html = ""
    for g in gates:
        gn     = g["gate"]
        desc   = g["description"]
        status = g["status"]
        badge  = _badge(status)
        col    = _STATUS_COL.get(status, _MUTED)

        if gn == 1:
            extra = f'<span class="gate-detail">{_e(g.get("detail", ""))}</span>'

        elif gn == 2:
            tr  = g.get("trades",  0)
            wp  = g.get("win_pct", 0.0)
            bar = _progress_bar(tr, t2_trades, col)
            extra = f'{bar}<span class="gate-detail">{wp:.1f}% win (need {t2_win:.0f}%)</span>'

        elif gn == 3:
            pr  = g.get("pairs", 0)
            bar = _progress_bar(pr, t3_pairs, col)
            extra = bar

        elif gn == 4:
            ai_ok  = g.get("ai_ok",  False)
            tsx_ok = g.get("tsx_ok", False)
            ai_col  = _GREEN if ai_ok  else _RED
            tsx_col = _GREEN if tsx_ok else _RED
            extra = (
                f'<span style="color:{ai_col}">{"✓" if ai_ok else "✗"}</span>'
                f'<span class="gate-detail">AI memory</span>'
                f'&nbsp;&nbsp;'
                f'<span style="color:{tsx_col}">{"✓" if tsx_ok else "✗"}</span>'
                f'<span class="gate-detail">TSX audit</span>'
            )

        else:
            extra = f'<span class="gate-detail">{_e(g.get("detail", ""))}</span>'

        rows_html += f"""
    <div class="gate-row">
      <span class="gate-num">{gn}</span>
      <span class="gate-desc">{_e(desc)}</span>
      {badge}
      <div class="gate-extra">{extra}</div>
    </div>"""

    if ready:
        banner = '<div class="gate-banner gate-banner-ready">&#x1F7E2; READY FOR LIVE TRADING</div>'
    else:
        noun   = "gate" if remaining == 1 else "gates"
        banner = f'<div class="gate-banner gate-banner-wait">&#x1F534; {remaining} {noun} remaining</div>'

    return f"""
  <div class="gate-panel">
    <div class="gate-panel-title">Trading Readiness</div>
    <div class="gate-rows">{rows_html}
    </div>
    {banner}
  </div>"""


# ---------------------------------------------------------------------------
# Main HTML builder
# ---------------------------------------------------------------------------

def _build_html(
    results:       list[ScanResult],
    fear_greed:    FearGreedData,
    loop_interval: int,
    portfolio:     Optional[PortfolioSummary] = None,
    alerts:        Optional[list[Alert]]      = None,
    paper:         Optional[PaperSummary]     = None,
    ai_stats:      Optional[dict]             = None,
    market_status: Optional[dict]             = None,
    loop_mode:     str                        = "LIVE",
    gate_status:   Optional[dict]             = None,
    exit_bars:     Optional[dict]             = None,
    buy_alloc:     Optional[float]            = None,
) -> str:
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    def _sort(lst: list[ScanResult]) -> list[ScanResult]:
        return sorted(lst, key=lambda r: (_SIG_ORDER.get(r.verdict.signal, 1), -r.verdict.confidence))

    watchlist_results = _sort([r for r in results if r.source == "watchlist"])
    universe_results  = _sort([r for r in results if r.source == "universe"])

    # Build position lookup for card tags
    pos_map: dict[str, PortfolioPosition] = {}
    if portfolio:
        for p in portfolio.positions:
            pos_map[p.symbol.upper()] = p

    # ── Watchlist section ────────────────────────────────────────────────────
    watchlist_section_html = ""
    if watchlist_results:
        wl_cards = "\n".join(
            _stock_card_html(r, pos_map.get(r.symbol.upper()), "watchlist-card")
            for r in watchlist_results
        )
        watchlist_section_html = f"""
  <div class="section-header watchlist">
    <div class="section-title">
      <span>📋</span>
      <span>My Watchlist</span>
      <span class="section-badge">{len(watchlist_results)} symbol{'s' if len(watchlist_results) != 1 else ''}</span>
    </div>
    <div class="section-sub">Always scanned every cycle</div>
  </div>
  <div class="cards-grid">
{wl_cards}
  </div>"""

    # ── Universe / Top Movers section ────────────────────────────────────────
    universe_section_html = ""
    if universe_results:
        mv_cards = "\n".join(
            _stock_card_html(r, pos_map.get(r.symbol.upper()), "mover-card")
            for r in universe_results
        )
        universe_section_html = f"""
  <div class="section-header movers">
    <div class="section-title">
      <span>🔥</span>
      <span>Top Movers</span>
      <span class="section-badge">{len(universe_results)} symbols today</span>
    </div>
    <div class="section-sub">S&amp;P 500 + TSX 60 · ranked by volume × momentum</div>
  </div>
  <div class="cards-grid">
{mv_cards}
  </div>"""

    portfolio_section  = _portfolio_section_html(portfolio) if portfolio else ""
    paper_section      = _paper_section_html(paper) if paper else ""
    overview_section   = _portfolio_overview_html(portfolio, paper)
    alerts_section     = _alerts_panel_html(alerts or [])
    readiness_section  = _readiness_panel_html(gate_status)

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta http-equiv="refresh" content="{loop_interval}">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Stock Research Bot</title>
  <style>{_css()}</style>
</head>
<body>

{_header_html(now_str, loop_interval, ai_stats, market_status, loop_mode)}
{_fg_section_html(fear_greed)}
{_rule_summary_html(results, held={p.symbol.upper() for p in paper.positions} if paper else set(), buy_alloc=buy_alloc)}
{_summary_html(results, held={p.symbol.upper() for p in paper.positions} if paper else set(), exit_bars=exit_bars)}
{portfolio_section}
{paper_section}
{overview_section}
{_top_picks_html(results)}
{readiness_section}
{watchlist_section_html}
{universe_section_html}

{alerts_section}

  <footer>
    Data is for informational purposes only. Not financial advice. &nbsp;·&nbsp;
    Stock Bot v0.4.5 · Phase 4.5 &nbsp;·&nbsp; Updated {_e(now_str)}
  </footer>

  <script>
    let s = {loop_interval};
    const el = document.getElementById('countdown');
    const t = setInterval(function() {{
      s--;
      if (s <= 0) {{ el.textContent = '0s'; clearInterval(t); }}
      else {{ el.textContent = s + 's'; }}
    }}, 1000);
  </script>

</body>
</html>"""


# ---------------------------------------------------------------------------
# Public renderer class
# ---------------------------------------------------------------------------

class DashboardRenderer:
    """
    Writes stock_dashboard.html to the repo root after every scan cycle.
    Never called from within a try/except here — caller (main.py) is the guard.
    """

    def __init__(self, loop_interval: int = 60) -> None:
        self.loop_interval = loop_interval

    def render(
        self,
        scan_results:  list[ScanResult],
        fear_greed:    FearGreedData,
        portfolio:     Optional[PortfolioSummary] = None,
        alerts:        Optional[list[Alert]]      = None,
        paper:         Optional[PaperSummary]     = None,
        ai_stats:      Optional[dict]             = None,
        market_status: Optional[dict]             = None,
        loop_mode:     str                        = "LIVE",
        gate_status:   Optional[dict]             = None,
        exit_bars:     Optional[dict]             = None,
        buy_alloc:     Optional[float]            = None,
    ) -> None:
        html_str = _build_html(scan_results, fear_greed, self.loop_interval, portfolio, alerts, paper, ai_stats, market_status, loop_mode, gate_status, exit_bars, buy_alloc)
        os.makedirs(os.path.dirname(os.path.abspath(_OUTPUT_PATH)), exist_ok=True)
        with open(_OUTPUT_PATH, "w", encoding="utf-8") as f:
            f.write(html_str)
        pos_count = len(portfolio.positions) if portfolio else 0
        logger.info(
            "Dashboard written → stock_dashboard.html  (%d symbols, %d portfolio positions)",
            len(scan_results), pos_count,
        )
