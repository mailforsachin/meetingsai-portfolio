"""Stage C: unit tests for the durable provider-call budget.

Covers _try_reserve_provider_call on SQLite:
  - returns True when the counter is under cap
  - returns False when the counter is at cap
  - returns False on commit failure
  - returns False on any exception
  - two successive reservations increment the counter to 2
  - a fresh session sees the persisted value (restart simulation)
  - the detection pass does not dispatch when the reservation returns False
  - the research pass does not dispatch when the reservation returns False
  - the budget is not reset by a second call

These are in-process unit tests.  The MariaDB-only concurrency proof is
in tests/integration/test_topic_provider_budget_mariadb.py (Docker).
"""
from __future__ import annotations

from unittest.mock import patch

import pytest
from sqlalchemy import text

import app.db.session as _session_mod
from app.ai.topic_intelligence import scheduler as sched
from app.models.meeting import Meeting
from app.models.topic import MeetingTopic


def _mk_meeting(db, meeting_id="m1"):
    m = Meeting(id=meeting_id, title="T", objective="o", organization="g")
    db.add(m); db.commit(); db.refresh(m)
    return m


def _budget_row(db, meeting_id):
    return db.execute(text(
        "SELECT used_calls FROM topic_provider_budgets WHERE meeting_id = :m"
    ), {"m": meeting_id}).fetchone()


# ---- helper: return values ----


def test_reservation_returns_true_under_cap(db_session):
    _mk_meeting(db_session)
    assert sched._try_reserve_provider_call("m1", 5) is True
    row = _budget_row(db_session, "m1")
    assert row is not None and row[0] == 1


def test_reservation_returns_false_at_cap(db_session):
    _mk_meeting(db_session)
    assert sched._try_reserve_provider_call("m1", 1) is True
    assert sched._try_reserve_provider_call("m1", 1) is False
    row = _budget_row(db_session, "m1")
    assert row is not None and row[0] == 1


def test_two_reservations_increment_to_two(db_session):
    _mk_meeting(db_session)
    assert sched._try_reserve_provider_call("m1", 10) is True
    assert sched._try_reserve_provider_call("m1", 10) is True
    row = _budget_row(db_session, "m1")
    assert row is not None and row[0] == 2


def test_budget_not_reset_by_second_call(db_session):
    """Repeated calls read the persisted value; no reset occurs."""
    _mk_meeting(db_session)
    sched._try_reserve_provider_call("m1", 3)
    sched._try_reserve_provider_call("m1", 3)
    sched._try_reserve_provider_call("m1", 3)
    assert sched._try_reserve_provider_call("m1", 3) is False
    row = _budget_row(db_session, "m1")
    assert row is not None and row[0] == 3


def test_fresh_session_sees_persisted_value(db_session):
    """Simulates restart: a new session reads the committed counter."""
    _mk_meeting(db_session)
    assert sched._try_reserve_provider_call("m1", 5) is True
    # Simulate a new "process" by opening a separate session from the
    # same bound SessionLocal and reading the persisted value.
    db2 = _session_mod.SessionLocal()
    try:
        row = db2.execute(text(
            "SELECT used_calls FROM topic_provider_budgets WHERE meeting_id = 'm1'"
        )).fetchone()
    finally:
        db2.close()
    assert row is not None and row[0] == 1


def test_reservation_returns_false_on_exception(db_session):
    """Any exception inside the helper must return False, never True."""
    _mk_meeting(db_session)
    with patch.object(_session_mod, "SessionLocal", side_effect=RuntimeError("boom")):
        assert sched._try_reserve_provider_call("m1", 5) is False


def test_reservation_returns_false_on_commit_failure(db_session):
    """If commit raises, the reservation must not be treated as held."""
    _mk_meeting(db_session)
    RealSessionLocal = _session_mod.SessionLocal

    class _BadSession:
        def __init__(self, inner):
            self._inner = inner
        def __getattr__(self, name):
            return getattr(self._inner, name)
        def execute(self, *a, **k):
            return self._inner.execute(*a, **k)
        def rollback(self):
            return self._inner.rollback()
        def close(self):
            return self._inner.close()
        def commit(self):
            raise RuntimeError("commit failed")

    def _factory():
        return _BadSession(RealSessionLocal())

    with patch.object(_session_mod, "SessionLocal", _factory):
        assert sched._try_reserve_provider_call("m1", 5) is False


# ---- pass wiring: no dispatch when reservation fails ----


def test_detection_pass_returns_without_dispatch_when_reservation_fails(
    db_session, monkeypatch
):
    _mk_meeting(db_session)
    # One segment so the pass has work to do.
    from app.models.discussion_segment import DiscussionSegment
    s = DiscussionSegment(meeting_id="m1", text="seg")
    db_session.add(s); db_session.commit()

    monkeypatch.setattr(
        sched, "_try_reserve_provider_call", lambda *a, **k: False, raising=True,
    )
    detect_calls = {"n": 0}
    def _fake_detect(*a, **k):
        detect_calls["n"] += 1
        return None
    monkeypatch.setattr(
        "app.ai.topic_intelligence.scheduler.detect_topics_for_segment",
        _fake_detect, raising=True,
    )

    sched._run_detection_pass(db_session, "m1", object())
    assert detect_calls["n"] == 0


def test_research_pass_skips_topic_when_reservation_fails(db_session, monkeypatch):
    _mk_meeting(db_session)
    db_session.add(MeetingTopic(
        meeting_id="m1", name="k", topic_type="concept",
        relevance="medium", research_status="pending",
    ))
    db_session.commit()

    monkeypatch.setattr(
        sched, "_try_reserve_provider_call", lambda *a, **k: False, raising=True,
    )
    research_calls = {"n": 0}
    def _fake_research(*a, **k):
        research_calls["n"] += 1
        return None
    monkeypatch.setattr(
        "app.ai.topic_intelligence.scheduler.research_pending_topic",
        _fake_research, raising=True,
    )

    sched._run_research_pass("m1", object())
    assert research_calls["n"] == 0
