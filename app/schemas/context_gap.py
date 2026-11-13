from __future__ import annotations

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field


class ContextGapOut(BaseModel):
    id: int
    meeting_id: str
    gap_kind: str
    category: str
    text: str
    rationale: Optional[str] = None
    thread_id: Optional[int] = None
    urgency: str
    status: str
    first_seen_at: datetime
    last_seen_at: datetime
    source_discussion_id: Optional[int] = None
    source_text: Optional[str] = None

    model_config = {"from_attributes": True}


class ContextGapList(BaseModel):
    items: list[ContextGapOut] = Field(default_factory=list)
    total: int
    limit: int
    offset: int
