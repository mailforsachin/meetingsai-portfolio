from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.ai.finalizer import finalize_meeting
from app.core.config import get_settings
from app.core.deps import require_user
from app.db.session import SessionLocal, get_db
from app.models.discussion_segment import DiscussionSegment
from app.models.meeting import Meeting
from app.models.transcript_segment import TranscriptSegment
from app.schemas.discussion import DiscussionList, DiscussionSegmentOut
from app.schemas.meeting import MeetingCreate, MeetingList, MeetingOut
from app.schemas.transcript import (
    TranscriptBatchIn,
    TranscriptBatchOut,
    TranscriptList,
    TranscriptSegmentOut,
)
from app.services.aggregator import flush_all
from app.websocket.manager import manager

log = logging.getLogger("meetingsai.api.meetings")

router = APIRouter(prefix="/api/meetings", tags=["meetings"], dependencies=[Depends(require_user)])


def _get_or_404(db: Session, meeting_id: str) -> Meeting:
    m = db.get(Meeting, meeting_id)
    if m is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="meeting not found")
    return m


async def _run_finalization_background(meeting_id: str) -> None:
    """Fire-and-forget finalization.  Never raises into the caller.

    We obtain the session factory via a runtime module lookup so that
    tests that override app.db.session.SessionLocal (monkeypatch) affect
    this code path too.  Do not replace with a top-level `from ... import
    SessionLocal`, which would bind the original object.
    """
    import app.db.session as _session_mod
    db = _session_mod.SessionLocal()
    try:
        row = finalize_meeting(db, meeting_id)
        # Terminal status on success: the meeting is finished.
        m = db.get(Meeting, meeting_id)
        if m is not None and m.status == "finalizing":
            m.status = "ended"
            db.commit()
        await manager.broadcast_json(
            meeting_id,
            {
                "type": "finalization",
                "meeting_id": meeting_id,
                "status": row.status,
                "finalization_id": row.id,
                "error": row.error,
            },
        )
    except Exception as e:
        log.warning("background finalization error meeting=%s err=%s", meeting_id, e)
        # Fail-safe: reset status so the meeting is retryable.  Without
        # this, a failed finalization leaves the meeting stuck at
        # 'finalizing' forever and blocks any further attempt.
        try:
            m = db.get(Meeting, meeting_id)
            if m is not None and m.status == "finalizing":
                m.status = "stopped"
                db.commit()
        except Exception:
            try:
                db.rollback()
            except Exception:
                pass
        try:
            await manager.broadcast_json(
                meeting_id,
                {"type": "finalization", "meeting_id": meeting_id,
                 "status": "error", "finalization_id": None, "error": str(e)[:200]},
            )
        except Exception:
            pass
    finally:
        db.close()


@router.post("", response_model=MeetingOut, status_code=status.HTTP_201_CREATED)
def create_meeting(payload: MeetingCreate, db: Session = Depends(get_db)) -> Meeting:
    m = Meeting(
        title=payload.title, objective=payload.objective,
        organization=payload.organization, role=payload.role,
        notes=payload.notes, status="created",
        topic_intelligence_eligible=get_settings().topic_intelligence_enabled,
    )
    db.add(m); db.commit(); db.refresh(m)
    return m


@router.get("", response_model=MeetingList)
def list_meetings(db: Session = Depends(get_db),
                  limit: int = Query(default=20, ge=1, le=100),
                  offset: int = Query(default=0, ge=0)) -> MeetingList:
    total = db.scalar(select(func.count()).select_from(Meeting)) or 0
    rows = db.scalars(
        select(Meeting).order_by(Meeting.created_at.desc(), Meeting.id.desc()).limit(limit).offset(offset)
    ).all()
    return MeetingList(items=[MeetingOut.model_validate(r) for r in rows],
                       total=int(total), limit=limit, offset=offset)


def _meeting_to_out(db: Session, m: Meeting) -> MeetingOut:
    last = db.scalar(
        select(func.max(TranscriptSegment.received_at))
        .where(TranscriptSegment.meeting_id == m.id)
    )
    # The DB stores naive UTC.  Attach UTC explicitly so Pydantic serializes
    # with a timezone offset and the browser parses it correctly.
    if last is not None and last.tzinfo is None:
        from datetime import timezone as _tz
        last = last.replace(tzinfo=_tz.utc)
    out = MeetingOut.model_validate(m)
    out.last_segment_received_at = last
    # G1: live WebSocket subscriber count (runtime value from the in-process manager).
    try:
        from app.websocket.manager import manager as _ws_manager
        out.ws_subscribers = _ws_manager.subscriber_count(m.id)
    except Exception:
        out.ws_subscribers = 0
    return out


@router.get("/{meeting_id}", response_model=MeetingOut)
def get_meeting(meeting_id: str, db: Session = Depends(get_db)) -> MeetingOut:
    m = _get_or_404(db, meeting_id)
    return _meeting_to_out(db, m)


@router.post("/{meeting_id}/start", response_model=MeetingOut)
def start_meeting(meeting_id: str, db: Session = Depends(get_db)) -> Meeting:
    m = _get_or_404(db, meeting_id)
    if m.status in ("stopping", "finalizing", "ended", "completed"):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT,
                            detail=f"cannot start meeting in status {m.status!r}")
    if m.status == "active":
        return m
    m.status = "active"
    m.started_at = datetime.now(timezone.utc).replace(tzinfo=None)
    db.commit(); db.refresh(m)
    return m


