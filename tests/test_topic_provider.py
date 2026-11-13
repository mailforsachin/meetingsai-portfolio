"""Tests for the Topic Intelligence provider, parser, and provenance safety.

All tests mock the prompt loader and the SDK boundary.  No live API calls.
No dependency on the T5 prompt file existing.

T7 DEPENDENCY (documented, not tested here):
  ResearchResult.raw_content must never be exposed in API responses.
  T7c must add a test proving no response schema serializes raw_content.
"""
from __future__ import annotations

import json
from dataclasses import FrozenInstanceError

import pytest

from app.ai.topic_intelligence.provider import (
    ModelOnlyProvider,
    ResearchResult,
    ResearchSource,
    TopicIntelligenceProvider,
)
from app.ai.topic_intelligence.parsing import (
    TopicSchemaError,
    parse_and_validate_topic,
)
from app.schemas.topic_ai_output import TopicBriefOutput


def _fake_prompt():
    from app.ai.prompt_loader import Prompt
    return Prompt(name="topic_brief.md", version="0.1.0", kind="topic_brief", body="sys")


class _FakeCall:
    def __init__(self, content, model="gpt-4o-mini", pt=10, ot=20):
        from app.ai.provider import RawModelCall
        self._r = RawModelCall(
            content=content, model=model,
            input_tokens=pt, output_tokens=ot, latency_ms=7,
        )

    def __getattr__(self, name):
        return getattr(self._r, name)


def _install(monkeypatch, content):
    from app.ai.topic_intelligence import provider as prov_mod
    monkeypatch.setattr(prov_mod, "load_prompt", lambda name: _fake_prompt())
    monkeypatch.setattr(
        "app.ai.provider.AIAnalysisProvider.call_model",
        lambda self, prompt, payload: _FakeCall(content),
        raising=True,
    )


def _install_raw(monkeypatch, raw_call):
    from app.ai.topic_intelligence import provider as prov_mod
    monkeypatch.setattr(prov_mod, "load_prompt", lambda name: _fake_prompt())
    monkeypatch.setattr(
        "app.ai.provider.AIAnalysisProvider.call_model",
        lambda self, prompt, payload: raw_call,
        raising=True,
    )


def test_provider_is_abstract():
    with pytest.raises(TypeError):
        TopicIntelligenceProvider()


def test_missing_api_key_raises():
    from app.ai.provider import AIProviderError
    with pytest.raises(AIProviderError):
        ModelOnlyProvider(api_key="", model="gpt-4o-mini")


def test_model_only_provider_returns_model_only_grounding(monkeypatch):
    body = {"summary": "brief", "sources": [], "notes": None}
    _install(monkeypatch, json.dumps(body))
    r = ModelOnlyProvider(api_key="k", model="gpt-4o-mini").research("X", "ctx")
    assert isinstance(r, ResearchResult)
    assert r.grounding_status == "model_only"
    assert r.provider == "model_only"
    assert r.summary == "brief"
    assert r.sources == []
    assert r.tool_calls is None
    assert r.model == "gpt-4o-mini"
    assert r.latency_ms == 7
    assert json.loads(r.brief_json)["summary"] == "brief"


def test_model_only_provider_stores_sources_but_keeps_model_only(monkeypatch):
    body = {
        "summary": "s",
        "sources": [
            {"url": "https://a.example", "title": "A", "snippet": "sa"},
            {"url": "https://b.example", "title": "B", "snippet": "sb"},
        ],
        "notes": None,
    }
    _install(monkeypatch, json.dumps(body))
    r = ModelOnlyProvider(api_key="k", model="gpt-4o-mini").research("X", "ctx")
    assert len(r.sources) == 2
    assert all(isinstance(s, ResearchSource) for s in r.sources)
    assert all(s.citation_index is None for s in r.sources)
    assert r.grounding_status == "model_only"


def test_tokens_used_sums_input_and_output(monkeypatch):
    body = {"summary": "s", "sources": [], "notes": None}
    _install(monkeypatch, json.dumps(body))
    r = ModelOnlyProvider(api_key="k", model="gpt-4o-mini").research("X", "ctx")
    assert r.tokens_used == 30


