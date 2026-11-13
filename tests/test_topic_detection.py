"""Tests for the Topic Intelligence detection pass.

All tests mock AIAnalysisProvider.call_model.  No live API calls.
Detection functions never commit or rollback -- callers own the
transaction.
"""
from __future__ import annotations

import json

import pytest
from sqlalchemy.exc import SQLAlchemyError

from app.ai.provider import AIProviderError
from app.ai.topic_intelligence.detection import (
    DetectionSummary,
    detect_topics_for_segment,
    _topic_lookup_key,
    _topic_display_name,
)
from app.models.discussion_segment import DiscussionSegment
from app.models.meeting import Meeting
from app.models.topic import MeetingTopic


def _mk_meeting(db, meeting_id="m1", objective="Test objective", org="TestOrg"):
    m = Meeting(id=meeting_id, title="Test", objective=objective, organization=org)
    db.add(m); db.commit(); db.refresh(m)
    return m


def _mk_segment(db, meeting_id, text="segment text"):
    s = DiscussionSegment(meeting_id=meeting_id, text=text)
    db.add(s); db.commit(); db.refresh(s)
    return s


class _FakeCall:
    def __init__(self, content):
        from app.ai.provider import RawModelCall
        self._r = RawModelCall(
            content=content, model="gpt-4o-mini",
            input_tokens=1, output_tokens=1, latency_ms=1,
        )
    def __getattr__(self, name):
        return getattr(self._r, name)


def _patch_call_model(monkeypatch, *, content=None, raise_exc=None):
    def _fake(self, prompt, payload):
        if raise_exc is not None:
            raise raise_exc
        return _FakeCall(content)
    monkeypatch.setattr(
        "app.ai.provider.AIAnalysisProvider.call_model",
        _fake,
        raising=True,
    )
    return object()  # provider stand-in; class is enough


def _provider():
    from app.ai.provider import AIAnalysisProvider
    return AIAnalysisProvider(api_key="k", model="gpt-4o-mini")


# ---- normalization helpers ----

def test_topic_lookup_key_casefolds_and_collapses():
    assert _topic_lookup_key("Vector   Search") == "vector search"
    assert _topic_lookup_key("  KAFKA ") == "kafka"


def test_topic_display_name_preserves_case_collapses_whitespace():
    assert _topic_display_name("  Vector   Search  ") == "Vector Search"
    assert _topic_display_name("Kafka") == "Kafka"


# ---- happy path ----

def test_detect_creates_topics(db_session, monkeypatch):
    m = _mk_meeting(db_session)
    s = _mk_segment(db_session, m.id)
    body = {"topics": [
        {"name": "Kafka", "topic_type": "technology", "relevance": "high"},
        {"name": "event-driven streaming", "topic_type": "concept", "relevance": "medium"},
    ]}
    _patch_call_model(monkeypatch, content=json.dumps(body))
    summary = detect_topics_for_segment(db_session, m.id, s.id, _provider())
    assert summary.succeeded is True
    assert summary.created == 2
    assert summary.updated == 0
    rows = db_session.query(MeetingTopic).filter_by(meeting_id=m.id).all()
    assert len(rows) == 2
    names = {r.name for r in rows}
    assert names == {"Kafka", "event-driven streaming"}
    for r in rows:
        assert r.research_status == "pending"
        assert r.first_seen_discussion_id == s.id


def test_detect_empty_topics_list_is_success(db_session, monkeypatch):
    m = _mk_meeting(db_session)
    s = _mk_segment(db_session, m.id)
    _patch_call_model(monkeypatch, content=json.dumps({"topics": []}))
    summary = detect_topics_for_segment(db_session, m.id, s.id, _provider())
    assert summary.succeeded is True
    assert summary.created == 0
    assert summary.updated == 0
    assert summary.provider_failed is False
    assert summary.schema_failed is False


