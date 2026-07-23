"""
Hermetic test for the nvidia_nim AI client timeout (stock_bot/ai/ai_engine.py).

2026-07-23 root-cause finding: the nvidia_nim branch of AIEngine.analyze()
never passed timeout= when constructing the OpenAI client, so it silently
fell back to the SDK's own default — Timeout(read=600, write=600, pool=600),
30x longer than the code's own _TIMEOUT_S=20 intended for every provider.
This explains AI calls observed taking up to ~800s ("successful", just slow)
and is the most likely root cause of the swing-book thread hanging for
hours on 2026-07-22 with nothing ever raised. Fix: pass timeout=_TIMEOUT_S
to the client constructor. This test pins that it stays wired.

No network calls — the OpenAI client class is replaced with a fake that
just records its constructor kwargs.
"""
from types import SimpleNamespace
from unittest.mock import patch

from stock_bot.ai.ai_engine import AIEngine, _TIMEOUT_S


class _FakeMessage:
    content = '{"signal": "HOLD", "confidence": 0, "reasoning": "test"}'
    reasoning_content = None


class _FakeChoice:
    message = _FakeMessage()


class _FakeCompletion:
    choices = [_FakeChoice()]


class _FakeChatCompletions:
    def create(self, **kwargs):
        return _FakeCompletion()


class _EmptyChoicesCompletion:
    choices = None   # 2026-07-23 live incident: HOOD got this shape from nvidia_nim


class _FakeChatCompletionsEmptyChoices:
    def create(self, **kwargs):
        return _EmptyChoicesCompletion()


class _FakeChat:
    completions = _FakeChatCompletions()


class _FakeChatEmptyChoices:
    completions = _FakeChatCompletionsEmptyChoices()


class _FakeOpenAIClient:
    """Records the kwargs it was constructed with — the whole point of the test."""
    captured_kwargs: dict = {}

    def __init__(self, **kwargs):
        _FakeOpenAIClient.captured_kwargs = kwargs
        self.chat = _FakeChat()


class _FakeOpenAIClientEmptyChoices:
    def __init__(self, **kwargs):
        self.chat = _FakeChatEmptyChoices()


def _fake_research():
    return SimpleNamespace(
        news=[],
        earnings=SimpleNamespace(earnings_note=None, next_earnings_date=None),
        sentiment=SimpleNamespace(score=0.0, label="NEUTRAL", confidence=0.0, post_count=0),
        market_trends_score=None,
        fear_greed=SimpleNamespace(score=50, label="Unknown"),
    )


def test_nvidia_nim_client_receives_the_intended_timeout(monkeypatch):
    monkeypatch.setenv("AI_PROVIDER", "nvidia_nim")
    monkeypatch.setenv("NVIDIA_API_KEY", "fake-key-for-test")

    engine = AIEngine()
    assert engine.enabled
    engine._openai_cls = _FakeOpenAIClient

    candle = SimpleNamespace(close=100.0)
    with patch("stock_bot.ai.ai_engine.time.sleep"):   # skip the real 3s rate-limit delay
        engine.analyze(
            symbol="TEST", candle=candle, indicators={}, research=_fake_research(),
        )

    assert _FakeOpenAIClient.captured_kwargs.get("timeout") == _TIMEOUT_S


def test_nvidia_nim_empty_choices_falls_back_to_hold_without_crashing(monkeypatch):
    # 2026-07-23 live incident: "nvidia_nim FULL ERROR for HOOD: TypeError:
    # 'NoneType' object is not subscriptable" — completion.choices came back
    # None and got subscripted directly. Must degrade to a HOLD verdict, not
    # raise an opaque TypeError.
    monkeypatch.setenv("AI_PROVIDER", "nvidia_nim")
    monkeypatch.setenv("NVIDIA_API_KEY", "fake-key-for-test")

    engine = AIEngine()
    assert engine.enabled
    engine._openai_cls = _FakeOpenAIClientEmptyChoices

    candle = SimpleNamespace(close=100.0)
    with patch("stock_bot.ai.ai_engine.time.sleep"):
        verdict = engine.analyze(
            symbol="HOOD", candle=candle, indicators={}, research=_fake_research(),
        )

    assert verdict.signal == "HOLD"
    assert verdict.confidence == 0


if __name__ == "__main__":
    import os
    import sys

    os.environ["AI_PROVIDER"] = "nvidia_nim"
    os.environ["NVIDIA_API_KEY"] = "fake-key-for-test"
    try:
        engine = AIEngine()
        assert engine.enabled
        engine._openai_cls = _FakeOpenAIClient
        candle = SimpleNamespace(close=100.0)
        with patch("stock_bot.ai.ai_engine.time.sleep"):
            engine.analyze(symbol="TEST", candle=candle, indicators={}, research=_fake_research())
        assert _FakeOpenAIClient.captured_kwargs.get("timeout") == _TIMEOUT_S
        print("PASS test_nvidia_nim_client_receives_the_intended_timeout")
        sys.exit(0)
    except AssertionError as e:
        print(f"FAIL: {e}")
        sys.exit(1)
