from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.db.types import LongText


class ContextGap(Base):
    __tablename__ = "context_gaps"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    meeting_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("meetings.id", ondelete="CASCADE"), nullable=False
    )
    run_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("context_review_runs.id", ondelete="SET NULL"), nullable=True
    )
    gap_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    category: Mapped[str] = mapped_column(String(32), nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    rationale: Mapped[str | None] = mapped_column(Text, nullable=True)
    thread_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("threads.id", ondelete="SET NULL"), nullable=True
    )
    urgency: Mapped[str] = mapped_column(String(16), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="open")
    first_seen_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now()
    )
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now(), onupdate=func.now()
    )
    evidence_from_run_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("context_review_runs.id", ondelete="SET NULL"), nullable=True
    )
    source_discussion_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("discussion_segments.id", ondelete="SET NULL"), nullable=True
    )
    source_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    provenance_json: Mapped[str | None] = mapped_column(LongText, nullable=True)
    norm_hash: Mapped[str] = mapped_column(String(64), nullable=False)

    __table_args__ = (
        Index("ix_context_gaps_meeting_status_urgency", "meeting_id", "status", "urgency"),
        Index("ix_context_gaps_meeting_hash_status", "meeting_id", "norm_hash", "status"),
    )
