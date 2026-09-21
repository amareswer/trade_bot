"""
Persisted "last full reconciliation cycle" outcome (accounting review,
sixth pass, 2026-09-21, P1: "shadow report can falsely declare PASSED" —
a fresh checkpoint existed for the account-wide cash scope while a specific
symbol's own reconciliation had never completed, or had actually failed;
raw checkpoint existence alone cannot distinguish "this symbol was verified"
from "some unrelated write happened to land nearby in time" — checkpoints
can also survive a LATER failed cycle unchanged, since a failure doesn't
retract an earlier success).

This is the single authoritative record of what the LATEST cycle actually
concluded — including failure, which symbols it was asked to cover, the
four-way verdict, and which database it was computed against — written by
bot/main.py right after computing it, read by
scripts/accounting_shadow_report.py instead of inferring status from raw
checkpoint rows. A plain atomic JSON sidecar (bot/atomic_json.py), not a
new SQLite table — this is a single current-status record, not something
that accumulates history.

Two-phase write (accounting review, seventh pass, 2026-09-21, P1: "a
failed status write preserves an earlier PASSED result" — if reconciliation
failed and the write of THAT failure itself then raised, the file was left
holding whatever the PREVIOUS successful write had contained, which could
still read as fresh and PASSED). write_in_progress() must be called BEFORE
a cycle attempt starts, unconditionally invalidating any prior completed
result the instant a new attempt begins; write() is called after the
attempt concludes (success or exception) with the real outcome. A crash or
raised exception between those two calls leaves the file showing
in_progress=True — which CycleStatus.ready (and the shadow report) must
always treat as not-ready, never as "no news, assume the old result still
holds".
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from bot.atomic_json import atomic_write_json


@dataclass
class CycleStatus:
    computed_at: str
    requested_symbols: "list[str]"
    block_state_ok: bool
    block_state_explain: str
    four_way_ran: bool
    four_way_ready: "Optional[bool]"
    four_way_explain: "Optional[str]"
    db_identity: "Optional[str]" = None
    in_progress: bool = False

    @property
    def ready(self) -> bool:
        """The one thing a caller actually wants: did the LATEST cycle
        conclude everything is reconciled, for the symbols it actually
        covered. in_progress=True (an attempt started but never confirmed
        complete — see module docstring) and four_way_ran=False (e.g.
        account-cash itself was already blocked before four-way ever got a
        chance to run) are BOTH real, not-ready states, never read as
        healthy by omission."""
        return (not self.in_progress and self.block_state_ok
                and self.four_way_ran and bool(self.four_way_ready))


def write_in_progress(path: str, *, requested_symbols: "list[str]", db_identity: "Optional[str]" = None) -> None:
    """Call BEFORE a reconciliation cycle attempt starts. See module
    docstring for exactly what this closes."""
    atomic_write_json(path, {
        "computed_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "requested_symbols": sorted(requested_symbols),
        "block_state_ok": False,
        "block_state_explain": "cycle in progress — no result yet",
        "four_way_ran": False,
        "four_way_ready": None,
        "four_way_explain": None,
        "db_identity": db_identity,
        "in_progress": True,
    })


def write(
    path: str, *, requested_symbols: "list[str]", block_state_ok: bool, block_state_explain: str,
    four_way_ran: bool, four_way_ready: "Optional[bool]" = None, four_way_explain: "Optional[str]" = None,
    db_identity: "Optional[str]" = None,
) -> None:
    """Called once per completed cycle attempt (success OR failure) —
    every cycle overwrites this file with its own outcome and clears
    in_progress, so a caller reading it always sees the MOST RECENT
    attempt's true, confirmed-complete result, never a stale success left
    over from before a later failure, and never an in-progress marker
    outliving the attempt it belonged to."""
    atomic_write_json(path, {
        "computed_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "requested_symbols": sorted(requested_symbols),
        "block_state_ok": block_state_ok,
        "block_state_explain": block_state_explain,
        "four_way_ran": four_way_ran,
        "four_way_ready": four_way_ready,
        "four_way_explain": four_way_explain,
        "db_identity": db_identity,
        "in_progress": False,
    })


def read(path: str) -> "Optional[CycleStatus]":
    """None means no cycle has EVER attempted at this path — the caller's
    job (see scripts/accounting_shadow_report.py) is to treat that as
    NOT_VERIFIED, never as an absence of problems."""
    if not os.path.exists(path):
        return None
    with open(path) as f:
        data = json.load(f)
    return CycleStatus(
        computed_at=data.get("computed_at", ""),
        requested_symbols=list(data.get("requested_symbols", [])),
        block_state_ok=bool(data.get("block_state_ok", False)),
        block_state_explain=str(data.get("block_state_explain", "")),
        four_way_ran=bool(data.get("four_way_ran", False)),
        four_way_ready=data.get("four_way_ready"),
        four_way_explain=data.get("four_way_explain"),
        db_identity=data.get("db_identity"),
        in_progress=bool(data.get("in_progress", False)),
    )
