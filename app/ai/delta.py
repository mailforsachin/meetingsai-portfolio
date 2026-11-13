"""Deterministic state delta computation (Phase D.1b).

The reducer produces entity-row mutations.  This module computes the
difference between the meeting state before a reducer run and the state
after, and returns a compact JSON-serializable delta.

The delta is our ground truth for "what changed."  The LLM does not
compute it; the deterministic reducer plus this diff do.
"""
from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.state import (
    ActionItem,
    Decision,
    Fact,
    Question,
    Risk,
)
from app.models.thread import Thread

log = logging.getLogger("meetingsai.ai.delta")


def snapshot_state_for_delta(db: Session, meeting_id: str) -> dict:
    """Return a lightweight snapshot of entity rows for diffing.

    Only the fields we need to detect changes.  No JSON blobs; no
    discussion segments.  Cheap to compute on every rolling run.
    """
    facts = {
        f.id: {"id": f.id, "key": f.key, "value": f.value, "status": f.status}
        for f in db.scalars(select(Fact).where(Fact.meeting_id == meeting_id)).all()
    }
    decisions = {
        d.id: {"id": d.id, "text": d.text, "status": d.status}
        for d in db.scalars(select(Decision).where(Decision.meeting_id == meeting_id)).all()
    }
    actions = {
        a.id: {
            "id": a.id, "text": a.text, "status": a.status,
            "owner": a.owner, "due": a.due,
        }
        for a in db.scalars(select(ActionItem).where(ActionItem.meeting_id == meeting_id)).all()
    }
    risks = {
        r.id: {"id": r.id, "text": r.text, "status": r.status, "severity": r.severity}
        for r in db.scalars(select(Risk).where(Risk.meeting_id == meeting_id)).all()
    }
    questions = {
        q.id: {"id": q.id, "text": q.text, "category": q.category, "status": q.status}
        for q in db.scalars(select(Question).where(Question.meeting_id == meeting_id)).all()
    }
    threads = {
        t.id: {"id": t.id, "key": t.key, "title": t.title, "status": t.status}
        for t in db.scalars(select(Thread).where(Thread.meeting_id == meeting_id)).all()
    }
    return {
        "facts": facts,
        "decisions": decisions,
        "actions": actions,
        "risks": risks,
        "questions": questions,
        "threads": threads,
    }


def compute_delta(pre: dict, post: dict, summary=None) -> dict:
    """Diff two snapshots and return the non-empty changes as a dict."""
    d: dict = {}

    # --- facts ---
    new_facts, updated_facts, superseded_facts = [], [], []
    for fid, row in post["facts"].items():
        if fid not in pre["facts"]:
            new_facts.append({"id": fid, "key": row["key"], "value": row["value"]})
        else:
            before = pre["facts"][fid]
            if before["value"] != row["value"]:
                updated_facts.append({"id": fid, "key": row["key"], "value": row["value"]})
            if before["status"] != row["status"] and row["status"] == "superseded":
                superseded_facts.append({"id": fid, "key": row["key"], "value": row["value"]})
    if new_facts:       d["new_facts"] = new_facts
    if updated_facts:   d["updated_facts"] = updated_facts
    if superseded_facts:d["superseded_facts"] = superseded_facts

    # --- threads ---
    new_threads = [
        {"id": tid, "key": row["key"], "title": row["title"]}
        for tid, row in post["threads"].items() if tid not in pre["threads"]
    ]
    if new_threads:
        d["new_threads"] = new_threads

    # --- decisions ---
    new_decisions = [
        {"id": did, "text": row["text"]}
        for did, row in post["decisions"].items() if did not in pre["decisions"]
    ]
    if new_decisions:
        d["new_decisions"] = new_decisions

    # --- actions + owner/due/status changes ---
    new_actions, status_changes, owner_changes, due_changes = [], [], [], []
    for aid, row in post["actions"].items():
        if aid not in pre["actions"]:
            new_actions.append({"id": aid, "text": row["text"], "owner": row["owner"]})
            continue
        before = pre["actions"][aid]
        if before["status"] != row["status"]:
            status_changes.append({"kind": "action", "id": aid,
                                   "from": before["status"], "to": row["status"]})
        if (before.get("owner") or None) != (row.get("owner") or None):
            owner_changes.append({"id": aid,
                                  "from": before.get("owner"), "to": row.get("owner")})
        if (before.get("due") or None) != (row.get("due") or None):
            due_changes.append({"id": aid,
                                "from": before.get("due"), "to": row.get("due")})
    if new_actions:   d["new_actions"] = new_actions
    if status_changes: d["status_changes"] = status_changes
    if owner_changes:  d["owner_changes"] = owner_changes
    if due_changes:    d["due_changes"] = due_changes

    # --- risks ---
    new_risks = [
        {"id": rid, "text": row["text"]}
        for rid, row in post["risks"].items() if rid not in pre["risks"]
    ]
    if new_risks:
        d["new_risks"] = new_risks

    # --- questions ---
    new_questions, resolved_questions = [], []
    for qid, row in post["questions"].items():
        if qid not in pre["questions"]:
            new_questions.append({"id": qid, "text": row["text"], "category": row["category"]})
        else:
            before = pre["questions"][qid]
            if before["status"] == "open" and row["status"] != "open":
                resolved_questions.append({"id": qid, "text": row["text"]})
    if new_questions:      d["new_questions"] = new_questions
    if resolved_questions: d["resolved_questions"] = resolved_questions

    # --- supersessions computed from summary if available ---
    if summary is not None and getattr(summary, "facts_superseded", 0):
        d["supersession_count"] = summary.facts_superseded
    if summary is not None and getattr(summary, "mode_persisted", None):
        d["mode_persisted"] = summary.mode_persisted

    return d