def test_detect_updates_existing_topic(db_session, monkeypatch):
    m = _mk_meeting(db_session)
    s1 = _mk_segment(db_session, m.id, "first")
    s2 = _mk_segment(db_session, m.id, "second")
    body = {"topics": [
        {"name": "Kafka", "topic_type": "technology", "relevance": "high"},
    ]}
    _patch_call_model(monkeypatch, content=json.dumps(body))
    summary1 = detect_topics_for_segment(db_session, m.id, s1.id, _provider())
    assert summary1.created == 1
    summary2 = detect_topics_for_segment(db_session, m.id, s2.id, _provider())
    assert summary2.created == 0
    assert summary2.updated == 1
    assert db_session.query(MeetingTopic).filter_by(meeting_id=m.id).count() == 1


def test_detect_case_insensitive_dedup(db_session, monkeypatch):
    m = _mk_meeting(db_session)
    s1 = _mk_segment(db_session, m.id, "first")
    s2 = _mk_segment(db_session, m.id, "second")
    _patch_call_model(monkeypatch, content=json.dumps({
        "topics": [{"name": "Kafka", "topic_type": "technology", "relevance": "high"}]
    }))
    detect_topics_for_segment(db_session, m.id, s1.id, _provider())
    _patch_call_model(monkeypatch, content=json.dumps({
        "topics": [{"name": "kafka", "topic_type": "technology", "relevance": "high"}]
    }))
    detect_topics_for_segment(db_session, m.id, s2.id, _provider())
    assert db_session.query(MeetingTopic).filter_by(meeting_id=m.id).count() == 1


def test_detect_whitespace_normalized_dedup(db_session, monkeypatch):
    m = _mk_meeting(db_session)
    s1 = _mk_segment(db_session, m.id, "first")
    s2 = _mk_segment(db_session, m.id, "second")
    _patch_call_model(monkeypatch, content=json.dumps({
        "topics": [{"name": "vector   search", "topic_type": "concept", "relevance": "medium"}]
    }))
    detect_topics_for_segment(db_session, m.id, s1.id, _provider())
    _patch_call_model(monkeypatch, content=json.dumps({
        "topics": [{"name": "vector search", "topic_type": "concept", "relevance": "medium"}]
    }))
    detect_topics_for_segment(db_session, m.id, s2.id, _provider())
    assert db_session.query(MeetingTopic).filter_by(meeting_id=m.id).count() == 1


def test_detect_preserves_first_display_name(db_session, monkeypatch):
    m = _mk_meeting(db_session)
    s1 = _mk_segment(db_session, m.id, "first")
    s2 = _mk_segment(db_session, m.id, "second")
    _patch_call_model(monkeypatch, content=json.dumps({
        "topics": [{"name": "Vector Search", "topic_type": "concept", "relevance": "low"}]
    }))
    detect_topics_for_segment(db_session, m.id, s1.id, _provider())
    _patch_call_model(monkeypatch, content=json.dumps({
        "topics": [{"name": "vector search", "topic_type": "concept", "relevance": "low"}]
    }))
    detect_topics_for_segment(db_session, m.id, s2.id, _provider())
    row = db_session.query(MeetingTopic).filter_by(meeting_id=m.id).one()
    assert row.name == "Vector Search"


def test_detect_updates_relevance_if_higher(db_session, monkeypatch):
    m = _mk_meeting(db_session)
    s1 = _mk_segment(db_session, m.id, "first")
    s2 = _mk_segment(db_session, m.id, "second")
    _patch_call_model(monkeypatch, content=json.dumps({
        "topics": [{"name": "kafka", "topic_type": "technology", "relevance": "low"}]
    }))
    detect_topics_for_segment(db_session, m.id, s1.id, _provider())
    _patch_call_model(monkeypatch, content=json.dumps({
        "topics": [{"name": "kafka", "topic_type": "technology", "relevance": "high"}]
    }))
    detect_topics_for_segment(db_session, m.id, s2.id, _provider())
    row = db_session.query(MeetingTopic).filter_by(meeting_id=m.id).one()
    assert row.relevance == "high"


