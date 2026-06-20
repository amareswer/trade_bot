"""
AIVerdict dataclass — the structured output of one AI analysis call.

Advisory only. Never triggers execution.

stop_loss and target_price are code-calculated from cfg.paper_stop_loss_pct /
cfg.paper_take_profit_pct and injected into the prompt — the AI copies them
verbatim rather than inventing its own levels.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Optional


@dataclass
class AIVerdict:
    symbol:        str
    signal:        str            # "BUY" | "SELL" | "HOLD"
    confidence:    int            # 0–100; < 55 is always coerced to HOLD
    target_price:  Optional[float]
    stop_loss:     Optional[float]
    reasoning:     str            # 2–4 sentence plain-English explanation
    trading_style: str            # "DAY" | "SWING" | "LONGTERM"
    timestamp:     datetime
    provider:      str = "unknown"  # which AI provider answered this call
