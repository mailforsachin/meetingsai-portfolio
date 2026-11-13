"""Topic detection pass.

Reads one discussion segment, calls the model, parses the response, and
upserts MeetingTopic rows for the meeting.  Never commits or rolls back --
the caller owns the transaction.

DB errors propagate.  Provider and schema errors are captured in a
structured DetectionSummary so the scheduler (T7a) can advance past a
poison segment without aborting the meeting.
"""
from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.ai.provider import AIAnalysisProvider, AIProviderError
from app.ai.prompt_loader import load_prompt
from app.ai.topic_intelligence.detection_parsing import (
    TopicDetectionSchemaError,
    parse_and_validate_detection,
)
from app.models.discussion_segment import DiscussionSegment
from app.models.meeting import Meeting
from app.models.topic import MeetingTopic
from app.schemas.topic_detection_output import ProposedTopic


_RELEVANCE_RANK = {"low": 1, "medium": 2, "high": 3}


@dataclass
class DetectionSummary:
    """Outcome of one detection pass over one segment.

    Exactly one of these is true:
      succeeded=True   -- provider call + parse succeeded (may have 0 topics)
      provider_failed  -- AIProviderError raised
      schema_failed    -- TopicDetectionSchemaError raised
      skipped          -- meeting or segment missing (stale input)

    `created=0, updated=0` with `succeeded=True` means the model returned
    an empty topic list -- a legitimate result.
    """
    succeeded: bool = False
    provider_failed: bool = False
    schema_failed: bool = False
    skipped: bool = False
    reason: str | None = None
    created: int = 0
    updated: int = 0
    error: str | None = None


def _topic_lookup_key(name: str) -> str:
    """Deterministic key for (meeting_id, name) dedup lookup.

    strip, collapse internal whitespace to a single space, casefold.
    Used ONLY for lookup -- never stored.
    """
    return " ".join(name.split()).casefold()


def _topic_display_name(name: str) -> str:
    """User-facing display name: strip and collapse internal whitespace.

    Preserves case.  This is what is stored in meeting_topics.name.
    """
    return " ".join(name.split())


def _relevance_rank(value: str) -> int:
    return _RELEVANCE_RANK.get(value, 0)


def _find_topic_by_lookup_key(
    db: Session, meeting_id: str, lookup_key: str
) -> MeetingTopic | None:
    """Find an existing topic by normalized key.

    Scans the meeting's topics in Python because the T2 schema has no
    normalized-key column.  Bounded by the number of topics per meeting.
    """
    rows = db.query(MeetingTopic).filter(
        MeetingTopic.meeting_id == meeting_id
    ).all()
    for row in rows:
        if _topic_lookup_key(row.name) == lookup_key:
            return row
    return None


def _upsert_topic(
    db: Session,
    meeting_id: str,
    proposed: ProposedTopic,
    segment_id: int | None,
) -> str:
    """Upsert one detected topic.  Returns 'created' or 'updated'.

    Does NOT commit or rollback.  DB errors propagate.
    """
    display = _topic_display_name(proposed.name)
    lookup = _topic_lookup_key(proposed.name)

    existing = _find_topic_by_lookup_key(db, meeting_id, lookup)
    if existing is not None:
        # Display name and first_seen_discussion_id are set once, on
        # creation, and preserved on subsequent sightings.
        if _relevance_rank(proposed.relevance) > _relevance_rank(existing.relevance):
            existing.relevance = proposed.relevance
        return "updated"

    row = MeetingTopic(
        meeting_id=meeting_id,
        name=display,
        topic_type=proposed.topic_type,
        relevance=proposed.relevance,
        research_status="pending",
        first_seen_discussion_id=segment_id,
    )
    db.add(row)
    return "created"


def detect_topics_for_segment(
    db: Session,
    meeting_id: str,
    segment_id: int,
    provider: AIAnalysisProvider,
    max_new_topics: int | None = None,
    max_segment_chars: int | None = None,
) -> DetectionSummary:
    """Detect topics in one segment and upsert MeetingTopic rows.

    Caller owns the transaction.  Adds and flushes rows, never commits.

    Raises:
        sqlalchemy.exc.SQLAlchemyError -- DB failures propagate.  The
        caller must roll back.
    """
    meeting = db.get(Meeting, meeting_id)
    if meeting is None:
        return DetectionSummary(skipped=True, reason="meeting_not_found")

    segment = db.get(DiscussionSegment, segment_id)
    if segment is None:
        return DetectionSummary(skipped=True, reason="segment_not_found")

    text = segment.text or ""
    if max_segment_chars is not None and len(text) > max_segment_chars:
        text = text[:max_segment_chars]

    prompt = load_prompt("topic_detection.md")
    payload = {
        "meeting_objective": meeting.objective,
        "meeting_organization": meeting.organization,
        "discussion_segment": {
            "id": segment.id,
            "started_at": segment.started_at.isoformat() if segment.started_at else None,
            "text": text,
        },
    }

    try:
        call = provider.call_model(prompt, payload)
    except AIProviderError as e:
        return DetectionSummary(
            provider_failed=True,
            error=str(e)[:1000],
        )

    try:
        out = parse_and_validate_detection(call.content)
    except TopicDetectionSchemaError as e:
        return DetectionSummary(
            schema_failed=True,
            error=str(e)[:1000],
        )

    proposals = out.topics
    if max_new_topics is not None:
        existing = db.scalar(
            select(func.count()).select_from(MeetingTopic)
            .where(MeetingTopic.meeting_id == meeting_id)
        ) or 0
        remaining = max(0, max_new_topics - existing)
        if remaining == 0:
            summary = DetectionSummary(succeeded=True)
            db.flush()
            return summary
        proposals = proposals[:remaining]

    summary = DetectionSummary(succeeded=True)
    for proposed in proposals:
        outcome = _upsert_topic(db, meeting_id, proposed, segment.id)
        if outcome == "created":
            summary.created += 1
        else:
            summary.updated += 1

    db.flush()
    return summary
