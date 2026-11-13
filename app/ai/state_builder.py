"""Assemble the current meeting state for the LLM prompt and for GET /state.

Phase H1 (2026-09-24): the LLM-facing payload now uses semantic thread keys,
not database thread IDs.  Every item carries `thread_key` (a slug string or
null) instead of `thread_id` (a database integer).  `current_state.threads[]`
lists the existing semantic threads.  Item-level `id` fields are retained
only where the reducer genuinely requires them (facts, for supersession
via `supersedes_fact_id`).  The API-facing `build_meeting_state` path is
unchanged and continues to return database IDs.
"""
from __future__ import annotations

import json
import logging
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.context_gap import ContextGap
from app.models.discussion_segment import DiscussionSegment
from app.models.meeting import Meeting
from app.models.meeting_delta import MeetingDelta
from app.models.state import (
    ActionItem,
    AIAnalysisRun,
    Decision,
    Fact,
    MeetingStateSnapshot,
    Question,
    Risk,
)
from app.models.thread import Thread
from app.schemas.context_gap import ContextGapOut
from app.schemas.state import (
    ActionItemOut,
    DecisionOut,
    FactOut,
    MeetingStateOut,
    QuestionOut,
    RiskOut,
    ThreadOut,
)

log = logging.getLogger("meetingsai.ai.state_builder")


# ---------------------------------------------------------------- helpers

def _latest_snapshot(db: Session, meeting_id: str) -> MeetingStateSnapshot | None:
    return db.scalars(
        select(MeetingStateSnapshot)
        .where(MeetingStateSnapshot.meeting_id == meeting_id)
        .order_by(MeetingStateSnapshot.created_at.desc(), MeetingStateSnapshot.id.desc())
        .limit(1)
    ).first()


def _last_run(db: Session, meeting_id: str) -> AIAnalysisRun | None:
    return db.scalars(
        select(AIAnalysisRun)
        .where(AIAnalysisRun.meeting_id == meeting_id)
        .order_by(AIAnalysisRun.started_at.desc(), AIAnalysisRun.id.desc())
        .limit(1)
    ).first()


def _latest_delta(db: Session, meeting_id: str) -> dict | None:
    row = db.scalars(
        select(MeetingDelta)
        .where(MeetingDelta.meeting_id == meeting_id)
        .order_by(MeetingDelta.created_at.desc(), MeetingDelta.id.desc())
        .limit(1)
    ).first()
    if row is None or not row.delta_json:
        return None
    try:
        return json.loads(row.delta_json)
    except json.JSONDecodeError:
        log.warning("malformed delta_json id=%s", row.id)
        return None


def _parse_snapshot(snapshot: MeetingStateSnapshot | None) -> dict[str, Any]:
    if snapshot is None or not snapshot.state_json:
        return {}
    try:
        parsed = json.loads(snapshot.state_json)
    except (json.JSONDecodeError, TypeError):
        return {}
    if not isinstance(parsed, dict):
        return {}
    return parsed


def _str_or_none(v: Any) -> str | None:
    if v is None:
        return None
    if isinstance(v, str):
        return v
    return str(v)


def _list_of_str(v: Any) -> list[str]:
    if not isinstance(v, list):
        return []
    return [item if isinstance(item, str) else str(item) for item in v if item is not None]


# ------------------------------------------------------------ LLM payload
# (unchanged from Phase B; preserved here so this file is the single source)

