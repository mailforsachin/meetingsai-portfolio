"""Context Review scheduler.

Third asyncio task in the app alongside the aggregation and analysis
schedulers.  Every `context_review_interval_seconds`, for each meeting
whose status is active or stopping:

  1. Skip if a context_review_runs row is currently running for that
     meeting.
  2. Skip if the meaningful-change gate says there is nothing new.
  3. Otherwise: build the payload, call the model with the
     ContextReviewOutput schema, apply the reducer, write the run row,
     broadcast a WS context_review event.

The scheduler never raises out of its loop.  Per-meeting errors are
logged and the loop continues.
"""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import func, select

from app.ai.context_review_reducer import apply_context_review
from app.ai.output_schema import AISchemaError
from app.ai.prompt_loader import load_prompt
from app.ai.provider import AIAnalysisProvider, AIProviderError
from app.core.config import get_settings
from app.db.session import SessionLocal
from app.models.context_review import ContextReviewRun
from app.models.discussion_segment import DiscussionSegment
from app.models.meeting import Meeting
from app.models.meeting_delta import MeetingDelta
from app.models.state import (
    ActionItem,
    Decision,
    Fact,
    Question,
    Risk,
)
from app.models.thread import Thread
from app.models.transcript_segment import TranscriptSegment
from app.schemas.context_review import ContextReviewOutput
from app.websocket.manager import manager

log = logging.getLogger("meetingsai.ai.context_review_scheduler")


def _now_utc_naive() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _last_ok_run(db, meeting_id: str) -> Optional[ContextReviewRun]:
    return db.scalars(
        select(ContextReviewRun)
        .where(ContextReviewRun.meeting_id == meeting_id)
        .where(ContextReviewRun.status == "ok")
        .order_by(ContextReviewRun.started_at.desc(), ContextReviewRun.id.desc())
        .limit(1)
    ).first()


def _running_run_exists(db, meeting_id: str) -> bool:
    row = db.scalars(
        select(ContextReviewRun)
        .where(ContextReviewRun.meeting_id == meeting_id)
        .where(ContextReviewRun.status == "running")
        .limit(1)
    ).first()
    return row is not None


def _newest_discussion_id(db, meeting_id: str) -> Optional[int]:
    return db.scalar(
        select(func.max(DiscussionSegment.id))
        .where(DiscussionSegment.meeting_id == meeting_id)
    )


def _count_new_discussions(db, meeting_id: str, after_id: int) -> int:
    return int(db.scalar(
        select(func.count())
        .select_from(DiscussionSegment)
        .where(DiscussionSegment.meeting_id == meeting_id)
        .where(DiscussionSegment.id > after_id)
    ) or 0)


def _should_run(db, meeting_id: str) -> tuple[bool, str, Optional[int]]:
    """Return (should_run, reason, newest_discussion_id)."""
    settings = get_settings()

    if not settings.context_review_enabled:
        return False, "disabled", None

    if _running_run_exists(db, meeting_id):
        return False, "already running", None

    newest = _newest_discussion_id(db, meeting_id)
    if newest is None:
        return False, "no discussion segments yet", None

    last = _last_ok_run(db, meeting_id)
    if last is None:
        return True, "first review", newest

    last_to = last.evidence_to_segment_id or 0
    if newest <= last_to:
        return False, "no new discussion since last review", newest

    new_count = _count_new_discussions(db, meeting_id, last_to)
    if new_count < settings.context_review_min_new_segments:
        return False, f"only {new_count} new discussions (need >= {settings.context_review_min_new_segments})", newest

    return True, f"{new_count} new discussions", newest


