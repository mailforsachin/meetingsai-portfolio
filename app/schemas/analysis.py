from __future__ import annotations

from datetime import datetime
from typing import Optional

from pydantic import BaseModel


class AnalysisRunOut(BaseModel):
    id: int
    meeting_id: str
    kind: str
    prompt_version: str
    model: str
    started_at: datetime
    finished_at: Optional[datetime] = None
    input_tokens: Optional[int] = None
    output_tokens: Optional[int] = None
    latency_ms: Optional[int] = None
    status: str
    error: Optional[str] = None
    last_discussion_segment_id: Optional[int] = None
    # output_json is intentionally NOT returned by the list endpoint.
    # A separate endpoint may expose it for forensic review.

    model_config = {"from_attributes": True}


class AnalysisRunList(BaseModel):
    items: list[AnalysisRunOut]
    total: int
    limit: int
    offset: int
