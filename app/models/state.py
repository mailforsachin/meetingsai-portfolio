from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Index, Integer, String, Text, func
from app.db.types import LongText
from typing import Optional

from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class AIAnalysisRun(Base):
    __tablename__ = "ai_analysis_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    meeting_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("meetings.id", ondelete="CASCADE"), nullable=False
    )
    kind: Mapped[str] = mapped_column(String(16), nullable=False)              # rolling | final
    prompt_version: Mapped[str] = mapped_column(String(32), nullable=False)
    model: Mapped[str] = mapped_column(String(64), nullable=False)

    started_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, server_default=func.now())
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    input_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    output_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)

    status: Mapped[str] = mapped_column(String(16), nullable=False, default="running")
    error: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Validated structured JSON as returned by the LLM.  NULL on error.
    # Never contains the prompt or the raw transcript.
    output_json: Mapped[str | None] = mapped_column(LongText, nullable=True)

    # Gate for "new discussion since last successful run".
    last_discussion_segment_id: Mapped[int | None] = mapped_column(Integer, nullable=True)

    __table_args__ = (
        Index("ix_ai_analysis_runs_meeting_started", "meeting_id", "started_at"),
    )


class MeetingStateSnapshot(Base):
    __tablename__ = "meeting_state_snapshots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    meeting_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("meetings.id", ondelete="CASCADE"), nullable=False
    )
    run_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("ai_analysis_runs.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, server_default=func.now())

    # Post-reducer state the app accepted.  Valid structured JSON.
    state_json: Mapped[str] = mapped_column(LongText, nullable=False)

    __table_args__ = (
        Index("ix_state_snapshots_meeting_created", "meeting_id", "created_at"),
    )


class Fact(Base):
    __tablename__ = "facts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    meeting_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("meetings.id", ondelete="CASCADE"), nullable=False
    )
    key: Mapped[str] = mapped_column(String(255), nullable=False)
    value: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)  # confirmed|tentative|disputed|superseded|inferred|unresolved
    # AI confidence, not objective truth probability.  0.0 .. 1.0
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)

    source_discussion_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("discussion_segments.id", ondelete="SET NULL"), nullable=True
    )
    source_text: Mapped[str | None] = mapped_column(Text, nullable=True)

    supersedes_fact_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("facts.id", ondelete="SET NULL"), nullable=True
    )

    first_seen_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, server_default=func.now())
    last_updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now(), onupdate=func.now()
    )


    meeting_relevance: Mapped[Optional[str]] = mapped_column(String(16), nullable=True)
    thread_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("threads.id", ondelete="SET NULL"), nullable=True
    )

    __table_args__ = (
        Index("ix_facts_meeting_key_status", "meeting_id", "key", "status"),
    )


class Decision(Base):
    __tablename__ = "decisions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    meeting_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("meetings.id", ondelete="CASCADE"), nullable=False
    )
    text: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="proposed")
    source_discussion_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("discussion_segments.id", ondelete="SET NULL"), nullable=True
    )
    source_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, server_default=func.now())
    last_updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now(), onupdate=func.now()
    )


    meeting_relevance: Mapped[Optional[str]] = mapped_column(String(16), nullable=True)
    thread_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("threads.id", ondelete="SET NULL"), nullable=True
    )

    __table_args__ = (Index("ix_decisions_meeting_status", "meeting_id", "status"),)


class ActionItem(Base):
    __tablename__ = "action_items"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    meeting_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("meetings.id", ondelete="CASCADE"), nullable=False
    )
    text: Mapped[str] = mapped_column(Text, nullable=False)
    owner: Mapped[str | None] = mapped_column(String(128), nullable=True)
    due: Mapped[str | None] = mapped_column(String(64), nullable=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="open")
    source_discussion_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("discussion_segments.id", ondelete="SET NULL"), nullable=True
    )
    source_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, server_default=func.now())
    last_updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now(), onupdate=func.now()
    )


    meeting_relevance: Mapped[Optional[str]] = mapped_column(String(16), nullable=True)
    thread_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("threads.id", ondelete="SET NULL"), nullable=True
    )
    previous_status: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)

    __table_args__ = (Index("ix_action_items_meeting_status", "meeting_id", "status"),)


class Risk(Base):
    __tablename__ = "risks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    meeting_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("meetings.id", ondelete="CASCADE"), nullable=False
    )
    text: Mapped[str] = mapped_column(Text, nullable=False)
    kind: Mapped[str] = mapped_column(String(16), nullable=False, default="risk")   # risk|issue
    severity: Mapped[str | None] = mapped_column(String(16), nullable=True)         # low|medium|high|critical
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="open")
    source_discussion_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("discussion_segments.id", ondelete="SET NULL"), nullable=True
    )
    source_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, server_default=func.now())
    last_updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now(), onupdate=func.now()
    )


    meeting_relevance: Mapped[Optional[str]] = mapped_column(String(16), nullable=True)
    thread_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("threads.id", ondelete="SET NULL"), nullable=True
    )

    __table_args__ = (Index("ix_risks_meeting_status", "meeting_id", "status"),)


class Question(Base):
    __tablename__ = "questions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    meeting_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("meetings.id", ondelete="CASCADE"), nullable=False
    )
    text: Mapped[str] = mapped_column(Text, nullable=False)
    category: Mapped[str] = mapped_column(String(8), nullable=False)   # now|later
    rationale: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="open")
    is_new: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    source_discussion_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("discussion_segments.id", ondelete="SET NULL"), nullable=True
    )
    source_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, server_default=func.now())
    last_updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now(), onupdate=func.now()
    )


    meeting_relevance: Mapped[Optional[str]] = mapped_column(String(16), nullable=True)
    thread_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("threads.id", ondelete="SET NULL"), nullable=True
    )

    __table_args__ = (
        Index("ix_questions_meeting_category_status", "meeting_id", "category", "status"),
    )
