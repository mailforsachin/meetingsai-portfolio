"""Topic research pass.

Runs one ModelOnlyProvider research call for one pending MeetingTopic and
persists TopicResearchRun, TopicSource, and TopicBrief rows atomically in
the caller's transaction.  Never commits or rolls back.

Stage A limitations (documented):

1. TopicResearchRun has no `status` column.  A failure is recorded by
   setting `error` on the run row and
   `meeting_topics.research_status = "failed"`.  There is no visible
   "running" state before the provider call completes.

2. A process crash before the caller commits may leave no audit row at
   all -- neither success nor failure.

3. T7a SHOULD wrap each call in a savepoint (or an equivalent per-topic
   transaction boundary) so one topic's DB failure does not roll back
   successful research for unrelated topics.  T6 does not implement this.

4. T7a must define bounded retry/cooldown for failed topics before
   wiring.  T6 records the failure but never retries.
"""
from __future__ import annotations

from sqlalchemy.orm import Session

from app.ai.provider import AIProviderError
from app.ai.topic_intelligence.parsing import TopicSchemaError
from app.ai.topic_intelligence.provider import ModelOnlyProvider, ResearchResult
from app.models.discussion_segment import DiscussionSegment
from app.models.meeting import Meeting
from app.models.topic import MeetingTopic, TopicBrief, TopicResearchRun, TopicSource


RESEARCH_CONTEXT_MAX_CHARS = 4000


def _resolve_context_segment(
    db: Session, topic: MeetingTopic
) -> tuple[str | None, bool]:
    """Return (segment_text, missing).

    missing=True when first_seen_discussion_id is null or the segment
    no longer exists.  In that case the caller must NOT call the model.
    """
    if topic.first_seen_discussion_id is None:
        return None, True
    segment = db.get(DiscussionSegment, topic.first_seen_discussion_id)
    if segment is None:
        return None, True
    return segment.text or "", False


def _build_research_context(meeting: Meeting, segment_text: str) -> str:
    """Bound the model-facing context to a documented maximum.

    Format:
        Objective: <objective>

        <segment_text>

    Truncates to RESEARCH_CONTEXT_MAX_CHARS, keeping the objective intact.
    """
    objective = (meeting.objective or "").strip()
    body = segment_text or ""
    header = f"Objective: {objective}\n\n" if objective else ""
    budget = RESEARCH_CONTEXT_MAX_CHARS - len(header)
    if budget <= 0:
        return header[:RESEARCH_CONTEXT_MAX_CHARS]
    if len(body) > budget:
        body = body[:budget]
    return header + body


def _record_failure(
    db: Session,
    meeting_id: str,
    topic: MeetingTopic,
    provider: ModelOnlyProvider,
    error: str,
) -> None:
    """Persist a failed run row and mark the topic failed.  No commit."""
    run = TopicResearchRun(
        topic_id=topic.id,
        provider="model_only",
        grounding_status="model_only",
        model=provider.model,
        error=error[:1000],
    )
    db.add(run)
    topic.research_status = "failed"
    db.flush()


def _persist_success(
    db: Session,
    topic: MeetingTopic,
    result: ResearchResult,
) -> None:
    """Persist run, sources, and brief.  No commit.  raw_content unused."""
    run = TopicResearchRun(
        topic_id=topic.id,
        provider=result.provider,
        grounding_status=result.grounding_status,
        model=result.model,
        tokens_used=result.tokens_used,
        tool_calls=result.tool_calls,
        latency_ms=result.latency_ms,
        error=None,
    )
    db.add(run)
    db.flush()

    for src in result.sources:
        db.add(TopicSource(
            research_run_id=run.id,
            url=src.url,
            title=src.title,
            snippet=src.snippet,
            citation_index=src.citation_index,
        ))

    db.add(TopicBrief(
        research_run_id=run.id,
        summary=result.summary,
        brief_json=result.brief_json,
        conceptual_diagram_mermaid=None,
    ))

    topic.research_status = "done"
    db.flush()


def research_pending_topic(
    db: Session,
    meeting_id: str,
    topic_id: int,
    provider: ModelOnlyProvider,
) -> ResearchResult | None:
    """Run research on one topic and persist the outcome.

    Caller owns the transaction.  Returns ResearchResult on success.
    Returns None on provider/schema failure or missing segment.  In all
    failure cases a TopicResearchRun row with `error` is added and the
    topic's research_status is set to "failed".

    Raises:
        sqlalchemy.exc.SQLAlchemyError -- DB failures propagate.  The
        caller must roll back.
    """
    meeting = db.get(Meeting, meeting_id)
    if meeting is None:
        return None

    topic = db.get(MeetingTopic, topic_id)
    if topic is None:
        return None

    segment_text, missing = _resolve_context_segment(db, topic)
    if missing:
        _record_failure(
            db, meeting_id, topic, provider,
            "first_seen_discussion_id is null or segment not found",
        )
        return None

    context = _build_research_context(meeting, segment_text or "")

    try:
        result = provider.research(topic.name, context)
    except (AIProviderError, TopicSchemaError) as e:
        _record_failure(db, meeting_id, topic, provider, str(e))
        return None

    _persist_success(db, topic, result)
    return result
