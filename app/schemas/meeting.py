from __future__ import annotations

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field


class MeetingCreate(BaseModel):
    title: str = Field(min_length=1, max_length=255)
    objective: Optional[str] = None
    organization: Optional[str] = Field(default=None, max_length=255)
    role: Optional[str] = Field(default=None, max_length=64)
    notes: Optional[str] = None


class MeetingOut(BaseModel):
    id: str
    title: str
    objective: Optional[str] = None
    organization: Optional[str] = None
    role: Optional[str] = None
    notes: Optional[str] = None
    status: str
    started_at: Optional[datetime] = None
    ended_at: Optional[datetime] = None
    created_at: datetime
    updated_at: datetime
    last_segment_received_at: Optional[datetime] = None
    # G1: number of live WebSocket subscribers for this meeting (runtime value).
    ws_subscribers: int = 0

    model_config = {"from_attributes": True}


class MeetingList(BaseModel):
    items: list[MeetingOut]
    total: int
    limit: int
    offset: int
