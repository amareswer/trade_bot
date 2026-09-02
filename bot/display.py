"""
Console display — ANSI colors, no external dependencies.
"""
from __future__ import annotations
from datetime import datetime, timezone

_builtin_print = print


def print(*args, **kwargs) -> None:  # noqa: A001 — deliberate module-scoped shadow
    """Console output is purely cosmetic — a broken stdout pipe (terminal
    disconnect, `timeout`-killed parent, journald hiccup on a VPS) must never
    crash a live trading bot. The file log handler is independent of this.
    Regression: 2026-09-02 a `BrokenPipeError` from display.warmup()'s print
    took the whole crypto bot down mid-warmup with a FATAL CRASH alert.
    """
    try:
        _builtin_print(*args, **kwargs)
    except (BrokenPipeError, OSError):
        pass

# ANSI codes
_R  = "\033[0m"
_B  = "\033[1m"
_DIM= "\033[2m"
_GR = "\033[92m"
_RD = "\033[91m"
_YL = "\033[93m"
_CY = "\033[96m"
_WH = "\033[97m"
_MG = "\033[95m"
_BL = "\033[94m"


# ── Header ───────────────────────────────────────────────────────────────────

def header(
    exchange:     str,
    symbol:       str,
    cash:         float,
    strategy:     str,
    live_trading: bool  = False,
    dry_run:      bool  = False,
    total_value:  float | None = None,
) -> None:
    if live_trading and not dry_run:
        cash_str = f"${cash:,.2f} cash"
        if total_value is not None and abs(total_value - cash) > 0.01:
            cash_str += f"  |  ${total_value:,.2f} total"
        mode = f"{_RD}{_B}LIVE{_R}  {cash_str}"
    elif dry_run:
        cash_str = f"${cash:,.2f} cash"
        if total_value is not None and abs(total_value - cash) > 0.01:
            cash_str += f"  |  ${total_value:,.2f} total"
        mode = f"{_YL}DRY RUN{_R}  {cash_str}"
    else:
        mode = f"paper ${cash:,.2f}"
    line = (
        f"  {_B}Trade Bot{_R}  ▸  {_CY}{exchange.capitalize()}{_R}"
        f"  ▸  {_B}{_BL}{symbol}{_R}  ▸  strategy={strategy}  ▸  {mode}"
    )
    bar = "─" * 62
    print(f"\n{bar}\n{line}\n{bar}\n")


# ── Warmup ───────────────────────────────────────────────────────────────────

def warmup(tick: int, current: int, total: int, price: float) -> None:
    pbar = _bar(current, total)
    print(
        f"  {_DIM}{_now()}{_R}  #{tick:04d}  {_WH}${price:>12,.2f}{_R}"
        f"  {_DIM}warmup {pbar} {current}/{total}{_R}"
    )


# ── Next candle countdown ─────────────────────────────────────────────────────

def next_candle(price: float, tick_n: int, countdown: str) -> None:
    print(
        f"  {_DIM}{_now()}{_R}  #{tick_n:04d}  {_WH}${price:>12,.2f}{_R}"
        f"  {_DIM}next candle in {countdown}{_R}"
    )


# ── Tick (main per-candle line) ───────────────────────────────────────────────

def tick(
    tick_n:        int,
    price:         float,
    raw_signal:    str,          # strategy signal before filtering
    final_signal:  str,          # signal after state machine + AI
    rsi:           float | None,
    trend:         str   | None,
    filter_reason: str   = "",   # why signal was downgraded (state machine)
    block_reason:  str   = "",   # why risk blocked (risk manager)
) -> None:
    rsi_label   = f"RSI {_rsi_color(rsi)}{rsi:.1f}{_R}" if rsi is not None else ""
    trend_label = f"{_trend_color(trend)}{trend}{_R}"    if trend           else ""
    indicators  = "  ".join(x for x in [rsi_label, trend_label] if x)

    # Show raw → final when signal was filtered down
    if raw_signal != final_signal and raw_signal != "HOLD":
        sig_display = f"{_DIM}{raw_signal}{_R} → {_signal(final_signal)}"
    else:
        sig_display = _signal(final_signal)

    reason = ""
    if filter_reason:
        reason = f"  {_DIM}[{filter_reason}]{_R}"
    elif block_reason:
        reason = f"  {_YL}⚠ {block_reason}{_R}"

    print(
        f"  {_DIM}{_now()}{_R}  #{tick_n:04d}  {_WH}${price:>12,.2f}{_R}"
        f"  {indicators}  →  {sig_display}{reason}"
    )


# ── State line (printed after every tick) ────────────────────────────────────

