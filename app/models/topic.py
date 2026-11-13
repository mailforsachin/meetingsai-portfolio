from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.db.types import LongText


class MeetingTopic(Base):
    __tablename__ = "meeting_topics"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    meeting_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("meetings.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    topic_type: Mapped[str] = mapped_column(String(32), nullable=False, default="concept")
    relevance: Mapped[str] = mapped_column(String(16), nullable=False, default="medium")
    research_status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    first_seen_discussion_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("discussion_segments.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (
        Index("ix_meeting_topics_meeting_status", "meeting_id", "research_status"),
        Index("ix_meeting_topics_meeting_name", "meeting_id", "name"),
    )


class TopicResearchRun(Base):
    __tablename__ = "topic_research_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    topic_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("meeting_topics.id", ondelete="CASCADE"), nullable=False
    )
    provider: Mapped[str] = mapped_column(String(32), nullable=False, default="model_only")
    grounding_status: Mapped[str] = mapped_column(String(16), nullable=False, default="model_only")
    model: Mapped[str | None] = mapped_column(String(64), nullable=True)
    tokens_used: Mapped[int | None] = mapped_column(Integer, nullable=True)
    tool_calls: Mapped[int | None] = mapped_column(Integer, nullable=True)
    latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, server_default=func.now())

    __table_args__ = (
        Index("ix_topic_research_runs_topic_created", "topic_id", "created_at"),
    )


class TopicSource(Base):
    __tablename__ = "topic_sources"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    research_run_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("topic_research_runs.id", ondelete="CASCADE"), nullable=False
    )
    url: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    title: Mapped[str | None] = mapped_column(String(512), nullable=True)
    snippet: Mapped[str | None] = mapped_column(Text, nullable=True)
    citation_index: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, server_default=func.now())

    __table_args__ = (
        Index("ix_topic_sources_run", "research_run_id"),
    )


class TopicBrief(Base):
    __tablename__ = "topic_briefs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    research_run_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("topic_research_runs.id", ondelete="CASCADE"), nullable=False
    )
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    conceptual_diagram_mermaid: Mapped[str | None] = mapped_column(Text, nullable=True)
    brief_json: Mapped[str | None] = mapped_column(LongText, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, server_default=func.now())

    __table_args__ = (
        UniqueConstraint("research_run_id", name="uq_topic_briefs_research_run"),
    )
