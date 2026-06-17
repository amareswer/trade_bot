"""
AI analysis engine for the stock bot — multi-provider, advisory only.

Supports three providers (set AI_PROVIDER in stock_bot/.env):
  openrouter         — meta-llama/llama-3.3-70b-instruct:free via openrouter.ai
  ollama_local       — any Ollama model running locally  (OLLAMA_BASE_URL)
  ollama_cloud/cloud — any Ollama model via ollama.com   (OLLAMA_CLOUD_API_KEY)

Failure modes:
  - Missing credentials → returns HOLD, confidence=0, reasoning="AI unavailable"
  - API call error      → returns HOLD, confidence=0, reasoning="AI unavailable"
  - JSON parse error    → returns HOLD, confidence=0, reasoning="AI parse error"

Never raises to the caller. One API call per symbol per cycle.
"""
from __future__ import annotations

import json
import logging
import os
import re   # used by _strip_fences and JSON object search
import time
from datetime import datetime

from dotenv import load_dotenv

from stock_bot.ai.prompt_builder import build_prompt
from stock_bot.ai.verdict        import AIVerdict
from stock_bot.research.aggregator import ResearchReport

import requests as _requests
from ollama import Client as OllamaClient

logger = logging.getLogger(__name__)

# Load root .env for OPENROUTER_API_KEY — kept separate from stock_bot/.env
_ROOT_ENV = os.path.normpath(
    os.path.join(os.path.dirname(__file__), "..", "..", ".env")
)
load_dotenv(dotenv_path=_ROOT_ENV, override=False)

_MODEL       = "meta-llama/llama-3.3-70b-instruct:free"  # OpenRouter default
_MAX_TOKENS  = 2048
_TEMPERATURE = 0.3
_TIMEOUT_S   = 20


def _hold_verdict(symbol: str, reason: str) -> AIVerdict:
    return AIVerdict(
        symbol        = symbol,
        signal        = "HOLD",
        confidence    = 0,
        target_price  = None,
        stop_loss     = None,
        reasoning     = reason,
        trading_style = "SWING",
        timestamp     = datetime.now(),
    )


def _strip_fences(raw: str) -> str:
    """Remove ```json ... ``` fences some models add despite instructions."""
    raw = re.sub(r"^```(?:json)?\s*", "", raw.strip(), flags=re.IGNORECASE)
    raw = re.sub(r"\s*```$", "", raw, flags=re.IGNORECASE)
    return raw.strip()


def _parse(raw: str, symbol: str) -> AIVerdict:
    logger.debug("AI raw response for %s: %r", symbol, raw[:400])
    text = _strip_fences(raw)

    # Auto-close missing closing brace (model occasionally omits it)
    text = text.strip()
    if text.startswith("{") and not text.endswith("}"):
        text = text + "}"

    # Step 1 — try standard parse
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        # Step 2 — find outermost { }
        match = re.search(r"\{[\s\S]*\}", text)
        if not match:
            raise ValueError("No JSON object found in AI response")
        text = match.group(0)

        # Step 3 — sanitise the reasoning field: it often contains unescaped
        # quotes, commas, and newlines that break the parser
        reasoning_clean = None
        reasoning_match = re.search(
            r'"reasoning"\s*:\s*"(.*?)"(?=\s*\})',
            text,
            re.DOTALL,
        )
        if reasoning_match:
            reasoning_clean = (
                reasoning_match.group(1)
                .replace('"', "'")
                .replace("\n", " ")
                .strip()
            )
            text = (
                text[: reasoning_match.start(1)]
                + reasoning_clean
                + text[reasoning_match.end(1) :]
            )

        # Step 4 — retry after sanitisation
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            # Step 5 — last resort: regex-extract each field individually
            data = {}
            for field, pattern in [
                ("signal",        r'"signal"\s*:\s*"(\w+)"'),
                ("confidence",    r'"confidence"\s*:\s*(\d+)'),
                ("trading_style", r'"trading_style"\s*:\s*"(\w+)"'),
            ]:
                m = re.search(pattern, text)
                if m:
                    data[field] = m.group(1)

            if "confidence" in data:
                data["confidence"] = int(data["confidence"])

            data.setdefault("signal",        "HOLD")
            data.setdefault("confidence",    0)
            data.setdefault("trading_style", "SWING")
            data.setdefault("target_price",  None)
            data.setdefault("stop_loss",     None)
            data.setdefault(
                "reasoning",
                reasoning_clean or "AI response parsed with fallback",
            )

    signal = str(data.get("signal", "HOLD")).upper()
    if signal not in {"BUY", "SELL", "HOLD"}:
        signal = "HOLD"

    confidence = int(round(float(data.get("confidence", 0))))
    confidence = max(0, min(100, confidence))

    # Enforce the confidence-floor rule
    if confidence < 55:
        signal = "HOLD"

    trading_style = str(data.get("trading_style", "SWING")).upper()
    if trading_style not in {"DAY", "SWING", "LONGTERM"}:
        trading_style = "SWING"

    def _opt_float(key: str) -> float | None:
        val = data.get(key)
        if val is None:
            return None
        try:
            return float(val)
        except (TypeError, ValueError):
            return None

    return AIVerdict(
        symbol        = symbol,
        signal        = signal,
        confidence    = confidence,
        target_price  = _opt_float("target_price"),
        stop_loss     = _opt_float("stop_loss"),
        reasoning     = str(data.get("reasoning", "")).strip(),
        trading_style = trading_style,
        timestamp     = datetime.now(),
    )