def _build_payload(db, meeting: Meeting, from_id: Optional[int]) -> tuple[dict, Optional[int], Optional[int]]:
    """Return (payload, from_segment_id, to_segment_id).

    The window is the recent N discussions whose id <= newest.  If from_id
    is provided and any discussion has id <= from_id, the window starts
    after from_id (bounded by recent_discussions).
    """
    settings = get_settings()

    discussions = list(db.scalars(
        select(DiscussionSegment)
        .where(DiscussionSegment.meeting_id == meeting.id)
        .order_by(DiscussionSegment.id.desc())
        .limit(settings.context_review_recent_discussions)
    ).all())
    discussions.reverse()
    to_segment = max((d.id for d in discussions), default=None)
    from_segment = min((d.id for d in discussions), default=None)

    # Fallback transcript window (last N transcript segments).
    transcripts = list(db.scalars(
        select(TranscriptSegment)
        .where(TranscriptSegment.meeting_id == meeting.id)
        .order_by(TranscriptSegment.id.desc())
        .limit(settings.context_review_recent_transcripts)
    ).all())
    transcripts.reverse()

    # Current state rows.
    actions = list(db.scalars(
        select(ActionItem)
        .where(ActionItem.meeting_id == meeting.id)
        .where(ActionItem.status == "open")
        .order_by(ActionItem.last_updated_at.desc())
        .limit(50)
    ).all())
    questions = list(db.scalars(
        select(Question)
        .where(Question.meeting_id == meeting.id)
        .where(Question.status == "open")
        .order_by(Question.last_updated_at.desc())
        .limit(50)
    ).all())
    risks = list(db.scalars(
        select(Risk)
        .where(Risk.meeting_id == meeting.id)
        .where(Risk.status == "open")
        .order_by(Risk.last_updated_at.desc())
        .limit(50)
    ).all())
    decisions = list(db.scalars(
        select(Decision)
        .where(Decision.meeting_id == meeting.id)
        .where(Decision.status == "proposed")
        .order_by(Decision.last_updated_at.desc())
        .limit(50)
    ).all())
    threads = list(db.scalars(
        select(Thread)
        .where(Thread.meeting_id == meeting.id)
        .where(Thread.status == "open")
        .order_by(Thread.updated_at.desc())
        .limit(50)
    ).all())

    # Thread counts.
    def counts(model):
        rows = db.execute(
            select(getattr(model, "thread_id"), func.count())
            .where(model.meeting_id == meeting.id)
            .group_by(getattr(model, "thread_id"))
        ).all()
        return {tid: int(n) for tid, n in rows if tid is not None}

    ac = counts(ActionItem); qc = counts(Question); rc = counts(Risk)
    dc = counts(Decision);  fc = counts(Fact)

    # Recent deltas.
    deltas = list(db.scalars(
        select(MeetingDelta)
        .where(MeetingDelta.meeting_id == meeting.id)
        .order_by(MeetingDelta.created_at.desc())
        .limit(3)
    ).all())

    # Previous context review output (for dedup hints).
    last_review = _last_ok_run(db, meeting.id)
    last_review_payload = None
    if last_review and last_review.output_json:
        try:
            last_review_payload = json.loads(last_review.output_json)
        except json.JSONDecodeError:
            last_review_payload = None

    payload = {
        "meeting": {
            "id": meeting.id,
            "title": meeting.title,
            "objective": meeting.objective,
            "mode": getattr(meeting, "mode", None),
            "organization": meeting.organization,
            "role": meeting.role,
            "notes": meeting.notes,
            "status": meeting.status,
        },
        "current_state": {
            "open_actions": [
                {"id": a.id, "text": a.text, "owner": a.owner, "due": a.due,
                 "status": a.status, "thread_id": a.thread_id}
                for a in actions
            ],
            "open_questions": [
                {"id": q.id, "text": q.text, "category": q.category,
                 "rationale": q.rationale, "thread_id": q.thread_id}
                for q in questions
            ],
            "open_risks": [
                {"id": r.id, "text": r.text, "kind": r.kind, "severity": r.severity,
                 "thread_id": r.thread_id}
                for r in risks
            ],
            "proposed_decisions": [
                {"id": d.id, "text": d.text, "thread_id": d.thread_id}
                for d in decisions
            ],
            "active_threads": [
                {"id": t.id, "key": t.key, "title": t.title, "summary": t.summary,
                 "status": t.status,
                 "action_count": ac.get(t.id, 0),
                 "question_count": qc.get(t.id, 0),
                 "risk_count": rc.get(t.id, 0),
                 "decision_count": dc.get(t.id, 0),
                 "fact_count": fc.get(t.id, 0)}
                for t in threads
            ],
        },
        "recent_discussions": [
            {"id": d.id,
             "started_at": d.started_at.isoformat() if d.started_at else None,
             "ended_at": d.ended_at.isoformat() if d.ended_at else None,
             "text": d.text,
             "segment_count": d.segment_count}
            for d in discussions
        ],
        "recent_transcripts": [
            {"id": t.id,
             "started_at": t.started_at.isoformat() if t.started_at else None,
             "text": t.text}
            for t in transcripts
        ],
        "recent_deltas": [
            json.loads(d.delta_json) for d in deltas if d.delta_json
        ],
        "last_review": last_review_payload,
    }
    return payload, from_segment, to_segment