def build_llm_payload(
    db: Session,
    meeting: Meeting,
    recent_limit: int,
) -> tuple[dict[str, Any], int | None]:
    discussions = list(db.scalars(
        select(DiscussionSegment)
        .where(DiscussionSegment.meeting_id == meeting.id)
        .order_by(DiscussionSegment.started_at.desc(), DiscussionSegment.id.desc())
        .limit(recent_limit)
    ).all())
    discussions.reverse()
    newest_id = max((d.id for d in discussions), default=None)

    facts = list(db.scalars(
        select(Fact).where(Fact.meeting_id == meeting.id)
        .where(Fact.status.in_(("confirmed", "tentative", "disputed", "unresolved")))
        .order_by(Fact.last_updated_at.desc()).limit(200)
    ).all())
    decisions = list(db.scalars(
        select(Decision).where(Decision.meeting_id == meeting.id)
        .where(Decision.status.in_(("proposed", "confirmed")))
        .order_by(Decision.last_updated_at.desc()).limit(100)
    ).all())
    actions = list(db.scalars(
        select(ActionItem).where(ActionItem.meeting_id == meeting.id)
        .where(ActionItem.status == "open")
        .order_by(ActionItem.last_updated_at.desc()).limit(100)
    ).all())
    risks = list(db.scalars(
        select(Risk).where(Risk.meeting_id == meeting.id)
        .where(Risk.status == "open")
        .order_by(Risk.last_updated_at.desc()).limit(100)
    ).all())
    questions = list(db.scalars(
        select(Question).where(Question.meeting_id == meeting.id)
        .where(Question.status == "open")
        .order_by(Question.last_updated_at.desc()).limit(100)
    ).all())

    # Phase H1: fetch existing threads for the model-facing state.  The LLM
    # must see semantic thread keys (slugs) so it can reuse them instead of
    # inventing numeric ones from database IDs it was shown previously.
    threads_rows = list(db.scalars(
        select(Thread)
        .where(Thread.meeting_id == meeting.id)
        .order_by(Thread.created_at.asc(), Thread.id.asc())
    ).all())
    thread_key_by_id = {t.id: t.key for t in threads_rows}

    snapshot = _latest_snapshot(db, meeting.id)
    snap = _parse_snapshot(snapshot)
    delta = _latest_delta(db, meeting.id)

    payload: dict[str, Any] = {
        "meeting": {
            "id": meeting.id,
            "title": meeting.title,
            "objective": meeting.objective,
            "organization": meeting.organization,
            "role": meeting.role,
            "notes": meeting.notes,
            "status": meeting.status,
            "mode": getattr(meeting, "mode", None),
        },
        "current_state": {
            "understanding": _str_or_none(snap.get("understanding")),
            "key_discussion": _list_of_str(snap.get("key_discussion")),
            "potential_response": _str_or_none(snap.get("potential_response")),
            # Phase H1: semantic thread identity.  No database ids.
            "threads": [
                {"key": t.key, "title": t.title, "status": t.status,
                 "summary": t.summary}
                for t in threads_rows
            ],
            "facts": [
                {"id": f.id, "key": f.key, "value": f.value, "status": f.status,
                 "confidence": f.confidence, "source_discussion_id": f.source_discussion_id,
                 "source_text": f.source_text, "supersedes_fact_id": f.supersedes_fact_id,
                 "meeting_relevance": f.meeting_relevance,
                 "thread_key": thread_key_by_id.get(f.thread_id)}
                for f in facts
            ],
            "decisions": [
                {"text": d.text, "status": d.status,
                 "source_discussion_id": d.source_discussion_id, "source_text": d.source_text,
                 "meeting_relevance": d.meeting_relevance,
                 "thread_key": thread_key_by_id.get(d.thread_id)}
                for d in decisions
            ],
            "action_items": [
                {"text": a.text, "owner": a.owner, "due": a.due, "status": a.status,
                 "source_discussion_id": a.source_discussion_id, "source_text": a.source_text,
                 "meeting_relevance": a.meeting_relevance,
                 "thread_key": thread_key_by_id.get(a.thread_id),
                 "previous_status": a.previous_status}
                for a in actions
            ],
            "risks": [
                {"text": r.text, "kind": r.kind, "severity": r.severity,
                 "status": r.status, "source_discussion_id": r.source_discussion_id,
                 "source_text": r.source_text, "meeting_relevance": r.meeting_relevance,
                 "thread_key": thread_key_by_id.get(r.thread_id)}
                for r in risks
            ],
            "questions": [
                {"text": q.text, "category": q.category, "rationale": q.rationale,
                 "status": q.status, "source_discussion_id": q.source_discussion_id,
                 "source_text": q.source_text, "meeting_relevance": q.meeting_relevance,
                 "thread_key": thread_key_by_id.get(q.thread_id)}
                for q in questions
            ],
        },
        "recent_discussion_segments": [
            {"id": d.id,
             "started_at": d.started_at.isoformat() if d.started_at else None,
             "ended_at": d.ended_at.isoformat() if d.ended_at else None,
             "text": d.text,
             "segment_count": d.segment_count}
            for d in discussions
        ],
        "recent_deltas": [delta] if delta else [],
    }
    return payload, newest_id


# --------------------------------------------------------- state for API

def _filter_relevance(rows, include_incidental: bool):
    if include_incidental:
        return list(rows)
    return [r for r in rows if (getattr(r, "meeting_relevance", None) or "relevant") != "incidental"]