def test_tokens_used_none_when_input_missing(monkeypatch):
    from app.ai.provider import RawModelCall
    _install_raw(
        monkeypatch,
        RawModelCall(
            content=json.dumps({"summary": "s", "sources": [], "notes": None}),
            model="m", input_tokens=None, output_tokens=20, latency_ms=1,
        ),
    )
    r = ModelOnlyProvider(api_key="k", model="gpt-4o-mini").research("X", "ctx")
    assert r.tokens_used is None


def test_tokens_used_none_when_output_missing(monkeypatch):
    from app.ai.provider import RawModelCall
    _install_raw(
        monkeypatch,
        RawModelCall(
            content=json.dumps({"summary": "s", "sources": [], "notes": None}),
            model="m", input_tokens=10, output_tokens=None, latency_ms=1,
        ),
    )
    r = ModelOnlyProvider(api_key="k", model="gpt-4o-mini").research("X", "ctx")
    assert r.tokens_used is None


def test_provider_raises_topic_schema_error_on_empty_content(monkeypatch):
    from app.ai.provider import RawModelCall
    _install_raw(
        monkeypatch,
        RawModelCall(
            content="", model="m",
            input_tokens=1, output_tokens=1, latency_ms=1,
        ),
    )
    with pytest.raises(TopicSchemaError):
        ModelOnlyProvider(api_key="k", model="gpt-4o-mini").research("X", "ctx")


def test_provider_raises_topic_schema_error_on_non_string_content(monkeypatch):
    from app.ai.provider import RawModelCall
    _install_raw(
        monkeypatch,
        RawModelCall(
            content=None, model="m",
            input_tokens=1, output_tokens=1, latency_ms=1,
        ),
    )
    with pytest.raises(TopicSchemaError):
        ModelOnlyProvider(api_key="k", model="gpt-4o-mini").research("X", "ctx")


def test_parse_and_validate_topic_valid():
    out = parse_and_validate_topic({"summary": "ok", "sources": [], "notes": None})
    assert isinstance(out, TopicBriefOutput)
    assert out.summary == "ok"


def test_parse_and_validate_topic_bad_json():
    with pytest.raises(TopicSchemaError):
        parse_and_validate_topic("{not json")


def test_parse_and_validate_topic_missing_summary():
    with pytest.raises(TopicSchemaError):
        parse_and_validate_topic({"sources": [], "notes": None})


def test_parse_and_validate_topic_empty_summary_rejected():
    with pytest.raises(TopicSchemaError):
        parse_and_validate_topic({"summary": "", "sources": [], "notes": None})


def test_parse_and_validate_topic_malformed_source_entry_rejected():
    with pytest.raises(TopicSchemaError):
        parse_and_validate_topic(
            {"summary": "s", "sources": ["not-an-object"], "notes": None}
        )


def test_parse_and_validate_topic_does_not_apply_rolling_coercions():
    out = parse_and_validate_topic(
        {"summary": "s", "sources": [], "understanding": {"nested": "obj"}}
    )
    assert isinstance(out, TopicBriefOutput)
    assert out.summary == "s"


def test_research_result_is_frozen():
    r = ResearchResult(
        topic_name="t", summary="s", grounding_status="model_only",
        provider="model_only", model="m", tokens_used=None, tool_calls=None,
        latency_ms=1, raw_content="{}", brief_json=None, sources=[],
    )
    with pytest.raises(FrozenInstanceError):
        r.summary = "mutated"


def test_research_result_raw_content_not_serialized_in_api_response():
    """T7 DEPENDENCY GUARD.

    T4 does not implement Topic Intelligence API endpoints.  When T7c
    adds GET /topics and GET /topics/{id}, no response schema may include
    ResearchResult.raw_content.  This test guards that requirement against
    the schemas that exist today and will fail if a future response schema
    accidentally exposes raw_content.
    """
    fields = {f.name for f in ResearchResult.__dataclass_fields__.values()}
    assert "raw_content" in fields, "raw_content must exist on ResearchResult"

    from app.schemas import topic as topic_schema_mod
    response_models = [
        obj for obj in vars(topic_schema_mod).values()
        if isinstance(obj, type) and hasattr(obj, "model_fields")
    ]
    for model in response_models:
        assert "raw_content" not in model.model_fields, (
            f"{model.__name__} must not expose raw_content"
        )
