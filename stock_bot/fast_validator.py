"""
Fast Validator — separate paper trading engine on 1-hour candles.

Designed for rapid signal validation: tight SL/TP, short max-hold, small
position count. Runs independently from the main paper trader — separate
state file and separate trades CSV. Never touches paper_trades.csv or
paper_state.json.

Rules enforced throughout:
  - yf.download only, no session= parameter
  - No .info calls anywhere
  - All thresholds from FastValidatorConfig (env-backed) — no numeric literals
  - fast_info only inside `if symbol.upper().endswith(".TO"):` block
"""
from __future__ import annotations

import csv
import json
import logging
import math
import os
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Callable, Optional

import yfinance as yf
from dotenv import load_dotenv

from stock_bot.data.yf_client import fetch_with_retry
from stock_bot.data.intraday_price import get_live_price
from stock_bot.indicators.indicators import (
    adx   as calc_adx,
    atr   as calc_atr,
    macd  as calc_macd,
    rsi   as calc_rsi,
    trend as calc_trend,
)
from stock_bot.ai.verdict import AIVerdict

logger = logging.getLogger(__name__)

# Load stock_bot/.env — same source as config.py
_ENV_PATH = os.path.join(os.path.dirname(__file__), ".env")
load_dotenv(dotenv_path=_ENV_PATH, override=False)

_STOCK_BOT_DIR   = os.path.dirname(os.path.abspath(__file__))
_STATE_JSON      = os.path.join(_STOCK_BOT_DIR, "fast_validator_state.json")
_TRADES_CSV      = os.path.join(_STOCK_BOT_DIR, "fast_trades.csv")

# Matches paper_trades.csv frozen schema (9 columns)
_CSV_HEADER = [
    "timestamp", "symbol", "side", "shares",
    "price", "total_value", "cash_remaining", "reason", "confidence",
]

# Serializes yf.download calls to avoid connection pool exhaustion
_yf_lock = threading.Lock()


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

def _env_str(key: str, default: str) -> str:
    return os.getenv(key, default).strip()


def _env_float(key: str, default: float) -> float:
    raw = os.getenv(key)
    if raw is None:
        return default
    try:
        return float(raw.strip())
    except ValueError:
        raise ValueError(f"FastValidator config: {key} must be a number, got '{raw}'")


def _env_int(key: str, default: int) -> int:
    raw = os.getenv(key)
    if raw is None:
        return default
    try:
        return int(raw.strip())
    except ValueError:
        raise ValueError(f"FastValidator config: {key} must be an integer, got '{raw}'")


@dataclass
class FastValidatorConfig:
    candle_interval:   str    # yfinance interval string (e.g. "1h")
    lookback_hours:    int    # hours of candle history to fetch
    sl_pct:            float  # stop-loss percentage (e.g. 1.5 = 1.5%)
    tp_pct:            float  # take-profit percentage (e.g. 3.0 = 3.0%)
    max_hold_hours:    int    # force-exit if position held longer than this
    max_positions:     int    # max simultaneous open positions
    min_confidence:    int    # minimum AI confidence to enter a trade
    price_sanity_pct:  float = 20.0  # reject candle close deviating > this % from prior close


def load_fast_config() -> FastValidatorConfig:
    return FastValidatorConfig(
        candle_interval = _env_str  ("FAST_CANDLE_INTERVAL",  "1h"),
        lookback_hours  = _env_int  ("FAST_LOOKBACK_HOURS",   168),
        sl_pct          = _env_float("FAST_SL_PCT",           1.5),
        tp_pct          = _env_float("FAST_TP_PCT",           3.0),
        max_hold_hours  = _env_int  ("FAST_MAX_HOLD_HOURS",   48),
        max_positions   = _env_int  ("FAST_MAX_POSITIONS",    2),
        min_confidence  = _env_int  ("FAST_MIN_CONFIDENCE",   70),
        price_sanity_pct = _env_float("FAST_PRICE_SANITY_PCT", 20.0),
    )


