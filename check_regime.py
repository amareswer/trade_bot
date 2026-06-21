"""
Regime monitoring script.

Run anytime to check current BTC/CAD regime status:
    python3 check_regime.py

Logs a daily snapshot to logs/regime_log.csv automatically.
"""
from __future__ import annotations

import csv
import os
from datetime import datetime, timezone
from typing import Optional

try:
    import ccxt
except ImportError:
    print("Error: ccxt not installed. Run: pip install ccxt")
    raise SystemExit(1)


def ema(closes: list, period: int) -> Optional[float]:
    if len(closes) < period:
        return None
    k = 2.0 / (period + 1)
    val = sum(closes[:period]) / period
    for price in closes[period:]:
        val = price * k + val * (1 - k)
    return val


def main():
    from config import cfg
    exchange_cls = getattr(ccxt, cfg.exchange.exchange.lower())
    exchange = exchange_cls()

    # ── Fetch data ────────────────────────────────────────────────────
    try:
        ohlcv = exchange.fetch_ohlcv(cfg.exchange.symbol, cfg.backtest.timeframe, limit=300)
    except Exception as e:
        print(f"Error fetching data: {e}")
        raise SystemExit(1)

    closes = [c[4] for c in ohlcv]

    # ── Validate ──────────────────────────────────────────────────────
    if len(closes) < 201:
        print(f"Not enough candles for EMA200 (got {len(closes)}, need 201)")
        raise SystemExit(1)

    # ── Calculate ─────────────────────────────────────────────────────
    current  = closes[-1]
    ema_now  = ema(closes,      200)
    ema_prev = ema(closes[:-6], 200)   # 24h ago (6 x 4h candles)

    gap          = ema_now - current
    gap_pct      = (current - ema_now) / ema_now * 100
    velocity     = ema_now - ema_prev              # CAD/day (negative = falling)
    velocity_pct = velocity / ema_prev * 100 if ema_prev else 0.0
    regime       = 'BULL' if current > ema_now else 'BEAR'

    if velocity < 0:
        est_days = gap / abs(velocity)
        est_str  = f"{est_days:.1f} days (optimistic; EMA decay slows near crossover)"
    else:
        est_str  = "EMA rising — crossover moving away from price"

    # ── Print ─────────────────────────────────────────────────────────
    now = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')
    print()
    print(f"  -- BTC/CAD Regime Status  [{now}] --")
    print(f"  Price:         ${current:>12,.2f} CAD")
    print(f"  EMA200:        ${ema_now:>12,.2f} CAD")
    print(f"  Gap:           ${gap:>12,.2f} CAD  ({gap_pct:+.2f}%)")
    print(f"  EMA velocity:  ${velocity:>+12,.2f} CAD/day  ({velocity_pct:+.3f}%/day)")
    print(f"  Est crossover: {est_str}")
    print(f"  Regime:        {regime}")
    print(f"  Bot action:    {'TRADING -- entries permitted' if regime == 'BULL' else 'WAITING -- cash protected, no new entries'}")
    print()

    # ── CSV snapshot ──────────────────────────────────────────────────
    log_dir = "logs"
    os.makedirs(log_dir, exist_ok=True)
    log_path = os.path.join(log_dir, "regime_log.csv")

    write_header = not os.path.exists(log_path)
    today = datetime.now(timezone.utc).strftime('%Y-%m-%d')

    # Read existing rows to avoid duplicate date entries
    existing_dates = set()
    if os.path.exists(log_path):
        with open(log_path, newline='') as f:
            for row in csv.DictReader(f):
                existing_dates.add(row.get('date', ''))

    if today not in existing_dates:
        with open(log_path, 'a', newline='') as f:
            writer = csv.DictWriter(f, fieldnames=[
                'date', 'price', 'ema200', 'gap_pct',
                'velocity_cad_day', 'est_days', 'regime'
            ])
            if write_header:
                writer.writeheader()
            writer.writerow({
                'date':             today,
                'price':            round(current, 2),
                'ema200':           round(ema_now, 2),
                'gap_pct':          round(gap_pct, 3),
                'velocity_cad_day': round(velocity, 2),
                'est_days':         round(gap / abs(velocity), 1) if velocity < 0 else 'n/a',
                'regime':           regime,
            })
        print(f"  Snapshot saved -> {log_path}")
    else:
        print(f"  Snapshot already recorded for {today} -> {log_path}")
    print()


if __name__ == "__main__":
    main()