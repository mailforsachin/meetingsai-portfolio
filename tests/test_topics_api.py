"""Tests for Topic Intelligence REST endpoints.

Covers auth, meeting scoping, cross-meeting isolation, pagination, status
filtering, detail response shape, latest-run selection, sources, brief,
sanitized failure categories, and input validation.  No live API calls.
Uses the shared in-memory SQLite fixtures from conftest.py.
"""
from __future__ import annotations

import json

import pytest

from app.models.discussion_segment import DiscussionSegment
from app.models.meeting import Meeting
from app.models.topic import MeetingTopic, TopicBrief, TopicResearchRun, TopicSource


def _mk_meeting(db, meeting_id="m1"):
    m = Meeting(id=meeting_id, title="T", status="active")
    db.add(m); db.commit(); db.refresh(m)
    return m


def _mk_topic(db, meeting_id, name="kafka", status="pending"):
    t = MeetingTopic(
        meeting_id=meeting_id, name=name, topic_type="technology",
        relevance="high", research_status=status,
    )
    db.add(t); db.commit(); db.refresh(t)
    return t


def _mk_run(db, topic_id, error=None, provider="model_only",
            grounding_status="model_only"):
    r = TopicResearchRun(
        topic_id=topic_id, provider=provider,
        grounding_status=grounding_status, model="gpt-4o-mini",
        tokens_used=5, latency_ms=10, error=error,
    )
    db.add(r); db.commit(); db.refresh(r)
    return r


def test_list_topics_requires_auth(anon_client, db_session):
    m = _mk_meeting(db_session)
    r = anon_client.get(f"/api/meetings/{m.id}/topics")
    assert r.status_code == 401


def test_get_topic_requires_auth(anon_client, db_session):
    m = _mk_meeting(db_session)
    t = _mk_topic(db_session, m.id)
    r = anon_client.get(f"/api/meetings/{m.id}/topics/{t.id}")
    assert r.status_code == 401


def test_list_topics_empty(client, db_session):
    m = _mk_meeting(db_session)
    r = client.get(f"/api/meetings/{m.id}/topics")
    assert r.status_code == 200
    assert r.json() == {"items": [], "total": 0, "limit": 50, "offset": 0}


def test_list_topics_returns_detected_topics(client, db_session):
    m = _mk_meeting(db_session)
    _mk_topic(db_session, m.id, name="kafka")
    _mk_topic(db_session, m.id, name="postgres")
    r = client.get(f"/api/meetings/{m.id}/topics")
    body = r.json()
    assert body["total"] == 2
    assert {t["name"] for t in body["items"]} == {"kafka", "postgres"}


def test_list_topics_pagination(client, db_session):
    m = _mk_meeting(db_session)
    for i in range(5):
        _mk_topic(db_session, m.id, name=f"t{i}")
    r = client.get(f"/api/meetings/{m.id}/topics?limit=2&offset=1")
    body = r.json()
    assert body["total"] == 5
    assert body["limit"] == 2
    assert body["offset"] == 1
    assert len(body["items"]) == 2


def test_list_topics_status_filter(client, db_session):
    m = _mk_meeting(db_session)
    _mk_topic(db_session, m.id, name="a", status="pending")
    _mk_topic(db_session, m.id, name="b", status="done")
    _mk_topic(db_session, m.id, name="c", status="failed")
    r = client.get(f"/api/meetings/{m.id}/topics?status=done")
    body = r.json()
    assert body["total"] == 1
    assert body["items"][0]["name"] == "b"


def test_list_topics_404_for_unknown_meeting(client):
    r = client.get("/api/meetings/does-not-exist/topics")
    assert r.status_code == 404


def test_list_topics_isolated_between_meetings(client, db_session):
    m1 = _mk_meeting(db_session, "m1")
    m2 = _mk_meeting(db_session, "m2")
    _mk_topic(db_session, m1.id, name="alpha")
    _mk_topic(db_session, m1.id, name="beta")
    _mk_topic(db_session, m2.id, name="gamma")
    r1 = client.get(f"/api/meetings/{m1.id}/topics")
    r2 = client.get(f"/api/meetings/{m2.id}/topics")
    assert {t["name"] for t in r1.json()["items"]} == {"alpha", "beta"}
    assert {t["name"] for t in r2.json()["items"]} == {"gamma"}


