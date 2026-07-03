"""
shadow_signal.py — live-vs-backtest signal fidelity checker.

(a) Fetches the last SHADOW_LOOKBACK closed candles for each UNIVERSE_WHITELIST
    symbol from Kraken, replays them through the same IndicatorStrategy class
    and config the live bot uses, and emits the signal per closed candle.

(b) Compares against CANDLE log lines in logs/trade_bot.log, reporting
    MATCH / MISMATCH per candle (with indicator values on mismatches).

(c) For every fill in trades.db, reports slippage vs the candle-close price
    logged at signal time, and compares effective fee assumptions.

(d) Writes a summary block to logs/shadow_report_<date>.md including the
    strategy hash.

Read-only — no config, whitelist, execution, or strategy changes.

Usage:
    python shadow_signal.py

Env vars:
    SHADOW_LOOKBACK=100        candles to compare (default 100)
    SHADOW_LOG=logs/trade_bot.log
    SHADOW_DB=logs/trades.db
    UNIVERSE_WHITELIST         read from .env (via cfg)
    CANDLE_MINUTES             live timeframe (via cfg; default 240 → 4h)
"""
from __future__ import annotations

import logging
logging.basicConfig(level=logging.WARNING)

import os
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import ccxt

from config import cfg
from bot.strategy.indicator_strategy import IndicatorStrategy, IndicatorConfig
from bot.strategy.threshold_strategy import Signal
from bot.data.historical_feed import Candle
from bot.strategy.fingerprint import compute_strategy_hash

# ── Config ─────────────────────────────────────────────────────────────────────
SHADOW_LOOKBACK = int(os.getenv("SHADOW_LOOKBACK", "100"))
LOG_PATH        = Path(os.getenv("SHADOW_LOG",     "logs/trade_bot.log"))
DB_PATH         = Path(os.getenv("SHADOW_DB",      "logs/trades.db"))

# Live candle timeframe — same mapping as main.py:_minutes_to_timeframe
_TF_MAP = {15:"15m", 30:"30m", 60:"1h", 120:"2h", 240:"4h",
           360:"6h", 480:"8h", 720:"12h", 1440:"1d"}
LIVE_TIMEFRAME = _TF_MAP.get(cfg.exchange.candle_minutes, "4h")

_wl = cfg.universe.universe_whitelist or ""
SYMBOLS     = [s.strip() for s in _wl.split(",") if s.strip()] or [cfg.exchange.symbol]
BACKTEST_FEE= cfg.backtest.fee_pct   # 0.008


# ── Log parser ─────────────────────────────────────────────────────────────────
# Line format:
# 2026-07-02 20:00:06,267 __main__ INFO CANDLE [BTC/CAD] 2026-07-02 20:00 UTC |
#   close=87346.10 RSI=60.7 ADX=31.4 trend=BULLISH spread=0.831% signal=HOLD -> HOLD
_CANDLE_RE = re.compile(
    r"CANDLE \[(?P<sym>[^\]]+)\] (?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}) UTC \| "
    r"close=(?P<close>[\d.]+) RSI=(?P<rsi>[\d.]+|n/a) ADX=(?P<adx>[\d.]+|n/a) "
    r"trend=(?P<trend>\w+) spread=(?P<spread>[\d.]+)% signal=(?P<signal>\w+)"
)

LogEntry = dict  # keys: sym, ts, close, rsi, adx, trend, spread, signal


def parse_log(log_path: Path, symbols: list[str]) -> dict[tuple[str, str], LogEntry]:
    """Return {(sym, ts_str): entry} for all matching CANDLE lines in the log."""
    index: dict[tuple[str, str], LogEntry] = {}
    if not log_path.exists():
        return index
    sym_set = set(symbols)
    with open(log_path, "r", errors="replace") as fh:
        for line in fh:
            if "CANDLE [" not in line:
                continue
            m = _CANDLE_RE.search(line)
            if not m:
                continue
            sym = m.group("sym")
            if sym not in sym_set:
                continue
            entry: LogEntry = {
                "sym":    sym,
                "ts":     m.group("ts"),
                "close":  float(m.group("close")),
                "rsi":    float(m.group("rsi")) if m.group("rsi") != "n/a" else None,
                "adx":    float(m.group("adx")) if m.group("adx") != "n/a" else None,
                "trend":  m.group("trend"),
                "spread": float(m.group("spread")),
                "signal": m.group("signal"),   # raw strategy signal (before ->)
            }
            index[(sym, entry["ts"])] = entry
    return index


# ── Strategy factory — mirrors main.py:_make_strategy() exactly ────────────────