def _last_close_sane(candles: list, sanity_pct: float) -> bool:
    """
    Corruption guard on the close used for entries/exits — same 20% deviation
    principle as get_live_price()'s previous-close check. Incident: 2026-06-29
    META candle printed $163.51 against a $564.87 entry (−71% in 20 min, price
    corruption not market) and wrote a phantom SL exit into the stats book.
    A single candle is trusted (nothing to compare against).
    """
    if len(candles) < 2:
        return True
    prev = candles[-2].close
    if prev <= 0:
        return True
    deviation = abs(candles[-1].close - prev) / prev * 100
    return deviation <= sanity_pct


# ---------------------------------------------------------------------------
# Position + State
# ---------------------------------------------------------------------------

@dataclass
class FastPosition:
    symbol:      str
    entry_time:  datetime
    entry_price: float
    sl_price:    float
    tp_price:    float
    confidence:  int
    shares:      float = 1.0   # normalized unit sizing for signal validation


@dataclass
class FastValidatorState:
    positions: list[FastPosition] = field(default_factory=list)

    def open_symbols(self) -> set[str]:
        return {p.symbol.upper() for p in self.positions}

    def position_count(self) -> int:
        return len(self.positions)

    def get(self, symbol: str) -> Optional[FastPosition]:
        sym = symbol.upper()
        for p in self.positions:
            if p.symbol.upper() == sym:
                return p
        return None

    def add(self, pos: FastPosition) -> None:
        self.positions.append(pos)

    def remove(self, symbol: str) -> None:
        sym = symbol.upper()
        self.positions = [p for p in self.positions if p.symbol.upper() != sym]

    # ── Persistence ────────────────────────────────────────────────────────

    def save(self, path: str = _STATE_JSON) -> None:
        data = {
            "positions": [
                {
                    "symbol":      p.symbol,
                    "entry_time":  p.entry_time.strftime("%Y-%m-%dT%H:%M:%S"),
                    "entry_price": p.entry_price,
                    "sl_price":    p.sl_price,
                    "tp_price":    p.tp_price,
                    "confidence":  p.confidence,
                    "shares":      p.shares,
                }
                for p in self.positions
            ],
            "last_updated": datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
        }
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
        except OSError as exc:
            logger.warning("Could not save fast_validator_state.json: %s", exc)

    @classmethod
    def load(cls, path: str = _STATE_JSON) -> "FastValidatorState":
        if not os.path.exists(path):
            return cls()
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            positions = []
            for d in data.get("positions", []):
                positions.append(FastPosition(
                    symbol      = d["symbol"],
                    entry_time  = datetime.strptime(d["entry_time"], "%Y-%m-%dT%H:%M:%S"),
                    entry_price = float(d["entry_price"]),
                    sl_price    = float(d["sl_price"]),
                    tp_price    = float(d["tp_price"]),
                    confidence  = int(d["confidence"]),
                    shares      = float(d.get("shares", 1.0)),
                ))
            return cls(positions=positions)
        except (KeyError, ValueError, json.JSONDecodeError, OSError) as exc:
            logger.warning("Could not load fast_validator_state.json (%s) — starting fresh", exc)
            return cls()


# ---------------------------------------------------------------------------
# Candle dataclass (mirrors price_feed.Candle — avoids circular import)
# ---------------------------------------------------------------------------

@dataclass
class FastCandle:
    timestamp:    datetime
    open:         float
    high:         float
    low:          float
    close:        float
    volume:       float


# ---------------------------------------------------------------------------
# FastValidator
# ---------------------------------------------------------------------------

