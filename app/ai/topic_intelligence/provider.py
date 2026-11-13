"""Topic Intelligence provider interface and Stage A implementation.

Stage A ships ModelOnlyProvider, which reuses the existing Chat
Completions path via AIAnalysisProvider.call_model() and applies topic-
specific parsing.  Stage B adds OpenAIResponsesProvider behind the same
interface, with no changes to callers.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass

from app.ai.provider import AIAnalysisProvider, RawModelCall
from app.ai.prompt_loader import load_prompt
from app.ai.topic_intelligence.parsing import (
    TopicSchemaError,
    parse_and_validate_topic,
)


@dataclass(frozen=True)
class ResearchSource:
    """One provenance source attached to a research run.

    Maps to one row in topic_sources.  All fields nullable: model_only
    research produces no sources; responses research fills them from
    url_citations.
    """
    url: str | None
    title: str | None
    snippet: str | None
    citation_index: int | None


@dataclass(frozen=True)
class ResearchResult:
    """Outcome of one topic research pass.

    Persisted by T6 (research.py) into:
      topic_research_runs  <- provider, grounding_status, model,
                              tokens_used, tool_calls, latency_ms
      topic_briefs         <- summary, brief_json
      topic_sources        <- sources (list -> N rows)

    raw_content is diagnostics only: not persisted, not logged, not
    returned through any API response.

    T7 DEPENDENCY: when T7c adds GET /topics and GET /topics/{id}, no
    response schema may include ResearchResult.raw_content.  T7 must add
    a test proving raw_content is never serialized in an API response.
    """
    topic_name: str
    summary: str
    grounding_status: str
    provider: str
    model: str
    tokens_used: int | None
    tool_calls: int | None
    latency_ms: int
    raw_content: str
    brief_json: str | None
    sources: list[ResearchSource]


def _total_tokens(call: RawModelCall) -> int | None:
    """Sum input + output tokens, or None if either is missing.

    Never substitutes zero for missing usage.  A partial count is treated
    the same as no count at all: tokens_used = None.
    """
    if call.input_tokens is None or call.output_tokens is None:
        return None
    return call.input_tokens + call.output_tokens


class TopicIntelligenceProvider(ABC):
    """Version-agnostic interface for topic research.

    Stage A ships ModelOnlyProvider.  Stage B adds OpenAIResponsesProvider
    behind this same interface, with no changes to callers.
    """

    @abstractmethod
    def research(self, topic_name: str, context: str) -> ResearchResult:
        """Research one topic and return a labeled result.

        Implementations must never label a run "verified" without a real
        url_citation from a web_search tool result.
        """


class ModelOnlyProvider(TopicIntelligenceProvider):
    """Research using the existing Chat Completions path, no web search.

    Every run is labeled grounding_status="model_only".  Source claims
    from the model are stored structurally but never promoted to verified.
    """

    def __init__(
        self,
        api_key: str,
        model: str,
        timeout_seconds: int = 60,
        max_completion_tokens: int | None = None,
    ) -> None:
        self._inner = AIAnalysisProvider(
            api_key=api_key, model=model, timeout_seconds=timeout_seconds,
            max_completion_tokens=max_completion_tokens,
        )

    def research(self, topic_name: str, context: str) -> ResearchResult:
        prompt = load_prompt("topic_brief.md")
        payload = {"topic": topic_name, "context": context}

        call = self._inner.call_model(prompt, payload)

        if not isinstance(call.content, str) or call.content == "":
            raise TopicSchemaError(
                "topic brief content is empty or not a string: "
                f"{type(call.content).__name__}"
            )

        brief = parse_and_validate_topic(call.content)

        sources = [
            ResearchSource(
                url=s.url, title=s.title, snippet=s.snippet, citation_index=None,
            )
            for s in brief.sources
        ]

        return ResearchResult(
            topic_name=topic_name,
            summary=brief.summary,
            grounding_status="model_only",
            provider="model_only",
            model=call.model,
            tokens_used=_total_tokens(call),
            tool_calls=None,
            latency_ms=call.latency_ms,
            raw_content=call.content,
            brief_json=brief.model_dump_json(),
            sources=sources,
        )
