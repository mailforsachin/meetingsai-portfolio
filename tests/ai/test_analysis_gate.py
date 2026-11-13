from __future__ import annotations

from datetime import datetime, timedelta, timezone, timezone, timezone

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db.base import Base
import app.models  # noqa: F401
from app.ai.analysis_scheduler import _should_run
from app.models.discussion_segment import DiscussionSegment
from app.models.meeting import Meeting
from app.models.state import AIAnalysisRun


def _db():
    eng = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
        future=True,
    )
    Base.metadata.create_all(eng)
    return sessionmaker(bind=eng, autoflush=False, autocommit=False, future=True)()


def test_no_discussion_blocks():
    db = _db(); m = Meeting(title="t", status="active"); db.add(m); db.commit()
    ok, reason = _should_run(db, m.id, 60)
    assert not ok and "no discussion" in reason


def test_first_run_allowed_when_discussion_exists():
    db = _db(); m = Meeting(title="t", status="active"); db.add(m); db.commit()
    db.add(DiscussionSegment(meeting_id=m.id, text="hello", segment_count=1))
    db.commit()
    ok, _ = _should_run(db, m.id, 60)
    assert ok


def test_cooldown_blocks_even_with_new_discussion():
    db = _db(); m = Meeting(title="t", status="active"); db.add(m); db.commit()
    db.add(DiscussionSegment(meeting_id=m.id, text="one", segment_count=1)); db.commit()
    d1 = db.query(DiscussionSegment).one()
    db.add(AIAnalysisRun(
        meeting_id=m.id, kind="rolling", prompt_version="x", model="m",
        status="ok", last_discussion_segment_id=d1.id,
        finished_at=datetime.now(timezone.utc).replace(tzinfo=None),
    ))
    db.commit()
    db.add(DiscussionSegment(meeting_id=m.id, text="two", segment_count=1)); db.commit()
    ok, reason = _should_run(db, m.id, 60)
    assert not ok and "cooldown" in reason


def test_after_cooldown_allowed():
    db = _db(); m = Meeting(title="t", status="active"); db.add(m); db.commit()
    db.add(DiscussionSegment(meeting_id=m.id, text="one", segment_count=1)); db.commit()
    d1 = db.query(DiscussionSegment).one()
    db.add(AIAnalysisRun(
        meeting_id=m.id, kind="rolling", prompt_version="x", model="m",
        status="ok", last_discussion_segment_id=d1.id,
        finished_at=datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(seconds=120),
    ))
    db.commit()
    db.add(DiscussionSegment(meeting_id=m.id, text="two", segment_count=1)); db.commit()
    ok, _ = _should_run(db, m.id, 60)
    assert ok