def _make_strategy() -> IndicatorStrategy:
    return IndicatorStrategy(IndicatorConfig(
        rsi_period               = cfg.strategy.rsi_period,
        rsi_oversold             = cfg.strategy.rsi_oversold,
        rsi_overbought           = cfg.strategy.rsi_overbought,
        fast_ema_period          = cfg.strategy.fast_ema_period,
        slow_ema_period          = cfg.strategy.slow_ema_period,
        adx_period               = cfg.strategy.adx_period,
        adx_threshold            = cfg.strategy.adx_threshold,
        adx_max                  = cfg.strategy.adx_max,
        min_ema_spread_pct       = cfg.strategy.min_ema_spread_pct,
        max_ema_spread_pct       = cfg.strategy.max_ema_spread_pct,
        rsi_filter_enabled       = cfg.strategy.rsi_filter_enabled,
        macd_enabled             = cfg.strategy.macd_enabled,
        regime_ema_period        = cfg.strategy.regime_ema_period,
        regime_ema_slope_filter  = cfg.strategy.regime_ema_slope_filter,
        volume_k                 = cfg.strategy.volume_k,
        pullback_rsi_min         = cfg.strategy.pullback_rsi_min,
        pullback_rsi_max         = cfg.strategy.pullback_rsi_max,
        breakout_rsi_min         = cfg.strategy.breakout_rsi_min,
        breakout_rsi_max         = cfg.strategy.breakout_rsi_max,
        breakout_lookback        = cfg.strategy.breakout_lookback,
        max_price_extension_pct  = cfg.strategy.max_price_extension_pct,
        breakout_adx_threshold   = cfg.strategy.breakout_adx_threshold,
        atr_volatile_multiplier  = cfg.strategy.atr_volatile_multiplier,
    ))


# ── Candle fetch + shadow replay ───────────────────────────────────────────────

ShadowRow = dict  # keys: sym, ts, close, shadow_sig, shadow_rsi, shadow_adx, shadow_trend


def shadow_replay(sym: str, exchange) -> list[ShadowRow]:
    """
    Fetch warmup + lookback candles from Kraken, replay through a fresh strategy
    instance, return shadow signal rows for the last SHADOW_LOOKBACK candles.
    """
    strategy = _make_strategy()
    warmup_n = max(strategy._warmup + 100, 250)
    total    = warmup_n + SHADOW_LOOKBACK

    try:
        raw = exchange.fetch_ohlcv(sym, timeframe=LIVE_TIMEFRAME, limit=total)
    except Exception as exc:
        print(f"  [{sym}] fetch error: {exc}")
        return []

    if not raw:
        print(f"  [{sym}] no OHLCV data returned")
        return []

    # Drop the in-progress (last, unclosed) candle if very recent
    now_ms  = datetime.now(timezone.utc).timestamp() * 1000
    tf_ms   = cfg.exchange.candle_minutes * 60 * 1000
    if raw and now_ms - raw[-1][0] < tf_ms:
        raw = raw[:-1]

    candles = [
        Candle(
            timestamp = datetime.fromtimestamp(r[0] / 1000, tz=timezone.utc),
            open      = float(r[1]),
            high      = float(r[2]),
            low       = float(r[3]),
            close     = float(r[4]),
            volume    = float(r[5]) if r[5] else 0.0,
        )
        for r in raw
    ]

    rows: list[ShadowRow] = []
    for i, candle in enumerate(candles):
        sig = strategy.evaluate(candle)
        # Only record the last SHADOW_LOOKBACK candles (after warmup is irrelevant)
        if i >= len(candles) - SHADOW_LOOKBACK:
            ts_str = candle.timestamp.strftime("%Y-%m-%d %H:%M")
            rows.append({
                "sym":          sym,
                "ts":           ts_str,
                "close":        candle.close,
                "shadow_sig":   sig.value,
                "shadow_rsi":   strategy.last_rsi,
                "shadow_adx":   strategy.last_adx,
                "shadow_trend": strategy.last_trend,
            })

    return rows


# ── Fill fidelity ──────────────────────────────────────────────────────────────

FillRow = dict


def load_fills(db_path: Path) -> list[FillRow]:
    """Load all fills from trades.db."""
    if not db_path.exists():
        return []
    try:
        con = sqlite3.connect(db_path)
        con.row_factory = sqlite3.Row
        rows = con.execute(
            "SELECT * FROM fills ORDER BY id"
        ).fetchall()
        con.close()
        return [dict(r) for r in rows]
    except Exception as exc:
        print(f"  trades.db load error: {exc}")
        return []


