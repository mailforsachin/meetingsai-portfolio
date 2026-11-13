"""Phase H1: model-facing payload and rolling reducer thread-key alignment.

These tests lock in the H1 fix:

- The LLM-facing payload exposes semantic `thread_key` strings, not
  database `thread_id` integers.  `current_state.threads[]` lists existing
  semantic threads.
- The rolling reducer rejects numeric and id-like thread keys, routing
  them to the per-meeting `__unthreaded__` sentinel — mirroring
  `context_review_reducer.py`.
- The rolling prompt v0.3.0 contains the extraction rules that drive
  higher-density output.
"""
from __future__ import annotations

import json

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db.base import Base
import app.models  # noqa: F401
from app.ai.prompt_loader import load_prompt
from app.ai.reducer import apply_rolling_output
from app.ai.state_builder import build_llm_payload
from app.models.discussion_segment import DiscussionSegment
from app.models.meeting import Meeting
from app.models.state import Fact
from app.models.thread import Thread
from app.schemas.ai_output import RollingAnalysisOutput


# ---------------------------------------------------------------- fixtures

def _db():
    eng = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
        future=True,
    )
    Base.metadata.create_all(eng)
    return sessionmaker(bind=eng, autoflush=False, autocommit=False, future=True)()


def _meeting(db, mode=None) -> Meeting:
    m = Meeting(title="h1-test", status="active", mode=mode, mode_inferred=False)
    db.add(m); db.commit(); db.refresh(m)
    return m


def _thread(db, meeting_id: str, key: str, title: str | None = None) -> Thread:
    t = Thread(meeting_id=meeting_id, key=key, title=(title or key), status="open")
    db.add(t); db.commit(); db.refresh(t)
    return t


