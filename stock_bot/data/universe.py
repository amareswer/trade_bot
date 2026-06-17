"""
Market universe fetcher — S&P 500 + TSX 60 components, pre-filtered by
volume and 5-day momentum.

get_universe()  → full symbol list from Wikipedia (file-cached for 24 h)
pre_filter()    → batch yf.download(), volume/price filter, top-N by activity
"""
from __future__ import annotations

import io
import json
import logging
import os
import re
import sys
import time

import pandas as pd
import requests
import yfinance as yf

from stock_bot.data.ipo_tracker import IPOTracker

logger = logging.getLogger(__name__)

_CACHE_FILE = os.path.join(os.path.dirname(os.path.dirname(__file__)), "universe_cache.json")
_SP500_URL  = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"
_TSX60_URL  = "https://en.wikipedia.org/wiki/S%26P/TSX_60"
_USER_AGENT = "Mozilla/5.0 (compatible; StockBot/1.0)"

_MIN_AVG_VOLUME = 500_000
_MIN_PRICE      = 1.00
_BATCH_SIZE     = 50

_FALLBACK_SYMBOLS: list[str] = [
    "AAPL", "MSFT", "GOOGL", "AMZN", "NVDA", "META", "TSLA", "JPM",
    "V",    "WMT",  "JNJ",   "PG",   "UNH",  "MA",   "HD",
    "BAC",  "XOM",  "AVGO",  "SHOP.TO", "RY.TO",
]

_TSX_BROKEN_PATTERNS: list[str] = [
    r'\.[A-Z]\.TO$',    # unconverted single-letter class: RCI.B.TO, CCL.B.TO
    r'-[A-Z]\.TO$',     # converted single-letter class:   RCI-B.TO, CCL-B.TO
    r'-[A-Z]{2,}\.TO$', # converted multi-letter class:    BIP-UN.TO
    r'\.UN\.TO$',       # unconverted income trusts:        BIP.UN.TO
    r'\.PR\.',          # unconverted preferred shares
    r'-PR\.',           # converted preferred shares
]


def _is_valid_tsx_symbol(symbol: str) -> bool:
    """Return False for TSX symbols Yahoo Finance cannot handle."""
    return not any(re.search(p, symbol) for p in _TSX_BROKEN_PATTERNS)


