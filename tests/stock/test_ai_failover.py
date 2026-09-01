"""Mistral provider + auto-failover — stock_bot/ai/ai_engine.py (added 2026-08-27).

Closes the "manually swap NVIDIA_MODEL on every nvidia_nim degradation" gap
(4 of those so far). nvidia_nim stays primary; after _FALLBACK_AFTER consecutive
API failures the engine switches to AI_FALLBACK_PROVIDER (mistral) for the rest
of the session. No network — _requests.post and the OpenAI client are faked.
"""
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from stock_bot.ai.ai_engine import AIEngine, _FALLBACK_AFTER


def _fake_research():
    return SimpleNamespace(
        news=[],
        earnings=SimpleNamespace(earnings_note=None, next_earnings_date=None),
        sentiment=SimpleNamespace(score=0.0, label="NEUTRAL", confidence=0.0, post_count=0),
        market_trends_score=None,
        fear_greed=SimpleNamespace(score=50, label="Unknown"),
    )


def _analyze(engine, symbol="TEST"):
    with patch("stock_bot.ai.ai_engine.time.sleep"):
        return engine.analyze(
            symbol=symbol, candle=SimpleNamespace(close=100.0),
            indicators={}, research=_fake_research(),
        )


class _OKPost:
    """Fake requests.post → a valid JSON verdict."""
    status_code = 200
    def raise_for_status(self): pass
    def json(self):
        return {"choices": [{"message": {
            "content": '{"signal": "BUY", "confidence": 72, "reasoning": "ok"}'}}]}


class _BoomOpenAI:
    def __init__(self, **kw): self.chat = self
    @property
    def completions(self): return self
    def create(self, **kw): raise RuntimeError("nvidia_nim down (410 Gone)")


class _OKOpenAI:
    """Fake OpenAI client → a valid JSON verdict (nvidia_nim path)."""
    def __init__(self, **kw): self.chat = self
    @property
    def completions(self): return self
    def create(self, **kw):
        msg = SimpleNamespace(
            content='{"signal": "HOLD", "confidence": 55, "reasoning": "ok"}',
            reasoning_content=None,
        )
        return SimpleNamespace(choices=[SimpleNamespace(message=msg)])


# ── Mistral as a standalone provider ────────────────────────────────────────

def test_mistral_provider_configures(monkeypatch):
    monkeypatch.setenv("AI_PROVIDER", "mistral")
    monkeypatch.setenv("MISTRAL_API_KEY", "test-key")
    monkeypatch.setenv("MISTRAL_MODEL", "ministral-8b-latest")
    e = AIEngine()
    assert e.enabled
    assert e._base_url == "https://api.mistral.ai/v1/chat/completions"
    assert e._model == "ministral-8b-latest"
    assert e._headers["Authorization"] == "Bearer test-key"


def test_mistral_provider_disabled_without_key(monkeypatch):
    monkeypatch.setenv("AI_PROVIDER", "mistral")
    monkeypatch.delenv("MISTRAL_API_KEY", raising=False)
    assert AIEngine().enabled is False


def test_mistral_call_produces_a_verdict(monkeypatch):
    monkeypatch.setenv("AI_PROVIDER", "mistral")
    monkeypatch.setenv("MISTRAL_API_KEY", "test-key")
    e = AIEngine()
    with patch("stock_bot.ai.ai_engine._requests.post", return_value=_OKPost()):
        v = _analyze(e)
    assert v.signal == "BUY" and v.confidence == 72
    assert v.provider == "mistral"


# ── Auto-failover ──────────────────────────────────────────────────────────

def test_failover_switches_after_threshold(monkeypatch):
    monkeypatch.setenv("AI_PROVIDER", "nvidia_nim")
    monkeypatch.setenv("NVIDIA_API_KEY", "test-key")
    monkeypatch.setenv("AI_FALLBACK_PROVIDER", "mistral")
    monkeypatch.setenv("MISTRAL_API_KEY", "test-key")
    e = AIEngine()
    e._openai_cls = _BoomOpenAI

    with patch("stock_bot.ai.ai_engine._requests.post", return_value=_OKPost()):
        # First _FALLBACK_AFTER-1 failures: still nvidia, no switch
        for _ in range(_FALLBACK_AFTER - 1):
            v = _analyze(e)
            assert v.provider == "unavailable"
        assert e._fallback_active is False
        # The _FALLBACK_AFTER-th failure trips the switch and the retry succeeds on mistral
        v = _analyze(e)
        assert e._fallback_active is True
        assert e._provider == "mistral"
        assert v.provider == "mistral" and v.signal == "BUY"
        # Subsequent calls stay on mistral
        assert _analyze(e).provider == "mistral"


def test_no_failover_without_env_var(monkeypatch):
    monkeypatch.setenv("AI_PROVIDER", "nvidia_nim")
    monkeypatch.setenv("NVIDIA_API_KEY", "test-key")
    monkeypatch.delenv("AI_FALLBACK_PROVIDER", raising=False)
    e = AIEngine()
    e._openai_cls = _BoomOpenAI
    for _ in range(_FALLBACK_AFTER + 3):
        assert _analyze(e).provider == "unavailable"
    assert e._fallback_active is False