def test_list_topics_invalid_limit(client, db_session):
    m = _mk_meeting(db_session)
    r = client.get(f"/api/meetings/{m.id}/topics?limit=0")
    assert r.status_code == 422
    r = client.get(f"/api/meetings/{m.id}/topics?limit=201")
    assert r.status_code == 422


def test_list_topics_invalid_offset(client, db_session):
    m = _mk_meeting(db_session)
    r = client.get(f"/api/meetings/{m.id}/topics?offset=-1")
    assert r.status_code == 422


def test_list_topics_invalid_status(client, db_session):
    m = _mk_meeting(db_session)
    r = client.get(f"/api/meetings/{m.id}/topics?status=bogus")
    assert r.status_code == 422


def test_get_topic_detail_no_run(client, db_session):
    m = _mk_meeting(db_session)
    t = _mk_topic(db_session, m.id)
    r = client.get(f"/api/meetings/{m.id}/topics/{t.id}")
    assert r.status_code == 200
    body = r.json()
    assert body["topic"]["id"] == t.id
    assert body["latest_run"] is None
    assert body["sources"] == []
    assert body["brief"] is None


def test_get_topic_detail_returns_latest_run_sources_brief(client, db_session):
    m = _mk_meeting(db_session)
    t = _mk_topic(db_session, m.id)
    _mk_run(db_session, t.id, error=None)
    latest = _mk_run(db_session, t.id, error=None)
    db_session.add(TopicSource(
        research_run_id=latest.id, url="https://a.example",
        title="A", snippet="sa", citation_index=0,
    ))
    db_session.add(TopicSource(
        research_run_id=latest.id, url="https://b.example",
        title="B", snippet="sb", citation_index=1,
    ))
    db_session.add(TopicBrief(
        research_run_id=latest.id, summary="brief",
        brief_json=json.dumps({"summary": "brief"}),
    ))
    db_session.commit()

    r = client.get(f"/api/meetings/{m.id}/topics/{t.id}")
    body = r.json()
    assert body["topic"]["id"] == t.id
    assert body["latest_run"]["id"] == latest.id
    assert len(body["sources"]) == 2
    assert {s["url"] for s in body["sources"]} == {"https://a.example", "https://b.example"}
    assert body["brief"]["summary"] == "brief"


def test_get_topic_detail_404_for_other_meeting(client, db_session):
    m1 = _mk_meeting(db_session, "m1")
    m2 = _mk_meeting(db_session, "m2")
    t = _mk_topic(db_session, m1.id)
    r = client.get(f"/api/meetings/{m2.id}/topics/{t.id}")
    assert r.status_code == 404


def test_get_topic_detail_404_for_missing_topic(client, db_session):
    m = _mk_meeting(db_session)
    r = client.get(f"/api/meetings/{m.id}/topics/99999")
    assert r.status_code == 404


def test_failure_kind_missing_segment(client, db_session):
    m = _mk_meeting(db_session)
    t = _mk_topic(db_session, m.id)
    _mk_run(db_session, t.id,
            error="first_seen_discussion_id is null or segment not found")
    r = client.get(f"/api/meetings/{m.id}/topics/{t.id}")
    assert r.json()["latest_run"]["failure_kind"] == "missing_segment"


def test_failure_kind_provider_error_model_call(client, db_session):
    m = _mk_meeting(db_session)
    t = _mk_topic(db_session, m.id)
    _mk_run(db_session, t.id, error="model call failed: connection reset")
    r = client.get(f"/api/meetings/{m.id}/topics/{t.id}")
    assert r.json()["latest_run"]["failure_kind"] == "provider_error"