def _find_signal_candle(
    fill: FillRow,
    log_index: dict[tuple[str, str], LogEntry],
) -> LogEntry | None:
    """
    Find the CANDLE log entry whose candle close triggered the fill.
    For candle-close signals: look for the log entry at or just before fill timestamp.
    For intra-candle exits (trail_stop): find the most recent prior log entry.
    """
    sym = fill["symbol"]
    try:
        fill_dt = datetime.fromisoformat(fill["timestamp"].replace("Z", "+00:00"))
    except Exception:
        return None

    # Search backwards through log entries for same symbol
    best: LogEntry | None = None
    best_dt: datetime | None = None
    for (s, ts_str), entry in log_index.items():
        if s != sym:
            continue
        try:
            entry_dt = datetime.strptime(ts_str, "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc)
        except Exception:
            continue
        if entry_dt <= fill_dt:
            if best_dt is None or entry_dt > best_dt:
                best    = entry
                best_dt = entry_dt
    return best


def fill_fidelity(
    fills: list[FillRow],
    log_index: dict[tuple[str, str], LogEntry],
) -> list[dict]:
    """
    For each fill, compute slippage vs signal-candle close.
    Fee: we can only verify assumptions (actual fee not stored in trades.db).
    """
    out: list[dict] = []
    for fill in fills:
        if not fill.get("price") or not fill.get("quantity"):
            # Zero-qty phantom fills — skip
            if fill.get("quantity", 0) == 0:
                out.append({**fill, "_skip": True, "_skip_reason": "zero-qty fill"})
                continue

        entry = _find_signal_candle(fill, log_index)
        signal_close   = entry["close"] if entry else None
        slippage_pct   = None
        if signal_close and fill.get("price"):
            raw_slip = (fill["price"] - signal_close) / signal_close * 100
            # BUY: positive = paid more than close (bad)
            # SELL: positive = sold above close (good) → invert for consistent sign
            slippage_pct = raw_slip if fill["side"] == "BUY" else -raw_slip

        # Use actual fee from DB if captured; otherwise fall back to assumed rate
        actual_fee_cost = fill.get("fee_cost") or 0.0
        if actual_fee_cost > 0 and fill.get("price") and fill.get("quantity"):
            actual_fee_pct = actual_fee_cost / (fill["price"] * fill["quantity"]) * 100
        else:
            actual_fee_pct = None
        assumed_fee_pct = 0.40 if fill["side"] == "BUY" else 0.80  # maker BUY, taker SELL

        out.append({
            **fill,
            "_skip":            False,
            "_signal_close":    signal_close,
            "_signal_ts":       entry["ts"]  if entry else None,
            "_slippage_pct":    slippage_pct,
            "_assumed_fee":     assumed_fee_pct,
            "_actual_fee_pct":  actual_fee_pct,
            "_backtest_fee":    BACKTEST_FEE * 100,
        })
    return out


# ── Report builder ─────────────────────────────────────────────────────────────

def build_report(
    shadow_rows:  list[ShadowRow],
    log_index:    dict[tuple[str, str], LogEntry],
    fill_details: list[dict],
    strategy_hash: str,
    now_str:       str,
) -> tuple[str, str]:
    """Return (terminal_output, markdown_text)."""
    lines_t: list[str] = []    # terminal
    lines_m: list[str] = []    # markdown

    # ── Signal comparison ──────────────────────────────────────────────────────
    matched = mismatched = log_only = shadow_only = 0
    mismatch_details: list[str]  = []
    comparison_rows:  list[dict] = []

    for row in shadow_rows:
        key   = (row["sym"], row["ts"])
        entry = log_index.get(key)

        if entry is None:
            shadow_only += 1
            comparison_rows.append({**row, "_status": "LOG_MISSING", "_log": None})
            continue

        if entry["signal"] == row["shadow_sig"]:
            matched += 1
            comparison_rows.append({**row, "_status": "MATCH", "_log": entry})
        else:
            mismatched += 1
            sh_rsi = f"{row['shadow_rsi']:.1f}" if row['shadow_rsi'] is not None else "n/a"
            sh_adx = f"{row['shadow_adx']:.1f}" if row['shadow_adx'] is not None else "n/a"
            detail = (
                f"  {row['sym']}  {row['ts']}  close={row['close']:.2f}\n"
                f"    log:    signal={entry['signal']:<4}  RSI={entry['rsi']}  "
                f"ADX={entry['adx']}  trend={entry['trend']}\n"
                f"    shadow: signal={row['shadow_sig']:<4}  "
                f"RSI={sh_rsi}  ADX={sh_adx}  trend={row['shadow_trend'] or 'n/a'}"
            )
            mismatch_details.append(detail)
            comparison_rows.append({**row, "_status": "MISMATCH", "_log": entry})

    for key, entry in log_index.items():
        if not any(r["sym"] == key[0] and r["ts"] == key[1] for r in shadow_rows):
            log_only += 1

    total_comparable = matched + mismatched
    match_rate = (matched / total_comparable * 100) if total_comparable > 0 else None

    # ── Fill fidelity summary ──────────────────────────────────────────────────
    real_fills    = [f for f in fill_details if not f.get("_skip")]
    slip_vals     = [f["_slippage_pct"] for f in real_fills if f.get("_slippage_pct") is not None]
    avg_slip      = sum(slip_vals) / len(slip_vals) if slip_vals else None

    # ── Terminal output ────────────────────────────────────────────────────────
    def tf(x: list[str]) -> None:
        lines_t.extend(x)

    tf(["", f"  ── Shadow Signal Report  [{now_str}] ──"])
    tf([f"  Strategy hash : {strategy_hash}"])
    tf([f"  Symbols       : {', '.join(SYMBOLS)}"])
    tf([f"  Lookback      : {SHADOW_LOOKBACK} candles × {LIVE_TIMEFRAME}  (Kraken)"])
    tf([""])
    tf([f"  Signal comparison"])
    tf([f"  {'─' * 50}"])
    tf([f"  Comparable (shadow + log):  {total_comparable}"])
    tf([f"  Match:                      {matched}"])
    tf([f"  Mismatch:                   {mismatched}"])
    tf([f"  Shadow-only (no log line):  {shadow_only}"])
    tf([f"  Log-only (no shadow):       {log_only}"])
    if match_rate is not None:
        status = "PASS (≥95%)" if match_rate >= 95.0 else "BELOW TARGET (<95%)"
        tf([f"  Match rate:                 {match_rate:.1f}%  {status}"])
    else:
        tf([f"  Match rate:                 N/A (no comparable pairs)"])

    if mismatch_details:
        tf(["", "  Mismatches:"])
        for d in mismatch_details:
            tf([d])

    tf(["", "  Fill fidelity"])
    tf([f"  {'─' * 50}"])
    tf([f"  Total fills in trades.db:   {len(fill_details)}"])
    tf([f"  Real fills (qty > 0):       {len(real_fills)}"])

    if real_fills:
        for f in real_fills:
            slip_s = f"{f['_slippage_pct']:+.3f}%" if f.get("_slippage_pct") is not None else "N/A"
            ref_s  = (f"signal close {f['_signal_close']:.2f} @ {f['_signal_ts']}"
                      if f["_signal_close"] else "no log match")
            fee_s  = (f"actual_fee={f['_actual_fee_pct']:.2f}%"
                      if f.get("_actual_fee_pct") is not None
                      else f"assumed_fee={f['_assumed_fee']:.2f}%")
            tf([f"  fill #{f['id']:>2}: {f['side']:4} {f['symbol']:8} "
                f"{f['quantity']:.6f} @ {f['price']:.2f}  "
                f"slippage={slip_s}  ({ref_s})  "
                f"{fee_s}"])

    if avg_slip is not None:
        tf([f"  Avg slippage (real fills):  {avg_slip:+.3f}%"])
    tf([f"  BACKTEST_FEE_PCT assumed:   {BACKTEST_FEE*100:.2f}%  "
        f"(vs live: ~0.40% maker BUY + 0.80% taker SELL = 1.20% round-trip)"])
    tf([""])

    # ── Markdown ───────────────────────────────────────────────────────────────
    def mf(x: list[str]) -> None:
        lines_m.extend(x)

    mf([f"# Shadow Signal Report — {now_str}", ""])
    mf([f"**Strategy hash:** `{strategy_hash}`  "])
    mf([f"**Symbols:** {', '.join(SYMBOLS)}  "])
    mf([f"**Lookback:** {SHADOW_LOOKBACK} × {LIVE_TIMEFRAME} candles (Kraken live data)  ", ""])

    mf(["## Signal fidelity", ""])
    mf([f"| Metric | Value |", "|--------|-------|"])
    mf([f"| Comparable candles | {total_comparable} |"])
    mf([f"| Match | {matched} |"])
    mf([f"| Mismatch | {mismatched} |"])
    mf([f"| Shadow-only (candle not in log) | {shadow_only} |"])
    mf([f"| Log-only (candle not shadow-replayed) | {log_only} |"])
    if match_rate is not None:
        gate = "✓ PASS (≥95%)" if match_rate >= 95.0 else "✗ BELOW TARGET (<95%)"
        mf([f"| **Match rate** | **{match_rate:.1f}%** {gate} |"])
    else:
        mf([f"| Match rate | N/A |"])
    mf([""])

    if mismatch_details:
        mf(["### Mismatches", ""])
        for d in mismatch_details:
            mf([f"```", d.strip(), "```", ""])
    else:
        mf(["No mismatches found in lookback window.", ""])

    mf(["## Fill fidelity", ""])
    mf([f"BACKTEST_FEE_PCT: **{BACKTEST_FEE*100:.2f}%**  "])
    mf([f"Live fee assumption: 0.40% maker BUY + 0.80% taker SELL = **1.20% round-trip**  "])
    mf([f"Fills with `fee_cost` populated in trades.db show `actual_fee=X.XX%`; "
        f"others show `assumed_fee` as fallback.", ""])

    if real_fills:
        mf(["| # | Side | Symbol | Qty | Price | Signal close | Slippage | Notes |",
            "|---|------|--------|-----|-------|-------------|----------|-------|"])
        for f in real_fills:
            slip_s  = f"{f['_slippage_pct']:+.3f}%" if f.get("_slippage_pct") is not None else "N/A"
            close_s = f"{f['_signal_close']:.2f} ({f['_signal_ts']})" if f["_signal_close"] else "N/A"
            mf([f"| {f['id']} | {f['side']} | {f['symbol']} | {f['quantity']:.6f} "
                f"| {f['price']:.2f} | {close_s} | {slip_s} "
                f"| {f.get('signal_reason') or ''} |"])
        if avg_slip is not None:
            mf(["", f"**Avg slippage (real fills):** {avg_slip:+.3f}%"])
    else:
        mf(["No real fills (qty > 0) in trades.db."])

    mf(["", "## Capital gate evaluation note", ""])
    mf(["The 15-fill capital gate requires **ALL** of the following:"])
    mf(["- Live PF ≥ 1.2 over ≥15 completed round-trips"])
    mf(["- Shadow match rate ≥ 95% (confirms strategy is executing as backtested)"])
    mf(["- Fee and slippage within assumptions (fill price within 0.5% of signal close)"])
    mf(["", "A failing PF with clean signal fidelity (≥95% match, slippage on-spec) means",
        "**variance, not strategy failure** — extend the window rather than demoting.",
        "A failing PF with poor fidelity requires investigation before any decision."])
    mf([""])

    return "\n".join(lines_t), "\n".join(lines_m)


# ── Main ───────────────────────────────────────────────────────────────────────

def main() -> None:
    strategy_hash = compute_strategy_hash()
    now_str       = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    date_tag      = datetime.now(timezone.utc).strftime("%Y%m%d")

    print(f"\n  shadow_signal.py  [{now_str}]")
    print(f"  strategy hash: {strategy_hash}  |  symbols: {', '.join(SYMBOLS)}")
    print(f"  lookback: {SHADOW_LOOKBACK} × {LIVE_TIMEFRAME}  |  log: {LOG_PATH}  |  db: {DB_PATH}")

    # ── Parse log ──────────────────────────────────────────────────────────────
    print(f"\n  Parsing {LOG_PATH} …", flush=True)
    log_index = parse_log(LOG_PATH, SYMBOLS)
    print(f"  {len(log_index)} CANDLE entries found for {SYMBOLS}")

    # ── Shadow replay ──────────────────────────────────────────────────────────
    exchange   = ccxt.kraken({"timeout": 30_000})
    all_shadow: list[ShadowRow] = []

    for sym in SYMBOLS:
        print(f"\n  [{sym}] shadow replay ({SHADOW_LOOKBACK} + warmup candles) …", flush=True)
        rows = shadow_replay(sym, exchange)
        print(f"  [{sym}] {len(rows)} shadow rows generated")
        all_shadow.extend(rows)

    # ── Fill fidelity ──────────────────────────────────────────────────────────
    print(f"\n  Loading fills from {DB_PATH} …", flush=True)
    fills        = load_fills(DB_PATH)
    fill_details = fill_fidelity(fills, log_index)
    print(f"  {len(fills)} fills loaded")

    # ── Build report ───────────────────────────────────────────────────────────
    terminal_out, md_text = build_report(
        all_shadow, log_index, fill_details, strategy_hash, now_str
    )

    print(terminal_out)

    log_dir  = Path("logs")
    log_dir.mkdir(exist_ok=True)
    out_path = log_dir / f"shadow_report_{date_tag}.md"
    out_path.write_text(md_text + "\n")
    print(f"  Report → {out_path}\n")


if __name__ == "__main__":
    main()
