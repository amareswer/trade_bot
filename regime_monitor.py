"""
regime_monitor.py — BTC/CAD and XRP/CAD live strategy regime health check.

Fetches the last 200 × 1h Kraken candles per symbol and reports four metrics:

  1. Current ADX(14)      — must be ≥ 18 for trend-following to have edge
  2. ADX-active %         — % of last 50 candles where ADX ≥ 18  (threshold: ≥ 40%)
  3. Rolling PF           — simulated profit factor over last 50 candles using live
                            SL=1.5% / TP=10% settings (threshold: ≥ 1.2)
  4. Current EMA spread   — (EMA9 − EMA21) / EMA21 as %; must be ≥ 0.4% to confirm
                            the EMAs are separated enough for a genuine trend signal

BUY signal for rolling PF uses simplified trend conditions (no EMA200 filter —
200 candles is the full dataset so EMA200 would leave zero tradeable candles):
  - ADX(14) ≥ ADX_THRESHOLD
  - EMA(9) > EMA(21)   (fast above slow = bullish trend)
  - RSI(14) < RSI_OVERSOLD  (pullback entry trigger)

Exits are resolved forward: first candle whose high ≥ TP price → profit,
first candle whose low ≤ SL price → loss. Unresolved entries are excluded.

Logs one line per run to logs/regime_health.log. Never places orders.

Usage:
    python regime_monitor.py

Cron (every 4 hours):
    0 */4 * * *  cd /path/to/trade_bot && python regime_monitor.py
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timezone
from pathlib import Path

# ── Settings ——————————————————————————————————————————————————————————————————
# Defaults match live Kraken config (CLAUDE.md). Override with env vars if needed.
# MONITOR_SYMBOLS: comma-separated list; falls back to MONITOR_SYMBOL for compatibility.
EXCHANGE_ID     = os.getenv("MONITOR_EXCHANGE",   "kraken")
_sym_env        = os.getenv("MONITOR_SYMBOLS", os.getenv("MONITOR_SYMBOL", "BTC/CAD,XRP/CAD"))
SYMBOLS         = [s.strip() for s in _sym_env.split(",") if s.strip()]
SYMBOL          = SYMBOLS[0]   # kept for backward-compat references in helpers
TIMEFRAME       = os.getenv("MONITOR_TIMEFRAME",  "1h")
FETCH_LIMIT     = int(os.getenv("MONITOR_LIMIT",  "200"))
WINDOW          = int(os.getenv("MONITOR_WINDOW", "50"))
ADX_PERIOD      = 14
ADX_THRESHOLD   = float(os.getenv("ADX_THRESHOLD",    "18.0"))
STOP_LOSS_PCT    = float(os.getenv("STOP_LOSS_PCT",       "0.015"))
TAKE_PROFIT_PCT  = float(os.getenv("TAKE_PROFIT_PCT",     "0.10"))
RSI_OVERSOLD     = float(os.getenv("RSI_OVERSOLD",        "30.0"))
FAST_EMA_PERIOD  = int(os.getenv("FAST_EMA_PERIOD",       "9"))
SLOW_EMA_PERIOD  = int(os.getenv("SLOW_EMA_PERIOD",       "21"))
MIN_EMA_SPREAD_PCT = float(os.getenv("MIN_EMA_SPREAD_PCT", "0.004"))

# Health pass/fail thresholds (what the strategy needs for edge)
PF_MIN           = 1.2
ADX_PCT_MIN      = 40.0
EMA_SPREAD_MIN   = MIN_EMA_SPREAD_PCT * 100  # convert to % for display (0.4)

# DOGE/CAD liquidity watchlist gate — not a trading symbol yet
DOGE_SYMBOL      = "DOGE/CAD"
DOGE_VOL_MIN_CAD = float(os.getenv("DOGE_VOL_MIN_CAD", "50000.0"))  # threshold to unlock

# ── Log file ——————————————————————————————————————————————————————————————————
LOG_DIR  = Path("logs")
LOG_DIR.mkdir(exist_ok=True)
LOG_FILE = LOG_DIR / "regime_health.log"


# ── Indicator helpers (inlined — no import chain needed) ——————————————————————

def _ema(prices: list[float], period: int) -> float | None:
    if len(prices) < period:
        return None
    k = 2.0 / (period + 1)
    v = sum(prices[:period]) / period
    for p in prices[period:]:
        v = p * k + v * (1.0 - k)
    return v


def _rsi(prices: list[float], period: int = 14) -> float | None:
    if len(prices) < period + 1:
        return None
    changes = [prices[i] - prices[i - 1] for i in range(1, len(prices))]
    gains   = [max(c, 0.0) for c in changes]
    losses  = [abs(min(c, 0.0)) for c in changes]
    avg_g   = sum(gains[:period]) / period
    avg_l   = sum(losses[:period]) / period
    for i in range(period, len(changes)):
        avg_g = (avg_g * (period - 1) + gains[i]) / period
        avg_l = (avg_l * (period - 1) + losses[i]) / period
    if avg_l == 0.0:
        return 100.0
    return 100.0 - (100.0 / (1.0 + avg_g / avg_l))


def _adx(
    highs: list[float],
    lows:  list[float],
    closes: list[float],
    period: int = 14,
) -> float | None:
    n = len(closes)
    if n < 2 * period + 1 or len(highs) != n or len(lows) != n:
        return None
    tr_list, pdm_list, ndm_list = [], [], []
    for i in range(1, n):
        h, lo, pc = highs[i], lows[i], closes[i - 1]
        ph, pl    = highs[i - 1], lows[i - 1]
        tr   = max(h - lo, abs(h - pc), abs(lo - pc))
        up   = h - ph
        down = pl - lo
        pdm  = up   if up > down and up > 0   else 0.0
        ndm  = down if down > up and down > 0 else 0.0
        tr_list.append(tr)
        pdm_list.append(pdm)
        ndm_list.append(ndm)
    atr  = sum(tr_list[:period])
    apdm = sum(pdm_list[:period])
    andm = sum(ndm_list[:period])
    dx_list: list[float] = []
    for i in range(period, len(tr_list)):
        atr  = atr  - atr  / period + tr_list[i]
        apdm = apdm - apdm / period + pdm_list[i]
        andm = andm - andm / period + ndm_list[i]
        pdi  = 100.0 * apdm / atr if atr > 0 else 0.0
        ndi  = 100.0 * andm / atr if atr > 0 else 0.0
        denom = pdi + ndi
        dx_list.append(100.0 * abs(pdi - ndi) / denom if denom > 0 else 0.0)
    if len(dx_list) < period:
        return None
    adx_val = sum(dx_list[:period]) / period
    for dx in dx_list[period:]:
        adx_val = (adx_val * (period - 1) + dx) / period
    return adx_val


# ── Metric computation ————————————————————————————————————————————————————————

def compute_adx_metrics(
    highs:  list[float],
    lows:   list[float],
    closes: list[float],
) -> tuple[float | None, float]:
    """
    Returns (current_adx, adx_active_pct).

    current_adx    — ADX(14) on the most recent candle (uses all FETCH_LIMIT candles).
    adx_active_pct — % of candles in the last WINDOW positions where ADX ≥ ADX_THRESHOLD.
    """
    n     = len(closes)
    start = max(0, n - WINDOW)

    adx_series: list[float | None] = []
    for i in range(start, n):
        adx_series.append(_adx(highs[: i + 1], lows[: i + 1], closes[: i + 1], period=ADX_PERIOD))

    current_adx = adx_series[-1] if adx_series else None

    valid      = [v for v in adx_series if v is not None]
    active_pct = (sum(1 for v in valid if v >= ADX_THRESHOLD) / len(valid) * 100) if valid else 0.0

    return current_adx, active_pct


def compute_rolling_pf(
    highs:  list[float],
    lows:   list[float],
    closes: list[float],
) -> tuple[float, int]:
    """
    Simulate BUY entries on trend signals over the last WINDOW candles.

    Signal: ADX ≥ threshold  AND  EMA(fast) > EMA(slow)  AND  RSI < RSI_OVERSOLD.
    Exit:   first subsequent candle whose high ≥ TP price  → profit;
            first subsequent candle whose low  ≤ SL price  → loss.
    Unresolved entries (no SL/TP hit before end of data) are excluded.

    Returns (profit_factor, completed_trade_count).
    """
    n     = len(closes)
    start = max(0, n - WINDOW)

    gross_profit = 0.0
    gross_loss   = 0.0
    trade_count  = 0

    for i in range(start, n):
        h  = highs[:i + 1]
        lo = lows[:i + 1]
        c  = closes[:i + 1]

        adx_val  = _adx(h, lo, c, period=ADX_PERIOD)
        rsi_val  = _rsi(c, period=14)
        fast_val = _ema(c, period=FAST_EMA_PERIOD)
        slow_val = _ema(c, period=SLOW_EMA_PERIOD)

        if any(v is None for v in [adx_val, rsi_val, fast_val, slow_val]):
            continue

        ema_spread_val = (fast_val - slow_val) / slow_val if slow_val != 0 else 0.0
        is_buy = (
            adx_val        >= ADX_THRESHOLD
            and fast_val   >  slow_val
            and rsi_val    <  RSI_OVERSOLD
            and ema_spread_val >= MIN_EMA_SPREAD_PCT
        )
        if not is_buy:
            continue

        entry    = closes[i]
        tp_price = entry * (1.0 + TAKE_PROFIT_PCT)
        sl_price = entry * (1.0 - STOP_LOSS_PCT)

        for j in range(i + 1, n):
            if highs[j] >= tp_price:
                gross_profit += TAKE_PROFIT_PCT
                trade_count  += 1
                break
            if lows[j] <= sl_price:
                gross_loss  += STOP_LOSS_PCT
                trade_count += 1
                break

    if gross_loss == 0.0:
        pf = float("inf") if gross_profit > 0.0 else 0.0
    else:
        pf = gross_profit / gross_loss

    return pf, trade_count


def compute_ema_spread(closes: list[float]) -> float | None:
    """
    Returns current EMA spread as a percentage: (EMA_fast − EMA_slow) / EMA_slow × 100.
    Positive = fast above slow (bullish). None if insufficient data.
    """
    fast = _ema(closes, FAST_EMA_PERIOD)
    slow = _ema(closes, SLOW_EMA_PERIOD)
    if fast is None or slow is None or slow == 0.0:
        return None
    return (fast - slow) / slow * 100.0


# ── Display ———————————————————————————————————————————————————————————————————

def print_table(
    now_str:     str,
    symbol:      str,
    current_adx: float | None,
    adx_pct:     float,
    pf:          float,
    trade_count: int,
    ema_spread:  float | None,
) -> None:
    adx_str    = f"{current_adx:.1f}" if current_adx is not None else "N/A"
    spread_str = f"{ema_spread:+.3f}%" if ema_spread is not None else "N/A"

    adx_ok     = current_adx is not None and current_adx >= ADX_THRESHOLD
    adx_pct_ok = adx_pct >= ADX_PCT_MIN
    spread_ok  = ema_spread is not None and ema_spread >= EMA_SPREAD_MIN

    # When the window produced no simulated trades, PF is unmeasurable — exclude
    # it from pass/fail so the verdict reflects only conditions we can actually evaluate.
    _pf_measurable = trade_count > 0
    if _pf_measurable:
        pf_str    = f"{pf:.2f}" if pf != float("inf") else "inf (no losses)"
        pf_ok     = pf >= PF_MIN
        pf_status = "PASS" if pf_ok else "WARN"
    else:
        pf_str    = "N/A"
        pf_ok     = False   # unused in verdict when not measurable
        pf_status = "N/A (no signals in window)"

    def mark(ok: bool) -> str:
        return "PASS" if ok else "WARN"

    w = 36
    print()
    print(f"  ── Regime Monitor  [{now_str}] ──")
    print(f"  {EXCHANGE_ID.capitalize()}  {symbol}  {TIMEFRAME}  |  "
          f"{FETCH_LIMIT} candles  |  window={WINDOW}")
    print()
    print(f"  {'Metric':<{w}}  {'Value':>12}  {'Threshold':>12}  Status")
    print("  " + "─" * (w + 38))
    print(f"  {'Current ADX(14)':<{w}}  {adx_str:>12}  "
          f"{'≥ ' + str(int(ADX_THRESHOLD)):>12}  {mark(adx_ok)}")
    print(f"  {'ADX ≥ 18  (last ' + str(WINDOW) + ' candles)':<{w}}  "
          f"{adx_pct:>11.1f}%  {'≥ ' + str(int(ADX_PCT_MIN)) + '%':>12}  {mark(adx_pct_ok)}")
    print(f"  {'Rolling PF  (last ' + str(WINDOW) + ' candles)':<{w}}  "
          f"{pf_str:>12}  {'≥ ' + str(PF_MIN):>12}  {pf_status}")
    print(f"  {'EMA spread  (EMA9 − EMA21) / EMA21':<{w}}  "
          f"{spread_str:>12}  {'≥ ' + f'{EMA_SPREAD_MIN:.1f}%':>12}  {mark(spread_ok)}")
    print()

    if _pf_measurable:
        print(f"  Rolling PF from {trade_count} completed trade(s)  "
              f"|  SL={STOP_LOSS_PCT * 100:.1f}%  TP={TAKE_PROFIT_PCT * 100:.1f}%")
        if trade_count < 5:
            print(f"  Note: PF is unreliable with fewer than 5 trades — use as indicative only")
    else:
        print(f"  Rolling PF: 0 completed trades — ADX+EMA+RSI+spread conditions not met in window")

    # Build verdict from measurable conditions only
    _measurable = [adx_ok, adx_pct_ok, spread_ok]
    if _pf_measurable:
        _measurable.append(pf_ok)
    ok_count    = sum(_measurable)
    total_count = len(_measurable)
    _cond_label = "measurable conditions" if total_count < 4 else "conditions"

    print()
    if ok_count == total_count:
        verdict = f"EDGE PRESENT   — all {total_count}/{total_count} {_cond_label} met"
    else:
        verdict = f"DEGRADED       — {ok_count}/{total_count} {_cond_label} met  (review before new entries)"
    print(f"  Verdict: {verdict}")
    print()


# ── Log file ——————————————————————————————————————————————————————————————————

def append_log(
    now_str:     str,
    symbol:      str,
    current_adx: float | None,
    adx_pct:     float,
    pf:          float,
    trade_count: int,
    ema_spread:  float | None,
) -> None:
    adx_str    = f"{current_adx:.2f}" if current_adx is not None else "N/A"
    pf_str     = f"{pf:.3f}" if pf != float("inf") else "inf"
    spread_str = f"{ema_spread:.4f}" if ema_spread is not None else "N/A"

    adx_ok     = current_adx is not None and current_adx >= ADX_THRESHOLD
    adx_pct_ok = adx_pct >= ADX_PCT_MIN
    spread_ok  = ema_spread is not None and ema_spread >= EMA_SPREAD_MIN
    # Exclude PF from verdict when window produced no signals (same logic as print_table)
    pf_ok      = (trade_count > 0 and pf >= PF_MIN)
    ok = adx_ok and adx_pct_ok and spread_ok and (trade_count == 0 or pf_ok)
    verdict = "EDGE" if ok else "DEGRADED"

    line = (
        f"{now_str}  "
        f"{symbol:<8}  "
        f"ADX={adx_str:>6}  "
        f"ADX%={adx_pct:>5.1f}  "
        f"PF={pf_str:>7}  "
        f"trades={trade_count:>3}  "
        f"spread={spread_str:>7}%  "
        f"verdict={verdict}\n"
    )
    with open(LOG_FILE, "a") as f:
        f.write(line)


# ── DOGE/CAD liquidity watchlist ——————————————————————————————————————————————

def _check_doge_liquidity(exchange, now_str: str) -> None:
    """
    Fetch the DOGE/CAD 24h ticker and report volume vs the $50k CAD unlock gate.
    Appends one line to the log. Does not affect trading — display only.
    """
    w = 36
    print()
    print(f"  ── Watchlist  [{now_str}] ──")
    print(f"  {EXCHANGE_ID.capitalize()}  {DOGE_SYMBOL}  |  liquidity gate only — not currently traded")
    print()
    print(f"  {'Metric':<{w}}  {'Value':>14}  {'Threshold':>12}  Status")
    print("  " + "─" * (w + 40))

    try:
        ticker   = exchange.fetch_ticker(DOGE_SYMBOL)
        last     = ticker.get("last") or 0.0
        vol_base = ticker.get("baseVolume")          # volume in DOGE
        vol_cad  = (vol_base * last) if vol_base else None
    except Exception as exc:
        print(f"  {'24h Volume (CAD)':<{w}}  {'ERROR':>14}  "
              f"{'≥ $' + f'{DOGE_VOL_MIN_CAD:,.0f}':>12}  WARN")
        print(f"  (ticker fetch failed: {exc})")
        print()
        return

    if vol_cad is None:
        vol_str = "N/A"
        gate_ok = False
    else:
        vol_str = f"${vol_cad:>12,.0f}"
        gate_ok = vol_cad >= DOGE_VOL_MIN_CAD

    threshold_str = f"≥ ${DOGE_VOL_MIN_CAD:,.0f}"
    status        = "PASS" if gate_ok else "WARN"
    print(f"  {'24h Volume (CAD)':<{w}}  {vol_str:>14}  {threshold_str:>12}  {status}")

    note = "volume gate OPEN — eligible for live trading" if gate_ok else \
           f"volume gate CLOSED — needs ${DOGE_VOL_MIN_CAD:,.0f} CAD/day"
    print()
    print(f"  {note}")
    print()

    vol_log = f"{vol_cad:,.0f}" if vol_cad is not None else "N/A"
    line = (
        f"{now_str}  "
        f"{'DOGE/CAD':<8}  "
        f"vol_cad={vol_log}  "
        f"threshold={DOGE_VOL_MIN_CAD:,.0f}  "
        f"verdict={'PASS' if gate_ok else 'WARN'}\n"
    )
    with open(LOG_FILE, "a") as f:
        f.write(line)


# ── Main ——————————————————————————————————————————————————————————————————————

def _check_symbol(exchange, symbol: str, now_str: str) -> bool:
    """Fetch candles for one symbol, compute metrics, print table, append log.
    Returns True on success, False on error."""
    print(
        f"\n  Fetching {FETCH_LIMIT} × {TIMEFRAME} candles of {symbol} "
        f"from {EXCHANGE_ID.capitalize()} …",
        flush=True,
    )

    try:
        raw = exchange.fetch_ohlcv(symbol, timeframe=TIMEFRAME, limit=FETCH_LIMIT)
    except Exception as exc:
        print(f"  ERROR fetching candles for {symbol}: {exc}")
        return False

    if not raw:
        print(f"  ERROR: no data returned for {symbol} on {EXCHANGE_ID}")
        return False

    closes = [float(row[4]) for row in raw]
    highs  = [float(row[2]) for row in raw]
    lows   = [float(row[3]) for row in raw]

    min_required = ADX_PERIOD * 2 + 1 + WINDOW
    if len(closes) < min_required:
        print(f"  ERROR: need ≥ {min_required} candles for warmup, got {len(closes)}")
        return False

    from_dt = datetime.fromtimestamp(raw[0][0]  / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")
    to_dt   = datetime.fromtimestamp(raw[-1][0] / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")
    print(f"  {len(closes)} candles  ({from_dt} → {to_dt})\n")

    current_adx, adx_pct = compute_adx_metrics(highs, lows, closes)
    pf, trade_count       = compute_rolling_pf(highs, lows, closes)
    ema_spread            = compute_ema_spread(closes)

    print_table(now_str, symbol, current_adx, adx_pct, pf, trade_count, ema_spread)
    append_log(now_str, symbol, current_adx, adx_pct, pf, trade_count, ema_spread)
    return True


def main() -> None:
    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    try:
        import ccxt  # noqa: PLC0415
    except ImportError:
        print("  ERROR: ccxt not installed. Run: pip install ccxt")
        sys.exit(1)

    exchange_cls = getattr(ccxt, EXCHANGE_ID.lower())
    exchange     = exchange_cls({"timeout": 20_000})

    any_ok = False
    for sym in SYMBOLS:
        ok = _check_symbol(exchange, sym, now_str)
        if ok:
            any_ok = True

    _check_doge_liquidity(exchange, now_str)

    if any_ok:
        print(f"  Logged → {LOG_FILE}\n")
    else:
        sys.exit(1)


if __name__ == "__main__":
    main()
