"""Strict schemas the LLM must emit.

Phase B (v0.2.0) adds:
  - meeting_mode_proposal       (rolling only, top level)
  - threads[]                   (proposed conversation threads)
  - meeting_relevance + thread_key + change on every item
  - speaker_turns[]             (per discussion segment, optional)
  - what_changed_proposal       (short narrative; the deterministic delta is
                                 computed separately by the reducer)

All new fields are optional.  Prompts that do not emit them continue to
validate.  Persistence and behaviour are added in later phases.
"""
from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field, field_validator, model_validator


# --- enums ---
FactStatus = Literal["confirmed", "tentative", "disputed", "superseded", "inferred", "unresolved"]
RiskKind = Literal["risk", "issue"]
RiskSeverity = Literal["low", "medium", "high", "critical"]
QuestionCategory = Literal["now", "later"]
MeetingRelevance = Literal["critical", "relevant", "contextual", "incidental"]
ChangeKind = Literal["created", "updated", "superseded", "resolved"]
MeetingMode = Literal[
    "action_tracker",
    "discovery",
    "technical_workshop",
    "architecture_review",
    "decision",
    "governance",
    "training",
    "demo",
    "general",
]


# --- shared sub-models ---
class ChangeAnnotation(BaseModel):
    """Optional per-item change annotation.

    kind describes what the model believes happened to this item relative
    to the prior state.  reason is a short human-readable note.
    supersedes_ref, when set, is a hint at the row this item supersedes;
    the reducer validates it before acting.
    """
    kind: ChangeKind
    reason: Optional[str] = None
    supersedes_ref: Optional[int] = None

    @field_validator("kind", mode="before")
    @classmethod
    def _coerce_unknown_kind(cls, v):
        """Phase H2 fix: accept out-of-enum kind values from the model.

        Observed in production: the model returned kind='confirmed'
        (a fact-status value) where a change-kind is expected.  Coercing
        to 'created' is safe because the reducer does not branch on
        change.kind; it uses supersedes_ref / supersedes_fact_id.
        """
        allowed = {"created", "updated", "superseded", "resolved"}
        if isinstance(v, str) and v.strip().lower() not in allowed:
            return "created"
        return v


class ProposedThread(BaseModel):
    key: Optional[str] = Field(default=None, max_length=64)
    title: Optional[str] = Field(default=None, max_length=255)
    status: Literal["open", "resolved", "dormant"] = "open"
    summary: Optional[str] = None

    @model_validator(mode="after")
    def _ensure_identity(self):
        """Phase H2 fix: accept thread objects from either model output shape.

        Observed in production across two models:
          - gpt-4o-mini finalization omitted `title`
          - gpt-4o finalization omitted `key`
        Both fields are the thread's identity in different code paths.
        Accept whichever is present and fill the other from it.  If both
        are missing, route to the __unthreaded__ sentinel used by the
        rolling reducer so the item still has a home.
        """
        k = (self.key or "").strip() if self.key else ""
        t = (self.title or "").strip() if self.title else ""
        if not k and not t:
            object.__setattr__(self, "key", "__unthreaded__")
            object.__setattr__(self, "title", "Unthreaded")
        elif not k:
            object.__setattr__(self, "key", t)
        elif not t:
            object.__setattr__(self, "title", k)
        return self


class SpeakerTurn(BaseModel):
    turn_index: int
    speaker_label: str = Field(min_length=1, max_length=64)  # e.g. "Speaker 1"
    text: str = Field(min_length=1)


# --- items ---
class ProposedFact(BaseModel):
    # The model sometimes emits a fact as {"text": "..."} matching the
    # shape of the other item types (decision, action, risk, question).
    # Accept that shape: key, value, status become optional with safe
    # defaults, and text becomes an accepted alternative.  The coercion
    # layer in app/ai/output_schema.py backfills key/value from text so
    # downstream code sees a populated fact.
    key: str = Field(default="", max_length=255)
    value: str = Field(default="")
    status: FactStatus = "tentative"
    text: Optional[str] = None
    confidence: Optional[float] = Field(default=None, ge=0.0, le=1.0)
    source_discussion_id: Optional[int] = None
    source_text: Optional[str] = None
    supersedes_fact_id: Optional[int] = None
    # Phase B additions
    meeting_relevance: Optional[MeetingRelevance] = None
    thread_key: Optional[str] = None
    change: Optional[ChangeAnnotation] = None


