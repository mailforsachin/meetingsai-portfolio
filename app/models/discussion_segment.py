from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class DiscussionSegment(Base):
    """A ~30 s deterministic aggregation of transcript_segments.

    This is a derived layer.  The underlying transcript_segments rows
    remain intact and queryable, and point at this row via
    transcript_segments.discussion_segment_id.
    """

    __tablename__ = "discussion_segments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    meeting_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("meetings.id", ondelete="CASCADE"), nullable=False
    )

    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    text: Mapped[str] = mapped_column(Text, nullable=False)
    segment_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    # token_estimate is a rough planning metric (len(text)//4).
    # It is NOT authoritative tokenizer output.
    token_estimate: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    processing_status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="pending"
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (
        Index("ix_discussion_segments_meeting_started", "meeting_id", "started_at"),
    )
