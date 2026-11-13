"""Background scheduler that runs the deterministic aggregator.

Runs as a single asyncio task attached to FastAPI's lifespan.  Wakes every
`interval_seconds`, iterates meetings with status 'active' or 'stopping',
aggregates one ripe window per meeting, and broadcasts a `discussion` WS
message for each newly created discussion segment.

The scheduler never raises out of its loop; any per-meeting error is
logged and the loop continues.
"""

from __future__ import annotations

import asyncio
import logging

from sqlalchemy import select

from app.core.config import get_settings
from app.db.session import SessionLocal
from app.models.meeting import Meeting
from app.services.aggregator import aggregate_mature_window
from app.ai.analysis_scheduler import notify_new_discussion
from app.websocket.manager import manager

log = logging.getLogger("meetingsai.scheduler")


def _discussion_payload(ds) -> dict:
    return {
        "type": "discussion",
        "meeting_id": ds.meeting_id,
        "discussion_id": ds.id,
        "started_at": ds.started_at.isoformat() if ds.started_at else None,
        "ended_at": ds.ended_at.isoformat() if ds.ended_at else None,
        "text": ds.text,
        "segment_count": ds.segment_count,
        "token_estimate": ds.token_estimate,
        "processing_status": ds.processing_status,
    }


async def _tick() -> None:
    settings = get_settings()
    db = SessionLocal()
    try:
        meetings = list(
            db.scalars(
                select(Meeting).where(Meeting.status.in_(("active", "stopping")))
            ).all()
        )
    finally:
        db.close()

    for m in meetings:
        db = SessionLocal()
        try:
            ds = aggregate_mature_window(
                db,
                m.id,
                window_seconds=settings.aggregation_window_seconds,
                max_segments=settings.aggregation_max_segments_per_run,
            )
            if ds is not None:
                await manager.broadcast_json(m.id, _discussion_payload(ds))
                notify_new_discussion(m.id)
        except Exception as e:
            log.warning("aggregate tick error meeting=%s err=%s", m.id, e)
        finally:
            db.close()


async def run_scheduler(stop_event: asyncio.Event) -> None:
    settings = get_settings()
    interval = max(1, int(settings.aggregation_scheduler_interval_seconds))
    log.info("scheduler starting; interval=%ss window=%ss",
             interval, settings.aggregation_window_seconds)
    try:
        while not stop_event.is_set():
            try:
                await _tick()
            except Exception as e:
                log.warning("scheduler tick top-level error: %s", e)
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=interval)
            except asyncio.TimeoutError:
                pass
    finally:
        log.info("scheduler stopped")
