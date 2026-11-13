"""Deterministic state reducer (Phase D).

Extended from Phase 9 to honor the new fields the model now emits:

  - meeting_relevance on every item
  - thread_key on every item (threads are matched or created)
  - change annotations (recorded in the delta, not on the entity)
  - action_items.previous_status on status changes
  - meeting_mode_proposal (persisted on the meeting when mode is null)

Computes a MeetingDelta by diffing the state before and after the reducer
runs.  The delta is not written to the DB here; the caller persists it in
`meeting_deltas` linked to the analysis run.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.meeting import Meeting
from app.models.state import (
    ActionItem,
    Decision,
    Fact,
    Question,
    Risk,
)
from app.models.thread import Thread
from app.schemas.ai_output import (
    ProposedActionItem,
    ProposedDecision,
    ProposedFact,
    ProposedQuestion,
    ProposedRisk,
    RollingAnalysisOutput,
)

log = logging.getLogger("meetingsai.ai.reducer")


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", s.strip().lower())


# Phase H1: reject thread keys that look like database identifiers.
# Mirrors the Context Review reducer (context_review_reducer.py) so both
# paths behave identically when the model emits a numeric or id-like key.
_NUMERIC_KEY = re.compile(r"^\d+$")
_ID_LIKE_KEY = re.compile(r"^(id|seg|disc|segment|discussion)[-_]?\d+$")
_UNTHREADED_KEY = "__unthreaded__"
_UNTHREADED_TITLE = "Unthreaded"


def _norm_key(s: str | None) -> str:
    """Normalize a thread key to a lowercase snake_case slug.

    Falls back to _UNTHREADED_KEY when the input is empty.  Numeric and
    id-like keys are rejected upstream by _is_rejected_thread_key, not here.
    """
    if s is None:
        return _UNTHREADED_KEY
    s = str(s).strip().lower()
    s = re.sub(r"[^a-z0-9]+", "_", s)
    s = s.strip("_")[:64]
    return s or _UNTHREADED_KEY


def _is_rejected_thread_key(key: str | None) -> bool:
    """True if the key is empty, numeric-only, or looks like a database id."""
    if key is None:
        return True
    k = str(key).strip().lower()
    if not k:
        return True
    if _NUMERIC_KEY.match(k):
        return True
    if _ID_LIKE_KEY.match(k):
        return True
    return False


@dataclass
class ReducerSummary:
    facts_created: int = 0
    facts_updated: int = 0
    facts_superseded: int = 0
    facts_disputed: int = 0
    decisions_created: int = 0
    decisions_updated: int = 0
    actions_created: int = 0
    actions_updated: int = 0
    risks_created: int = 0
    risks_updated: int = 0
    questions_created: int = 0
    questions_updated: int = 0
    threads_created: int = 0
    threads_matched: int = 0
    mode_persisted: str | None = None
    warnings: list[str] = field(default_factory=list)


# ---------- thread lookup ----------

_thread_cache: dict[tuple[str, str], Thread] = {}


def _get_or_create_unthreaded(db: Session, meeting_id: str) -> Thread:
    """Return the per-meeting __unthreaded__ sentinel thread, creating it if needed.

    Phase H1: mirrors context_review_reducer._get_or_create_unthreaded so
    items whose thread_key was rejected land in the same place regardless
    of which reducer processed them.
    """
    cache_key = (meeting_id, _UNTHREADED_KEY)
    if cache_key in _thread_cache:
        return _thread_cache[cache_key]
    existing = db.scalars(
        select(Thread)
        .where(Thread.meeting_id == meeting_id)
        .where(Thread.key == _UNTHREADED_KEY)
        .limit(1)
    ).first()
    if existing is not None:
        _thread_cache[cache_key] = existing
        return existing
    t = Thread(
        meeting_id=meeting_id,
        key=_UNTHREADED_KEY,
        title=_UNTHREADED_TITLE,
        status="open",
        summary=None,
    )
    db.add(t)
    db.flush()
    _thread_cache[cache_key] = t
    return t


def _get_or_create_thread(
    db: Session, meeting_id: str, key: str | None, title: str | None, summary: str | None
) -> Thread:
    """Return a Thread for the given semantic key, or the sentinel if rejected.

    Phase H1: never returns None.  A numeric or id-like key is routed to
    the per-meeting __unthreaded__ thread, matching the Context Review
    reducer's behavior so items that could not be semantically grouped
    remain visible in the UI rather than disappearing under thread_id=None.
    """
    if _is_rejected_thread_key(key):
        return _get_or_create_unthreaded(db, meeting_id)

    norm_key = _norm_key(key)
    cache_key = (meeting_id, norm_key)
    if cache_key in _thread_cache:
        return _thread_cache[cache_key]

    existing = db.scalars(
        select(Thread)
        .where(Thread.meeting_id == meeting_id)
        .where(Thread.key == norm_key)
        .limit(1)
    ).first()
    if existing is not None:
        _thread_cache[cache_key] = existing
        return existing

    t = Thread(
        meeting_id=meeting_id,
        key=norm_key,
        title=(title or key or norm_key).strip()[:255] or norm_key,
        status="open",
        summary=(summary or None),
    )
    db.add(t)
    db.flush()
    _thread_cache[cache_key] = t
    return t


# ---------- facts ----------

def _apply_fact(db: Session, meeting_id: str, p: ProposedFact, s: ReducerSummary) -> Fact:
    existing: Fact | None = db.scalars(
        select(Fact)
        .where(Fact.meeting_id == meeting_id)
        .where(Fact.key == p.key)
        .where(Fact.status.in_(("confirmed", "tentative", "disputed", "unresolved")))
        .order_by(Fact.last_updated_at.desc())
        .limit(1)
    ).first()

    supersedes_target: Fact | None = None
    if p.supersedes_fact_id is not None:
        cand = db.get(Fact, p.supersedes_fact_id)
        if cand is None:
            s.warnings.append(f"fact {p.key!r}: supersedes target not found")
        elif cand.meeting_id != meeting_id:
            s.warnings.append(f"fact {p.key!r}: supersedes target in another meeting")
        elif cand.status == "superseded":
            s.warnings.append(f"fact {p.key!r}: supersedes target already superseded")
        else:
            supersedes_target = cand

    proposed_status = p.status
    if supersedes_target is None and proposed_status == "confirmed" and existing is not None:
        if _norm(existing.value) != _norm(p.value):
            proposed_status = "disputed"
            s.facts_disputed += 1
    if supersedes_target is None and p.supersedes_fact_id is not None and proposed_status == "confirmed":
        proposed_status = "tentative"

    # Resolve thread
    thread_id = None
    if p.thread_key:
        t = _get_or_create_thread(db, meeting_id, p.thread_key, p.thread_key, None)
        thread_id = t.id
        s.threads_matched += 1

    if existing is not None and _norm(existing.value) == _norm(p.value):
        if p.confidence is not None and (existing.confidence is None or p.confidence > existing.confidence):
            existing.confidence = p.confidence
        if p.source_text and not existing.source_text:
            existing.source_text = p.source_text
        if p.source_discussion_id and not existing.source_discussion_id:
            existing.source_discussion_id = p.source_discussion_id
        if p.meeting_relevance:
            existing.meeting_relevance = p.meeting_relevance
        if thread_id is not None:
            existing.thread_id = thread_id
        existing.last_updated_at = _now()
        s.facts_updated += 1
        return existing

    if supersedes_target is not None:
        supersedes_target.status = "superseded"
        supersedes_target.last_updated_at = _now()
        s.facts_superseded += 1

    new_fact = Fact(
        meeting_id=meeting_id,
        key=p.key,
        value=p.value,
        status=proposed_status,
        confidence=p.confidence,
        source_discussion_id=p.source_discussion_id,
        source_text=p.source_text,
        supersedes_fact_id=supersedes_target.id if supersedes_target is not None else None,
        meeting_relevance=p.meeting_relevance,
        thread_id=thread_id,
    )
    db.add(new_fact)
    s.facts_created += 1
    return new_fact


# ---------- decisions / actions / risks / questions ----------

def _apply_decision(db: Session, meeting_id: str, p: ProposedDecision, s: ReducerSummary) -> None:
    key = _norm(p.text)
    thread_id = None
    if p.thread_key:
        t = _get_or_create_thread(db, meeting_id, p.thread_key, p.thread_key, None)
        thread_id = t.id
    for e in db.scalars(select(Decision).where(Decision.meeting_id == meeting_id)).all():
        if _norm(e.text) == key:
            if p.meeting_relevance: e.meeting_relevance = p.meeting_relevance
            if thread_id is not None: e.thread_id = thread_id
            e.last_updated_at = _now()
            s.decisions_updated += 1
            return
    db.add(Decision(
        meeting_id=meeting_id, text=p.text, status=p.status,
        source_discussion_id=p.source_discussion_id, source_text=p.source_text,
        meeting_relevance=p.meeting_relevance, thread_id=thread_id,
    ))
    s.decisions_created += 1


def _apply_action(db: Session, meeting_id: str, p: ProposedActionItem, s: ReducerSummary) -> None:
    key = _norm(p.text)
    thread_id = None
    if p.thread_key:
        t = _get_or_create_thread(db, meeting_id, p.thread_key, p.thread_key, None)
        thread_id = t.id
    for e in db.scalars(select(ActionItem).where(ActionItem.meeting_id == meeting_id)).all():
        if _norm(e.text) == key:
            if p.owner and not e.owner: e.owner = p.owner
            if p.due and not e.due: e.due = p.due
            if p.meeting_relevance: e.meeting_relevance = p.meeting_relevance
            if thread_id is not None: e.thread_id = thread_id
            if p.previous_status and e.status != p.previous_status:
                # Status changed; record previous and update.
                e.previous_status = e.status
                e.status = p.previous_status
            e.last_updated_at = _now()
            s.actions_updated += 1
            return
    db.add(ActionItem(
        meeting_id=meeting_id, text=p.text, owner=p.owner, due=p.due, status="open",
        source_discussion_id=p.source_discussion_id, source_text=p.source_text,
        meeting_relevance=p.meeting_relevance, thread_id=thread_id,
    ))
    s.actions_created += 1


def _apply_risk(db: Session, meeting_id: str, p: ProposedRisk, s: ReducerSummary) -> None:
    key = _norm(p.text)
    thread_id = None
    if p.thread_key:
        t = _get_or_create_thread(db, meeting_id, p.thread_key, p.thread_key, None)
        thread_id = t.id
    for e in db.scalars(select(Risk).where(Risk.meeting_id == meeting_id)).all():
        if _norm(e.text) == key:
            if p.severity and not e.severity: e.severity = p.severity
            if p.meeting_relevance: e.meeting_relevance = p.meeting_relevance
            if thread_id is not None: e.thread_id = thread_id
            e.last_updated_at = _now()
            s.risks_updated += 1
            return
    db.add(Risk(
        meeting_id=meeting_id, text=p.text, kind=p.kind, severity=p.severity, status="open",
        source_discussion_id=p.source_discussion_id, source_text=p.source_text,
        meeting_relevance=p.meeting_relevance, thread_id=thread_id,
    ))
    s.risks_created += 1


def _apply_question(db: Session, meeting_id: str, p: ProposedQuestion, s: ReducerSummary) -> None:
    key = _norm(p.text)
    thread_id = None
    if p.thread_key:
        t = _get_or_create_thread(db, meeting_id, p.thread_key, p.thread_key, None)
        thread_id = t.id
    for e in db.scalars(select(Question).where(Question.meeting_id == meeting_id)).all():
        if _norm(e.text) == key and e.category == p.category:
            if p.meeting_relevance: e.meeting_relevance = p.meeting_relevance
            if thread_id is not None: e.thread_id = thread_id
            e.last_updated_at = _now()
            s.questions_updated += 1
            return
    db.add(Question(
        meeting_id=meeting_id, text=p.text, category=p.category, rationale=p.rationale,
        status="open", is_new=True,
        source_discussion_id=p.source_discussion_id, source_text=p.source_text,
        meeting_relevance=p.meeting_relevance, thread_id=thread_id,
    ))
    s.questions_created += 1


def _clear_questions_is_new(db: Session, meeting_id: str) -> None:
    for r in db.scalars(
        select(Question).where(Question.meeting_id == meeting_id).where(Question.is_new.is_(True))
    ).all():
        r.is_new = False


# ---------- top-level ----------

def apply_rolling_output(db: Session, meeting_id: str, out: RollingAnalysisOutput) -> ReducerSummary:
    # Reset the per-call thread cache; it must not leak across sessions.
    _thread_cache.clear()
    s = ReducerSummary()

    # Persist the mode if the meeting has none and the model proposed one.
    m = db.get(Meeting, meeting_id)
    if m is not None and m.mode is None and out.meeting_mode_proposal:
        m.mode = out.meeting_mode_proposal
        m.mode_inferred = True
        s.mode_persisted = out.meeting_mode_proposal

    _clear_questions_is_new(db, meeting_id)

    for p in out.facts:
        _apply_fact(db, meeting_id, p, s)
    for d in out.decisions:
        _apply_decision(db, meeting_id, d, s)
    for a in out.action_items:
        _apply_action(db, meeting_id, a, s)
    for r in out.risks:
        _apply_risk(db, meeting_id, r, s)
    for q in list(out.questions_now) + list(out.questions_later):
        _apply_question(db, meeting_id, q, s)

    db.flush()
    return s
