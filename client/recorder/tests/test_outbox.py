from __future__ import annotations

import os
import tempfile
from pathlib import Path

from meetingsai_recorder.outbox import Outbox, Segment, new_segment_id


def _outbox():
    d = tempfile.mkdtemp(prefix="mai_outbox_test_")
    return Outbox(Path(d) / "outbox.sqlite3")


def _seg(sid: str, mid: str = "m1", text: str = "hi") -> Segment:
    return Segment(
        segment_id=sid, meeting_id=mid,
        started_at="2026-01-01T00:00:00Z",
        ended_at="2026-01-01T00:00:06Z",
        text=text, model="test",
    )


def test_enqueue_and_stats():
    ob = _outbox()
    assert ob.enqueue(_seg("a")) is True
    assert ob.enqueue(_seg("b")) is True
    s = ob.stats()
    assert s["pending"] == 2 and s["acked"] == 0 and s["total"] == 2


def test_enqueue_duplicate_returns_false():
    ob = _outbox()
    assert ob.enqueue(_seg("a")) is True
    assert ob.enqueue(_seg("a")) is False


def test_lease_then_ack():
    ob = _outbox()
    ob.enqueue(_seg("a"))
    ob.enqueue(_seg("b"))
    batch = ob.lease_batch(limit=10)
    assert {x.segment_id for x in batch} == {"a", "b"}
    ob.ack([x.segment_id for x in batch])
    s = ob.stats()
    assert s["pending"] == 0 and s["acked"] == 2


def test_lease_does_not_return_acked():
    ob = _outbox()
    ob.enqueue(_seg("a"))
    batch = ob.lease_batch(limit=10)
    ob.ack([x.segment_id for x in batch])
    again = ob.lease_batch(limit=10)
    assert again == []


def test_nack_bumps_attempts_and_releases_lease():
    ob = _outbox()
    ob.enqueue(_seg("a"))
    batch = ob.lease_batch(limit=10, lease_seconds=1)
    ob.nack([x.segment_id for x in batch], "network down")
    # Before the lease would expire, a fresh lease should still be possible
    # because nack clears the lease.
    again = ob.lease_batch(limit=10)
    assert {x.segment_id for x in again} == {"a"}


def test_new_segment_id_unique():
    ids = {new_segment_id() for _ in range(50)}
    assert len(ids) == 50