class ProposedDecision(BaseModel):
    text: str = Field(min_length=1)
    status: Literal["proposed", "confirmed"] = "proposed"
    source_discussion_id: Optional[int] = None
    source_text: Optional[str] = None
    meeting_relevance: Optional[MeetingRelevance] = None
    thread_key: Optional[str] = None
    change: Optional[ChangeAnnotation] = None


class ProposedActionItem(BaseModel):
    text: str = Field(min_length=1)
    owner: Optional[str] = None
    due: Optional[str] = None
    source_discussion_id: Optional[int] = None
    source_text: Optional[str] = None
    meeting_relevance: Optional[MeetingRelevance] = None
    thread_key: Optional[str] = None
    change: Optional[ChangeAnnotation] = None
    previous_status: Optional[str] = None


class ProposedRisk(BaseModel):
    text: str = Field(min_length=1)
    kind: RiskKind = "risk"
    severity: Optional[RiskSeverity] = None
    source_discussion_id: Optional[int] = None
    source_text: Optional[str] = None
    meeting_relevance: Optional[MeetingRelevance] = None
    thread_key: Optional[str] = None
    change: Optional[ChangeAnnotation] = None


class ProposedQuestion(BaseModel):
    text: str = Field(min_length=1)
    category: QuestionCategory
    rationale: Optional[str] = None
    source_discussion_id: Optional[int] = None
    source_text: Optional[str] = None
    meeting_relevance: Optional[MeetingRelevance] = None
    thread_key: Optional[str] = None
    change: Optional[ChangeAnnotation] = None
    # Optional useful references the model may provide.
    related_action: Optional[str] = None
    related_risk: Optional[str] = None


class DiscussionSegmentAnnotation(BaseModel):
    """Optional model annotation for one discussion segment."""
    id: int
    speaker_turns: list[SpeakerTurn] = Field(default_factory=list)
    thread_key: Optional[str] = None


_COERCE_TO_DEFAULT = {
    "understanding": "",
    "key_discussion": [],
    "missing_information": [],
    "contradictions": [],
    "next_steps": [],
}


def _coerce_nulls(data):
    if isinstance(data, dict):
        for key, default in _COERCE_TO_DEFAULT.items():
            if key in data and data[key] is None:
                data[key] = default
    return data


class RollingAnalysisOutput(BaseModel):
    """Structured output for a rolling analysis run (v0.2.0).

    All string and list fields accept None and coerce to their default.
    The model frequently returns null for empty values, and rejecting that
    would fail otherwise valid runs.  See Phase B fix (2026-09-23).
    """
    # v0.1.0 fields (unchanged)
    understanding: str = Field(default="")
    key_discussion: list[str] = Field(default_factory=list)
    facts: list[ProposedFact] = Field(default_factory=list)
    decisions: list[ProposedDecision] = Field(default_factory=list)
    action_items: list[ProposedActionItem] = Field(default_factory=list)
    risks: list[ProposedRisk] = Field(default_factory=list)
    questions_now: list[ProposedQuestion] = Field(default_factory=list)
    questions_later: list[ProposedQuestion] = Field(default_factory=list)
    potential_response: Optional[str] = None
    missing_information: list[str] = Field(default_factory=list)
    contradictions: list[str] = Field(default_factory=list)
    next_steps: list[str] = Field(default_factory=list)

    # v0.2.0 additions (all optional)
    meeting_mode_proposal: Optional[MeetingMode] = None
    threads: list[ProposedThread] = Field(default_factory=list)
    discussion_annotations: list[DiscussionSegmentAnnotation] = Field(default_factory=list)
    what_changed_proposal: Optional[str] = None

    @field_validator("what_changed_proposal", mode="before")
    @classmethod
    def _coerce_what_changed_proposal(cls, v):
        """Phase H2 fix: accept whatever shape the model emits.

        Observed in production: the model returned a dict such as
        {"new_actions": [...], "thread_id": 43} instead of the string
        narrative described in the prompt.  Serialize structured values
        to JSON text rather than rejecting the whole output.
        """
        if v is None:
            return None
        if isinstance(v, str):
            return v
        try:
            import json as _json
            return _json.dumps(v, ensure_ascii=False, default=str)
        except Exception:
            return str(v)

    @model_validator(mode="before")
    @classmethod
    def _normalise_nulls(cls, data):
        return _coerce_nulls(data)


class FinalizationOutput(RollingAnalysisOutput):
    """Model output for the one-time finalization pass (v0.2.0).

    Adds the three derived artifacts.  Cleaned transcript is expected to
    include speaker labels where the model was confident.
    """
    cleaned_transcript: Optional[str] = None
    executive_summary: Optional[str] = None
    pm_notes_markdown: Optional[str] = None
