from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class TranscriptSegment(Base):
    __tablename__ = "transcript_segments"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    meeting_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("meetings.id", ondelete="CASCADE"), nullable=False, index=True
    )
    segment_id: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)

    speaker_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    speaker_name: Mapped[str | None] = mapped_column(String(128), nullable=True)

    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    model: Mapped[str | None] = mapped_column(String(64), nullable=True)

    received_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now()
    )

    # Assigned by the aggregator (Phase 8). Nullable until aggregated.
    discussion_segment_id: Mapped[int | None] = mapped_column(
        Integer,
        ForeignKey("discussion_segments.id", ondelete="SET NULL"),
        nullable=True,
    )

    __table_args__ = (
        Index("ix_transcript_segments_meeting_received", "meeting_id", "received_at"),
        Index("ix_transcript_segments_discussion", "discussion_segment_id"),
    )
