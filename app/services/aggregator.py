"""Deterministic transcript aggregation.

Groups consecutive, unassigned transcript_segments into discussion_segments
targeting ~30 s of transcript, preserving granular evidence.  This module
contains no LLM logic and no interpretation of content.  Text is simply
concatenated with single spaces and timestamps are derived from the
underlying transcript segments.

Two entry points:

  aggregate_mature_window(db, meeting_id, ...)
      Used by the background scheduler.  Only aggregates a window whose
      OLDEST unassigned segment is at least `window_seconds` old, so a
      full window of backlog has accumulated.  Ripeness is measured from
      the oldest segment, not the newest: during a live meeting the
      newest segment is always a few seconds old, so a newest-based
      check would never fire.

  flush_all(db, meeting_id, ...)
      Used by the /stop path.  Aggregates every remaining unassigned
      segment, including a partial final window.  Also bounded by
      `max_segments` per iteration; callers can loop until no
      unassigned segments remain.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.discussion_segment import DiscussionSegment
from app.models.transcript_segment import TranscriptSegment

log = logging.getLogger("meetingsai.aggregator")


def _now_utc_naive() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _fetch_unassigned(db: Session, meeting_id: str, limit: int) -> list[TranscriptSegment]:
    return list(
        db.scalars(
            select(TranscriptSegment)
            .where(TranscriptSegment.meeting_id == meeting_id)
            .where(TranscriptSegment.discussion_segment_id.is_(None))
            .order_by(TranscriptSegment.received_at.asc(), TranscriptSegment.id.asc())
            .limit(limit)
        ).all()
    )


def _select_window(rows: list[TranscriptSegment], window_seconds: int) -> list[TranscriptSegment]:
    """Take a prefix of `rows` whose time span is < window_seconds.

    The span is measured from the first row's received_at to each candidate
    row's received_at.  If the first row has no received_at (should not
    happen, DB default fills it) we fall back to a positional cap of one.
    """
    if not rows:
        return []

    first_ts = rows[0].received_at
    if first_ts is None:
        return [rows[0]]

    window: list[TranscriptSegment] = [rows[0]]
    for r in rows[1:]:
        r_ts = r.received_at or first_ts
        if (r_ts - first_ts).total_seconds() >= window_seconds:
            break
        window.append(r)
    return window


def _persist_window(db: Session, meeting_id: str, window: list[TranscriptSegment]) -> DiscussionSegment:
    parts = [(s.text or "").strip() for s in window if (s.text or "").strip()]
    text = " ".join(parts).strip()
    if not text:
        # All segments empty: do not create a discussion row with no text.
        # Mark them as attached to a sentinel-free bucket by leaving them
        # unattached; caller should decide what to do.  In practice segments
        # with empty text should never have been ingested.
        raise ValueError("window contains no non-empty text")

    started = window[0].started_at or window[0].received_at
    ended = window[-1].ended_at or window[-1].received_at

    # token_estimate is a rough planning metric (len(text)//4).
    # It is NOT authoritative tokenizer output.
    token_estimate = max(1, len(text) // 4)

    ds = DiscussionSegment(
        meeting_id=meeting_id,
        started_at=started,
        ended_at=ended,
        text=text,
        segment_count=len(window),
        token_estimate=token_estimate,
        processing_status="pending",
    )
    db.add(ds)
    db.flush()  # assign ds.id
    for s in window:
        s.discussion_segment_id = ds.id
    db.flush()
    return ds


def aggregate_mature_window(
    db: Session,
    meeting_id: str,
    window_seconds: int,
    max_segments: int,
) -> Optional[DiscussionSegment]:
    """Aggregate one window once its OLDEST segment is at least window_seconds old.

    Returns the created DiscussionSegment, or None if no window was ripe.

    Ripeness is judged from the oldest unassigned segment, not the newest.
    A meeting receiving segments every few seconds would never have an old
    newest segment, so a newest-based check would never fire during a live
    meeting.  Ripeness must therefore be measured from the tail of the
    backlog (the oldest unassigned segment).
    """
    rows = _fetch_unassigned(db, meeting_id, max_segments)
    if not rows:
        return None

    # Ripe check: the OLDEST unassigned segment must be old enough that a
    # full window of backlog has accumulated.
    oldest_ts = rows[0].received_at or _now_utc_naive()
    age = (_now_utc_naive() - oldest_ts).total_seconds()
    if age < window_seconds:
        return None

    window = _select_window(rows, window_seconds)
    if not window:
        return None

    ds = _persist_window(db, meeting_id, window)
    db.commit()
    db.refresh(ds)
    log.info(
        "aggregate_mature_window meeting=%s discussion=%s segments=%d chars=%d",
        meeting_id, ds.id, ds.segment_count, len(ds.text),
    )
    return ds


def flush_all(
    db: Session,
    meeting_id: str,
    window_seconds: int,
    max_segments: int,
) -> list[DiscussionSegment]:
    """Aggregate every remaining unassigned segment, including the last partial window.

    Bounded by max_segments per call; callers may loop until the return
    list is empty (unlikely at typical sizes).
    """
    created: list[DiscussionSegment] = []
    while True:
        rows = _fetch_unassigned(db, meeting_id, max_segments)
        if not rows:
            break
        window = _select_window(rows, window_seconds)
        if not window:
            break
        try:
            ds = _persist_window(db, meeting_id, window)
        except ValueError:
            # No non-empty text; discard from future selection by attaching
            # them to a discussion only if text exists.  In practice this
            # cannot happen because ingest requires min_length=1.
            break
        db.commit()
        db.refresh(ds)
        created.append(ds)
        log.info(
            "flush_all meeting=%s discussion=%s segments=%d chars=%d",
            meeting_id, ds.id, ds.segment_count, len(ds.text),
        )
        if len(window) < len(rows):
            # Remaining rows exist; loop again.
            continue
        break
    return created
