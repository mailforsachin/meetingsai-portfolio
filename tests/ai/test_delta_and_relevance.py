"""Tests for Phase D: thread creation, relevance persistence, delta diff,
meeting mode persistence, action previous_status on status change.
"""
from __future__ import annotations

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db.base import Base
import app.models  # noqa: F401
from app.ai.delta import compute_delta, snapshot_state_for_delta
from app.ai.reducer import apply_rolling_output
from app.models.meeting import Meeting
from app.models.state import ActionItem, Fact
from app.models.thread import Thread
from app.schemas.ai_output import RollingAnalysisOutput


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
    m = Meeting(title="t", status="active")
    db.add(m); db.commit(); db.refresh(m)
    return m


def _out(**kw):
    base = dict(understanding="u", key_discussion=[], facts=[], decisions=[],
                action_items=[], risks=[], questions_now=[], questions_later=[],
                potential_response=None, missing_information=[], contradictions=[],
                next_steps=[])
    base.update(kw)
    return RollingAnalysisOutput.model_validate(base)


def test_reducer_creates_thread_from_thread_key():
    db = _db(); m = _meeting(db)
    apply_rolling_output(db, m.id, _out(facts=[{
        "key": "device_migration",
        "value": "7 devices offline",
        "status": "confirmed",
        "thread_key": "device_migration",
    }]))
    db.commit()
    threads = db.query(Thread).filter_by(meeting_id=m.id).all()
    assert len(threads) == 1
    assert threads[0].key == "device_migration"
    f = db.query(Fact).one()
    assert f.thread_id == threads[0].id


def test_reducer_persists_meeting_relevance():
    db = _db(); m = _meeting(db)
    apply_rolling_output(db, m.id, _out(facts=[{
        "key": "small_talk_1",
        "value": "someone has a chihuahua",
        "status": "confirmed",
        "meeting_relevance": "incidental",
    }]))
    db.commit()
    f = db.query(Fact).one()
    assert f.meeting_relevance == "incidental"


def test_reducer_persists_mode_when_missing():
    db = _db(); m = _meeting(db)
    assert m.mode is None
    apply_rolling_output(db, m.id, _out(meeting_mode_proposal="action_tracker"))
    db.commit()
    db.refresh(m)
    assert m.mode == "action_tracker"
    assert m.mode_inferred is True


def test_reducer_keeps_existing_mode():
    db = _db(); m = _meeting(db); m.mode = "discovery"; db.commit()
    apply_rolling_output(db, m.id, _out(meeting_mode_proposal="action_tracker"))
    db.commit(); db.refresh(m)
    assert m.mode == "discovery"


def test_action_previous_status_on_transition():
    db = _db(); m = _meeting(db)
    apply_rolling_output(db, m.id, _out(action_items=[{
        "text": "Escalate documentation",
        "owner": "FDE", "due": "today", "status": "open",
    }]))
    db.commit()
    a = db.query(ActionItem).one()
    # Second call with previous_status suggesting a transition to 'done'
    apply_rolling_output(db, m.id, _out(action_items=[{
        "text": "Escalate documentation",
        "previous_status": "done",
    }]))
    db.commit()
    a = db.query(ActionItem).one()
    assert a.status == "done"
    assert a.previous_status == "open"


def test_delta_detects_new_facts_and_threads():
    db = _db(); m = _meeting(db)
    pre = snapshot_state_for_delta(db, m.id)
    apply_rolling_output(db, m.id, _out(facts=[{
        "key": "documentation_status",
        "value": "outstanding",
        "status": "tentative",
        "thread_key": "documentation",
    }]))
    db.commit()
    post = snapshot_state_for_delta(db, m.id)
    delta = compute_delta(pre, post)
    assert "new_facts" in delta and len(delta["new_facts"]) == 1
    assert delta["new_facts"][0]["key"] == "documentation_status"
    assert "new_threads" in delta and len(delta["new_threads"]) == 1
    assert delta["new_threads"][0]["key"] == "documentation"


def test_delta_detects_action_status_change():
    db = _db(); m = _meeting(db)
    apply_rolling_output(db, m.id, _out(action_items=[{
        "text": "Clarify device ownership", "owner": "FDE", "status": "open",
    }]))
    db.commit()
    pre = snapshot_state_for_delta(db, m.id)
    # Transition to done
    apply_rolling_output(db, m.id, _out(action_items=[{
        "text": "Clarify device ownership",
        "previous_status": "done",
    }]))
    db.commit()
    post = snapshot_state_for_delta(db, m.id)
    delta = compute_delta(pre, post)
    assert "status_changes" in delta
    ch = delta["status_changes"][0]
    assert ch["kind"] == "action" and ch["from"] == "open" and ch["to"] == "done"


def test_build_state_out_filters_incidental_by_default():
    from app.ai.state_builder import build_state_out
    db = _db(); m = _meeting(db)
    apply_rolling_output(db, m.id, _out(facts=[
        {"key": "critical_fact", "value": "v", "status": "confirmed",
         "meeting_relevance": "critical"},
        {"key": "noise_fact", "value": "v", "status": "confirmed",
         "meeting_relevance": "incidental"},
    ]))
    db.commit()
    default = build_state_out(db, m.id, include_incidental=False)
    assert len(default.facts) == 1
    assert default.facts[0].key == "critical_fact"
    assert default.incidental_counts.get("facts") == 1

    full = build_state_out(db, m.id, include_incidental=True)
    assert len(full.facts) == 2
