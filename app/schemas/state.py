"""State response schemas (Phase D.2).

Every item carries meeting_relevance and thread_id.  Incidental items are
filtered out of the default response by the state builder; they can be
requested explicitly.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from pydantic import BaseModel, Field

from app.schemas.context_gap import ContextGapOut


class FactOut(BaseModel):
    id: int
    key: str
    value: str
    status: str
    confidence: Optional[float] = None
    source_discussion_id: Optional[int] = None
    source_text: Optional[str] = None
    supersedes_fact_id: Optional[int] = None
    first_seen_at: datetime
    last_updated_at: datetime
    meeting_relevance: Optional[str] = None
    thread_id: Optional[int] = None

    model_config = {"from_attributes": True}


class DecisionOut(BaseModel):
    id: int
    text: str
    status: str
    source_discussion_id: Optional[int] = None
    source_text: Optional[str] = None
    first_seen_at: datetime
    last_updated_at: datetime
    meeting_relevance: Optional[str] = None
    thread_id: Optional[int] = None

    model_config = {"from_attributes": True}


class ActionItemOut(BaseModel):
    id: int
    text: str
    owner: Optional[str] = None
    due: Optional[str] = None
    status: str
    source_discussion_id: Optional[int] = None
    source_text: Optional[str] = None
    first_seen_at: datetime
    last_updated_at: datetime
    meeting_relevance: Optional[str] = None
    thread_id: Optional[int] = None
    previous_status: Optional[str] = None

    model_config = {"from_attributes": True}


class RiskOut(BaseModel):
    id: int
    text: str
    kind: str
    severity: Optional[str] = None
    status: str
    source_discussion_id: Optional[int] = None
    source_text: Optional[str] = None
    first_seen_at: datetime
    last_updated_at: datetime
    meeting_relevance: Optional[str] = None
    thread_id: Optional[int] = None

    model_config = {"from_attributes": True}


class QuestionOut(BaseModel):
    id: int
    text: str
    category: str
    rationale: Optional[str] = None
    status: str
    is_new: bool
    source_discussion_id: Optional[int] = None
    source_text: Optional[str] = None
    first_seen_at: datetime
    last_updated_at: datetime
    meeting_relevance: Optional[str] = None
    thread_id: Optional[int] = None

    model_config = {"from_attributes": True}


class ThreadOut(BaseModel):
    id: int
    key: str
    title: str
    status: str
    summary: Optional[str] = None
    fact_count: int = 0
    decision_count: int = 0
    action_count: int = 0
    risk_count: int = 0
    question_count: int = 0

    model_config = {"from_attributes": True}


class MeetingStateOut(BaseModel):
    meeting_id: str
    mode: Optional[str] = None
    mode_inferred: bool = False

    # Conversational fields (from the latest snapshot)
    understanding: Optional[str] = None
    key_discussion: list[str] = Field(default_factory=list)
    potential_response: Optional[str] = None
    missing_information: list[str] = Field(default_factory=list)
    contradictions: list[str] = Field(default_factory=list)
    next_steps: list[str] = Field(default_factory=list)

    # Entities
    facts: list[FactOut] = Field(default_factory=list)
    decisions: list[DecisionOut] = Field(default_factory=list)
    action_items: list[ActionItemOut] = Field(default_factory=list)
    risks: list[RiskOut] = Field(default_factory=list)
    questions_now: list[QuestionOut] = Field(default_factory=list)
    questions_later: list[QuestionOut] = Field(default_factory=list)

    # Grouping + delta
    threads: list[ThreadOut] = Field(default_factory=list)
    delta: Optional[dict[str, Any]] = None

    # Counts of incidental items excluded from the default view
    incidental_counts: dict[str, int] = Field(default_factory=dict)
    context_gaps: list[ContextGapOut] = Field(default_factory=list)
    context_gaps_total: int = 0

    # Provenance
    last_snapshot_at: Optional[datetime] = None
    last_analysis_run_id: Optional[int] = None