def test_detect_does_not_downgrade_relevance(db_session, monkeypatch):
    m = _mk_meeting(db_session)
    s1 = _mk_segment(db_session, m.id, "first")
    s2 = _mk_segment(db_session, m.id, "second")
    _patch_call_model(monkeypatch, content=json.dumps({
        "topics": [{"name": "kafka", "topic_type": "technology", "relevance": "high"}]
    }))
    detect_topics_for_segment(db_session, m.id, s1.id, _provider())
    _patch_call_model(monkeypatch, content=json.dumps({
        "topics": [{"name": "kafka", "topic_type": "technology", "relevance": "low"}]
    }))
    detect_topics_for_segment(db_session, m.id, s2.id, _provider())
    row = db_session.query(MeetingTopic).filter_by(meeting_id=m.id).one()
    assert row.relevance == "high"


# ---- failure outcomes ----

def test_detect_provider_error_returns_structured_outcome(db_session, monkeypatch):
    m = _mk_meeting(db_session)
    s = _mk_segment(db_session, m.id)
    _patch_call_model(monkeypatch, raise_exc=AIProviderError("boom"))
    summary = detect_topics_for_segment(db_session, m.id, s.id, _provider())
    assert summary.succeeded is False
    assert summary.provider_failed is True
    assert summary.schema_failed is False
    assert summary.skipped is False
    assert summary.error and "boom" in summary.error
    assert db_session.query(MeetingTopic).count() == 0


def test_detect_schema_error_returns_structured_outcome(db_session, monkeypatch):
    m = _mk_meeting(db_session)
    s = _mk_segment(db_session, m.id)
    _patch_call_model(monkeypatch, content="{not json")
    summary = detect_topics_for_segment(db_session, m.id, s.id, _provider())
    assert summary.succeeded is False
    assert summary.provider_failed is False
    assert summary.schema_failed is True
    assert summary.skipped is False
    assert summary.error is not None
    assert db_session.query(MeetingTopic).count() == 0


def test_detect_skips_when_meeting_missing(db_session, monkeypatch):
    _patch_call_model(monkeypatch, content=json.dumps({"topics": []}))
    summary = detect_topics_for_segment(db_session, "nonexistent", 1, _provider())
    assert summary.skipped is True
    assert summary.succeeded is False
    assert summary.reason == "meeting_not_found"


def test_detect_skips_when_segment_missing(db_session, monkeypatch):
    m = _mk_meeting(db_session)
    _patch_call_model(monkeypatch, content=json.dumps({"topics": []}))
    summary = detect_topics_for_segment(db_session, m.id, 99999, _provider())
    assert summary.skipped is True
    assert summary.succeeded is False
    assert summary.reason == "segment_not_found"


def test_detect_db_flush_error_propagates(db_session, monkeypatch):
    m = _mk_meeting(db_session)
    s = _mk_segment(db_session, m.id)
    _patch_call_model(monkeypatch, content=json.dumps({
        "topics": [{"name": "kafka", "topic_type": "technology", "relevance": "high"}]
    }))
    def _bad_flush():
        raise SQLAlchemyError("flush failed")
    monkeypatch.setattr(db_session, "flush", _bad_flush, raising=True)
    with pytest.raises(SQLAlchemyError):
        detect_topics_for_segment(db_session, m.id, s.id, _provider())


def test_detect_db_query_error_propagates(db_session, monkeypatch):
    m = _mk_meeting(db_session)
    s = _mk_segment(db_session, m.id)
    _patch_call_model(monkeypatch, content=json.dumps({"topics": []}))
    def _bad_get(model, pk):
        raise SQLAlchemyError("get failed")
    monkeypatch.setattr(db_session, "get", _bad_get, raising=True)
    with pytest.raises(SQLAlchemyError):
        detect_topics_for_segment(db_session, m.id, s.id, _provider())


def test_detect_creates_topics_with_valid_fk(db_session, monkeypatch):
    m = _mk_meeting(db_session)
    s = _mk_segment(db_session, m.id)
    _patch_call_model(monkeypatch, content=json.dumps({
        "topics": [{"name": "kafka", "topic_type": "technology", "relevance": "high"}]
    }))
    detect_topics_for_segment(db_session, m.id, s.id, _provider())
    row = db_session.query(MeetingTopic).one()
    db_session.refresh(m); db_session.refresh(s)
    assert row.meeting_id == m.id
    assert row.first_seen_discussion_id == s.id
