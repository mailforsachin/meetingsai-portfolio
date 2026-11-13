"""Stage C: detection input and topic-count caps.

Exercises the two new optional parameters on detect_topics_for_segment:
  - max_segment_chars: truncates segment.text before building the payload
  - max_new_topics:   truncates proposals before upsert, based on the
                      current topic count for the meeting

The tests stub AIAnalysisProvider.call_model with a fake that captures the
payload it received, so we can assert the truncation without a network call.
No DB beyond the in-memory SQLite engine.  No .env, no external calls.
"""
from __future__ import annotations

import json

import pytest

from app.ai.provider import AIAnalysisProvider, RawModelCall
from app.ai.topic_intelligence.detection import detect_topics_for_segment
from app.models.discussion_segment import DiscussionSegment
from app.models.meeting import Meeting
from app.models.topic import MeetingTopic


def _mk_meeting(db, meeting_id="m1"):
    m = Meeting(id=meeting_id, title="Test", objective="obj", organization="org")
    db.add(m); db.commit(); db.refresh(m)
    return m


def _mk_segment(db, meeting_id, text="seg"):
    s = DiscussionSegment(meeting_id=meeting_id, text=text)
    db.add(s); db.commit(); db.refresh(s)
    return s


class _CapturingProvider:
    """Fake provider that records the payload it was called with."""

    def __init__(self, topic_names):
        self.topic_names = topic_names
        self.captured_payload = None

    def call_model(self, prompt, payload):
        self.captured_payload = payload
        content = json.dumps({
            "topics": [
                {"name": n, "topic_type": "concept", "relevance": "medium"}
                for n in self.topic_names
            ]
        })
        return RawModelCall(
            content=content, model="gpt-4o-mini",
            input_tokens=1, output_tokens=1, latency_ms=1,
        )


def _patch(monkeypatch, provider):
    monkeypatch.setattr(
        "app.ai.provider.AIAnalysisProvider.call_model",
        provider.call_model,
        raising=True,
    )


# ---- max_segment_chars ----


def test_segment_text_truncated_to_max_chars(db_session, monkeypatch):
    m = _mk_meeting(db_session)
    s = _mk_segment(db_session, m.id, text="A" * 10000)
    prov = _CapturingProvider(["x"])
    _patch(monkeypatch, prov)

    detect_topics_for_segment(
        db_session, m.id, s.id, AIAnalysisProvider(api_key="k", model="m"),
        max_segment_chars=100,
    )

    sent = prov.captured_payload["discussion_segment"]["text"]
    assert len(sent) == 100


def test_segment_text_unchanged_when_under_cap(db_session, monkeypatch):
    m = _mk_meeting(db_session)
    s = _mk_segment(db_session, m.id, text="short")
    prov = _CapturingProvider(["x"])
    _patch(monkeypatch, prov)

    detect_topics_for_segment(
        db_session, m.id, s.id, AIAnalysisProvider(api_key="k", model="m"),
        max_segment_chars=100,
    )

    assert prov.captured_payload["discussion_segment"]["text"] == "short"


def test_segment_text_not_truncated_when_cap_is_none(db_session, monkeypatch):
    m = _mk_meeting(db_session)
    s = _mk_segment(db_session, m.id, text="B" * 500)
    prov = _CapturingProvider(["x"])
    _patch(monkeypatch, prov)

    detect_topics_for_segment(
        db_session, m.id, s.id, AIAnalysisProvider(api_key="k", model="m"),
        max_segment_chars=None,
    )

    assert len(prov.captured_payload["discussion_segment"]["text"]) == 500


# ---- max_new_topics ----


def _count(db, meeting_id):
    return db.query(MeetingTopic).filter_by(meeting_id=meeting_id).count()


def test_max_new_topics_truncates_proposals(db_session, monkeypatch):
    m = _mk_meeting(db_session)
    s = _mk_segment(db_session, m.id)
    prov = _CapturingProvider(["a", "b", "c", "d", "e"])
    _patch(monkeypatch, prov)

    detect_topics_for_segment(
        db_session, m.id, s.id, AIAnalysisProvider(api_key="k", model="m"),
        max_new_topics=2,
    )

    assert _count(db_session, m.id) == 2


def test_max_new_topics_zero_when_at_cap(db_session, monkeypatch):
    m = _mk_meeting(db_session)
    s = _mk_segment(db_session, m.id)
    for n in ("x", "y"):
        db_session.add(MeetingTopic(
            meeting_id=m.id, name=n, topic_type="concept",
            relevance="medium", research_status="pending",
        ))
    db_session.commit()
    assert _count(db_session, m.id) == 2

    prov = _CapturingProvider(["a", "b"])
    _patch(monkeypatch, prov)

    summary = detect_topics_for_segment(
        db_session, m.id, s.id, AIAnalysisProvider(api_key="k", model="m"),
        max_new_topics=2,
    )

    assert summary.succeeded is True
    assert summary.created == 0
    assert _count(db_session, m.id) == 2


def test_max_new_topics_partial_when_one_slot_left(db_session, monkeypatch):
    m = _mk_meeting(db_session)
    s = _mk_segment(db_session, m.id)
    db_session.add(MeetingTopic(
        meeting_id=m.id, name="existing", topic_type="concept",
        relevance="medium", research_status="pending",
    ))
    db_session.commit()

    prov = _CapturingProvider(["a", "b", "c"])
    _patch(monkeypatch, prov)

    summary = detect_topics_for_segment(
        db_session, m.id, s.id, AIAnalysisProvider(api_key="k", model="m"),
        max_new_topics=2,
    )

    assert summary.succeeded is True
    assert _count(db_session, m.id) == 2


def test_max_new_topics_none_means_unlimited(db_session, monkeypatch):
    m = _mk_meeting(db_session)
    s = _mk_segment(db_session, m.id)
    prov = _CapturingProvider(["a", "b", "c", "d", "e"])
    _patch(monkeypatch, prov)

    detect_topics_for_segment(
        db_session, m.id, s.id, AIAnalysisProvider(api_key="k", model="m"),
        max_new_topics=None,
    )

    assert _count(db_session, m.id) == 5


# ---- both caps together ----


def test_both_caps_applied_together(db_session, monkeypatch):
    m = _mk_meeting(db_session)
    s = _mk_segment(db_session, m.id, text="C" * 5000)
    prov = _CapturingProvider(["a", "b", "c"])
    _patch(monkeypatch, prov)

    detect_topics_for_segment(
        db_session, m.id, s.id, AIAnalysisProvider(api_key="k", model="m"),
        max_segment_chars=200,
        max_new_topics=1,
    )

    assert len(prov.captured_payload["discussion_segment"]["text"]) == 200
    assert _count(db_session, m.id) == 1
