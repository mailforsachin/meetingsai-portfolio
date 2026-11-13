from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.db.types import LongText


def _now_utc_naive() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


class MeetingFinalization(Base):
    __tablename__ = "meeting_finalizations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    meeting_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("meetings.id", ondelete="CASCADE"), nullable=False
    )
    run_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("ai_analysis_runs.id", ondelete="SET NULL"), nullable=True
    )
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    error: Mapped[str | None] = mapped_column(Text, nullable=True)

    prompt_version: Mapped[str] = mapped_column(String(32), nullable=False)
    model: Mapped[str] = mapped_column(String(64), nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=_now_utc_naive)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    input_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    output_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)

    cleaned_transcript: Mapped[str | None] = mapped_column(LongText, nullable=True)
    executive_summary: Mapped[str | None] = mapped_column(LongText, nullable=True)
    pm_notes: Mapped[str | None] = mapped_column(LongText, nullable=True)
    final_state_json: Mapped[str | None] = mapped_column(LongText, nullable=True)
    output_json: Mapped[str | None] = mapped_column(LongText, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=_now_utc_naive)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=_now_utc_naive, onupdate=_now_utc_naive
    )

    __table_args__ = (
        Index("ix_meeting_finalizations_meeting_status", "meeting_id", "status"),
    )
