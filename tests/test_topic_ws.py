"""Tests for Topic Intelligence WebSocket events.

Two capture strategies:

  - snapshot_capture:  callback opens a fresh SessionLocal at emission
                       time and records committed DB state alongside the
                       payload.  Used for tests 1, 4, 5.
  - plain_capture:     records the payload only, no DB access.  Used for
                       tests 2, 3, 6 (no cross-session interference).

Scheduling tests (7, 8) capture the actual asyncio.Task returned by
_broadcast_topic_event and await it directly -- no sleep-based pumping.
"""
from __future__ import annotations

import asyncio
import json

import pytest
from sqlalchemy.orm import Session, sessionmaker

from app.ai.topic_intelligence import scheduler as sched_mod
from app.models.discussion_segment import DiscussionSegment
from app.models.meeting import Meeting
from app.models.topic import MeetingTopic, TopicResearchRun
from app.ai.topic_intelligence.scheduler import _reset_watermark_for_tests


@pytest.fixture(autouse=True)
def _clear_watermark():
    """The detection watermark is module-level state.  Reset it before
    and after every test so a prior test's watermark does not silently
    filter out this test's segments.  Teardown runs even if the test
    fails (yield-based fixture)."""
    _reset_watermark_for_tests()
    yield
    _reset_watermark_for_tests()


def _mk_meeting(db, meeting_id="m1"):
    m = Meeting(id=meeting_id, title="T", status="active")
    db.add(m); db.commit(); db.refresh(m)
    return m


def _mk_segment(db, meeting_id, text="seg"):
    s = DiscussionSegment(meeting_id=meeting_id, text=text)
    db.add(s); db.commit(); db.refresh(s)
    return s


def _mk_topic(db, meeting_id, name="kafka", status="pending", segment_id=None):
    t = MeetingTopic(
        meeting_id=meeting_id, name=name, topic_type="technology",
        relevance="high", research_status=status,
        first_seen_discussion_id=segment_id,
    )
    db.add(t); db.commit(); db.refresh(t)
    return t


class _FakeDetector:
    def __init__(self, topics):
        self._topics = topics
    def call_model(self, prompt, payload):
        from app.ai.provider import RawModelCall
        return RawModelCall(
            content=json.dumps({"topics": self._topics}),
            model="m", input_tokens=1, output_tokens=1, latency_ms=1,
        )


class _FakeResearcher:
    def __init__(self, outcome="ok"):
        self.model = "gpt-4o-mini"
        self.outcome = outcome
    def research(self, topic_name, context):
        from app.ai.provider import AIProviderError
        from app.ai.topic_intelligence.provider import ResearchResult
        if self.outcome == "provider_fail":
            raise AIProviderError("boom")
        return ResearchResult(
            topic_name=topic_name, summary="brief",
            grounding_status="model_only", provider="model_only",
            model="gpt-4o-mini", tokens_used=1, tool_calls=None, latency_ms=1,
            raw_content="RAW",
            brief_json=json.dumps({"summary": "brief", "sources": [], "notes": None}),
            sources=[],
        )


def _bind_test_session(db_session, monkeypatch):
    import app.db.session as session_mod
    TestSession = sessionmaker(
        bind=db_session.get_bind(), autoflush=False, autocommit=False, future=True,
    )
    monkeypatch.setattr(session_mod, "SessionLocal", TestSession, raising=True)


@pytest.fixture
def plain_capture(monkeypatch):
    """Record payloads only.  No DB access.  Used where the test installs
    a custom (potentially failing) SessionLocal."""
    calls: list[dict] = []
    monkeypatch.setattr(
        sched_mod, "_broadcast_topic_event",
        lambda payload: calls.append(dict(payload)),
        raising=True,
    )
    return calls


@pytest.fixture
def snapshot_capture(monkeypatch):
    """Record payload + committed DB state at emission time."""
    snapshots: list[dict] = []

    def _capture(payload):
        snap = {"payload": dict(payload), "db": {}}
        import app.db.session as session_mod
        from app.models.topic import MeetingTopic, TopicResearchRun
        probe = session_mod.SessionLocal()
        try:
            if payload.get("kind") == "researched" and payload.get("topic_id"):
                topic = probe.get(MeetingTopic, payload["topic_id"])
                runs = probe.query(TopicResearchRun).filter_by(
                    topic_id=payload["topic_id"]
                ).all()
                snap["db"] = {
                    "topic_status": topic.research_status if topic else None,
                    "run_count": len(runs),
                    "run_errors": [r.error for r in runs],
                }
            elif payload.get("kind") == "detected":
                snap["db"] = {
                    "topic_count": probe.query(MeetingTopic).filter_by(
                        meeting_id=payload["meeting_id"]
                    ).count(),
                }
        finally:
            probe.close()
        snapshots.append(snap)

    monkeypatch.setattr(
        sched_mod, "_broadcast_topic_event", _capture, raising=True,
    )
    return snapshots


