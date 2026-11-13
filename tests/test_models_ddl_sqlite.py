"""Guard: every model must be creatable on SQLite.

This catches dialect-specific constructs (LONGTEXT, UTC_TIMESTAMP, ...) that
compile on MariaDB but fail on SQLite test engines.  Adds a permanent guard
for the class of bug we hit twice.
"""
from __future__ import annotations

from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool

from app.db.base import Base
import app.models  # noqa: F401  registers all models


def test_full_metadata_creates_on_sqlite():
    eng = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
        future=True,
    )
    Base.metadata.create_all(eng)
    tables = set(Base.metadata.tables.keys())
    for expected in (
        "meetings", "transcript_segments", "discussion_segments",
        "ai_analysis_runs", "meeting_state_snapshots",
        "facts", "decisions", "action_items", "risks", "questions",
        "meeting_finalizations",
        "meeting_topics", "topic_research_runs", "topic_sources", "topic_briefs",
    ):
        assert expected in tables, f"missing table: {expected}"


def test_finalization_row_insert_on_sqlite():
    """Insert a MeetingFinalization row to ensure no server_default surprises."""
    from sqlalchemy.orm import sessionmaker
    from app.models.meeting import Meeting
    from app.models.finalization import MeetingFinalization

    eng = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
        future=True,
    )
    Base.metadata.create_all(eng)
    S = sessionmaker(bind=eng, autoflush=False, autocommit=False, future=True)
    db = S()
    try:
        m = Meeting(title="x", status="active")
        db.add(m); db.commit(); db.refresh(m)
        row = MeetingFinalization(
            meeting_id=m.id, status="pending",
            prompt_version="0.1.0", model="gpt-4o-mini",
        )
        db.add(row); db.commit(); db.refresh(row)
        assert row.id is not None
        assert row.started_at is not None
        assert row.created_at is not None
        assert row.updated_at is not None
    finally:
        db.close()


def test_topic_row_insert_on_sqlite():
    """Insert MeetingTopic -> TopicResearchRun -> TopicSource/TopicBrief rows.

    Ensures server_default columns and FKs behave on SQLite.
    """
    from sqlalchemy.orm import sessionmaker
    from app.models.meeting import Meeting
    from app.models.topic import (
        MeetingTopic,
        TopicBrief,
        TopicResearchRun,
        TopicSource,
    )

    eng = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
        future=True,
    )
    Base.metadata.create_all(eng)
    S = sessionmaker(bind=eng, autoflush=False, autocommit=False, future=True)
    db = S()
    try:
        m = Meeting(title="x", status="active")
        db.add(m); db.commit(); db.refresh(m)

        topic = MeetingTopic(meeting_id=m.id, name="vector search")
        db.add(topic); db.commit(); db.refresh(topic)
        assert topic.id is not None
        assert topic.research_status == "pending"
        assert topic.created_at is not None

        run = TopicResearchRun(topic_id=topic.id)
        db.add(run); db.commit(); db.refresh(run)
        assert run.id is not None
        assert run.grounding_status == "model_only"

        src = TopicSource(research_run_id=run.id, url="https://example.com")
        db.add(src); db.commit(); db.refresh(src)
        assert src.id is not None

        brief = TopicBrief(research_run_id=run.id, summary="hello")
        db.add(brief); db.commit(); db.refresh(brief)
        assert brief.id is not None
        assert brief.created_at is not None
    finally:
        db.close()
