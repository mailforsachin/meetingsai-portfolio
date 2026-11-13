"""Deterministic reducer for Context Review output.

Takes a validated ContextReviewOutput and persists it as context_gaps.

Rules:
  - Dedup by norm_hash (sha256 of normalized text).  Two consecutive
    runs that see the same gap do not create a duplicate row; the
    existing open gap's last_seen_at is bumped, its urgency may be
    raised, and its provenance gains new discussion_ids.
  - Resolved or superseded gaps are not resurrected.
  - Threads proposed by the model are validated: numeric-only or empty
    keys are rejected.  Items with rejected keys fall into the meeting's
    per-meeting "__unthreaded__" thread.
  - This function never writes to questions, actions, decisions, risks,
    facts, or any other table.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.context_gap import ContextGap
from app.models.thread import Thread
from app.schemas.context_review import ContextReviewOutput, ProposedGap

log = logging.getLogger("meetingsai.ai.context_review")


URGENCY_ORDER = {"low": 0, "medium": 1, "high": 2, "critical": 3}
_UNTHREADED_KEY = "__unthreaded__"
_UNTHREADED_TITLE = "Unthreaded"


@dataclass
class ContextReviewSummary:
    created: int = 0
    updated: int = 0
    skipped_resolved: int = 0
    threads_matched: int = 0
    threads_rejected: int = 0
    warnings: list[str] = field(default_factory=list)


# ---------- helpers ----------

def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _normalize_for_hash(text: str) -> str:
    s = text.strip().lower()
    # Strip punctuation that is not semantically important for identity.
    s = re.sub(r"[^\w\s]", " ", s)
    s = re.sub(r"\s+", " ", s)
    return s.strip()


def _norm_hash(text: str) -> str:
    return hashlib.sha256(_normalize_for_hash(text).encode("utf-8")).hexdigest()


def _norm_key(key: str) -> str:
    s = key.strip().lower()
    s = re.sub(r"[^a-z0-9]+", "_", s).strip("_")
    return s[:64] or "unnamed"


_NUMERIC_KEY = re.compile(r"^\d+$")
_ID_LIKE_KEY = re.compile(r"^(id|seg|disc|segment|discussion)[-_]?\d+$")


def _is_rejected_thread_key(key: str) -> bool:
    k = key.strip().lower()
    if not k:
        return True
    if _NUMERIC_KEY.match(k):
        return True
    if _ID_LIKE_KEY.match(k):
        return True
    return False


def _resolve_thread(
    db: Session, meeting_id: str, key: str, title: Optional[str], summary: Optional[str],
    summary_counters: ContextReviewSummary,
) -> Thread:
    if _is_rejected_thread_key(key):
        summary_counters.threads_rejected += 1
        return _get_or_create_unthreaded(db, meeting_id)

    norm_key = _norm_key(key)
    existing = db.scalars(
        select(Thread)
        .where(Thread.meeting_id == meeting_id)
        .where(Thread.key == norm_key)
        .limit(1)
    ).first()
    if existing is not None:
        summary_counters.threads_matched += 1
        return existing

    t = Thread(
        meeting_id=meeting_id,
        key=norm_key,
        title=(title or key).strip()[:255] or norm_key,
        status="open",
        summary=(summary or None),
    )
    db.add(t)
    db.flush()
    summary_counters.threads_matched += 1
    return t


def _get_or_create_unthreaded(db: Session, meeting_id: str) -> Thread:
    existing = db.scalars(
        select(Thread)
        .where(Thread.meeting_id == meeting_id)
        .where(Thread.key == _UNTHREADED_KEY)
        .limit(1)
    ).first()
    if existing is not None:
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
    return t


def _merge_provenance(existing_json: Optional[str], new_ids: list[int]) -> str:
    data = {}
    if existing_json:
        try:
            data = json.loads(existing_json)
        except json.JSONDecodeError:
            data = {}
    if not isinstance(data, dict):
        data = {}
    evidence = data.get("evidence")
    if not isinstance(evidence, dict):
        evidence = {}
    ids = evidence.get("discussion_ids")
    if not isinstance(ids, list):
        ids = []
    merged = list({*ids, *[int(i) for i in new_ids if isinstance(i, int)]})
    evidence["discussion_ids"] = merged
    data["evidence"] = evidence
    return json.dumps(data, ensure_ascii=False)


def _find_open_gap(db: Session, meeting_id: str, norm_hash: str) -> Optional[ContextGap]:
    return db.scalars(
        select(ContextGap)
        .where(ContextGap.meeting_id == meeting_id)
        .where(ContextGap.norm_hash == norm_hash)
        .where(ContextGap.status == "open")
        .order_by(ContextGap.id.desc())
        .limit(1)
    ).first()


def _has_non_open_gap(db: Session, meeting_id: str, norm_hash: str) -> bool:
    row = db.scalars(
        select(ContextGap)
        .where(ContextGap.meeting_id == meeting_id)
        .where(ContextGap.norm_hash == norm_hash)
        .where(ContextGap.status.in_(("resolved", "superseded")))
        .limit(1)
    ).first()
    return row is not None


# ---------- main reducer ----------

def apply_context_review(
    db: Session,
    meeting_id: str,
    run_id: int,
    out: ContextReviewOutput,
    evidence_from_segment_id: Optional[int] = None,
    evidence_to_segment_id: Optional[int] = None,
) -> ContextReviewSummary:
    s = ContextReviewSummary()
    now = _now()

    for gap in out.gaps:
        nh = _norm_hash(gap.text)
        existing = _find_open_gap(db, meeting_id, nh)

        if existing is not None:
            # Bump last_seen_at.
            existing.last_seen_at = now
            # Raise urgency if strictly higher.
            cur = URGENCY_ORDER.get(existing.urgency, 0)
            new = URGENCY_ORDER.get(gap.urgency, 0)
            if new > cur:
                existing.urgency = gap.urgency
            # Merge provenance.
            existing.provenance_json = _merge_provenance(
                existing.provenance_json, gap.evidence.discussion_ids
            )
            # Attach thread if not attached yet.
            if existing.thread_id is None and gap.thread_key:
                t = _resolve_thread(db, meeting_id, gap.thread_key, gap.thread_key, None, s)
                existing.thread_id = t.id
            # Keep evidence_from_run_id pointing at the first observation.
            s.updated += 1
            continue

        if _has_non_open_gap(db, meeting_id, nh):
            s.skipped_resolved += 1
            continue

        thread_id = None
        if gap.thread_key:
            t = _resolve_thread(db, meeting_id, gap.thread_key, gap.thread_key, None, s)
            thread_id = t.id

        source_discussion_id = (
            gap.evidence.discussion_ids[0] if gap.evidence.discussion_ids else None
        )
        provenance = json.dumps(
            {
                "evidence": {
                    "discussion_ids": list(gap.evidence.discussion_ids or []),
                }
            },
            ensure_ascii=False,
        )

        row = ContextGap(
            meeting_id=meeting_id,
            run_id=run_id,
            gap_kind=gap.gap_kind,
            category=gap.category,
            text=gap.text,
            rationale=gap.rationale,
            thread_id=thread_id,
            urgency=gap.urgency,
            status="open",
            first_seen_at=now,
            last_seen_at=now,
            evidence_from_run_id=run_id,
            source_discussion_id=source_discussion_id,
            source_text=gap.evidence.source_text,
            provenance_json=provenance,
            norm_hash=nh,
        )
        db.add(row)
        s.created += 1

    db.flush()
    log.info(
        "context_review apply meeting=%s run=%s created=%d updated=%d skipped=%d",
        meeting_id, run_id, s.created, s.updated, s.skipped_resolved,
    )
    return s