def _discussion(db, meeting_id: str, text: str) -> DiscussionSegment:
    d = DiscussionSegment(
        meeting_id=meeting_id,
        text=text,
        segment_count=1,
        token_estimate=max(1, len(text) // 4),
        processing_status="done",
    )
    db.add(d); db.commit(); db.refresh(d)
    return d


def _out(**kw) -> RollingAnalysisOutput:
    base = dict(
        understanding="u",
        key_discussion=[],
        facts=[],
        decisions=[],
        action_items=[],
        risks=[],
        questions_now=[],
        questions_later=[],
        potential_response=None,
        missing_information=[],
        contradictions=[],
        next_steps=[],
        threads=[],
    )
    base.update(kw)
    return RollingAnalysisOutput.model_validate(base)


# ------------------------------------------------------- payload shape (A)

def test_payload_exposes_threads_with_key_not_id():
    db = _db()
    m = _meeting(db)
    t = _thread(db, m.id, "project_scope", "Project Scope")
    assert t.id is not None

    payload, _ = build_llm_payload(db, m, recent_limit=10)
    threads = payload["current_state"]["threads"]
    assert len(threads) == 1
    entry = threads[0]
    assert entry["key"] == "project_scope"
    assert entry["title"] == "Project Scope"
    assert "id" not in entry, "database Thread.id must not appear in the LLM payload"


def test_payload_items_use_thread_key_not_thread_id():
    db = _db()
    m = _meeting(db)
    t = _thread(db, m.id, "project_scope")
    d = _discussion(db, m.id, "some discussion text")
    f = Fact(
        meeting_id=m.id, key="k1", value="v1", status="confirmed",
        source_discussion_id=d.id, thread_id=t.id,
    )
    db.add(f); db.commit()

    payload, _ = build_llm_payload(db, m, recent_limit=10)
    facts = payload["current_state"]["facts"]
    assert len(facts) == 1
    fact = facts[0]
    assert fact["thread_key"] == "project_scope"
    assert "thread_id" not in fact, "raw thread_id must not appear on items"


def test_payload_facts_keep_id_for_supersession():
    db = _db()
    m = _meeting(db)
    d = _discussion(db, m.id, "x")
    f = Fact(
        meeting_id=m.id, key="k1", value="v1", status="confirmed",
        source_discussion_id=d.id,
    )
    db.add(f); db.commit(); db.refresh(f)

    payload, _ = build_llm_payload(db, m, recent_limit=10)
    fact = payload["current_state"]["facts"][0]
    assert "id" in fact, "facts must keep their id so supersedes_fact_id can reference them"
    assert fact["id"] == f.id


def test_payload_contains_no_numeric_thread_keys():
    db = _db()
    m = _meeting(db)
    t = _thread(db, m.id, "project_scope")
    d = _discussion(db, m.id, "x")
    f = Fact(
        meeting_id=m.id, key="k1", value="v1", status="confirmed",
        source_discussion_id=d.id, thread_id=t.id,
    )
    db.add(f); db.commit()

    payload, _ = build_llm_payload(db, m, recent_limit=10)
    serialized = json.dumps(payload, default=str)
    assert '"thread_key": "1"' not in serialized
    assert '"thread_key": "35"' not in serialized
    assert '"thread_key": "36"' not in serialized
    assert '"thread_id"' not in serialized


# -------------------------------------------- rolling reducer alignment (B)

def test_rolling_reducer_rejects_numeric_thread_key_to_unthreaded():
    db = _db()
    m = _meeting(db)
    apply_rolling_output(db, m.id, _out(facts=[{
        "key": "k1", "value": "v1", "status": "confirmed",
        "thread_key": "35",
    }]))
    db.commit()
    threads = db.query(Thread).all()
    assert len(threads) == 1
    assert threads[0].key == "__unthreaded__"
    f = db.query(Fact).one()
    assert f.thread_id == threads[0].id


def test_rolling_reducer_rejects_id_like_thread_key_to_unthreaded():
    db = _db()
    m = _meeting(db)
    apply_rolling_output(db, m.id, _out(facts=[{
        "key": "k1", "value": "v1", "status": "confirmed",
        "thread_key": "disc_17",
    }]))
    db.commit()
    threads = db.query(Thread).all()
    assert len(threads) == 1
    assert threads[0].key == "__unthreaded__"


def test_rolling_reducer_reuses_existing_semantic_thread():
    db = _db()
    m = _meeting(db)
    t = _thread(db, m.id, "project_scope")
    apply_rolling_output(db, m.id, _out(facts=[{
        "key": "k1", "value": "v1", "status": "confirmed",
        "thread_key": "project_scope",
    }]))
    db.commit()
    threads = db.query(Thread).all()
    assert len(threads) == 1
    f = db.query(Fact).one()
    assert f.thread_id == t.id


def test_rolling_reducer_creates_new_semantic_thread():
    db = _db()
    m = _meeting(db)
    apply_rolling_output(db, m.id, _out(facts=[{
        "key": "k1", "value": "v1", "status": "confirmed",
        "thread_key": "legal_review",
    }]))
    db.commit()
    t = db.query(Thread).one()
    assert t.key == "legal_review"


# -------------------------------------------------- prompt v0.3.0 (C)

def test_prompt_v040_has_action_extraction_rule():
    p = load_prompt("meeting_analysis.md")
    assert p.version == "0.4.0"
    assert "Action items are first-class outputs" in p.body
    assert "Extract them aggressively" in p.body
    assert "Missing fields do NOT justify dropping the action" in p.body


def test_prompt_v040_has_numeric_thread_key_rule():
    p = load_prompt("meeting_analysis.md")
    assert "Never use numeric-only thread keys" in p.body
    assert 'Never emit `"35"`, `"36"`, `"37"`' in p.body


def test_prompt_v040_has_evidence_hierarchy():
    p = load_prompt("meeting_analysis.md")
    assert "PRIMARY EVIDENCE" in p.body
    assert "PRIOR STATE" in p.body
    assert "not as truth" in p.body

def test_prompt_v040_has_worked_examples():
    """v0.4.0 adds few-shot examples for actions, decisions, risks."""
    p = load_prompt("meeting_analysis.md")
    assert "Worked example" in p.body
    assert "Send the PPT to John" in p.body
    assert "Send out the NDA" in p.body
    assert "Set up a Monday meeting invite" in p.body
    assert "Use AWS deployment for the POC" in p.body
    assert "Risk of raising commercial terms" in p.body


def test_prompt_v040_has_owner_rules():
    """v0.4.0 clarifies that first-person commitments do not establish owner."""
    p = load_prompt("meeting_analysis.md")
    assert "Owner rules" in p.body
    assert "do NOT infer the speaker" in p.body
    assert "owner: null" in p.body


def test_prompt_v040_has_self_check():
    """v0.4.0 adds a final self-check block before returning."""
    p = load_prompt("meeting_analysis.md")
    assert "Final self-check before returning" in p.body
    assert "Do not emit empty arrays by default" in p.body
