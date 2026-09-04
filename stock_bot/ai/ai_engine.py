"""
AI analysis engine for the stock bot — multi-provider, advisory only.

Providers (set AI_PROVIDER in stock_bot/.env):
  nvidia_nim         — model from NVIDIA_MODEL, via NVIDIA NIM   (NVIDIA_API_KEY, stock_bot/.env)
  mistral            — model from MISTRAL_MODEL (default mistral-small-latest),
                       via api.mistral.ai's free "Experiment" tier  (MISTRAL_API_KEY, root .env)
  openrouter         — model from OPENROUTER_MODEL, via openrouter.ai  (OPENROUTER_API_KEY, root .env)
  ollama_local       — any Ollama model running locally   (OLLAMA_BASE_URL)
  ollama_cloud/cloud — any Ollama model via ollama.com     (OLLAMA_CLOUD_API_KEY)

Auto-failover (opt-in): set AI_FALLBACK_PROVIDER (mistral | openrouter | nvidia_nim)
in the .env. After _FALLBACK_AFTER consecutive API failures — or sustained parse
failures — the engine switches to that provider for the rest of the session and
fires (via main.py's per-cycle _update_ai_health) a Telegram alert — closes the
"manually swap the model on every nvidia_nim degradation" gap (4 of those so far,
see stock_bot/.env's NVIDIA_MODEL history). Off unless AI_FALLBACK_PROVIDER is set.
The primary and fallback must differ (fallback == primary is a no-op).

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

# ── Signal memory (per-session, not persisted to disk) ────────────────────────
_signal_memory: dict[str, list[dict]] = {}
_MEMORY_MAX = 3


def get_signal_memory(symbol: str) -> list[dict]:
    """Return up to the last 3 stored verdicts for symbol (oldest first)."""
    return list(_signal_memory.get(symbol.upper(), []))


def _store_verdict(symbol: str, verdict: AIVerdict) -> None:
    """Append a successfully parsed verdict to the session memory. No-op for error verdicts."""
    if verdict.provider in ("unavailable", "skipped"):
        return
    key = symbol.upper()
    entry = {
        "signal":            verdict.signal,
        "confidence":        verdict.confidence,
        "timestamp":         verdict.timestamp,
        "reasoning_summary": (verdict.reasoning or "")[:80],
    }
    history = _signal_memory.setdefault(key, [])
    history.append(entry)
    if len(history) > _MEMORY_MAX:
        _signal_memory[key] = history[-_MEMORY_MAX:]

# Load root .env for OPENROUTER_API_KEY / MISTRAL_API_KEY — kept separate from
# stock_bot/.env (NVIDIA_API_KEY lives there).
_ROOT_ENV = os.path.normpath(
    os.path.join(os.path.dirname(__file__), "..", "..", ".env")
)
load_dotenv(dotenv_path=_ROOT_ENV, override=False)

# OpenRouter default — may be stale (their free model roster changes; the
# 2026-era `...:free` slug 404'd). Override with OPENROUTER_MODEL in root .env.
_MODEL                 = "meta-llama/llama-3.3-70b-instruct:free"
_MISTRAL_MODEL_DEFAULT = "mistral-small-latest"
_MAX_TOKENS   = 2048
_TEMPERATURE  = 0.3
_TIMEOUT_S    = 20
_FALLBACK_AFTER = 5   # consecutive API failures before switching to AI_FALLBACK_PROVIDER


def _hold_verdict(symbol: str, reason: str, provider: str = "unavailable") -> AIVerdict:
    return AIVerdict(
        symbol        = symbol,
        signal        = "HOLD",
        confidence    = 0,
        target_price  = None,
        stop_loss     = None,
        reasoning     = reason,
        trading_style = "SWING",
        timestamp     = datetime.now(),
        provider      = provider,
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
        # Auto-failover state (see module docstring). Off unless the env var is set.
        self._primary_provider     = self._provider
        self._fallback_provider    = os.getenv("AI_FALLBACK_PROVIDER", "").strip().lower()
        self._fallback_active      = False
        self._consecutive_failures = 0
        self._last_call_failed     = False

        if self._provider == "openrouter":
            self._model   = os.getenv("OPENROUTER_MODEL", _MODEL).strip()
            api_key       = os.getenv("OPENROUTER_API_KEY", "").strip()
            if not api_key:
                logger.warning("Stock AI disabled — OPENROUTER_API_KEY not set in root .env")
                return
            self._base_url = "https://openrouter.ai/api/v1/chat/completions"
            self._headers  = {
                "Content-Type":  "application/json",
                "Authorization": f"Bearer {api_key}",
            }

        elif self._provider == "mistral":
            self._model   = os.getenv("MISTRAL_MODEL", _MISTRAL_MODEL_DEFAULT).strip()
            api_key       = os.getenv("MISTRAL_API_KEY", "").strip()
            if not api_key:
                logger.warning("Stock AI disabled — MISTRAL_API_KEY not set in root .env")
                return
            self._base_url = "https://api.mistral.ai/v1/chat/completions"
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

        elif self._provider == "nvidia_nim":
            api_key = os.getenv("NVIDIA_API_KEY", "").strip()
            if not api_key:
                raise ValueError(
                    "NVIDIA_API_KEY is empty in .env — "
                    "set it or change AI_PROVIDER"
                )
            try:
                from openai import OpenAI as _OpenAIClient
                self._openai_cls = _OpenAIClient
            except ImportError:
                logger.warning(
                    "Stock AI disabled — openai package required for nvidia_nim: "
                    "run: pip install openai"
                )
                return
            # Default is a placeholder — NVIDIA_MODEL is always set in stock_bot/.env
            # (its value has changed 4x as models hit end-of-life; see that file).
            self._model    = os.getenv("NVIDIA_MODEL", "nvidia/nemotron-3-nano-30b-a3b").strip()
            self._api_key  = api_key
            self._base_url = "https://integrate.api.nvidia.com/v1"
            print(f"  AI provider: nvidia_nim")
            print(f"  Model:       {self._model}")
            print(f"  Key:         {api_key[:8]}... (truncated)")

        else:
            logger.warning(
                "Stock AI disabled — unknown AI_PROVIDER=%r "
                "(valid: nvidia_nim | mistral | openrouter | ollama_local | ollama_cloud | cloud)",
                self._provider,
            )
            return

        self._ready = True
        # Snapshot the primary provider's routing config so a failover can be
        # REVERTED later (see _revert_to_primary). A transient primary outage
        # that trips the one-shot failover used to strand the engine on the
        # fallback for the rest of the session even after the primary recovered
        # — 2026-09-01: mistral 503'd for ~1h, failover went to a dead nvidia
        # model, AI stayed dark for hours after mistral was back.
        self._primary_route = {
            k: getattr(self, k)
            for k in ("_provider", "_model", "_base_url",
                      "_headers", "_api_key", "_openai_cls")
            if hasattr(self, k)
        }
        logger.info("Stock AIEngine ready | provider=%s model=%s", self._provider, self._model)

    @property
    def enabled(self) -> bool:
        return self._ready

    def _rate_limit_sleep(self) -> None:
        if self._provider == "openrouter":
            time.sleep(4)
        elif self._provider == "mistral":
            # Free "Experiment" tier is ~1 req/s. Calls are already sequential
            # per symbol; 2s keeps a full universe pass comfortably under that.
            time.sleep(2.0)
        elif self._provider == "nvidia_nim":
            # 2026-07-27: raised from 3.0s after mistral-nemotron hit repeated
            # RateLimitError 429s at 3s spacing (69 of 103 failures in one
            # session were 429s) — 40rpm nominal cap wasn't leaving enough
            # headroom in practice. 6s keeps a 26-symbol pass under ~7rpm.
            time.sleep(6.0)

    def _switch_to_fallback(self) -> bool:
        """Reconfigure the engine to AI_FALLBACK_PROVIDER for the rest of the
        session. One-way, once. Supported targets: mistral / openrouter (HTTP)
        and nvidia_nim (OpenAI SDK client). ollama_* are not switchable targets.
        Returns True iff the switch happened.
        """
        if self._fallback_active or not self._fallback_provider:
            return False
        if self._fallback_provider == self._primary_provider:
            logger.warning(
                "AI_FALLBACK_PROVIDER == AI_PROVIDER (%s) — no fallback to switch to",
                self._primary_provider,
            )
            return False
        fp = self._fallback_provider
        if fp == "mistral":
            key = os.getenv("MISTRAL_API_KEY", "").strip()
            if not key:
                logger.warning("AI failover unavailable — MISTRAL_API_KEY not set in root .env")
                return False
            self._model    = os.getenv("MISTRAL_MODEL", _MISTRAL_MODEL_DEFAULT).strip()
            self._base_url = "https://api.mistral.ai/v1/chat/completions"
        elif fp == "openrouter":
            key = os.getenv("OPENROUTER_API_KEY", "").strip()
            if not key:
                logger.warning("AI failover unavailable — OPENROUTER_API_KEY not set in root .env")
                return False
            self._model    = os.getenv("OPENROUTER_MODEL", _MODEL).strip()
            self._base_url = "https://openrouter.ai/api/v1/chat/completions"
        elif fp == "nvidia_nim":
            key = os.getenv("NVIDIA_API_KEY", "").strip()
            if not key:
                logger.warning("AI failover unavailable — NVIDIA_API_KEY not set in stock_bot/.env")
                return False
            try:
                from openai import OpenAI as _OpenAIClient
            except ImportError:
                logger.warning("AI failover unavailable — openai package required for nvidia_nim")
                return False
            self._openai_cls = _OpenAIClient
            self._api_key    = key
            self._model      = os.getenv("NVIDIA_MODEL", "deepseek-ai/deepseek-v4-pro-0813").strip()
            self._base_url   = "https://integrate.api.nvidia.com/v1"
            # _analyze_once's nvidia_nim branch uses the OpenAI client, not
            # self._headers — nothing else to set.
        else:
            logger.warning(
                "AI_FALLBACK_PROVIDER=%r not supported (mistral | openrouter | nvidia_nim)", fp,
            )
            return False
        if fp != "nvidia_nim":
            self._headers = {
                "Content-Type":  "application/json",
                "Authorization": f"Bearer {key}",
            }
        logger.error(
            "AI FAILOVER: %s failed %d consecutive calls — switching to %s (%s) "
            "for the rest of this session",
            self._primary_provider, self._consecutive_failures, fp, self._model,
        )
        self._provider        = fp
        self._fallback_active = True
        self._consecutive_failures = 0
        return True

    def _revert_to_primary(self) -> bool:
        """Undo an active failover — restore the primary provider's routing from
        the __init__ snapshot. Called when the FALLBACK itself racks up
        _FALLBACK_AFTER consecutive failures: at that point the primary's
        earlier (often transient) outage may well have cleared, and staying on a
        dead fallback helps nobody. Resets _fallback_active so a later failover
        can fire again — worst case (both providers down) the engine ping-pongs,
        which is harmless for an advisory-only signal. Returns True iff reverted.
        """
        if not self._fallback_active or not getattr(self, "_primary_route", None):
            return False
        for k, v in self._primary_route.items():
            setattr(self, k, v)
        logger.error(
            "AI FAILBACK: fallback %s failed %d consecutive calls — reverting to "
            "primary %s (%s); its earlier outage may have cleared",
            self._fallback_provider, self._consecutive_failures,
            self._primary_provider, self._model,
        )
        self._fallback_active      = False
        self._consecutive_failures = 0
        return True

    def analyze(
        self,
        symbol:          str,
        candle,
        indicators:      dict,
        research:        ResearchReport,
        stop_loss_pct:   float = 0.05,
        take_profit_pct: float = 0.12,
    ) -> AIVerdict:
        """Analyze one symbol → AIVerdict. Never raises. Triggers the one-shot
        failover to AI_FALLBACK_PROVIDER after _FALLBACK_AFTER consecutive API
        failures (not parse errors — those are a prompt/model issue a failover
        won't fix)."""
        if not self.enabled:
            return _hold_verdict(symbol, "AI unavailable — provider not configured")

        verdict = self._analyze_once(
            symbol, candle, indicators, research, stop_loss_pct, take_profit_pct,
        )
        # A model that returns unparseable garbage every call (2026-08-27:
        # nemotron rambled its reasoning and truncated before the JSON, 75%
        # failure) is functionally as dead as one that's down — both are fixed
        # by switching providers. So sustained parse failures trigger the
        # failover too, not just API errors.
        if not (self._last_call_failed or self._last_call_parse_failed):
            self._consecutive_failures = 0
            return verdict

        self._consecutive_failures += 1
        if self._consecutive_failures >= _FALLBACK_AFTER:
            # Not yet failed over → switch to the fallback.
            # Already failed over and the fallback is failing too → revert to
            # the primary (its outage may have cleared). Either way, retry this
            # same symbol once on the newly-selected provider.
            switched = (self._switch_to_fallback() if not self._fallback_active
                        else self._revert_to_primary())
            if switched:
                return self._analyze_once(
                    symbol, candle, indicators, research, stop_loss_pct, take_profit_pct,
                )
        return verdict

    def _analyze_once(
        self,
        symbol:          str,
        candle,
        indicators:      dict,
        research:        ResearchReport,
        stop_loss_pct:   float = 0.05,
        take_profit_pct: float = 0.12,
    ) -> AIVerdict:
        """
        Analyze one symbol and return a verdict.
        Falls back to HOLD(confidence=0) on any failure.
        """
        if not self.enabled:
            return _hold_verdict(symbol, "AI unavailable — provider not configured")

        self._last_call_failed       = False   # True on an API/transport error
        self._last_call_parse_failed = False   # True when a response came back but _parse raised

        prompt = build_prompt(symbol, candle, indicators, research,
                              stop_loss_pct=stop_loss_pct, take_profit_pct=take_profit_pct,
                              previous_signals=get_signal_memory(symbol))
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
            elif self._provider == "nvidia_nim":
                # 2026-07-23: this branch never passed timeout= — the OpenAI SDK
                # silently defaulted to Timeout(read=600, write=600, pool=600),
                # 30x the intended _TIMEOUT_S=20, plus up to 2 SDK-level retries
                # on top. Root cause of AI calls observed taking up to ~800s
                # ("successful", just slow) and of the swing-book thread hang
                # (2026-07-22) that could freeze for hours with nothing raised.
                client = self._openai_cls(
                    base_url    = self._base_url,
                    api_key     = self._api_key,
                    timeout     = _TIMEOUT_S,
                    # 2026-07-27: SDK default retries (2) fire immediately on
                    # 429/500 with their own short backoff, bypassing our
                    # _rate_limit_sleep pacing entirely — effectively bursting
                    # 3 requests where we intended 1. Disabled so every retry
                    # decision goes through our own spacing.
                    max_retries = 0,
                )
                completion = client.chat.completions.create(
                    model       = self._model,
                    messages    = [{"role": "user", "content": prompt}],
                    temperature = _TEMPERATURE,
                    top_p       = 1,
                    # 2026-08-27: was 1024 — a reasoning model (nemotron) spends
                    # that on chain-of-thought in the response body and truncates
                    # before the JSON. 4096 gives it room. (nemotron is off as of
                    # this date — mistral is primary — but keep the headroom for
                    # any future nvidia model used as a fallback.)
                    max_tokens  = 4096,
                    stream      = False,
                    # 2026-09-03: nemotron-3.5-lightning (the only NVIDIA model
                    # still deployed on this account) defaults thinking ON — the
                    # real scan prompt then burns 30s+ of reasoning tokens and
                    # blows past _TIMEOUT_S=20 on every call. Disabling thinking
                    # drops it to ~1.7s and the model emits the JSON verdict
                    # directly. NVIDIA NIM ignores this key for models that don't
                    # support it, so it is safe for any future swap.
                    extra_body  = {"chat_template_kwargs": {"enable_thinking": False}},
                )
                # 2026-07-23: completion.choices can come back None/empty (content
                # filter, empty generation, provider-side hiccup) — subscripting it
                # directly raised an opaque "TypeError: 'NoneType' object is not
                # subscriptable" (seen live on HOOD). Same safe HOLD-fallback
                # outcome either way, but this makes the cause diagnosable.
                if not completion.choices:
                    logger.warning(
                        "nvidia_nim returned no choices for %s (empty generation "
                        "or content filter) — treating as unavailable", symbol,
                    )
                    self._last_call_failed = True
                    return _hold_verdict(symbol, "AI returned empty response")
                # Extract reasoning if present (ignore it)
                reasoning = getattr(
                    completion.choices[0].message,
                    "reasoning_content",
                    None,
                )
                # Only use the actual content for parsing
                response_text = completion.choices[0].message.content
                latency_ms = (time.monotonic() - t0) * 1000
                self._rate_limit_sleep()
                try:
                    verdict          = _parse(response_text, symbol)
                    verdict.symbol   = symbol
                    verdict.provider = "nvidia_nim"
                    logger.info(
                        "AI verdict for %s: %s conf=%d style=%s latency=%.0fms [nvidia_nim]",
                        symbol, verdict.signal, verdict.confidence,
                        verdict.trading_style, latency_ms,
                    )
                    _store_verdict(symbol, verdict)
                    return verdict
                except Exception as parse_exc:
                    logger.warning("AI parse failed for %s (%s) | raw=%r", symbol, parse_exc, response_text[:120])
                    self._last_call_parse_failed = True
                    return _hold_verdict(symbol, "AI parse error")
            else:  # openrouter | ollama_local | mistral
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
            self._last_call_failed = True
            if self._provider == "nvidia_nim":
                logger.warning(
                    "nvidia_nim FULL ERROR for %s: %s: %s",
                    symbol, type(exc).__name__, str(exc),
                )
                # 2026-07-27: this branch used to return with no sleep at all —
                # a failure (often itself a 429) removed the pacing gap instead
                # of widening it, so the very next symbol's call fired
                # immediately into the same rate limit. Root cause of observed
                # back-to-back RateLimitError bursts (CSCO/T/WMT within 15s).
                self._rate_limit_sleep()
                return _hold_verdict(symbol, "AI unavailable")
            else:
                logger.warning("AI API call failed for %s [%s]: %s", symbol, self._provider, exc)
                self._rate_limit_sleep()
                return _hold_verdict(symbol, "AI unavailable")

        latency_ms = (time.monotonic() - t0) * 1000
        self._rate_limit_sleep()
        try:
            verdict          = _parse(raw, symbol)
            verdict.symbol   = symbol
            verdict.provider = self._provider
            logger.info(
                "AI verdict for %s: %s conf=%d style=%s latency=%.0fms [%s]",
                symbol, verdict.signal, verdict.confidence,
                verdict.trading_style, latency_ms, self._provider,
            )
            _store_verdict(symbol, verdict)
            return verdict
        except Exception as exc:
            logger.warning("AI parse failed for %s (%s) | raw=%r", symbol, exc, raw[:120])
            self._last_call_parse_failed = True
            return _hold_verdict(symbol, "AI parse error")
