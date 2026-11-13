"""Tests for the Context Review reducer.

These are fixture-driven. No network. No real LLM. They lock in the
semantics of the reducer that we designed against the real-meeting
feedback: owner gaps, decision gaps, cost gaps, deadline gaps,
dependency gaps, dedup across successive runs, non-resurrection of
resolved gaps, and thread-key hygiene.
"""
from __future__ import annotations

import json

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db.base import Base
import app.models  # noqa: F401
from app.ai.context_review_reducer import apply_context_review, _norm_hash
from app.models.context_gap import ContextGap
from app.models.context_review import ContextReviewRun
from app.models.meeting import Meeting
from app.models.thread import Thread
from app.schemas.context_review import (
    ContextReviewOutput,
    ProposedGap,
    ProposedGapEvidence,
)


def _db():
    eng = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
        future=True,
    )
    Base.metadata.create_all(eng)
    return sessionmaker(bind=eng, autoflush=False, autocommit=False, future=True)()


def _meeting(db):
    m = Meeting(title="KHC - M365 Copilot", status="active")
    db.add(m); db.commit(); db.refresh(m)
    return m


def _run(db, meeting_id):
    r = ContextReviewRun(
        meeting_id=meeting_id,
        prompt_version="0.1.0",
        model="gpt-4o-mini",
        status="running",
    )
    db.add(r); db.commit(); db.refresh(r)
    return r


def _gap(**kw):
    base = dict(
        gap_kind="owner_gap",
        category="contextual",
        text="Who owns the next action for the 22 SharePoint sites?",
        urgency="high",
        evidence={"discussion_ids": [518], "source_text": "22 SharePoint sites"},
    )
    base.update(kw)
    return ProposedGap.model_validate(base)


def _out(gaps):
    return ContextReviewOutput.model_validate({"gaps": gaps, "threads": [], "notes": None})


# ---------- core scenarios ----------

def test_owner_gap_for_sharepoint_sites_created():
    db = _db(); m = _meeting(db); r = _run(db, m.id)
    s = apply_context_review(db, m.id, r.id, _out([_gap()]))
    db.commit()
    assert s.created == 1 and s.updated == 0
    g = db.query(ContextGap).one()
    assert g.gap_kind == "owner_gap"
    assert "SharePoint" in g.text
    assert g.urgency == "high"
    assert g.status == "open"


def test_decision_gap_for_legal_governance():
    db = _db(); m = _meeting(db); r = _run(db, m.id)
    s = apply_context_review(db, m.id, r.id, _out([_gap(
        gap_kind="decision_gap",
        category="decision_gap",
        text="Who is the business/legal decision owner for the governance controls?",
        urgency="critical",
    )]))
    db.commit()
    assert s.created == 1
    g = db.query(ContextGap).one()
    assert g.gap_kind == "decision_gap"
    assert g.urgency == "critical"


def test_cost_gap_for_licensing_estimate_tentative_not_fact():
    db = _db(); m = _meeting(db); r = _run(db, m.id)
    s = apply_context_review(db, m.id, r.id, _out([_gap(
        gap_kind="cost_gap",
        category="contextual",
        text="Is the approximately $100K/year licensing estimate confirmed, and which controls does it cover?",
        urgency="high",
    )]))
    db.commit()
    assert s.created == 1
    # Verify: no `facts` row was created by Context Review.
    from app.models.state import Fact
    assert db.query(Fact).count() == 0


def test_deadline_gap_for_legal_approval():
    db = _db(); m = _meeting(db); r = _run(db, m.id)
    s = apply_context_review(db, m.id, r.id, _out([_gap(
        gap_kind="deadline_gap",
        category="deadline_gap",
        text="By what date does Legal need to provide its decision on the governance controls?",
        urgency="high",
    )]))
    db.commit()
    assert s.created == 1
    g = db.query(ContextGap).one()
    assert g.gap_kind == "deadline_gap"


