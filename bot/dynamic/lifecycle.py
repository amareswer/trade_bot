"""
Dynamic symbol lifecycle — admit, manage, and retire symbols in the paper
universe WITHOUT restarting the process.

Core invariant (point 4 of the dynamic-universe design): a symbol with an
OPEN position is never torn down just because it dropped out of the current
candidate list. It keeps being ticked, keeps its stop-loss/take-profit
managed, and is only retired once its position is fully flat AND it is no
longer eligible.

Persistence: a manifest file (`logs/dynamic_active_symbols.json` by default)
records every symbol currently admitted. On restart, `restart_recovery()`
reloads that manifest FIRST — independent of whatever the fresh universe
scan says this cycle — so a crash or restart can never silently lose track
of an open position. Each symbol's own trading state (cash/position/orders)
lives in its own state file (one per symbol, via `state_path_for`), the same
one-file-per-symbol pattern the live bot already uses for BTC/CAD and
SOL/CAD (`logs/live_state_<SYM>.json`) — just in a distinct directory/prefix
so this paper system can never collide with or overwrite live state.
"""
from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Callable

logger = logging.getLogger(__name__)

_DEFAULT_MANIFEST_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "logs", "dynamic_active_symbols.json",
)
_DEFAULT_STATE_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "logs", "dynamic_paper_state",
)


def state_path_for(symbol: str, state_dir: str = _DEFAULT_STATE_DIR) -> str:
    return os.path.join(state_dir, f"{symbol.replace('/', '_')}.json")


@dataclass
class SymbolHandle:
    """Everything one admitted symbol needs to be ticked each cycle. Mirrors
    the core of bot.main.run()'s per-symbol symbol_state[sym] dict, trimmed
    to what the dynamic paper runner actually uses (no native-stop / MTF /
    Telegram overlay fields — those are live-exchange-only features, not
    applicable to a dry-run paper evaluation of the core strategy)."""
    symbol:       str
    strategy:     object
    sm:           object   # TradingStateMachine
    pm:           object   # PositionManager
    executor:     object   # LiveExecutor(dry_run=True) or equivalent
    last_ts_ms:   int | None = None
    admitted_at:  float = field(default_factory=time.time)


