"""
Simple threshold strategy: BUY below lower bound, SELL above upper bound, else HOLD.

Future extension point: implement the same evaluate() interface for any strategy
(moving average crossover, RSI, etc.) and swap it in main.py.
"""

from dataclasses import dataclass
from enum import Enum


class Signal(Enum):
    BUY = "BUY"
    SELL = "SELL"
    HOLD = "HOLD"


@dataclass
class ThresholdStrategy:
    buy_threshold: float
    sell_threshold: float

    def evaluate(self, price: float) -> Signal:
        if price < self.buy_threshold:
            return Signal.BUY
        if price > self.sell_threshold:
            return Signal.SELL
        return Signal.HOLD


# ---------------------------------------------------------------------------
# FUTURE: AI / ML strategy placeholder
# ---------------------------------------------------------------------------
# class MLStrategy:
#     """Drop-in replacement: loads a trained model and predicts BUY/SELL/HOLD."""
#     def evaluate(self, price: float) -> Signal: ...
