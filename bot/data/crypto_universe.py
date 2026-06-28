"""
Scan all CAD pairs on an exchange, rank by momentum, return top movers.

Usage:
    import ccxt
    from bot.data.crypto_universe import CryptoUniverse

    ex = ccxt.kraken({"apiKey": ..., "secret": ...})
    symbols = CryptoUniverse().get_top_movers(ex, n=5)
"""

import json
import logging
import os
from typing import List, Optional

from config import cfg

logger = logging.getLogger(__name__)

_REGISTRY_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "config", "approved_symbols.json",
)


def load_approved_symbols() -> List[str]:
    """
    Return the list of approved symbols from config/approved_symbols.json,
    or an empty list if the file doesn't exist or has no approved entries.
    """
    if not os.path.exists(_REGISTRY_PATH):
        return []
    try:
        with open(_REGISTRY_PATH) as f:
            data = json.load(f)
        approved = [e["symbol"] for e in data.get("approved", []) if e.get("symbol")]
        return approved
    except Exception as exc:
        logger.warning("Failed to load approved_symbols.json: %s", exc)
        return []


class CryptoUniverse:
    def get_top_movers(
        self,
        exchange,
        n: int = 5,
        quote: Optional[str] = None,
        min_quote_vol: Optional[float] = None,
    ) -> List[str]:
        """
        Return the approved symbol list.

        When UNIVERSE_WHITELIST is set (comma-separated), returns those symbols
        directly without hitting the exchange for a momentum scan — the whitelist
        represents pre-validated pairs (walk-forward passed + liquidity confirmed).

        Otherwise: scan all quote-currency pairs on exchange, rank by momentum
        score, return top n symbols. Score = quoteVolume × percentage.
        Fills remaining slots with highest-volume pairs when fewer than n
        positive movers exist. Never returns empty — fallback is [f'ETH/{quote}'].
        """
        quote = (quote or cfg.universe.universe_quote).upper()

        # Priority 1: config/approved_symbols.json (managed by universe_manager.py)
        registry_symbols = load_approved_symbols()
        if registry_symbols:
            logger.info("universe: registry mode — %d symbols: %s", len(registry_symbols), registry_symbols)
            print(f"[UNIVERSE] Registry (approved_symbols.json): {registry_symbols}", flush=True)
            return registry_symbols

        # Priority 2: UNIVERSE_WHITELIST in .env (manual override)
        whitelist_raw = cfg.universe.universe_whitelist.strip()
        if whitelist_raw:
            symbols = [s.strip() for s in whitelist_raw.split(",") if s.strip()]
            logger.info("universe: whitelist mode — %d symbols: %s", len(symbols), symbols)
            print(f"[UNIVERSE] Whitelist (.env): {symbols}", flush=True)
            return symbols

        min_quote_vol = min_quote_vol if min_quote_vol is not None else cfg.universe.min_vol
        exclude_set = {
            s.strip().upper()
            for s in cfg.universe.universe_exclude.split(",")
            if s.strip()
        }
        fallback = [f"ETH/{quote}"]

        try:
            markets = exchange.load_markets()
        except Exception as exc:
            logger.warning("universe: load_markets() failed — %s", exc)
            return fallback

        pairs = [
            s for s in markets
            if s.endswith(f"/{quote}")
            and markets[s].get("active", False)
            and markets[s].get("spot", False)
            and s.split("/")[0] not in exclude_set
        ]
        logger.info("universe: %d crypto %s pairs after stablecoin filter", len(pairs), quote)
        print(f"[UNIVERSE] Scanning {len(pairs)} {quote} crypto pairs …", flush=True)

        if not pairs:
            logger.warning("universe: no active %s spot pairs found", quote)
            return fallback

        try:
            tickers = exchange.fetch_tickers(pairs)
        except Exception as exc:
            logger.warning("universe: fetch_tickers() failed — %s", exc)
            return fallback

        scored: List[tuple] = []   # (score, volume, symbol)
        for sym in pairs:
            t = tickers.get(sym, {})
            pct = t.get("percentage")
            vol = t.get("quoteVolume") or 0.0
            last = t.get("last")

            if last is None or vol < min_quote_vol:
                continue

            score = vol * pct if (pct is not None and pct > 0) else 0.0
            logger.info(
                "universe: %s  vol=%.0f  chg=%s%%  score=%.0f",
                sym,
                vol,
                f"{pct:.2f}" if pct is not None else "N/A",
                score,
            )
            scored.append((score, vol, sym))

        if not scored:
            logger.warning("universe: no pairs passed volume filter — using fallback")
            return fallback

        # Sort by descending score; ties broken by descending volume
        scored.sort(key=lambda x: (x[0], x[1]), reverse=True)

        # First pass: positive momentum
        positive = [sym for score, vol, sym in scored if score > 0]
        result = positive[:n]

        # Fill remaining slots with highest-volume pairs regardless of direction
        if len(result) < n:
            by_volume = [sym for _, _, sym in scored if sym not in result]
            result.extend(by_volume[: n - len(result)])

        if not result:
            return fallback

        logger.info("universe top %d: %s", n, result)
        print(f"[UNIVERSE] Top {n}: {result}", flush=True)
        return result
