"""Finalization endpoints.

Four endpoints:
  POST /api/meetings/{id}/finalize           run the pipeline synchronously
  GET  /api/meetings/{id}/finalization       return the latest finalization row
  GET  /api/meetings/{id}/export.json        full structured export
  GET  /api/meetings/{id}/export.md          single Markdown export

All require authentication.  All are scoped to the meeting in the path.
"""
from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import PlainTextResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.ai.finalizer import finalize_meeting
from app.ai.state_builder import build_state_out
from app.core.deps import require_user
from app.db.session import get_db
from app.models.discussion_segment import DiscussionSegment
from app.models.finalization import MeetingFinalization
from app.models.meeting import Meeting
from app.models.transcript_segment import TranscriptSegment
from app.models.state import Decision, ActionItem, Risk, Fact, Question

router = APIRouter(
    prefix="/api/meetings",
    tags=["finalization"],
    dependencies=[Depends(require_user)],
)


def _get_meeting_or_404(db: Session, meeting_id: str) -> Meeting:
    m = db.get(Meeting, meeting_id)
    if m is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="meeting not found")
    return m


def _latest_finalization(db: Session, meeting_id: str) -> MeetingFinalization | None:
    return db.scalars(
        select(MeetingFinalization)
        .where(MeetingFinalization.meeting_id == meeting_id)
        .order_by(MeetingFinalization.id.desc())
        .limit(1)
    ).first()


@router.post("/{meeting_id}/finalize")
def finalize(meeting_id: str, db: Session = Depends(get_db)) -> dict:
    m = _get_meeting_or_404(db, meeting_id)
    if m.status not in ("finalizing", "completed", "active", "stopping", "ended"):
        # accept anything that is not 'created' with no transcript
        pass
    row = finalize_meeting(db, meeting_id)
    if row.status != "ok":
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"finalization failed: {row.error}",
        )
    return {
        "finalization_id": row.id,
        "status": row.status,
        "latency_ms": row.latency_ms,
        "input_tokens": row.input_tokens,
        "output_tokens": row.output_tokens,
        "prompt_version": row.prompt_version,
        "model": row.model,
    }


@router.get("/{meeting_id}/finalization")
def get_finalization(meeting_id: str, db: Session = Depends(get_db)) -> dict:
    _get_meeting_or_404(db, meeting_id)
    row = _latest_finalization(db, meeting_id)
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="no finalization yet")
    return {
        "id": row.id,
        "meeting_id": row.meeting_id,
        "run_id": row.run_id,
        "status": row.status,
        "error": row.error,
        "prompt_version": row.prompt_version,
        "model": row.model,
        "started_at": row.started_at.isoformat() if row.started_at else None,
        "finished_at": row.finished_at.isoformat() if row.finished_at else None,
        "latency_ms": row.latency_ms,
        "input_tokens": row.input_tokens,
        "output_tokens": row.output_tokens,
        "cleaned_transcript": row.cleaned_transcript,
        "executive_summary": row.executive_summary,
        "pm_notes": row.pm_notes,
    }


@router.get("/{meeting_id}/export.json")
def export_json(meeting_id: str, db: Session = Depends(get_db)) -> dict:
    m = _get_meeting_or_404(db, meeting_id)
    fin = _latest_finalization(db, meeting_id)
    state = build_state_out(db, meeting_id)

    raw_segments = list(db.scalars(
        select(TranscriptSegment)
        .where(TranscriptSegment.meeting_id == meeting_id)
        .order_by(TranscriptSegment.received_at.asc(), TranscriptSegment.id.asc())
    ).all())
    discussions = list(db.scalars(
        select(DiscussionSegment)
        .where(DiscussionSegment.meeting_id == meeting_id)
        .order_by(DiscussionSegment.started_at.asc(), DiscussionSegment.id.asc())
    ).all())

    return {
        "exported_at": datetime.utcnow().isoformat() + "Z",
        "meeting": {
            "id": m.id,
            "title": m.title,
            "objective": m.objective,
            "organization": m.organization,
            "role": m.role,
            "notes": m.notes,
            "status": m.status,
            "started_at": m.started_at.isoformat() if m.started_at else None,
            "ended_at": m.ended_at.isoformat() if m.ended_at else None,
        },
        "finalization": None if fin is None else {
            "id": fin.id,
            "status": fin.status,
            "cleaned_transcript": fin.cleaned_transcript,
            "executive_summary": fin.executive_summary,
            "pm_notes": fin.pm_notes,
            "prompt_version": fin.prompt_version,
            "model": fin.model,
            "finished_at": fin.finished_at.isoformat() if fin.finished_at else None,
            "final_state_json": fin.final_state_json,
        },
        "state": state.model_dump(mode="json"),
        "raw_transcript": [
            {"id": s.id, "segment_id": s.segment_id, "started_at": s.started_at.isoformat() if s.started_at else None,
             "ended_at": s.ended_at.isoformat() if s.ended_at else None, "text": s.text, "model": s.model}
            for s in raw_segments
        ],
        "discussion_segments": [
            {"id": d.id, "started_at": d.started_at.isoformat() if d.started_at else None,
             "ended_at": d.ended_at.isoformat() if d.ended_at else None,
             "text": d.text, "segment_count": d.segment_count}
            for d in discussions
        ],
    }


