"""Tests for the Topic Intelligence research pass.

All tests mock ModelOnlyProvider.research.  No live API calls.
research_pending_topic never commits or rolls back -- callers own the
transaction.
"""
from __future__ import annotations

import json

import pytest
from sqlalchemy.exc import SQLAlchemyError

from app.ai.provider import AIProviderError
from app.ai.topic_intelligence.parsing import TopicSchemaError
from app.ai.topic_intelligence.provider import ResearchResult, ResearchSource
from app.ai.topic_intelligence.research import (
    RESEARCH_CONTEXT_MAX_CHARS,
    _build_research_context,
    _resolve_context_segment,
    research_pending_topic,
)
from app.models.discussion_segment import DiscussionSegment
from app.models.meeting import Meeting
from app.models.topic import MeetingTopic, TopicBrief, TopicResearchRun, TopicSource


def _mk_meeting(db, meeting_id="m1", objective="Test objective"):
    m = Meeting(id=meeting_id, title="Test", objective=objective)
    db.add(m); db.commit(); db.refresh(m)
    return m


def _mk_segment(db, meeting_id, text="segment context text"):
    s = DiscussionSegment(meeting_id=meeting_id, text=text)
    db.add(s); db.commit(); db.refresh(s)
    return s


def _mk_topic(db, meeting_id, name="kafka", segment_id=None):
    t = MeetingTopic(
        meeting_id=meeting_id, name=name,
        topic_type="technology", relevance="high",
        research_status="pending",
        first_seen_discussion_id=segment_id,
    )
    db.add(t); db.commit(); db.refresh(t)
    return t


def _mk_result(topic_name="kafka", summary="brief", sources=None):
    return ResearchResult(
        topic_name=topic_name,
        summary=summary,
        grounding_status="model_only",
        provider="model_only",
        model="gpt-4o-mini",
        tokens_used=30, tool_calls=None, latency_ms=7,
        raw_content="RAW MODEL CONTENT SHOULD NOT PERSIST",
        brief_json=json.dumps({"summary": summary, "sources": [], "notes": None}),
        sources=sources or [],
    )


class _FakeProvider:
    def __init__(self, result=None, raise_exc=None):
        self.model = "gpt-4o-mini"
        self._result = result
        self._raise = raise_exc
    def research(self, topic_name, context):
        if self._raise is not None:
            raise self._raise
        return self._result


def test_research_persists_run_source_brief(db_session):
    m = _mk_meeting(db_session)
    s = _mk_segment(db_session, m.id)
    t = _mk_topic(db_session, m.id, segment_id=s.id)
    result = _mk_result(sources=[
        ResearchSource(url="https://a.example", title="A", snippet="sa", citation_index=None),
        ResearchSource(url="https://b.example", title="B", snippet="sb", citation_index=None),
    ])
    provider = _FakeProvider(result=result)
    out = research_pending_topic(db_session, m.id, t.id, provider)
    assert out is result
    runs = db_session.query(TopicResearchRun).all()
    assert len(runs) == 1
    run = runs[0]
    assert run.provider == "model_only"
    assert run.grounding_status == "model_only"
    assert run.model == "gpt-4o-mini"
    assert run.tokens_used == 30
    assert run.latency_ms == 7
    assert run.error is None
    briefs = db_session.query(TopicBrief).all()
    assert len(briefs) == 1
    assert briefs[0].research_run_id == run.id
    assert briefs[0].summary == "brief"
    sources = db_session.query(TopicSource).all()
    assert len(sources) == 2
    for src in sources:
        assert src.research_run_id == run.id
        assert src.citation_index is None


def test_research_sets_topic_status_done(db_session):
    m = _mk_meeting(db_session)
    s = _mk_segment(db_session, m.id)
    t = _mk_topic(db_session, m.id, segment_id=s.id)
    research_pending_topic(db_session, m.id, t.id, _FakeProvider(result=_mk_result()))
    db_session.refresh(t)
    assert t.research_status == "done"