class DynamicSymbolManager:
    def __init__(
        self,
        make_strategy: Callable[[], object],
        warmup_strategy: Callable[[object, object, str, str], "int | None"],
        make_state_machine: Callable[[], object],
        make_position_manager: Callable[[], object],
        make_executor: Callable[[str, str], object],   # (symbol, state_path) -> executor
        manifest_path: str = _DEFAULT_MANIFEST_PATH,
        state_dir: str = _DEFAULT_STATE_DIR,
    ):
        self._make_strategy         = make_strategy
        self._warmup_strategy       = warmup_strategy
        self._make_state_machine    = make_state_machine
        self._make_position_manager = make_position_manager
        self._make_executor         = make_executor
        self._manifest_path         = manifest_path
        self._state_dir             = state_dir
        self._active: dict[str, SymbolHandle] = {}

    # ── Public API ───────────────────────────────────────────────────────

    @property
    def active_symbols(self) -> list[str]:
        return list(self._active.keys())

    def get(self, symbol: str) -> SymbolHandle | None:
        return self._active.get(symbol)

    def items(self) -> list[tuple[str, SymbolHandle]]:
        return list(self._active.items())

    def is_admitted(self, symbol: str) -> bool:
        return symbol in self._active

    def has_open_position(self, symbol: str) -> bool:
        h = self._active.get(symbol)
        return h is not None and getattr(h.executor, "position", 0.0) > 1e-9

    def open_position_symbols(self) -> list[str]:
        return [s for s, h in self._active.items() if getattr(h.executor, "position", 0.0) > 1e-9]

    def admit(self, symbol: str, exchange, timeframe: str) -> SymbolHandle:
        """Initialize indicators/warmup/execution/state for a newly admitted
        symbol, without touching any other symbol. No-op (returns the
        existing handle) if already admitted."""
        if symbol in self._active:
            return self._active[symbol]

        strat = self._make_strategy()
        state_path = state_path_for(symbol, self._state_dir)
        self._warmup_strategy(strat, exchange, timeframe, symbol)
        handle = SymbolHandle(
            symbol=symbol,
            strategy=strat,
            sm=self._make_state_machine(),
            pm=self._make_position_manager(),
            executor=self._make_executor(symbol, state_path),
        )
        self._active[symbol] = handle
        self._save_manifest()
        logger.info("dynamic universe: admitted %s", symbol)
        return handle

    def retire_if_flat(self, symbol: str) -> bool:
        """Remove a symbol from active management IF it is flat. Returns
        True if retired, False if kept (still holding a position, or not
        currently admitted at all). Never retires a symbol with an open
        position, regardless of candidate-list membership."""
        handle = self._active.get(symbol)
        if handle is None:
            return False
        if getattr(handle.executor, "position", 0.0) > 1e-9:
            return False
        del self._active[symbol]
        self._save_manifest()
        logger.info("dynamic universe: retired %s (flat)", symbol)
        return True

    def sync_to_candidates(self, eligible_symbols: set[str]) -> list[str]:
        """Retire every admitted-but-flat symbol that has dropped out of the
        current eligible set. Returns the list of symbols actually retired.
        Symbols with an open position are untouched no matter what."""
        retired = []
        for sym in list(self._active.keys()):
            if sym in eligible_symbols:
                continue
            if self.retire_if_flat(sym):
                retired.append(sym)
        return retired

    def restart_recovery(self, exchange, timeframe: str) -> list[str]:
        """
        Reload the persisted manifest and re-admit every symbol on it,
        regardless of the current candidate list — a symbol that held an
        open position at the last shutdown must keep being managed even if
        it would no longer be eligible today. Also defensively scans the
        state directory for any per-symbol state file showing a nonzero
        position that ISN'T on the manifest (e.g. the manifest write raced
        the process dying) so a position is never silently lost.
        Returns the list of symbols recovered.
        """
        recovered: list[str] = []
        for sym in self._load_manifest():
            self.admit(sym, exchange, timeframe)
            recovered.append(sym)

        for sym in self._scan_state_dir_for_open_positions():
            if sym in self._active:
                continue
            logger.warning(
                "dynamic universe: %s has a state file showing an open "
                "position but was NOT on the manifest — recovering it "
                "defensively so the position isn't lost", sym,
            )
            self.admit(sym, exchange, timeframe)
            recovered.append(sym)

        return recovered

    # ── Persistence ──────────────────────────────────────────────────────

    def _save_manifest(self) -> None:
        try:
            os.makedirs(os.path.dirname(self._manifest_path), exist_ok=True)
            with open(self._manifest_path, "w") as f:
                json.dump({"symbols": list(self._active.keys()), "saved_at": time.time()}, f)
        except Exception as exc:
            logger.warning("dynamic universe: manifest save failed: %s", exc)

    def _load_manifest(self) -> list[str]:
        try:
            if not os.path.exists(self._manifest_path):
                return []
            with open(self._manifest_path) as f:
                data = json.load(f)
            return list(data.get("symbols", []))
        except Exception as exc:
            logger.warning("dynamic universe: manifest load failed: %s", exc)
            return []

    def _scan_state_dir_for_open_positions(self) -> list[str]:
        found: list[str] = []
        try:
            if not os.path.isdir(self._state_dir):
                return found
            for fname in os.listdir(self._state_dir):
                if not fname.endswith(".json"):
                    continue
                try:
                    with open(os.path.join(self._state_dir, fname)) as f:
                        data = json.load(f)
                    if float(data.get("position", 0.0) or 0.0) > 1e-9:
                        sym = fname[:-5].replace("_", "/", 1)
                        found.append(sym)
                except Exception:
                    continue
        except Exception as exc:
            logger.warning("dynamic universe: state dir scan failed: %s", exc)
        return found
