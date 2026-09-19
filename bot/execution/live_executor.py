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
import uuid
from datetime import datetime, timezone

import ccxt

from bot.alerts.telegram import TelegramAlerter
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

# Pre-trade minimum-size guard (2026-07-30): the ATR-risk-capped BUY sizer
# can land close to the exchange's amt_min at high price / wide ATR — this
# margin decides how close is "close enough to warn about" before an order
# is even placed. Read directly here rather than via config.py — this
# guard's scope is live_executor.py + tests only.
_MIN_SIZE_SAFETY_MARGIN = float(os.getenv("MIN_SIZE_SAFETY_MARGIN", "1.5"))

# The only two Kraken ordertypes this bot ever places as a native protective
# stop (see _place_native_stop / _place_native_trailing_stop). Kraken's raw
# ordertype string lives at order['info']['descr']['ordertype'] — confirmed
# via ccxt's kraken.py parse_order(), which preserves the raw AddOrder/
# fetchOpenOrders response verbatim under 'info'. The unified ccxt 'type'
# field is NOT reliable for this: Kraken's 'stop-loss' ordertype maps to
# unified type 'market' (indistinguishable from a genuine market order) —
# only the raw descr.ordertype string distinguishes our two stop kinds from
# everything else that could be resting on the symbol.
_NATIVE_STOP_ORDERTYPES = frozenset({"stop-loss", "trailing-stop"})

# ccxt unified statuses that mean an order is genuinely done and NOT resting —
# safe to treat a zero-fill order in one of these states as truly gone.
# Anything else (open/pending/None/an unrecognized string) must NOT be
# treated as safe to retry over: cancel_order() can "succeed" while the
# order still reads back live (eventual consistency), or fail outright, and
# a zero-fill order in either case may still be resting on the exchange.
_CANCELLED_TERMINAL_STATUSES = frozenset({"canceled", "cancelled", "closed", "expired", "rejected"})


class _SubmissionOutcomeUnknown(Exception):
    """Raised internally when an order-submission exception's outcome could
    not be confirmed one way or the other (the reconciliation lookup itself
    failed). Caught in execute() to hold back — no fill recorded, no
    rejection recorded, no further order placed — rather than guessing.
    2026-09-18 review finding: the old code equated 'could not verify' with
    'confirmed did not happen' at every submission path and fell back to
    placing another order, risking a duplicate against a request whose
    response was merely lost, not refused."""


class _SubmissionAborted(Exception):
    """Raised internally when a durable pre-submission intent could not be
    persisted (_save_state() failed) — the exchange was NEVER contacted for
    this attempt. Distinct from _SubmissionOutcomeUnknown: there, the
    exchange WAS contacted and the outcome is genuinely unclear; here we
    know FOR CERTAIN nothing was submitted, so a caller can safely treat
    this exactly like a confirmed non-event (e.g. safe to re-arm a SELL's
    native stop, since the position provably didn't change — unlike the
    unknown case, where doing so could be wrong). 2026-09-18 PASS-3 review
    finding: the old code proceeded to submit anyway after a failed
    pre-submit persistence, leaving no recovery record for an order that
    might still be accepted."""


def _raw_ordertype(order: dict) -> str:
    """Raw Kraken descr.ordertype string from a ccxt-parsed order dict
    (fetch_open_orders etc.) — see _is_native_stop_order for why the raw
    field is needed instead of ccxt's unified 'type'."""
    try:
        return order.get("info", {}).get("descr", {}).get("ordertype", "")
    except AttributeError:
        return ""


def _is_native_stop_order(order: dict) -> bool:
    """True if a ccxt-parsed order dict (from fetch_open_orders etc.) is one
    of the two ordertypes this bot's own native-stop logic ever places."""
    return _raw_ordertype(order) in _NATIVE_STOP_ORDERTYPES


def _match_by_client_order_id(orders: list[dict], client_order_id: str) -> dict | None:
    """Find the order (open or closed) whose clientOrderId matches — ccxt's
    kraken.py parses this from Kraken's own cl_ord_id field. Returns None
    if not found; ambiguity (more than one match, which should be
    impossible for a UUID we generated ourselves) logs and returns None
    rather than guessing."""
    matches = [o for o in orders if o.get("clientOrderId") == client_order_id]
    if len(matches) > 1:
        logger.error(
            "Multiple orders matched clientOrderId %s — this should be "
            "impossible for a freshly generated UUID, not auto-adopting any",
            client_order_id,
        )
        return None
    return matches[0] if matches else None


def _resting_order_quantity(order: dict) -> float | None:
    """Unfilled quantity still resting for a ccxt-parsed order — 'remaining'
    when ccxt computed it, else the full 'amount' (both ultimately sourced
    from Kraken's raw 'vol'/'vol_exec' fields via ccxt's kraken.py
    parse_order — confirmed by reading the installed ccxt 4.5.56 source:
    `amount = self.safe_string(order, 'vol', amount)`, `filled =
    self.safe_string(order, 'vol_exec')`, with safe_order() deriving
    'remaining'). None if neither field is present/parseable (e.g. a test
    double, or a genuinely malformed response) — callers must treat None as
    'unknown, skip the check', not as a mismatch."""
    for key in ("remaining", "amount"):
        val = order.get(key)
        if val is None:
            continue
        try:
            return float(val)
        except (TypeError, ValueError):
            continue
    return None


def _extract_stop_trigger(order: dict) -> tuple[float | None, float | None]:
    """From a ccxt-parsed order's raw info (order['info']), extract
    (stop_price, trailing_pct) for whichever kind of native stop this is.
    Reads Kraken's raw fields directly rather than ccxt's unified 'price' —
    ccxt deliberately nulls unified 'price' for a trailing-stop order (it's
    a relative '+X%' string, not an absolute price — see kraken.py
    parse_order: `if price.endswith('%'): price = None`), so the raw
    descr.price / stopprice fields are the only place this is available.
    Returns (None, None) if neither is parseable. Used to resize a resting
    stop at the SAME level/trailing-pct it already had (Gap A fix) or to
    adopt an untracked order's real level (Gap B fix) — always read back
    from the exchange's own order, never recomputed from avg_entry/ATR."""
    info = order.get("info", {}) or {}
    descr = info.get("descr", {}) or {}
    price_field = str(descr.get("price", ""))
    if price_field.endswith("%"):
        try:
            return None, abs(float(price_field.rstrip("%"))) / 100.0
        except ValueError:
            return None, None
    try:
        stop_price = float(info.get("stopprice", 0) or 0)
    except (TypeError, ValueError):
        return None, None
    return (stop_price if stop_price > 0 else None), None


