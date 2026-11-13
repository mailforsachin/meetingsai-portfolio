"""Regression test for the aggregator ripeness check.

Phase G7: the scheduler's ripeness check was measuring from the newest
unassigned segment, which never ripens during a live meeting (the newest
segment is always a few seconds old).  The check must measure from the
OLDEST unassigned segment, so that a full window of backlog accumulates
before it is aggregated.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db.base import Base
import app.models  # noqa: F401
from app.models.discussion_segment import DiscussionSegment
from app.models.meeting import Meeting
from app.models.transcript_segment import TranscriptSegment
from app.services.aggregator import aggregate_mature_window


def _db():
    eng = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
        future=True,
    )
    Base.metadata.create_all(eng)
    return sessionmaker(bind=eng, autoflush=False, autocommit=False, future=True)()


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _meeting(db) -> Meeting:
    m = Meeting(title="ripeness-test", status="active")
    db.add(m)
    db.commit()
    db.refresh(m)
    return m


def _seg(db, meeting_id: str, seg_id: str, text: str, age_seconds: float) -> TranscriptSegment:
    ts = _now() - timedelta(seconds=age_seconds)
    s = TranscriptSegment(
        meeting_id=meeting_id,
        segment_id=seg_id,
        text=text,
        started_at=ts,
        ended_at=ts + timedelta(seconds=5),
        received_at=ts,
    )
    db.add(s)
    return s


def test_ripe_when_oldest_segment_is_old_enough_even_if_newest_is_fresh():
    """Live-meeting shape: newest segment is seconds old, oldest is a minute old.

    Old (buggy) logic measured from the newest, so this returned None and
    no discussion was ever produced during a live meeting.  New logic
    measures from the oldest and aggregates the backlog.
    """
    db = _db()
    m = _meeting(db)

    a = _seg(db, m.id, "seg-a", "alpha content", age_seconds=60)
    b = _seg(db, m.id, "seg-b", "bravo content", age_seconds=40)
    c = _seg(db, m.id, "seg-c", "charlie content", age_seconds=5)
    db.commit()
    for s in (a, b, c):
        db.refresh(s)

    ds = aggregate_mature_window(db, m.id, window_seconds=30, max_segments=100)

    assert ds is not None, (
        "aggregate_mature_window returned None for a meeting with a 60s-old "
        "oldest segment; the ripeness check is measuring from the newest "
        "segment again"
    )
    assert isinstance(ds, DiscussionSegment)
    assert ds.segment_count == 2, "window should include exactly the two oldest segments"
    assert "alpha content" in ds.text
    assert "bravo content" in ds.text
    assert "charlie content" not in ds.text, "the freshest segment must not be aggregated yet"

    db.refresh(a); db.refresh(b); db.refresh(c)
    assert a.discussion_segment_id == ds.id
    assert b.discussion_segment_id == ds.id
    assert c.discussion_segment_id is None, (
        "the segment inside the active window must stay unassigned for the next tick"
    )


def test_not_ripe_when_all_segments_are_fresh():
    """All segments younger than window_seconds: nothing should be aggregated.

    This guards the opposite regression: someone removing the age gate
    entirely, which would aggregate mid-sentence fragments.
    """
    db = _db()
    m = _meeting(db)

    _seg(db, m.id, "seg-x", "x", age_seconds=5)
    _seg(db, m.id, "seg-y", "y", age_seconds=3)
    db.commit()

    ds = aggregate_mature_window(db, m.id, window_seconds=30, max_segments=100)
    assert ds is None
    remaining = db.query(TranscriptSegment).filter(
        TranscriptSegment.discussion_segment_id.is_(None)
    ).count()
    assert remaining == 2


def test_no_unassigned_segments_returns_none():
    """Empty backlog is not an error; returns None."""
    db = _db()
    m = _meeting(db)
    assert aggregate_mature_window(db, m.id, window_seconds=30, max_segments=100) is None
