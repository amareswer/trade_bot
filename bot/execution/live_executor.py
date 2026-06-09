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

import logging
import time
from datetime import datetime, timezone

import ccxt

from bot.execution.executor import Order, OrderSide, OrderStatus, Portfolio

logger = logging.getLogger(__name__)


class LiveExecutor:
    """
    Places real market orders on Kraken via ccxt.
    Interface identical to PaperExecutor.
    """

    def __init__(
        self,
        exchange_id:   str,
        symbol:        str,
        api_key:       str,
        api_secret:    str,
        starting_cash: float = 10_000.0,
        dry_run:       bool  = False,
    ):
        self.symbol    = symbol
        self.dry_run   = dry_run
        self._portfolio = Portfolio(cash=starting_cash)

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
            logger.info("Markets loaded: %d symbols", len(self._markets))
        except Exception as exc:
            if not dry_run:
                raise RuntimeError(
                    f"load_markets() failed — refusing to start in live mode: {exc}"
                ) from exc
            logger.error("load_markets() failed (dry-run, continuing without validation): %s", exc)

        logger.info(
            "LiveExecutor initialized | symbol=%s dry_run=%s",
            symbol, dry_run,
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

        limits    = market.get("limits", {})
        amt_min   = limits.get("amount", {}).get("min")
        cost_min  = limits.get("cost",   {}).get("min")

        base  = self.symbol.split("/")[0]
        quote = self.symbol.split("/")[1]
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

    # ── Core execution ────────────────────────────────────────────────

    def execute(
        self,
        signal,
        price:    float,
        quantity: float,
    ) -> Order | None:
        from bot.strategy.threshold_strategy import Signal

        if signal not in (Signal.BUY, Signal.SELL):
            return None

        side = OrderSide.BUY if signal == Signal.BUY else OrderSide.SELL

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
            return Order(
                order_id      = "rejected",
                symbol        = self.symbol,
                side          = side,
                quantity      = quantity,
                price         = price,
                status        = OrderStatus.REJECTED,
                created_at    = ts,
                reject_reason = str(exc),
            )

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
                logger.warning(
                    "LIVE ORDER: %s %.6f %s",
                    side.value, quantity, self.symbol,
                )
                ccxt_side = "buy" if side == OrderSide.BUY else "sell"
                raw = self._exchange.create_order(
                    symbol = self.symbol,
                    type   = "market",
                    side   = ccxt_side,
                    amount = quantity,
                )
                order_id_str = str(raw.get("id", ""))
                filled_qty   = float(raw.get("filled", 0.0))
                fill_price   = float(raw.get("average") or raw.get("price") or price)

                # Poll up to 3 times for 'closed' status.
                # If still open after 3 polls, save state with whatever was filled —
                # never leave cash/position unupdated after a real order was sent.
                last_raw = raw
                for poll_num in range(1, 4):
                    time.sleep(1)
                    try:
                        last_raw   = self._exchange.fetch_order(order_id_str, self.symbol)
                        filled_qty = float(last_raw.get("filled", filled_qty))
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
                    filled_qty = float(last_raw.get("filled", filled_qty))
                    fill_price = float(
                        last_raw.get("average") or
                        last_raw.get("price")   or
                        price
                    )
                    logger.warning(
                        "ORDER %s NOT CLOSED after 3 polls — saving state with "
                        "partial fill=%.6f %s @ %.2f. Manual verification recommended.",
                        order_id_str, filled_qty, self.symbol, fill_price,
                    )

                quantity = filled_qty  # use actual filled amount, not requested

            except ccxt.InsufficientFunds as exc:
                logger.error("Insufficient funds: %s", exc)
                return Order(
                    order_id      = "rejected",
                    symbol        = self.symbol,
                    side          = side,
                    quantity      = quantity,
                    price         = price,
                    status        = OrderStatus.REJECTED,
                    created_at    = ts,
                    reject_reason = f"Insufficient funds: {exc}",
                )
            except ccxt.BaseError as exc:
                logger.error("ccxt order error: %s", exc)
                return Order(
                    order_id      = "rejected",
                    symbol        = self.symbol,
                    side          = side,
                    quantity      = quantity,
                    price         = price,
                    status        = OrderStatus.REJECTED,
                    created_at    = ts,
                    reject_reason = f"Exchange error: {exc}",
                )

        total_value = fill_price * quantity

        if side == OrderSide.BUY:
            prev_cost = self._portfolio._cost_basis * self._portfolio.position
            self._portfolio.cash      -= total_value
            self._portfolio.position  += quantity
            self._portfolio._cost_basis = (
                (prev_cost + fill_price * quantity) / self._portfolio.position
                if self._portfolio.position > 0 else 0.0
            )
        else:
            pnl = (fill_price - self._portfolio._cost_basis) * quantity
            self._portfolio.realized_pnl += pnl
            self._portfolio.cash         += total_value
            self._portfolio.position      = max(0.0, self._portfolio.position - quantity)
            if self._portfolio.position == 0:
                self._portfolio._cost_basis = 0.0

        return Order(
            order_id   = order_id_str,
            symbol     = self.symbol,
            side       = side,
            quantity   = quantity,
            price      = fill_price,
            status     = OrderStatus.FILLED,
            created_at = ts,
            filled_at  = datetime.now(timezone.utc),
        )

    def filled_orders(self) -> list:
        """Compatible with PaperExecutor interface."""
        return []

    def rejected_orders(self) -> list:
        """Compatible with PaperExecutor interface."""
        return []

    @property
    def orders(self) -> list:
        """Compatible with PaperExecutor interface."""
        return []

    def reset(self) -> None:
        """Compatible with PaperExecutor interface."""
        self._portfolio.position     = 0.0
        self._portfolio._cost_basis  = 0.0
        self._portfolio.realized_pnl = 0.0

    def portfolio_snapshot(self, current_price: float) -> None:
        """Compatible with PaperExecutor interface."""
        logger.info(
            "PORTFOLIO | cash=$%.2f | pos=%.6f %s | total=$%.2f",
            self._portfolio.cash,
            self._portfolio.position,
            self.symbol,
            self._portfolio.total_value(current_price),
        )