class StockUniverse:
    def __init__(self, refresh_hours: int = 24) -> None:
        self._refresh_hours = refresh_hours

    # ── Public API ────────────────────────────────────────────────────────────

    def get_universe(self) -> list[str]:
        """Return full symbol list (S&P 500 + TSX 60), using file cache when fresh."""
        cached = self._load_cache()
        if cached is not None:
            logger.info("Universe loaded from cache (%d symbols)", len(cached))
            return cached

        symbols: list[str] = []
        symbols += self._fetch_sp500()
        symbols += self._fetch_tsx60()

        ipo_symbols = IPOTracker().get_recent_ipos()
        print(f"IPO tracker adding: {ipo_symbols}")
        symbols = ipo_symbols + symbols  # prepend so they survive the top-N cut

        if not symbols:
            logger.warning("Universe fetch failed — using hardcoded fallback list")
            return _FALLBACK_SYMBOLS[:]

        seen: set[str] = set()
        unique: list[str] = []
        for s in symbols:
            if s not in seen:
                seen.add(s)
                unique.append(s)

        self._save_cache(unique)
        logger.info("Universe fetched: %d symbols", len(unique))
        return unique

    def pre_filter(self, symbols: list[str], n: int = 20) -> list[str]:
        """
        Batch-download 5-day OHLCV, filter by avg volume and price, rank by
        volume × 5d price change, return top n.
        """
        logger.info("Pre-filtering %d symbols → top %d", len(symbols), n)
        metrics = self._batch_metrics(symbols)

        candidates = {
            sym: m for sym, m in metrics.items()
            if m["avg_volume"] >= _MIN_AVG_VOLUME and m["price"] >= _MIN_PRICE
        }
        logger.info("%d symbols passed volume/price filter", len(candidates))

        ranked = sorted(
            candidates.items(),
            key=lambda kv: kv[1]["avg_volume"] * kv[1]["price_change_5d"],
            reverse=True,
        )

        result = [sym for sym, _ in ranked[:n]]
        logger.info("Top movers selected: %s", ", ".join(result))
        return result if result else _FALLBACK_SYMBOLS[:n]

    # ── Wikipedia fetchers ────────────────────────────────────────────────────

    def _fetch_sp500(self) -> list[str]:
        try:
            resp = requests.get(_SP500_URL, timeout=15, headers={"User-Agent": _USER_AGENT})
            resp.raise_for_status()
            tables = pd.read_html(io.StringIO(resp.text))
            for t in tables:
                for col in ("Symbol", "Ticker", "Ticker symbol"):
                    if col in t.columns:
                        raw = t[col].dropna().astype(str).tolist()
                        # yfinance uses "-" not "." for BRK.B etc.
                        symbols = [s.replace(".", "-") for s in raw]
                        logger.info("S&P 500: %d symbols fetched", len(symbols))
                        return symbols
        except Exception as exc:
            logger.warning("S&P 500 Wikipedia fetch failed: %s", exc)
        return []

    def _fetch_tsx60(self) -> list[str]:
        try:
            resp = requests.get(_TSX60_URL, timeout=15, headers={"User-Agent": _USER_AGENT})
            resp.raise_for_status()
            tables = pd.read_html(io.StringIO(resp.text))
            for t in tables:
                for col in ("Ticker", "Symbol", "Ticker symbol"):
                    if col in t.columns:
                        raw = t[col].dropna().astype(str).tolist()
                        symbols = []
                        for s in raw:
                            s = s.strip()
                            if not s.endswith(".TO"):
                                s = s + ".TO"
                            s = s.replace(".UN.TO", "-UN.TO")
                            s = s.replace(".B.TO",  "-B.TO")
                            s = s.replace(".A.TO",  "-A.TO")
                            s = s.replace(".PR.",   "-PR.")
                            symbols.append(s)
                        symbols = [s for s in symbols if _is_valid_tsx_symbol(s)]
                        logger.info("TSX 60: %d symbols fetched", len(symbols))
                        return symbols
        except Exception as exc:
            logger.warning("TSX 60 Wikipedia fetch failed: %s", exc)
        return []

    # ── yfinance batch download ───────────────────────────────────────────────

    def _batch_metrics(self, symbols: list[str]) -> dict[str, dict]:
        """Return {symbol: {price, avg_volume, price_change_5d}} for valid symbols."""
        result: dict[str, dict] = {}

        for i in range(0, len(symbols), _BATCH_SIZE):
            batch = symbols[i : i + _BATCH_SIZE]
            try:
                devnull = open(os.devnull, "w")
                old_stdout, old_stderr = sys.stdout, sys.stderr
                sys.stdout = devnull
                sys.stderr = devnull
                try:
                    data = yf.download(
                        batch,
                        period      = "5d",
                        interval    = "1d",
                        auto_adjust = True,
                        progress    = False,
                    )
                finally:
                    sys.stdout = old_stdout
                    sys.stderr = old_stderr
                    devnull.close()
                if data.empty:
                    continue

                # MultiIndex columns when multiple tickers; flat when single
                if isinstance(data.columns, pd.MultiIndex):
                    close_df  = data["Close"]
                    volume_df = data["Volume"]
                else:
                    close_df  = data[["Close"]].rename(columns={"Close": batch[0]})
                    volume_df = data[["Volume"]].rename(columns={"Volume": batch[0]})

                for sym in close_df.columns:
                    closes  = close_df[sym].dropna()
                    volumes = volume_df[sym].dropna()
                    if len(closes) < 2 or volumes.empty:
                        continue
                    price      = float(closes.iloc[-1])
                    avg_vol    = float(volumes.mean())
                    pct_change = float(
                        (closes.iloc[-1] - closes.iloc[0]) / closes.iloc[0]
                    )
                    result[sym] = {
                        "price":           price,
                        "avg_volume":      avg_vol,
                        "price_change_5d": pct_change,
                    }

            except Exception as exc:
                logger.warning(
                    "Batch download failed (batch %d): %s", i // _BATCH_SIZE + 1, exc
                )

        return result

    # ── File cache ────────────────────────────────────────────────────────────

    def _load_cache(self) -> list[str] | None:
        try:
            if not os.path.exists(_CACHE_FILE):
                return None
            with open(_CACHE_FILE) as f:
                cached = json.load(f)
            age_hours = (time.time() - cached["timestamp"]) / 3600
            if age_hours > self._refresh_hours:
                logger.debug("Universe cache expired (%.1f h old)", age_hours)
                return None
            return cached["symbols"]
        except Exception as exc:
            logger.debug("Cache load failed: %s", exc)
            return None

    def _save_cache(self, symbols: list[str]) -> None:
        try:
            with open(_CACHE_FILE, "w") as f:
                json.dump({"timestamp": time.time(), "symbols": symbols}, f)
        except Exception as exc:
            logger.warning("Cache save failed: %s", exc)
