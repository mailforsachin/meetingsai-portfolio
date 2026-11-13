"""Per-meeting analysis scheduler.

Waits on two signals:
  - a new discussion broadcast (in-process asyncio event keyed by meeting_id)
  - a periodic tick (10 s default)

For each active meeting, checks:
  - a new discussion segment exists since the last successful run?
  - at least `analysis_min_interval_seconds` since the last successful run
    (or no successful run yet)?

If both true, runs one analysis: provider → validate → reducer →
snapshot → audit row → broadcast `state_update`.

If a new discussion arrives during cooldown, the scheduler does not
skip it; it waits for the cooldown to elapse and processes the batch.
"""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select

from app.ai.output_schema import AISchemaError
from app.ai.prompt_loader import load_prompt
from app.ai.provider import AIAnalysisProvider, AIProviderError
from app.ai.reducer import apply_rolling_output
from app.ai.delta import compute_delta, snapshot_state_for_delta
from app.ai.state_builder import build_llm_payload, build_state_out
from app.core.config import get_settings
from app.db.session import SessionLocal
from app.models.discussion_segment import DiscussionSegment
from app.models.meeting import Meeting
from app.models.meeting_delta import MeetingDelta
from app.models.state import AIAnalysisRun, MeetingStateSnapshot
from app.schemas.state import MeetingStateOut
from app.websocket.manager import manager

log = logging.getLogger("meetingsai.ai.scheduler")

# In-process trigger map: meeting_id -> asyncio.Event
_triggers: dict[str, asyncio.Event] = {}
_trigger_lock = asyncio.Lock()


def trigger_analysis(meeting_id: str) -> None:
    """Wake the analysis scheduler for a meeting.

    Safe to call from any coroutine.  If no event exists yet, creates one.
    """
    ev = _triggers.get(meeting_id)
    if ev is None:
        ev = asyncio.Event()
        _triggers[meeting_id] = ev
    ev.set()


async def _wait_for_trigger_or_timeout(meeting_id: str, timeout: float) -> None:
    ev = _triggers.get(meeting_id)
    if ev is None:
        ev = asyncio.Event()
        _triggers[meeting_id] = ev
    try:
        await asyncio.wait_for(ev.wait(), timeout=timeout)
    except asyncio.TimeoutError:
        pass
    finally:
        ev.clear()


def _now_utc_naive() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _last_successful_run(db, meeting_id: str) -> AIAnalysisRun | None:
    return db.scalars(
        select(AIAnalysisRun)
        .where(AIAnalysisRun.meeting_id == meeting_id)
        .where(AIAnalysisRun.status == "ok")
        .order_by(AIAnalysisRun.started_at.desc())
        .limit(1)
    ).first()


def _newest_discussion_id(db, meeting_id: str) -> int | None:
    return db.scalar(
        select(func.max(DiscussionSegment.id)).where(DiscussionSegment.meeting_id == meeting_id)
    )


def _should_run(db, meeting_id: str, min_interval_s: int) -> tuple[bool, str]:
    last = _last_successful_run(db, meeting_id)
    newest = _newest_discussion_id(db, meeting_id)
    if newest is None:
        return False, "no discussion segments yet"
    if last is None:
        return True, "first run"
    last_gate = last.last_discussion_segment_id or 0
    if newest <= last_gate:
        return False, "no new discussion since last run"
    since = _now_utc_naive() - (last.finished_at or last.started_at)
    if since.total_seconds() < min_interval_s:
        return False, f"cooldown ({int(since.total_seconds())}s < {min_interval_s}s)"
    return True, "ok"


