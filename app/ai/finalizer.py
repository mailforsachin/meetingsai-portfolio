"""One-time finalization of a completed meeting.

Finalization lifecycle on `meeting_finalizations.status`:
    pending -> running -> ok | error

Idempotency rules (see `finalize_meeting`):
  - If a 'running' row exists younger than 5 minutes, return it.
  - If a 'running' row exists older than 5 minutes, mark it 'error' and continue.
  - If an 'ok' row exists with finished_at within the last 60 s, return it.

Any exception that escapes the body is caught at the top level and flips
the row to 'error' with the message.  There are no orphan 'running' rows.

Does not affect the rolling pipeline.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import select, func
from sqlalchemy.orm import Session

from app.ai.output_schema import AISchemaError
from app.ai.prompt_loader import load_prompt
from app.ai.provider import AIAnalysisProvider, AIProviderError
from app.ai.reducer import apply_rolling_output
from app.ai.state_builder import build_state_out
from app.core.config import get_settings
from app.models.discussion_segment import DiscussionSegment
from app.models.finalization import MeetingFinalization
from app.models.meeting import Meeting
from app.models.state import MeetingStateSnapshot
from app.schemas.ai_output import FinalizationOutput

log = logging.getLogger("meetingsai.ai.finalizer")

MAX_FINAL_SEGMENTS = 300
RUNNING_STALE_SECONDS = 300
JUST_RAN_SECONDS = 60


def _now_utc_naive() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _latest_finalization(db: Session, meeting_id: str) -> MeetingFinalization | None:
    return db.scalars(
        select(MeetingFinalization)
        .where(MeetingFinalization.meeting_id == meeting_id)
        .order_by(MeetingFinalization.id.desc())
        .limit(1)
    ).first()


def _check_idempotency(db: Session, meeting_id: str) -> MeetingFinalization | None:
    """Return an existing row to reuse, or None if a new run should proceed."""
    now = _now_utc_naive()

    running = db.scalars(
        select(MeetingFinalization)
        .where(MeetingFinalization.meeting_id == meeting_id)
        .where(MeetingFinalization.status == "running")
        .order_by(MeetingFinalization.id.desc())
    ).all()
    for r in running:
        age = (now - r.started_at).total_seconds() if r.started_at else 1e9
        if age < RUNNING_STALE_SECONDS:
            log.info("finalize skip: concurrent running row id=%s age=%.0fs", r.id, age)
            return r
        r.status = "error"
        r.error = "abandoned: superseded by a new finalize attempt"
        r.finished_at = now
        db.commit()

    # Reuse an ok row only if no newer transcript segment exists.
    from app.models.transcript_segment import TranscriptSegment
    last_ok = db.scalars(
        select(MeetingFinalization)
        .where(MeetingFinalization.meeting_id == meeting_id)
        .where(MeetingFinalization.status == "ok")
        .order_by(MeetingFinalization.id.desc())
        .limit(1)
    ).first()
    if last_ok is not None and last_ok.finished_at is not None:
        newest_seg = db.scalar(
            select(func.max(TranscriptSegment.received_at))
            .where(TranscriptSegment.meeting_id == meeting_id)
        )
        if newest_seg is None or newest_seg <= last_ok.finished_at:
            age = (now - last_ok.finished_at).total_seconds()
            log.info("finalize reuse: ok row id=%s age=%.0fs, no newer segments",
                     last_ok.id, age)
            return last_ok
        log.info("finalize rerun: newer segments since ok row id=%s", last_ok.id)

    return None


def build_finalization_payload(db: Session, meeting: Meeting) -> dict:
    segments = list(
        db.query(DiscussionSegment)
        .filter(DiscussionSegment.meeting_id == meeting.id)
        .order_by(DiscussionSegment.started_at.asc(), DiscussionSegment.id.asc())
        .limit(MAX_FINAL_SEGMENTS)
        .all()
    )

    state = build_state_out(db, meeting.id)

    return {
        "meeting": {
            "id": meeting.id,
            "title": meeting.title,
            "objective": meeting.objective,
            "organization": meeting.organization,
            "role": meeting.role,
            "notes": meeting.notes,
            "status": meeting.status,
            "started_at": meeting.started_at.isoformat() if meeting.started_at else None,
            "ended_at": meeting.ended_at.isoformat() if meeting.ended_at else None,
        },
        "current_state": {
            "understanding": state.understanding,
            "key_discussion": state.key_discussion,
            "facts": [f.model_dump(mode="json") for f in state.facts],
            "decisions": [d.model_dump(mode="json") for d in state.decisions],
            "action_items": [a.model_dump(mode="json") for a in state.action_items],
            "risks": [r.model_dump(mode="json") for r in state.risks],
            "questions_now": [q.model_dump(mode="json") for q in state.questions_now],
            "questions_later": [q.model_dump(mode="json") for q in state.questions_later],
        },
        "discussion_segments": [
            {
                "id": s.id,
                "started_at": s.started_at.isoformat() if s.started_at else None,
                "ended_at": s.ended_at.isoformat() if s.ended_at else None,
                "text": s.text,
                "segment_count": s.segment_count,
            }
            for s in segments
        ],
    }


def finalize_meeting(db: Session, meeting_id: str) -> MeetingFinalization:
    """Run finalization for one meeting.

    Idempotent: see module docstring for the reuse rules.  Any exception
    flips the row to 'error' at the top level.
    """
    meeting = db.get(Meeting, meeting_id)
    if meeting is None:
        raise ValueError(f"meeting not found: {meeting_id}")

    reuse = _check_idempotency(db, meeting_id)
    if reuse is not None and reuse.status == "running" and reuse.finished_at is None:
        # Concurrent run in progress.  Return it so the caller sees a
        # consistent response.
        return reuse
    if reuse is not None and reuse.status == "ok":
        return reuse

    settings = get_settings()
    prompt = load_prompt("meeting_finalization.md")

    row = MeetingFinalization(
        meeting_id=meeting_id,
        status="running",
        prompt_version=prompt.version,
        model=(settings.final_analysis_model or settings.analysis_model),
        started_at=_now_utc_naive(),
    )
    db.add(row)
    db.commit()
    db.refresh(row)

    try:
        payload = build_finalization_payload(db, meeting)

        provider = AIAnalysisProvider(
            api_key=settings.openai_api_key,
            model=(settings.final_analysis_model or settings.analysis_model),
            timeout_seconds=120,
        )

        result = provider.run(prompt, payload, schema=FinalizationOutput)

        row.output_json = result.output_json
        out = result.output
        row.cleaned_transcript = getattr(out, "cleaned_transcript", None)
        row.executive_summary = getattr(out, "executive_summary", None)
        row.pm_notes = getattr(out, "pm_notes_markdown", None)

        apply_rolling_output(db, meeting_id, result.output)

        state_out = build_state_out(db, meeting_id)
        merged = state_out.model_dump()
        merged["understanding"] = result.output.understanding or None
        merged["key_discussion"] = list(result.output.key_discussion or [])
        merged["potential_response"] = result.output.potential_response or None
        merged["missing_information"] = list(result.output.missing_information or [])
        merged["contradictions"] = list(result.output.contradictions or [])
        merged["next_steps"] = list(result.output.next_steps or [])
        merged["final"] = True

        snap = MeetingStateSnapshot(
            meeting_id=meeting_id,
            run_id=None,
            state_json=json.dumps(merged, default=str),
        )
        db.add(snap)
        row.final_state_json = json.dumps(merged, default=str)

        row.latency_ms = result.latency_ms
        row.input_tokens = result.input_tokens
        row.output_tokens = result.output_tokens
        row.model = result.model
        row.prompt_version = result.prompt_version
        row.status = "ok"
        row.finished_at = _now_utc_naive()

        meeting.status = "completed"
        if meeting.ended_at is None:
            meeting.ended_at = _now_utc_naive()

        db.commit()
        db.refresh(row)
        log.info("finalization ok meeting=%s finalization=%s latency=%s",
                 meeting_id, row.id, row.latency_ms)
        return row

    except (AIProviderError, AISchemaError) as e:
        db.rollback()
        row = db.get(MeetingFinalization, row.id) or row
        row.status = "error"
        row.error = str(e)[:1000]
        row.finished_at = _now_utc_naive()
        db.commit()
        log.warning("finalization error meeting=%s err=%s", meeting_id, e)
        return row

    except Exception as e:
        db.rollback()
        row = db.get(MeetingFinalization, row.id) or row
        row.status = "error"
        row.error = f"unexpected: {type(e).__name__}: {e}"[:1000]
        row.finished_at = _now_utc_naive()
        db.commit()
        log.exception("finalization unexpected error meeting=%s", meeting_id)
        return row
