"""
AI advisory engine — OpenRouter-backed, advisory only.

Flow:
    IndicatorStrategy → signal
        → AIEngine.advise()       ← optional, never blocks execution
            → merge_signals()     ← AI can only conservatively downgrade to HOLD
                → RiskManager     ← still final authority

AI CANNOT:
  - Initiate trades when strategy says HOLD
  - Override the risk engine
  - Crash the bot on failure (all errors return None)

Requires env var: OPENROUTER_API_KEY
Missing key or any failure → AI silently disabled, strategy signal used as-is.
"""
from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass

from bot.strategy.threshold_strategy import Signal
from bot.execution.executor import Portfolio

logger = logging.getLogger(__name__)

_SYSTEM_PROMPT = """You are an advisory AI for a crypto paper-trading bot.
Your role is purely advisory — you never execute trades.

Given market data, output a JSON object with exactly these three fields:
  signal     : "BUY" | "SELL" | "HOLD"
  confidence : float 0.0 to 1.0
  reasoning  : one concise sentence (max 12 words)

Rules:
- When uncertain, output HOLD.
- Be conservative — a missed trade is safer than a wrong one.
- Respond with raw JSON only. No markdown, no explanation, no extra text."""


@dataclass
class AIAdvice:
    signal:     Signal
    confidence: float
    reasoning:  str
    latency_ms: float
    model:      str


class AIEngine:
    """
    Optional AI advisory layer backed by OpenRouter.

    advise() returns AIAdvice or None.
    None means AI is unavailable — caller uses strategy signal unchanged.
    """

    def __init__(
        self,
        model:          str   = "anthropic/claude-haiku-4-5",
        timeout_s:      float = 8.0,
        min_confidence: float = 0.65,
    ):
        self.model          = model
        self.timeout_s      = timeout_s
        self.min_confidence = min_confidence
        self._client        = None

        api_key = os.getenv("OPENROUTER_API_KEY")
        if not api_key:
            logger.warning("AI advisory disabled — OPENROUTER_API_KEY not set.")
            return

        try:
            from openai import OpenAI
            self._client = OpenAI(
                api_key  = api_key,
                base_url = "https://openrouter.ai/api/v1",
            )
            logger.info(
                "AIEngine ready | model=%s | timeout=%.0fs | min_conf=%.0f%%",
                model, timeout_s, min_confidence * 100,
            )
        except Exception as exc:
            logger.warning("AIEngine init failed (%s) — AI advisory disabled.", exc)

    @property
    def enabled(self) -> bool:
        return self._client is not None

    def advise(
        self,
        price:           float,
        rsi:             float | None,
        trend:           str   | None,
        strategy_signal: Signal,
        recent_prices:   list[float],
        portfolio:       Portfolio,
        symbol:          str = "BTC/USDT",
    ) -> AIAdvice | None:
        """
        Request an advisory signal from the AI.
        Returns None on any failure — caller falls back to strategy signal.
        """
        if not self.enabled:
            return None

        prompt = _build_prompt(price, rsi, trend, strategy_signal, recent_prices, portfolio, symbol)

        t0 = time.monotonic()
        try:
            response = self._client.chat.completions.create(
                model       = self.model,
                messages    = [
                    {"role": "system", "content": _SYSTEM_PROMPT},
                    {"role": "user",   "content": prompt},
                ],
                max_tokens  = 80,
                temperature = 0.2,
                timeout     = self.timeout_s,
            )
        except Exception as exc:
            logger.debug("AI call failed (%s) — using strategy signal.", exc)
            return None

        latency_ms = (time.monotonic() - t0) * 1000
        return _parse_response(response, latency_ms, self.model, self.min_confidence)


def merge_signals(strategy_signal: Signal, advice: AIAdvice | None) -> Signal:
    """
    Advisory merge rules:
      - HOLD always stays HOLD  (AI cannot initiate trades)
      - AI unavailable (None)   → strategy signal unchanged
      - AI agrees               → strategy signal unchanged
      - AI disagrees            → downgrade to HOLD (conservative)
      - AI confidence too low   → already normalised to HOLD inside _parse_response
    """
    if strategy_signal == Signal.HOLD:
        return Signal.HOLD
    if advice is None:
        return strategy_signal
    if advice.signal == strategy_signal:
        return strategy_signal
    return Signal.HOLD         # AI disagreed — hold off


# ── Internal helpers ─────────────────────────────────────────────────────────

def _build_prompt(
    price:           float,
    rsi:             float | None,
    trend:           str   | None,
    strategy_signal: Signal,
    recent_prices:   list[float],
    portfolio:       Portfolio,
    symbol:          str = "BTC/USDT",
) -> str:
    rsi_str    = f"{rsi:.1f}" if rsi is not None else "n/a"
    trend_str  = trend or "n/a"
    prices_str = ", ".join(f"{p:,.0f}" for p in recent_prices[-8:])
    return (
        f"Symbol: {symbol}\n"
        f"Current price: ${price:,.2f}\n"
        f"RSI(14): {rsi_str}\n"
        f"EMA trend: {trend_str}\n"
        f"Strategy signal: {strategy_signal.value}\n"
        f"Recent prices: [{prices_str}]\n"
        f"Portfolio — cash: ${portfolio.cash:,.2f} | position: {portfolio.position:.4f} {symbol.split('/')[0]}\n"
        f"\nShould the bot act on this signal?"
    )


def _parse_response(
    response,
    latency_ms:  float,
    model:       str,
    min_conf:    float,
) -> AIAdvice | None:
    try:
        raw = response.choices[0].message.content.strip()

        # Strip markdown fences some models add despite instructions
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]

        data = json.loads(raw)

        sig_str    = str(data.get("signal", "HOLD")).upper()
        signal     = Signal[sig_str] if sig_str in Signal.__members__ else Signal.HOLD
        confidence = float(data.get("confidence", 0.0))
        reasoning  = str(data.get("reasoning", "")).strip()

        # Treat low-confidence responses as HOLD
        if confidence < min_conf:
            signal = Signal.HOLD

        return AIAdvice(
            signal     = signal,
            confidence = confidence,
            reasoning  = reasoning,
            latency_ms = latency_ms,
            model      = model,
        )
    except Exception as exc:
        raw_snippet = ""
        try:
            raw_snippet = response.choices[0].message.content[:80]
        except Exception:
            pass
        logger.warning("AI parse failed (%s) | raw=%r", exc, raw_snippet)
        return None