def _attachment_text(body: str, filename: str) -> PlainTextResponse:
    """Return a PlainTextResponse as a downloadable attachment."""
    return PlainTextResponse(
        content=body,
        media_type="text/plain; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


def _split_md_section(md: str, header: str) -> str | None:
    """Return the content of a level-2 markdown section, or None if absent.

    Extracts everything after the exact header line "## <header>" up to the
    next level-2 header (or end of document).
    """
    lines = md.splitlines()
    start = None
    needle = f"## {header}"
    for i, ln in enumerate(lines):
        if ln.strip() == needle:
            start = i + 1
            break
    if start is None:
        return None
    end = len(lines)
    for j in range(start, len(lines)):
        if lines[j].startswith("## "):
            end = j
            break
    body = "\n".join(lines[start:end]).strip()
    return body or None


@router.get("/{meeting_id}/export/clean-transcript.txt", response_class=PlainTextResponse)
def export_clean_transcript_txt(meeting_id: str, db: Session = Depends(get_db)) -> PlainTextResponse:
    """Download the cleaned transcript as plain text."""
    _get_meeting_or_404(db, meeting_id)
    fin = _latest_finalization(db, meeting_id)
    if fin is None or fin.status != "ok" or not fin.cleaned_transcript:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="clean transcript not available (finalization has not produced one)",
        )
    return _attachment_text(
        fin.cleaned_transcript,
        f"clean-transcript-{meeting_id}.txt",
    )


@router.get("/{meeting_id}/export/raw-transcript.txt", response_class=PlainTextResponse)
def export_raw_transcript_txt(meeting_id: str, db: Session = Depends(get_db)) -> PlainTextResponse:
    """Download the raw transcript as plain text, one segment per line.

    Format: [YYYY-MM-DD HH:MM:SS] [Speaker N]? text
    Speaker prefix is only included when speaker_name is non-null.
    """
    _get_meeting_or_404(db, meeting_id)
    rows = list(db.scalars(
        select(TranscriptSegment)
        .where(TranscriptSegment.meeting_id == meeting_id)
        .order_by(TranscriptSegment.received_at.asc(), TranscriptSegment.id.asc())
    ).all())

    lines = []
    for r in rows:
        ts = (r.started_at or r.received_at)
        ts_str = ts.strftime("%Y-%m-%d %H:%M:%S") if ts else "-"
        speaker = ""
        if r.speaker_name:
            speaker = f" [{r.speaker_name}]"
        lines.append(f"[{ts_str}]{speaker} {r.text}")

    body = "\n".join(lines) + ("\n" if lines else "")
    return _attachment_text(body, f"raw-transcript-{meeting_id}.txt")


@router.get("/{meeting_id}/export.md", response_class=PlainTextResponse)
def export_md(
    meeting_id: str,
    section: str | None = Query(default=None),
    db: Session = Depends(get_db),
) -> str:
    m = _get_meeting_or_404(db, meeting_id)
    fin = _latest_finalization(db, meeting_id)
    state = build_state_out(db, meeting_id)

    # Validate the section parameter early if present.
    if section is not None and section not in ("summary", "pm_notes"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="unsupported section (expected: summary | pm_notes)",
        )

    def bullets(items, fmt):
        if not items:
            return "_none_\n"
        return "".join(fmt(x) for x in items)

    md = []
    md.append(f"# {m.title}")
    md.append("")
    if m.objective: md.append(f"**Objective:** {m.objective}")
    if m.organization: md.append(f"**Organization:** {m.organization}")
    if m.role: md.append(f"**Role:** {m.role}")
    md.append(f"**Status:** {m.status}")
    md.append("")

    if fin is not None and fin.executive_summary:
        md.append("## Executive Summary")
        md.append("")
        md.append(fin.executive_summary)
        md.append("")

    if state.understanding:
        md.append("## Understanding")
        md.append("")
        md.append(state.understanding)
        md.append("")

    if state.key_discussion:
        md.append("## Key Discussion")
        md.append("")
        for k in state.key_discussion:
            md.append(f"- {k}")
        md.append("")

    if state.facts:
        md.append("## Facts")
        md.append("")
        for f in state.facts:
            md.append(f"- **{f.key}**: {f.value}  _(_{f.status}, confidence {f.confidence}_)_")
        md.append("")

    if state.decisions:
        md.append("## Decisions")
        md.append("")
        for d in state.decisions:
            md.append(f"- {d.text}")
        md.append("")

    if state.action_items:
        md.append("## Action Items")
        md.append("")
        for a in state.action_items:
            owner = f" — **{a.owner}**" if a.owner else ""
            due = f" (due: {a.due})" if a.due else ""
            md.append(f"- {a.text}{owner}{due}")
        md.append("")

    if state.risks:
        md.append("## Risks / Issues")
        md.append("")
        for r in state.risks:
            md.append(f"- [{r.kind}] {r.text}")
        md.append("")

    if state.questions_now or state.questions_later:
        md.append("## Questions")
        md.append("")
        for q in state.questions_now:
            md.append(f"- **now:** {q.text}")
        for q in state.questions_later:
            md.append(f"- **later:** {q.text}")
        md.append("")

    if fin is not None and fin.pm_notes:
        md.append("## PM Notes")
        md.append("")
        md.append(fin.pm_notes)
        md.append("")

    if fin is not None and fin.cleaned_transcript:
        md.append("## Cleaned Transcript")
        md.append("")
        md.append(fin.cleaned_transcript)
        md.append("")

    full = "\n".join(md)

    if section == "summary":
        body = _split_md_section(full, "Executive Summary")
        if body is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                                detail="Executive Summary not present in this export")
        # Prepend the meeting title so the downloaded file is self-describing.
        return f"# {m.title}\n\n## Executive Summary\n\n{body}\n"

    if section == "pm_notes":
        body = _split_md_section(full, "PM Notes")
        if body is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                                detail="PM Notes not present in this export")
        return f"# {m.title}\n\n## PM Notes\n\n{body}\n"

    return full