async def _run_one(db, meeting_id: str) -> None:
    settings = get_settings()
    meeting = db.get(Meeting, meeting_id)
    if meeting is None or meeting.status not in ("active", "stopping"):
        return

    should, reason, newest_id = _should_run(db, meeting_id)
    if not should:
        log.debug("skip context review meeting=%s reason=%s", meeting_id, reason)
        return

    payload, from_segment, to_segment = _build_payload(db, meeting, None)

    prompt = load_prompt("context_review.md")

    run = ContextReviewRun(
        meeting_id=meeting_id,
        prompt_version=prompt.version,
        model=settings.context_review_model,
        status="running",
        started_at=_now_utc_naive(),
        evidence_from_segment_id=from_segment,
        evidence_to_segment_id=to_segment,
    )
    db.add(run); db.commit(); db.refresh(run)

    provider = AIAnalysisProvider(
        api_key=settings.openai_api_key,
        model=settings.context_review_model,
    )
    try:
        result = provider.run(prompt, payload, schema=ContextReviewOutput)
    except (AIProviderError, AISchemaError) as e:
        run.status = "error"
        run.error = str(e)[:1000]
        run.finished_at = _now_utc_naive()
        db.commit()
        log.warning("context review error meeting=%s err=%s", meeting_id, e)
        return

    run.output_json = result.output_json
    run.latency_ms = result.latency_ms
    run.input_tokens = result.input_tokens
    run.output_tokens = result.output_tokens

    try:
        s = apply_context_review(db, meeting_id, run.id, result.output)
        run.status = "ok"
        run.finished_at = _now_utc_naive()
        db.commit()
    except Exception as e:
        db.rollback()
        run = db.get(ContextReviewRun, run.id) or run
        run.status = "error"
        run.error = f"reducer failed: {e}"[:1000]
        run.finished_at = _now_utc_naive()
        db.commit()
        log.warning("context review reducer error meeting=%s err=%s", meeting_id, e)
        return

    try:
        await manager.broadcast_json(
            meeting_id,
            {"type": "context_review", "meeting_id": meeting_id,
             "run_id": run.id, "gap_count": s.created + s.updated},
        )
    except Exception:
        pass

    log.info(
        "context review ok meeting=%s run=%s created=%d updated=%d skipped=%d latency=%s",
        meeting_id, run.id, s.created, s.updated, s.skipped_resolved, run.latency_ms,
    )


async def run_context_review_scheduler(stop_event: asyncio.Event) -> None:
    settings = get_settings()
    interval = max(15, int(settings.context_review_interval_seconds))
    log.info("context review scheduler starting; interval=%ss model=%s enabled=%s",
             interval, settings.context_review_model, settings.context_review_enabled)
    try:
        while not stop_event.is_set():
            if not settings.context_review_enabled:
                try:
                    await asyncio.wait_for(stop_event.wait(), timeout=60)
                except asyncio.TimeoutError:
                    pass
                continue

            db = SessionLocal()
            try:
                meetings = list(db.scalars(
                    select(Meeting).where(Meeting.status.in_(("active", "stopping")))
                ).all())
            finally:
                db.close()

            for m in meetings:
                db = SessionLocal()
                try:
                    await _run_one(db, m.id)
                except Exception as e:
                    log.warning("context review loop error meeting=%s err=%s", m.id, e)
                finally:
                    db.close()

            try:
                await asyncio.wait_for(stop_event.wait(), timeout=interval)
            except asyncio.TimeoutError:
                pass
    finally:
        log.info("context review scheduler stopped")