@router.post("/{meeting_id}/stop", response_model=MeetingOut)
async def stop_meeting(meeting_id: str, db: Session = Depends(get_db)) -> Meeting:
    m = _get_or_404(db, meeting_id)

    # Idempotent: if finalization already in flight or done, return as-is.
    if m.status in ("finalizing", "completed", "ended"):
        return m

    settings = get_settings()

    m.status = "stopping"
    db.commit(); db.refresh(m)

    try:
        created = flush_all(
            db, meeting_id,
            window_seconds=settings.aggregation_window_seconds,
            max_segments=settings.aggregation_max_segments_per_run,
        )
    except Exception:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="aggregation flush failed; meeting remains in 'stopping'",
        )

    for ds in created:
        try:
            await manager.broadcast_json(meeting_id, {
                "type": "discussion", "meeting_id": meeting_id,
                "discussion_id": ds.id,
                "started_at": ds.started_at.isoformat() if ds.started_at else None,
                "ended_at": ds.ended_at.isoformat() if ds.ended_at else None,
                "text": ds.text, "segment_count": ds.segment_count,
                "token_estimate": ds.token_estimate,
                "processing_status": ds.processing_status,
            })
        except Exception:
            pass

    remaining = db.scalar(
        select(func.count())
        .select_from(TranscriptSegment)
        .where(TranscriptSegment.meeting_id == meeting_id)
        .where(TranscriptSegment.discussion_segment_id.is_(None))
    ) or 0

    if remaining == 0:
        m.status = "finalizing"
        m.ended_at = datetime.now(timezone.utc).replace(tzinfo=None)
        db.commit(); db.refresh(m)

        # Finalization runs synchronously when FINALIZE_SYNC=1 (test mode);
        # otherwise it is fire-and-forget so /stop returns immediately.
        import os
        if os.environ.get("FINALIZE_SYNC") == "1":
            await _run_finalization_background(meeting_id)
            # The background task used its own session; refresh this one so
            # the returned meeting reflects the finalization outcome.
            db.refresh(m)
            log.info("synchronous finalization complete meeting=%s status=%s",
                     meeting_id, m.status)
        else:
            asyncio.create_task(_run_finalization_background(meeting_id))
            log.info("background finalization scheduled meeting=%s", meeting_id)
    return m


@router.post("/{meeting_id}/transcript", response_model=TranscriptBatchOut)
async def ingest_transcript(meeting_id: str, payload: TranscriptBatchIn,
                            db: Session = Depends(get_db)) -> TranscriptBatchOut:
    _get_or_404(db, meeting_id)
    incoming_ids = [s.segment_id for s in payload.segments]
    existing = set(db.scalars(
        select(TranscriptSegment.segment_id).where(TranscriptSegment.segment_id.in_(incoming_ids))
    ).all())

    accepted = 0; duplicates = 0; accepted_rows: list[TranscriptSegment] = []
    for s in payload.segments:
        if s.segment_id in existing:
            duplicates += 1; continue
        row = TranscriptSegment(
            meeting_id=meeting_id, segment_id=s.segment_id,
            speaker_id=s.speaker_id, speaker_name=s.speaker_name,
            started_at=s.started_at, ended_at=s.ended_at,
            text=s.text, model=s.model,
        )
        db.add(row)
        try:
            db.flush(); accepted += 1; accepted_rows.append(row)
        except IntegrityError:
            db.rollback(); duplicates += 1; accepted_rows.clear()
            existing = set(db.scalars(
                select(TranscriptSegment.segment_id).where(TranscriptSegment.segment_id.in_(incoming_ids))
            ).all())
    db.commit()
    for row in accepted_rows:
        db.refresh(row)
    for row in accepted_rows:
        try:
            await manager.broadcast_json(meeting_id, {
                "type": "segment", "meeting_id": meeting_id,
                "segment_id": row.segment_id, "text": row.text,
                "started_at": row.started_at.isoformat() if row.started_at else None,
                "received_at": row.received_at.isoformat() if row.received_at else None,
                "model": row.model,
            })
        except Exception:
            pass
    return TranscriptBatchOut(accepted=accepted, duplicates=duplicates)


@router.get("/{meeting_id}/transcript", response_model=TranscriptList)
def list_transcript(meeting_id: str, db: Session = Depends(get_db),
                    limit: int = Query(default=100, ge=1, le=500),
                    offset: int = Query(default=0, ge=0)) -> TranscriptList:
    _get_or_404(db, meeting_id)
    total = db.scalar(select(func.count()).select_from(TranscriptSegment)
                      .where(TranscriptSegment.meeting_id == meeting_id)) or 0
    rows = db.scalars(
        select(TranscriptSegment)
        .where(TranscriptSegment.meeting_id == meeting_id)
        .order_by(TranscriptSegment.received_at.asc(), TranscriptSegment.id.asc())
        .limit(limit).offset(offset)
    ).all()
    return TranscriptList(items=[TranscriptSegmentOut.model_validate(r) for r in rows],
                          total=int(total), limit=limit, offset=offset)


@router.get("/{meeting_id}/discussions", response_model=DiscussionList)
def list_discussions(meeting_id: str, db: Session = Depends(get_db),
                     limit: int = Query(default=100, ge=1, le=500),
                     offset: int = Query(default=0, ge=0)) -> DiscussionList:
    _get_or_404(db, meeting_id)
    total = db.scalar(select(func.count()).select_from(DiscussionSegment)
                      .where(DiscussionSegment.meeting_id == meeting_id)) or 0
    rows = db.scalars(
        select(DiscussionSegment)
        .where(DiscussionSegment.meeting_id == meeting_id)
        .order_by(DiscussionSegment.started_at.asc(), DiscussionSegment.id.asc())
        .limit(limit).offset(offset)
    ).all()
    return DiscussionList(items=[DiscussionSegmentOut.model_validate(r) for r in rows],
                          total=int(total), limit=limit, offset=offset)