class AIEngine:
    """
    Stock bot AI analysis engine — multi-provider.

    analyze() always returns an AIVerdict — never raises, never blocks the loop.
    Provider is selected by AI_PROVIDER in stock_bot/.env.
    """

    def __init__(self) -> None:
        self._ready    = False
        self._provider = os.getenv("AI_PROVIDER", "openrouter").strip().lower()

        if self._provider == "openrouter":
            self._model   = _MODEL
            api_key       = os.getenv("OPENROUTER_API_KEY", "").strip()
            if not api_key:
                logger.warning("Stock AI disabled — OPENROUTER_API_KEY not set in root .env")
                return
            self._base_url = "https://openrouter.ai/api/v1/chat/completions"
            self._headers  = {
                "Content-Type":  "application/json",
                "Authorization": f"Bearer {api_key}",
            }

        elif self._provider == "ollama_local":
            self._model    = os.getenv("OLLAMA_MODEL", "llama3.2").strip()
            ollama_url     = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434").strip()
            self._base_url = ollama_url.rstrip("/") + "/v1/chat/completions"
            self._headers  = {"Content-Type": "application/json"}

        elif self._provider in ("ollama_cloud", "cloud"):
            self._model   = os.getenv("OLLAMA_CLOUD_MODEL", "gpt-oss:120b-cloud").strip()
            cloud_key     = os.getenv("OLLAMA_CLOUD_API_KEY", "").strip()
            if not cloud_key:
                logger.warning("Stock AI disabled — OLLAMA_CLOUD_API_KEY not set in stock_bot/.env")
                return
            self._ollama_client = OllamaClient(
                host    = "https://ollama.com",
                headers = {"Authorization": f"Bearer {cloud_key}"},
            )

        else:
            logger.warning(
                "Stock AI disabled — unknown AI_PROVIDER=%r "
                "(valid: openrouter | ollama_local | ollama_cloud | cloud)",
                self._provider,
            )
            return

        self._ready = True
        logger.info("Stock AIEngine ready | provider=%s model=%s", self._provider, self._model)

    @property
    def enabled(self) -> bool:
        return self._ready

    def analyze(
        self,
        symbol:     str,
        candle,
        indicators: dict,
        research:   ResearchReport,
    ) -> AIVerdict:
        """
        Analyze one symbol and return a verdict.
        Falls back to HOLD(confidence=0) on any failure.
        """
        if not self.enabled:
            return _hold_verdict(symbol, "AI unavailable — provider not configured")

        prompt = build_prompt(symbol, candle, indicators, research)
        t0     = time.monotonic()
        raw    = ""

        try:
            if self._provider in ("ollama_cloud", "cloud"):
                messages = [{"role": "user", "content": prompt}]
                for part in self._ollama_client.chat(
                    self._model,
                    messages = messages,
                    stream   = True,
                ):
                    raw += part["message"]["content"]
            else:
                payload = {
                    "model":       self._model,
                    "messages":    [{"role": "user", "content": prompt}],
                    "max_tokens":  _MAX_TOKENS,
                    "temperature": _TEMPERATURE,
                }
                resp = _requests.post(
                    self._base_url,
                    headers = self._headers,
                    json    = payload,
                    timeout = _TIMEOUT_S,
                )
                resp.raise_for_status()
                raw = resp.json()["choices"][0]["message"]["content"] or ""
        except Exception as exc:
            logger.warning("AI API call failed for %s [%s]: %s", symbol, self._provider, exc)
            return _hold_verdict(symbol, "AI unavailable")

        latency_ms = (time.monotonic() - t0) * 1000
        try:
            verdict        = _parse(raw, symbol)
            verdict.symbol = symbol
            logger.info(
                "AI verdict for %s: %s conf=%d style=%s latency=%.0fms [%s]",
                symbol, verdict.signal, verdict.confidence,
                verdict.trading_style, latency_ms, self._provider,
            )
            return verdict
        except Exception as exc:
            logger.warning("AI parse failed for %s (%s) | raw=%r", symbol, exc, raw[:120])
            return _hold_verdict(symbol, "AI parse error")