class FastValidator:
    """
    1-hour candle paper trading engine for rapid signal validation.

    Lifecycle per run_cycle():
      1. check_exits()  — close positions that hit SL, TP, max-hold, or reversal
      2. entry scan     — open positions on remaining slots if AI signals BUY
    """

    def __init__(
        self,
        cfg:                 FastValidatorConfig | None = None,
        state:               FastValidatorState  | None = None,
        blocked_symbols_fn:  Callable[[], set[str]] | None = None,
    ) -> None:
        self.cfg   = cfg   or load_fast_config()
        self.state = state or FastValidatorState.load()
        # Callable that returns symbols currently held in the position book.
        # Used to prevent double exposure (swing + position on same symbol).
        self._blocked_symbols_fn = blocked_symbols_fn
        self._ensure_csv_header()

    # ── Data fetch ─────────────────────────────────────────────────────────

    def fetch_candles_1h(self, symbol: str) -> list[FastCandle] | None:
        """
        Fetch 1-hour candles via yf.download — no session= parameter.
        Lookback is derived from FAST_LOOKBACK_HOURS rounded up to whole days.
        TSX price sanity check uses fast_info inside the .TO guard block only.
        """
        days_needed = max(1, math.ceil(self.cfg.lookback_hours / 24))
        # yfinance 1h interval is limited to the last 730 days; cap safely
        period_str  = f"{min(days_needed, 729)}d"

        with _yf_lock:
            df = fetch_with_retry(
                lambda: yf.download(
                    symbol,
                    period       = period_str,
                    interval     = self.cfg.candle_interval,
                    auto_adjust  = True,
                    actions      = False,
                    progress     = False,
                ),
                label=f"{symbol}:fast_candles",
            )
            time.sleep(0.5)

        if df is None or df.empty:
            logger.debug("FastValidator: empty result for %s", symbol)
            return None

        # Flatten MultiIndex columns (yfinance >= 0.2.38 single-ticker)
        if hasattr(df.columns, "nlevels") and df.columns.nlevels > 1:
            df.columns = [col[0] if isinstance(col, tuple) else col for col in df.columns]

        candles: list[FastCandle] = []
        for ts, row in df.iterrows():
            try:
                close = float(row["Close"])
                if math.isnan(close) or close <= 0:
                    continue
                candles.append(FastCandle(
                    timestamp = ts.to_pydatetime(),
                    open      = float(row["Open"]),
                    high      = float(row["High"]),
                    low       = float(row["Low"]),
                    close     = close,
                    volume    = float(row["Volume"]),
                ))
            except (KeyError, ValueError, TypeError) as exc:
                logger.debug("FastValidator: skipping malformed row %s at %s: %s", symbol, ts, exc)

        if not candles:
            return None

        # Trim to the requested lookback window
        cutoff = datetime.utcnow() - timedelta(hours=self.cfg.lookback_hours)
        candles = [c for c in candles if c.timestamp.replace(tzinfo=None) >= cutoff]
        if not candles:
            return None

        latest = candles[-1].close

        # TSX sanity check: fast_info only for .TO symbols
        if symbol.upper().endswith(".TO"):
            def _fetch_tsx_fi():
                fi = yf.Ticker(symbol).fast_info
                return getattr(fi, "last_price", None) or getattr(fi, "lastPrice", None)

            tsx_last = fetch_with_retry(
                _fetch_tsx_fi,
                label=f"{symbol}:fast_tsx_sanity",
                max_attempts=2,
            )
            if tsx_last and tsx_last > 0:
                deviation = abs(latest - tsx_last) / tsx_last
                if deviation > 0.05:
                    logger.warning(
                        "FastValidator %s: candle close $%.2f vs fast_info.last_price $%.2f "
                        "(%.1f%%) — rejecting as corrupted",
                        symbol, latest, tsx_last, deviation * 100,
                    )
                    return None

        logger.debug("FastValidator: fetched %d 1h candles for %s", len(candles), symbol)
        return candles

    # ── Signal computation ─────────────────────────────────────────────────

    def compute_signals(self, candles: list[FastCandle]) -> dict:
        """
        Compute RSI, MACD, trend, ADX, ATR from candle list.
        Returns dict with keys: rsi, macd_line, macd_signal, macd_hist,
        trend, adx, atr.  Any value may be None on insufficient data.
        """
        closes = [c.close  for c in candles]
        highs  = [c.high   for c in candles]
        lows   = [c.low    for c in candles]

        rsi_val  = calc_rsi(closes)
        macd_out = calc_macd(closes)
        if macd_out is not None:
            macd_line, macd_signal, macd_hist = macd_out
        else:
            macd_line = macd_signal = macd_hist = None

        trend_val = calc_trend(closes)
        adx_val   = calc_adx(highs, lows, closes)
        atr_val   = calc_atr(highs, lows, closes)

        return {
            "rsi":         rsi_val,
            "macd_line":   macd_line,
            "macd_signal": macd_signal,
            "macd_hist":   macd_hist,
            "trend":       trend_val,
            "adx":         adx_val,
            "atr":         atr_val,
        }

    # ── Exit logic ─────────────────────────────────────────────────────────

    def check_exits(
        self,
        state:           FastValidatorState,
        current_candles: dict[str, list[FastCandle]],
        signals:         dict[str, dict],
    ) -> list[dict]:
        """
        Evaluate all open positions for exit conditions.

        Returns list of exit records (one per closed position):
          {symbol, reason, exit_price, entry_price, pnl_pct, hold_hours}

        Mutates `state` in-place by removing closed positions.
        Writes each exit to fast_trades.csv.
        """
        exits: list[dict] = []
        to_close: list[tuple[FastPosition, str, float]] = []   # (pos, reason, exit_price)

        now = datetime.utcnow()

        for pos in list(state.positions):
            hold_hours = (now - pos.entry_time).total_seconds() / 3600.0
            candles = current_candles.get(pos.symbol.upper())
            if not candles:
                # No candles this cycle (rate limit / market holiday). SL/TP and
                # signal-reversal need a fresh price and can wait — but MAX_HOLD
                # is the trade-completion guarantee for the stats book and must
                # not be starved by feed gaps: fall back to the guarded
                # live-price helper (same one the main book's SL/TP watcher uses).
                if hold_hours >= self.cfg.max_hold_hours:
                    live = get_live_price(pos.symbol)
                    if live is not None:
                        to_close.append((pos, "MAX_HOLD", live))
                        continue
                logger.warning(
                    "FastValidator: no candles for open position %s — skipping"
                    " exit check (held %.1fh / max %dh)",
                    pos.symbol, hold_hours, self.cfg.max_hold_hours,
                )
                continue

            if not _last_close_sane(candles, self.cfg.price_sanity_pct):
                logger.warning(
                    "FastValidator: %s close $%.4f deviates > %.0f%% from prior"
                    " candle — suspected corruption, skipping exit check this cycle",
                    pos.symbol, candles[-1].close, self.cfg.price_sanity_pct,
                )
                continue

            latest = candles[-1].close
            sig = signals.get(pos.symbol.upper(), {})

            # Priority 1: stop-loss
            if latest <= pos.sl_price:
                to_close.append((pos, "SL", latest))
                continue

            # Priority 2: take-profit
            if latest >= pos.tp_price:
                to_close.append((pos, "TP", latest))
                continue

            # Priority 3: max hold exceeded
            if hold_hours >= self.cfg.max_hold_hours:
                to_close.append((pos, "MAX_HOLD", latest))
                continue

            # Priority 4: signal reversal (trend flipped bearish while long)
            trend_now = sig.get("trend", "NEUTRAL")
            if trend_now == "BEARISH":
                to_close.append((pos, "SIGNAL_REVERSAL", latest))
                continue

        for pos, reason, exit_price in to_close:
            pnl_pct    = (exit_price - pos.entry_price) / pos.entry_price * 100
            hold_hours = (now - pos.entry_time).total_seconds() / 3600.0

            exit_rec = {
                "symbol":      pos.symbol,
                "reason":      reason,
                "exit_price":  exit_price,
                "entry_price": pos.entry_price,
                "pnl_pct":     round(pnl_pct, 3),
                "hold_hours":  round(hold_hours, 2),
            }
            exits.append(exit_rec)

            self._write_trade(
                symbol      = pos.symbol,
                side        = "SELL",
                shares      = pos.shares,
                price       = exit_price,
                reason      = reason,
                confidence  = pos.confidence,
            )
            state.remove(pos.symbol)
            logger.info(
                "FastValidator EXIT  %s  reason=%-14s  exit=$%.4f  pnl=%+.2f%%  hold=%.1fh",
                pos.symbol, reason, exit_price, pnl_pct, hold_hours,
            )

        return exits

    # ── Entry logic ────────────────────────────────────────────────────────

    def _try_enter(
        self,
        symbol:     str,
        candles:    list[FastCandle],
        sig:        dict,
        ai_engine,
        verdict:    AIVerdict,
    ) -> bool:
        """
        Attempt to open a position on `symbol`.  Returns True on entry.
        Requires: AI says BUY, confidence >= min, trend not bearish.
        """
        if verdict.signal != "BUY":
            return False
        if verdict.confidence < self.cfg.min_confidence:
            return False
        if sig.get("trend") == "BEARISH":
            return False

        if self._blocked_symbols_fn is not None:
            blocked = self._blocked_symbols_fn()
            if symbol.upper() in blocked:
                logger.info(
                    "FastValidator: skip %s — already held in position book (dual-exposure guard)",
                    symbol,
                )
                return False

        if not _last_close_sane(candles, self.cfg.price_sanity_pct):
            logger.warning(
                "FastValidator: %s entry rejected — close $%.4f deviates > %.0f%%"
                " from prior candle (suspected corruption)",
                symbol, candles[-1].close, self.cfg.price_sanity_pct,
            )
            return False

        entry_price = candles[-1].close
        sl_price    = round(entry_price * (1.0 - self.cfg.sl_pct  / 100.0), 6)
        tp_price    = round(entry_price * (1.0 + self.cfg.tp_pct  / 100.0), 6)

        pos = FastPosition(
            symbol      = symbol.upper(),
            entry_time  = datetime.utcnow(),
            entry_price = entry_price,
            sl_price    = sl_price,
            tp_price    = tp_price,
            confidence  = verdict.confidence,
            shares      = 1.0,
        )
        self.state.add(pos)
        self._write_trade(
            symbol     = symbol.upper(),
            side       = "BUY",
            shares     = pos.shares,
            price      = entry_price,
            reason     = f"AI:{verdict.signal} conf={verdict.confidence}",
            confidence = verdict.confidence,
        )
        logger.info(
            "FastValidator ENTRY %s  price=$%.4f  SL=$%.4f  TP=$%.4f  conf=%d",
            symbol, entry_price, sl_price, tp_price, verdict.confidence,
        )
        return True

    # ── Full cycle ─────────────────────────────────────────────────────────

    def run_cycle(self, symbols: list[str], ai_engine) -> dict:
        """
        One full scan cycle:
          1. Fetch candles + compute signals for all symbols
          2. check_exits() on open positions
          3. Scan for entries on available slots

        Returns summary dict with: exits (list), entries (list), open_count (int).
        Never raises — all failures are logged and skipped.
        """
        # Import here to avoid circular dependency at module load time
        from stock_bot.research.aggregator import ResearchReport
        from stock_bot.research.earnings   import EarningsInfo
        from stock_bot.research.fear_greed import FearGreedData
        from stock_bot.research.sentiment_scraper import SentimentData

        _empty_research = lambda sym: ResearchReport(
            symbol              = sym,
            timestamp           = datetime.utcnow(),
            news                = [],
            sentiment           = SentimentData(score=0.0, label="NEUTRAL", post_count=0),
            market_trends_score = None,
            earnings            = EarningsInfo(),
            fear_greed          = FearGreedData(score=50, label="Unknown", last_updated="unavailable"),
        )

        all_candles: dict[str, list[FastCandle]] = {}
        all_signals: dict[str, dict]             = {}

        # Fetch candles and compute signals for all symbols
        for sym in symbols:
            key = sym.upper()
            candles = self.fetch_candles_1h(sym)
            if candles:
                all_candles[key] = candles
                all_signals[key] = self.compute_signals(candles)
            else:
                logger.debug("FastValidator: no candles for %s — skipping", sym)

        # Phase 1: check exits
        exits = self.check_exits(self.state, all_candles, all_signals)

        # Phase 2: look for entries on open slots
        entries: list[str] = []
        open_count = self.state.position_count()

        for sym in symbols:
            if open_count >= self.cfg.max_positions:
                break
            key = sym.upper()
            if key in self.state.open_symbols():
                continue   # already holding this symbol
            candles = all_candles.get(key)
            sig     = all_signals.get(key, {})
            if not candles:
                continue

            try:
                verdict = ai_engine.analyze(
                    symbol          = sym,
                    candle          = candles[-1],
                    indicators      = sig,
                    research        = _empty_research(sym),
                    stop_loss_pct   = self.cfg.sl_pct  / 100.0,
                    take_profit_pct = self.cfg.tp_pct  / 100.0,
                )
            except Exception as exc:
                logger.warning("FastValidator: AI call failed for %s: %s", sym, exc)
                continue

            entered = self._try_enter(sym, candles, sig, ai_engine, verdict)
            if entered:
                entries.append(key)
                open_count += 1

        # Persist state after each cycle
        self.state.save()

        return {
            "exits":      exits,
            "entries":    entries,
            "open_count": self.state.position_count(),
        }

    # ── CSV helpers ────────────────────────────────────────────────────────

    def _ensure_csv_header(self) -> None:
        if not os.path.exists(_TRADES_CSV):
            try:
                with open(_TRADES_CSV, "w", newline="", encoding="utf-8") as f:
                    csv.writer(f).writerow(_CSV_HEADER)
                logger.info("Created fast_trades.csv at %s", _TRADES_CSV)
            except OSError as exc:
                logger.warning("Could not create fast_trades.csv: %s", exc)

    def _write_trade(
        self,
        symbol:     str,
        side:       str,
        shares:     float,
        price:      float,
        reason:     str = "",
        confidence: int = 0,
    ) -> None:
        row = [
            datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"),
            symbol,
            side,
            f"{shares:.4f}",
            f"{price:.4f}",
            f"{shares * price:.2f}",  # total_value
            "0.00",                   # cash_remaining — not tracked in fast validator
            reason,
            confidence,
        ]
        try:
            with open(_TRADES_CSV, "a", newline="", encoding="utf-8") as f:
                csv.writer(f).writerow(row)
        except OSError as exc:
            logger.warning("Could not write to fast_trades.csv: %s", exc)


