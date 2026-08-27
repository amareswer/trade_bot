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