class LiveExecutor:
    """
    Places real market orders on Kraken via ccxt.
    Interface identical to PaperExecutor.
    """

    # Exchange balance must exceed state-file position by at least this much
    # before the external-holdings guard fires (avoids false positives from
    # sub-satoshi rounding differences between Kraken and state files).
    _EXTERNAL_THRESHOLD = 1e-5  # 10 satoshis / 10 DOGE / etc.

    # Gap A (2026-08-20 follow-up, see _reconcile_resting_stop_quantity):
    # tolerance for comparing a resting native stop order's own volume
    # against the current position — same satoshi-scale magnitude as
    # _EXTERNAL_THRESHOLD above, for the same "don't false-positive on
    # floating-point/rounding noise" reason.
    _STOP_QTY_MISMATCH_THRESHOLD = 1e-5

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
        native_stop_loss_enabled: bool  = False,
        max_slippage_pct:         float = 0.0,
    ):
        self.symbol                    = symbol
        self.dry_run                   = dry_run
        self._order_type               = order_type
        self._starting_cash            = starting_cash
        self._state_path               = state_path
        self._adopt_external_holdings  = adopt_external_holdings
        self._native_stop_loss_enabled = native_stop_loss_enabled
        self._max_slippage_pct         = max_slippage_pct
        self._portfolio                = Portfolio(cash=starting_cash)
        self._fills:      list[Order]  = []
        self._rejects:    list[Order]  = []
        self._fees_paid:           float = 0.0
        self._bot_opened_position: bool  = False
        # Durable fill journal (2026-09-18 review finding, P1-3): portfolio
        # state (cash/position/pnl) is updated and persisted to
        # self._state_path INSIDE execute() itself, but the trade_log
        # (SQLite) write happens separately, afterward, in bot/main.py — a
        # crash between those two writes leaves holdings updated with no
        # fill record anywhere. This field is set to a full recovery
        # record (everything needed to reconstruct that trade_log row) in
        # the SAME _save_state() call that first persists the fill, and
        # only cleared once the caller confirms the trade_log write
        # actually happened (ack_journal_entry()). A restart that finds
        # this non-None found a fill whose accounting effect is real and
        # persisted, but whose external trade-log row may be missing —
        # bot/main.py replays it into trade_log before acking. This closes
        # the specific crash window between accounting and logging; it is
        # not a general transactional journal across every write in the
        # system.
        # 2026-09-18 FOLLOW-UP review finding (P1): a single dict here meant
        # a second fill recorded before the first was acked silently
        # OVERWROTE the first's still-pending recovery record. Now a list —
        # every unacked fill survives independently until its own ack.
        self._pending_journal_entries: list[dict] = []
        # 2026-09-18 PASS-3 review finding (P1): a monotonic in-memory
        # counter here was NOT persisted, so it reset to 0 on every
        # restart — a native stop's order_id is the SAME across each of
        # its own partial-fill deltas, so a post-restart delta could reuse
        # an exec_key already used (and acked) BEFORE the restart,
        # silently discarding a genuinely new fill as "already recorded".
        # Replaced entirely: exec_key now comes from Order.exec_key (a
        # fresh UUID assigned once per Order at construction, requiring no
        # persisted counter and no restart-collision risk — see
        # bot/execution/executor.py's Order dataclass).
        # 2026-09-18 PASS-3 review finding (P0): a single dict here meant an
        # opposite-side submission (a SELL while a BUY was still pending)
        # could silently overwrite the only tracked entry, and a
        # successfully-ACKNOWLEDGED-but-not-yet-SETTLED order (accepted,
        # still open) cleared this immediately — leaving nothing to stop a
        # second execute() call from submitting again while the first was
        # still genuinely unresolved. Now keyed by role ("buy"/"sell" for
        # ordinary trade submissions, "protect" for native-stop placement)
        # so each has its own independent slot, and only cleared once the
        # caller (execute() / _place_native_stop) has confirmed a TERMINAL
        # outcome — not merely that the initial network call didn't raise.
        self._pending_submissions: dict[str, dict] = {}
        # 2026-09-18 PASS-4 review finding (P0): the ordinary BUY/SELL fill
        # path treated ANY positive filled quantity as a completed order —
        # a genuinely still-OPEN order with a partial fill (status="open",
        # filled=0.001 out of amount=0.002) was recorded as a one-shot
        # FILLED order for the full observed quantity, the remaining
        # resting quantity became completely untracked, and the pending-
        # submission slot was cleared as if the order were done. Generic,
        # role-keyed (buy/sell) cumulative tracker — same delta discipline
        # _native_stop_last_recorded_* already gives native stops (kept
        # separate under role "protect", not unified here, to avoid
        # destabilizing that already-tested mechanism in the same change).
        self._order_progress: dict[str, dict] = {}
        # 2026-09-18 follow-up review finding (P0): a native stop that is
        # PARTIALLY filled but still OPEN (resting for the remainder) was
        # being treated as fully resolved the instant any fill was seen,
        # clearing the tracked id while the real order was still live on
        # the exchange. Tracks how much of the CURRENT native stop's fill
        # has already been recorded, so only the NEW delta is booked on
        # each check and the id is retained until a genuinely terminal
        # status is observed. Reset to 0 whenever a fresh stop is placed.
        self._native_stop_last_recorded_filled: float = 0.0
        # 2026-09-18 PASS-3 review finding (P1): the code above tracked
        # cumulative QUANTITY to compute a fill delta, but then applied the
        # order's CUMULATIVE average price and CUMULATIVE fee to that
        # delta quantity — wrong proceeds when price moves between partial
        # fills, and double-charged fees already deducted by a prior
        # delta. These track cumulative cost (quote-currency) and fee so
        # the delta's own price/fee can be derived by subtraction, exactly
        # like the quantity delta already is.
        self._native_stop_last_recorded_cost: float = 0.0
        self._native_stop_last_recorded_fee:  float = 0.0
        # Set False if a fill's accounting update could not be durably
        # persisted (_save_state() raised) — execute() then refuses new
        # BUYs until a later save succeeds, rather than trading on top of
        # state that may not survive a crash/restart.
        self._state_write_healthy: bool = True
        # Resting exchange-side stop-loss order — a backstop that survives the
        # bot process dying (VPS outage, crash loop, etc.). Usually static:
        # placed once per fill at whatever SL price main.py already computed
        # (fixed % or ATR), never repriced. When main.py's software trailing
        # stop is itself active (ATR unavailable + TRAILING_STOP_PCT set), it
        # gets swapped for a native Kraken trailing-stop order instead — see
        # sync_protective_stop() and _place_native_trailing_stop().
        self._native_stop_order_id:    str | None   = None
        self._native_stop_price:       float | None = None
        self._native_stop_is_trailing: bool         = False
        # PASS-7 review finding (P1, finding 4): a transient failure
        # verifying a native stop's FINAL state (it's absent from open
        # orders, or the position went flat) used to permanently discard
        # the ability to recover that order's execution/P&L — a NEXT,
        # healthy restart had nothing left telling it which historical
        # order to check. This holds the reference (plus the baseline it
        # must be compared against, captured at the moment it was lost —
        # NOT the currently-tracked stop's baseline, which may by then
        # belong to an entirely different replacement order) durably,
        # independent of _native_stop_order_id (which is always cleared
        # immediately — we DO know the order is no longer live/tracked,
        # we just don't yet know its outcome). Retried every tick (see
        # reconcile_pending_orders) and at every subsequent startup until
        # it resolves.
        #
        # PASS-8 review finding (P1, finding 3): a SINGLE slot silently
        # replaced an already-pending order's frozen baseline/basis the
        # moment a SECOND historical order needed the same treatment
        # (e.g. the original stop is still unresolved when its
        # replacement, covering residual inventory, ALSO later goes
        # unresolved) — permanently losing the first one's recovery.
        # A list, keyed by order_id per entry, so multiple historical
        # orders are tracked and retried independently.
        self._unresolved_stop_recoveries: list = []
        # PASS-7 review finding (P0): captured ONCE, immediately after
        # _load_state() and BEFORE _sync_cash()/_sync_position() can
        # zero cost_basis/position for a fully-offline-filled stop —
        # startup recovery of that stop's P&L must use THIS preserved
        # basis, never the (by-then-zeroed) live self._portfolio._cost_basis.
        self._startup_recovery_cost_basis: float = 0.0
        self._startup_recovery_position:   float = 0.0
        # PASS-7 review — carried-over correctness gap, now closed: a fill
        # discovered deep inside _rearm_native_stop_after_failed_sell()
        # (a rejected SELL's stop re-placement turning out to already be
        # filled) has nowhere to go via execute()'s own single-Order
        # return contract. Queued here instead — drained by bot/main.py
        # every tick via drain_discovered_fills(), alongside
        # reconcile_pending_orders(), and routed through the SAME
        # PositionManager/state-machine/capital-pool/risk/trade-log
        # consumer any other discovered fill gets. Deliberately NOT
        # persisted: see _rearm_native_stop_after_failed_sell's own
        # docstring for why a crash before the next drain is not a
        # correctness gap either.
        self._pending_discovered_fills: list = []
        # Set by _place_limit_order when a post-only limit silently degrades to
        # a market (taker) order; read + cleared once per fill in execute().
        self._maker_fallback_reason:   str | None   = None
        self._alerter = TelegramAlerter(
            cfg.alerts.telegram_bot_token,
            cfg.alerts.telegram_chat_id,
            enabled=cfg.alerts.telegram_enabled,
        )

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
        # PASS-7 review finding (P0): capture the JUST-LOADED cost basis/
        # position BEFORE _sync_cash()/_sync_position() get any chance to
        # zero/overwrite them (which _sync_position() does the moment the
        # exchange shows the position fully closed — exactly the case
        # where a native stop that fully executed offline needs ITS
        # original basis to compute real P&L, not the post-sync zero).
        #
        # Only (re-)capture when the JUST-LOADED position is still > 0, or
        # there is genuinely NOTHING outstanding that depends on the
        # preserved snapshot. If position is ALREADY 0 as loaded from disk
        # while something is STILL outstanding, a PRIOR startup attempt
        # already zeroed the live portfolio fields and crashed before
        # finishing recovery — _load_state() just restored the CORRECT
        # preserved snapshot from THAT attempt's own save into these same
        # fields; re-deriving from the now-zeroed live cost_basis here
        # would clobber it with 0, permanently losing the original basis
        # on this second attempt. Reproduced exactly by injecting a crash
        # between _sync_position() and recovery finishing, then retrying:
        # without this guard the retry computed +$156 fabricated profit
        # again instead of the real -$14 loss.
        #
        # PASS-8 review finding (P1, finding 1): "outstanding" is not just
        # _native_stop_order_id — a PENDING 'protect' submission (a
        # placement accepted before tracking was ever committed) or an
        # already-queued unresolved historical order carries the EXACT
        # same dependency on this snapshot, and the old guard's `not
        # self._native_stop_order_id` treated their presence as "nothing
        # to protect", clobbering the snapshot with 0 on the very next
        # restart. Reproduced exactly: a pending protect submission (no
        # _native_stop_order_id set at all) fully fills offline; restart
        # recovered +$156 fabricated profit instead of the real -$14 loss.
        _outstanding = (
            self._native_stop_order_id
            or self._pending_submissions.get("protect")
            or self._unresolved_stop_recoveries
        )
        if self._portfolio.position > 0 or not _outstanding:
            self._startup_recovery_cost_basis = self._portfolio._cost_basis
            self._startup_recovery_position   = self._portfolio.position
        # 2026-09-18 review finding (P1-7): a startup cash/position sync
        # failure fell back to configured starting_cash / the last saved
        # position (fictional/stale numbers, not the real exchange state)
        # and alerted, but exposed no persistent flag a caller could gate
        # new BUYs on — the bot could open a fresh position sized against
        # capital it never actually confirmed having. Startup-only: these
        # syncs run once in __init__, so this reflects THIS process's
        # startup only, not a live health check re-evaluated per tick.
        self._startup_sync_healthy: bool = True
        if not dry_run:
            base  = symbol.split("/")[0]
            quote = symbol.split("/")[1]
            # PASS-7 review finding (P0): persist the preserved basis NOW,
            # in its own dedicated (already-part-of-_save_state) fields —
            # before _sync_position() can save the portfolio's own
            # cost_basis/position as zero. A crash between here and
            # finishing native-stop startup recovery still leaves this
            # snapshot durable for the NEXT restart to recover from,
            # rather than re-deriving it from what would by then be an
            # already-zeroed on-disk _portfolio._cost_basis.
            if self._native_stop_order_id:
                self._save_state()
            # 2026-09-19 PASS-5 review finding (P1, carried over from
            # PASS-4 finding 3): must run BEFORE _sync_cash()/_sync_position()
            # — see _reconcile_pending_orders_at_startup()'s own docstring
            # for why recovering a pending order AFTER those syncs have
            # already re-baselined cash/position from the exchange's
            # current (already-inclusive) balance double-counts its effect.
            self._reconcile_pending_orders_at_startup()
            exchange_cash, sync_error = self._sync_cash()
            self._portfolio.cash = exchange_cash
            if sync_error:
                self._startup_sync_healthy = False
            self._sync_position(symbol)
            if self._native_stop_loss_enabled:
                self._verify_resting_stop_on_startup()

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

    def _reconcile_pending_orders_at_startup(self) -> None:
        """PASS-5 review finding (P1, carried over unaddressed from PASS-4
        finding 3): a pending BUY/SELL submission recovered ACROSS A
        RESTART must never have its cash effect applied through the
        ordinary delta-application path — _sync_cash()/_sync_position()
        (called immediately after this, from __init__) re-baseline cash/
        position directly from the exchange's CURRENT free balance, which
        by definition ALREADY reflects every fill that ever happened,
        including this pending one (Kraken nets out fees automatically
        too, so the fee is already absorbed into that balance as well).
        Applying the SAME fill's delta again on top of that freshly-synced
        cash double-counts it.

        Reproduced exactly: start $1,000 cash. A 0.001 BTC BUY at $90,000
        fills; the process dies after the submission wrapper persisted
        acceptance but before local fill accounting ran. Restart with
        exchange CAD cash $910 and BTC 0.001. The OLD code recovered the
        pending order through ordinary execute() accounting, which
        deducted ANOTHER $90 from the already-$910 balance, landing at
        $820 instead of $910.

        This is purely a JOURNAL/REPORTING reconciliation, run BEFORE
        _sync_cash()/_sync_position(): it records the fill in
        pending_journal_entries (so it reaches TradeLog — a real execution
        must never go completely unlogged just because it was crash-
        recovered) and seeds position/cost_basis/bot_opened_position/
        realized_pnl/fees_paid — the fields the exchange does NOT report
        directly — so the upcoming _sync_position() call's own "position
        confirmed from exchange" path takes over cleanly instead of
        misclassifying the recovered fill as an untracked ambient/external
        balance (its "prev_position < 1e-9 and not bot_opened_position"
        branch would otherwise silently drop a genuine BUY's resulting
        position). Cash itself is deliberately left completely untouched
        here — _sync_cash(), called right after this, is the sole source
        of truth for it; re-deriving it here would be exactly the
        double-count this method exists to prevent.

        Reuses the SAME pure _record_order_delta() progress tracking as
        the live execute() path, so a submission only partially recorded
        before the crash (some of it already durably applied pre-crash)
        recovers only the genuinely NEW remainder, not the full cumulative
        amount again. Only ever processes an order it can positively
        identify as confirmed terminal or confirmed never-placed — anything
        still genuinely unresolved (a lookup failure, or truly still open)
        is left exactly as persisted, for the normal execute()-time
        reconciliation path (which already handles this correctly for the
        same-process, no-restart case) to keep retrying later."""
        quote = self.symbol.split("/")[1]
        _dirty = False
        for role in ("buy", "sell"):
            entry = self._pending_submissions.get(role)
            if entry is None:
                continue
            ccxt_side = entry.get("side", role)
            adopted, confirmed_empty = self._find_untracked_entry_order(
                ccxt_side, entry.get("client_order_id"), order_id=entry.get("order_id"),
            )
            if adopted is None:
                if confirmed_empty:
                    logger.warning(
                        "STARTUP RECONCILIATION [%s/%s]: prior unresolved "
                        "submission confirmed never placed — clearing.",
                        self.symbol, role,
                    )
                    del self._pending_submissions[role]
                    _dirty = True
                # else: the lookup itself failed — leave exactly as
                # persisted; the normal execute()-time path retries later.
                continue

            status      = str(adopted.get("status") or "").lower()
            is_terminal = status in _CANCELLED_TERMINAL_STATUSES
            filled      = float(adopted.get("filled") or 0.0)

            if filled <= 0:
                if is_terminal:
                    logger.warning(
                        "STARTUP RECONCILIATION [%s/%s]: order %s confirmed "
                        "%s with no fill — clearing.",
                        self.symbol, role, adopted.get("id"), status,
                    )
                    self._order_progress.pop(role, None)
                    del self._pending_submissions[role]
                    _dirty = True
                continue

            cumulative_cost = float(adopted.get("cost") or 0.0)
            if cumulative_cost <= 0:
                _avg = float(adopted.get("average") or adopted.get("price") or 0.0)
                cumulative_cost = _avg * filled
            fee_data     = adopted.get("fee") or {}
            fee_cost     = float(fee_data.get("cost") or 0.0)
            fee_currency = fee_data.get("currency") or quote
            order_id_str = str(adopted.get("id", ""))

            delta_qty, delta_cost, delta_fee = self._record_order_delta(
                role, order_id_str, filled, cumulative_cost, fee_cost,
            )
            self._pending_submissions[role] = {**entry, "order_id": order_id_str}
            _dirty = True

            if delta_qty <= 0:
                if delta_fee > 0:
                    self._record_fee_adjustment_journal_entry(
                        order_id_str, delta_fee, fee_currency,
                    )
                if is_terminal:
                    self._order_progress.pop(role, None)
                    del self._pending_submissions[role]
                continue

            delta_price = delta_cost / delta_qty if delta_qty > 0 else 0.0
            pnl = None
            if ccxt_side == "buy":
                prev_cost = self._portfolio._cost_basis * self._portfolio.position
                self._portfolio.position += delta_qty
                self._portfolio._cost_basis = (
                    (prev_cost + delta_price * delta_qty) / self._portfolio.position
                    if self._portfolio.position > 0 else 0.0
                )
                self._bot_opened_position = True
            else:
                pnl = (delta_price - self._portfolio._cost_basis) * delta_qty
                self._portfolio.realized_pnl += pnl
                self._portfolio.position      = max(0.0, self._portfolio.position - delta_qty)
                if self._portfolio.position == 0:
                    self._portfolio._cost_basis = 0.0
                    self._bot_opened_position   = False

            if delta_fee > 0 and fee_currency == quote:
                self._fees_paid += delta_fee
                # Deliberately NOT deducted from cash here — _sync_cash(),
                # called right after this method returns, already reflects
                # the post-fee exchange balance.

            order = Order(
                order_id     = order_id_str,
                symbol       = self.symbol,
                side         = OrderSide.BUY if ccxt_side == "buy" else OrderSide.SELL,
                quantity     = delta_qty,
                price        = delta_price,
                status       = OrderStatus.FILLED,
                created_at   = datetime.now(timezone.utc),
                filled_at    = datetime.now(timezone.utc),
                fee_cost     = delta_fee,
                fee_currency = fee_currency,
                pnl          = pnl,
            )
            self._fills.append(order)
            self._record_pending_journal_entry(order)
            logger.warning(
                "STARTUP RECONCILIATION [%s/%s]: recovered a %s fill of "
                "%.8f @ %.2f (order %s) — journaled; cash left for the "
                "upcoming exchange sync to establish, not double-applied "
                "here.", self.symbol, role, ccxt_side, delta_qty, delta_price, order_id_str,
            )
            if is_terminal:
                self._order_progress.pop(role, None)
                del self._pending_submissions[role]

        if _dirty:
            self._save_state()

    def reconcile_pending_orders(self) -> "list[Order]":
        """PASS-5 review finding (P1): a pending BUY/SELL submission must
        be reconciled independently of a FRESH trade signal for the same
        role — the state machine suppresses further BUY signals once
        LONG, so a partially-filled BUY's remaining quantity (or a pending
        SELL that finishes on its own) could otherwise sit completely
        unresolved indefinitely: no accounting update, no fee/ledger
        entry, no capital-pool allocation, no protective-stop resize —
        until some UNRELATED event happened to touch that role again.
        Call this once per tick, for every symbol, BEFORE evaluating any
        new signal — never require a fresh trade decision merely to
        finish accounting for an order that already resolved on the
        exchange.

        Unlike _reconcile_pending_orders_at_startup() (which runs BEFORE
        _sync_cash()/_sync_position() re-baseline cash/position from the
        exchange's current balance, and so must NEVER apply a cash/
        position delta itself — see that method's own docstring), this is
        the ordinary SAME-PROCESS case: nothing else re-baselines cash/
        position between ticks, so a genuinely new delta is applied
        through the EXACT same portfolio-mutation logic execute() uses.

        Returns every fill Order discovered this way (empty list if
        nothing changed) — callers must route each through the SAME
        bookkeeping consumer a normal execute() fill gets (PositionManager
        / state machine / capital pool / risk / trade log), exactly like a
        discovered native-stop fill already is via
        _process_discovered_sell_fill / _process_discovered_buy_fill.
        Never raises — same "must never crash the trading loop" contract
        as sync_protective_stop().

        PASS-7 review finding (P1, finding 4): also retries any
        previously-unresolved historical native-stop recovery every tick
        (not just at the next restart) — a transient final-state lookup
        failure must not have to wait for a restart to resolve."""
        self._retry_unresolved_stop_recoveries()
        discovered: list[Order] = []
        quote = self.symbol.split("/")[1]
        for role in ("buy", "sell"):
            entry = self._pending_submissions.get(role)
            if entry is None:
                continue
            try:
                ccxt_side = entry.get("side", role)
                adopted, confirmed_empty = self._find_untracked_entry_order(
                    ccxt_side, entry.get("client_order_id"), order_id=entry.get("order_id"),
                )
                if adopted is None:
                    if confirmed_empty:
                        logger.warning(
                            "PENDING RECONCILIATION [%s/%s]: prior "
                            "unresolved submission confirmed never placed "
                            "— clearing.", self.symbol, role,
                        )
                        del self._pending_submissions[role]
                        self._save_state()
                    continue

                status       = str(adopted.get("status") or "").lower()
                is_terminal  = status in _CANCELLED_TERMINAL_STATUSES
                filled       = float(adopted.get("filled") or 0.0)
                order_id_str = str(adopted.get("id", ""))

                if filled <= 0:
                    if is_terminal:
                        logger.warning(
                            "PENDING RECONCILIATION [%s/%s]: order %s "
                            "confirmed %s with no fill — clearing.",
                            self.symbol, role, order_id_str, status,
                        )
                        self._order_progress.pop(role, None)
                        del self._pending_submissions[role]
                        self._save_state()
                    continue

                cumulative_cost = float(adopted.get("cost") or 0.0)
                if cumulative_cost <= 0:
                    _avg = float(adopted.get("average") or adopted.get("price") or 0.0)
                    cumulative_cost = _avg * filled
                fee_data      = adopted.get("fee") or {}
                fee_cost_raw  = float(fee_data.get("cost") or 0.0)
                fee_currency  = fee_data.get("currency") or quote

                delta_qty, delta_cost, delta_fee = self._record_order_delta(
                    role, order_id_str, filled, cumulative_cost, fee_cost_raw,
                )
                if self._pending_submissions.get(role):
                    self._pending_submissions[role] = {**entry, "order_id": order_id_str}

                if delta_qty <= 0:
                    if delta_fee > 0:
                        self._apply_ordinary_order_fee_only_adjustment(
                            role, order_id_str, delta_fee, fee_currency,
                        )
                    if is_terminal:
                        self._order_progress.pop(role, None)
                        del self._pending_submissions[role]
                    self._save_state()
                    continue

                delta_price = delta_cost / delta_qty if delta_qty > 0 else 0.0
                total_value = delta_price * delta_qty
                pnl = None
                if ccxt_side == "buy":
                    prev_cost = self._portfolio._cost_basis * self._portfolio.position
                    self._portfolio.cash        -= total_value
                    self._portfolio.position    += delta_qty
                    self._portfolio._cost_basis = (
                        (prev_cost + delta_price * delta_qty) / self._portfolio.position
                        if self._portfolio.position > 0 else 0.0
                    )
                    self._bot_opened_position = True
                else:
                    pnl = (delta_price - self._portfolio._cost_basis) * delta_qty
                    self._portfolio.realized_pnl += pnl
                    self._portfolio.cash          += total_value
                    self._portfolio.position       = max(0.0, self._portfolio.position - delta_qty)
                    if self._portfolio.position == 0:
                        self._portfolio._cost_basis = 0.0
                        self._bot_opened_position    = False

                if delta_fee > 0:
                    if fee_currency != quote:
                        logger.warning(
                            "Pending-reconciliation fee currency mismatch "
                            "[%s/%s]: fee=%.6f %s but quote=%s — not "
                            "deducting.", self.symbol, role, delta_fee,
                            fee_currency, quote,
                        )
                    else:
                        self._portfolio.cash -= delta_fee
                        self._fees_paid      += delta_fee

                order = Order(
                    order_id     = order_id_str,
                    symbol       = self.symbol,
                    side         = OrderSide.BUY if ccxt_side == "buy" else OrderSide.SELL,
                    quantity     = delta_qty,
                    price        = delta_price,
                    status       = OrderStatus.FILLED,
                    created_at   = datetime.now(timezone.utc),
                    filled_at    = datetime.now(timezone.utc),
                    fee_cost     = delta_fee,
                    fee_currency = fee_currency,
                    pnl          = pnl,
                )
                self._fills.append(order)
                self._record_pending_journal_entry(order)
                if is_terminal:
                    self._order_progress.pop(role, None)
                    del self._pending_submissions[role]
                self._save_state()
                discovered.append(order)
                logger.warning(
                    "PENDING RECONCILIATION [%s/%s]: recovered a %s fill "
                    "of %.8f @ %.2f (order %s) independent of a new "
                    "signal.", self.symbol, role, ccxt_side, delta_qty,
                    delta_price, order_id_str,
                )
            except Exception as exc:
                logger.error(
                    "PENDING RECONCILIATION [%s/%s] FAILED: %s — leaving "
                    "state untouched, will retry next tick.",
                    self.symbol, role, exc,
                )
        return discovered

    def drain_discovered_fills(self) -> "list[Order]":
        """PASS-6/PASS-7 review — carried-over correctness gap, now
        closed: returns and clears every fill queued by
        _rearm_native_stop_after_failed_sell() (a rejected SELL's
        stop-replacement turning out to already be filled — the one
        remaining case where a discovered fill has no return-value path
        back to bot/main.py's bookkeeping consumer, since it happens deep
        inside execute()'s own reject-handling, which must still return
        that call's own REJECTED Order). Call this once per tick, exactly
        like reconcile_pending_orders() — every entry must be routed
        through the SAME PositionManager/state-machine/capital-pool/risk/
        trade-log consumer any other discovered fill gets."""
        drained = self._pending_discovered_fills
        self._pending_discovered_fills = []
        return drained

    def _sync_cash(self) -> tuple[float, str | None]:
        """
        Fetch free balance in the quote currency from the exchange.
        Returns (cash, error_msg): error_msg is None on success, or the reason
        we fell back to starting_cash.
        """
        quote = self.symbol.split("/")[1]
        try:
            balance = fetch_with_retry(
                self._exchange.fetch_balance,
                label=f"balance sync [{self.symbol}]",
            )
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
            self._alerter.error(
                f"LiveExecutor startup balance sync FAILED for {self.symbol} after retries: {msg}"
                f" — falling back to starting_cash=${self._starting_cash:.2f}."
                f" Verify real exchange balance manually."
            )
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
            balance        = fetch_with_retry(
                self._exchange.fetch_balance,
                label=f"position sync [{symbol}]",
            )
            exchange_free  = float(balance.get("free",  {}).get(base, 0.0))
            exchange_total = float(balance.get("total", {}).get(base, 0.0))
        except Exception as exc:
            logger.warning("_sync_position: fetch_balance failed — %s", exc)
            self._alerter.error(
                f"LiveExecutor startup position sync FAILED for {symbol} after retries: {exc}"
                f" — managed position left at last saved state (external-holdings guard"
                f" and drift detection were skipped this startup)."
                f" Verify real exchange position manually."
            )
            self._startup_sync_healthy = False
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
                # On fetch failure, do NOT write a fabricated 0.0 (that would
                # overstate realized P&L on the next SELL by the full proceeds) —
                # leave cost_basis at whatever _load_state() restored and warn.
                try:
                    current_price = float(self._exchange.fetch_ticker(symbol)["last"])
                except Exception as exc:
                    logger.warning(
                        "STATE MISMATCH [%s]: exchange holds %.6f %s but saved"
                        " position=0, and fetch_ticker failed (%s) — cost_basis"
                        " NOT reseeded, left at saved value %.2f. Verify manually.",
                        self.symbol, exchange_total, base, exc, self._portfolio._cost_basis,
                    )
                    print(
                        f"  POSITION RESEED SKIPPED [{self.symbol}]:"
                        f" exchange {exchange_total:.6f} {base} vs saved-state"
                        f" mismatch, but fetch_ticker failed — cost_basis left at"
                        f" ${self._portfolio._cost_basis:,.2f} (verify manually).",
                        flush=True,
                    )
                else:
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

    # ── Native stop-loss (exchange-side backstop) ───────────────────────
    #
    # A resting stop order placed directly on Kraken so an open position is
    # still protected if the bot process itself is unavailable (crash loop,
    # VPS outage, extended network partition) — the software SL/TP path in
    # main.py only works while the bot is alive and polling. Deliberately
    # USUALLY STATIC: placed once per fill at whatever price main.py already
    # computed (fixed % or ATR), never repriced. The software SL/TP/trailing/
    # partial-TP logic is unaffected and keeps being the primary exit path
    # whenever the bot is running — this order is pure insurance, cancelled
    # the moment the bot exits the position itself. Kraken's 'stop-loss'
    # order type triggers as a market order (same reasoning as urgent=True
    # everywhere else in this file: a stop exit must never sit in a limit
    # book while price runs away).
    #
    # EXCEPTION — native trailing: when main.py's own trailing-stop logic is
    # active (TRAILING_STOP_PCT>0 and the software ATR SL is unavailable —
    # see sync_protective_stop() below), the backstop is instead placed as a
    # Kraken 'trailing-stop' order (ordertype derived by ccxt from the
    # trailingPercent param). Unlike the static order, Kraken's own matching
    # engine tracks the peak and reprices the trigger itself — no repeated
    # placement calls needed as price moves favorably, only on a quantity
    # change (partial TP / partial fill), which still requires cancel+replace
    # since order volume can't be amended via create_order.

    @property
    def has_resting_stop(self) -> bool:
        return self._native_stop_order_id is not None

    def _journal_native_stop_execution_without_cash_effect(
        self, order_id: str, filled_qty: float, fill_price: float,
        fee_cost: float, fee_currency: str, cost_basis: "float | None" = None,
    ) -> Order:
        """PASS-6 review finding (P1): the startup-safe counterpart to
        _record_stop_triggered_fill. That method is for the LIVE, mid-run
        case where NOTHING else has yet applied the fill's cash/position
        effect. At startup, _sync_cash()/_sync_position() (which always
        run before any native-stop startup reconciliation) ALREADY
        reflect every execution that happened on the exchange, including
        one this executor never got to record locally before a crash —
        re-applying cash/position here would double-count it. But
        realized P&L and fees_paid are NOT established by the exchange
        sync (Kraken's free balance doesn't report "realized P&L"), and
        the execution itself still needs a journal entry — skipping those
        is exactly PASS-6 finding 1: a real fill and its loss disappearing
        entirely from the recovery journal, even though cash converged.

        cost_basis (PASS-7 review finding, P0): defaults to the CURRENT
        self._portfolio._cost_basis, but the caller may pass the
        PRESERVED pre-sync basis explicitly — when a stop fully executes
        offline, _sync_position() zeroes cost_basis (position is now 0)
        BEFORE this ever runs, so reading the current value here would
        compute P&L against a basis that no longer reflects what was
        actually paid for the position being closed (reproduced exactly:
        cost basis $85,000 -> zeroed -> P&L computed against $0 fabricated
        +$156 "profit" on a real $14 loss)."""
        quote = self.symbol.split("/")[1]
        _cost_basis = self._portfolio._cost_basis if cost_basis is None else cost_basis
        pnl = (fill_price - _cost_basis) * filled_qty
        self._portfolio.realized_pnl += pnl
        if fee_cost > 0:
            if fee_currency and fee_currency != quote:
                logger.warning(
                    "Native-stop startup-recovery fee currency mismatch "
                    "[%s]: fee=%.6f %s but quote=%s — not counted (manual "
                    "reconciliation needed)", self.symbol, fee_cost, fee_currency, quote,
                )
            else:
                self._fees_paid += fee_cost
        order = Order(
            order_id     = f"native-stop:{order_id}",
            symbol       = self.symbol,
            side         = OrderSide.SELL,
            quantity     = filled_qty,
            price        = fill_price,
            status       = OrderStatus.FILLED,
            created_at   = datetime.now(timezone.utc),
            filled_at    = datetime.now(timezone.utc),
            fee_cost     = fee_cost,
            fee_currency = fee_currency,
            pnl          = pnl,
        )
        self._fills.append(order)
        self._record_pending_journal_entry(order)
        logger.warning(
            "STARTUP RECONCILIATION [%s/protect]: recovered a stop-"
            "triggered exit of %.8f @ %.2f (order %s) that happened "
            "before this restart — journaled; cash/position already "
            "reflected via the exchange sync.",
            self.symbol, filled_qty, fill_price, order_id,
        )
        return order

    def _recover_missed_native_stop_execution(
        self, order: dict, *,
        baseline_filled: "float | None" = None,
        baseline_cost:   "float | None" = None,
        baseline_fee:    "float | None" = None,
        cost_basis_override: "float | None" = None,
        advance_tracked_baseline: bool = True,
    ) -> None:
        """PASS-6/PASS-7 review findings (P1/P0): compares the order's
        CURRENT cumulative filled/cost/fee against a baseline — any
        positive delta is a real execution that happened but was never
        journaled (a crash prevented the normal in-process accounting
        from recording it). Journals it (P&L included) via the cash-free
        helper above, THEN advances the SHARED _native_stop_last_recorded_*
        baseline (unless advance_tracked_baseline=False — see below) — the
        exact same "commit progress and economics together" discipline
        _record_order_delta's callers use, just anchored to the exchange's
        already-synced truth instead of a fresh in-process fill.

        On an ORDINARY (non-crash) restart this is a safe no-op: the last
        successful save already advanced the baseline to match the
        order's cumulative state, so the delta is exactly zero.

        baseline_filled/cost/fee (PASS-7 review finding, P1, finding 4):
        default to reading the SHARED self._native_stop_last_recorded_*
        fields (the normal "this IS the currently tracked stop" case).
        Pass explicit values when recovering a HISTORICAL order that is
        NOT the currently tracked one (e.g. a retried _unresolved_stop_
        recovery entry) — the shared fields may by then belong to an
        entirely different (replacement) stop, and comparing against them
        would silently corrupt an unrelated order's own delta tracking.
        advance_tracked_baseline=False in that same situation: no
        currently-tracked stop should have ITS baseline touched by
        recovering a DIFFERENT, already-detached historical order.

        cost_basis_override (PASS-7 review finding, P0): see
        _journal_native_stop_execution_without_cash_effect's own
        docstring — required whenever _sync_position() may already have
        zeroed self._portfolio._cost_basis before this runs."""
        _bf   = self._native_stop_last_recorded_filled if baseline_filled is None else baseline_filled
        _bc   = self._native_stop_last_recorded_cost   if baseline_cost   is None else baseline_cost
        _bfee = self._native_stop_last_recorded_fee    if baseline_fee   is None else baseline_fee

        cumulative_filled = float(order.get("filled") or 0.0)
        new_delta = max(0.0, cumulative_filled - _bf)
        cumulative_cost = float(order.get("cost") or 0.0)
        if cumulative_cost <= 0:
            _avg = float(order.get("average") or order.get("price") or 0.0)
            cumulative_cost = _avg * cumulative_filled
        cumulative_fee = float((order.get("fee") or {}).get("cost") or 0.0)
        delta_cost = max(0.0, cumulative_cost - _bc)
        delta_fee  = max(0.0, cumulative_fee  - _bfee)
        fee_currency = (order.get("fee") or {}).get("currency", "") or self.symbol.split("/")[1]

        if new_delta > 0:
            delta_price = delta_cost / new_delta if new_delta > 0 else 0.0
            self._journal_native_stop_execution_without_cash_effect(
                str(order.get("id", "")), new_delta, delta_price, delta_fee, fee_currency,
                cost_basis=cost_basis_override,
            )
        elif delta_fee > 0:
            # PASS-7 review finding (P1, finding 3): a fee finalized while
            # offline, with the FILLED QUANTITY unchanged, used to just
            # advance the baseline below with no journal entry at all —
            # the correction was silently "consumed" (the baseline now
            # matches it) without ever reaching TradeLog. Cash-free, same
            # discipline as the quantity-delta branch above — never
            # fabricates a zero-quantity execution row.
            _quote = self.symbol.split("/")[1]
            if fee_currency == _quote:
                self._fees_paid += delta_fee
            self._record_fee_adjustment_journal_entry(
                f"native-stop:{order.get('id', '')}", delta_fee, fee_currency,
            )

        if advance_tracked_baseline:
            self._native_stop_last_recorded_filled = cumulative_filled
            self._native_stop_last_recorded_cost   = cumulative_cost
            self._native_stop_last_recorded_fee    = cumulative_fee

    def _resolve_pending_protect_submission_at_startup(self) -> None:
        """PASS-6 review finding (P1): ordinary buy/sell startup recovery
        (_reconcile_pending_orders_at_startup) never covered the 'protect'
        role — a native-stop PLACEMENT interrupted by a crash between
        persisting the submission intent and confirming/tracking its
        outcome sat unresolved indefinitely (nothing else re-checks a
        pending 'protect' submission independently of a position-changing
        event). Resolves it the same way _find_untracked_entry_order-based
        reconciliation always does, but WITHOUT applying cash/position
        (already reflected via the exchange sync that runs before this),
        journaling any discovered fill through the cash-free helper above."""
        entry = self._pending_submissions.get("protect")
        if entry is None:
            return
        adopted, confirmed_empty = self._find_untracked_entry_order(
            "sell", entry.get("client_order_id"), order_id=entry.get("order_id"),
        )
        if adopted is None:
            if confirmed_empty:
                logger.warning(
                    "STARTUP RECONCILIATION [%s/protect]: prior unresolved "
                    "placement confirmed never placed — clearing.", self.symbol,
                )
                del self._pending_submissions["protect"]
                self._save_state()
            return

        order_id     = str(adopted.get("id", ""))
        status       = str(adopted.get("status") or "").lower()
        is_terminal  = status in _CANCELLED_TERMINAL_STATUSES
        filled       = float(adopted.get("filled") or 0.0)
        is_trailing  = _raw_ordertype(adopted) == "trailing-stop"
        stop_price, _ = _extract_stop_trigger(adopted)

        if filled <= 0:
            if not is_terminal:
                # Genuinely resting, unfilled — start tracking it; nothing
                # to journal.
                self._native_stop_order_id    = order_id
                self._native_stop_price       = None if is_trailing else stop_price
                self._native_stop_is_trailing = is_trailing
                self._native_stop_last_recorded_filled = 0.0
                self._native_stop_last_recorded_cost   = 0.0
                self._native_stop_last_recorded_fee    = 0.0
            self._resolve_pending_submission("protect")
            self._save_state()
            return

        cumulative_cost = float(adopted.get("cost") or 0.0)
        if cumulative_cost <= 0:
            _avg = float(adopted.get("average") or adopted.get("price") or 0.0)
            cumulative_cost = _avg * filled
        fee_data     = adopted.get("fee") or {}
        fee_cost     = float(fee_data.get("cost") or 0.0)
        fee_currency = fee_data.get("currency") or self.symbol.split("/")[1]
        fill_price   = cumulative_cost / filled if filled > 0 else 0.0

        # PASS-8 review finding (P1, finding 1): this call was missing
        # cost_basis — defaulting to the CURRENT self._portfolio.
        # _cost_basis, which _sync_position() may have ALREADY zeroed if
        # this placement's fill closed the entire position while offline
        # (the exact same class of bug PASS-7's tracked-stop fix
        # addressed, just at this SEPARATE entry point: a placement whose
        # identity lives in pending_submissions['protect'] rather than
        # _native_stop_order_id). Reproduced exactly: cash correct at
        # $1,155.68, but gross P&L fabricated at +$156 instead of the
        # real -$14.
        self._journal_native_stop_execution_without_cash_effect(
            order_id, filled, fill_price, fee_cost, fee_currency,
            cost_basis=self._startup_recovery_cost_basis,
        )

        if is_terminal:
            self._clear_native_stop_tracking_fields()
        else:
            self._native_stop_order_id    = order_id
            self._native_stop_price       = None if is_trailing else stop_price
            self._native_stop_is_trailing = is_trailing
            self._native_stop_last_recorded_filled = filled
            self._native_stop_last_recorded_cost   = cumulative_cost
            self._native_stop_last_recorded_fee    = fee_cost
        self._resolve_pending_submission("protect")
        self._save_state()

    def _clear_native_stop_tracking_fields(self) -> None:
        """Shared "this identity is no longer the currently-tracked
        resting stop" reset — factored out (PASS-8 review) so every
        caller clears the exact same fields, in the exact same way."""
        self._native_stop_order_id    = None
        self._native_stop_price       = None
        self._native_stop_is_trailing = False
        self._native_stop_last_recorded_filled = 0.0
        self._native_stop_last_recorded_cost   = 0.0
        self._native_stop_last_recorded_fee    = 0.0

    def _queue_unresolved_stop_recovery(
        self, order_id: str, cost_basis: "float | None",
        baseline_filled: float, baseline_cost: float, baseline_fee: float,
    ) -> None:
        """PASS-8 review finding (P1, finding 3): add or UPDATE (never
        silently overwrite a DIFFERENT order's entry) a historical
        recovery reference in the durable multi-entry queue. A single-slot
        design silently replaced an already-pending order's frozen
        baseline/basis the moment a SECOND historical order needed the
        same treatment (e.g. the original stop is still unresolved when
        its replacement, covering residual inventory, ALSO later goes
        unresolved) — permanently losing the first one's recovery.
        Reproduced exactly: O1 (0.001 @ $78,000, -$7) queued unresolved,
        then O2 (0.0005 @ $78,000, -$3.50) ALSO queued — the single-slot
        design retained only O2, permanently losing O1's $7 loss."""
        for entry in self._unresolved_stop_recoveries:
            if entry.get("order_id") == order_id:
                entry.update({
                    "cost_basis": cost_basis, "baseline_filled": baseline_filled,
                    "baseline_cost": baseline_cost, "baseline_fee": baseline_fee,
                })
                return
        self._unresolved_stop_recoveries.append({
            "order_id": order_id, "cost_basis": cost_basis,
            "baseline_filled": baseline_filled, "baseline_cost": baseline_cost,
            "baseline_fee": baseline_fee,
        })

    def _reconcile_historical_stop_order(
        self, order: dict, *,
        baseline_filled: float, baseline_cost: float, baseline_fee: float,
        cost_basis: "float | None",
    ) -> str:
        """PASS-8 review finding (P1, finding 2 — the shared classification
        every startup/retry entry point must agree on): recovers any
        missed fill/fee delta (cash-free, via
        _recover_missed_native_stop_execution) for a HISTORICAL order —
        one that is NOT necessarily the currently tracked resting stop —
        against the GIVEN baseline/cost_basis (never the shared
        self._native_stop_last_recorded_*/self._portfolio._cost_basis,
        which may by now belong to an entirely different order). Returns:

          "terminal"   — confirmed closed/canceled/rejected/expired. Fully
                         resolved; safe for the caller to stop tracking or
                         retrying it.
          "still_open" — confirmed genuinely still resting on the
                         exchange. NOT safe to abandon — the caller must
                         either attempt cancellation or keep tracking/
                         retrying it.
          "unknown"    — status couldn't be confidently classified. Same
                         "do not abandon" treatment as still_open.

        Never mutates cash/position itself — every caller's own context
        already established whether that's correct (via the startup
        exchange sync) or needs separate handling."""
        self._recover_missed_native_stop_execution(
            order, baseline_filled=baseline_filled, baseline_cost=baseline_cost,
            baseline_fee=baseline_fee, cost_basis_override=cost_basis,
            advance_tracked_baseline=False,
        )
        status = str(order.get("status") or "").lower()
        if status in _CANCELLED_TERMINAL_STATUSES:
            return "terminal"
        if status == "open":
            return "still_open"
        return "unknown"

    def _recover_flat_native_stop_at_startup(self) -> None:
        """PASS-7 review finding (P0): the position is ALREADY flat (0) by
        the time this runs — _sync_position() zeroed cost_basis for
        exactly this reason (a native stop fully executed offline). The
        OLD code called the LIVE _cancel_native_stop() here, which applies
        the fill's cash effect a SECOND time (cash already reflects it via
        _sync_cash()) and computes P&L against the now-zero cost_basis,
        fabricating profit out of nothing. Reproduced exactly: cash
        $1,311.36 instead of $1,155.68, reported profit +$156 instead of
        the real -$14.

        Fixed: query the tracked order directly and recover any missed
        delta cash-free, using self._startup_recovery_cost_basis (captured
        immediately after _load_state(), before any sync could zero it) —
        never the LIVE _cancel_native_stop() path, and never the current
        (by now zeroed) self._portfolio._cost_basis.

        PASS-8 review finding (P1, finding 2): a FLAT local position does
        NOT establish that the order's own, independent life on the
        exchange has terminated — the old code unconditionally cleared
        tracking after recovery regardless of the order's actual status.
        Reproduced exactly: BTC balance externally zeroed (a transfer/
        close unrelated to the stop) while O1 was still genuinely resting
        — the old code cleared _native_stop_order_id anyway, and
        fetch_open_orders() still showed O1 live and now completely
        unmanaged. Fixed: only clear tracking on a CONFIRMED terminal
        status; otherwise keep it tracked (a later BUY's own
        sync_protective_stop() cancels it first, same as any other
        resting stop) rather than silently abandoning a live order."""
        order_id = self._native_stop_order_id
        try:
            final_order = fetch_with_retry(
                lambda: self._exchange.fetch_order(order_id, self.symbol),
                label=f"native stop final-state check (flat) [{self.symbol}]",
            )
        except Exception as exc:
            logger.warning(
                "Could not verify final state of native stop %s (position "
                "already flat) on %s: %s — preserving its historical "
                "reference for a later retry instead of discarding it.",
                order_id, self.symbol, exc,
            )
            self._queue_unresolved_stop_recovery(
                order_id, self._startup_recovery_cost_basis,
                self._native_stop_last_recorded_filled,
                self._native_stop_last_recorded_cost,
                self._native_stop_last_recorded_fee,
            )
            self._clear_native_stop_tracking_fields()
            self._save_state()
            return

        classification = self._reconcile_historical_stop_order(
            final_order,
            baseline_filled=self._native_stop_last_recorded_filled,
            baseline_cost=self._native_stop_last_recorded_cost,
            baseline_fee=self._native_stop_last_recorded_fee,
            cost_basis=self._startup_recovery_cost_basis,
        )
        if classification != "terminal":
            logger.error(
                "NATIVE STOP FLAT BUT STILL LIVE [%s]: order %s reads "
                "back status=%r with the local position at zero — "
                "retaining tracking rather than abandoning a live order.",
                self.symbol, order_id, final_order.get("status"),
            )
            self._alerter.error(
                f"NATIVE STOP FLAT BUT STILL LIVE [{self.symbol}]: order "
                f"{order_id} is still resting on the exchange even though "
                f"the local position is zero — check Kraken; the bot "
                f"will keep tracking it and resolve it via the normal "
                f"cancel-before-place path on the next entry for this "
                f"symbol."
            )
            self._native_stop_last_recorded_filled = float(final_order.get("filled") or 0.0)
            _c = float(final_order.get("cost") or 0.0)
            if _c <= 0:
                _avg = float(final_order.get("average") or final_order.get("price") or 0.0)
                _c = _avg * self._native_stop_last_recorded_filled
            self._native_stop_last_recorded_cost = _c
            self._native_stop_last_recorded_fee  = float(
                (final_order.get("fee") or {}).get("cost") or 0.0
            )
            self._save_state()
            return

        self._clear_native_stop_tracking_fields()
        self._save_state()

    def _retry_unresolved_stop_recoveries(self) -> None:
        """PASS-7 review finding (P1, finding 4) / PASS-8 review finding
        (P1, finding 3): retries every previously-failed final-state
        lookup for historical (no longer currently-tracked) native stops
        — called at startup and every tick (via reconcile_pending_orders)
        until each resolves. Each entry uses ITS OWN frozen baseline/cost-
        basis, never the SHARED self._native_stop_last_recorded_*/
        self._portfolio._cost_basis fields (which may by now belong to an
        entirely different replacement stop) — see
        _reconcile_historical_stop_order's own docstring.

        PASS-8 review finding (P1, finding 2 — applies here too): a
        successful fetch is not proof of terminal settlement. An entry is
        only removed once _reconcile_historical_stop_order confirms
        "terminal" — otherwise its frozen baseline is updated to the
        order's current cumulative state (so a later retry's delta isn't
        double-counted) and it stays queued."""
        if not self._unresolved_stop_recoveries:
            return
        still_pending: list = []
        for entry in list(self._unresolved_stop_recoveries):
            order_id = entry.get("order_id")
            try:
                final_order = fetch_with_retry(
                    lambda: self._exchange.fetch_order(order_id, self.symbol),
                    label=f"unresolved native-stop history retry [{self.symbol}]",
                )
            except Exception as exc:
                logger.warning(
                    "Unresolved native-stop history for %s on %s still "
                    "unavailable: %s — will retry again later.",
                    order_id, self.symbol, exc,
                )
                still_pending.append(entry)
                continue
            classification = self._reconcile_historical_stop_order(
                final_order,
                baseline_filled=entry.get("baseline_filled", 0.0),
                baseline_cost=entry.get("baseline_cost", 0.0),
                baseline_fee=entry.get("baseline_fee", 0.0),
                cost_basis=entry.get("cost_basis"),
            )
            if classification == "terminal":
                logger.warning(
                    "UNRESOLVED NATIVE-STOP HISTORY RECOVERED [%s]: order "
                    "%s finally confirmed and journaled.", self.symbol, order_id,
                )
            else:
                entry["baseline_filled"] = float(final_order.get("filled") or 0.0)
                _c = float(final_order.get("cost") or 0.0)
                if _c <= 0:
                    _avg = float(final_order.get("average") or final_order.get("price") or 0.0)
                    _c = _avg * entry["baseline_filled"]
                entry["baseline_cost"] = _c
                entry["baseline_fee"]  = float((final_order.get("fee") or {}).get("cost") or 0.0)
                still_pending.append(entry)
        self._unresolved_stop_recoveries = still_pending
        self._save_state()

    def _verify_resting_stop_on_startup(self) -> None:
        """
        Called once from __init__ (live mode only) after _sync_position.
        Confirms a stop order recorded in the state file is still open on
        the exchange; clears the tracked id if it isn't (filled while the
        bot was down — the whole point of this feature working correctly —
        or cancelled manually). Never places a NEW price/trailing-pct level
        here: this method only knows the OLD price, and a fresh BUY may have
        changed the picture; main.py owns deciding what price a replacement
        should use and calls sync_protective_stop() itself if this leaves a
        real gap (position open, no resting stop).

        Also scans the same already-fetched open-orders list for how many
        stop-type orders (see _is_native_stop_order) exist on this symbol
        overall — not just whether OUR tracked id survived. This bot's own
        logic only ever cancels-then-places, never places without cancelling
        first, so more than one should be structurally impossible via normal
        operation; if it's ever found anyway (manual intervention outside the
        bot, a race, a bug), that's not something to silently resolve by
        picking one — alert loudly.

        Two adjacent gaps, both CLOSED 2026-08-20 in a follow-up pass (see
        .memory/execution_layer.md for the full writeup):

        Gap A — resting order's quantity can go stale (Kraken-side fills
        while the bot was down, either a partial fill on the resting stop
        itself or an external event changing the position) even when the
        order is still confirmed open. Checked in
        _reconcile_resting_stop_quantity() against the already-reconciled
        self._portfolio.position (set by _sync_position just before this
        method runs). Always alerts on any mismatch; only auto-resizes
        (cancel + replace at the SAME price/trailing-pct, read back from the
        resting order's own raw fields — never recomputed from avg_entry/
        ATR) when under-sized — the genuinely unprotected direction. An
        over-sized resting stop is left alone (benign — Kraken can't oversell
        a position — and resizing a trailing stop would forfeit its
        server-tracked trail progress for no protective benefit).

        Gap B — an untracked-but-real resting order. If
        native_stop_order_id is missing/lost (state file corruption, or a
        crash between create_order() succeeding and _save_state()
        persisting it), the "no tracked id" branch below used to do nothing
        to look for a real order already resting — main.py's startup
        reconciliation would then place a second stop alongside the
        untracked first one. Now: if exactly one untracked stop-type order
        is found, adopted verbatim in _adopt_untracked_stop() (id/price/
        trailing-flag trusted from the exchange, same "exchange is
        authoritative" philosophy as _sync_position/_sync_cash). Two or more
        untracked candidates is the same ambiguous case as above — alert,
        adopt nothing (confirmed the pre-existing ambiguity alert did NOT
        cover this case: it only ever ran after the "no tracked id" early
        return, so a lost-id restart never reached it).

        PASS-6 review finding (P1): also resolves any pending 'protect'
        role submission FIRST — unconditionally, regardless of current
        position — since a pending placement's own OUTCOME (did it fill?)
        needs journaling as a historical fact even if the position it was
        protecting has since changed by some other means.

        PASS-7 review finding (P1, finding 4): also retries any
        previously-unresolved historical stop recovery FIRST, independent
        of the currently tracked slot or position state.
        """
        self._resolve_pending_protect_submission_at_startup()
        self._retry_unresolved_stop_recoveries()

        if self._portfolio.position <= 0:
            # No position to protect — any leftover id is stale by
            # definition, but PASS-7 review finding (P0): it may have
            # FULLY EXECUTED OFFLINE before going stale, not just been
            # cancelled — _sync_position() zeroing position/cost_basis
            # reflects the cash effect of exactly that execution, but its
            # P&L/journal are still missing. The OLD code called the LIVE
            # _cancel_native_stop() here, which re-applies the fill's cash
            # effect a SECOND time (already reflected via sync) and
            # computes P&L against the NOW-ZERO cost_basis, fabricating
            # profit out of nothing. Reproduced exactly: cash $1,311.36
            # instead of $1,155.68, reported profit +$156 instead of the
            # real -$14. Fixed: recover cash-free, using the basis
            # preserved BEFORE the sync could zero it.
            if self._native_stop_order_id:
                self._recover_flat_native_stop_at_startup()
            return

        try:
            open_orders = fetch_with_retry(
                lambda: self._exchange.fetch_open_orders(self.symbol),
                label=f"open orders check [{self.symbol}]",
            )
        except Exception as exc:
            logger.warning(
                "Could not verify resting stop on startup: %s — leaving "
                "tracked state as-is, will re-check on the next "
                "placement/cancel.", exc,
            )
            return

        _stop_orders = [o for o in open_orders if _is_native_stop_order(o)]

        if not self._native_stop_order_id:
            # Gap B: no tracked id — look for a real untracked order before
            # assuming the position is naked.
            if len(_stop_orders) == 1:
                self._adopt_untracked_stop(_stop_orders[0])
            elif len(_stop_orders) > 1:
                self._alert_ambiguous_stops(_stop_orders)
                logger.warning(
                    "NATIVE STOP GAP [%s]: position=%.6f open, no tracked "
                    "stop, and multiple untracked stop orders found — not "
                    "auto-adopting any of them, manual review needed.",
                    self.symbol, self._portfolio.position,
                )
            else:
                logger.warning(
                    "NATIVE STOP GAP [%s]: position=%.6f open but no "
                    "resting stop recorded and none found on the exchange "
                    "— main.py startup reconciliation should place one.",
                    self.symbol, self._portfolio.position,
                )
            return

        still_open = any(
            str(o.get("id", "")) == self._native_stop_order_id for o in open_orders
        )

        if len(_stop_orders) > 1:
            self._alert_ambiguous_stops(_stop_orders)

        if still_open:
            if self._native_stop_is_trailing:
                logger.warning(
                    "Native TRAILING stop confirmed on restart: %s",
                    self._native_stop_order_id,
                )
            else:
                logger.warning(
                    "Native stop confirmed on restart: %s @ %.2f",
                    self._native_stop_order_id, self._native_stop_price or 0.0,
                )
            matched = next(
                o for o in open_orders
                if str(o.get("id", "")) == self._native_stop_order_id
            )
            # PASS-5/PASS-6 review findings (P0/P1), surfaced by the
            # stateful crash-injection harness: this branch confirmed the
            # tracked stop is still open but neither re-seeded
            # _native_stop_last_recorded_* from ITS CURRENT cumulative
            # filled/cost/fee (PASS-5 — double-counted a fee/quantity on
            # the next cancel cycle) NOR recovered the execution/P&L for
            # whatever delta occurred before this restart (PASS-6 — a real
            # fill and its realized loss silently disappeared, seeding the
            # baseline as if that execution never happened). Cash/position
            # for this delta are ALREADY reflected via _sync_cash()/
            # _sync_position() (this method runs after both) — only the
            # journal entry and realized P&L were missing.
            self._recover_missed_native_stop_execution(matched)
            self._save_state()
            self._reconcile_resting_stop_quantity(matched)
        else:
            # PASS-6 review finding (P1): the tracked order is gone from
            # open orders — it either filled to completion or was
            # cancelled while the bot was down. The old code assumed the
            # worst (unprotected) and moved on without ever checking
            # WHICH — a stop that fully executed offline had its final
            # fill/P&L silently discarded, identical in spirit to the
            # still-open case above. Query it directly; a genuine fill
            # recovers through the same cash-free journal path.
            _final_order = None
            _lookup_failed = False
            _order_id = self._native_stop_order_id
            try:
                _final_order = fetch_with_retry(
                    lambda: self._exchange.fetch_order(_order_id, self.symbol),
                    label=f"native stop final-state check [{self.symbol}]",
                )
            except Exception as exc:
                _lookup_failed = True
                logger.warning(
                    "Could not verify final state of gone native stop %s "
                    "on %s: %s — preserving its historical reference for "
                    "a later retry instead of discarding it.",
                    _order_id, self.symbol, exc,
                )
            if _lookup_failed:
                # Position is still > 0 here (the early return above
                # already handled the flat case), so
                # self._portfolio._cost_basis has NOT been zeroed by
                # _sync_position() — safe to capture directly, independent
                # of the currently-tracked slot (cleared below regardless).
                self._queue_unresolved_stop_recovery(
                    _order_id, self._portfolio._cost_basis,
                    self._native_stop_last_recorded_filled,
                    self._native_stop_last_recorded_cost,
                    self._native_stop_last_recorded_fee,
                )
                self._clear_native_stop_tracking_fields()
                self._save_state()
                return
            if _final_order is not None:
                classification = self._reconcile_historical_stop_order(
                    _final_order,
                    baseline_filled=self._native_stop_last_recorded_filled,
                    baseline_cost=self._native_stop_last_recorded_cost,
                    baseline_fee=self._native_stop_last_recorded_fee,
                    cost_basis=self._portfolio._cost_basis,
                )
                if classification != "terminal":
                    # PASS-8 review finding (P1, finding 2): absent from
                    # the fetch_open_orders() LIST is not proof the order
                    # has terminated — a direct fetch can still show it
                    # genuinely resting (eventual consistency, or a race).
                    # Never abandon a live order's identity — keep it
                    # tracked exactly as the still-open branch above does.
                    logger.error(
                        "NATIVE STOP GAP INCONCLUSIVE [%s]: order %s "
                        "absent from the open-orders list but a direct "
                        "fetch shows status=%r — retaining tracking "
                        "rather than abandoning a live order.",
                        self.symbol, _order_id, _final_order.get("status"),
                    )
                    self._native_stop_last_recorded_filled = float(_final_order.get("filled") or 0.0)
                    _c = float(_final_order.get("cost") or 0.0)
                    if _c <= 0:
                        _avg = float(_final_order.get("average") or _final_order.get("price") or 0.0)
                        _c = _avg * self._native_stop_last_recorded_filled
                    self._native_stop_last_recorded_cost = _c
                    self._native_stop_last_recorded_fee  = float(
                        (_final_order.get("fee") or {}).get("cost") or 0.0
                    )
                    self._save_state()
                    return
            logger.warning(
                "NATIVE STOP GAP [%s]: tracked order %s is no longer open "
                "(filled or cancelled while the bot was down) — position=%.6f "
                "may be unprotected; main.py startup reconciliation should "
                "place a fresh one.",
                self.symbol, _order_id, self._portfolio.position,
            )
            self._clear_native_stop_tracking_fields()
            self._save_state()

    def _alert_ambiguous_stops(self, stop_orders: list[dict]) -> None:
        """Shared by both the tracked-id-still-open path and the Gap B
        no-tracked-id path — 2+ real stop-type orders resting at once is
        the same ambiguous, needs-manual-review situation regardless of
        which path found it."""
        _ids = [str(o.get("id", "")) for o in stop_orders]
        logger.error(
            "NATIVE STOP AMBIGUOUS [%s]: %d stop-type orders found "
            "resting simultaneously (%s) — this bot's own logic never "
            "places more than one (always cancel-then-place); needs "
            "manual review.",
            self.symbol, len(stop_orders), ", ".join(_ids),
        )
        self._alerter.error(
            f"NATIVE STOP AMBIGUOUS [{self.symbol}]: {len(stop_orders)} "
            f"stop-type orders resting simultaneously ({', '.join(_ids)}) "
            f"— manual review needed."
        )

    def _adopt_untracked_stop(self, order: dict) -> None:
        """Gap B fix: a real stop-type order is resting on Kraken with no
        matching tracked id in our state. Trust the exchange over our own
        (lost) bookkeeping and adopt it verbatim, instead of letting
        main.py's startup reconciliation place a duplicate alongside it."""
        order_id      = str(order.get("id", ""))
        is_trailing   = _raw_ordertype(order) == "trailing-stop"
        stop_price, _ = _extract_stop_trigger(order)
        self._native_stop_order_id    = order_id
        self._native_stop_price       = None if is_trailing else stop_price
        self._native_stop_is_trailing = is_trailing
        # Seed from whatever this order already shows filled/cost/fee at
        # adoption time — any fill that happened before we started
        # tracking it this session is not ours to invent accounting for;
        # only NEW fill observed from this point forward should ever be
        # recorded (2026-09-18 PASS-3 review finding: cost/fee must be
        # seeded together with quantity, or the first NEW delta recorded
        # after adoption would wrongly treat this order's entire
        # pre-adoption cumulative cost/fee as if it were that one delta's).
        _seed_filled = float(order.get("filled") or 0.0)
        _seed_cost   = float(order.get("cost") or 0.0)
        if _seed_cost <= 0:
            _seed_avg = float(order.get("average") or order.get("price") or 0.0)
            _seed_cost = _seed_avg * _seed_filled
        self._native_stop_last_recorded_filled = _seed_filled
        self._native_stop_last_recorded_cost   = _seed_cost
        self._native_stop_last_recorded_fee    = float((order.get("fee") or {}).get("cost") or 0.0)
        logger.warning(
            "NATIVE STOP ADOPTED [%s]: found untracked resting %s order %s "
            "— adopted instead of placing a duplicate.",
            self.symbol, "trailing" if is_trailing else "static", order_id,
        )
        self._alerter.message(
            f"ℹ️ NATIVE STOP ADOPTED [{self.symbol}]: found an "
            f"untracked resting {'trailing' if is_trailing else 'static'} "
            f"stop order {order_id} on restart — adopted it rather than "
            f"placing a second one. The state file's tracked id was likely "
            f"lost to a crash before it could be saved."
        )
        self._save_state()

    def _reconcile_resting_stop_quantity(self, order: dict) -> None:
        """Gap A fix: compare a confirmed-still-open resting stop's actual
        volume against the current (already-reconciled) position. Always
        alerts on a mismatch beyond a satoshi-scale tolerance; only
        auto-resizes when the resting order is UNDER-sized (the genuinely
        unprotected direction). An over-sized resting order is left alone —
        benign (Kraken can't oversell a position) and, for a trailing stop,
        resizing would forfeit server-tracked trail progress for no
        protective benefit. See _verify_resting_stop_on_startup docstring.
        """
        resting_qty = _resting_order_quantity(order)
        if resting_qty is None:
            return  # unknown — nothing to compare, leave as-is

        position = self._portfolio.position
        if abs(resting_qty - position) <= self._STOP_QTY_MISMATCH_THRESHOLD:
            return  # matches, nothing to do

        under_protected = resting_qty < position
        logger.warning(
            "NATIVE STOP QTY MISMATCH [%s]: resting order %s covers %.6f "
            "but position is %.6f (%s).",
            self.symbol, self._native_stop_order_id, resting_qty, position,
            "under-protected — resizing" if under_protected else "over-sized — benign, left as-is",
        )
        self._alerter.error(
            f"NATIVE STOP QTY MISMATCH [{self.symbol}]: resting stop "
            f"{self._native_stop_order_id} covers {resting_qty:.6f} but "
            f"position is {position:.6f}."
            + (
                " Position is UNDER-protected — resizing now."
                if under_protected
                else " Resting stop covers more than the position (benign"
                     " — left as-is)."
            )
        )
        if not under_protected:
            return  # benign over-sized case — leave the resting order untouched

        stop_price, trailing_pct = _extract_stop_trigger(order)
        was_trailing = _raw_ordertype(order) == "trailing-stop"
        # _fill_order (if any) is not surfaced further here: this method
        # runs only from _verify_resting_stop_on_startup(), i.e. INSIDE
        # LiveExecutor.__init__() before bot/main.py has constructed this
        # symbol's PositionManager/state machine/capital-pool slot — there
        # is no live bookkeeping to route it to yet. The fill's accounting
        # is still correct and complete via _record_stop_triggered_fill
        # (cash/position) and _pending_journal_entry (trade_log, replayed
        # at startup) — main.py's own restart-recovery seeding then reads
        # the executor's already-correct post-fill position when it
        # constructs pm/sm/capital_pool for this run, so nothing here is
        # silently lost, just resolved through a different (startup-time)
        # path than the runtime one _process_discovered_sell_fill covers.
        outcome, _fill_order = self._cancel_native_stop()
        if outcome != "cancelled":
            # "filled" — the stop already executed and closed/reduced the
            # position (_record_stop_triggered_fill already handled the
            # accounting); nothing left to resize. "unknown" — cancellation
            # is unconfirmed; placing a replacement here risks two live
            # stops resting on the exchange at once. Either way, do not
            # place a replacement — leave it for the next reconciliation.
            if outcome == "unknown":
                logger.error(
                    "NATIVE STOP RESIZE ABORTED [%s]: cancellation "
                    "unconfirmed — not placing a replacement to avoid two "
                    "live stops.", self.symbol,
                )
            return
        if was_trailing and trailing_pct is not None:
            self._place_native_trailing_stop(position, trailing_pct)
        elif not was_trailing and stop_price is not None:
            self._place_native_stop(position, stop_price)
        else:
            logger.error(
                "NATIVE STOP QTY MISMATCH [%s]: could not read back the "
                "resting order's own price/trailing-pct to resize it — "
                "cancelled the under-sized stop and left nothing in its "
                "place.",
                self.symbol,
            )
            self._alerter.error(
                f"NATIVE STOP RESIZE FAILED [{self.symbol}]: cancelled an "
                f"under-sized resting stop but couldn't read back its "
                f"price/trailing-pct to replace it — position is now "
                f"UNPROTECTED by this backstop until the next reconciliation."
            )

    def _record_stop_triggered_fill(self, fill_info: dict) -> Order:
        """A native stop executed on the exchange outside the bot's own
        order-placement path (caught while trying to cancel it — a race
        between our cancel and the stop actually triggering). Records it
        through the SAME accounting a normal SELL fill gets (position, cash,
        realized P&L, fees, an appended Order) so the trade log / CSV /
        Telegram alert / P&L stay correct — this must run exactly once per
        detected fill, which is why it lives here, the single place that
        discovers one, rather than being reconstructed at each call site.

        2026-09-18 review finding: the old _cancel_native_stop() caught
        every exception and unconditionally cleared the tracked stop id —
        a fill DURING cancellation (the stop won the race) was silently
        discarded: no fill record, no P&L update, no CSV row, no alert.

        2026-09-19 PASS-5 review finding (P0): this used to call
        _save_state() itself, immediately — persisting the fill's economic
        effect and journal entry BEFORE its caller (_cancel_native_stop)
        advanced _native_stop_last_recorded_* to reflect it. A crash in
        between left the OLD (lower) progress baseline on disk alongside
        an ALREADY-APPLIED fill: on restart, the identical cumulative fill
        was reprocessed as a brand-new delta and applied a SECOND time
        (fresh UUID, no way for the ledger to recognize it as the same
        execution) — reproduced exactly: cash $1,180 instead of $1,090,
        journal 2 entries instead of 1. Fixed: PURE (in-memory only, no
        I/O). The caller advances progress and persists everything —
        economics, progress and journal entry — together, in ONE save."""
        filled_qty = float(fill_info["filled"])
        if self._portfolio.position > 0:
            filled_qty = min(filled_qty, self._portfolio.position)
        fill_price   = float(fill_info.get("price") or 0.0)
        fee_data     = fill_info.get("fee") or {}
        fee_cost     = float(fee_data.get("cost") or 0.0)
        fee_currency = fee_data.get("currency") or self.symbol.split("/")[1]
        quote        = self.symbol.split("/")[1]
        total_value  = fill_price * filled_qty

        pnl = (fill_price - self._portfolio._cost_basis) * filled_qty
        self._portfolio.realized_pnl += pnl
        self._portfolio.cash         += total_value
        self._portfolio.position      = max(0.0, self._portfolio.position - filled_qty)
        if self._portfolio.position == 0:
            self._portfolio._cost_basis = 0.0
            self._bot_opened_position   = False

        if fee_cost > 0:
            if fee_currency != quote:
                logger.warning(
                    "Native-stop fee currency mismatch: fee=%.6f %s but "
                    "quote=%s — not deducting (manual reconciliation needed)",
                    fee_cost, fee_currency, quote,
                )
            else:
                self._portfolio.cash -= fee_cost
                self._fees_paid      += fee_cost

        order = Order(
            order_id      = f"native-stop:{fill_info.get('order_id', '')}",
            symbol        = self.symbol,
            side          = OrderSide.SELL,
            quantity      = filled_qty,
            price         = fill_price,
            status        = OrderStatus.FILLED,
            created_at    = datetime.now(timezone.utc),
            filled_at     = datetime.now(timezone.utc),
            fee_cost      = fee_cost,
            fee_currency  = fee_currency,
            pnl           = pnl,
        )
        self._fills.append(order)
        logger.warning(
            "NATIVE STOP FILLED DURING CANCEL [%s]: recorded exit %.8f @ %.2f "
            "(order %s)", self.symbol, filled_qty, fill_price, fill_info.get("order_id"),
        )
        self._alerter.error(
            f"NATIVE STOP FILLED [{self.symbol}]: the protective stop executed "
            f"on the exchange during a cancel attempt (won the race) before "
            f"the bot's own exit could be placed — recorded as the actual "
            f"exit: {filled_qty:.8f} @ {fill_price:,.2f}."
        )
        self._record_pending_journal_entry(order)
        return order

    def _apply_native_stop_fee_only_adjustment(
        self, order_id: str, delta_fee: float, fee_currency: str,
    ) -> None:
        """PASS-4 review finding 5: a native stop's filled quantity/cost are
        unchanged since the last check, but its FEE was updated or finalized
        (some exchanges report a provisional fee at fill time and settle the
        real one shortly after). Applies the fee delta directly to
        cash/fees_paid — deliberately does NOT fabricate a zero-quantity
        fill Order (that would misrepresent a fee correction as a trade).
        The caller (_cancel_native_stop) only invokes this once per genuine
        fee delta — its own cumulative-fee tracking is the idempotency/
        provenance record, persisted the same way the quantity/cost
        baseline already is, so a restart between the quantity fill and
        this fee settlement still applies it exactly once.

        2026-09-19 PASS-5 review findings: (1) PURE — does NOT save state
        itself. The caller advances _native_stop_last_recorded_* and
        persists everything (cash effect + progress baseline + journal
        entry) in ONE atomic save, exactly like a real fill. (2) also
        queues a durable "fee_adjustment" journal entry (previously this
        updated executor cash but emitted nothing for TradeLog, so the
        correction never reached net-of-fee reporting even though the
        original quantity fill had already been logged and acknowledged)."""
        quote = self.symbol.split("/")[1]
        if fee_currency and fee_currency != quote:
            logger.warning(
                "Native-stop fee-only adjustment currency mismatch: "
                "fee=%.6f %s but quote=%s — not deducting (manual "
                "reconciliation needed)", delta_fee, fee_currency, quote,
            )
            return
        self._portfolio.cash -= delta_fee
        self._fees_paid      += delta_fee
        logger.warning(
            "NATIVE STOP FEE ADJUSTMENT [%s]: order %s fee finalized "
            "+%.6f (quantity/cost unchanged since the last check) — "
            "applied once.", self.symbol, order_id, delta_fee,
        )
        # 2026-09-19 PASS-6 review finding (P1): use the SAME "native-stop:"
        # prefixed identity _record_stop_triggered_fill gives this order's
        # own fill row (Order.order_id) — live_comparison.py's fee-
        # adjustment attribution matches by this exact string, and a raw
        # (unprefixed) id here would never match its own fill.
        self._record_fee_adjustment_journal_entry(
            f"native-stop:{order_id}", delta_fee, fee_currency,
        )

    def _cancel_native_stop(self) -> "tuple[str, Order | None]":
        """Cancel the resting native stop, but only clear tracked protection
        once its actual outcome is confirmed.

        Returns (outcome, fill_order):
          "cancelled", None  — confirmed terminal cancel, nothing filled.
              Safe for the caller to place a replacement or proceed with
              its own exit.
          "filled", Order    — the stop itself executed to a CONFIRMED
              terminal status (in the race between our cancel and its own
              trigger) before cancellation took effect. Already fully
              recorded via _record_stop_triggered_fill — the returned
              Order IS that exit; the caller must not also place its own
              SELL or a replacement stop. Tracked id is cleared.
          "partial", Order   — the stop filled SOME (new) quantity but is
              CONFIRMED still open/resting for the remainder — a genuine
              partial fill on a live order, not a completed exit. The new
              delta is recorded via _record_stop_triggered_fill (never a
              previously-recorded amount — see
              _native_stop_last_recorded_filled), but the tracked id/price
              are DELIBERATELY RETAINED (still live on the exchange). The
              caller must NOT place a replacement stop (would duplicate
              the still-resting original) — 2026-09-18 follow-up review
              finding: the old version conflated this with "filled" purely
              because `filled > 0`, ignoring `status`, and cleared the
              tracked id while the real order was still resting.
          "unknown", None    — cancellation could not be confirmed (the
              cancel call and/or the follow-up verification failed, or the
              order still reads back open/unresolved). Tracked protection
              is left EXACTLY as it was — not cleared — so nothing downstream
              can mistake "unconfirmed" for "gone" and duplicate an order or
              leave the position believing it's protected when it might not
              be. 2026-09-18 review finding: the old version caught every
              exception here and cleared the id unconditionally regardless
              of what actually happened on the exchange.
        """
        if not self._native_stop_order_id:
            return "cancelled", None
        _order_id = self._native_stop_order_id
        cancel_raised = False
        try:
            self._exchange.cancel_order(_order_id, self.symbol)
            logger.warning("Native stop cancelled: %s", _order_id)
        except Exception as exc:
            cancel_raised = True
            logger.info(
                "Native stop cancel %s: %s — verifying actual state before "
                "clearing tracked protection", _order_id, exc,
            )

        # Verify regardless of whether cancel_order raised — a "successful"
        # cancel can still race a fill, and a raised exception doesn't by
        # itself prove the order is gone (eventual consistency, or the
        # order had already filled and cancel legitimately rejected it).
        try:
            order = fetch_with_retry(
                lambda: self._exchange.fetch_order(_order_id, self.symbol),
                attempts=2, delay_s=1.0,
                label=f"native stop cancel verification [{self.symbol}]",
            )
        except Exception as exc:
            logger.error(
                "NATIVE STOP CANCEL UNCONFIRMED [%s]: could not verify order "
                "%s after cancel (cancel_raised=%s, %s) — leaving tracked "
                "protection in place rather than assuming it's gone.",
                self.symbol, _order_id, cancel_raised, exc,
            )
            self._alerter.error(
                f"NATIVE STOP CANCEL UNCONFIRMED [{self.symbol}]: could not "
                f"verify whether stop {_order_id} actually cancelled — "
                f"treating it as still active until this resolves."
            )
            return "unknown", None

        cumulative_filled = float(order.get("filled") or 0.0)
        status            = str(order.get("status") or "").lower()
        is_terminal        = status in _CANCELLED_TERMINAL_STATUSES
        # 2026-09-18 follow-up review finding (P0): this used to treat ANY
        # positive `filled` as proof the stop was fully resolved, even
        # when `status` still read "open" (a genuine partial fill on a
        # stop that is STILL resting for the remainder) — clearing the
        # tracked id while the real order remained live, so a later
        # protection sync could place a REPLACEMENT stop on top of it.
        # Reproduced: starting position 0.002, cancel timeout, fetch
        # returns status="open" with cumulative filled=0.001 — old code
        # returned "filled"/tracked-id=None despite 0.001 still resting.
        # Fixed: only clear tracked state on a CONFIRMED terminal status.
        # A positive filled with a non-terminal status is a genuine
        # partial fill of a STILL-OPEN order — record only the NEW delta
        # (never re-record what a prior check already booked) and keep
        # tracking the (still resting, now-reduced) stop.
        new_delta = max(0.0, cumulative_filled - self._native_stop_last_recorded_filled)

        # 2026-09-18 PASS-3 review finding (P1): the delta QUANTITY was
        # computed correctly, but the delta's PRICE and FEE were not — the
        # order's CUMULATIVE average price and CUMULATIVE fee were applied
        # to the new delta quantity wholesale. Reproduced exactly: fill 1
        # (0.001 @ avg 90000, cumulative fee $0.36) then fill 2 (cumulative
        # 0.002 @ avg 95000, cumulative fee $0.76) — correct total proceeds
        # are $190 - $0.76 = $189.24 (fill 2's own price is actually
        # $100,000: cumulative cost $190 minus fill 1's $90, over 0.001),
        # but the old code produced $183.88 (used $95,000 for fill 2's
        # price and re-charged the FULL $0.76 fee on top of fill 1's
        # already-deducted $0.36). Fixed: track cumulative COST and FEE
        # (not just quantity) so the delta's own price/fee are derived by
        # subtraction, exactly like the quantity delta already is.
        # `cost` (ccxt's own cumulative quote-currency total) is preferred
        # over back-computing average*filled when the exchange provides it
        # directly.
        cumulative_cost = float(order.get("cost") or 0.0)
        if cumulative_cost <= 0:
            _avg = float(order.get("average") or order.get("price") or 0.0)
            cumulative_cost = _avg * cumulative_filled
        cumulative_fee = float((order.get("fee") or {}).get("cost") or 0.0)

        # 2026-09-18 PASS-4 review finding (P1): quantity, cost and fee
        # deltas must be reconciled INDEPENDENTLY — the old code gated fee
        # booking on `new_delta > 0` (quantity), so a fee that becomes
        # available or is corrected while the filled QUANTITY is unchanged
        # (a common settlement-timing quirk — a provisional fee at fill
        # time, finalized shortly after) was silently discarded. Worse, the
        # very same terminal snapshot that carries this correction also
        # clears the tracked counters right below, permanently losing it.
        # Reproduced exactly: snapshot 1 open, 0.001 filled, fee $0 ->
        # snapshot 2 canceled, SAME 0.001 filled, fee finalized at $0.36 —
        # old code recorded $0 in fees and cleared the stop id.
        delta_cost = max(0.0, cumulative_cost - self._native_stop_last_recorded_cost)
        delta_fee  = max(0.0, cumulative_fee  - self._native_stop_last_recorded_fee)

        fill_order = None
        if new_delta > 0:
            delta_price = delta_cost / new_delta if new_delta > 0 else 0.0
            fill_info = {
                "order_id": _order_id,
                "filled":   new_delta,
                "price":    delta_price,
                "fee":      {
                    "cost": delta_fee,
                    "currency": (order.get("fee") or {}).get("currency", ""),
                },
            }
            fill_order = self._record_stop_triggered_fill(fill_info)
        elif delta_fee > 0:
            # No NEW quantity, but a fee-only adjustment is outstanding —
            # apply it directly rather than fabricating a zero-quantity
            # "fill" Order (which would misrepresent this as a trade).
            self._apply_native_stop_fee_only_adjustment(
                _order_id, delta_fee, (order.get("fee") or {}).get("currency", ""),
            )

        if new_delta > 0 or delta_fee > 0:
            # Only ever advance forward on something NEW actually observed
            # — never overwrite the tracked baseline with an unchanged (or,
            # in a weird/regressed snapshot, lower) cumulative figure.
            self._native_stop_last_recorded_filled = cumulative_filled
            self._native_stop_last_recorded_cost   = cumulative_cost
            self._native_stop_last_recorded_fee    = cumulative_fee

        if is_terminal:
            self._native_stop_order_id    = None
            self._native_stop_price       = None
            self._native_stop_is_trailing = False
            self._native_stop_last_recorded_filled = 0.0
            self._native_stop_last_recorded_cost   = 0.0
            self._native_stop_last_recorded_fee    = 0.0
            self._save_state()
            return "filled" if fill_order is not None else "cancelled", fill_order

        if fill_order is not None:
            # Partial fill, order still genuinely open/resting — the
            # tracked id/price are DELIBERATELY retained (still live on
            # the exchange), only the delta is booked.
            logger.warning(
                "NATIVE STOP PARTIAL FILL [%s]: order %s filled %.8f more "
                "(cumulative %.8f) but remains open — tracked protection "
                "retained, not cleared.",
                self.symbol, _order_id, new_delta, cumulative_filled,
            )
            self._save_state()
            return "partial", fill_order

        if delta_fee > 0:
            # 2026-09-19 PASS-5 review finding (P0): a fee-only correction
            # applied above (via _apply_native_stop_fee_only_adjustment)
            # while the order remains open/resting is real durable state
            # (cash effect + advanced progress + a queued journal entry)
            # that must be saved — falling through to the generic
            # "not confirmed" branch below would return without ever
            # persisting it.
            logger.warning(
                "NATIVE STOP FEE-ONLY UPDATE [%s]: order %s fee adjustment "
                "applied while still open (no new fill quantity) — tracked "
                "protection retained.", self.symbol, _order_id,
            )
            self._save_state()
            return "unknown", None

        logger.error(
            "NATIVE STOP CANCEL NOT CONFIRMED [%s]: order %s reads back "
            "status=%r filled=%.8f after a cancel attempt — leaving tracked "
            "protection in place.",
            self.symbol, _order_id, order.get("status"), cumulative_filled,
        )
        self._alerter.error(
            f"NATIVE STOP CANCEL NOT CONFIRMED [{self.symbol}]: stop "
            f"{_order_id} still reads back status={order.get('status')!r} "
            f"after a cancel attempt — treating it as still active."
        )
        return "unknown", None

    def _rearm_native_stop_after_failed_sell(
        self, restore: "tuple[float | None, bool]",
    ) -> None:
        """A SELL that we pre-cancelled the native stop for got rejected — the
        position is now naked. Put the stop back at its previous level so it
        isn't unprotected until the caller retries. Static stops re-place
        cleanly; a trailing stop can't (its % isn't recoverable here) so it
        alerts loudly instead.

        PASS-6/PASS-7 review — carried-over correctness gap, now closed:
        _place_native_stop() can itself discover the replacement was
        ALREADY filled (an adopted historical order via
        _create_order_persisted's reconciliation) and returns that fill.
        This call site used to discard it entirely — the executor's own
        cash/position updated correctly, but PositionManager/state
        machine/capital pool/risk/trade log never learned about it.
        execute()'s own return contract (a single Order for the CURRENT
        call's own outcome) can't ALSO carry an unrelated fill discovered
        deep inside its own reject-handling — instead of changing that
        contract, this queues the discovery onto an IN-MEMORY side channel
        (drain_discovered_fills()) that bot/main.py's per-tick loop drains
        alongside reconcile_pending_orders(), routing it through the SAME
        PositionManager/state-machine/capital-pool/risk/trade-log consumer
        any other discovered fill gets.

        PASS-8 review qualification: this queue is deliberately NOT
        persisted and is NOT itself an acknowledged, exactly-once durable
        queue — describing it that way would overclaim what the drain
        test actually proves (only that draining an in-memory list returns
        it once and clears it). Its restart safety comes from a SEPARATE
        mechanism: this fill's cash/position/realized-P&L effect is
        ALREADY fully applied and durably saved (via _record_stop_
        triggered_fill's own state write and pending-journal entry) by
        the time it's queued here, so a crash before the next tick drains
        it does not lose that effect — only the IN-PROCESS bookkeeping
        consumers (PositionManager/state-machine/capital-pool) would miss
        the notification, and bot/main.py's existing restart-recovery path
        re-seeds those fresh from the executor's already-correct position
        rather than needing to replay this specific event. Broader
        restart/consumer integration coverage beyond the drain-returns-
        and-clears test remains a useful thing to add, not something
        already proven here."""
        _price, _was_trailing = restore
        if self._portfolio.position <= 0:
            return
        if _was_trailing or _price is None:
            self._alerter.error(
                f"NAKED POSITION [{self.symbol}]: a SELL was rejected after its "
                f"native stop was cancelled, and the stop could not be re-armed "
                f"(was trailing). Position is UNPROTECTED — check Kraken now."
            )
            return
        logger.warning(
            "Re-arming native stop [%s] @ %.2f after a rejected SELL", self.symbol, _price,
        )
        _discovered = self._place_native_stop(self._portfolio.position, _price)
        if _discovered is not None:
            self._pending_discovered_fills.append(_discovered)

    def _handle_protective_placement_result(
        self, raw: dict, quantity: float, is_trailing: bool,
        stop_price: "float | None", trailing_pct: "float | None",
    ) -> "Order | None":
        """PASS-4 review finding 1b: dispatch a protective-stop create/
        adopt response by its ACTUAL status and cumulative fill, instead of
        assuming any response from _create_order_persisted means "a stop is
        now freshly resting". That wrapper's own reconciliation can
        legitimately hand back an ADOPTED historical order — most
        plausibly one whose earlier placement attempt was
        _SubmissionOutcomeUnknown and has since resolved on the exchange —
        which may already be closed (fully filled), partially filled while
        still open, or a confirmed-empty cancel/reject. Reproduced exactly:
        a closed order with filled == the full requested quantity used to
        still get recorded as resting protection (`has_resting_stop=True`)
        with the position untouched and the fill journal empty — the
        exchange said the stop already executed; local state said there
        was still a held, protected position.

        Returns the fill Order if this response shows the stop already
        executed some quantity — the caller (sync_protective_stop, via
        _place_native_stop/_place_native_trailing_stop) must surface this
        exactly like a fill discovered during cancellation, so it reaches
        the same PositionManager/state-machine/capital-pool/trade-log
        consumer a normal exit does."""
        order_id    = str(raw.get("id", ""))
        status      = str(raw.get("status") or "").lower()
        filled      = float(raw.get("filled") or 0.0)
        is_terminal = status in _CANCELLED_TERMINAL_STATUSES

        if filled <= 0:
            if is_terminal:
                # canceled/rejected/expired, nothing filled — a confirmed
                # failed placement, not resting protection.
                logger.error(
                    "NATIVE STOP PLACEMENT RESULT [%s]: order %s came back "
                    "%s with no fill — treating as a failed placement, not "
                    "resting protection.", self.symbol, order_id, status,
                )
                self._alerter.error(
                    f"NATIVE STOP FAILED [{self.symbol}]: placement order "
                    f"{order_id} came back {status} with no fill — position "
                    f"is UNPROTECTED if the bot goes down. Software SL/TP "
                    f"still works while the bot is running."
                )
                self._native_stop_order_id    = None
                self._native_stop_price       = None
                self._native_stop_is_trailing = False
                self._native_stop_last_recorded_filled = 0.0
                self._native_stop_last_recorded_cost   = 0.0
                self._native_stop_last_recorded_fee    = 0.0
                return None
            # open, unfilled — the normal/expected case: genuinely resting.
            self._native_stop_order_id    = order_id
            self._native_stop_price       = None if is_trailing else stop_price
            self._native_stop_is_trailing = is_trailing
            self._native_stop_last_recorded_filled = 0.0
            self._native_stop_last_recorded_cost   = 0.0
            self._native_stop_last_recorded_fee    = 0.0
            if is_trailing:
                logger.warning(
                    "Native TRAILING stop placed [%s]: %.6f trailing %.2f%% (order %s)",
                    self.symbol, quantity, (trailing_pct or 0.0) * 100, order_id,
                )
            else:
                logger.warning(
                    "Native stop placed [%s]: %.6f @ %.2f (order %s)",
                    self.symbol, quantity, stop_price or 0.0, order_id,
                )
            return None

        # filled > 0 — the stop already executed some or all of its
        # quantity (most plausibly an adopted historical order that has
        # since filled). Book it through the SAME consumer a fill
        # discovered mid-cancel uses.
        cumulative_cost = float(raw.get("cost") or 0.0)
        if cumulative_cost <= 0:
            _avg = float(raw.get("average") or raw.get("price") or 0.0)
            cumulative_cost = _avg * filled
        fee_data = raw.get("fee") or {}
        fill_info = {
            "order_id": order_id, "filled": filled,
            "price": cumulative_cost / filled if filled > 0 else 0.0,
            "fee": {
                "cost": float(fee_data.get("cost") or 0.0),
                "currency": fee_data.get("currency", ""),
            },
        }
        fill_order = self._record_stop_triggered_fill(fill_info)

        if is_terminal:
            logger.warning(
                "NATIVE STOP PLACEMENT RESULT [%s]: order %s came back %s "
                "already filled %.8f — recorded as the exit; no resting "
                "protection remains.", self.symbol, order_id, status, filled,
            )
            self._native_stop_order_id    = None
            self._native_stop_price       = None
            self._native_stop_is_trailing = False
            self._native_stop_last_recorded_filled = 0.0
            self._native_stop_last_recorded_cost   = 0.0
            self._native_stop_last_recorded_fee    = 0.0
        else:
            logger.warning(
                "NATIVE STOP PLACEMENT RESULT [%s]: order %s already "
                "partially filled %.8f at placement — booked; remainder "
                "still resting and tracked.", self.symbol, order_id, filled,
            )
            self._native_stop_order_id    = order_id
            self._native_stop_price       = None if is_trailing else stop_price
            self._native_stop_is_trailing = is_trailing
            self._native_stop_last_recorded_filled = filled
            self._native_stop_last_recorded_cost   = cumulative_cost
            self._native_stop_last_recorded_fee    = float(fee_data.get("cost") or 0.0)
        return fill_order

    def _place_native_stop(self, quantity: float, stop_price: float) -> "Order | None":
        # 2026-09-18 PASS-3 review finding (P0): this used to call
        # create_order() directly, outside _create_order_persisted — a
        # timeout gave no way to tell "definitely didn't place" from
        # "maybe did, response lost", and the except-clause cleared
        # tracking unconditionally either way. Reproduced: two
        # sync_protective_stop() calls hitting a response timeout each
        # placed a real stop — two live stops, second one untracked.
        # Routed through the SAME persisted-submission mechanism ordinary
        # trade orders use, under role="protect" — a distinct slot from
        # "buy"/"sell" so a protective stop (ccxt side "sell") is never
        # confused with an ordinary strategy SELL that happens to be
        # pending at the same time.
        try:
            _price_str = self._exchange.price_to_precision(self.symbol, stop_price)
            raw = self._create_order_persisted(
                "protect", "sell", quantity, stop_price,
                lambda cid: self._exchange.create_order(
                    self.symbol, "market", "sell", quantity,
                    params={"stopLossPrice": _price_str, "clientOrderId": cid},
                ),
                label=f"native stop placement [{self.symbol}]",
            )
            fill_order = self._handle_protective_placement_result(
                raw, quantity, is_trailing=False,
                stop_price=stop_price, trailing_pct=None,
            )
            self._save_state()
            # Confirmed placed/adopted and durably tracked (native_stop_
            # order_id + saved state) — the submission concern is resolved
            # regardless of which branch inside _create_order_persisted
            # produced this order.
            self._resolve_pending_submission("protect")
            return fill_order
        except _SubmissionOutcomeUnknown as exc:
            # Outcome genuinely unresolved — do NOT clear tracked state
            # (there may be a stop resting we can't yet confirm) and do
            # NOT treat this as "failed" (the software SL/TP alert below
            # implies nothing is protecting the position, which may be
            # false). The pending-submission "protect" slot stays
            # persisted; the next sync_protective_stop call retries
            # reconciliation via the SAME client_order_id.
            logger.error(
                "NATIVE STOP PLACEMENT UNRESOLVED [%s]: %s — leaving "
                "existing tracked state untouched.", self.symbol, exc,
            )
            self._alerter.error(
                f"NATIVE STOP PLACEMENT UNRESOLVED [{self.symbol}]: {exc}. "
                f"Could not confirm whether a protective stop is actually "
                f"resting — will retry reconciliation on the next sync "
                f"rather than assume success or failure."
            )
            return None
        except Exception as exc:
            # Confirmed failure (including _SubmissionAborted — a
            # pre-submit persistence failure means nothing was ever sent).
            logger.error("Native stop placement FAILED [%s]: %s", self.symbol, exc)
            self._alerter.error(
                f"NATIVE STOP FAILED [{self.symbol}]: could not place backstop "
                f"stop-loss for {quantity:.6f} @ {stop_price:.2f} — {exc}. "
                f"Position is UNPROTECTED if the bot goes down. Software SL/TP "
                f"still works while the bot is running."
            )
            self._native_stop_order_id    = None
            self._native_stop_price       = None
            self._native_stop_is_trailing = False
            self._resolve_pending_submission("protect")
            return None

    def _place_native_trailing_stop(self, quantity: float, trailing_pct: float) -> "Order | None":
        """
        Places a Kraken 'trailing-stop' order (market-triggered — ccxt derives
        this ordertype from the trailingPercent param, same non-limit
        reasoning as _place_native_stop). Unlike the static stop, Kraken's own
        matching engine tracks the peak/trough itself from the moment this
        order is accepted — the trigger reprices server-side as the market
        moves favorably, so no repeated placement calls are needed to follow
        a rising price the way sync_protective_stop's caller does for the
        static path. A fresh call here (e.g. resizing on a partial fill)
        necessarily restarts the exchange's tracked peak from the price at
        placement time — there's no ccxt/Kraken amend for a resting order's
        volume, so this is an accepted precision loss on resize, same as the
        static order's own per-resize snapshot.

        Deliberately NO retry — identical reasoning to _place_native_stop:
        a retry after an accepted-but-timed-out create_order would place an
        untracked duplicate.

        params={"trailingPercent": ...} verified 2026-08-19 — not just by
        reading, by RUNNING the actual installed ccxt (4.5.56, sha256
        604e267f4d5246491ab0f3ec88ecd9526defad3b7abc0c5d79871fa8cce2eac5 —
        .venv/lib/python3.11/site-packages/ccxt/kraken.py) against this exact
        param. Re-run `verify_kraken_trailing_stop_param.py` (repo root) any
        time to reproduce. That run's literal output:

            ex.order_request('createOrder', 'BTC/CAD', 'market',
                {'pair': 'XBTCAD', 'type': 'sell', 'ordertype': 'market',
                 'volume': '0.00100000'},
                amount=0.001, price=None,
                params={'trailingPercent': '2.0000'})
            ==>
            {'pair': 'XBTCAD', 'type': 'sell', 'ordertype': 'trailing-stop',
             'volume': '0.00100000', 'trigger': 'last', 'price': '+2.0000%'}

        i.e. Kraken's native trailing-stop ordertype + a relative '+X%' price
        field — exactly the shape Kraken's own AddOrder docs describe
        (docs.kraken.com/api/docs/rest-api/add-order/, fetched 2026-08-19):
        ordertype enum includes 'trailing-stop' with no spot/margin
        distinction; price is documented as "must use a relative price...
        the `+` prefix... The `%` suffix also works". The literal ccxt source
        producing this (kraken.py, installed copy, verbatim):

            2058:        trailingPercent = self.safe_string(params, 'trailingPercent')
            2062:        isTrailingPercentOrder = trailingPercent is not None
            2094:        elif isTrailingAmountOrder or isTrailingPercentOrder:
            2097:                trailingPercentString = ('+' + trailingPercent) if (trailingPercent.endswith('%')) else ('+' + trailingPercent + '%')
            2101:            trailingActivationPriceType = self.safe_string(params, 'trigger', 'last')
            2102:            request['trigger'] = trailingActivationPriceType
            2103:            if isLimitOrder or (trailingLimitAmount is not None) or (trailingLimitPercent is not None):
            2112:            else:
            2113:                request['ordertype'] = 'trailing-stop'
            2114:                if trailingPercent is not None:
            2115:                    request['price'] = trailingPercentString

        ccxt's own docstring on this param says "*margin only*" (kraken.py:1637)
        — confirmed NOT enforced anywhere in create_order()/order_request()
        (no market['spot']/market['margin'] check exists in either), and the
        sibling stopLossPrice param under the identical annotation is what
        the existing static backstop already places live on spot BTC/CAD.
        """
        # 2026-09-18 PASS-3 review finding (P0): same fix as _place_native_stop
        # — routed through _create_order_persisted under role="protect" so a
        # response-lost timeout is reconciled (via client_order_id) instead
        # of assumed failed and silently re-attempted.
        try:
            _pct_str = f"{trailing_pct * 100:.4f}"
            raw = self._create_order_persisted(
                "protect", "sell", quantity, None,
                lambda cid: self._exchange.create_order(
                    self.symbol, "market", "sell", quantity,
                    params={"trailingPercent": _pct_str, "clientOrderId": cid},
                ),
                label=f"native trailing stop placement [{self.symbol}]",
            )
            fill_order = self._handle_protective_placement_result(
                raw, quantity, is_trailing=True,
                stop_price=None, trailing_pct=trailing_pct,
            )
            self._save_state()
            self._resolve_pending_submission("protect")
            return fill_order
        except _SubmissionOutcomeUnknown as exc:
            logger.error(
                "NATIVE TRAILING STOP PLACEMENT UNRESOLVED [%s]: %s — leaving "
                "existing tracked state untouched.", self.symbol, exc,
            )
            self._alerter.error(
                f"NATIVE TRAILING STOP PLACEMENT UNRESOLVED [{self.symbol}]: "
                f"{exc}. Could not confirm whether a protective stop is "
                f"actually resting — will retry reconciliation on the next sync."
            )
            return None
        except Exception as exc:
            logger.error("Native trailing stop placement FAILED [%s]: %s", self.symbol, exc)
            self._alerter.error(
                f"NATIVE TRAILING STOP FAILED [{self.symbol}]: could not place "
                f"backstop trailing stop for {quantity:.6f} trailing "
                f"{trailing_pct * 100:.2f}% — {exc}. Position is UNPROTECTED "
                f"if the bot goes down. Software SL/TP still works while the "
                f"bot is running."
            )
            self._native_stop_order_id    = None
            self._native_stop_price       = None
            self._native_stop_is_trailing = False
            self._resolve_pending_submission("protect")
            return None

    @property
    def native_stop_is_trailing(self) -> bool:
        return self._native_stop_is_trailing

    @property
    def native_stop_price(self) -> float | None:
        """Static stop level, or None for a trailing stop (Kraken tracks the
        peak server-side — there's no fixed price to read back) or when
        nothing is resting. Public so main.py's restart-recovery seeding
        (_seed_native_stop_state) can mirror this already-reconciled state
        into symbol_state without reaching into a private attribute."""
        return self._native_stop_price

    def sync_protective_stop(
        self, stop_price: float | None, trailing_pct: float | None = None,
    ) -> "list[Order]":
        """
        Reconcile the resting native stop with the current position. Call
        after every fill that changes position (BUY, strategy SELL, SL/TP
        exit, partial TP) and once at startup for a held position with no
        confirmed resting stop.

        trailing_pct>0 and position>0: cancel any existing resting stop and
            place a native Kraken trailing-stop sized to the CURRENT
            position, trailing by trailing_pct. Takes priority over
            stop_price when both are given.
        stop_price>0 and position>0 (trailing_pct None/0): cancel any
            existing resting stop (if any) and place a fresh static stop
            sized to the CURRENT position. Idempotent and safe to call
            repeatedly.
        Otherwise (stop_price=None and no trailing_pct, or position<=0):
            cancel any existing resting stop, don't replace (position
            closed — nothing to protect).

        No-op when the feature is disabled or dry-run. Never raises —
        failures are logged/alerted from the helpers above, not here; this
        must never be able to crash the trading loop.

        Returns EVERY fill Order discovered during this call, in the order
        they actually happened — the cancel-side discovery (if the old
        stop filled during cancellation) FIRST, then the replacement-
        placement-side discovery (if the response for the NEW stop shows
        it already executed too) SECOND. Empty list if nothing was
        discovered. 2026-09-18 follow-up review finding (P1), sharpened
        2026-09-19 PASS-5: this used to return only ONE Optional Order —
        when a cancel discovered a terminal partial fill from the OLD stop
        AND the replacement's own placement response showed it had
        ALREADY filled too (a second, independent execution event), the
        `placement_fill if placement_fill is not None else fill_order`
        expression silently discarded whichever one wasn't returned.
        Reproduced: position 0.002, old stop cancels with 0.001 filled,
        replacement comes back closed with another 0.001 filled — executor
        inventory correctly reaches zero and the journal holds both 0.001
        entries, but the old return contract could report only one of them
        to the caller's PositionManager/capital-pool/trade-log consumer.
        Callers (bot/main.py) must process EVERY entry in this list,
        in order, through the same fill-bookkeeping path a normal SELL
        gets — see _process_discovered_sell_fill / the plural
        _process_discovered_sell_fills loop helper.
        """
        if self.dry_run or not self._native_stop_loss_enabled:
            return []
        discovered: list[Order] = []
        outcome, fill_order = self._cancel_native_stop()
        if fill_order is not None:
            discovered.append(fill_order)
        if outcome in ("unknown", "partial"):
            # "unknown" — cancellation of the existing stop couldn't be
            # confirmed. "partial" — the stop filled SOME of its quantity
            # but is CONFIRMED still open/resting for the remainder
            # (_cancel_native_stop deliberately retained its tracked id
            # for exactly this case) — a real fill DID happen (fill_order
            # is not None) even though no replacement is placed. Either
            # way, placing a replacement now risks two live stops resting
            # on the exchange at once — leave it; the next
            # sync_protective_stop call (next fill/cycle) tries again.
            logger.error(
                "PROTECTIVE STOP SYNC ABORTED [%s]: existing resting stop "
                "is %s — not placing a replacement this cycle.",
                self.symbol, outcome,
            )
            return discovered
        # outcome == "filled" already ran _record_stop_triggered_fill,
        # which reduced self._portfolio.position — the check below then
        # naturally sees the updated (likely zero, or a genuine still-open
        # remainder with no resting stop at all — tracked id was cleared)
        # position and does the right thing either way.
        if self._portfolio.position <= 0:
            return discovered
        placement_fill = None
        if trailing_pct is not None and trailing_pct > 0:
            placement_fill = self._place_native_trailing_stop(self._portfolio.position, trailing_pct)
        elif stop_price is not None and stop_price > 0:
            placement_fill = self._place_native_stop(self._portfolio.position, stop_price)
        if placement_fill is not None:
            discovered.append(placement_fill)
        return discovered

    # ── State persistence ─────────────────────────────────────────────

    def _save_state(self) -> bool:
        """Persist portfolio state to disk so restarts can reconcile.

        Returns True on success, False on failure — 2026-09-18 PASS-3
        review finding: callers that need a save to have actually
        succeeded BEFORE proceeding (e.g. persisting a submission intent
        before contacting the exchange) previously had no way to check;
        this only ever set self._state_write_healthy, which nothing
        consulted before deciding whether to go ahead."""
        state = {
            "symbol":       self.symbol,
            "cash":         self._portfolio.cash,
            "position":     self._portfolio.position,
            "cost_basis":   self._portfolio._cost_basis,
            "realized_pnl": self._portfolio.realized_pnl,
            "fees_paid":    self._fees_paid,
            "bot_opened":   self._bot_opened_position,
            "native_stop_order_id":    self._native_stop_order_id,
            "native_stop_price":       self._native_stop_price,
            "native_stop_is_trailing": self._native_stop_is_trailing,
            "pending_journal_entries": self._pending_journal_entries,
            "pending_submissions":     self._pending_submissions,
            "native_stop_last_recorded_filled": self._native_stop_last_recorded_filled,
            "native_stop_last_recorded_cost":   self._native_stop_last_recorded_cost,
            "native_stop_last_recorded_fee":    self._native_stop_last_recorded_fee,
            "order_progress": self._order_progress,
            "unresolved_stop_recoveries": self._unresolved_stop_recoveries,
            "startup_recovery_cost_basis": self._startup_recovery_cost_basis,
            "startup_recovery_position":   self._startup_recovery_position,
            "saved_at":     datetime.now(timezone.utc).isoformat(),
        }
        try:
            from bot.atomic_json import atomic_write_json
            atomic_write_json(self._state_path, state)
            logger.warning(
                "State saved: cash=%.2f pos=%.6f", state["cash"], state["position"],
            )
            self._state_write_healthy = True
            return True
        except Exception as exc:
            logger.error("Failed to save state: %s", exc)
            if self._state_write_healthy:
                self._alerter.error(
                    f"STATE WRITE FAILED [{self.symbol}]: {exc}. New BUYs are "
                    f"blocked until a state save succeeds again — trading on "
                    f"top of accounting that isn't durably persisted risks "
                    f"losing it on a crash/restart."
                )
            self._state_write_healthy = False
            return False

    def _record_pending_journal_entry(self, order: Order) -> None:
        """Capture everything bot/main.py needs to write this fill's
        trade_log row, BEFORE the state save that persists it — so a crash
        between the accounting update and the (separate, downstream)
        trade_log write is recoverable on restart via
        pending_journal_entries, instead of the fill silently having no
        record anywhere despite the portfolio already reflecting it.
        Appends (never overwrites) — a second fill recorded before the
        first is acked must not lose the first's recovery record.

        exec_key comes straight from order.exec_key (2026-09-18 PASS-3
        review finding — see Order.exec_key's own docstring for why this
        replaced an order_id+counter scheme that could collide across a
        restart) — TradeLog uses it for an idempotent insert, so a replay
        that runs twice (e.g. the ack itself is what didn't survive a
        second crash) writes the row at most once, AND a normal write
        followed by a replay of the SAME fill (crash between them) is
        equally deduplicated, since both use this identical key. pnl is
        included so a replayed SELL carries real P&L instead of NULL.

        Does NOT save state itself (2026-09-19 PASS-5 review finding) — the
        caller mutates this queue as part of a single larger in-memory
        transaction (portfolio + progress + journal) and persists it all
        together in ONE _save_state() call."""
        self._pending_journal_entries.append({
            "kind":         "fill",
            "order_id":     order.order_id,
            "exec_key":     order.exec_key,
            "side":         order.side.value,
            "symbol":       self.symbol,
            "quantity":     order.quantity,
            "price":        order.price,
            "pnl":          order.pnl,
            "fee_cost":     order.fee_cost,
            "fee_currency": order.fee_currency,
            "filled_at":    (order.filled_at or order.created_at).isoformat(),
        })

    def _record_fee_adjustment_journal_entry(
        self, order_id: str, delta_fee: float, fee_currency: str,
    ) -> None:
        """PASS-5 review finding (P1): a fee-only correction (native-stop or
        ordinary order) updated executor cash/fees_paid but had no durable
        accounting-adjustment event for TradeLog — once the original
        quantity fill was already logged and acknowledged, the late fee
        never reached the database net-of-fee reports read from. Queued on
        the SAME pending-journal mechanism fills use (a distinct "kind" so
        bot/main.py's replay routes it to a fee-adjustment consumer instead
        of log_fill), with its own unique adjustment_id as the idempotency
        key — a restart or a repeated replay applies it at most once.
        Does NOT save state itself — see _record_pending_journal_entry."""
        self._pending_journal_entries.append({
            "kind":           "fee_adjustment",
            "adjustment_id":  str(uuid.uuid4()),
            "order_id":       order_id,
            "symbol":         self.symbol,
            "delta_fee":      delta_fee,
            "fee_currency":   fee_currency,
            "recorded_at":    datetime.now(timezone.utc).isoformat(),
        })

    def ack_journal_entry(self, order_id: str) -> None:
        """Called by bot/main.py once the trade_log row for order_id has
        actually been written — removes that ONE fill entry (the first
        queued match, FIFO, restricted to kind="fill"/unset — 2026-09-19
        PASS-5: a fee_adjustment entry can legitimately share the same
        underlying order_id and must never be matched/removed here, only by
        its own adjustment_id via ack_fee_adjustment) so a future restart
        doesn't replay an already-logged fill. A mismatched order_id
        (nothing queued for it) is a no-op, logged: never clears a
        DIFFERENT fill's still-pending entry."""
        for i, entry in enumerate(self._pending_journal_entries):
            if entry.get("kind", "fill") == "fill" and entry.get("order_id") == order_id:
                del self._pending_journal_entries[i]
                self._save_state()
                return
        if self._pending_journal_entries:
            logger.warning(
                "ack_journal_entry(%s) does not match any pending fill entry "
                "(pending entries: %s) — not clearing anything",
                order_id, [(e.get("kind", "fill"), e.get("order_id")) for e in self._pending_journal_entries],
            )

    def ack_fee_adjustment(self, adjustment_id: str) -> None:
        """Called by bot/main.py once the fee-adjustment row for
        adjustment_id has actually been written to TradeLog — removes that
        ONE fee_adjustment entry so a future restart doesn't replay an
        already-applied correction. A mismatched adjustment_id is a no-op,
        logged."""
        for i, entry in enumerate(self._pending_journal_entries):
            if entry.get("kind") == "fee_adjustment" and entry.get("adjustment_id") == adjustment_id:
                del self._pending_journal_entries[i]
                self._save_state()
                return
        if self._pending_journal_entries:
            logger.warning(
                "ack_fee_adjustment(%s) does not match any pending "
                "fee_adjustment entry — not clearing anything", adjustment_id,
            )

    @property
    def pending_journal_entries(self) -> list[dict]:
        """Fills whose accounting effect is already persisted but whose
        trade_log row may be missing (crash between the two writes) — an
        empty list means every recorded fill has been acknowledged. Read
        at startup by bot/main.py to replay each before resuming normal
        trading. A copy — callers must use ack_journal_entry() to mutate."""
        return list(self._pending_journal_entries)

    @property
    def state_write_healthy(self) -> bool:
        return self._state_write_healthy

    @property
    def pending_submissions(self) -> dict[str, dict]:
        """Submissions whose outcome hasn't been CONFIRMED TERMINAL yet,
        keyed by role ("buy"/"sell" for ordinary trade submissions,
        "protect" for native-stop placement) — an empty dict means nothing
        is currently unresolved. 2026-09-18 PASS-3 review finding: an
        order the exchange ACKNOWLEDGED (accepted, still open/unsettled)
        used to clear this immediately, which is not the same as a
        confirmed terminal outcome — this now stays populated until the
        caller (execute() / _place_native_stop) confirms one. A new
        submission of the SAME role refuses to proceed while its slot is
        occupied, always trying to resolve that entry (via its own
        client_order_id) first; a different role's slot is untouched.
        Survives a restart via _save_state()/_load_state()."""
        return dict(self._pending_submissions)

    @property
    def startup_sync_healthy(self) -> bool:
        """False if this process's startup cash and/or position
        reconciliation against the real exchange balance failed — cash/
        position are running on a fallback (configured starting_cash / the
        last saved state) rather than a confirmed-real number. Never
        re-evaluated mid-run; a restart after the exchange/network issue
        resolves is what clears it."""
        return self._startup_sync_healthy

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
        self._native_stop_order_id    = state.get("native_stop_order_id")
        _nsp                          = state.get("native_stop_price")
        self._native_stop_price       = float(_nsp) if _nsp is not None else None
        # Informational only — startup reconciliation in main.py always
        # re-arms a STATIC fallback on a restart-with-gap regardless of what
        # kind the previous process had resting (trail_peak/atr_sl reset to
        # 0 in-memory on every restart too, so there's nothing to resume the
        # trailing distance FROM). Restored here only so a still-open order
        # confirmed by _verify_resting_stop_on_startup logs its real kind.
        self._native_stop_is_trailing = bool(state.get("native_stop_is_trailing", False))
        # Backward-compat migration: an older state file has the singular
        # "pending_journal_entry" key (a dict or None) instead of today's
        # "pending_journal_entries" list — wrap it rather than silently
        # losing an unacked entry across the upgrade.
        if "pending_journal_entries" in state:
            self._pending_journal_entries = list(state.get("pending_journal_entries") or [])
        else:
            _legacy = state.get("pending_journal_entry")
            self._pending_journal_entries = [_legacy] if _legacy is not None else []
        # Backward-compat migration: an older state file has the singular
        # "pending_submission" key (a dict or None, keyed implicitly by
        # whatever side it was) instead of today's role-keyed dict.
        if "pending_submissions" in state:
            self._pending_submissions = dict(state.get("pending_submissions") or {})
        else:
            _legacy_sub = state.get("pending_submission")
            self._pending_submissions = (
                {_legacy_sub["side"]: _legacy_sub}
                if _legacy_sub and _legacy_sub.get("side") else {}
            )
        self._native_stop_last_recorded_filled = float(
            state.get("native_stop_last_recorded_filled", 0.0) or 0.0
        )
        self._native_stop_last_recorded_cost = float(
            state.get("native_stop_last_recorded_cost", 0.0) or 0.0
        )
        self._native_stop_last_recorded_fee = float(
            state.get("native_stop_last_recorded_fee", 0.0) or 0.0
        )
        self._order_progress = dict(state.get("order_progress") or {})
        # PASS-8 review finding (P1, finding 3): migrated from a single
        # dict-or-None slot to a list — a pre-existing state file (this
        # session's own earlier schema) may still have the OLD singular
        # key; wrap it rather than silently losing an in-flight recovery
        # across the upgrade.
        if "unresolved_stop_recoveries" in state:
            self._unresolved_stop_recoveries = list(state.get("unresolved_stop_recoveries") or [])
        else:
            _legacy_unresolved = state.get("unresolved_stop_recovery")
            self._unresolved_stop_recoveries = [_legacy_unresolved] if _legacy_unresolved else []
        self._startup_recovery_cost_basis = float(state.get("startup_recovery_cost_basis", 0.0) or 0.0)
        self._startup_recovery_position   = float(state.get("startup_recovery_position", 0.0) or 0.0)
        if self._unresolved_stop_recoveries:
            logger.error(
                "UNRESOLVED NATIVE-STOP HISTORY ON RESTART [%s]: %s — "
                "prior final-state lookup(s) failed; will retry.",
                self.symbol, self._unresolved_stop_recoveries,
            )
        if self._pending_submissions:
            logger.error(
                "UNRESOLVED SUBMISSION(S) ON RESTART [%s]: %s — this process "
                "will try to reconcile each (via its own client_order_id) "
                "before attempting any new submission of the same role.",
                self.symbol, self._pending_submissions,
            )
        logger.warning(
            "Accounting restored: cost_basis=%.2f pnl=%.2f fees=%.4f (saved %s)",
            self._portfolio._cost_basis, self._portfolio.realized_pnl,
            self._fees_paid, state.get("saved_at", "?"),
        )
        if self._pending_journal_entries:
            logger.error(
                "UNACKED FILL JOURNAL ENTRIES [%s]: %d — this fill's "
                "accounting is already reflected above, but its trade_log "
                "row(s) may be missing (a crash between the two writes). "
                "Caller must replay each into trade_log and call "
                "ack_journal_entry() for it. %s",
                self.symbol, len(self._pending_journal_entries),
                self._pending_journal_entries,
            )
        return True

    # ── Validation ────────────────────────────────────────────────────

    def _lookup_amt_min(self) -> float | None:
        """Best-effort amt_min lookup for the pre-trade min-size guard.
        Mirrors _validate_order()'s own lookup but never raises — returns
        None (guard no-ops) if markets aren't loaded or the symbol is
        missing, same as _validate_order()'s own soft-fail behavior there."""
        if self._markets is None:
            return None
        market = self._markets.get(self.symbol)
        if not market:
            return None
        return market.get("limits", {}).get("amount", {}).get("min")

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

    def _find_untracked_entry_order(
        self, side: str, client_order_id: str | None = None,
        order_id: str | None = None,
    ) -> "tuple[dict | None, bool]":
        """After an exception during order submission, check whether the
        exchange actually accepted the order despite the lost/failed
        response — a network error on the response does not mean the
        request never reached Kraken (2026-09 finding: the old code assumed
        it did and placed a market order straight on top, risking a
        duplicate).

        A resting (still-open) order isn't the only way that exception can
        resolve — the order may have already fully filled and closed by the
        time we check, which fetch_open_orders() alone can never see
        (reproduced independently: an empty open-orders list was read as
        'nothing to adopt' even when the original order had, in fact,
        already filled). client_order_id — a UUID generated fresh for this
        specific placement attempt, sent as Kraken's cl_ord_id — resolves
        this definitively: check both open and closed orders for a match,
        rather than guessing from order shape alone.

        Falls back to the coarser 'exactly one same-side non-stop order'
        heuristic only if no client_order_id was supplied.

        order_id: the exchange's own order id from a PRIOR resolved call on
        this same pending submission (2026-09-18 PASS-4 review finding P0):
        a persisted pending record can already know the real order_id (set
        the moment an earlier call adopted or placed it), but reconciliation
        was still re-deriving it from a client-id search over only the
        latest 10 closed orders — an order absent from that limited page is
        NOT proof it was never submitted, just that it isn't on that one
        page (a restart or a long gap between checks can easily push it
        off). When a stored order_id is known, query it DIRECTLY via
        fetch_order() first — authoritative, unbounded by any page size.

        Returns (order, confirmed_empty):
          (order, True)   — found and adopted; confirmed_empty is moot.
          (None, True)    — the lookup(s) completed successfully and
              genuinely found nothing (or an ambiguous multi-match, logged
              loudly). Safe for the caller to treat as a real rejection.
          (None, False)   — a lookup itself failed (network/exchange error
              verifying). 2026-09-18 review finding: the old version
              returned bare None for this case too, which every caller then
              treated as "confirmed empty" and fell back to a fresh order —
              risking a duplicate against a submission whose outcome is
              genuinely unknown, not confirmed absent. Callers must NOT
              treat this the same as a confirmed-empty result."""
        if order_id:
            try:
                direct = fetch_with_retry(
                    lambda: self._exchange.fetch_order(order_id, self.symbol),
                    attempts=2, delay_s=1.0,
                    label=f"post-error order reconciliation (direct id) [{self.symbol}]",
                )
                return direct, True
            except ccxt.OrderNotFound:
                # Confirmed BY THE EXCHANGE ITSELF: no such order exists.
                # Fall through to the client-id/heuristic checks below only
                # as a defensive fallback — they should not normally find
                # anything a confirmed-absent order_id lookup didn't.
                logger.warning(
                    "Stored order_id %s not found on %s (confirmed by the "
                    "exchange) — falling back to client-id/heuristic checks.",
                    order_id, self.symbol,
                )
            except Exception as exc:
                logger.warning(
                    "Could not verify stored order_id %s on %s (%s) — "
                    "outcome unknown, not confirmed failed.",
                    order_id, self.symbol, exc,
                )
                return None, False
        try:
            open_orders = fetch_with_retry(
                lambda: self._exchange.fetch_open_orders(self.symbol),
                attempts=2, delay_s=1.0,
                label=f"post-error order reconciliation (open) [{self.symbol}]",
            )
        except Exception as exc:
            logger.warning(
                "Could not verify order state after a submission error (%s) "
                "— outcome unknown, not confirmed failed", exc,
            )
            return None, False

        if client_order_id:
            match = _match_by_client_order_id(open_orders, client_order_id)
            if match is not None:
                return match, True
            try:
                closed_orders = fetch_with_retry(
                    lambda: self._exchange.fetch_closed_orders(self.symbol, limit=10),
                    attempts=2, delay_s=1.0,
                    label=f"post-error order reconciliation (closed) [{self.symbol}]",
                )
            except Exception as exc:
                logger.warning(
                    "Could not check closed orders after a submission error "
                    "(%s) — the order may have already filled and closed; "
                    "outcome unknown, not confirmed failed", exc,
                )
                return None, False
            return _match_by_client_order_id(closed_orders, client_order_id), True

        # No client_order_id available (shouldn't happen in normal use —
        # every call site generates one) — fall back to the coarser
        # same-side/non-stop heuristic against open orders only.
        candidates = [
            o for o in open_orders
            if not _is_native_stop_order(o) and str(o.get("side", "")).lower() == side
        ]
        if len(candidates) == 1:
            return candidates[0], True
        if len(candidates) > 1:
            logger.error(
                "Multiple untracked %s orders found on %s after a submission "
                "error — cannot tell which is ours, not auto-adopting any",
                side, self.symbol,
            )
        return None, True

    def _create_order_persisted(
        self,
        role: str,
        ccxt_side: str,
        quantity: float,
        price: "float | None",
        order_call,
        label: str,
        fast_reject_exceptions: tuple = (),
    ) -> dict:
        """
        THE single choke point for every live order submission this
        executor makes — the limit-chase primary attempt, every one of its
        market fallbacks, the direct passive-limit BUY, the direct market
        order, and native-stop placement all route through this.
        `order_call(client_order_id)` performs the actual
        self._exchange.create_order(...) call (each call site has a
        different positional/keyword shape — this wrapper doesn't try to
        unify that, only the submission bookkeeping around it).

        role: the pending-submission slot key — "buy"/"sell" for ordinary
        trade entry/exit, "protect" for native-stop placement. Independent
        slots (2026-09-18 PASS-3 review finding): an opposite-side or
        different-purpose submission must never overwrite another still-
        unresolved one just because they happen to share ccxt_side (a
        protective stop and a strategy SELL are both ccxt side "sell" but
        must never be confused with each other).

        IMPORTANT — this only PERSISTS the submission intent and performs
        the network call; it does NOT clear the pending-submission slot on
        a successful/adopted response (2026-09-18 PASS-3 review finding:
        the exchange ACKNOWLEDGING a request, e.g. status="open", is not
        proof of terminal settlement — clearing here let a second
        execute() call submit again while the first order was still
        genuinely open). The caller MUST call _resolve_pending_submission()
        once it has independently confirmed a terminal outcome (filled/
        closed/rejected) — see execute()'s bottom success/reject paths and
        _place_native_stop()/_place_native_trailing_stop().

        fast_reject_exceptions: exception types that are a definite,
        synchronous rejection from the exchange (e.g. ccxt.InvalidOrder —
        Kraken saying "no" is not an ambiguous lost response) — these skip
        the reconciliation round-trip entirely and re-raise immediately,
        clearing the pending slot (a confirmed non-event, safe to clear
        right away, unlike a merely-accepted order).
        """
        _entry = self._pending_submissions.get(role)
        if _entry is not None:
            _cid = _entry.get("client_order_id")
            adopted, confirmed_empty = self._find_untracked_entry_order(
                ccxt_side, _cid, order_id=_entry.get("order_id"),
            )
            if adopted is not None:
                _adopted_status = str(adopted.get("status") or "").lower()
                _adopted_filled = float(adopted.get("filled") or 0.0)
                if _adopted_status in _CANCELLED_TERMINAL_STATUSES and _adopted_filled <= 0:
                    # 2026-09-18 PASS-4 review finding (P1): a CONFIRMED
                    # canceled/rejected/expired attempt with NOTHING filled
                    # is not a legitimate outcome to "adopt" — it's the same
                    # already-dead attempt this exact caller (e.g. the limit
                    # chase's own retry loop) already confirmed terminal a
                    # moment ago. Adopting it here returned the dead order
                    # as if it were the result of THIS call, re-fed it back
                    # into the chase, and got re-discovered/re-adopted on
                    # every subsequent retry (including the final market
                    # fallback) — no replacement or fallback order was ever
                    # actually placed. Treat it exactly like a confirmed-
                    # empty result: release the slot and let the caller's
                    # own next attempt get a genuinely fresh identity.
                    logger.warning(
                        "SUBMISSION RECOVERY [%s/%s]: prior unresolved "
                        "submission (client_order_id=%s) found CONFIRMED "
                        "%s with no fill — releasing it (not adopting a "
                        "dead order) and proceeding.",
                        self.symbol, role, _cid, _adopted_status,
                    )
                    del self._pending_submissions[role]
                    self._save_state()
                else:
                    logger.warning(
                        "SUBMISSION RECOVERY [%s/%s]: prior unresolved submission "
                        "(client_order_id=%s) found resting/filled — adopting it "
                        "instead of placing a new order this cycle.",
                        self.symbol, role, _cid,
                    )
                    self._pending_submissions[role] = {**_entry, "order_id": adopted.get("id")}
                    self._save_state()
                    return adopted
            elif confirmed_empty:
                logger.warning(
                    "SUBMISSION RECOVERY [%s/%s]: prior unresolved submission "
                    "(client_order_id=%s) confirmed never placed — clearing "
                    "it and proceeding.", self.symbol, role, _cid,
                )
                del self._pending_submissions[role]
                self._save_state()
            else:
                raise _SubmissionOutcomeUnknown(
                    f"a prior {role} submission on {self.symbol} "
                    f"(client_order_id={_cid}) is still unresolved — "
                    f"refusing a new submission until it resolves"
                )

        client_order_id = str(uuid.uuid4())
        self._pending_submissions[role] = {
            "side": ccxt_side, "client_order_id": client_order_id,
            "quantity": quantity, "price": price, "label": label,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        if not self._save_state():
            # 2026-09-18 PASS-3 review finding (P0): a failed pre-submit
            # persistence used to be silently ignored (only
            # _state_write_healthy flipped) — the exchange call proceeded
            # anyway, so an order with no durable recovery record could go
            # live. We know FOR CERTAIN nothing was submitted yet (the
            # exchange was never contacted) — remove the entry that never
            # actually persisted and abstain, rather than proceed blind.
            del self._pending_submissions[role]
            raise _SubmissionAborted(
                f"could not durably persist {role} submission intent for "
                f"{self.symbol} — refusing to submit without a recovery record"
            )

        try:
            raw = order_call(client_order_id)
        except fast_reject_exceptions:
            del self._pending_submissions[role]
            self._save_state()
            raise
        except Exception as exc:
            logger.warning(
                "%s: %s (%s) — checking whether the exchange accepted it "
                "before treating as failed", label, type(exc).__name__, exc,
            )
            adopted, confirmed_empty = self._find_untracked_entry_order(ccxt_side, client_order_id)
            if adopted is not None:
                logger.warning(
                    "%s order %s found resting/filled despite the submission "
                    "error — adopting it instead of assuming failure",
                    label, adopted.get("id"),
                )
                self._pending_submissions[role] = {
                    **self._pending_submissions[role], "order_id": adopted.get("id"),
                }
                self._save_state()
                return adopted
            if confirmed_empty:
                del self._pending_submissions[role]
                self._save_state()
                raise
            raise _SubmissionOutcomeUnknown(
                f"could not verify {label} order state after a submission "
                f"error on {self.symbol} ({type(exc).__name__})"
            ) from exc

        self._pending_submissions[role] = {
            **self._pending_submissions[role], "order_id": raw.get("id"),
        }
        self._save_state()
        return raw

    def _resolve_pending_submission(self, role: str) -> None:
        """Called once the caller has independently confirmed a TERMINAL
        outcome (filled/closed/rejected) for the submission in this role's
        slot — clears it so a future submission of the same role doesn't
        pointlessly try to reconcile an already-settled order. A no-op if
        nothing is pending for this role (always safe to call)."""
        if role in self._pending_submissions:
            del self._pending_submissions[role]
            self._save_state()

    def _record_order_delta(
        self, role: str, order_id: str,
        cumulative_filled: float, cumulative_cost: float, cumulative_fee: float,
    ) -> "tuple[float, float, float]":
        """
        2026-09-18 PASS-4 review finding (P0): generalizes the same delta-
        tracking discipline native stops already had
        (_native_stop_last_recorded_*) to ORDINARY buy/sell orders — a
        genuinely still-OPEN order with a partial fill is not a completed
        order; only the NEW increment since the last check may be recorded,
        never the full cumulative amount again.

        Tracks cumulative filled/cost/fee for whichever order currently
        occupies `role`'s progress slot, returning only the delta since the
        last call. Seeds fresh (the full cumulative becomes the delta) the
        first time a given order_id is seen for this role, or if the
        role's previously tracked order_id differs (a genuinely different
        order — e.g. after the prior one resolved and a fresh one began).

        2026-09-19 PASS-5 review finding (P0): this used to call
        _save_state() itself, immediately — persisting "this delta has been
        consumed" BEFORE the caller applied its actual economic effect
        (portfolio cash/position) and journal entry, which happen later in
        the SAME execute() call. A crash in between left a durably
        persisted progress snapshot with NO corresponding fill anywhere:
        on restart the identical exchange snapshot recomputes a ZERO delta
        (already "consumed" per the saved progress) and the real 0.001 BTC
        fill was gone for good — reproduced exactly. Fixed: this method is
        now PURE (in-memory only, no I/O). The caller mutates progress,
        applies the fill's economic effect, appends the journal entry, and
        AND ONLY THEN calls _save_state() exactly once — so a crash before
        that single save leaves the OLD (pre-delta) state entirely intact
        (the identical delta is safely re-derived from scratch on restart),
        and a crash after it captures the fill's progress, economics and
        journal entry together, atomically, in one file replacement.
        """
        prior = self._order_progress.get(role)
        if prior is None or prior.get("order_id") != order_id:
            prior = {"order_id": order_id, "filled": 0.0, "cost": 0.0, "fee": 0.0}
        delta_filled = max(0.0, cumulative_filled - prior["filled"])
        delta_cost   = max(0.0, cumulative_cost   - prior["cost"])
        delta_fee    = max(0.0, cumulative_fee    - prior["fee"])
        self._order_progress[role] = {
            "order_id": order_id, "filled": cumulative_filled,
            "cost": cumulative_cost, "fee": cumulative_fee,
        }
        return delta_filled, delta_cost, delta_fee

    def _clear_order_progress(self, role: str) -> None:
        """Called once an order reaches a CONFIRMED terminal status —
        clears its progress-tracking entry so a genuinely NEW order later
        occupying this role's slot starts its own delta tracking fresh
        rather than being compared against the old order's cumulative
        totals. A no-op if nothing is tracked for this role.

        2026-09-19 PASS-5 review finding (P0): PURE (in-memory only, no
        I/O) for the same reason _record_order_delta is — the caller must
        persist this together with the fill it's finalizing, in ONE save,
        not as a separate write."""
        if role in self._order_progress:
            del self._order_progress[role]

    def _apply_ordinary_order_fee_only_adjustment(
        self, role: str, order_id: str, delta_fee: float, fee_currency: str,
    ) -> None:
        """PASS-5 review finding (P1): the ordinary-order counterpart to
        _apply_native_stop_fee_only_adjustment — an ordinary BUY/SELL's
        filled quantity/cost are unchanged since the last check, but its
        FEE was updated or finalized. Applies the fee delta directly to
        cash/fees_paid and queues a durable "fee_adjustment" journal entry
        for TradeLog — deliberately does NOT fabricate a zero-quantity fill
        Order. PURE (no I/O) — the caller persists this together with
        whatever else it mutated (progress advance, pending_submissions'
        order_id, terminal cleanup) in ONE save."""
        quote = self.symbol.split("/")[1]
        if fee_currency and fee_currency != quote:
            logger.warning(
                "Order fee-only adjustment currency mismatch [%s/%s]: "
                "fee=%.6f %s but quote=%s — not deducting (manual "
                "reconciliation needed)", self.symbol, role, delta_fee, fee_currency, quote,
            )
            return
        self._portfolio.cash -= delta_fee
        self._fees_paid      += delta_fee
        logger.warning(
            "ORDER FEE ADJUSTMENT [%s/%s]: order %s fee finalized +%.6f "
            "(quantity/cost unchanged since the last check) — applied once.",
            self.symbol, role, order_id, delta_fee,
        )
        self._record_fee_adjustment_journal_entry(order_id, delta_fee, fee_currency)

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
        self._maker_fallback_reason = None

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
                self._maker_fallback_reason = f"orderbook fetch failed ({type(exc).__name__})"
                return self._create_order_persisted(
                    side, side, quantity, None,
                    lambda cid: self._exchange.create_order(
                        self.symbol, "market", side, quantity, None, {"clientOrderId": cid},
                    ),
                    label=f"market fallback (orderbook fetch failed) [{self.symbol}]",
                )

            logger.warning(
                "LIMIT %s attempt %d/%d: %.6f %s @ %.2f (post-only, tick_pct=%.6f)",
                side.upper(), timeout_attempts + 1, max_attempts,
                quantity, self.symbol, limit_price_f, tick_pct,
            )

            # A unique id THIS specific attempt can be traced by regardless
            # of whether create_order()'s response is ever seen — offline-
            # verified against the real installed ccxt (order_request() maps
            # clientOrderId -> Kraken's cl_ord_id, coexists with postOnly's
            # oflags=post). See the exception handler below for why this
            # exists: fetch_open_orders() alone can't tell "never reached
            # Kraken" apart from "reached Kraken and already fully filled".
            try:
                # postOnly (not timeInForce="PO") — found 2026-08-26 after
                # SOL/CAD's first live BUY silently fell back to market.
                # ccxt's Kraken adapter passes timeInForce through nearly
                # verbatim into Kraken's own `timeinforce` field, which only
                # accepts GTC/IOC/GTD — "PO" isn't one, so Kraken rejected
                # every attempt with EGeneral:Invalid arguments:timeinforce,
                # silently falling back to market every time since this was
                # introduced (commit 08644b1f, 2026-06-22). postOnly=True is
                # ccxt's actual unified param for this (translates to
                # oflags=post) — verified against the real installed ccxt via
                # verify_kraken_postonly_param.py (no network calls in the
                # assertion itself; only the one public load_markets() call).
                #
                # Routed through _create_order_persisted (2026-09-18
                # follow-up review finding): persists the submission intent
                # before calling create_order and refuses a fresh same-side
                # attempt while a prior one is still unresolved.
                # ccxt.InvalidOrder is a definite, synchronous rejection
                # (Kraken saying "would cross the spread"), not an
                # ambiguous lost response — fast_reject_exceptions skips
                # the reconciliation round-trip for it so the immediate
                # tick_pct-halving retry below stays exactly as fast as before.
                raw = self._create_order_persisted(
                    side, side, quantity, limit_price,
                    lambda cid: self._exchange.create_order(
                        self.symbol, "limit", side, quantity, limit_price,
                        {"postOnly": True, "clientOrderId": cid},
                    ),
                    label=f"limit chase attempt [{self.symbol}]",
                    fast_reject_exceptions=(ccxt.InvalidOrder,),
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
                    self._maker_fallback_reason = "spread too tight for post-only"
                    return self._create_order_persisted(
                        side, side, quantity, None,
                        lambda cid: self._exchange.create_order(
                            self.symbol, "market", side, quantity, None, {"clientOrderId": cid},
                        ),
                        label=f"market fallback (spread too tight) [{self.symbol}]",
                    )
                continue
            except (_SubmissionOutcomeUnknown, _SubmissionAborted):
                # Outcome genuinely unresolved (or the submission was never
                # even attempted due to a persistence failure) — do NOT
                # fall back to market (that's exactly the duplicate-order
                # risk this exists to prevent). Propagate up to execute(),
                # which holds back entirely. The pending-submission slot
                # (if any) stays persisted so the next attempt reconciles
                # this one first.
                raise
            except Exception as exc:
                # _create_order_persisted already tried reconciliation and
                # confirmed the limit order genuinely never landed (not an
                # unknown outcome — that path raises _SubmissionOutcomeUnknown
                # instead, caught above) — safe to fall back to market, same
                # as this method always has for a confirmed limit failure.
                self._maker_fallback_reason = f"exchange rejected limit order ({type(exc).__name__})"
                return self._create_order_persisted(
                    side, side, quantity, None,
                    lambda cid: self._exchange.create_order(
                        self.symbol, "market", side, quantity, None, {"clientOrderId": cid},
                    ),
                    label=f"market fallback (limit rejected) [{self.symbol}]",
                )

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
                post_status = str(post_cancel.get("status") or "").lower()
                if post_status not in _CANCELLED_TERMINAL_STATUSES:
                    # cancel_order() either failed outright (cancel_ok=False)
                    # or "succeeded" but the order still reads back as
                    # live/open — eventual consistency, a race, or a
                    # silently ignored cancel. Either way the first order
                    # may still be resting on the exchange; looping back to
                    # place a second one here is exactly how you end up with
                    # two live orders on the same side. Abort without
                    # re-placing, same as the unverifiable-state branch below.
                    logger.error(
                        "LIMIT %s %s: cancel did not reach a confirmed "
                        "terminal state (status=%r, cancel_ok=%s) — aborting "
                        "chase without re-placing.",
                        side.upper(), order_id, post_cancel.get("status"), cancel_ok,
                    )
                    return post_cancel
            except Exception as post_exc:
                logger.warning("post-cancel fetch_order %s failed: %s", order_id, post_exc)
                # Whether cancel_order itself failed, or it succeeded but this
                # verification call failed, we cannot confirm the order's fate —
                # it may still be live or may have caught a fill in the cancel
                # race. Re-placing risks a double fill; return the unresolved
                # dict and let execute()'s poll loop settle it.
                logger.error(
                    "LIMIT %s %s: state unverifiable after cancel (cancel_ok=%s) — "
                    "aborting chase without re-placing.",
                    side.upper(), order_id, cancel_ok,
                )
                return raw

            timeout_attempts += 1

        logger.warning(
            "limit chase failed after %d retries, falling back to market order",
            cfg.exchange.limit_chase_max_retries,
        )
        self._maker_fallback_reason = (
            f"limit chase timed out after {cfg.exchange.limit_chase_max_retries} retries"
        )
        return self._create_order_persisted(
            side, side, quantity, None,
            lambda cid: self._exchange.create_order(
                self.symbol, "market", side, quantity, None, {"clientOrderId": cid},
            ),
            label=f"market fallback (chase exhausted) [{self.symbol}]",
        )

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

        # Cleared per call so a stale flag from a prior order (e.g. one that
        # hit the qty=0 guard after a fallback) can't misfire on this one.
        self._maker_fallback_reason = None
        # Default True (dry-run and every already-terminal live path never
        # override this) — only a genuinely still-open live order with a
        # partial fill sets this False, so the bottom success block knows
        # NOT to resolve the pending-submission slot for it. See the
        # "Shared fee extraction" section below (2026-09-18 PASS-4 review
        # finding).
        _is_order_terminal = True

        if signal not in (Signal.BUY, Signal.SELL):
            return None

        side  = OrderSide.BUY if signal == Signal.BUY else OrderSide.SELL
        quote = self.symbol.split("/")[1]

        if side == OrderSide.BUY and quantity <= 0:
            logger.warning("LiveExecutor: BUY quantity=0, skipping")
            return None
        if side == OrderSide.BUY and not self._startup_sync_healthy:
            # 2026-09-18 review finding: don't open a new position sized
            # against cash/holdings figures that were never actually
            # confirmed against the real exchange balance this startup.
            logger.error(
                "BUY BLOCKED [%s]: startup balance/position sync failed for "
                "this process — refusing a new entry until restarted with "
                "a working exchange connection.", self.symbol,
            )
            return None
        if side == OrderSide.BUY and not self._state_write_healthy:
            # 2026-09-18 review finding: a durable accounting write is a
            # precondition for a new entry, not an afterthought — opening a
            # new position on top of state that isn't confirmed persisted
            # risks losing track of it entirely on a crash/restart. SELL/
            # exits are never blocked by this — reducing risk must still be
            # possible even while state persistence is degraded.
            logger.error(
                "BUY BLOCKED [%s]: last state save failed — refusing a new "
                "entry until accounting can be durably persisted again.",
                self.symbol,
            )
            return None
        if side == OrderSide.SELL and self._portfolio.position <= 0:
            logger.warning("LiveExecutor: SELL with no position, skipping")
            return None

        if side == OrderSide.SELL:
            # 2026-09-18 review finding: this used to unconditionally
            # overwrite ANY requested SELL quantity with the full position
            # — a caller asking for a partial exit (PARTIAL_TP_PCT) got its
            # entire position liquidated instead, silently. Every real
            # caller (bot/main.py's strategy SELL, urgent SL/TP, and
            # partial TP) already passes a deliberate quantity — honor it,
            # capped at what's actually held so an overlarge/stale request
            # can never oversell. A non-positive/missing quantity (the only
            # case nothing upstream should ever produce) still falls back
            # to a full close, preserving the old default for that case.
            quantity = (
                self._portfolio.position if quantity is None or quantity <= 0
                else min(quantity, self._portfolio.position)
            )
        ts       = datetime.now(timezone.utc)

        # Pre-trade minimum-size guard: warn BEFORE placing the order if the
        # computed BUY qty is within MIN_SIZE_SAFETY_MARGIN of amt_min —
        # losing a signal to a hard rejection here costs a full signal cycle
        # (~3 weeks) and 1/15 of the capital gate. Alert only — never
        # auto-round the quantity up, which would silently break the ATR
        # risk cap this sizing exists to enforce. The order still proceeds
        # (or fails _validate_order below) exactly as it would without this
        # guard; it changes nothing about what gets sent.
        if side == OrderSide.BUY:
            _amt_min = self._lookup_amt_min()
            if _amt_min and _amt_min > 0 and quantity < _amt_min * _MIN_SIZE_SAFETY_MARGIN:
                _headroom = quantity / _amt_min
                logger.warning(
                    "MIN-SIZE GUARD [%s]: computed BUY qty %.8f is within the "
                    "%.2fx safety margin of amt_min %.8f — headroom %.2fx",
                    self.symbol, quantity, _MIN_SIZE_SAFETY_MARGIN, _amt_min, _headroom,
                )
                self._alerter.error(
                    f"MIN-SIZE GUARD [{self.symbol}]: computed BUY qty {quantity:.8f} "
                    f"vs amt_min {_amt_min:.8f} — headroom {_headroom:.2f}x "
                    f"(safety margin {_MIN_SIZE_SAFETY_MARGIN:.2f}x). A price rise or "
                    f"wider stop could push the next signal below the exchange minimum."
                )

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
            # A resting native stop reserves 100% of the base asset on the
            # exchange, so ANY SELL for the position — urgent SL/TP, strategy
            # exit, partial TP — fails "Insufficient funds" until it's cancelled.
            # Cancel it HERE, before placing the sell. SOL/CAD incident
            # 2026-08-27: the cancel used to run only AFTER a successful fill
            # (bot/main.py's SL/TP block), which could never happen while the
            # stop held the coins — an 8-minute retry-and-reject loop against
            # Kraken. On a full close the stop stays gone; on a partial fill
            # main.py's _resync_native_stop re-places it smaller; on a rejected
            # SELL _rearm_native_stop_after_failed_sell puts it back.
            _native_stop_restore: "tuple[float | None, bool] | None" = None
            if side == OrderSide.SELL and self._native_stop_order_id:
                _native_stop_restore = (
                    self._native_stop_price, self._native_stop_is_trailing,
                )
                logger.warning(
                    "Cancelling resting native stop %s before %s SELL of %.8f %s",
                    self._native_stop_order_id, "urgent" if urgent else "",
                    quantity, self.symbol,
                )
                _cancel_outcome, _stop_fill_order = self._cancel_native_stop()

                if _cancel_outcome == "unknown":
                    # Cancellation outcome is unresolved — the stop may
                    # still be resting (holding the coins) or may have
                    # already filled. Placing a SELL now risks either an
                    # InsufficientFunds retry loop or, worse, doubling an
                    # exit that already happened. Hold back; the tracked
                    # stop id/price were left untouched by _cancel_native_stop
                    # so the next cycle's sync_protective_stop/SELL attempt
                    # tries the cancel again rather than assuming success.
                    logger.error(
                        "SELL ABORTED [%s]: native stop cancellation could "
                        "not be confirmed — refusing to place a SELL while "
                        "unresolved.", self.symbol,
                    )
                    self._alerter.error(
                        f"SELL HELD BACK [{self.symbol}]: native stop "
                        f"cancellation could not be confirmed before a SELL "
                        f"— refusing to place an order while it's unresolved "
                        f"(would risk selling into a still-resting stop, or "
                        f"duplicating its own exit). Will retry next cycle."
                    )
                    return None

                if _stop_fill_order is not None:
                    # The native stop itself already executed this exit
                    # (won the race with our cancel) — already fully
                    # recorded via _record_stop_triggered_fill. That IS
                    # the SELL result; do not place a second order on top.
                    return _stop_fill_order

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
                    # Routed through _create_order_persisted — same
                    # reconciliation the limit-chase path already uses,
                    # extended to these two paths (direct passive-limit
                    # BUY, direct/urgent market order) so an exception here
                    # is verified rather than assumed failed, AND (2026-09-18
                    # follow-up review finding) so an unresolved outcome is
                    # persisted and blocks a fresh same-side submission
                    # instead of silently retrying blind on the next call.
                    if self._order_type == "limit" and side == OrderSide.BUY and not urgent:
                        # Passive bid 0.2% below market — post-only guarantees maker rate (0.40%, confirmed Jun 14 fill)
                        limit_price = round(price * 0.998, 2)
                        logger.warning(
                            "LIMIT BUY: %.6f %s @ %.2f (0.2%% below %.2f, post-only)",
                            quantity, self.symbol, limit_price, price,
                        )
                        raw = self._create_order_persisted(
                            ccxt_side, ccxt_side, quantity, limit_price,
                            lambda cid: self._exchange.create_order(
                                symbol=self.symbol, type="limit", side=ccxt_side,
                                amount=quantity, price=limit_price,
                                params={"postOnly": True, "clientOrderId": cid},
                            ),
                            label="direct LIMIT BUY",
                        )
                    else:
                        logger.warning(
                            "LIVE ORDER: %s %.6f %s",
                            side.value, quantity, self.symbol,
                        )
                        raw = self._create_order_persisted(
                            ccxt_side, ccxt_side, quantity, None,
                            lambda cid: self._exchange.create_order(
                                symbol=self.symbol, type="market", side=ccxt_side,
                                amount=quantity, params={"clientOrderId": cid},
                            ),
                            label="urgent/direct MARKET" if urgent else "direct MARKET",
                        )
                    order_id_str = str(raw.get("id", ""))
                    filled_qty   = float(raw.get("filled") or 0.0)
                    fill_price   = float(raw.get("average") or raw.get("price") or price)

                    # Poll up to 9 times for 'closed' status — but ONLY if the
                    # order isn't ALREADY confirmed terminal. _create_order_persisted
                    # can return an ADOPTED historical order (found already closed
                    # via reconciliation, e.g. after a submission-outcome-unknown
                    # recovery) — re-polling that ID with fetch_order() is redundant
                    # at best, and at worst overwrites a good terminal snapshot with
                    # a stale/misleading one from a query the exchange (or a test's
                    # mock) doesn't keep consistent for an already-settled order
                    # (PASS-4 review finding 1a: this exact clobbering hid a
                    # terminal fill behind a fabricated "still open" status).
                    last_raw = raw
                    if order_id_str and str(last_raw.get("status") or "").lower() not in _CANCELLED_TERMINAL_STATUSES:
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
                            # Confirmed terminal (closed) with genuinely
                            # nothing filled — safe to treat like any other
                            # confirmed non-event.
                            if _native_stop_restore is not None:
                                self._rearm_native_stop_after_failed_sell(_native_stop_restore)
                            self._resolve_pending_submission(side.value.lower())
                            return None
                    else:
                        # Limit order with filled=0, or order not yet closed —
                        # do not infer, and — 2026-09-18 PASS-3 review finding
                        # — do NOT re-arm protection either: the order may
                        # still be genuinely resting and could fill later,
                        # same "don't act on an unresolved outcome" reasoning
                        # as the _SubmissionOutcomeUnknown handler above.
                        # Deliberately leave the pending-submission slot
                        # (if any) untouched too — still unresolved.
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

                # 2026-09-18 PASS-4 review finding (P0): a genuinely
                # still-OPEN order with SOME quantity filled was treated as
                # a completed order — the full cumulative quantity/cost/fee
                # got applied as a one-shot fill, the pending-submission
                # slot was cleared, and the REMAINING resting quantity
                # became completely untracked (a later fill on the same
                # order could never be reconciled as a delta, and a later
                # signal could place a genuinely duplicate order).
                # Reproduced: status="open", filled=0.001, amount=0.002 —
                # old code recorded a FILLED 0.001 order and cleared
                # tracking despite 0.001 still resting. Fixed with the SAME
                # delta-tracking discipline native stops already have:
                # only a CONFIRMED terminal status means "this order is
                # done" — anything else records just the NEW delta and
                # keeps the submission pending.
                # Delta accounting runs REGARDLESS of terminal status — a
                # terminal snapshot may be the tail end of an order that was
                # already partially recorded while it was still open (e.g.
                # this exact BUY: 0.001/0.002 open, then 0.002/0.002 closed).
                # Only booking the delta in the non-terminal branch and the
                # FULL cumulative amount in the terminal branch would
                # double-count the already-recorded partial the moment the
                # order finally closes.
                # 2026-09-19 PASS-5 review finding (P0): _record_order_delta/
                # _clear_order_progress are now PURE (in-memory only, no
                # I/O) — every mutation below (progress, pending_submissions'
                # order_id, order_progress clearing) stays in memory until
                # ONE save commits them together with the fill's actual
                # economic effect (portfolio cash/position) and journal
                # entry, applied later in this same call. A crash before
                # that single save leaves the untouched OLD state on disk
                # (the identical exchange snapshot is safely re-processed
                # from scratch on restart); a crash after it captures
                # everything atomically. The two early-return branches below
                # (nothing new / fee-only) are the only places that must
                # save explicitly THEMSELVES, since nothing later in this
                # call will do it for them.
                _role = side.value.lower()
                _is_order_terminal = str(last_raw.get("status") or "").lower() in _CANCELLED_TERMINAL_STATUSES
                _cumulative_cost = float(last_raw.get("cost") or 0.0)
                if _cumulative_cost <= 0:
                    _cumulative_cost = fill_price * quantity
                _delta_qty, _delta_cost, _delta_fee = self._record_order_delta(
                    _role, order_id_str, quantity, _cumulative_cost, fee_cost,
                )
                if self._pending_submissions.get(_role):
                    self._pending_submissions[_role] = {
                        **self._pending_submissions[_role], "order_id": order_id_str,
                    }

                if _delta_qty <= 0:
                    # 2026-09-19 PASS-5 review finding (P1): a fee-only
                    # correction (quantity/cost unchanged since the last
                    # check) used to be discarded outright here — apply it
                    # directly, same discipline as the native-stop fix,
                    # rather than fabricating a zero-quantity "fill".
                    if _delta_fee > 0:
                        self._apply_ordinary_order_fee_only_adjustment(
                            _role, order_id_str, _delta_fee, fee_currency,
                        )
                    if _is_order_terminal:
                        logger.warning(
                            "%s order %s closed TERMINAL with no NEW fill "
                            "since the last check (cumulative %.8f) — "
                            "nothing further to record; resolving.",
                            side.value, order_id_str, quantity,
                        )
                        self._clear_order_progress(_role)
                        self._resolve_pending_submission(_role)
                        self._save_state()
                    elif _delta_fee > 0:
                        # Still open, but the fee adjustment above changed
                        # durable state (cash/fees_paid/progress/pending
                        # order_id) — persist it now since nothing else in
                        # this call will.
                        self._save_state()
                    else:
                        logger.warning(
                            "%s order %s remains open with no NEW fill since "
                            "the last check (cumulative %.8f) — nothing to "
                            "record, submission stays pending.",
                            side.value, order_id_str, quantity,
                        )
                        # Nothing new observed at all — no save. A crash here
                        # leaves the prior (still-accurate) state on disk;
                        # the identical snapshot re-derives the same zero
                        # delta on restart, losing nothing.
                    return None
                logger.warning(
                    "%s order %s %s: new delta %.8f (cumulative %.8f)%s",
                    side.value, order_id_str,
                    "TERMINAL" if _is_order_terminal else "PARTIAL (still open)",
                    _delta_qty, quantity,
                    "" if _is_order_terminal else
                    " — recording the delta only; submission stays pending for the remainder.",
                )
                quantity   = _delta_qty
                fill_price = _delta_cost / _delta_qty if _delta_qty > 0 else fill_price
                fee_cost   = _delta_fee

                # Maker→taker silent-degradation guard. _place_limit_order sets
                # this whenever a post-only limit fell back to a market (taker)
                # order — the fill went through but at ~2x the fee. The
                # logger.warning inside that method names the path; this raises
                # it to a Telegram alert so it can't hide for months again (the
                # 2026-06→08 post-only bug did exactly that).
                if self._maker_fallback_reason:
                    logger.warning(
                        "MAKER FALLBACK [%s]: %s post-only limit degraded to a "
                        "market order — %s",
                        self.symbol, side.value, self._maker_fallback_reason,
                    )
                    self._alerter.error(
                        f"MAKER FALLBACK [{self.symbol}]: {side.value} was meant to "
                        f"be a post-only limit (maker fee ~0.25-0.40%) but filled as "
                        f"a market (taker) order (~0.80%) — {self._maker_fallback_reason}. "
                        f"The trade went through; this is a fee/execution alert, not "
                        f"a block."
                    )
                    self._maker_fallback_reason = None

            except _SubmissionOutcomeUnknown as exc:
                # A submission exception's outcome could not be confirmed
                # (the reconciliation lookup itself failed) — the order may
                # or may not have reached the exchange. Recording either a
                # fill or a REJECTED order here would be a guess; hold back
                # entirely so nothing downstream mistakes this for a
                # confirmed, safe-to-retry failure. 2026-09-18 review
                # finding: the old code always guessed (REJECTED or a
                # market fallback) here.
                #
                # 2026-09-18 PASS-3 review finding: do NOT re-arm the
                # cancelled native stop here for a SELL — we do not know
                # whether this SELL actually went through. If it did, the
                # position is smaller (or flat) than self._portfolio still
                # shows (accounting is untouched on this path), so a
                # "restore the old level" re-arm could size a replacement
                # stop against a position that's no longer accurate.
                # Determining the SELL's real outcome first, before
                # touching protection again, is the caller's job on the
                # next cycle (sync_protective_stop will attempt the cancel/
                # verify again and reconcile whatever is actually true).
                logger.error("ORDER OUTCOME UNKNOWN [%s]: %s", self.symbol, exc)
                self._alerter.error(
                    f"ORDER OUTCOME UNKNOWN [{self.symbol}]: {exc}. Not "
                    f"recording a fill or a rejection — verify on the "
                    f"exchange before the next signal."
                    + (
                        f" Native stop protection was cancelled before this "
                        f"attempt and has NOT been restored, since the SELL's "
                        f"own outcome is unresolved — check Kraken directly."
                        if _native_stop_restore is not None else ""
                    )
                )
                return None
            except _SubmissionAborted as exc:
                # The submission was never even attempted (a durable
                # pre-submit persistence failure) — we know FOR CERTAIN
                # nothing happened on the exchange, so it's safe to treat
                # exactly like a confirmed rejection: re-arm a cancelled
                # native stop (the position provably didn't change) and
                # record a REJECTED order (safe to retry).
                logger.error("SUBMISSION ABORTED [%s]: %s", self.symbol, exc)
                self._alerter.error(
                    f"SUBMISSION ABORTED [{self.symbol}]: {exc}. No order "
                    f"was placed."
                )
                if _native_stop_restore is not None:
                    self._rearm_native_stop_after_failed_sell(_native_stop_restore)
                self._resolve_pending_submission(side.value.lower())
                order = Order(
                    order_id      = "rejected",
                    symbol        = self.symbol,
                    side          = side,
                    quantity      = quantity,
                    price         = price,
                    status        = OrderStatus.REJECTED,
                    created_at    = ts,
                    reject_reason = f"Submission aborted: {exc}",
                )
                self._rejects.append(order)
                return order
            except ccxt.InsufficientFunds as exc:
                logger.error("Insufficient funds: %s", exc)
                if _native_stop_restore is not None:
                    self._rearm_native_stop_after_failed_sell(_native_stop_restore)
                self._resolve_pending_submission(side.value.lower())
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
                if _native_stop_restore is not None:
                    self._rearm_native_stop_after_failed_sell(_native_stop_restore)
                self._resolve_pending_submission(side.value.lower())
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

        # ── Slippage guard (2026-08-07) ──────────────────────────────────
        # Post-fill only — the fill has already happened by the time slippage
        # is known, so this can never block a trade, only flag one that
        # landed unexpectedly worse than the price the bot evaluated the
        # signal against. Dry-run naturally never trips this (fill_price ==
        # price exactly, skipped entirely to avoid log noise). Direction-
        # aware: only an unfavorable fill counts — paying less on a BUY or
        # receiving more on a SELL is never a problem worth flagging.
        if not self.dry_run and price > 0:
            _slippage_pct = (
                (fill_price - price) / price if side == OrderSide.BUY
                else (price - fill_price) / price
            )
            logger.info(
                "Fill vs expected [%s]: %s expected=%.2f filled=%.2f slippage=%+.3f%%",
                self.symbol, side.value, price, fill_price, _slippage_pct * 100,
            )
            if self._max_slippage_pct > 0 and _slippage_pct > self._max_slippage_pct:
                logger.warning(
                    "SLIPPAGE GUARD [%s]: %s filled %.2f%% worse than expected "
                    "(expected %.2f, filled %.2f, threshold %.2f%%)",
                    self.symbol, side.value, _slippage_pct * 100, price, fill_price,
                    self._max_slippage_pct * 100,
                )
                self._alerter.error(
                    f"SLIPPAGE WARNING [{self.symbol}]: {side.value} filled at "
                    f"{fill_price:,.2f} vs expected {price:,.2f} — "
                    f"{_slippage_pct*100:.2f}% worse than expected "
                    f"(threshold {self._max_slippage_pct*100:.2f}%). The fill already "
                    f"happened — this is a post-fill alert, not a block."
                )

        pnl = None
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
                self._alerter.error(
                    f"FEE CURRENCY MISMATCH [{self.symbol}]: fee={fee_cost:.6f} "
                    f"{fee_currency} but quote={quote} — not deducted, cash ledger "
                    f"will silently drift from the exchange balance until reconciled manually"
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
            pnl          = pnl,
        )
        self._fills.append(order)
        self._record_pending_journal_entry(order)
        # 2026-09-18 PASS-3/4 review findings: only resolve the pending-
        # submission slot (and, 2026-09-19 PASS-5, clear order-progress
        # tracking) once the ORDER ITSELF is confirmed terminal
        # (_is_order_terminal, set above — True for dry-run and any
        # already-closed/canceled live order, False for a genuinely
        # still-open order that merely had a partial-fill delta recorded
        # this call). A no-op for dry-run (never populates either) or the
        # qty=0 GUARD paths above that return None instead of reaching
        # here (deliberately still unresolved).
        #
        # 2026-09-19 PASS-5 review finding (P0): this is now the ONLY save
        # in the delta_qty>0 path — _fills.append/_record_pending_journal_
        # entry above, and pending_submissions'/order_progress's mutations
        # (here and earlier in the "Shared fee extraction" section), are
        # all in-memory only until this ONE call. A crash before it leaves
        # the prior committed state entirely intact (the fill is safely
        # re-derived as the same delta on restart); a crash after it
        # captures the fill's progress, economics, pending-submission
        # state and journal entry together, atomically.
        if _is_order_terminal:
            self._clear_order_progress(side.value.lower())
            self._resolve_pending_submission(side.value.lower())
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
