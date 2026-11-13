"""Pydantic schema for the Context Review LLM output.

Context Review does one thing: detect unresolved meeting state.  Its
output is a list of gaps.  It does not emit actions, decisions, risks,
or questions.  It does not write to the database.  The reducer in
app.ai.context_review_reducer is the only place that persists gaps.

Every gap carries:
  - gap_kind: what kind of unresolved state this is
  - category: which question family it belongs to
  - text: the actionable observation or question
  - urgency: low | medium | high | critical
  - evidence: discussion_ids (at least one) and source_text (exact phrase)

The model may also propose threads, reusing the same shape as the rolling
analysis.  Thread keys are validated by the reducer before persistence.
"""
from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field, model_validator

# Reuse the thread shape from ai_output to keep the contracts aligned.
from app.schemas.ai_output import ProposedThread  # noqa: F401


GapKind = Literal[
    "owner_gap",
    "deadline_gap",
    "decision_gap",
    "dependency_gap",
    "risk_gap",
    "cost_gap",
    "unresolved_issue",
    "implied_question",
    "conflicting_statements",
]

GapCategory = Literal[
    "literal",
    "contextual",
    "follow_up",
    "decision_gap",
    "owner_gap",
    "deadline_gap",
    "dependency_gap",
    "risk_gap",
]

GapUrgency = Literal["low", "medium", "high", "critical"]


class ProposedGapEvidence(BaseModel):
    discussion_ids: list[int] = Field(default_factory=list)
    source_text: Optional[str] = None


class ProposedGap(BaseModel):
    gap_kind: GapKind
    category: GapCategory
    text: str = Field(min_length=1)
    rationale: Optional[str] = None
    thread_key: Optional[str] = None
    urgency: GapUrgency
    evidence: ProposedGapEvidence


class ContextReviewOutput(BaseModel):
    """Top-level output the model must produce for one Context Review run."""
    gaps: list[ProposedGap] = Field(default_factory=list)
    threads: list[ProposedThread] = Field(default_factory=list)
    notes: Optional[str] = None

    @model_validator(mode="before")
    @classmethod
    def _normalise_nulls(cls, data):
        """The model sometimes emits `null` for empty lists.  Coerce to defaults."""
        if isinstance(data, dict):
            for key in ("gaps", "threads"):
                if key in data and data[key] is None:
                    data[key] = []
        return data
