"""
Dynamic universe eligibility screener — paper-only (see bot/dynamic/__init__.py).

Discovers active spot markets on the configured exchange for one or more
quote currencies, and filters them down to a list a paper-trading system
could safely consider, using ONLY information available at scan time (no
future data, no "today's winners" hindsight).

This is deliberately a SEPARATE class from bot.data.crypto_universe.CryptoUniverse
(the momentum top-mover picker the live BTC/CAD + SOL/CAD bot already uses via
UNIVERSE_WHITELIST/get_top_movers). CryptoUniverse is small, already tested, and
wired into the live/paused bot — this module does not touch it or change its
behavior. DynamicUniverseScreener is new, broader, paper-mode-only, and is used
exclusively by dynamic_universe_bot.py / dynamic_universe_backtest.py.

Filters applied, in order (cheapest/no-extra-network-call first):
    1. market must be active + spot (not a future/swap/option)
    2. base must not be a configured stablecoin (DYNAMIC_EXCLUDE_BASES)
    3. base must not look like a leveraged/derivative token (regex)
    4. 24h quote volume >= DYNAMIC_MIN_QUOTE_VOLUME
    5. exchange minimum order size must fit inside the caller's slot_cash
    6. top-of-book spread <= DYNAMIC_MAX_SPREAD_PCT           (1 extra call/symbol)
    7. order-book depth within DYNAMIC_DEPTH_BAND_PCT of mid  (same call as #6)
    8. at least DYNAMIC_MIN_HISTORY_CANDLES of OHLCV history  (1 extra call/symbol)
    9. duplicate-base dedup — same base under >1 quote currency keeps only the
       highest-volume listing (a no-op with a single configured quote, which is
       today's CAD-only default; matters once DYNAMIC_QUOTE_CURRENCIES grows)

Steps 6-8 only run on the subset that already passed 1-5 and is within
DYNAMIC_MAX_CANDIDATES by volume rank, to bound network calls.

Fail-safe on discovery failure (load_markets()/fetch_tickers() raising, or
returning nothing): fall back to the last cached eligible list, marked
`stale=True`. If there is no usable cache either (never written, or older
than DYNAMIC_CACHE_MAX_AGE_HOURS), return an EMPTY eligible list — this
module never invents an arbitrary "buy this coin" fallback. An empty eligible
list means "admit no new positions this cycle"; it must never be read as
"buy whatever's first" by a caller.
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

_CACHE_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "logs", "dynamic_universe_cache.json",
)

# Matches common leveraged/derivative token naming conventions across
# exchanges (Kraken doesn't list these today, but ccxt's market list is
# exchange-configurable, so this stays a real filter, not dead code).
_LEVERAGED_TOKEN_RE = re.compile(
    r"(\d[LS]$)|(UP$)|(DOWN$)|(BULL)|(BEAR)",
    re.IGNORECASE,
)


@dataclass
class CandidateResult:
    """One symbol's eligibility verdict, with enough detail to explain a
    rejection on the dashboard/report — never just a bare True/False."""
    symbol:        str
    base:          str
    quote:         str
    eligible:      bool
    reasons:       list[str] = field(default_factory=list)   # rejection reasons; empty if eligible
    quote_volume:  float | None = None
    spread_pct:    float | None = None
    depth_quote:   float | None = None   # min(bid_depth, ask_depth) within the band
    history_candles: int | None = None
    min_order_quote: float | None = None  # exchange minimum order cost, in quote currency


@dataclass
class ScreenResult:
    eligible:   list[CandidateResult]
    rejected:   list[CandidateResult]
    scanned_at: float
    stale:      bool = False   # True when this came from cache after a discovery failure

    @property
    def eligible_symbols(self) -> list[str]:
        return [c.symbol for c in self.eligible]

    def all_candidates(self) -> list[CandidateResult]:
        """Eligible + rejected, for a dashboard/report table."""
        return self.eligible + self.rejected


class DynamicUniverseScreener:
    def __init__(self, cfg_dynamic, cache_path: str = _CACHE_PATH):
        self._cfg = cfg_dynamic
        self._cache_path = cache_path

    # ── Public API ───────────────────────────────────────────────────────

    def discover(self, exchange, slot_cash: float) -> ScreenResult:
        """
        Scan every configured quote currency's spot markets on `exchange`
        and return the combined, deduplicated ScreenResult.

        slot_cash: the CASH a single position slot would actually have to
        spend, used to check the exchange minimum order size is reachable —
        a candidate whose minimum order costs more than the slot ever holds
        is not tradeable no matter how liquid it is.
        """
        try:
            markets = exchange.load_markets()
        except Exception as exc:
            logger.warning("dynamic universe: load_markets() failed — %s", exc)
            return self._fallback("load_markets() failed: %s" % exc)

        if not markets:
            return self._fallback("load_markets() returned nothing")

        per_quote_results: list[CandidateResult] = []
        for quote in self._cfg.quote_list:
            try:
                per_quote_results.extend(
                    self._screen_quote(exchange, markets, quote, slot_cash)
                )
            except Exception as exc:
                logger.warning("dynamic universe: screening %s failed — %s", quote, exc)
                # One quote currency failing doesn't invalidate the others —
                # but if EVERY quote fails, per_quote_results stays empty and
                # we fall through to the cache/empty-list fail-safe below.

        if not per_quote_results:
            return self._fallback("no candidates produced by any configured quote currency")

        deduped = self._dedup_by_base(per_quote_results)
        eligible = [c for c in deduped if c.eligible]
        rejected = [c for c in deduped if not c.eligible]
        result = ScreenResult(eligible=eligible, rejected=rejected, scanned_at=time.time(), stale=False)
        self._save_cache(result)
        self._append_snapshot_log(result)
        return result

    # ── Per-quote screening ──────────────────────────────────────────────

    def _screen_quote(self, exchange, markets: dict, quote: str, slot_cash: float) -> list[CandidateResult]:
        exclude = self._cfg.exclude_set
        raw_pairs = [
            s for s in markets
            if s.endswith(f"/{quote}")
            and markets[s].get("spot", False)
        ]

        candidates: list[CandidateResult] = []
        cheap_pass: list[tuple[str, dict, float]] = []  # (symbol, market, quote_volume) for stage-2 checks

        for sym in raw_pairs:
            m = markets[sym]
            base = sym.split("/")[0].upper()
            reasons: list[str] = []

            if not m.get("active", False):
                reasons.append("market inactive")
            if base in exclude:
                reasons.append(f"excluded base ({base} is a stablecoin/fiat)")
            if _LEVERAGED_TOKEN_RE.search(base):
                reasons.append(f"looks like a leveraged/derivative token ({base})")

            if reasons:
                candidates.append(CandidateResult(sym, base, quote, eligible=False, reasons=reasons))
                continue

            cheap_pass.append((sym, m, base))

        if not cheap_pass:
            return candidates

        # Volume filter — one fetch_tickers() call for the whole batch.
        try:
            tickers = exchange.fetch_tickers([s for s, _, _ in cheap_pass])
        except Exception as exc:
            logger.warning("dynamic universe: fetch_tickers(%s) failed — %s", quote, exc)
            for sym, m, base in cheap_pass:
                candidates.append(CandidateResult(
                    sym, base, quote, eligible=False,
                    reasons=[f"could not fetch ticker data: {exc}"],
                ))
            return candidates

        volume_pass: list[tuple[str, dict, str, float]] = []
        for sym, m, base in cheap_pass:
            t = tickers.get(sym, {}) or {}
            vol = t.get("quoteVolume") or 0.0
            reasons = []
            if vol < self._cfg.min_quote_volume:
                reasons.append(
                    f"24h quote volume {vol:,.0f} < minimum {self._cfg.min_quote_volume:,.0f}"
                )

            min_order_quote = self._min_order_quote(m, t.get("last"))
            if min_order_quote is not None and slot_cash > 0 and min_order_quote > slot_cash:
                reasons.append(
                    f"exchange minimum order (~{min_order_quote:.2f} {quote}) exceeds "
                    f"slot cash ({slot_cash:.2f} {quote})"
                )

            if reasons:
                candidates.append(CandidateResult(
                    sym, base, quote, eligible=False, reasons=reasons,
                    quote_volume=vol, min_order_quote=min_order_quote,
                ))
            else:
                volume_pass.append((sym, m, base, vol))

        if not volume_pass:
            return candidates

        # Rank by volume and cap to max_candidates before the expensive
        # per-symbol calls (order book + OHLCV history).
        volume_pass.sort(key=lambda x: x[3], reverse=True)
        within_cap = volume_pass[: self._cfg.max_candidates]
        over_cap = volume_pass[self._cfg.max_candidates :]
        for sym, m, base, vol in over_cap:
            candidates.append(CandidateResult(
                sym, base, quote, eligible=False,
                reasons=[f"outside top {self._cfg.max_candidates} by volume this cycle"],
                quote_volume=vol,
            ))

        for sym, m, base, vol in within_cap:
            candidates.append(self._deep_check(exchange, sym, base, quote, m, vol))

        return candidates

    def _deep_check(self, exchange, sym: str, base: str, quote: str, market: dict, vol: float) -> CandidateResult:
        reasons: list[str] = []
        min_order_quote = self._min_order_quote(market, None)
        spread_pct = None
        depth_quote = None
        history_candles = None

        try:
            book = exchange.fetch_order_book(sym)
            bids = book.get("bids") or []
            asks = book.get("asks") or []
            if not bids or not asks:
                reasons.append("empty order book")
            else:
                best_bid, best_ask = bids[0][0], asks[0][0]
                mid = (best_bid + best_ask) / 2.0
                spread_pct = (best_ask - best_bid) / mid if mid > 0 else None
                if spread_pct is not None and spread_pct > self._cfg.max_spread_pct:
                    reasons.append(
                        f"spread {spread_pct * 100:.3f}% > max {self._cfg.max_spread_pct * 100:.3f}%"
                    )
                # ccxt order-book levels are [price, amount, ...] — some
                # exchanges (Kraken included) append a timestamp as a third
                # element, so unpack defensively rather than assuming len==2.
                band = self._cfg.depth_band_pct
                bid_depth = sum(
                    lvl[0] * lvl[1] for lvl in bids if lvl[0] >= mid * (1 - band)
                )
                ask_depth = sum(
                    lvl[0] * lvl[1] for lvl in asks if lvl[0] <= mid * (1 + band)
                )
                depth_quote = min(bid_depth, ask_depth)
                if depth_quote < self._cfg.min_depth_quote:
                    reasons.append(
                        f"order-book depth {depth_quote:,.0f} {quote} within "
                        f"{band * 100:.1f}% of mid < minimum {self._cfg.min_depth_quote:,.0f}"
                    )
        except Exception as exc:
            reasons.append(f"order book fetch failed: {exc}")

        try:
            ohlcv = exchange.fetch_ohlcv(sym, timeframe="4h", limit=self._cfg.min_history_candles)
            history_candles = len(ohlcv) if ohlcv else 0
            if history_candles < self._cfg.min_history_candles:
                reasons.append(
                    f"only {history_candles} candles of history < required "
                    f"{self._cfg.min_history_candles}"
                )
        except Exception as exc:
            reasons.append(f"OHLCV history fetch failed: {exc}")

        return CandidateResult(
            sym, base, quote, eligible=not reasons, reasons=reasons,
            quote_volume=vol, spread_pct=spread_pct, depth_quote=depth_quote,
            history_candles=history_candles, min_order_quote=min_order_quote,
        )

    @staticmethod
    def _min_order_quote(market: dict, last_price: float | None) -> float | None:
        """Best-effort exchange minimum order size, expressed in quote currency."""
        limits = market.get("limits") or {}
        cost_min = (limits.get("cost") or {}).get("min")
        if cost_min is not None:
            return float(cost_min)
        amount_min = (limits.get("amount") or {}).get("min")
        if amount_min is not None and last_price:
            return float(amount_min) * float(last_price)
        return None

    @staticmethod
    def _dedup_by_base(candidates: list[CandidateResult]) -> list[CandidateResult]:
        """
        When the same base appears under more than one quote currency, keep
        only the highest-volume listing as a candidate for a position — the
        others are marked ineligible with a reason, not silently dropped
        (still visible on the dashboard/report as "duplicate exposure").
        No-op today with a single configured quote currency.
        """
        by_base: dict[str, list[CandidateResult]] = {}
        for c in candidates:
            by_base.setdefault(c.base, []).append(c)

        result: list[CandidateResult] = []
        for base, group in by_base.items():
            if len(group) == 1:
                result.append(group[0])
                continue
            eligible_in_group = [c for c in group if c.eligible]
            if not eligible_in_group:
                result.extend(group)
                continue
            eligible_in_group.sort(key=lambda c: c.quote_volume or 0.0, reverse=True)
            keep = eligible_in_group[0]
            result.append(keep)
            for c in eligible_in_group[1:]:
                result.append(CandidateResult(
                    c.symbol, c.base, c.quote, eligible=False,
                    reasons=[f"duplicate base exposure — {keep.symbol} already selected for {base}"],
                    quote_volume=c.quote_volume,
                ))
            for c in group:
                if not c.eligible:
                    result.append(c)
        return result

    # ── Cache / fail-safe ────────────────────────────────────────────────

    def _fallback(self, reason: str) -> ScreenResult:
        logger.warning("dynamic universe: discovery failed (%s) — trying cache", reason)
        cached = self._load_cache()
        if cached is not None:
            logger.warning(
                "dynamic universe: serving cached universe from %.1fh ago (stale)",
                (time.time() - cached.scanned_at) / 3600,
            )
            return cached
        logger.warning(
            "dynamic universe: no usable cache — returning EMPTY eligible list "
            "(block new entries; never falling back to an arbitrary symbol)"
        )
        return ScreenResult(eligible=[], rejected=[], scanned_at=time.time(), stale=True)

    def _append_snapshot_log(self, result: ScreenResult) -> None:
        """
        2026-09-18 review finding (P1-8): a current eligibility list applied
        retroactively across historical backtest windows cannot reconstruct
        which pairs were actually liquid/eligible on any past date —
        exchanges don't expose that after the fact. _save_cache() above only
        ever keeps the LATEST scan (overwritten each call), so even that
        single snapshot is lost once superseded.

        This appends one line per successful scan to a separate,
        never-overwritten JSONL log — every scan this screener ever runs
        going forward is preserved with its own timestamp, so a future
        analysis has REAL point-in-time eligibility to work from instead of
        reconstructing the past from today's list (which the code comment
        atop this module already calls out as impossible for prior dates).
        Best-effort: a write failure here never affects discover()'s return
        value or the fail-safe cache above.
        """
        try:
            path = os.path.join(os.path.dirname(self._cache_path), "dynamic_universe_snapshots.jsonl")
            line = json.dumps({
                "scanned_at": result.scanned_at,
                "eligible":   [c.symbol for c in result.eligible],
                "rejected":   len(result.rejected),
            })
            with open(path, "a") as f:
                f.write(line + "\n")
        except Exception as exc:
            logger.warning("dynamic universe: snapshot log append failed: %s", exc)

    def _save_cache(self, result: ScreenResult) -> None:
        try:
            os.makedirs(os.path.dirname(self._cache_path), exist_ok=True)
            payload = {
                "scanned_at": result.scanned_at,
                "eligible": [c.symbol for c in result.eligible],
            }
            with open(self._cache_path, "w") as f:
                json.dump(payload, f)
        except Exception as exc:
            logger.warning("dynamic universe: cache save failed: %s", exc)

    def _load_cache(self) -> ScreenResult | None:
        try:
            if not os.path.exists(self._cache_path):
                return None
            with open(self._cache_path) as f:
                data = json.load(f)
            age_hours = (time.time() - data["scanned_at"]) / 3600
            if age_hours > self._cfg.cache_max_age_hours:
                logger.warning(
                    "dynamic universe: cache is %.1fh old (> %.1fh max) — "
                    "treating as unusable, not trusting it",
                    age_hours, self._cfg.cache_max_age_hours,
                )
                return None
            eligible = [
                CandidateResult(sym, sym.split("/")[0], sym.split("/")[1], eligible=True)
                for sym in data.get("eligible", [])
            ]
            return ScreenResult(eligible=eligible, rejected=[], scanned_at=data["scanned_at"], stale=True)
        except Exception as exc:
            logger.debug("dynamic universe: cache load failed: %s", exc)
            return None
