"""Topic Intelligence response schemas (Stage A).

Four resources: meeting_topics, topic_research_runs, topic_sources,
topic_briefs.  All are ORM-backed and serialized via from_attributes.

Consistent with the rest of app/schemas:
  - *Out classes for ORM-backed responses
  - *List envelopes for paginated collections
  - brief_json is intentionally NOT returned in Stage A, matching the
    AnalysisRunOut precedent.  A detail schema may be added in Stage B.

The raw error field on TopicResearchRun is never exposed through the API.
Instead, the API layer computes a sanitized failure_kind from a known set
of categories.  See app/api/topics.py.
"""
from __future__ import annotations

from datetime import datetime
from typing import Literal, Optional

from pydantic import BaseModel, Field


class TopicSourceOut(BaseModel):
    id: int
    research_run_id: int
    url: Optional[str] = None
    title: Optional[str] = None
    snippet: Optional[str] = None
    citation_index: Optional[int] = None
    created_at: datetime

    model_config = {"from_attributes": True}


class TopicBriefOut(BaseModel):
    id: int
    research_run_id: int
    summary: str
    conceptual_diagram_mermaid: Optional[str] = None
    # brief_json intentionally omitted from Stage A.
    # A detail schema may return it in Stage B.
    created_at: datetime

    model_config = {"from_attributes": True}


class TopicResearchRunOut(BaseModel):
    id: int
    topic_id: int
    provider: str
    grounding_status: str
    model: Optional[str] = None
    tokens_used: Optional[int] = None
    tool_calls: Optional[int] = None
    latency_ms: Optional[int] = None
    failure_kind: Optional[
        Literal["provider_error", "schema_error", "missing_segment", "unknown"]
    ] = None
    created_at: datetime

    model_config = {"from_attributes": True}


class MeetingTopicOut(BaseModel):
    id: int
    meeting_id: str
    name: str
    topic_type: str
    relevance: str
    research_status: str
    first_seen_discussion_id: Optional[int] = None
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class MeetingTopicList(BaseModel):
    items: list[MeetingTopicOut] = Field(default_factory=list)
    total: int
    limit: int
    offset: int


class TopicDetailOut(BaseModel):
    topic: MeetingTopicOut
    latest_run: Optional[TopicResearchRunOut] = None
    sources: list[TopicSourceOut] = Field(default_factory=list)
    brief: Optional[TopicBriefOut] = None

    model_config = {"from_attributes": True}
