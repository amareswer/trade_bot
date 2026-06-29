"""
Market universe fetcher — S&P 500, NASDAQ-100, S&P 400, TSX 60, TSX Composite,
and user-configured ETFs; pre-filtered by volume and composite momentum score.

get_universe()  → full symbol list from Wikipedia (file-cached per UNIVERSE_REFRESH_HOURS)
pre_filter()    → batch yf.download(), volume/price/score filter, top-N by activity

All thresholds and weights come from cfg (populated from .env).
No strategy values are hardcoded in this file.
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
from yfinance.exceptions import YFRateLimitError

from stock_bot.data.ipo_tracker import IPOTracker

logger = logging.getLogger(__name__)

_CACHE_FILE   = os.path.join(os.path.dirname(os.path.dirname(__file__)), "universe_cache.json")
_SP500_URL    = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"
_TSX60_URL    = "https://en.wikipedia.org/wiki/S%26P/TSX_60"
_NASDAQ100_URL = "https://en.wikipedia.org/wiki/Nasdaq-100"
_SP400_URL    = "https://en.wikipedia.org/wiki/List_of_S%26P_400_companies"
_TSX_COMP_URL = "https://en.wikipedia.org/wiki/S%26P/TSX_Composite_Index"
_USER_AGENT   = "Mozilla/5.0 (compatible; StockBot/1.0)"

_BATCH_SIZE  = int(os.getenv("UNIVERSE_BATCH_SIZE",  "25"))
_BATCH_DELAY = float(os.getenv("UNIVERSE_BATCH_DELAY", "2.0"))

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
    def __init__(self, cfg=None, refresh_hours: int = 24) -> None:
        self._refresh_hours = refresh_hours
        self._cfg = cfg

    # ── Public API ────────────────────────────────────────────────────────────

    def get_universe(self) -> list[str]:
        """Return full symbol list from configured sources, using file cache when fresh."""
        cached = self._load_cache()
        if cached is not None:
            logger.info("Universe loaded from cache (%d symbols)", len(cached))
            return cached

        sources_raw = getattr(self._cfg, 'universe_sources',
                              'sp500,nasdaq100,sp400,tsx60,tsx_composite,etfs')
        enabled_sources = {s.strip().lower() for s in sources_raw.split(',')}

        source_map = {
            'sp500':          self._fetch_sp500,
            'nasdaq100':      self._fetch_nasdaq100,
            'sp400':          self._fetch_sp400,
            'tsx60':          self._fetch_tsx60,
            'tsx_composite':  self._fetch_tsx_composite,
            'etfs':           self._fetch_etfs_from_config,
        }

        symbols: list[str] = []
        for source_name, fetcher in source_map.items():
            if source_name in enabled_sources:
                fetched = fetcher()
                logger.info("Source '%s': %d symbols", source_name, len(fetched))
                symbols += fetched
            else:
                logger.info("Source '%s': DISABLED (not in UNIVERSE_SOURCES)", source_name)

        ipo_symbols = IPOTracker().get_recent_ipos()
        if ipo_symbols:
            logger.info("IPO tracker adding: %s", ipo_symbols)
        symbols = ipo_symbols + symbols  # prepend so IPOs survive the top-N cut

        if not symbols:
            logger.warning("All universe fetches failed — using fallback list")
            return _FALLBACK_SYMBOLS[:]

        seen: set[str] = set()
        unique: list[str] = []
        for s in symbols:
            if s not in seen:
                seen.add(s)
                unique.append(s)

        self._save_cache(unique)
        logger.info("Universe total: %d unique symbols from sources: %s",
                    len(unique), ', '.join(sorted(enabled_sources)))
        return unique

    def pre_filter(self, symbols: list[str], n: int = 20, market_status: dict = None) -> list[str]:
        """
        Batch-download 30-day OHLCV, filter by avg volume, price, and composite
        momentum score, return top n ranked by score.

        All thresholds and weights come from cfg / .env — nothing hardcoded here.

        If market_status is provided, only symbols from currently open markets
        are considered — US closed drops S&P500 symbols, CA closed drops .TO symbols.
        """
        if market_status is not None:
            us_open = market_status.get("us_open", True)
            ca_open = market_status.get("ca_open", True)
            eligible = []
            for s in symbols:
                is_canadian = s.endswith(".TO")
                if is_canadian and ca_open:
                    eligible.append(s)
                elif not is_canadian and us_open:
                    eligible.append(s)
            if not eligible:
                logger.warning("No eligible symbols — all markets closed")
                return []
            symbols = eligible

        logger.info("Pre-filtering %d symbols → top %d", len(symbols), n)
        metrics = self._batch_metrics(symbols)

        min_avg_volume = getattr(self._cfg, 'universe_min_avg_volume', 300_000)
        min_price      = getattr(self._cfg, 'universe_min_price',      1.0)
        min_score      = getattr(self._cfg, 'universe_min_score',      0.001)

        for sym, m in metrics.items():
            passes = (
                m["avg_volume"] >= min_avg_volume
                and m["price"]  >= min_price
                and m["score"]  >  min_score
            )
            logger.debug(
                "SCREENER %s: 1d=%.2f%% 5d=%.2f%% vol_ratio=%.1fx score=%.4f %s",
                sym, m["change_1d"] * 100, m["change_5d"] * 100,
                m["volume_ratio"], m["score"],
                "PASS" if passes else "FAIL",
            )

        candidates = {
            sym: m for sym, m in metrics.items()
            if m["avg_volume"] >= min_avg_volume
            and m["price"]     >= min_price
            and m["score"]     >  min_score
        }
        logger.info("%d symbols passed volume/price/momentum filter", len(candidates))

        ranked = sorted(
            candidates.items(),
            key=lambda kv: kv[1]["score"],
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
                        symbols = [s.replace(".", "-") for s in raw]
                        logger.info("S&P 500: %d symbols fetched", len(symbols))
                        return symbols
        except Exception as exc:
            logger.warning("S&P 500 Wikipedia fetch failed: %s", exc)
        return []

    def _fetch_nasdaq100(self) -> list[str]:
        try:
            resp = requests.get(_NASDAQ100_URL, timeout=15,
                                headers={"User-Agent": _USER_AGENT})
            resp.raise_for_status()
            tables = pd.read_html(io.StringIO(resp.text))
            for t in tables:
                for col in ("Ticker", "Symbol", "Ticker symbol"):
                    if col in t.columns:
                        raw = t[col].dropna().astype(str).tolist()
                        symbols = [s.strip().replace(".", "-") for s in raw
                                   if s.strip() and not s.startswith("^")]
                        if len(symbols) >= 50:
                            logger.info("NASDAQ-100: %d symbols", len(symbols))
                            return symbols
        except Exception as exc:
            logger.warning("NASDAQ-100 fetch failed: %s", exc)
        return []

    def _fetch_sp400(self) -> list[str]:
        try:
            resp = requests.get(_SP400_URL, timeout=15,
                                headers={"User-Agent": _USER_AGENT})
            resp.raise_for_status()
            tables = pd.read_html(io.StringIO(resp.text))
            for t in tables:
                for col in ("Ticker", "Symbol", "Ticker symbol"):
                    if col in t.columns:
                        raw = t[col].dropna().astype(str).tolist()
                        symbols = [s.strip().replace(".", "-") for s in raw
                                   if s.strip() and not s.startswith("^")]
                        if len(symbols) >= 100:
                            logger.info("S&P 400: %d symbols", len(symbols))
                            return symbols
        except Exception as exc:
            logger.warning("S&P 400 fetch failed: %s", exc)
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

    def _fetch_tsx_composite(self) -> list[str]:
        try:
            resp = requests.get(_TSX_COMP_URL, timeout=15,
                                headers={"User-Agent": _USER_AGENT})
            resp.raise_for_status()
            tables = pd.read_html(io.StringIO(resp.text))
            for t in tables:
                for col in ("Ticker", "Symbol", "Ticker symbol"):
                    if col in t.columns:
                        raw = t[col].dropna().astype(str).tolist()
                        symbols = []
                        for s in raw:
                            s = s.strip()
                            if not s or s.startswith("^"):
                                continue
                            if not s.endswith(".TO"):
                                s = s + ".TO"
                            s = s.replace(".UN.TO", "-UN.TO")
                            s = s.replace(".B.TO",  "-B.TO")
                            s = s.replace(".A.TO",  "-A.TO")
                            s = s.replace(".PR.",   "-PR.")
                            if _is_valid_tsx_symbol(s):
                                symbols.append(s)
                        if len(symbols) >= 30:
                            logger.info("TSX Composite: %d symbols", len(symbols))
                            return symbols
        except Exception as exc:
            logger.warning("TSX Composite fetch failed: %s", exc)
        return []

    def _fetch_etfs_from_config(self) -> list[str]:
        """Returns the ETF list from UNIVERSE_ETFS in .env — no hardcoded values."""
        etfs_raw = getattr(self._cfg, 'universe_etfs', '')
        if not etfs_raw:
            logger.info("UNIVERSE_ETFS not set — ETF source skipped")
            return []
        etfs = [s.strip().upper() for s in etfs_raw.split(',') if s.strip()]
        logger.info("ETFs from config: %d symbols", len(etfs))
        return etfs

    # ── yfinance batch download ───────────────────────────────────────────────

    def _batch_metrics(self, symbols: list[str]) -> dict[str, dict]:
        """
        Return {symbol: {price, avg_volume, change_1d, change_5d, volume_ratio,
        momentum_score, score}} for valid symbols.

        Scoring weights and SPY benchmark come from cfg / .env.
        Fetches 30 calendar days (~22 trading days) to compute 20-day volume
        average, 1-day price change, and 5-day price change.
        """
        w_vol    = getattr(self._cfg, 'universe_weight_volume', 0.35)
        w_mom5d  = getattr(self._cfg, 'universe_weight_mom5d',  0.30)
        w_mom1d  = getattr(self._cfg, 'universe_weight_mom1d',  0.20)
        w_relstr = getattr(self._cfg, 'universe_weight_relstr', 0.15)

        # SPY benchmark for relative strength — fetched once, reused for all symbols
        spy_5d_return = 0.0
        try:
            spy_raw = yf.download("SPY", period="10d", interval="1d",
                                  auto_adjust=True, progress=False)
            if isinstance(spy_raw.columns, pd.MultiIndex):
                spy_raw.columns = spy_raw.columns.get_level_values(0)
            spy_closes = spy_raw["Close"].dropna().tolist()
            if len(spy_closes) >= 6:
                spy_5d_return = (spy_closes[-1] - spy_closes[-6]) / spy_closes[-6]
                logger.debug("SPY 5d return: %.3f%%", spy_5d_return * 100)
        except Exception as exc:
            logger.debug("SPY benchmark fetch failed (non-fatal): %s", exc)

        result: dict[str, dict] = {}

        for i in range(0, len(symbols), _BATCH_SIZE):
            batch   = symbols[i : i + _BATCH_SIZE]
            batch_n = i // _BATCH_SIZE + 1

            if i > 0:
                time.sleep(_BATCH_DELAY)

            data = None
            for attempt in range(2):
                devnull = open(os.devnull, "w")
                old_stdout, old_stderr = sys.stdout, sys.stderr
                sys.stdout = devnull
                sys.stderr = devnull
                try:
                    data = yf.download(
                        batch,
                        period      = "30d",
                        interval    = "1d",
                        auto_adjust = True,
                        progress    = False,
                    )
                except YFRateLimitError:
                    if attempt == 0:
                        logger.warning(
                            "Universe: rate limited on batch %d, retrying in 30s", batch_n
                        )
                        time.sleep(30)
                    else:
                        logger.warning(
                            "Universe: batch %d failed after retry, skipping", batch_n
                        )
                    data = None
                except Exception as exc:
                    logger.warning("Batch download failed (batch %d): %s", batch_n, exc)
                    data = None
                    break
                finally:
                    sys.stdout = old_stdout
                    sys.stderr = old_stderr
                    devnull.close()

                if data is not None:
                    break

            if data is None or data.empty:
                continue

            if isinstance(data.columns, pd.MultiIndex):
                close_df  = data["Close"]
                volume_df = data["Volume"]
            else:
                close_df  = data[["Close"]].rename(columns={"Close": batch[0]})
                volume_df = data[["Volume"]].rename(columns={"Volume": batch[0]})

            for sym in close_df.columns:
                closes  = close_df[sym].dropna()
                volumes = volume_df[sym].dropna()
                if len(closes) < 7 or volumes.empty:
                    continue

                close    = float(closes.iloc[-1])
                close_1d = float(closes.iloc[-2])
                close_5d = float(closes.iloc[-6])

                volume     = float(volumes.iloc[-1])
                avg_vol_20 = float(volumes.iloc[-20:].mean()) if len(volumes) >= 20 else float(volumes.mean())

                change_1d    = (close - close_1d) / close_1d if close_1d > 0 else 0.0
                change_5d    = (close - close_5d) / close_5d if close_5d > 0 else 0.0
                volume_ratio = volume / avg_vol_20 if avg_vol_20 > 0 else 1.0

                vol_surge    = min(volume_ratio, 10.0)
                mom_5d       = abs(change_5d)
                mom_1d       = abs(change_1d)
                rel_strength = abs(change_5d - spy_5d_return)

                score = (
                    w_vol    * vol_surge
                  + w_mom5d  * mom_5d
                  + w_mom1d  * mom_1d
                  + w_relstr * rel_strength
                )

                result[sym] = {
                    "price":          close,
                    "avg_volume":     avg_vol_20,
                    "change_1d":      change_1d,
                    "change_5d":      change_5d,
                    "volume_ratio":   volume_ratio,
                    "momentum_score": (w_mom5d * mom_5d + w_mom1d * mom_1d),
                    "score":          score,
                }

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