# ---------------------------------------------------------------------------
# FastValidatorReport
# ---------------------------------------------------------------------------

class FastValidatorReport:
    """
    Reads fast_trades.csv and produces a summary report.

    Metrics: completed trades, win rate, avg hold hours,
    confidence band breakdown (mirrors ConfidenceBandTracker logic).
    """

    _BAND_NAMES = {
        "HIGH": "90–100",
        "MED":  "80–89",
        "LOW":  "70–79",
        "PRE":  "<70 / no conf",
    }

    @staticmethod
    def _confidence_band(confidence: int) -> str:
        if confidence >= 90:
            return "HIGH"
        if confidence >= 80:
            return "MED"
        if confidence >= 70:
            return "LOW"
        return "PRE"

    def load_trades(self, csv_path: str = _TRADES_CSV) -> list[dict]:
        trades: list[dict] = []
        if not os.path.exists(csv_path):
            return trades
        with open(csv_path, "r", encoding="utf-8", newline="") as f:
            reader = csv.reader(f)
            for row in reader:
                if not row:
                    continue
                if row[0].strip().lower() == "timestamp":
                    continue
                try:
                    datetime.strptime(row[0].strip()[:19], "%Y-%m-%d %H:%M:%S")
                except (ValueError, IndexError):
                    continue
                d: dict = {}
                for i, col in enumerate(_CSV_HEADER):
                    d[col] = row[i].strip() if i < len(row) else ""
                try:
                    d["shares"]     = float(d["shares"])     if d["shares"]     else 0.0
                    d["price"]      = float(d["price"])      if d["price"]      else 0.0
                    d["confidence"] = int(float(d["confidence"])) if d["confidence"] else 0
                except (ValueError, TypeError):
                    d["shares"]     = 0.0
                    d["price"]      = 0.0
                    d["confidence"] = 0
                trades.append(d)
        return trades

    def pair_trades(self, trades: list[dict]) -> list[dict]:
        """
        FIFO BUY→SELL pairing. Returns completed round-trips with hold_hours.
        Unpaired BUYs (still open) are excluded.
        """
        open_buys: dict[str, list[dict]] = {}
        pairs: list[dict] = []

        for t in trades:
            sym  = t.get("symbol", "").upper()
            side = t.get("side", "").upper()
            if not sym or not side:
                continue
            if side == "BUY":
                open_buys.setdefault(sym, []).append(t)
            elif side == "SELL":
                queue = open_buys.get(sym, [])
                if not queue:
                    continue
                buy = queue.pop(0)

                entry_price = buy["price"]
                exit_price  = t["price"]
                shares      = buy["shares"]
                pnl_pct     = (
                    round((exit_price - entry_price) / entry_price * 100, 3)
                    if entry_price > 0 else 0.0
                )

                try:
                    entry_dt   = datetime.strptime(buy["timestamp"][:19], "%Y-%m-%d %H:%M:%S")
                    exit_dt    = datetime.strptime(t["timestamp"][:19],   "%Y-%m-%d %H:%M:%S")
                    hold_hours = max(0.0, (exit_dt - entry_dt).total_seconds() / 3600.0)
                except (ValueError, KeyError):
                    hold_hours = 0.0

                pairs.append({
                    "symbol":      sym,
                    "entry_date":  buy["timestamp"][:16],
                    "exit_date":   t["timestamp"][:16],
                    "entry_price": entry_price,
                    "exit_price":  exit_price,
                    "shares":      shares,
                    "pnl":         round((exit_price - entry_price) * shares, 4),
                    "pnl_pct":     pnl_pct,
                    "confidence":  buy["confidence"],
                    "exit_reason": t.get("reason", ""),
                    "hold_hours":  round(hold_hours, 2),
                })

        return pairs

    def get_stats(self, csv_path: str = _TRADES_CSV) -> dict:
        """
        Return summary stats as a dict for use by external reporters (e.g. weekly email).

        Keys: completed (int), win_rate (float, percent), avg_hold_hours (float).
        All values are 0 / 0.0 when no completed trades exist.
        """
        trades = self.load_trades(csv_path)
        pairs  = self.pair_trades(trades)
        n = len(pairs)
        if n == 0:
            return {"completed": 0, "win_rate": 0.0, "avg_hold_hours": 0.0}
        wins          = sum(1 for p in pairs if p["pnl_pct"] > 0)
        win_rate      = round(wins / n * 100, 1)
        avg_hold_hours = round(sum(p["hold_hours"] for p in pairs) / n, 1)
        return {"completed": n, "win_rate": win_rate, "avg_hold_hours": avg_hold_hours}

    def generate(self, csv_path: str = _TRADES_CSV) -> str:
        trades = self.load_trades(csv_path)
        pairs  = self.pair_trades(trades)

        n_buys  = sum(1 for t in trades if t.get("side", "").upper() == "BUY")
        n_sells = sum(1 for t in trades if t.get("side", "").upper() == "SELL")

        now_str = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")
        width   = 72
        sep     = "─" * width

        lines = [
            "═" * width,
            "  FAST VALIDATOR — 1H CANDLE REPORT",
            f"  Generated: {now_str}",
            "═" * width,
            "",
            "  ACTIVITY",
            f"  {sep}",
            f"  Total signals:    {len(trades)} ({n_buys} entries, {n_sells} exits)",
            f"  Completed trades: {len(pairs)}",
            "",
        ]

        # ── Per-trade table ───────────────────────────────────────────────
        lines += [
            "  COMPLETED ROUND-TRIPS",
            f"  {sep}",
        ]
        if pairs:
            hdr = (
                f"  {'Symbol':<8} {'Entry':<17} {'Exit':<17}"
                f" {'Entry$':>8} {'Exit$':>8} {'PnL%':>7} {'Hold(h)':>8}  Exit Reason"
            )
            lines.append(hdr)
            lines.append(f"  {sep}")
            for p in pairs:
                result = "WIN" if p["pnl_pct"] > 0 else ("EVEN" if p["pnl_pct"] == 0 else "LOSS")
                lines.append(
                    f"  {p['symbol']:<8} {p['entry_date']:<17} {p['exit_date']:<17}"
                    f" ${p['entry_price']:>7,.3f} ${p['exit_price']:>7,.3f}"
                    f" {p['pnl_pct']:>+6.2f}% {p['hold_hours']:>7.1f}h"
                    f"  {p['exit_reason'][:20]:<20}  {result}"
                )
        else:
            lines.append("  No completed round-trips yet.")
        lines.append("")

        # ── Summary stats ─────────────────────────────────────────────────
        lines += [
            "  SUMMARY STATS",
            f"  {sep}",
        ]
        n = len(pairs)
        if n > 0:
            wins      = [p for p in pairs if p["pnl_pct"] > 0]
            losses    = [p for p in pairs if p["pnl_pct"] < 0]
            win_rate  = len(wins) / n * 100
            gross_win = sum(p["pnl"] for p in wins)
            gross_los = abs(sum(p["pnl"] for p in losses))
            pf        = gross_win / gross_los if gross_los > 0 else float("inf")
            avg_hold  = sum(p["hold_hours"] for p in pairs) / n
            best      = max(pairs, key=lambda p: p["pnl_pct"])
            worst     = min(pairs, key=lambda p: p["pnl_pct"])

            lines.append(f"  Completed trades:  {n}")
            lines.append(f"  Win rate:          {win_rate:.1f}%")
            if pf == float("inf"):
                lines.append( "  Profit factor:     ∞ (no losses)")
            else:
                lines.append(f"  Profit factor:     {pf:.2f}")
            lines.append(f"  Avg hold (hours):  {avg_hold:.1f}")
            lines.append(f"  Best trade:        {best['pnl_pct']:+.2f}% ({best['symbol']})")
            lines.append(f"  Worst trade:       {worst['pnl_pct']:+.2f}% ({worst['symbol']})")
        else:
            lines.append("  Completed trades:  0")
            lines.append("  Win rate:          —")
            lines.append("  Profit factor:     —")

        lines.append("")

        # ── Confidence band breakdown ─────────────────────────────────────
        lines += [
            "  CONFIDENCE BAND BREAKDOWN",
            f"  {sep}",
            f"  {'Band':<8} {'Range':<14} {'Trades':>6} {'Win%':>6} {'Avg PnL%':>9} {'Avg Hold(h)':>12}  Verdict",
            f"  {sep}",
        ]

        bands: dict[str, list[dict]] = {"HIGH": [], "MED": [], "LOW": [], "PRE": []}
        for p in pairs:
            bands[self._confidence_band(p["confidence"])].append(p)

        all_pairs: list[dict] = []
        for band_key in ("HIGH", "MED", "LOW", "PRE"):
            ps        = bands[band_key]
            range_str = self._BAND_NAMES[band_key]
            bn        = len(ps)
            if bn == 0:
                lines.append(f"  {band_key:<8} {range_str:<14} {bn:>6} {'—':>6} {'—':>9} {'—':>12}  NO DATA")
                continue
            b_wins   = sum(1 for p in ps if p["pnl_pct"] > 0)
            b_win_p  = b_wins / bn * 100
            b_avg_p  = sum(p["pnl_pct"] for p in ps) / bn
            b_avg_h  = sum(p["hold_hours"] for p in ps) / bn

            if band_key == "PRE":
                verdict = "PRE-TRACKER"
            elif bn < 5:
                verdict = "NEED MORE DATA"
            elif b_win_p >= 55:
                verdict = "EDGE"
            elif b_win_p >= 45:
                verdict = "WEAK"
            else:
                verdict = "NOISE"

            lines.append(
                f"  {band_key:<8} {range_str:<14} {bn:>6} {b_win_p:>5.1f}% {b_avg_p:>+8.1f}% {b_avg_h:>11.1f}h  {verdict}"
            )
            all_pairs.extend(ps)

        n_all = len(all_pairs)
        lines.append(f"  {sep}")
        if n_all > 0:
            wa_wins = sum(1 for p in all_pairs if p["pnl_pct"] > 0)
            wa_wp   = wa_wins / n_all * 100
            wa_ap   = sum(p["pnl_pct"] for p in all_pairs) / n_all
            wa_ah   = sum(p["hold_hours"] for p in all_pairs) / n_all
            lines.append(
                f"  {'TOTAL':<8} {'ALL':<14} {n_all:>6} {wa_wp:>5.1f}% {wa_ap:>+8.1f}% {wa_ah:>11.1f}h"
            )
        else:
            lines.append(f"  {'TOTAL':<8} {'ALL':<14} {'0':>6} {'—':>6} {'—':>9} {'—':>12}")

        lines.append("═" * width)
        lines.append("")

        return "\n".join(lines)
