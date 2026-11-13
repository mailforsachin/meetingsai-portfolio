"""Regression tests for the AIAnalysisProvider refactor.

These tests exercise the REAL call_model() implementation through a mocked
OpenAI SDK boundary.  No live API calls.  They cover:
  - SDK argument construction
  - Response content + usage extraction
  - Transport failure
  - Malformed response (missing choices attribute, empty choices list)
  - None content -> "" (preserved behavior: AISchemaError at run())
  - run() AnalysisResult contract regression
  - run() AISchemaError on bad JSON
"""
from __future__ import annotations

import json

import pytest

from app.ai.provider import AIAnalysisProvider, AIProviderError, RawModelCall
from app.ai.prompt_loader import Prompt
from app.ai.output_schema import AISchemaError


def _prompt(body="sys", name="test.md", version="0.0.1") -> Prompt:
    return Prompt(name=name, version=version, kind="test", body=body)


class _FakeUsage:
    def __init__(self, pt, ct):
        self.prompt_tokens = pt
        self.completion_tokens = ct


class _FakeMessage:
    def __init__(self, content):
        self.content = content


class _FakeChoice:
    def __init__(self, content):
        self.message = _FakeMessage(content)


class _FakeResponse:
    def __init__(self, content, pt=11, ct=22):
        self.choices = [_FakeChoice(content)]
        self.usage = _FakeUsage(pt, ct)


class _EmptyChoicesResponse:
    def __init__(self):
        self.choices = []
        self.usage = _FakeUsage(0, 0)


class _NoChoicesAttrResponse:
    def __init__(self):
        self.usage = _FakeUsage(0, 0)


class _NoneContentResponse:
    def __init__(self):
        self.choices = [_FakeChoice(None)]
        self.usage = _FakeUsage(1, 1)


class _FakeCompletions:
    def __init__(self, response=None, raise_exc=None, capture=None):
        self._response = response
        self._raise = raise_exc
        self._capture = capture if capture is not None else {}

    def create(self, **kwargs):
        self._capture.update(kwargs)
        if self._raise is not None:
            raise self._raise
        return self._response


class _FakeClient:
    def __init__(self, completions):
        self.chat = type("_FakeChat", (), {"completions": completions})()


def _install(monkeypatch, completions):
    from app.ai import provider as provider_mod
    monkeypatch.setattr(
        provider_mod, "OpenAI",
        lambda api_key: _FakeClient(completions),
        raising=True,
    )


def test_call_model_constructs_sdk_arguments_correctly(monkeypatch):
    capture = {}
    fake = _FakeCompletions(response=_FakeResponse('{"x":1}'), capture=capture)
    _install(monkeypatch, fake)
    p = _prompt(body="SYS BODY")
    provider = AIAnalysisProvider(api_key="k", model="gpt-4o-mini", timeout_seconds=42)
    provider.call_model(p, {"a": 1})
    assert capture["model"] == "gpt-4o-mini"
    assert capture["response_format"] == {"type": "json_object"}
    assert capture["temperature"] == 0.2
    assert capture["timeout"] == 42
    msgs = capture["messages"]
    assert msgs[0]["role"] == "system"
    assert msgs[0]["content"] == "SYS BODY"
    assert msgs[1]["role"] == "user"
    assert json.loads(msgs[1]["content"]) == {"a": 1}


def test_call_model_extracts_content_and_usage(monkeypatch):
    fake = _FakeCompletions(response=_FakeResponse('{"x":1}', pt=11, ct=22))
    _install(monkeypatch, fake)
    provider = AIAnalysisProvider(api_key="k", model="gpt-4o-mini")
    call = provider.call_model(_prompt(), {"a": 1})
    assert isinstance(call, RawModelCall)
    assert call.content == '{"x":1}'
    assert call.model == "gpt-4o-mini"
    assert call.input_tokens == 11
    assert call.output_tokens == 22
    assert call.latency_ms >= 0


def test_call_model_raises_on_transport_failure(monkeypatch):
    fake = _FakeCompletions(raise_exc=RuntimeError("boom"))
    _install(monkeypatch, fake)
    provider = AIAnalysisProvider(api_key="k", model="gpt-4o-mini")
    with pytest.raises(AIProviderError) as ei:
        provider.call_model(_prompt(), {})
    assert str(ei.value).startswith("model call failed:")


def test_call_model_raises_on_missing_choices_attribute(monkeypatch):
    fake = _FakeCompletions(response=_NoChoicesAttrResponse())
    _install(monkeypatch, fake)
    provider = AIAnalysisProvider(api_key="k", model="gpt-4o-mini")
    with pytest.raises(AIProviderError) as ei:
        provider.call_model(_prompt(), {})
    assert str(ei.value).startswith("model response malformed:")


def test_call_model_raises_on_empty_choices_list(monkeypatch):
    fake = _FakeCompletions(response=_EmptyChoicesResponse())
    _install(monkeypatch, fake)
    provider = AIAnalysisProvider(api_key="k", model="gpt-4o-mini")
    with pytest.raises(AIProviderError) as ei:
        provider.call_model(_prompt(), {})
    assert str(ei.value).startswith("model response malformed:")


def test_call_model_empty_string_on_none_content(monkeypatch):
    """content=None is coerced to "" by `or ""` -- preserved behavior."""
    fake = _FakeCompletions(response=_NoneContentResponse())
    _install(monkeypatch, fake)
    provider = AIAnalysisProvider(api_key="k", model="gpt-4o-mini")
    call = provider.call_model(_prompt(), {})
    assert call.content == ""


def test_run_raises_schema_error_on_none_content(monkeypatch):
    """The None -> "" path surfaces as AISchemaError from run(), not AIProviderError."""
    fake = _FakeCompletions(response=_NoneContentResponse())
    _install(monkeypatch, fake)
    provider = AIAnalysisProvider(api_key="k", model="gpt-4o-mini")
    with pytest.raises(AISchemaError):
        provider.run(_prompt(), {})


def test_run_preserves_analysis_result_shape(monkeypatch):
    payload = {
        "understanding": "u",
        "key_discussion": ["k1"],
        "potential_response": None,
        "facts": [],
        "decisions": [],
        "action_items": [],
        "risks": [],
        "questions_now": [],
        "questions_later": [],
        "missing_information": [],
        "contradictions": [],
        "next_steps": [],
    }
    fake = _FakeCompletions(response=_FakeResponse(json.dumps(payload), pt=11, ct=22))
    _install(monkeypatch, fake)
    prompt = _prompt(version="9.9.9")
    provider = AIAnalysisProvider(api_key="k", model="gpt-4o-mini")
    result = provider.run(prompt, {"a": 1})
    assert result.raw_content == json.dumps(payload)
    assert result.model == "gpt-4o-mini"
    assert result.prompt_version == "9.9.9"
    assert result.input_tokens == 11
    assert result.output_tokens == 22
    assert result.latency_ms >= 0
    assert result.output.understanding == "u"
    assert result.output.key_discussion == ["k1"]
    assert json.loads(result.output_json)["understanding"] == "u"


def test_run_raises_ai_schema_error_on_bad_json(monkeypatch):
    fake = _FakeCompletions(response=_FakeResponse("{not json"))
    _install(monkeypatch, fake)
    provider = AIAnalysisProvider(api_key="k", model="gpt-4o-mini")
    with pytest.raises(AISchemaError):
        provider.run(_prompt(), {})