def state_line(
    state:      str,
    cooldown:   int,
    last_trade: str,
) -> None:
    state_col = {
        "IDLE":     _DIM,
        "LONG":     _GR,
        "COOLDOWN": _YL,
    }.get(state, _WH)

    cd_str = f"  cooldown {_YL}{cooldown}{_R} remaining" if cooldown > 0 else ""
    print(
        f"  {_DIM}{'':>8}{_R}  "
        f"STATE {state_col}{_B}{state}{_R}"
        f"{cd_str}"
        f"  last {_DIM}{last_trade}{_R}"
    )


# ── Position line ─────────────────────────────────────────────────────────────

def position_line(
    quantity:       float,
    symbol:         str,
    avg_entry:      float,
    unrealized_pnl: float,
    realized_pnl:   float,
    cash:           float,
) -> None:
    if quantity == 0:
        print(
            f"  {_DIM}{'':>8}{_R}  "
            f"cash {_CY}${cash:>10,.2f}{_R}  "
            f"{_DIM}no open position{_R}  "
            f"realized {_pnl(realized_pnl)}"
        )
    else:
        base = symbol.split("/")[0]
        print(
            f"  {_DIM}{'':>8}{_R}  "
            f"cash {_CY}${cash:>10,.2f}{_R}  "
            f"pos {_WH}{quantity:.4f} {base}{_R}"
            f"  entry {_DIM}${avg_entry:,.2f}{_R}"
            f"  unreal {_pnl(unrealized_pnl)}"
            f"  realized {_pnl(realized_pnl)}"
        )


# ── Fill / Reject ─────────────────────────────────────────────────────────────

def fill(side: str, qty: float, symbol: str, price: float, total: float, pnl: float | None = None) -> None:
    color = _GR if side == "BUY" else _RD
    pnl_str = f"  pnl {_pnl(pnl)}" if pnl is not None else ""
    print(
        f"  {' ':>8}  {_B}{color}▶ {side} FILLED{_R}"
        f"  {qty} {symbol} @ ${price:,.2f}  =  ${total:,.2f}{pnl_str}"
    )


def reject(reason: str) -> None:
    print(f"  {' ':>8}  {_RD}✗ REJECTED{_R}  {_DIM}{reason}{_R}")


# ── AI advice ─────────────────────────────────────────────────────────────────

def ai_advice(signal: str, confidence: float, reasoning: str, latency_ms: float, vetoed: bool) -> None:
    sig_col  = {"BUY": _GR, "SELL": _RD, "HOLD": _DIM}.get(signal, _DIM)
    veto_tag = f"  {_YL}→ vetoed to HOLD{_R}" if vetoed else ""
    print(
        f"  {_DIM}{'':>8}{_R}  {_MG}AI{_R}  "
        f"{sig_col}{signal}{_R}  conf={confidence:.0%}  "
        f"{_DIM}{reasoning}{_R}  {_DIM}({latency_ms:.0f}ms){_R}"
        f"{veto_tag}"
    )


# ── Separator / Footer ────────────────────────────────────────────────────────

def separator() -> None:
    print(f"  {_DIM}{'─' * 58}{_R}")


def stopped(ticks: int, fills: int, rejects: int, pos: float, cash: float, realized_pnl: float) -> None:
    print(f"\n{'─' * 62}")
    print(f"  Bot stopped  │  ticks={ticks}  fills={fills}  rejects={rejects}")
    print(f"  Final: cash=${cash:,.2f}  pos={pos:.4f}  realized_pnl={'+' if realized_pnl >= 0 else ''}${realized_pnl:,.2f}")
    print(f"{'─' * 62}\n")


# ── Internal helpers ──────────────────────────────────────────────────────────

def _now() -> str:
    return datetime.now().strftime("%H:%M:%S")


def _bar(current: int, total: int, width: int = 10) -> str:
    filled = int(width * current / total)
    return f"[{'█' * filled}{'░' * (width - filled)}]"


def _signal(s: str) -> str:
    if s == "BUY":
        return f"{_B}{_GR}BUY {_R}"
    if s == "SELL":
        return f"{_B}{_RD}SELL{_R}"
    return f"{_DIM}HOLD{_R}"


def _rsi_color(rsi: float | None) -> str:
    if rsi is None:   return _DIM
    if rsi > 70:      return _RD
    if rsi < 30:      return _GR
    return _WH


def _trend_color(t: str | None) -> str:
    if t == "BULLISH": return _GR
    if t == "BEARISH": return _RD
    return _DIM


def _pnl(v: float) -> str:
    color = _GR if v >= 0 else _RD
    sign  = "+" if v >= 0 else ""
    return f"{color}{sign}${v:,.2f}{_R}"
