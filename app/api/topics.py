"""Topic Intelligence REST endpoints (Stage A).

Two endpoints:
    GET /api/meetings/{meeting_id}/topics
    GET /api/meetings/{meeting_id}/topics/{topic_id}

Meeting-scoped and authenticated.  Never returns raw error text, brief_json,
raw_content, or exception diagnostics.  Failures are exposed as a sanitized
failure_kind drawn from a fixed set of categories.
"""
from __future__ import annotations

import logging
from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.deps import require_user
from app.db.session import get_db
from app.models.meeting import Meeting
from app.models.topic import MeetingTopic, TopicBrief, TopicResearchRun, TopicSource
from app.schemas.topic import (
    MeetingTopicList,
    MeetingTopicOut,
    TopicBriefOut,
    TopicDetailOut,
    TopicResearchRunOut,
    TopicSourceOut,
)

log = logging.getLogger("meetingsai.api.topics")

router = APIRouter(
    prefix="/api/meetings",
    tags=["topics"],
    dependencies=[Depends(require_user)],
)


def _get_meeting_or_404(db: Session, meeting_id: str) -> Meeting:
    m = db.get(Meeting, meeting_id)
    if m is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="meeting not found"
        )
    return m


def _failure_kind(error: Optional[str]) -> Optional[str]:
    """Map a raw TopicResearchRun.error to a sanitized category.

    Returns None when no error is recorded.  Unknown formats map to
    "unknown".  Never returns or echoes the raw error text.

    Recognized prefixes are exactly those produced by the T6/T7a
    research path (app/ai/topic_intelligence/research.py,
    app/ai/provider.py, app/ai/topic_intelligence/parsing.py).
    """
    if error is None:
        return None
    if error.startswith("first_seen_discussion_id is null"):
        return "missing_segment"
    if error.startswith("model call failed"):
        return "provider_error"
    if error.startswith("model response malformed"):
        return "provider_error"
    if error.startswith("invalid JSON from model"):
        return "schema_error"
    if error.startswith("schema validation failed"):
        return "schema_error"
    return "unknown"


def _run_to_out(run: TopicResearchRun) -> TopicResearchRunOut:
    """Build a TopicResearchRunOut, sanitizing the failure category.

    Explicit field-by-field construction makes it obvious that the raw
    `run.error` text never reaches the response.  Only the sanitized
    category is exposed.
    """
    return TopicResearchRunOut(
        id=run.id,
        topic_id=run.topic_id,
        provider=run.provider,
        grounding_status=run.grounding_status,
        model=run.model,
        tokens_used=run.tokens_used,
        tool_calls=run.tool_calls,
        latency_ms=run.latency_ms,
        failure_kind=_failure_kind(run.error),
        created_at=run.created_at,
    )


@router.get("/{meeting_id}/topics", response_model=MeetingTopicList)
def list_topics(
    meeting_id: str,
    db: Session = Depends(get_db),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    status_filter: Optional[Literal["pending", "done", "failed"]] = Query(
        default=None, alias="status"
    ),
) -> MeetingTopicList:
    _get_meeting_or_404(db, meeting_id)

    filters = [MeetingTopic.meeting_id == meeting_id]
    if status_filter is not None:
        filters.append(MeetingTopic.research_status == status_filter)

    total = db.scalar(
        select(func.count()).select_from(MeetingTopic).where(*filters)
    ) or 0
    rows = db.scalars(
        select(MeetingTopic)
        .where(*filters)
        .order_by(MeetingTopic.id.desc())
        .limit(limit)
        .offset(offset)
    ).all()
    return MeetingTopicList(
        items=[MeetingTopicOut.model_validate(r) for r in rows],
        total=int(total),
        limit=limit,
        offset=offset,
    )


@router.get("/{meeting_id}/topics/{topic_id}", response_model=TopicDetailOut)
def get_topic(
    meeting_id: str,
    topic_id: int,
    db: Session = Depends(get_db),
) -> TopicDetailOut:
    _get_meeting_or_404(db, meeting_id)

    topic = db.scalar(
        select(MeetingTopic).where(
            MeetingTopic.id == topic_id,
            MeetingTopic.meeting_id == meeting_id,
        )
    )
    if topic is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="topic not found"
        )

    latest_run = db.scalars(
        select(TopicResearchRun)
        .where(TopicResearchRun.topic_id == topic.id)
        .order_by(TopicResearchRun.id.desc())
        .limit(1)
    ).first()

    if latest_run is None:
        return TopicDetailOut(
            topic=MeetingTopicOut.model_validate(topic),
            latest_run=None,
            sources=[],
            brief=None,
        )

    sources = list(db.scalars(
        select(TopicSource)
        .where(TopicSource.research_run_id == latest_run.id)
        .order_by(TopicSource.citation_index.is_(None), TopicSource.citation_index.asc(), TopicSource.id.asc())
    ).all())

    brief = db.scalars(
        select(TopicBrief).where(TopicBrief.research_run_id == latest_run.id)
    ).first()

    return TopicDetailOut(
        topic=MeetingTopicOut.model_validate(topic),
        latest_run=_run_to_out(latest_run),
        sources=[TopicSourceOut.model_validate(s) for s in sources],
        brief=TopicBriefOut.model_validate(brief) if brief is not None else None,
    )
