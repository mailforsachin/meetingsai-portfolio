from __future__ import annotations

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field


class TranscriptSegmentIn(BaseModel):
    segment_id: str = Field(min_length=1, max_length=64)
    text: str = Field(min_length=1)
    started_at: Optional[datetime] = None
    ended_at: Optional[datetime] = None
    speaker_id: Optional[str] = Field(default=None, max_length=64)
    speaker_name: Optional[str] = Field(default=None, max_length=128)
    model: Optional[str] = Field(default=None, max_length=64)


class TranscriptBatchIn(BaseModel):
    segments: list[TranscriptSegmentIn] = Field(min_length=1, max_length=200)


class TranscriptSegmentOut(BaseModel):
    id: int
    segment_id: str
    meeting_id: str
    speaker_id: Optional[str] = None
    speaker_name: Optional[str] = None
    started_at: Optional[datetime] = None
    ended_at: Optional[datetime] = None
    text: str
    model: Optional[str] = None
    received_at: datetime

    model_config = {"from_attributes": True}


class TranscriptBatchOut(BaseModel):
    accepted: int
    duplicates: int


class TranscriptList(BaseModel):
    items: list[TranscriptSegmentOut]
    total: int
    limit: int
    offset: int