def test_no_failover_when_fallback_key_missing(monkeypatch):
    monkeypatch.setenv("AI_PROVIDER", "nvidia_nim")
    monkeypatch.setenv("NVIDIA_API_KEY", "test-key")
    monkeypatch.setenv("AI_FALLBACK_PROVIDER", "mistral")
    monkeypatch.delenv("MISTRAL_API_KEY", raising=False)
    e = AIEngine()
    e._openai_cls = _BoomOpenAI
    for _ in range(_FALLBACK_AFTER + 2):
        assert _analyze(e).provider == "unavailable"
    assert e._fallback_active is False


def test_switch_to_fallback_is_one_way(monkeypatch):
    monkeypatch.setenv("AI_PROVIDER", "nvidia_nim")
    monkeypatch.setenv("NVIDIA_API_KEY", "test-key")
    monkeypatch.setenv("AI_FALLBACK_PROVIDER", "mistral")
    monkeypatch.setenv("MISTRAL_API_KEY", "test-key")
    e = AIEngine()
    e._consecutive_failures = _FALLBACK_AFTER
    assert e._switch_to_fallback() is True
    assert e._switch_to_fallback() is False   # already switched


# ── nvidia_nim as a fallback TARGET (2026-08-27 — mistral primary, deepseek-v4-pro fallback) ──

def test_failover_to_nvidia_nim_switches_client(monkeypatch):
    monkeypatch.setenv("AI_PROVIDER", "mistral")
    monkeypatch.setenv("MISTRAL_API_KEY", "test-key")
    monkeypatch.setenv("AI_FALLBACK_PROVIDER", "nvidia_nim")
    monkeypatch.setenv("NVIDIA_API_KEY", "test-key")
    monkeypatch.setenv("NVIDIA_MODEL", "deepseek-ai/deepseek-v4-pro-0813")
    e = AIEngine()
    assert e._provider == "mistral"
    e._consecutive_failures = _FALLBACK_AFTER
    with patch("openai.OpenAI", _OKOpenAI):
        assert e._switch_to_fallback() is True
    assert e._provider == "nvidia_nim"
    assert e._model == "deepseek-ai/deepseek-v4-pro-0813"
    assert e._base_url == "https://integrate.api.nvidia.com/v1"
    assert e._openai_cls is _OKOpenAI       # OpenAI-SDK client, not the HTTP path


def test_failover_to_nvidia_nim_produces_a_verdict(monkeypatch):
    monkeypatch.setenv("AI_PROVIDER", "mistral")
    monkeypatch.setenv("MISTRAL_API_KEY", "test-key")
    monkeypatch.setenv("AI_FALLBACK_PROVIDER", "nvidia_nim")
    monkeypatch.setenv("NVIDIA_API_KEY", "test-key")
    monkeypatch.setenv("NVIDIA_MODEL", "deepseek-ai/deepseek-v4-pro-0813")
    e = AIEngine()

    # primary mistral fails every call → after threshold, switch + retry on nvidia
    boom = SimpleNamespace(raise_for_status=lambda: (_ for _ in ()).throw(RuntimeError("mistral 503")))
    with patch("stock_bot.ai.ai_engine._requests.post", return_value=boom), \
         patch("openai.OpenAI", _OKOpenAI):
        for _ in range(_FALLBACK_AFTER + 1):
            v = _analyze(e)
    assert e._fallback_active is True
    assert e._provider == "nvidia_nim"
    assert v.provider == "nvidia_nim" and v.signal == "HOLD" and v.confidence == 55


def test_failover_to_nvidia_nim_needs_the_key(monkeypatch):
    monkeypatch.setenv("AI_PROVIDER", "mistral")
    monkeypatch.setenv("MISTRAL_API_KEY", "test-key")
    monkeypatch.setenv("AI_FALLBACK_PROVIDER", "nvidia_nim")
    monkeypatch.delenv("NVIDIA_API_KEY", raising=False)
    e = AIEngine()
    e._consecutive_failures = _FALLBACK_AFTER
    assert e._switch_to_fallback() is False
    assert e._fallback_active is False


def test_no_failover_when_fallback_equals_primary(monkeypatch):
    monkeypatch.setenv("AI_PROVIDER", "mistral")
    monkeypatch.setenv("MISTRAL_API_KEY", "test-key")
    monkeypatch.setenv("AI_FALLBACK_PROVIDER", "mistral")
    e = AIEngine()
    e._consecutive_failures = _FALLBACK_AFTER
    assert e._switch_to_fallback() is False   # fallback == primary is a no-op
    assert e._fallback_active is False


