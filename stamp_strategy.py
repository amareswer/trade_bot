"""
stamp_strategy.py — record the current strategy code hash as the validated baseline.

Run this immediately after a passing walk-forward to mark the current code
as the version whose results are trusted.  The live bot and backtest.py will
emit a loud WARNING if strategy code diverges from this stamp.

Usage:
    python stamp_strategy.py

Optional env override:
    STRATEGY_HASH_FILE=/custom/path/validated_strategy_hash python stamp_strategy.py
"""
import os
from pathlib import Path

from bot.strategy.fingerprint import compute_strategy_hash

hash_file = Path(os.getenv("STRATEGY_HASH_FILE", "logs/validated_strategy_hash"))
hash_val  = compute_strategy_hash()

hash_file.parent.mkdir(parents=True, exist_ok=True)
hash_file.write_text(hash_val + "\n")

print(f"Strategy hash stamped:  {hash_val}")
print(f"Written to:             {hash_file}")
print()
print("The live bot and backtest.py will now warn if strategy code changes")
print("without a matching walk-forward re-run.")
