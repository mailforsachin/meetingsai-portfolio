"""Stage C: the double gate inside _run_one.

The existing test test_scheduler_does_not_call_providers_when_disabled
covers the outer scheduler-loop gate (enabled=False short-circuits before
_run_one is ever called).  This file covers the gate INSIDE _run_one:

  - enabled=False -> _run_one returns without calling detection or research
  - test_mode=True, empty test_meeting_id -> returns, no calls
  - test_mode=True, non-matching id -> returns, no calls
  - test_mode=True, matching id -> proceeds to detection and research

Calls to detection and research are replaced with mocks so we can assert
they were not invoked.  No network, no .env, no external calls.
"""
from __future__ import annotations

import asyncio

from app.core.config import get_settings


def _set_env(monkeypatch, **env):
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    get_settings.cache_clear()


async def _run(meeting_id):
    from app.ai.topic_intelligence.scheduler import _run_one
    await _run_one(meeting_id)


def _spy_passes(monkeypatch):
    """Replace detection and research passes with counters."""
    calls = {"detect": 0, "research": 0}
    import app.ai.topic_intelligence.scheduler as sched

    def _fake_detect(db, meeting_id, provider):
        calls["detect"] += 1
        return None

    def _fake_research(meeting_id, provider):
        calls["research"] += 1
        return None

    monkeypatch.setattr(sched, "_run_detection_pass", _fake_detect, raising=True)
    monkeypatch.setattr(sched, "_run_research_pass", _fake_research, raising=True)
    return calls


def test_run_one_returns_when_disabled(monkeypatch):
    _set_env(monkeypatch,
             TOPIC_INTELLIGENCE_ENABLED="false",
             TOPIC_INTELLIGENCE_TEST_MODE="false")
    calls = _spy_passes(monkeypatch)
    asyncio.run(_run("m1"))
    assert calls == {"detect": 0, "research": 0}


def test_run_one_returns_when_test_mode_and_empty_id(monkeypatch):
    _set_env(monkeypatch,
             TOPIC_INTELLIGENCE_ENABLED="true",
             TOPIC_INTELLIGENCE_TEST_MODE="true",
             TOPIC_INTELLIGENCE_TEST_MEETING_ID="")
    calls = _spy_passes(monkeypatch)
    asyncio.run(_run("m1"))
    assert calls == {"detect": 0, "research": 0}


def test_run_one_returns_when_test_mode_and_whitespace_id(monkeypatch):
    _set_env(monkeypatch,
             TOPIC_INTELLIGENCE_ENABLED="true",
             TOPIC_INTELLIGENCE_TEST_MODE="true",
             TOPIC_INTELLIGENCE_TEST_MEETING_ID="   ")
    calls = _spy_passes(monkeypatch)
    asyncio.run(_run("m1"))
    assert calls == {"detect": 0, "research": 0}


def test_run_one_returns_when_test_mode_and_wrong_id(monkeypatch):
    _set_env(monkeypatch,
             TOPIC_INTELLIGENCE_ENABLED="true",
             TOPIC_INTELLIGENCE_TEST_MODE="true",
             TOPIC_INTELLIGENCE_TEST_MEETING_ID="other-meeting")
    calls = _spy_passes(monkeypatch)
    asyncio.run(_run("m1"))
    assert calls == {"detect": 0, "research": 0}


def test_run_one_proceeds_when_test_mode_and_matching_id(monkeypatch):
    _set_env(monkeypatch,
             TOPIC_INTELLIGENCE_ENABLED="true",
             TOPIC_INTELLIGENCE_TEST_MODE="true",
             TOPIC_INTELLIGENCE_TEST_MEETING_ID="m1")
    calls = _spy_passes(monkeypatch)
    asyncio.run(_run("m1"))
    assert calls == {"detect": 1, "research": 1}


def test_run_one_proceeds_when_test_mode_off(monkeypatch):
    _set_env(monkeypatch,
             TOPIC_INTELLIGENCE_ENABLED="true",
             TOPIC_INTELLIGENCE_TEST_MODE="false",
             TOPIC_INTELLIGENCE_TEST_MEETING_ID="")
    calls = _spy_passes(monkeypatch)
    asyncio.run(_run("any-meeting-id"))
    assert calls == {"detect": 1, "research": 1}
