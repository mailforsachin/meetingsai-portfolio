"""Topic Intelligence scheduler.

Runs two passes per tick, sequentially:

  1. Detection pass: for active meetings, find discussion segments that
     have not yet been detection-processed by this process, and run
     detect_topics_for_segment.

  2. Research pass: for each meeting, find MeetingTopic rows eligible for
     research and run research_pending_topic.  Each research attempt uses
     its own SessionLocal so one topic's DB failure cannot roll back
     already-committed sibling results.

Retry policy (Stage A):
    Maximum THREE total research attempts per topic, including the
    initial attempt.  A topic is eligible for retry when:
      - research_status is "pending", or
      - research_status is "failed" AND fewer than 3 TopicResearchRun
        rows exist for it AND the cooldown has elapsed since the most
        recent run.
    A topic with research_status="done" is terminal and never retried.
    Missing-segment failures are terminal and never retried (they are
    identifiable from TopicResearchRun.error).

Cooldown (derived from TopicResearchRun.created_at):
    Attempt 1: immediate.
    Attempt 2: >= 120 s after the most recent run.
    Attempt 3: >= 300 s after the most recent run.

Detection watermark (Stage A):
    In-memory only, per process, per meeting.  Advanced only when
    detection either succeeded or failed via provider/schema error.
    NOT advanced on skipped input (missing meeting/segment) or on DB
    failure.  On process restart, detection re-processes existing
    segments; dedup in the T6 module makes this harmless.  Progress is
    NOT durable; there is no persistent detection cursor.

Single-worker assumption:
    This scheduler assumes one process, one scheduler task.  Multi-worker
    deployments would require DB-level coordination (for example,
    SELECT ... FOR UPDATE SKIP LOCKED on eligible topics).  Not
    implemented in Stage A.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta

from sqlalchemy import func, select

import app.db.session as _session_mod
from app.websocket.manager import manager

from app.ai.provider import AIAnalysisProvider
from app.ai.topic_intelligence.detection import detect_topics_for_segment
from app.ai.topic_intelligence.provider import ModelOnlyProvider
from app.ai.topic_intelligence.research import research_pending_topic
from app.core.config import get_settings
from app.models.discussion_segment import DiscussionSegment
from app.models.meeting import Meeting
from app.models.topic import MeetingTopic, TopicResearchRun

log = logging.getLogger("meetingsai.ai.topic_scheduler")


MAX_SEGMENTS_PER_TICK = 5
MAX_RESEARCH_PER_TICK = 3
MAX_RESEARCH_ATTEMPTS = 3

# Cooldown in seconds BEFORE attempting the Nth run.  Index 0 is unused
# (there is no cooldown before the first attempt).  Attempt 2 requires
# 120 s since the prior run.  Attempt 3 requires 300 s.
_RETRY_COOLDOWN_SECONDS = {
    2: 120,
    3: 300,
}

_MISSING_SEGMENT_DIAGNOSTIC = "first_seen_discussion_id is null or segment not found"


def _try_reserve_provider_call(meeting_id: str, cap: int) -> bool:
    """Fail-closed: True only if THIS attempt reserved a slot.

    Runs in its own session so the caller's transaction is untouched.
    The reservation is committed independently and only then does the
    caller proceed to dispatch.
    """
    from sqlalchemy import text
    db = None
    try:
        db = _session_mod.SessionLocal()
        # Idempotent row initialization.  MariaDB and SQLite use
        # different syntax for the no-op on conflict.
        dialect = db.bind.dialect.name if db.bind is not None else ""
        if dialect == "sqlite":
            init_sql = (
                "INSERT INTO topic_provider_budgets (meeting_id, used_calls) "
                "VALUES (:mid, 0) "
                "ON CONFLICT (meeting_id) DO NOTHING"
            )
        else:
            init_sql = (
                "INSERT INTO topic_provider_budgets (meeting_id, used_calls) "
                "VALUES (:mid, 0) "
                "ON DUPLICATE KEY UPDATE meeting_id = meeting_id"
            )
        db.execute(text(init_sql), {"mid": meeting_id})
        result = db.execute(text(
            "UPDATE topic_provider_budgets "
            "SET used_calls = used_calls + 1 "
            "WHERE meeting_id = :mid AND used_calls < :cap"
        ), {"mid": meeting_id, "cap": cap})
        if result.rowcount != 1:
            db.rollback()
            return False
        db.commit()
        return True
    except Exception:
        try:
            db.rollback()
        except Exception:
            pass
        return False
    finally:
        if db is not None:
            db.close()


async def _safe_broadcast(meeting_id: str, payload: dict) -> None:
    """Await one broadcast, swallowing any exception.

    The scheduled task must not raise -- an unhandled exception in a
    fire-and-forget task would surface as an event-loop error and would
    not be caught by the sync call site.  All exceptions are logged here.
    """
    try:
        await manager.broadcast_json(meeting_id, payload)
    except Exception as e:
        log.warning(
            "topic_intelligence broadcast failed meeting=%s kind=%s err=%s",
            meeting_id, payload.get("kind"), type(e).__name__,
        )


def _broadcast_topic_event(payload: dict) -> "asyncio.Task | None":
    """Schedule a fire-and-forget broadcast of a topic_intelligence event.

    Called after a successful commit.  Wrapped in its own coroutine so
    broadcast failures never propagate to the scheduler.  Uses the
    running event loop, since the scheduler always executes within one.

    Returns the scheduled Task (or None when no loop is running) so
    tests can await it deterministically.
    """
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        log.debug("topic_intelligence broadcast skipped: no running loop")
        return None
    return loop.create_task(_safe_broadcast(payload["meeting_id"], payload))


# In-memory detection watermark: meeting_id -> highest segment id processed.
# See module docstring for its exact semantics and limitations.
_detection_watermark: dict[str, int] = {}


def _reset_watermark_for_tests() -> None:
    """Test-only hook.  Not called by production code."""
    _detection_watermark.clear()


def _now_utc_naive() -> datetime:
    return datetime.utcnow()


def _newest_unprocessed_segment_id(db, meeting_id: str) -> int | None:
    """Highest discussion_segment id for the meeting, or None if none exist."""
    return db.scalar(
        select(func.max(DiscussionSegment.id)).where(
            DiscussionSegment.meeting_id == meeting_id
        )
    )


def _segments_to_process(db, meeting_id: str, limit: int) -> list[int]:
    """Return up to `limit` segment ids newer than the in-memory watermark.

    Oldest first, so a backlog is drained in order.  If the watermark has
    no entry for the meeting, all segments are eligible.
    """
    watermark = _detection_watermark.get(meeting_id, 0)
    rows = db.scalars(
        select(DiscussionSegment.id)
        .where(
            DiscussionSegment.meeting_id == meeting_id,
            DiscussionSegment.id > watermark,
        )
        .order_by(DiscussionSegment.id.asc())
        .limit(limit)
    ).all()
    return list(rows)


def _count_research_runs(db, topic_id: int) -> int:
    return db.scalar(
        select(func.count()).select_from(TopicResearchRun).where(
            TopicResearchRun.topic_id == topic_id
        )
    ) or 0


def _latest_research_run(db, topic_id: int) -> TopicResearchRun | None:
    return db.scalars(
        select(TopicResearchRun)
        .where(TopicResearchRun.topic_id == topic_id)
        .order_by(TopicResearchRun.id.desc())
        .limit(1)
    ).first()


def _is_terminal_failure(run: TopicResearchRun | None) -> bool:
    """Missing-segment failures are terminal.  No other failure is."""
    if run is None or run.error is None:
        return False
    return run.error.startswith(_MISSING_SEGMENT_DIAGNOSTIC)


def _research_eligible(
    db, topic: MeetingTopic, now: datetime
) -> bool:
    """Return True iff this topic is eligible for another research attempt.

    Assumes topic.research_status is "pending" or "failed".
    """
    if topic.research_status == "pending":
        # First attempt.  No prior runs.
        return _count_research_runs(db, topic.id) == 0

    if topic.research_status != "failed":
        return False

    run_count = _count_research_runs(db, topic.id)
    if run_count >= MAX_RESEARCH_ATTEMPTS:
        return False

    latest = _latest_research_run(db, topic.id)
    if _is_terminal_failure(latest):
        return False

    if latest is None or latest.created_at is None:
        # No timestamped run: eligible immediately.
        return True

    next_attempt_number = run_count + 1
    cooldown = _RETRY_COOLDOWN_SECONDS.get(next_attempt_number, 0)
    if cooldown == 0:
        return True
    return now >= latest.created_at + timedelta(seconds=cooldown)


def _eligible_topics(db, meeting_id: str, limit: int) -> list[int]:
    """Return up to `limit` topic ids eligible for research, oldest first."""
    now = _now_utc_naive()
    candidates = db.scalars(
        select(MeetingTopic)
        .where(
            MeetingTopic.meeting_id == meeting_id,
            MeetingTopic.research_status.in_(("pending", "failed")),
        )
        .order_by(MeetingTopic.id.asc())
    ).all()
    eligible: list[int] = []
    for topic in candidates:
        if _research_eligible(db, topic, now):
            eligible.append(topic.id)
            if len(eligible) >= limit:
                break
    return eligible


def _run_detection_pass(db, meeting_id: str, provider: AIAnalysisProvider) -> None:
    settings = get_settings()
    segment_ids = _segments_to_process(db, meeting_id, MAX_SEGMENTS_PER_TICK)
    created_total = 0
    updated_total = 0
    for seg_id in segment_ids:
        if not _try_reserve_provider_call(
            meeting_id,
            settings.topic_intelligence_max_provider_calls_per_meeting,
        ):
            return
        summary = detect_topics_for_segment(
            db, meeting_id, seg_id, provider,
            max_new_topics=settings.topic_intelligence_max_topics_per_meeting,
            max_segment_chars=settings.topic_intelligence_max_segment_chars,
        )
        try:
            db.commit()
        except Exception:
            db.rollback()
            log.warning("detection commit failed meeting=%s segment=%s", meeting_id, seg_id)
            return
        if summary.succeeded or summary.provider_failed or summary.schema_failed:
            _detection_watermark[meeting_id] = seg_id
        # skipped (missing input) OR any unhandled DB error: do not advance.
        created_total += summary.created
        updated_total += summary.updated

    if created_total + updated_total > 0:
        _broadcast_topic_event({
            "type": "topic_intelligence",
            "meeting_id": meeting_id,
            "kind": "detected",
            "topic_id": None,
            "status": None,
        })


def _run_research_pass(meeting_id: str, provider: ModelOnlyProvider) -> None:
    settings = get_settings()
    """Research pass.  Each topic runs in its own session, so one topic's
    failure cannot roll back already-committed siblings.

    Provider and schema errors are caught inside research_pending_topic
    and recorded as failure rows.  DB errors propagate out of that
    function; we roll back that topic's session only.
    """
    probe = _session_mod.SessionLocal()
    try:
        topic_ids = _eligible_topics(probe, meeting_id, MAX_RESEARCH_PER_TICK)
    finally:
        probe.close()

    # Topic cap: filter eligible topics to the first N by insertion order.
    probe2 = _session_mod.SessionLocal()
    try:
        from sqlalchemy import select as _select
        allowed_ids = probe2.scalars(
            _select(MeetingTopic.id)
            .where(MeetingTopic.meeting_id == meeting_id)
            .order_by(MeetingTopic.id.asc())
        ).all()
        allowed = set(allowed_ids[:settings.topic_intelligence_max_topics_per_meeting])
    finally:
        probe2.close()
    topic_ids = [t for t in topic_ids if t in allowed]

    for topic_id in topic_ids:
        if not _try_reserve_provider_call(
            meeting_id,
            settings.topic_intelligence_max_provider_calls_per_meeting,
        ):
            continue
        db = _session_mod.SessionLocal()
        status_after = None
        try:
            research_pending_topic(db, meeting_id, topic_id, provider)
            db.commit()
            topic_row = db.get(MeetingTopic, topic_id)
            status_after = topic_row.research_status if topic_row else None
        except Exception as e:
            db.rollback()
            log.warning(
                "research db error meeting=%s topic=%s err=%s",
                meeting_id, topic_id, type(e).__name__,
            )
        finally:
            db.close()

        if status_after is not None:
            _broadcast_topic_event({
                "type": "topic_intelligence",
                "meeting_id": meeting_id,
                "kind": "researched",
                "topic_id": topic_id,
                "status": status_after,
            })


async def _run_one(meeting_id: str) -> None:
    settings = get_settings()

    if not settings.topic_intelligence_enabled:
        return
    if settings.topic_intelligence_test_mode:
        allowlist = (settings.topic_intelligence_test_meeting_id or "").strip()
        if not allowlist or meeting_id != allowlist:
            return

    detector = AIAnalysisProvider(
        api_key=settings.openai_api_key,
        model=settings.analysis_model,
        max_completion_tokens=settings.topic_intelligence_max_completion_tokens,
    )
    researcher = ModelOnlyProvider(
        api_key=settings.openai_api_key,
        model=settings.topic_intelligence_model,
        max_completion_tokens=settings.topic_intelligence_max_completion_tokens,
    )

    db = _session_mod.SessionLocal()
    try:
        _run_detection_pass(db, meeting_id, detector)
    finally:
        db.close()

    _run_research_pass(meeting_id, researcher)


async def run_topic_intelligence_scheduler(stop_event: asyncio.Event) -> None:
    settings = get_settings()
    interval = max(15, int(settings.topic_intelligence_interval_seconds))
    log.info(
        "topic intelligence scheduler starting; interval=%ss model=%s enabled=%s",
        interval, settings.topic_intelligence_model, settings.topic_intelligence_enabled,
    )
    try:
        while not stop_event.is_set():
            if not settings.topic_intelligence_enabled:
                try:
                    await asyncio.wait_for(stop_event.wait(), timeout=60)
                except asyncio.TimeoutError:
                    pass
                continue

            db = _session_mod.SessionLocal()
            try:
                meetings = list(db.scalars(
                    select(Meeting).where(
                        Meeting.status.in_(("active", "stopping")),
                        Meeting.topic_intelligence_eligible.is_(True),
                    )
                ).all())
            finally:
                db.close()

            for m in meetings:
                try:
                    await _run_one(m.id)
                except Exception as e:
                    log.warning(
                        "topic intelligence loop error meeting=%s err=%s",
                        m.id, type(e).__name__,
                    )

            try:
                await asyncio.wait_for(stop_event.wait(), timeout=interval)
            except asyncio.TimeoutError:
                pass
    finally:
        log.info("topic intelligence scheduler stopped")
