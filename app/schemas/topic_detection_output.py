"""Pydantic schemas for Topic Intelligence detection output.

The detection prompt asks the model to identify candidate topics in one
discussion segment.  This schema validates that output.  It is isolated
from the rolling/finalization schemas in app/schemas/ai_output.py.

The five-topic cap is a prompt-level instruction, not a schema constraint.
An otherwise-valid response with more than five topics must not fail
validation solely due to the count.  T6 handles dedup and truncation.
"""
from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field


TopicType = Literal[
    "technology",
    "concept",
    "person",
    "organization",
    "project",
    "other",
]

TopicRelevance = Literal["high", "medium", "low"]


class ProposedTopic(BaseModel):
    """One candidate topic proposed by the detection model."""
    name: str = Field(min_length=1, max_length=255)
    topic_type: TopicType
    relevance: TopicRelevance
    rationale: Optional[str] = None


class TopicDetectionOutput(BaseModel):
    """Top-level detection output for one discussion segment."""
    topics: list[ProposedTopic] = Field(default_factory=list)