def test_dependency_gap_for_microsoft_friday():
    db = _db(); m = _meeting(db); r = _run(db, m.id)
    s = apply_context_review(db, m.id, r.id, _out([_gap(
        gap_kind="dependency_gap",
        category="dependency_gap",
        text="If Microsoft does not provide the required information by Friday, what is the fallback plan?",
        urgency="high",
    )]))
    db.commit()
    assert s.created == 1
    g = db.query(ContextGap).one()
    assert g.gap_kind == "dependency_gap"


# ---------- dedup ----------

def test_duplicate_gap_across_runs_is_updated_not_recreated():
    db = _db(); m = _meeting(db)
    r1 = _run(db, m.id); r2 = _run(db, m.id)

    apply_context_review(db, m.id, r1.id, _out([_gap()])); db.commit()
    s2 = apply_context_review(db, m.id, r2.id, _out([_gap()])); db.commit()

    assert db.query(ContextGap).count() == 1
    assert s2.created == 0
    assert s2.updated == 1


def test_urgency_raised_but_not_lowered():
    db = _db(); m = _meeting(db)
    r1 = _run(db, m.id); r2 = _run(db, m.id); r3 = _run(db, m.id)

    apply_context_review(db, m.id, r1.id, _out([_gap(urgency="high")])); db.commit()
    apply_context_review(db, m.id, r2.id, _out([_gap(urgency="critical")])); db.commit()
    apply_context_review(db, m.id, r3.id, _out([_gap(urgency="medium")])); db.commit()

    g = db.query(ContextGap).one()
    assert g.urgency == "critical"


def test_provenance_merges_across_runs():
    db = _db(); m = _meeting(db)
    r1 = _run(db, m.id); r2 = _run(db, m.id)

    g1 = _gap(evidence={"discussion_ids": [100], "source_text": "first sighting"})
    g2 = _gap(evidence={"discussion_ids": [200], "source_text": "second sighting"})
    apply_context_review(db, m.id, r1.id, _out([g1])); db.commit()
    apply_context_review(db, m.id, r2.id, _out([g2])); db.commit()

    g = db.query(ContextGap).one()
    prov = json.loads(g.provenance_json)
    assert sorted(prov["evidence"]["discussion_ids"]) == [100, 200]


# ---------- resolution ----------

def test_resolved_gap_is_not_resurrected():
    db = _db(); m = _meeting(db)
    r1 = _run(db, m.id); r2 = _run(db, m.id)

    apply_context_review(db, m.id, r1.id, _out([_gap()])); db.commit()
    g = db.query(ContextGap).one()
    g.status = "resolved"; db.commit()

    s2 = apply_context_review(db, m.id, r2.id, _out([_gap()])); db.commit()
    assert db.query(ContextGap).count() == 1
    assert s2.created == 0
    assert s2.skipped_resolved == 1
    g = db.query(ContextGap).one()
    assert g.status == "resolved"


# ---------- threads ----------

def test_numeric_thread_key_rejected_goes_to_unthreaded():
    db = _db(); m = _meeting(db); r = _run(db, m.id)
    s = apply_context_review(db, m.id, r.id, _out([_gap(thread_key="13")]))
    db.commit()
    assert s.threads_rejected == 1
    threads = db.query(Thread).all()
    assert len(threads) == 1
    assert threads[0].key == "__unthreaded__"
    g = db.query(ContextGap).one()
    assert g.thread_id == threads[0].id


def test_valid_thread_key_creates_thread():
    db = _db(); m = _meeting(db); r = _run(db, m.id)
    apply_context_review(db, m.id, r.id, _out([_gap(thread_key="sharepoint_governance")]))
    db.commit()
    t = db.query(Thread).one()
    assert t.key == "sharepoint_governance"


# ---------- schema validation ----------

def test_null_gaps_and_threads_coerce_to_empty():
    out = ContextReviewOutput.model_validate({"gaps": None, "threads": None, "notes": None})
    assert out.gaps == []
    assert out.threads == []


def test_norm_hash_is_stable_across_case_and_punctuation():
    a = _norm_hash("Who owns the 22 SharePoint sites?")
    b = _norm_hash("who owns the 22 sharepoint sites")
    assert a == b
