from __future__ import annotations

from datetime import datetime
from typing import Optional

from pydantic import BaseModel


class DiscussionSegmentOut(BaseModel):
    id: int
    meeting_id: str
    started_at: Optional[datetime] = None
    ended_at: Optional[datetime] = None
    text: str
    segment_count: int
    token_estimate: int
    processing_status: str
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class DiscussionList(BaseModel):
    items: list[DiscussionSegmentOut]
    total: int
    limit: int
    offset: int