def test_failure_kind_provider_error_malformed_response(client, db_session):
    m = _mk_meeting(db_session)
    t = _mk_topic(db_session, m.id)
    _mk_run(db_session, t.id, error="model response malformed: choices empty")
    r = client.get(f"/api/meetings/{m.id}/topics/{t.id}")
    assert r.json()["latest_run"]["failure_kind"] == "provider_error"


def test_failure_kind_schema_error_invalid_json(client, db_session):
    m = _mk_meeting(db_session)
    t = _mk_topic(db_session, m.id)
    _mk_run(db_session, t.id, error="invalid JSON from model: Expecting value")
    r = client.get(f"/api/meetings/{m.id}/topics/{t.id}")
    assert r.json()["latest_run"]["failure_kind"] == "schema_error"


def test_failure_kind_schema_error_validation(client, db_session):
    m = _mk_meeting(db_session)
    t = _mk_topic(db_session, m.id)
    _mk_run(db_session, t.id, error="schema validation failed: summary missing")
    r = client.get(f"/api/meetings/{m.id}/topics/{t.id}")
    assert r.json()["latest_run"]["failure_kind"] == "schema_error"


def test_failure_kind_unknown(client, db_session):
    m = _mk_meeting(db_session)
    t = _mk_topic(db_session, m.id)
    _mk_run(db_session, t.id, error="something weird")
    r = client.get(f"/api/meetings/{m.id}/topics/{t.id}")
    assert r.json()["latest_run"]["failure_kind"] == "unknown"


def test_failure_kind_none_when_no_error(client, db_session):
    m = _mk_meeting(db_session)
    t = _mk_topic(db_session, m.id)
    _mk_run(db_session, t.id, error=None)
    r = client.get(f"/api/meetings/{m.id}/topics/{t.id}")
    assert r.json()["latest_run"]["failure_kind"] is None


def test_raw_error_text_not_in_response(client, db_session):
    m = _mk_meeting(db_session)
    t = _mk_topic(db_session, m.id)
    sensitive = "model call failed: internal-host.internal.corp:5432 timeout"
    _mk_run(db_session, t.id, error=sensitive)
    r = client.get(f"/api/meetings/{m.id}/topics/{t.id}")
    assert sensitive not in r.text
    assert "internal-host" not in r.text
    assert "5432" not in r.text
    assert r.json()["latest_run"]["failure_kind"] == "provider_error"


def test_raw_error_field_absent_from_latest_run(client, db_session):
    m = _mk_meeting(db_session)
    t = _mk_topic(db_session, m.id)
    _mk_run(db_session, t.id, error="model call failed: something")
    r = client.get(f"/api/meetings/{m.id}/topics/{t.id}")
    assert "error" not in r.json()["latest_run"]
    assert "failure_kind" in r.json()["latest_run"]


def test_brief_json_not_in_response(client, db_session):
    m = _mk_meeting(db_session)
    t = _mk_topic(db_session, m.id)
    run = _mk_run(db_session, t.id, error=None)
    marker = "RAW_BRIEF_JSON_MARKER_XYZ"
    db_session.add(TopicBrief(
        research_run_id=run.id, summary="ok",
        brief_json=json.dumps({"marker": marker}),
    ))
    db_session.commit()
    r = client.get(f"/api/meetings/{m.id}/topics/{t.id}")
    assert marker not in r.text
    body = r.json()
    assert body["brief"]["summary"] == "ok"
    assert "brief_json" not in body["brief"]


def test_raw_content_not_in_detail_response(client, db_session):
    m = _mk_meeting(db_session)
    t = _mk_topic(db_session, m.id)
    _mk_run(db_session, t.id, error=None)
    r = client.get(f"/api/meetings/{m.id}/topics/{t.id}")
    assert "raw_content" not in r.text


def test_listing_does_not_expose_diagnostics(client, db_session):
    m = _mk_meeting(db_session)
    _mk_topic(db_session, m.id, name="kafka")
    r = client.get(f"/api/meetings/{m.id}/topics")
    for item in r.json()["items"]:
        assert "error" not in item
        assert "failure_kind" not in item
