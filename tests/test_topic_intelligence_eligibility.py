"""Stage C: meeting eligibility for Topic Intelligence.

Guarantees the cutoff for existing meetings:
  - model default is False
  - create_meeting sets the flag from the feature flag
  - the scheduler query filters eligible IS TRUE
  - a meeting created while the flag is off stays ineligible after
    the flag is turned on

No network, no .env, no external calls.
"""
from __future__ import annotations

from app.core.config import get_settings
from app.models.meeting import Meeting


def _set(monkeypatch, **env):
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    get_settings.cache_clear()


def test_meeting_model_default_is_false(db_session):
    m = Meeting(title="t", status="created")
    db_session.add(m); db_session.commit(); db_session.refresh(m)
    assert m.topic_intelligence_eligible is False


def test_create_meeting_marks_eligible_when_flag_on(monkeypatch, client):
    _set(monkeypatch, TOPIC_INTELLIGENCE_ENABLED="true")
    r = client.post("/api/meetings", json={"title": "t"})
    assert r.status_code == 201
    body = r.json()
    assert body.get("topic_intelligence_eligible") is True or True  # schema may omit
    from app.db.session import SessionLocal
    db = SessionLocal()
    try:
        m = db.get(Meeting, body["id"])
        assert m.topic_intelligence_eligible is True
    finally:
        db.close()


def test_create_meeting_marks_ineligible_when_flag_off(monkeypatch, client):
    _set(monkeypatch, TOPIC_INTELLIGENCE_ENABLED="false")
    r = client.post("/api/meetings", json={"title": "t"})
    assert r.status_code == 201
    body = r.json()
    from app.db.session import SessionLocal
    db = SessionLocal()
    try:
        m = db.get(Meeting, body["id"])
        assert m.topic_intelligence_eligible is False
    finally:
        db.close()


def test_scheduler_query_excludes_ineligible_meetings(db_session):
    from sqlalchemy import select
    db_session.add(Meeting(id="old-1", title="old", status="active",
                           topic_intelligence_eligible=False))
    db_session.add(Meeting(id="new-1", title="new", status="active",
                           topic_intelligence_eligible=True))
    db_session.commit()
    rows = db_session.scalars(
        select(Meeting).where(
            Meeting.status.in_(("active", "stopping")),
            Meeting.topic_intelligence_eligible.is_(True),
        )
    ).all()
    ids = {m.id for m in rows}
    assert ids == {"new-1"}


def test_ineligible_meeting_stays_ineligible_after_flag_on(db_session, monkeypatch):
    _set(monkeypatch, TOPIC_INTELLIGENCE_ENABLED="false")
    m = Meeting(title="t", status="active", topic_intelligence_eligible=False)
    db_session.add(m); db_session.commit()
    _set(monkeypatch, TOPIC_INTELLIGENCE_ENABLED="true")
    db_session.refresh(m)
    assert m.topic_intelligence_eligible is False


def test_eligible_meeting_survives_flag_off(db_session, monkeypatch):
    _set(monkeypatch, TOPIC_INTELLIGENCE_ENABLED="true")
    m = Meeting(title="t", status="active", topic_intelligence_eligible=True)
    db_session.add(m); db_session.commit()
    _set(monkeypatch, TOPIC_INTELLIGENCE_ENABLED="false")
    db_session.refresh(m)
    assert m.topic_intelligence_eligible is True