def _run_one(db, meeting_id: str) -> None:
    settings = get_settings()
    meeting = db.get(Meeting, meeting_id)
    if meeting is None:
        return
    if meeting.status not in ("active", "stopping"):
        return

    should, reason = _should_run(db, meeting_id, settings.analysis_min_interval_seconds)
    if not should:
        log.debug("skip analysis meeting=%s reason=%s", meeting_id, reason)
        return

    payload, newest_discussion_id = build_llm_payload(
        db, meeting, settings.analysis_recent_discussion_segments
    )
    if newest_discussion_id is None:
        return

    prompt = load_prompt("meeting_analysis.md")

    # Record the run as 'running' first, so failures are auditable.
    run = AIAnalysisRun(
        meeting_id=meeting_id,
        kind="rolling",
        prompt_version=prompt.version,
        model=settings.analysis_model,
        status="running",
        last_discussion_segment_id=newest_discussion_id,
    )
    db.add(run)
    db.commit()
    db.refresh(run)

    provider = AIAnalysisProvider(
        api_key=settings.openai_api_key,
        model=settings.analysis_model,
    )
    try:
        result = provider.run(prompt, payload)
    except (AIProviderError, AISchemaError) as e:
        run.status = "error"
        run.error = str(e)[:1000]
        run.finished_at = _now_utc_naive()
        db.commit()
        log.warning("analysis error meeting=%s err=%s", meeting_id, e)
        return

    run.output_json = result.output_json
    run.input_tokens = result.input_tokens
    run.output_tokens = result.output_tokens
    run.latency_ms = result.latency_ms
    run.model = result.model
    run.prompt_version = result.prompt_version

    try:
        pre = snapshot_state_for_delta(db, meeting_id)
        summary = apply_rolling_output(db, meeting_id, result.output)
        post = snapshot_state_for_delta(db, meeting_id)
        delta = compute_delta(pre, post, summary)

        # Snapshot = post-reducer state, but the conversational fields
        # (understanding, key_discussion, potential_response, lists) are
        # taken from THIS run's validated output.  Entity rows come from
        # the reducer, which has just run.
        state_out = build_state_out(db, meeting_id)
        merged = state_out.model_dump()
        merged["understanding"] = result.output.understanding or None
        merged["key_discussion"] = list(result.output.key_discussion or [])
        merged["potential_response"] = result.output.potential_response or None
        merged["missing_information"] = list(result.output.missing_information or [])
        merged["contradictions"] = list(result.output.contradictions or [])
        merged["next_steps"] = list(result.output.next_steps or [])
        snapshot = MeetingStateSnapshot(
            meeting_id=meeting_id,
            run_id=run.id,
            state_json=json.dumps(merged, default=str),
        )
        db.add(snapshot)

        # Persist the deterministic delta.
        import json as _json
        db.add(MeetingDelta(
            meeting_id=meeting_id,
            run_id=run.id,
            delta_json=_json.dumps(delta, default=str),
        ))
        run.status = "ok"
        run.finished_at = _now_utc_naive()
        db.commit()
    except Exception as e:
        db.rollback()
        run = db.get(AIAnalysisRun, run.id)
        if run is not None:
            run.status = "error"
            run.error = f"reducer failed: {e}"[:1000]
            run.finished_at = _now_utc_naive()
            db.commit()
        log.warning("reducer error meeting=%s err=%s", meeting_id, e)
        return

    # Broadcast state_update (fire-and-forget into the WS manager).
    try:
        loop = asyncio.get_event_loop()
        loop.create_task(manager.broadcast_json(
            meeting_id,
            {"type": "state_update", "meeting_id": meeting_id, "run_id": run.id},
        ))
    except RuntimeError:
        pass


async def run_analysis_scheduler(stop_event: asyncio.Event) -> None:
    settings = get_settings()
    tick = 10  # seconds
    log.info("analysis scheduler starting; min_interval=%ss model=%s",
             settings.analysis_min_interval_seconds, settings.analysis_model)
    try:
        while not stop_event.is_set():
            db = SessionLocal()
            try:
                meetings = list(
                    db.scalars(
                        select(Meeting).where(Meeting.status.in_(("active", "stopping")))
                    ).all()
                )
            finally:
                db.close()

            for m in meetings:
                db = SessionLocal()
                try:
                    _run_one(db, m.id)
                except Exception as e:
                    log.warning("analysis loop error meeting=%s err=%s", m.id, e)
                finally:
                    db.close()

            # Wait for either a trigger or the tick interval.
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=tick)
            except asyncio.TimeoutError:
                pass
    finally:
        log.info("analysis scheduler stopped")


def notify_new_discussion(meeting_id: str) -> None:
    """Called by the aggregator to wake analysis for a meeting.

    Wraps the trigger in a safe fallback for callers that are not on
    an asyncio loop.
    """
    try:
        loop = asyncio.get_running_loop()
        loop.create_task(_trigger_async(meeting_id))
    except RuntimeError:
        pass


async def _trigger_async(meeting_id: str) -> None:
    trigger_analysis(meeting_id)