def test_detection_broadcasts_aggregate_event(db_session, snapshot_capture, monkeypatch):
    m = _mk_meeting(db_session)
    seg = _mk_segment(db_session, m.id)
    _bind_test_session(db_session, monkeypatch)
    fake = _FakeDetector(topics=[
        {"name": "kafka", "topic_type": "technology", "relevance": "high"},
        {"name": "postgres", "topic_type": "technology", "relevance": "medium"},
    ])
    monkeypatch.setattr(
        "app.ai.topic_intelligence.detection.AIAnalysisProvider.call_model",
        fake.call_model, raising=True,
    )
    sched_mod._run_detection_pass(db_session, m.id, fake)

    assert len(snapshot_capture) == 1
    snap = snapshot_capture[0]
    assert snap["payload"] == {
        "type": "topic_intelligence",
        "meeting_id": m.id,
        "kind": "detected",
        "topic_id": None,
        "status": None,
    }
    assert snap["db"]["topic_count"] == 2


def test_detection_no_event_when_zero_topics(db_session, plain_capture, monkeypatch):
    m = _mk_meeting(db_session)
    _mk_segment(db_session, m.id)
    fake = _FakeDetector(topics=[])
    monkeypatch.setattr(
        "app.ai.topic_intelligence.detection.AIAnalysisProvider.call_model",
        fake.call_model, raising=True,
    )
    sched_mod._run_detection_pass(db_session, m.id, fake)
    assert plain_capture == []


def test_detection_no_event_on_commit_failure(db_session, plain_capture, monkeypatch):
    """Commit failure must: call rollback, leave no persisted topics, and
    emit zero broadcasts."""
    m = _mk_meeting(db_session)
    _mk_segment(db_session, m.id)

    fake = _FakeDetector(topics=[
        {"name": "kafka", "topic_type": "technology", "relevance": "high"},
        {"name": "postgres", "topic_type": "technology", "relevance": "medium"},
    ])
    monkeypatch.setattr(
        "app.ai.topic_intelligence.detection.AIAnalysisProvider.call_model",
        fake.call_model, raising=True,
    )

    rollback_calls = {"n": 0}
    real_rollback = db_session.rollback
    def _spy_rollback():
        rollback_calls["n"] += 1
        return real_rollback()
    monkeypatch.setattr(db_session, "rollback", _spy_rollback, raising=True)

    def _bad_commit():
        raise RuntimeError("commit failed")
    monkeypatch.setattr(db_session, "commit", _bad_commit, raising=True)

    sched_mod._run_detection_pass(db_session, m.id, fake)

    assert rollback_calls["n"] == 1
    assert plain_capture == []
    db_session.expire_all()
    rows = db_session.query(MeetingTopic).filter_by(meeting_id=m.id).all()
    assert rows == []


def test_research_broadcasts_per_topic_event(db_session, snapshot_capture, monkeypatch):
    m = _mk_meeting(db_session)
    seg = _mk_segment(db_session, m.id)
    t = _mk_topic(db_session, m.id, name="kafka", status="pending", segment_id=seg.id)
    _bind_test_session(db_session, monkeypatch)
    fake = _FakeResearcher(outcome="ok")
    monkeypatch.setattr(
        "app.ai.topic_intelligence.research.ModelOnlyProvider.research",
        fake.research, raising=True,
    )
    sched_mod._run_research_pass(m.id, fake)

    assert len(snapshot_capture) == 1
    snap = snapshot_capture[0]
    assert snap["payload"]["kind"] == "researched"
    assert snap["payload"]["topic_id"] == t.id
    assert snap["payload"]["status"] == "done"
    assert snap["db"]["topic_status"] == "done"
    assert snap["db"]["run_count"] == 1
    assert snap["db"]["run_errors"] == [None]


