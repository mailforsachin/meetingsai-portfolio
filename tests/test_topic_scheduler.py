"""Tests for the Topic Intelligence scheduler.

All tests use in-memory SQLite and mock providers.  No live API calls.
The scheduler's internal helpers are imported directly so we can test
retry eligibility, watermark semantics, and per-topic isolation without
spinning the async loop.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta

import pytest

from app.ai.topic_intelligence import scheduler as sched
from app.ai.topic_intelligence.scheduler import (
    MAX_RESEARCH_ATTEMPTS,
    _RETRY_COOLDOWN_SECONDS,
    _detection_watermark,
    _eligible_topics,
    _is_terminal_failure,
    _research_eligible,
    _reset_watermark_for_tests,
    _run_detection_pass,
    _run_research_pass,
    _segments_to_process,
    run_topic_intelligence_scheduler,
)
from app.models.discussion_segment import DiscussionSegment
from app.models.meeting import Meeting
from app.models.topic import MeetingTopic, TopicResearchRun


@pytest.fixture(autouse=True)
def _clear_watermark():
    _reset_watermark_for_tests()
    yield
    _reset_watermark_for_tests()


def _mk_meeting(db, meeting_id="m1"):
    m = Meeting(id=meeting_id, title="Test", status="active")
    db.add(m); db.commit(); db.refresh(m)
    return m


def _mk_segment(db, meeting_id, text="seg"):
    s = DiscussionSegment(meeting_id=meeting_id, text=text)
    db.add(s); db.commit(); db.refresh(s)
    return s


def _mk_topic(db, meeting_id, name="kafka", status="pending", segment_id=None):
    t = MeetingTopic(
        meeting_id=meeting_id, name=name,
        topic_type="technology", relevance="high",
        research_status=status, first_seen_discussion_id=segment_id,
    )
    db.add(t); db.commit(); db.refresh(t)
    return t


def _mk_run(db, topic_id, error=None, created_at=None):
    r = TopicResearchRun(
        topic_id=topic_id,
        provider="model_only", grounding_status="model_only",
        model="gpt-4o-mini", error=error,
    )
    if created_at is not None:
        r.created_at = created_at
    db.add(r); db.commit(); db.refresh(r)
    return r


# ---- retry eligibility and cooldown ----

def test_pending_topic_with_no_runs_is_eligible(db_session):
    m = _mk_meeting(db_session)
    t = _mk_topic(db_session, m.id, status="pending")
    assert _research_eligible(db_session, t, datetime.utcnow()) is True


def test_done_topic_is_not_eligible(db_session):
    m = _mk_meeting(db_session)
    t = _mk_topic(db_session, m.id, status="done")
    assert _research_eligible(db_session, t, datetime.utcnow()) is False


def test_failed_topic_with_one_run_immediate_retry_blocked(db_session):
    """Attempt 2 needs 120 s cooldown."""
    m = _mk_meeting(db_session)
    t = _mk_topic(db_session, m.id, status="failed")
    _mk_run(db_session, t.id, error="boom", created_at=datetime.utcnow())
    assert _research_eligible(db_session, t, datetime.utcnow()) is False


def test_failed_topic_eligible_after_120s(db_session):
    m = _mk_meeting(db_session)
    t = _mk_topic(db_session, m.id, status="failed")
    past = datetime.utcnow() - timedelta(seconds=121)
    _mk_run(db_session, t.id, error="boom", created_at=past)
    assert _research_eligible(db_session, t, datetime.utcnow()) is True


def test_failed_topic_attempt_2_needs_300s_before_attempt_3(db_session):
    m = _mk_meeting(db_session)
    t = _mk_topic(db_session, m.id, status="failed")
    past = datetime.utcnow() - timedelta(seconds=400)
    _mk_run(db_session, t.id, error="boom", created_at=past)
    _mk_run(db_session, t.id, error="boom", created_at=past)
    # 2 runs exist -> next would be attempt 3 -> 300 s cooldown -> 400 s elapsed OK
    assert _research_eligible(db_session, t, datetime.utcnow()) is True


def test_failed_topic_three_runs_is_terminal(db_session):
    m = _mk_meeting(db_session)
    t = _mk_topic(db_session, m.id, status="failed")
    past = datetime.utcnow() - timedelta(seconds=1000)
    for _ in range(3):
        _mk_run(db_session, t.id, error="boom", created_at=past)
    assert _research_eligible(db_session, t, datetime.utcnow()) is False


def test_missing_segment_failure_is_terminal(db_session):
    m = _mk_meeting(db_session)
    t = _mk_topic(db_session, m.id, status="failed")
    past = datetime.utcnow() - timedelta(seconds=1000)
    _mk_run(db_session, t.id, error="first_seen_discussion_id is null or segment not found",
            created_at=past)
    assert _research_eligible(db_session, t, datetime.utcnow()) is False


def test_eligible_topics_bounded_by_limit(db_session):
    m = _mk_meeting(db_session)
    for i in range(10):
        _mk_topic(db_session, m.id, name=f"t{i}", status="pending")
    ids = _eligible_topics(db_session, m.id, limit=3)
    assert len(ids) == 3


def test_eligible_topics_skips_done_and_terminal(db_session):
    m = _mk_meeting(db_session)
    a = _mk_topic(db_session, m.id, name="a", status="pending")
    b = _mk_topic(db_session, m.id, name="b", status="done")
    c = _mk_topic(db_session, m.id, name="c", status="failed")
    past = datetime.utcnow() - timedelta(seconds=1000)
    for _ in range(3):
        _mk_run(db_session, c.id, error="boom", created_at=past)
    ids = _eligible_topics(db_session, m.id, limit=10)
    assert ids == [a.id]


# ---- detection watermark ----

def test_segments_to_process_returns_all_when_no_watermark(db_session):
    m = _mk_meeting(db_session)
    ids = [_mk_segment(db_session, m.id).id for _ in range(5)]
    got = _segments_to_process(db_session, m.id, limit=10)
    assert got == ids


def test_segments_to_process_skips_below_watermark(db_session):
    m = _mk_meeting(db_session)
    segs = [_mk_segment(db_session, m.id).id for _ in range(5)]
    _detection_watermark[m.id] = segs[2]
    got = _segments_to_process(db_session, m.id, limit=10)
    assert got == segs[3:]


def test_segments_to_process_bounded(db_session):
    m = _mk_meeting(db_session)
    for _ in range(10):
        _mk_segment(db_session, m.id)
    got = _segments_to_process(db_session, m.id, limit=3)
    assert len(got) == 3


class _FakeDetectionProvider:
    """Minimal stand-in for AIAnalysisProvider.call_model."""
    def __init__(self, mode="ok", topics=None):
        self.mode = mode
        self.topics = topics or []
    def call_model(self, prompt, payload):
        from app.ai.provider import AIProviderError, RawModelCall
        if self.mode == "provider_fail":
            raise AIProviderError("boom")
        if self.mode == "schema_fail":
            return RawModelCall(content="{not json", model="m",
                                input_tokens=1, output_tokens=1, latency_ms=1)
        return RawModelCall(
            content=json.dumps({"topics": self.topics}),
            model="m", input_tokens=1, output_tokens=1, latency_ms=1,
        )


def test_detection_pass_advances_watermark_on_success(db_session, monkeypatch):
    m = _mk_meeting(db_session)
    seg = _mk_segment(db_session, m.id)
    monkeypatch.setattr(
        "app.ai.topic_intelligence.detection.AIAnalysisProvider.call_model",
        _FakeDetectionProvider(mode="ok").call_model, raising=True,
    )
    prov = _FakeDetectionProvider(mode="ok")
    _run_detection_pass(db_session, m.id, prov)
    assert _detection_watermark.get(m.id) == seg.id


def test_detection_pass_advances_on_provider_failure(db_session, monkeypatch):
    m = _mk_meeting(db_session)
    seg = _mk_segment(db_session, m.id)
    prov = _FakeDetectionProvider(mode="provider_fail")
    _run_detection_pass(db_session, m.id, prov)
    assert _detection_watermark.get(m.id) == seg.id


def test_detection_pass_advances_on_schema_failure(db_session, monkeypatch):
    m = _mk_meeting(db_session)
    seg = _mk_segment(db_session, m.id)
    prov = _FakeDetectionProvider(mode="schema_fail")
    _run_detection_pass(db_session, m.id, prov)
    assert _detection_watermark.get(m.id) == seg.id


def test_detection_pass_does_not_advance_when_skipped(db_session):
    m = _mk_meeting(db_session)
    # segment_id points to a nonexistent segment -> skipped
    prov = _FakeDetectionProvider(mode="ok")
    _run_detection_pass(db_session, m.id, prov)
    # No segments exist for this meeting, so nothing to process; watermark unset.
    assert _detection_watermark.get(m.id) is None


def test_detection_pass_does_not_advance_on_db_commit_error(db_session, monkeypatch):
    m = _mk_meeting(db_session)
    seg = _mk_segment(db_session, m.id)
    prov = _FakeDetectionProvider(mode="ok")

    real_commit = db_session.commit
    def _bad_commit():
        raise RuntimeError("commit failed")
    monkeypatch.setattr(db_session, "commit", _bad_commit, raising=True)
    _run_detection_pass(db_session, m.id, prov)
    # commit failed -> watermark must NOT advance.
    assert _detection_watermark.get(m.id) is None
    # Restore commit for fixture teardown.
    monkeypatch.setattr(db_session, "commit", real_commit, raising=True)


class _FakeResearchProvider:
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
            raw_content="RAW", brief_json=json.dumps({"summary": "brief", "sources": [], "notes": None}),
            sources=[],
        )


def test_research_pass_isolates_db_failure_per_topic(db_session, monkeypatch):
    """One topic's DB failure must not roll back a sibling's committed run."""
    m = _mk_meeting(db_session)
    seg = _mk_segment(db_session, m.id)
    ok_topic = _mk_topic(db_session, m.id, name="ok", status="pending", segment_id=seg.id)
    bad_topic = _mk_topic(db_session, m.id, name="bad", status="pending", segment_id=seg.id)

    # Patch research_pending_topic to fail for bad_topic only, via a
    # real call for ok_topic that uses the fake provider.
    from app.ai.topic_intelligence import scheduler as sched_mod
    from app.ai.topic_intelligence import research as research_mod

    real_research = research_mod.research_pending_topic

    def _selective(db, meeting_id, topic_id, provider):
        if topic_id == bad_topic.id:
            raise RuntimeError("simulated db failure")
        return real_research(db, meeting_id, topic_id, provider)

    monkeypatch.setattr(
        "app.ai.topic_intelligence.scheduler.research_pending_topic",
        _selective, raising=True,
    )
    _run_research_pass(m.id, _FakeResearchProvider(outcome="ok"))

    ok_runs = db_session.query(TopicResearchRun).filter_by(topic_id=ok_topic.id).count()
    bad_runs = db_session.query(TopicResearchRun).filter_by(topic_id=bad_topic.id).count()
    assert ok_runs == 1
    assert bad_runs == 0


import asyncio


def test_scheduler_exits_when_stop_event_set(monkeypatch):
    async def _go():
        stop = asyncio.Event()
        stop.set()
        await run_topic_intelligence_scheduler(stop)
    asyncio.run(_go())


def test_scheduler_does_not_call_providers_when_disabled(monkeypatch):
    """With topic_intelligence_enabled=False, no provider calls occur."""
    from app.core.config import get_settings
    monkeypatch.setenv("TOPIC_INTELLIGENCE_ENABLED", "false")
    get_settings.cache_clear()

    calls = {"n": 0}

    async def _fake_run_one(meeting_id):
        calls["n"] += 1

    monkeypatch.setattr(
        "app.ai.topic_intelligence.scheduler._run_one",
        _fake_run_one, raising=True,
    )

    async def _go():
        stop = asyncio.Event()
        # Set a very short timeout before stopping.
        async def _stopper():
            await asyncio.sleep(0.05)
            stop.set()
        await asyncio.gather(
            run_topic_intelligence_scheduler(stop),
            _stopper(),
        )
    asyncio.run(_go())
    get_settings.cache_clear()
    assert calls["n"] == 0


def test_is_terminal_failure_recognizes_missing_segment(db_session):
    m = _mk_meeting(db_session)
    t = _mk_topic(db_session, m.id, status="failed")
    r = _mk_run(db_session, t.id, error="first_seen_discussion_id is null or segment not found")
    assert _is_terminal_failure(r) is True


def test_is_terminal_failure_false_for_other_errors(db_session):
    m = _mk_meeting(db_session)
    t = _mk_topic(db_session, m.id, status="failed")
    r = _mk_run(db_session, t.id, error="boom")
    assert _is_terminal_failure(r) is False


def test_retry_cooldown_table_matches_policy():
    assert _RETRY_COOLDOWN_SECONDS == {2: 120, 3: 300}


def test_max_research_attempts_is_three():
    assert MAX_RESEARCH_ATTEMPTS == 3