def build_state_out(
    db: Session,
    meeting_id: str,
    include_incidental: bool = False,
) -> MeetingStateOut:
    """Assemble the current state as a MeetingStateOut, for GET /state."""
    m = db.get(Meeting, meeting_id)
    snapshot = _latest_snapshot(db, meeting_id)
    last_run = _last_run(db, meeting_id)
    snap = _parse_snapshot(snapshot)
    delta = _latest_delta(db, meeting_id)

    facts = _filter_relevance(
        db.scalars(
            select(Fact).where(Fact.meeting_id == meeting_id)
            .where(Fact.status.in_(("confirmed", "tentative", "disputed", "unresolved")))
            .order_by(Fact.last_updated_at.desc())
        ).all(),
        include_incidental,
    )
    decisions = _filter_relevance(
        db.scalars(
            select(Decision).where(Decision.meeting_id == meeting_id)
            .where(Decision.status.in_(("proposed", "confirmed")))
            .order_by(Decision.last_updated_at.desc())
        ).all(),
        include_incidental,
    )
    actions = _filter_relevance(
        db.scalars(
            select(ActionItem).where(ActionItem.meeting_id == meeting_id)
            .where(ActionItem.status == "open")
            .order_by(ActionItem.last_updated_at.desc())
        ).all(),
        include_incidental,
    )
    risks = _filter_relevance(
        db.scalars(
            select(Risk).where(Risk.meeting_id == meeting_id)
            .where(Risk.status == "open")
            .order_by(Risk.last_updated_at.desc())
        ).all(),
        include_incidental,
    )
    q_now = _filter_relevance(
        db.scalars(
            select(Question).where(Question.meeting_id == meeting_id)
            .where(Question.status == "open").where(Question.category == "now")
            .order_by(Question.last_updated_at.desc())
        ).all(),
        include_incidental,
    )
    q_later = _filter_relevance(
        db.scalars(
            select(Question).where(Question.meeting_id == meeting_id)
            .where(Question.status == "open").where(Question.category == "later")
            .order_by(Question.last_updated_at.desc())
        ).all(),
        include_incidental,
    )

    threads_rows = list(db.scalars(
        select(Thread).where(Thread.meeting_id == meeting_id)
        .order_by(Thread.created_at.asc(), Thread.id.asc())
    ).all())

    # Context Review gaps: open, ranked by urgency then recency.  Top 5 for
    # the state response; the total is returned alongside.
    _URGENCY_RANK = {"critical": 0, "high": 1, "medium": 2, "low": 3}
    gap_rows_all = list(db.scalars(
        select(ContextGap)
        .where(ContextGap.meeting_id == meeting_id)
        .where(ContextGap.status == "open")
    ).all())
    gap_rows_all.sort(
        key=lambda g: (_URGENCY_RANK.get(g.urgency, 9), -(g.last_seen_at.timestamp() if g.last_seen_at else 0))
    )
    context_gaps_top = [ContextGapOut.model_validate(g) for g in gap_rows_all[:5]]
    context_gaps_total = len(gap_rows_all)

    # Group counts per thread.
    def counts(model, field="thread_id"):
        rows = db.execute(
            select(getattr(model, field), func.count()).where(model.meeting_id == meeting_id)
            .group_by(getattr(model, field))
        ).all()
        return {tid: n for tid, n in rows if tid is not None}

    fc = counts(Fact)
    dc = counts(Decision)
    ac = counts(ActionItem)
    rc = counts(Risk)
    qc = counts(Question)

    threads_out = [
        ThreadOut(
            id=t.id, key=t.key, title=t.title, status=t.status, summary=t.summary,
            fact_count=fc.get(t.id, 0),
            decision_count=dc.get(t.id, 0),
            action_count=ac.get(t.id, 0),
            risk_count=rc.get(t.id, 0),
            question_count=qc.get(t.id, 0),
        )
        for t in threads_rows
    ]

    # Counts of incidental excluded from the default view.
    incidental_counts = {}
    if not include_incidental:
        for label, model in (("facts", Fact), ("decisions", Decision),
                             ("action_items", ActionItem), ("risks", Risk),
                             ("questions", Question)):
            n = db.scalar(
                select(func.count()).select_from(model)
                .where(model.meeting_id == meeting_id)
                .where(model.meeting_relevance == "incidental")
            ) or 0
            if n:
                incidental_counts[label] = int(n)

    return MeetingStateOut(
        meeting_id=meeting_id,
        mode=getattr(m, "mode", None) if m is not None else None,
        mode_inferred=bool(getattr(m, "mode_inferred", False)) if m is not None else False,
        understanding=_str_or_none(snap.get("understanding")),
        key_discussion=_list_of_str(snap.get("key_discussion")),
        potential_response=_str_or_none(snap.get("potential_response")),
        missing_information=_list_of_str(snap.get("missing_information")),
        contradictions=_list_of_str(snap.get("contradictions")),
        next_steps=_list_of_str(snap.get("next_steps")),
        facts=[FactOut.model_validate(f) for f in facts],
        decisions=[DecisionOut.model_validate(d) for d in decisions],
        action_items=[ActionItemOut.model_validate(a) for a in actions],
        risks=[RiskOut.model_validate(r) for r in risks],
        questions_now=[QuestionOut.model_validate(q) for q in q_now],
        questions_later=[QuestionOut.model_validate(q) for q in q_later],
        threads=threads_out,
        context_gaps=context_gaps_top,
        context_gaps_total=context_gaps_total,
        delta=delta,
        incidental_counts=incidental_counts,
        last_snapshot_at=snapshot.created_at if snapshot else None,
        last_analysis_run_id=last_run.id if last_run else None,
    )