def test_research_broadcasts_on_failed_research(db_session, snapshot_capture, monkeypatch):
    m = _mk_meeting(db_session)
    seg = _mk_segment(db_session, m.id)
    t = _mk_topic(db_session, m.id, name="kafka", status="pending", segment_id=seg.id)
    _bind_test_session(db_session, monkeypatch)
    fake = _FakeResearcher(outcome="provider_fail")
    monkeypatch.setattr(
        "app.ai.topic_intelligence.research.ModelOnlyProvider.research",
        fake.research, raising=True,
    )
    sched_mod._run_research_pass(m.id, fake)

    assert len(snapshot_capture) == 1
    snap = snapshot_capture[0]
    assert snap["payload"]["status"] == "failed"
    assert snap["db"]["topic_status"] == "failed"
    assert snap["db"]["run_count"] == 1
    assert snap["db"]["run_errors"][0] is not None


class _CommitFailSession(Session):
    def commit(self):  # type: ignore[override]
        raise RuntimeError("commit failed")


def test_research_no_event_on_commit_failure(db_session, plain_capture, monkeypatch):
    m = _mk_meeting(db_session)
    seg = _mk_segment(db_session, m.id)
    t = _mk_topic(db_session, m.id, name="kafka", status="pending", segment_id=seg.id)

    fake = _FakeResearcher(outcome="ok")
    monkeypatch.setattr(
        "app.ai.topic_intelligence.research.ModelOnlyProvider.research",
        fake.research, raising=True,
    )

    bind = db_session.get_bind()
    Factory = sessionmaker(
        bind=bind, class_=_CommitFailSession,
        autoflush=False, autocommit=False, future=True,
    )
    import app.db.session as session_mod
    monkeypatch.setattr(session_mod, "SessionLocal", Factory, raising=True)

    sched_mod._run_research_pass(m.id, fake)

    assert plain_capture == []
    db_session.expire_all()
    t2 = db_session.get(MeetingTopic, t.id)
    assert t2.research_status == "pending"
    runs = db_session.query(TopicResearchRun).filter_by(topic_id=t.id).all()
    assert runs == []


def test_broadcast_topic_event_schedules_and_swallows(monkeypatch):
    from app.ai.topic_intelligence import scheduler as sm

    calls = []
    async def _explode(meeting_id, payload):
        calls.append((meeting_id, payload))
        raise RuntimeError("boom")

    monkeypatch.setattr(sm.manager, "broadcast_json", _explode, raising=True)

    async def _go():
        task = sm._broadcast_topic_event({
            "type": "topic_intelligence", "meeting_id": "m1",
            "kind": "detected", "topic_id": None, "status": None,
        })
        assert task is not None
        await task
        pending = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
        assert pending == []

    asyncio.run(_go())
    assert calls == [("m1", {
        "type": "topic_intelligence", "meeting_id": "m1",
        "kind": "detected", "topic_id": None, "status": None,
    })]


def test_broadcast_topic_event_schedules_success(monkeypatch):
    from app.ai.topic_intelligence import scheduler as sm

    calls = []
    async def _ok(meeting_id, payload):
        calls.append((meeting_id, payload))

    monkeypatch.setattr(sm.manager, "broadcast_json", _ok, raising=True)

    async def _go():
        task = sm._broadcast_topic_event({
            "type": "topic_intelligence", "meeting_id": "m2",
            "kind": "researched", "topic_id": 42, "status": "done",
        })
        assert task is not None
        await task
        pending = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
        assert pending == []

    asyncio.run(_go())
    assert calls == [("m2", {
        "type": "topic_intelligence", "meeting_id": "m2",
        "kind": "researched", "topic_id": 42, "status": "done",
    })]


def test_broadcast_topic_event_no_loop_returns_none():
    from app.ai.topic_intelligence import scheduler as sm
    result = sm._broadcast_topic_event({
        "type": "topic_intelligence", "meeting_id": "m1",
        "kind": "detected", "topic_id": None, "status": None,
    })
    assert result is None


def test_safe_broadcast_awaits_and_swallows(monkeypatch):
    from app.ai.topic_intelligence import scheduler as sm

    calls = []
    async def _explode(meeting_id, payload):
        calls.append((meeting_id, payload))
        raise RuntimeError("boom")

    monkeypatch.setattr(sm.manager, "broadcast_json", _explode, raising=True)
    asyncio.run(sm._safe_broadcast("m1", {"kind": "detected"}))
    assert calls == [("m1", {"kind": "detected"})]


def test_safe_broadcast_awaits_success(monkeypatch):
    from app.ai.topic_intelligence import scheduler as sm

    calls = []
    async def _ok(meeting_id, payload):
        calls.append((meeting_id, payload))

    monkeypatch.setattr(sm.manager, "broadcast_json", _ok, raising=True)
    asyncio.run(sm._safe_broadcast("m1", {"kind": "researched", "topic_id": 42}))
    assert calls == [("m1", {"kind": "researched", "topic_id": 42})]