def test_research_empty_sources_creates_no_source_rows(db_session):
    m = _mk_meeting(db_session)
    s = _mk_segment(db_session, m.id)
    t = _mk_topic(db_session, m.id, segment_id=s.id)
    research_pending_topic(db_session, m.id, t.id, _FakeProvider(result=_mk_result(sources=[])))
    assert db_session.query(TopicSource).count() == 0
    assert db_session.query(TopicResearchRun).count() == 1
    assert db_session.query(TopicBrief).count() == 1


def test_research_provider_error_records_failure(db_session):
    m = _mk_meeting(db_session)
    s = _mk_segment(db_session, m.id)
    t = _mk_topic(db_session, m.id, segment_id=s.id)
    provider = _FakeProvider(raise_exc=AIProviderError("boom"))
    out = research_pending_topic(db_session, m.id, t.id, provider)
    assert out is None
    runs = db_session.query(TopicResearchRun).all()
    assert len(runs) == 1
    assert runs[0].error is not None
    assert "boom" in runs[0].error
    assert runs[0].grounding_status == "model_only"
    assert db_session.query(TopicSource).count() == 0
    assert db_session.query(TopicBrief).count() == 0
    db_session.refresh(t)
    assert t.research_status == "failed"


def test_research_schema_error_records_failure(db_session):
    m = _mk_meeting(db_session)
    s = _mk_segment(db_session, m.id)
    t = _mk_topic(db_session, m.id, segment_id=s.id)
    provider = _FakeProvider(raise_exc=TopicSchemaError("bad schema"))
    out = research_pending_topic(db_session, m.id, t.id, provider)
    assert out is None
    run = db_session.query(TopicResearchRun).one()
    assert "bad schema" in run.error
    db_session.refresh(t)
    assert t.research_status == "failed"


def test_research_missing_segment_skips_provider(db_session):
    m = _mk_meeting(db_session)
    t = _mk_topic(db_session, m.id, segment_id=None)  # no first_seen
    called = {"n": 0}
    class _SpyProvider:
        model = "gpt-4o-mini"
        def research(self, topic_name, context):
            called["n"] += 1
            return _mk_result()
    research_pending_topic(db_session, m.id, t.id, _SpyProvider())
    assert called["n"] == 0
    run = db_session.query(TopicResearchRun).one()
    assert "first_seen_discussion_id is null" in run.error
    db_session.refresh(t)
    assert t.research_status == "failed"


def test_research_missing_segment_row_skips_provider(db_session):
    m = _mk_meeting(db_session)
    t = _mk_topic(db_session, m.id, segment_id=99999)  # nonexistent segment
    called = {"n": 0}
    class _SpyProvider:
        model = "gpt-4o-mini"
        def research(self, topic_name, context):
            called["n"] += 1
            return _mk_result()
    research_pending_topic(db_session, m.id, t.id, _SpyProvider())
    assert called["n"] == 0
    run = db_session.query(TopicResearchRun).one()
    assert run.error is not None
    db_session.refresh(t)
    assert t.research_status == "failed"


def test_research_rollback_leaves_no_partial_rows(db_session):
    """Atomicity: if the caller rolls back, nothing survives."""
    m = _mk_meeting(db_session)
    s = _mk_segment(db_session, m.id)
    t = _mk_topic(db_session, m.id, segment_id=s.id)
    result = _mk_result(sources=[
        ResearchSource(url="https://a.example", title="A", snippet="sa", citation_index=None),
    ])
    research_pending_topic(db_session, m.id, t.id, _FakeProvider(result=result))
    db_session.rollback()
    assert db_session.query(TopicResearchRun).count() == 0
    assert db_session.query(TopicSource).count() == 0
    assert db_session.query(TopicBrief).count() == 0
    db_session.refresh(t)
    assert t.research_status == "pending"


def test_research_does_not_commit_internally(db_session):
    """Spy on commit: research_pending_topic must never call it."""
    m = _mk_meeting(db_session)
    s = _mk_segment(db_session, m.id)
    t = _mk_topic(db_session, m.id, segment_id=s.id)
    commits = {"n": 0}
    real_commit = db_session.commit
    def _spy_commit():
        commits["n"] += 1
        return real_commit()
    db_session.commit = _spy_commit
    research_pending_topic(db_session, m.id, t.id, _FakeProvider(result=_mk_result()))
    assert commits["n"] == 0