def test_sustained_parse_failures_trigger_failover(monkeypatch):
    """2026-08-27: nemotron returned unparseable reasoning-text ~75% of calls.
    A model that answers with garbage every time is as dead as one that's down —
    both are fixed by switching providers, so sustained parse failures now
    trigger the failover too (they used to be exempt)."""
    monkeypatch.setenv("AI_PROVIDER", "mistral")
    monkeypatch.setenv("MISTRAL_API_KEY", "test-key")
    monkeypatch.setenv("AI_FALLBACK_PROVIDER", "openrouter")
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    e = AIEngine()

    class _Garbage:
        status_code = 200
        def raise_for_status(self): pass
        def json(self):
            return {"choices": [{"message": {"content": "not json at all — just prose"}}]}

    _ok = type("_OK", (), {
        "status_code": 200, "raise_for_status": lambda s: None,
        "json": lambda s: {"choices": [{"message": {"content":
            '{"signal": "HOLD", "confidence": 40, "reasoning": "x"}'}}]},
    })()

    posts = [_Garbage()] * _FALLBACK_AFTER + [_ok]
    with patch("stock_bot.ai.ai_engine._requests.post", side_effect=posts):
        for _ in range(_FALLBACK_AFTER - 1):
            _analyze(e)
        assert e._fallback_active is False
        v = _analyze(e)   # the _FALLBACK_AFTER-th parse failure trips the switch + retry
    assert e._fallback_active is True and e._provider == "openrouter"
    assert v.provider == "openrouter" and v.signal == "HOLD"


# ── failback: a dead fallback reverts to the (recovered) primary ────────────
# 2026-09-01: mistral 503'd for ~1h → one-shot failover to a dead nvidia model
# → AI stayed dark for hours after mistral recovered, because the failover never
# reverted. _revert_to_primary() fixes that.

def _boom_post():
    return SimpleNamespace(
        raise_for_status=lambda: (_ for _ in ()).throw(RuntimeError("mistral 503")),
    )


def test_fallback_failure_reverts_to_recovered_primary(monkeypatch):
    monkeypatch.setenv("AI_PROVIDER", "mistral")
    monkeypatch.setenv("MISTRAL_API_KEY", "test-key")
    monkeypatch.setenv("AI_FALLBACK_PROVIDER", "nvidia_nim")
    monkeypatch.setenv("NVIDIA_API_KEY", "test-key")
    monkeypatch.setenv("NVIDIA_MODEL", "deepseek-ai/deepseek-v4-pro-0813")
    e = AIEngine()

    # mistral down for the first 5 posts, then recovered; nvidia fallback dead throughout
    posts = [_boom_post()] * _FALLBACK_AFTER + [_OKPost()]
    with patch("stock_bot.ai.ai_engine._requests.post", side_effect=posts), \
         patch("openai.OpenAI", _BoomOpenAI):
        for _ in range(_FALLBACK_AFTER):        # trip the failover to nvidia
            _analyze(e)
        assert e._fallback_active is True and e._provider == "nvidia_nim"
        for _ in range(_FALLBACK_AFTER):        # nvidia fails too → revert to mistral + retry
            v = _analyze(e)

    assert e._fallback_active is False
    assert e._provider == "mistral"
    assert v.provider == "mistral" and v.signal == "BUY"


def test_revert_lets_the_failover_fire_again(monkeypatch):
    monkeypatch.setenv("AI_PROVIDER", "mistral")
    monkeypatch.setenv("MISTRAL_API_KEY", "test-key")
    monkeypatch.setenv("AI_FALLBACK_PROVIDER", "nvidia_nim")
    monkeypatch.setenv("NVIDIA_API_KEY", "test-key")
    e = AIEngine()
    e._openai_cls = _BoomOpenAI

    with patch("stock_bot.ai.ai_engine._requests.post", return_value=_boom_post()), \
         patch("openai.OpenAI", _BoomOpenAI):
        for _ in range(_FALLBACK_AFTER):
            _analyze(e)
        assert e._fallback_active is True                    # → nvidia
        for _ in range(_FALLBACK_AFTER):
            _analyze(e)
        assert e._fallback_active is False                   # reverted → mistral
        for _ in range(_FALLBACK_AFTER):
            _analyze(e)
        assert e._fallback_active is True                    # failed over again


def test_revert_is_noop_when_not_failed_over(monkeypatch):
    monkeypatch.setenv("AI_PROVIDER", "mistral")
    monkeypatch.setenv("MISTRAL_API_KEY", "test-key")
    monkeypatch.setenv("AI_FALLBACK_PROVIDER", "nvidia_nim")
    monkeypatch.setenv("NVIDIA_API_KEY", "test-key")
    e = AIEngine()
    assert e._revert_to_primary() is False
    assert e._provider == "mistral"


def test_one_off_parse_failure_does_not_trigger_failover(monkeypatch):
    monkeypatch.setenv("AI_PROVIDER", "mistral")
    monkeypatch.setenv("MISTRAL_API_KEY", "test-key")
    monkeypatch.setenv("AI_FALLBACK_PROVIDER", "openrouter")
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    e = AIEngine()

    def _mk(content):
        return type("_R", (), {
            "status_code": 200, "raise_for_status": lambda s: None,
            "json": lambda s, c=content: {"choices": [{"message": {"content": c}}]},
        })()

    seq = [_mk("garbage")] + [_mk('{"signal":"HOLD","confidence":30,"reasoning":"x"}')] * 4
    with patch("stock_bot.ai.ai_engine._requests.post", side_effect=seq):
        for _ in range(5):
            _analyze(e)
    assert e._fallback_active is False   # a clean parse resets the counter
