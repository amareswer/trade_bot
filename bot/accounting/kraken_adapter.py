"""
Real ccxt.kraken implementation of bot/accounting/engine.py's ExchangeAdapter
protocol.

Every CCXT/Kraken claim below was verified directly against the installed
ccxt==4.5.56 source in this repo's .venv (inspect.getsource), the same
discipline CRYPTO_BOT_EXECUTION_ACCOUNTING_DESIGN_2026-09-19.md §1 used —
not assumed or carried over from a prior draft.

── Why this is a custom adapter, not a call to ex.fetch_my_trades() ────────
ccxt.kraken.fetch_my_trades() calls privatePostTradesHistory exactly ONCE
and returns whatever that single response contains. Kraken's raw response
carries `result.count` (the account-WIDE total matching record count) and
accepts an `ofs` request parameter for paging through it — ccxt's unified
wrapper discards `count` entirely and never drives an `ofs` loop. Design §1
established this from source; this adapter is the paginated loop the design
says is required for any caller needing guaranteed-complete coverage.

── The `symbol` parameter is informational only, not a server-side filter ──
Kraken's `TradesHistory` endpoint (privatePostTradesHistory) has NO pair/
symbol request parameter at all — confirmed by inspecting
ccxt.kraken.fetch_my_trades's own `request` dict construction, which never
adds one. Every page returned is account-wide, across every traded symbol.
Passing a `market` into ccxt's `parse_trades(trades, market, ...)` would
make it filter the OUTPUT to that one symbol via `filter_by_symbol_since_
limit` (confirmed via inspect.getsource(ccxt.Exchange.parse_trades_helper))
— but that would silently break the coverage-proof arithmetic, because
Kraken's own `count` counts every symbol's trades, not just the requested
one: filtering post-hoc would make `len(trades) == count` false for any
account with more than one traded symbol, permanently reporting incomplete
coverage. This adapter therefore ALWAYS retrieves and proves coverage over
the FULL account-wide page (market=None passed to parse_trades — ccxt then
derives each trade's own symbol from its `pair` field via safe_symbol), and
`fetch_my_trades_page`'s `symbol` argument only filters the RETURNED
`.trades` list after the account-wide totals have already been computed
against the unfiltered page. Callers that need a genuine completeness proof
must call with `symbol=None` and bucket the result themselves (this is
exactly what reconciliation.py does) — calling with a specific `symbol` is
only useful for the audit / straggler-matching paths, which don't rely on
`reported_total` meaning anything symbol-scoped.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from bot.accounting.engine import LedgerMovement, TradePage
from bot.accounting.store import ObservedTrade


def _ms_from_iso(iso: Optional[str]) -> Optional[int]:
    if iso is None:
        return None
    dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def _iso_from_ms(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class KrakenAccountingAdapter:
    """Wraps an already-constructed, already-`load_markets()`-ed
    `ccxt.kraken` instance. Never constructs its own exchange object (the
    caller owns API keys / rate limiting / the single shared instance)."""

    def __init__(self, exchange) -> None:
        self._ex = exchange

    # -- trades --------------------------------------------------------

    def fetch_my_trades_page(
        self, symbol: Optional[str], *, since: Optional[str], offset: int, limit: int,
    ) -> TradePage:
        """One page of privatePostTradesHistory, with Kraken's own
        `ofs`/`count` preserved (see module docstring for why the unified
        ccxt.fetch_my_trades cannot be used here). `limit` here caps how
        many of THIS page's records this call keeps (mirrors the reference
        model's page_size semantics) — Kraken itself does not accept a
        page-size request parameter for this endpoint (confirmed: the
        commented-out example in ccxt's source lists only start/end/ofs/
        type/trades), so a full page is always fetched from Kraken and
        trimmed client-side to `limit`, with `next_offset` computed from the
        true `count` so pagination still advances correctly regardless of
        how a caller chose to slice it.
        """
        ex = self._ex
        request: dict = {"ofs": offset}
        since_ms = _ms_from_iso(since)
        if since_ms is not None:
            # Kraken's own documented `start` semantics: "starting unix
            # timestamp... (exclusive)" — matches ccxt's own
            # fetch_my_trades construction (design §1).
            request["start"] = ex.parse_to_int(since_ms / 1000)
        response = ex.privatePostTradesHistory(ex.extend(request, {}))
        result = response.get("result", {})
        raw_trades = result.get("trades", {}) or {}
        count = int(result.get("count", 0) or 0)
        ids = list(raw_trades.keys())
        for tid in ids:
            raw_trades[tid]["id"] = tid
        # market=None deliberately (see module docstring) — parse every
        # trade regardless of pair, so the coverage count stays account-wide
        # and correct.
        parsed = ex.parse_trades(raw_trades, None, None, None)
        parsed.sort(key=lambda t: (t.get("timestamp") or 0, t.get("id") or ""))
        page = parsed[:limit]
        fetched_so_far_end = offset + len(page)
        next_offset = fetched_so_far_end if fetched_so_far_end < count else None
        observed = [_to_observed_trade(t) for t in page]
        if symbol is not None:
            observed = [t for t in observed if t.symbol == symbol]
        return TradePage(trades=observed, reported_total=count, next_offset=next_offset)

    # -- balances / movements -------------------------------------------

    def fetch_balance_total(self, asset: str) -> float:
        bal = self._ex.fetch_balance()
        total = bal.get("total", {}) or {}
        return float(total.get(asset, 0.0) or 0.0)

    def fetch_deposits(self, asset: str, *, since: Optional[str]) -> "list[LedgerMovement]":
        since_ms = _ms_from_iso(since)
        raw = self._ex.fetch_deposits(code=asset, since=since_ms) or []
        return [_to_movement(r, "deposit") for r in raw]

    def fetch_withdrawals(self, asset: str, *, since: Optional[str]) -> "list[LedgerMovement]":
        since_ms = _ms_from_iso(since)
        raw = self._ex.fetch_withdrawals(code=asset, since=since_ms) or []
        return [_to_movement(r, "withdrawal") for r in raw]

    # -- precision (design §5 — market metadata, never a flat epsilon) --

    def amount_tolerance(self, symbol: str) -> float:
        """Half of one amount increment for `symbol`, per design §5.
        ccxt.kraken uses TICK_SIZE precision mode (confirmed:
        `ccxt.kraken().precisionMode == ccxt.TICK_SIZE`) — `precision.amount`
        is already the real minimum increment, not a decimal-place count."""
        market = self._ex.markets.get(symbol) or self._ex.market(symbol)
        tick = float((market.get("precision") or {}).get("amount") or 1e-8)
        return tick / 2.0

    def cash_tolerance(self, quote: str) -> float:
        """Half of one minor-unit increment for the quote currency
        (design §5 — CAD is effectively 2 decimal places, not BTC's
        amount precision). Prefers the currency's own `display_decimals`
        (Kraken's raw info field — the actual tradable/displayed
        granularity of cash, e.g. CAD=2) over its raw internal `precision`
        (Kraken carries CAD internally at 4 decimals — too tight a
        tolerance for a cash comparison built from trade `cost` figures
        that were themselves already rounded to 2dp by the exchange)."""
        currency = (self._ex.currencies or {}).get(quote)
        if currency:
            info = currency.get("info") or {}
            dd = info.get("display_decimals")
            if dd is not None:
                try:
                    return (10 ** -int(dd)) / 2.0
                except (TypeError, ValueError):
                    pass
            prec = currency.get("precision")
            if prec:
                return float(prec) / 2.0
        return 0.005  # 2dp fallback, half-increment of $0.01


def _to_observed_trade(t: dict) -> ObservedTrade:
    fee = t.get("fee") or {}
    ts_ms = t.get("timestamp")
    exchange_ts = _iso_from_ms(ts_ms) if ts_ms is not None else ""
    return ObservedTrade(
        trade_id=str(t.get("id")),
        order_id=str(t.get("order") or ""),
        symbol=str(t.get("symbol") or ""),
        side=str(t.get("side") or "").lower(),
        price=float(t.get("price") or 0.0),
        amount=float(t.get("amount") or 0.0),
        cost=float(t.get("cost") or 0.0),
        fee_cost=float(fee.get("cost") or 0.0),
        fee_currency=str(fee.get("currency") or ""),
        exchange_timestamp=exchange_ts,
        source="live",
    )


def _to_movement(raw: dict, kind: str) -> LedgerMovement:
    amount = float(raw.get("amount") or 0.0)
    signed = amount if kind == "deposit" else -abs(amount)
    ts_ms = raw.get("timestamp")
    ts_iso = _iso_from_ms(ts_ms) if ts_ms is not None else ""
    return LedgerMovement(
        entry_id=str(raw.get("id") or raw.get("txid") or ""),
        type=kind,
        asset=str(raw.get("currency") or raw.get("code") or ""),
        amount=signed,
        timestamp=ts_iso,
    )
