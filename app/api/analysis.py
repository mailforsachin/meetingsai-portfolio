from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.ai.state_builder import build_state_out
from app.core.deps import require_user
from app.db.session import get_db
from app.models.context_gap import ContextGap
from app.models.meeting import Meeting
from app.models.state import AIAnalysisRun
from app.schemas.analysis import AnalysisRunList, AnalysisRunOut
from app.schemas.context_gap import ContextGapList, ContextGapOut
from app.schemas.state import MeetingStateOut

router = APIRouter(
    prefix="/api/meetings",
    tags=["analysis"],
    dependencies=[Depends(require_user)],
)


def _get_meeting_or_404(db: Session, meeting_id: str) -> Meeting:
    m = db.get(Meeting, meeting_id)
    if m is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="meeting not found")
    return m


@router.get("/{meeting_id}/state", response_model=MeetingStateOut)
def get_state(
    meeting_id: str,
    include_incidental: bool = Query(default=False),
    db: Session = Depends(get_db),
) -> MeetingStateOut:
    """Current post-reducer state, assembled from live entity tables.

    By default, facts/decisions/actions/risks/questions marked
    `meeting_relevance = incidental` are excluded from the response.
    Pass `?include_incidental=true` to see them.

    Does not read LLM proposed output.  The last snapshot timestamp and
    last analysis run id are included for traceability.
    """
    _get_meeting_or_404(db, meeting_id)
    return build_state_out(db, meeting_id, include_incidental=include_incidental)


@router.get("/{meeting_id}/analysis", response_model=AnalysisRunList)
def list_analysis_runs(
    meeting_id: str,
    db: Session = Depends(get_db),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> AnalysisRunList:
    """Paginated list of analysis runs.  Never exposes output_json."""
    _get_meeting_or_404(db, meeting_id)
    total = db.scalar(
        select(func.count())
        .select_from(AIAnalysisRun)
        .where(AIAnalysisRun.meeting_id == meeting_id)
    ) or 0
    rows = db.scalars(
        select(AIAnalysisRun)
        .where(AIAnalysisRun.meeting_id == meeting_id)
        .order_by(AIAnalysisRun.started_at.desc(), AIAnalysisRun.id.desc())
        .limit(limit)
        .offset(offset)
    ).all()
    return AnalysisRunList(
        items=[AnalysisRunOut.model_validate(r) for r in rows],
        total=int(total),
        limit=limit,
        offset=offset,
    )


@router.get("/{meeting_id}/analysis/{run_id}/output")
def get_analysis_output(
    meeting_id: str,
    run_id: int,
    db: Session = Depends(get_db),
) -> dict:
    """Return the validated JSON the model proposed for one run.

    Scoped to the requested meeting: a run belonging to another meeting
    returns 404 even if the run id exists.
    """
    _get_meeting_or_404(db, meeting_id)
    run = db.get(AIAnalysisRun, run_id)
    if run is None or run.meeting_id != meeting_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="run not found")
    if run.output_json is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"run has no output_json (status={run.status})",
        )
    import json
    try:
        parsed = json.loads(run.output_json)
    except json.JSONDecodeError:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="stored output_json is not valid JSON",
        )
    return {
        "run_id": run.id,
        "meeting_id": run.meeting_id,
        "kind": run.kind,
        "prompt_version": run.prompt_version,
        "model": run.model,
        "status": run.status,
        "started_at": run.started_at.isoformat() if run.started_at else None,
        "finished_at": run.finished_at.isoformat() if run.finished_at else None,
        "output": parsed,
    }


@router.get("/{meeting_id}/context_gaps", response_model=ContextGapList)
def list_context_gaps(
    meeting_id: str,
    status: str = Query(default="open"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
) -> ContextGapList:
    """Full list of Context Review gaps for a meeting.

    By default returns open gaps.  Pass `status=resolved` or
    `status=superseded` for other states.  The default sort is urgency
    (critical, high, medium, low) then last_seen_at descending.
    """
    _get_meeting_or_404(db, meeting_id)

    base = (
        select(ContextGap)
        .where(ContextGap.meeting_id == meeting_id)
        .where(ContextGap.status == status)
    )
    total = db.scalar(
        select(func.count()).select_from(ContextGap)
        .where(ContextGap.meeting_id == meeting_id)
        .where(ContextGap.status == status)
    ) or 0
    rows = list(db.scalars(base).all())

    rank = {"critical": 0, "high": 1, "medium": 2, "low": 3}
    rows.sort(key=lambda g: (rank.get(g.urgency, 9),
                             -(g.last_seen_at.timestamp() if g.last_seen_at else 0)))
    page = rows[offset:offset + limit]

    return ContextGapList(
        items=[ContextGapOut.model_validate(g) for g in page],
        total=int(total),
        limit=limit,
        offset=offset,
    )
