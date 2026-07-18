"""
Atomic JSON writes for state files (2026-07-17).

Why: live_state_*.json and ibkr_state.json were written with a plain
json.dump straight onto the live file — a crash or power loss mid-write
truncates the file the bots trust for position/cash/PnL state. State
integrity is safety-critical here (the 2026-06-27 external-holdings
incident was a bad state flag). RiskManager already used the tmp+replace
pattern; this makes it shared. os.replace() is atomic on POSIX — readers
see either the old file or the new one, never a partial write.
"""
from __future__ import annotations

import json
import os


def atomic_write_json(path: str, data, indent: int = 2) -> None:
    """Write data as JSON to path via tmp-file + atomic rename.

    Raises on failure (callers keep their existing try/except + logging —
    error handling policy stays theirs, only the write mechanics change).
    """
    dirpath = os.path.dirname(path)
    if dirpath:
        os.makedirs(dirpath, exist_ok=True)
    tmp_path = path + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=indent)
    os.replace(tmp_path, path)