def test_research_does_not_persist_raw_content(db_session):
    """raw_content is diagnostics only; it must not appear in any field."""
    m = _mk_meeting(db_session)
    s = _mk_segment(db_session, m.id)
    t = _mk_topic(db_session, m.id, segment_id=s.id)
    result = _mk_result()
    research_pending_topic(db_session, m.id, t.id, _FakeProvider(result=result))

    # The raw marker must not appear in any persisted column value.
    marker = "RAW MODEL CONTENT SHOULD NOT PERSIST"
    for row in db_session.query(TopicResearchRun).all():
        for col in ("provider", "grounding_status", "model", "error"):
            val = getattr(row, col)
            assert val is None or marker not in str(val)
    for row in db_session.query(TopicBrief).all():
        assert marker not in str(row.summary or "")
        assert marker not in str(row.brief_json or "")
        assert marker not in str(row.conceptual_diagram_mermaid or "")
    for row in db_session.query(TopicSource).all():
        for col in ("url", "title", "snippet"):
            val = getattr(row, col)
            assert val is None or marker not in str(val)


def test_research_fk_integrity(db_session):
    m = _mk_meeting(db_session)
    s = _mk_segment(db_session, m.id)
    t = _mk_topic(db_session, m.id, segment_id=s.id)
    result = _mk_result(sources=[
        ResearchSource(url="https://a.example", title="A", snippet="sa", citation_index=None),
    ])
    research_pending_topic(db_session, m.id, t.id, _FakeProvider(result=result))
    run = db_session.query(TopicResearchRun).one()
    brief = db_session.query(TopicBrief).one()
    src = db_session.query(TopicSource).one()
    assert run.topic_id == t.id
    assert brief.research_run_id == run.id
    assert src.research_run_id == run.id


def test_research_skips_when_topic_missing(db_session):
    m = _mk_meeting(db_session)
    out = research_pending_topic(db_session, m.id, 99999, _FakeProvider(result=_mk_result()))
    assert out is None
    assert db_session.query(TopicResearchRun).count() == 0


def test_research_skips_when_meeting_missing(db_session):
    out = research_pending_topic(db_session, "nonexistent", 1, _FakeProvider(result=_mk_result()))
    assert out is None
    assert db_session.query(TopicResearchRun).count() == 0


def test_research_db_flush_error_propagates(db_session):
    m = _mk_meeting(db_session)
    s = _mk_segment(db_session, m.id)
    t = _mk_topic(db_session, m.id, segment_id=s.id)
    def _bad_flush():
        raise SQLAlchemyError("flush failed")
    db_session.flush = _bad_flush
    with pytest.raises(SQLAlchemyError):
        research_pending_topic(db_session, m.id, t.id, _FakeProvider(result=_mk_result()))


def test_resolve_context_segment_returns_missing_when_null(db_session):
    m = _mk_meeting(db_session)
    t = _mk_topic(db_session, m.id, segment_id=None)
    text, missing = _resolve_context_segment(db_session, t)
    assert missing is True
    assert text is None


def test_resolve_context_segment_returns_missing_when_row_gone(db_session):
    m = _mk_meeting(db_session)
    t = _mk_topic(db_session, m.id, segment_id=99999)
    text, missing = _resolve_context_segment(db_session, t)
    assert missing is True
    assert text is None


def test_build_research_context_includes_objective(db_session):
    m = _mk_meeting(db_session, objective="Scope Phase 2")
    ctx = _build_research_context(m, "some discussion text")
    assert "Scope Phase 2" in ctx
    assert "some discussion text" in ctx


def test_build_research_context_bounded(db_session):
    m = _mk_meeting(db_session, objective="obj")
    huge = "x" * (RESEARCH_CONTEXT_MAX_CHARS * 2)
    ctx = _build_research_context(m, huge)
    assert len(ctx) <= RESEARCH_CONTEXT_MAX_CHARS
