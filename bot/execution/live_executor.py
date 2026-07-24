"""
Live order executor — places real orders on Kraken via ccxt.

WARNING: This executor uses real money. Only enable when:
  1. LIVE_TRADING=true in .env
  2. Kraken API key and secret are set
  3. You have verified dry run behavior is correct

The interface is identical to PaperExecutor so main.py can
swap between them with a single config flag.
"""
from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime, timezone

import ccxt

from bot.execution.executor import Order, OrderSide, OrderStatus, Portfolio
from bot.exchanges.retry import fetch_with_retry
from config import cfg

logger = logging.getLogger(__name__)

# Legacy fallback — every caller in bot/main.py passes an explicit per-symbol
# path (logs/live_state_BTC_CAD.json etc.), so this constant is never reached.
_DEFAULT_STATE_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "logs", "live_state.json",
)


class LiveExecutor:
    """
    Places real market orders on Kraken via ccxt.
    Interface identical to PaperExecutor.
    """

    # Exchange balance must exceed state-file position by at least this much
    # before the external-holdings guard fires (avoids false positives from
    # sub-satoshi rounding differences between Kraken and state files).
    _EXTERNAL_THRESHOLD = 1e-5  # 10 satoshis / 10 DOGE / etc.

    def __init__(
        self,
        exchange_id:              str,
        symbol:                   str,
        api_key:                  str,
        api_secret:               str,
        starting_cash:            float = 10_000.0,
        dry_run:                  bool  = False,
        state_path:               str   = _DEFAULT_STATE_PATH,
        order_type:               str   = "market",
        adopt_external_holdings:  bool  = False,
    ):
        self.symbol                    = symbol
        self.dry_run                   = dry_run
        self._order_type               = order_type
        self._starting_cash            = starting_cash
        self._state_path               = state_path
        self._adopt_external_holdings  = adopt_external_holdings
        self._portfolio                = Portfolio(cash=starting_cash)
        self._fills:      list[Order]  = []
        self._rejects:    list[Order]  = []
        self._fees_paid:           float = 0.0
        self._bot_opened_position: bool  = False

        exchange_cls = getattr(ccxt, exchange_id.lower())
        self._exchange = exchange_cls({
            "apiKey":          api_key,
            "secret":          api_secret,
            "timeout":         15_000,
            "enableRateLimit": True,
        })

        # load_markets is a public endpoint — no API key required.
        # In live mode, failure is fatal: validation is useless without market data.
        self._markets: dict | None = None
        try:
            self._markets = self._exchange.load_markets()
            logger.warning("Markets loaded: %d symbols", len(self._markets))
        except Exception as exc:
            if not dry_run:
                raise RuntimeError(
                    f"load_markets() failed — refusing to start in live mode: {exc}"
                ) from exc
            logger.error("load_markets() failed (dry-run, continuing without validation): %s", exc)

        # State restore + balance reconciliation.
        # In live mode: load saved state (position/cost_basis) then override cash
        # with the actual exchange balance to detect restart drift.
        # In dry-run: load saved state only (no API call for balance).
        self._load_state()
        if not dry_run:
            base  = symbol.split("/")[0]
            quote = symbol.split("/")[1]
            exchange_cash, sync_error = self._sync_cash()
            self._portfolio.cash = exchange_cash
            self._sync_position(symbol)

            # Unmissable startup line — print() bypasses logging so it always
            # appears in the terminal regardless of log level configuration.
            if sync_error:
                print(
                    f"  LIVE BALANCE: ${self._portfolio.cash:.2f} {quote}"
                    f" (FALLBACK — fetch_balance FAILED: {sync_error})"
                    f" | position: {self._portfolio.position:.6f} {base}",
                    flush=True,
                )
            else:
                print(
                    f"  LIVE BALANCE: ${self._portfolio.cash:.2f} {quote}"
                    f" | position: {self._portfolio.position:.6f} {base}"
                    f" | source: {exchange_id} fetch_balance",
                    flush=True,
                )

        logger.warning(
            "LiveExecutor ready | symbol=%s dry_run=%s cash=%.2f pos=%.6f",
            symbol, dry_run, self._portfolio.cash, self._portfolio.position,
        )

    # ── Read-only properties ──────────────────────────────────────────

    @property
    def cash(self) -> float:
        return self._portfolio.cash

    @property
    def position(self) -> float:
        return self._portfolio.position

    @property
    def avg_entry(self) -> float:
        return self._portfolio._cost_basis

    @property
    def portfolio(self) -> Portfolio:
        return self._portfolio

    @property
    def fees_paid(self) -> float:
        return self._fees_paid

    # ── Balance sync ──────────────────────────────────────────────────

    def _sync_cash(self) -> tuple[float, str | None]:
        """
        Fetch free balance in the quote currency from the exchange.
        Returns (cash, error_msg): error_msg is None on success, or the reason
        we fell back to starting_cash.
        """
        quote = self.symbol.split("/")[1]
        try:
            balance = self._exchange.fetch_balance()
            free    = balance.get("free", {})
            if quote not in free:
                available = sorted(k for k, v in free.items() if v and float(v or 0) > 0)
                msg = (
                    f"'{quote}' not in exchange free balance "
                    f"(non-zero currencies: {available}) — check SYMBOL or API key permissions"
                )
                logger.warning("_sync_cash: %s; using starting_cash=%.2f", msg, self._starting_cash)
                return self._starting_cash, msg
            amount = float(free[quote])
            logger.warning("Balance sync: %.2f %s free on exchange", amount, quote)
            return amount, None
        except Exception as exc:
            msg = str(exc)
            logger.warning("_sync_cash failed: %s — using starting_cash=%.2f", msg, self._starting_cash)
            return self._starting_cash, msg

    def _sync_position(self, symbol: str) -> None:
        """
        Reconcile managed position against exchange balance on startup.

        The state file is the primary source of truth for what the BOT manages.
        The exchange balance may exceed the state file (deposits, manual trades,
        or positions opened by a different session) — that excess is "external
        holdings" and is NOT traded unless ADOPT_EXTERNAL_HOLDINGS=true.

        Outcomes:
          exchange > state + threshold, adopt=False  → warn + keep state qty
          exchange > state + threshold, adopt=True   → adopt all (old behaviour)
          exchange == state (within threshold)        → confirm from exchange
          exchange == 0, state > 0                   → externally closed; zero state
        """
        base = symbol.split("/")[0]
        try:
            balance        = self._exchange.fetch_balance()
            exchange_free  = float(balance.get("free",  {}).get(base, 0.0))
            exchange_total = float(balance.get("total", {}).get(base, 0.0))
        except Exception as exc:
            logger.warning("_sync_position: fetch_balance failed — %s", exc)
            return

        # prev_position is what _load_state() set from the on-disk state file.
        # This is the quantity the bot "owns" from its own trading records.
        prev_position = self._portfolio.position

        if exchange_total > 1e-9:
            # ── External holdings guard ───────────────────────────────────────
            # If exchange holds more than the state file recorded, the surplus
            # is not under bot management: deposits, manual buys, or stale
            # _bot_opened_position flags from a different session.
            _excess = exchange_total - prev_position
            if _excess > self._EXTERNAL_THRESHOLD and not self._adopt_external_holdings:
                logger.warning(
                    "EXTERNAL HOLDINGS DETECTED [%s]: exchange %.6f %s"
                    " > state-file %.6f — %.6f %s not under bot management"
                    " and will not be traded"
                    " (set ADOPT_EXTERNAL_HOLDINGS=true to opt in)",
                    symbol, exchange_total, base, prev_position, _excess, base,
                )
                print(
                    f"  EXTERNAL HOLDINGS [{symbol}]:"
                    f" exchange {exchange_total:.6f} {base}  "
                    f"  state-file {prev_position:.6f} {base}  "
                    f"  {_excess:.6f} {base} NOT under bot management — will not be traded.",
                    flush=True,
                )
                # Do not adopt the excess — managed position stays at prev_position.
                self._save_state()
                return

            # ── Normal adoption ───────────────────────────────────────────────
            self._portfolio.position = exchange_total

            if exchange_free < 1e-9:
                # Kraken settlement window: fill landed in total, not yet free.
                logger.warning(
                    "%s settling on exchange: total=%.6f free=0"
                    " — using total as position, will become free shortly",
                    base, exchange_total,
                )
                print(
                    f"  SETTLEMENT: {base} total={exchange_total:.6f} free=0"
                    f" — position set to total, awaiting settlement.",
                    flush=True,
                )

            if prev_position < 1e-9:
                if not self._bot_opened_position:
                    # adopt_external_holdings=True and bot didn't open this:
                    # treat it as unmanaged even with adopt flag (safety net).
                    logger.warning(
                        "AMBIENT BALANCE IGNORED [%s]: exchange holds %.6f %s"
                        " but bot_opened_position=False — skipping adoption.",
                        self.symbol, exchange_total, base,
                    )
                    print(
                        f"  AMBIENT BALANCE IGNORED [{self.symbol}]:"
                        f" exchange has {exchange_total:.6f} {base}"
                        f" but this bot did not open it — skipping adoption.",
                        flush=True,
                    )
                    self._portfolio.position    = 0.0
                    self._portfolio._cost_basis = 0.0
                    return
                # Bot opened this position on a prior run — reseed cost_basis.
                try:
                    current_price = float(self._exchange.fetch_ticker(symbol)["last"])
                except Exception:
                    current_price = 0.0
                self._portfolio._cost_basis = current_price
                logger.warning(
                    "STATE MISMATCH: exchange holds %.6f %s but saved position=0."
                    " Reseeded cost_basis at current price %.2f",
                    exchange_total, base, current_price,
                )
                print(
                    f"  POSITION RESEEDED: {exchange_total:.6f} {base}"
                    f" @ ${current_price:,.2f}"
                    f" (exchange vs saved-state mismatch)",
                    flush=True,
                )
            else:
                logger.warning(
                    "Position confirmed from exchange: %.6f %s (free=%.6f)",
                    exchange_total, base, exchange_free,
                )
        else:
            # exchange_total == 0: genuinely no position on exchange.
            if prev_position > 1e-9:
                logger.warning(
                    "POSITION CLOSED EXTERNALLY [%s]: exchange shows 0 %s"
                    " but saved position=%.6f — zeroing state."
                    " No SELL will be placed.",
                    self.symbol, base, prev_position,
                )
                print(
                    f"  POSITION CLOSED EXTERNALLY [{self.symbol}]:"
                    f" exchange has 0 {base} but state held {prev_position:.6f}"
                    f" — state zeroed. No SELL issued.",
                    flush=True,
                )
            self._portfolio.position    = 0.0
            self._portfolio._cost_basis = 0.0
            self._bot_opened_position   = False

        self._save_state()

    # ── State persistence ─────────────────────────────────────────────

    def _save_state(self) -> None:
        """Persist portfolio state to disk so restarts can reconcile."""
        state = {
            "symbol":       self.symbol,
            "cash":         self._portfolio.cash,
            "position":     self._portfolio.position,
            "cost_basis":   self._portfolio._cost_basis,
            "realized_pnl": self._portfolio.realized_pnl,
            "fees_paid":    self._fees_paid,
            "bot_opened":   self._bot_opened_position,
            "saved_at":     datetime.now(timezone.utc).isoformat(),
        }
        try:
            from bot.atomic_json import atomic_write_json
            atomic_write_json(self._state_path, state)
            logger.warning(
                "State saved: cash=%.2f pos=%.6f", state["cash"], state["position"],
            )
        except Exception as exc:
            logger.error("Failed to save state: %s", exc)

    def _load_state(self) -> bool:
        """
        Restore accounting fields from disk.
        Returns True if state was successfully loaded, False if starting fresh.

        # position and cash are always overridden by _sync_position and _sync_cash
        # — only accounting fields (cost_basis, realized_pnl, fees_paid) are
        # restored from disk. The state file is an accounting ledger, not a
        # position tracker.
        """
        try:
            with open(self._state_path) as f:
                state = json.load(f)
        except FileNotFoundError:
            logger.info("No saved state at %s — starting fresh", self._state_path)
            return False
        except Exception as exc:
            logger.warning("Could not load saved state: %s", exc)
            return False

        if state.get("symbol") != self.symbol:
            logger.warning(
                "Saved state symbol '%s' != current '%s' — ignoring saved state",
                state.get("symbol"), self.symbol,
            )
            return False

        # Restore all fields. In live mode, cash and position are subsequently
        # overwritten by _sync_cash / _sync_position (exchange is authoritative).
        # In dry-run there is no exchange sync, so these values must come from state.
        self._portfolio.cash          = float(state.get("cash",         self._starting_cash))
        self._portfolio.position      = float(state.get("position",     0.0))
        self._portfolio._cost_basis   = float(state.get("cost_basis",   0.0))
        self._portfolio.realized_pnl  = float(state.get("realized_pnl", 0.0))
        self._fees_paid               = float(state.get("fees_paid",     0.0))
        self._bot_opened_position     = bool(state.get("bot_opened",    False))
        logger.warning(
            "Accounting restored: cost_basis=%.2f pnl=%.2f fees=%.4f (saved %s)",
            self._portfolio._cost_basis, self._portfolio.realized_pnl,
            self._fees_paid, state.get("saved_at", "?"),
        )
        return True

    # ── Validation ────────────────────────────────────────────────────

    def _validate_order(self, side: OrderSide, quantity: float, price: float) -> None:
        """Check order against exchange minimums. Raises ValueError with a self-explanatory message."""
        if self._markets is None:
            logger.warning("Cannot validate order — markets not loaded")
            return

        market = self._markets.get(self.symbol)
        if not market:
            logger.warning("Symbol %s not in loaded markets — skipping validation", self.symbol)
            return

        limits   = market.get("limits", {})
        amt_min  = limits.get("amount", {}).get("min")
        cost_min = limits.get("cost",   {}).get("min")

        base     = self.symbol.split("/")[0]
        quote    = self.symbol.split("/")[1]
        req_cost = quantity * price

        errors = []

        if amt_min and quantity < amt_min:
            min_cost = amt_min * price
            errors.append(
                f"requested {quantity:.6f} {base} (${req_cost:.2f} {quote}), "
                f"Kraken minimum {amt_min:.6f} {base} (~${min_cost:.2f} {quote})"
                f" — increase RISK_PER_TRADE_PCT or capital"
            )

        if cost_min and req_cost < cost_min:
            min_qty = cost_min / price if price > 0 else 0.0
            errors.append(
                f"requested {quantity:.6f} {base} (${req_cost:.2f} {quote}), "
                f"min cost ${cost_min:.2f} {quote} (~{min_qty:.6f} {base})"
                f" — increase RISK_PER_TRADE_PCT or capital"
            )

        if errors:
            raise ValueError("; ".join(errors))

    # ── Limit order chasing ───────────────────────────────────────────

    def _place_limit_order(self, side: str, quantity: float, price: float) -> dict:
        """
        Post-only limit order with automatic repricing.

        Places a post-only limit order just inside the spread, polls until filled,
        and reprices up to cfg.exchange.limit_chase_max_retries times on timeout.

        ccxt.InvalidOrder (Kraken PO rejection — order would cross spread):
            Halves the tick offset and retries without consuming a timeout retry slot.
            Falls back to market if tick_pct drops below 0.000001.

        Any other ccxt exception: retried once (2026-07-24 — the July 6/15
        incidents both started with a single transient depth-fetch error
        cascading straight into a market-order fallback), then falls back
        to market if the retry also fails.

        Returns the raw ccxt order dict of the final order. Market-fallback
        paths return the immediate create_order response, which on Kraken can
        still be status=None / filled=0 before the fill propagates — the
        caller (execute) polls fetch_order until the order resolves.
        """
        _MIN_TICK_PCT    = 0.000001
        tick_pct         = cfg.exchange.limit_chase_tick_pct
        timeout_attempts = 0
        max_attempts     = cfg.exchange.limit_chase_max_retries + 1

        while timeout_attempts < max_attempts:
            # Fetch orderbook and compute limit price — network errors fall back immediately.
            try:
                book = fetch_with_retry(
                    lambda: self._exchange.fetch_order_book(self.symbol, limit=5),
                    attempts=2, delay_s=1.5, label=f"order book fetch [{self.symbol}]",
                )
                if side == "buy":
                    bid           = float(book["bids"][0][0])
                    limit_price_f = bid * (1.0 + tick_pct)
                else:
                    ask           = float(book["asks"][0][0])
                    limit_price_f = ask * (1.0 + tick_pct)
                limit_price = self._exchange.price_to_precision(self.symbol, limit_price_f)
            except Exception as exc:
                logger.warning(
                    "_place_limit_order: %s (%s) — falling back to market order",
                    type(exc).__name__, exc,
                )
                return self._exchange.create_order(self.symbol, "market", side, quantity)

            logger.warning(
                "LIMIT %s attempt %d/%d: %.6f %s @ %.2f (post-only, tick_pct=%.6f)",
                side.upper(), timeout_attempts + 1, max_attempts,
                quantity, self.symbol, limit_price_f, tick_pct,
            )

            try:
                raw = self._exchange.create_order(
                    self.symbol, "limit", side, quantity, limit_price,
                    {"timeInForce": "PO"},
                )
                order_id = str(raw.get("id", ""))
            except ccxt.InvalidOrder:
                # Kraken rejected PO because the price would cross the spread.
                # Halve the tick offset and retry — does not consume a timeout slot.
                tick_pct /= 2.0
                logger.warning(
                    "PO order would cross spread — retrying with tighter offset %.6f",
                    tick_pct,
                )
                if tick_pct < _MIN_TICK_PCT:
                    logger.warning("spread too tight for post-only, using market")
                    return self._exchange.create_order(self.symbol, "market", side, quantity)
                continue
            except Exception as exc:
                logger.warning(
                    "_place_limit_order: %s (%s) — falling back to market order",
                    type(exc).__name__, exc,
                )
                return self._exchange.create_order(self.symbol, "market", side, quantity)

            # Quick return if exchange already shows the order as closed
            if raw.get("status") == "closed":
                return raw

            # Poll for fill every 5 s up to the configured timeout
            deadline = time.time() + cfg.exchange.limit_chase_timeout_s
            while time.time() < deadline:
                time.sleep(5)
                try:
                    polled = self._exchange.fetch_order(order_id, self.symbol)
                    if polled.get("status") == "closed":
                        logger.warning("LIMIT %s filled: %s", side.upper(), order_id)
                        return polled
                except Exception as poll_exc:
                    logger.warning("fetch_order %s failed: %s", order_id, poll_exc)

            # Timeout — cancel and consume one timeout retry slot
            cancel_ok = True
            try:
                self._exchange.cancel_order(order_id, self.symbol)
                logger.warning(
                    "LIMIT %s timed out (attempt %d) — cancelled %s",
                    side.upper(), timeout_attempts + 1, order_id,
                )
            except Exception as cancel_exc:
                cancel_ok = False
                logger.warning("cancel_order %s failed: %s", order_id, cancel_exc)

            # The order may have filled (fully or partially) in the race between
            # the last poll and the cancel — a cancel of a filled order raises.
            # Re-placing without checking would double-fill; a partial fill on a
            # cancelled order would vanish from the books. Verify before retrying.
            try:
                post_cancel = self._exchange.fetch_order(order_id, self.symbol)
                if float(post_cancel.get("filled") or 0.0) > 0:
                    logger.warning(
                        "LIMIT %s %s filled %.6f during cancel race — recording it, no re-place",
                        side.upper(), order_id, float(post_cancel.get("filled") or 0.0),
                    )
                    return post_cancel
            except Exception as post_exc:
                logger.warning("post-cancel fetch_order %s failed: %s", order_id, post_exc)
                if not cancel_ok:
                    # Cancel failed AND we cannot verify the order's fate — the
                    # order may still be live. Re-placing risks a double fill;
                    # return the unresolved dict and let execute()'s poll loop
                    # settle it.
                    logger.error(
                        "LIMIT %s %s: cancel failed and state unverifiable — "
                        "aborting chase without re-placing.",
                        side.upper(), order_id,
                    )
                    return raw

            timeout_attempts += 1

        logger.warning(
            "limit chase failed after %d retries, falling back to market order",
            cfg.exchange.limit_chase_max_retries,
        )
        return self._exchange.create_order(self.symbol, "market", side, quantity)

    # ── Core execution ────────────────────────────────────────────────

    def execute(
        self,
        signal,
        price:    float,
        quantity: float,
        urgent:   bool = False,
    ) -> Order | None:
        """
        urgent=True forces a plain market order regardless of
        LIMIT_ORDER_ENABLED / ORDER_TYPE. SL/TP exits must never sit in a
        limit-chase: a post-only sell above the ask in a falling market can
        spend minutes repricing while the stop level runs away.
        """
        from bot.strategy.threshold_strategy import Signal

        if signal not in (Signal.BUY, Signal.SELL):
            return None

        side  = OrderSide.BUY if signal == Signal.BUY else OrderSide.SELL
        quote = self.symbol.split("/")[1]

        if side == OrderSide.BUY and quantity <= 0:
            logger.warning("LiveExecutor: BUY quantity=0, skipping")
            return None
        if side == OrderSide.SELL and self._portfolio.position <= 0:
            logger.warning("LiveExecutor: SELL with no position, skipping")
            return None

        quantity = self._portfolio.position if side == OrderSide.SELL else quantity
        ts       = datetime.now(timezone.utc)

        # Validate against exchange minimums in both dry-run and live mode.
        # Dry-run must exercise the rejection path — that is the point of dry-run.
        try:
            self._validate_order(side, quantity, price)
        except ValueError as exc:
            logger.error("ORDER REJECTED: %s", exc)
            order = Order(
                order_id      = "rejected",
                symbol        = self.symbol,
                side          = side,
                quantity      = quantity,
                price         = price,
                status        = OrderStatus.REJECTED,
                created_at    = ts,
                reject_reason = str(exc),
            )
            self._rejects.append(order)
            return order

        fee_cost     = 0.0
        fee_currency = quote

        if self.dry_run:
            logger.warning(
                "DRY RUN — would place %s %.6f %s @ ~%.2f",
                side.value, quantity, self.symbol, price,
            )
            print(
                f"  [DRY RUN] {side.value} {quantity:.6f} {self.symbol}"
                f" @ ~{price:,.2f}",
                flush=True,
            )
            fill_price   = price
            filled_qty   = quantity
            order_id_str = "dry_run"

        else:
            try:
                ccxt_side = "buy" if side == OrderSide.BUY else "sell"

                if cfg.exchange.limit_order_enabled and not urgent:
                    # Limit-chase path. _place_limit_order polls orders it placed
                    # itself, but its market-FALLBACK paths return the immediate
                    # create_order response — on Kraken that can be status=None /
                    # filled=0 before the fill propagates. Poll until resolved,
                    # same as the direct market path below. Incident 2026-07-15:
                    # order OFIPRK-N6JMC-IRHKMX filled $7.73 but the unpolled
                    # filled=0 hit the qty=0 guard and the fill went unrecorded.
                    raw          = self._place_limit_order(ccxt_side, quantity, price)
                    order_id_str = str(raw.get("id", ""))
                    filled_qty   = float(raw.get("filled") or 0.0)
                    fill_price   = float(raw.get("average") or raw.get("price") or price)
                    last_raw     = raw
                    if order_id_str and last_raw.get("status") not in ("closed", "canceled"):
                        for poll_num in range(1, 10):
                            time.sleep(1)
                            try:
                                last_raw   = self._exchange.fetch_order(order_id_str, self.symbol)
                                filled_qty = float(last_raw.get("filled") or filled_qty)
                                if last_raw.get("status") in ("closed", "canceled"):
                                    break
                            except Exception as poll_exc:
                                logger.warning(
                                    "fetch_order poll %d failed: %s", poll_num, poll_exc,
                                )
                        fill_price = float(
                            last_raw.get("average") or
                            last_raw.get("price")   or
                            fill_price
                        )
                    quantity = filled_qty
                else:
                    if self._order_type == "limit" and side == OrderSide.BUY and not urgent:
                        # Passive bid 0.2% below market — post-only guarantees maker rate (0.40%, confirmed Jun 14 fill)
                        limit_price = round(price * 0.998, 2)
                        logger.warning(
                            "LIMIT BUY: %.6f %s @ %.2f (0.2%% below %.2f, post-only)",
                            quantity, self.symbol, limit_price, price,
                        )
                        raw = self._exchange.create_order(
                            symbol = self.symbol,
                            type   = "limit",
                            side   = ccxt_side,
                            amount = quantity,
                            price  = limit_price,
                            params = {"postOnly": True},
                        )
                    else:
                        logger.warning(
                            "LIVE ORDER: %s %.6f %s",
                            side.value, quantity, self.symbol,
                        )
                        raw = self._exchange.create_order(
                            symbol = self.symbol,
                            type   = "market",
                            side   = ccxt_side,
                            amount = quantity,
                        )
                    order_id_str = str(raw.get("id", ""))
                    filled_qty   = float(raw.get("filled") or 0.0)
                    fill_price   = float(raw.get("average") or raw.get("price") or price)

                    # Poll up to 9 times for 'closed' status.
                    # If still open after polls, use whatever 'filled' amount the
                    # last poll reported — never leave cash/position unupdated after
                    # a real order was sent.
                    last_raw = raw
                    for poll_num in range(1, 10):
                        time.sleep(1)
                        try:
                            last_raw   = self._exchange.fetch_order(order_id_str, self.symbol)
                            filled_qty = float(last_raw.get("filled") or filled_qty)
                            if last_raw.get("status") == "closed":
                                fill_price = float(
                                    last_raw.get("average") or
                                    last_raw.get("price")   or
                                    price
                                )
                                break
                        except Exception as poll_exc:
                            logger.warning("fetch_order poll %d failed: %s", poll_num, poll_exc)
                    else:
                        filled_qty = float(last_raw.get("filled") or filled_qty)
                        fill_price = float(
                            last_raw.get("average") or
                            last_raw.get("price")   or
                            price
                        )
                        if self._order_type == "limit" and last_raw.get("status") not in ("closed", "filled"):
                            try:
                                self._exchange.cancel_order(order_id_str, self.symbol)
                                logger.warning(
                                    "LIMIT ORDER %s not filled after polls — cancelled. "
                                    "Consider ORDER_TYPE=market for guaranteed fills.",
                                    order_id_str,
                                )
                            except Exception as _cancel_exc:
                                logger.warning("Failed to cancel limit order %s: %s", order_id_str, _cancel_exc)
                        else:
                            logger.warning(
                                "ORDER %s NOT CLOSED after 3 polls — saving state with "
                                "partial fill=%.6f %s @ %.2f. Manual verification recommended.",
                                order_id_str, filled_qty, self.symbol, fill_price,
                            )

                    quantity = filled_qty

                # Recovery: quantity is 0 after polling — try to recover the true fill.
                # Applies to both BUY and SELL (BUY can hit qty=0 when _place_limit_order
                # falls back to a market order and the initial create_order response has
                # filled=0 before the fill propagates). Priority:
                #   1. last_raw["filled"] if non-zero — authoritative (exchange confirms it)
                #   2. last_raw["amount"] ONLY for market orders that closed — safe inference
                #      (closed market order = fully executed; amount = what was requested)
                #      Never use amount for limit orders — they may partially fill or cancel.
                #   3. If neither recovers a positive qty → return None to prevent phantom row.
                if quantity <= 0:
                    _last_filled = float(last_raw.get("filled") or 0.0)
                    _last_status = last_raw.get("status")
                    # Classify by the ACTUAL order type the exchange executed, not
                    # the configured one: the limit-chase falls back to market
                    # orders while ORDER_TYPE=limit, and treating that fallback as
                    # a limit order blocked the amount inference on 2026-07-15.
                    _actual_type = last_raw.get("type") or self._order_type
                    _is_market   = (_actual_type != "limit")

                    _side_str = side.value
                    if _last_filled > 0:
                        # `filled` is now non-zero — initial create_order response was stale.
                        quantity   = _last_filled
                        filled_qty = _last_filled
                        logger.warning(
                            "%s filled settled to %.6f after polling (order %s)"
                            " — initial response had filled=0",
                            _side_str, _last_filled, order_id_str,
                        )
                    elif _last_status in ("closed", "filled") and _is_market:
                        # Market order closed with filled still=0 — infer from amount.
                        _req_amt = float(last_raw.get("amount") or 0.0)
                        if _req_amt > 0:
                            quantity   = _req_amt
                            filled_qty = _req_amt
                            logger.warning(
                                "%s market order %s closed with filled=0"
                                " — inferring fill qty from amount=%.6f."
                                " Verify on exchange if P&L looks wrong.",
                                _side_str, order_id_str, _req_amt,
                            )
                        else:
                            logger.error(
                                "%s qty=0 GUARD: order %s closed but amount=0 too"
                                " — skipping fill record. Manual verification required.",
                                _side_str, order_id_str,
                            )
                            return None
                    else:
                        # Limit order with filled=0, or order not yet closed — do not infer.
                        logger.error(
                            "%s qty=0 GUARD: order %s status=%s order_type=%s filled=0"
                            " — skipping fill record to prevent phantom row."
                            " Manual verification required.",
                            _side_str, order_id_str, _last_status, _actual_type,
                        )
                        return None

                # Shared fee extraction — works for both limit-chase and market paths.
                # Log the raw dict so the true fee structure is auditable.
                fee_data     = last_raw.get("fee") or {}
                logger.warning("Fee dict from exchange: %s", fee_data)
                fee_cost     = float(fee_data.get("cost") or 0.0)
                fee_currency = fee_data.get("currency") or quote

            except ccxt.InsufficientFunds as exc:
                logger.error("Insufficient funds: %s", exc)
                order = Order(
                    order_id      = "rejected",
                    symbol        = self.symbol,
                    side          = side,
                    quantity      = quantity,
                    price         = price,
                    status        = OrderStatus.REJECTED,
                    created_at    = ts,
                    reject_reason = f"Insufficient funds: {exc}",
                )
                self._rejects.append(order)
                return order
            except ccxt.BaseError as exc:
                logger.error("ccxt order error: %s", exc)
                order = Order(
                    order_id      = "rejected",
                    symbol        = self.symbol,
                    side          = side,
                    quantity      = quantity,
                    price         = price,
                    status        = OrderStatus.REJECTED,
                    created_at    = ts,
                    reject_reason = f"Exchange error: {exc}",
                )
                self._rejects.append(order)
                return order

        total_value = fill_price * quantity

        if side == OrderSide.BUY:
            prev_cost = self._portfolio._cost_basis * self._portfolio.position
            self._portfolio.cash      -= total_value
            self._portfolio.position  += quantity
            self._portfolio._cost_basis = (
                (prev_cost + fill_price * quantity) / self._portfolio.position
                if self._portfolio.position > 0 else 0.0
            )
            self._bot_opened_position = True
        else:
            pnl = (fill_price - self._portfolio._cost_basis) * quantity
            self._portfolio.realized_pnl += pnl
            self._portfolio.cash         += total_value
            self._portfolio.position      = max(0.0, self._portfolio.position - quantity)
            if self._portfolio.position == 0:
                self._portfolio._cost_basis   = 0.0
                self._bot_opened_position     = False

        # Deduct exchange fee (live only). If fee is in a non-quote currency
        # (e.g. Kraken fee tokens), skip and log — do not silently mis-account.
        if fee_cost > 0:
            if fee_currency != quote:
                logger.warning(
                    "Fee currency mismatch: fee=%.6f %s but quote=%s — "
                    "not deducting (manual reconciliation needed)",
                    fee_cost, fee_currency, quote,
                )
            else:
                self._portfolio.cash -= fee_cost
                self._fees_paid      += fee_cost
                logger.warning("Fee deducted: %.6f %s", fee_cost, quote)

        order = Order(
            order_id     = order_id_str,
            symbol       = self.symbol,
            side         = side,
            quantity     = quantity,
            price        = fill_price,
            status       = OrderStatus.FILLED,
            created_at   = ts,
            filled_at    = datetime.now(timezone.utc),
            fee_cost     = fee_cost,
            fee_currency = fee_currency,
        )
        self._fills.append(order)
        self._save_state()
        return order

    # ── Order history ─────────────────────────────────────────────────

    def filled_orders(self) -> list[Order]:
        return list(self._fills)

    def rejected_orders(self) -> list[Order]:
        return list(self._rejects)

    @property
    def orders(self) -> list[Order]:
        return list(self._fills) + list(self._rejects)

    # ── Lifecycle ─────────────────────────────────────────────────────

    def reset(self) -> None:
        """Reset all state back to starting conditions."""
        self._portfolio.cash          = self._starting_cash
        self._portfolio.position      = 0.0
        self._portfolio._cost_basis   = 0.0
        self._portfolio.realized_pnl  = 0.0
        self._fills.clear()
        self._rejects.clear()

    def portfolio_snapshot(self, current_price: float) -> None:
        logger.info(
            "PORTFOLIO | cash=$%.2f | pos=%.6f %s | total=$%.2f",
            self._portfolio.cash,
            self._portfolio.position,
            self.symbol,
            self._portfolio.total_value(current_price),
        )
