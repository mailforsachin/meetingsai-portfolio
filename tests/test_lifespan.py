"""Dedicated lifespan wiring tests for the topic intelligence scheduler.

These tests drive app.main.lifespan directly with fake scheduler
coroutines that capture the shared stop event.  They confirm that all
four schedulers are scheduled and that the topic scheduler receives the
stop event, which is set on lifespan exit.

We do NOT reload app.main (avoids re-running module-level init).  We do
NOT expose internal task state from production code.  We do NOT rely on
TestClient startup as a proxy for this behavior.
"""
from __future__ import annotations

import asyncio

import app.main as main_mod


def _install_fake_schedulers(monkeypatch, captured):
    """Replace the four scheduler coroutines with fakes that wait on the
    supplied stop_event.  Records (name, stop_event) in `captured`."""

    async def _fake_agg(stop_event):
        captured.append(("aggregation-scheduler", stop_event))
        await stop_event.wait()

    async def _fake_ai(stop_event):
        captured.append(("analysis-scheduler", stop_event))
        await stop_event.wait()

    async def _fake_ctx(stop_event):
        captured.append(("context-review-scheduler", stop_event))
        await stop_event.wait()

    async def _fake_topic(stop_event):
        captured.append(("topic-intelligence-scheduler", stop_event))
        await stop_event.wait()

    # Patch by name at the module attribute the lifespan uses.
    monkeypatch.setattr(main_mod, "run_scheduler", _fake_agg, raising=True)
    monkeypatch.setattr(main_mod, "run_analysis_scheduler", _fake_ai, raising=True)
    monkeypatch.setattr(main_mod, "run_context_review_scheduler", _fake_ctx, raising=True)
    monkeypatch.setattr(main_mod, "run_topic_intelligence_scheduler", _fake_topic, raising=True)


def test_lifespan_schedules_all_four_schedulers(monkeypatch):
    captured: list[tuple[str, asyncio.Event]] = []
    _install_fake_schedulers(monkeypatch, captured)

    async def _go():
        async with main_mod.lifespan(main_mod.app):
            # All four fake schedulers should have been awaited into existence.
            # Yield once so the create_task'd coroutines get a chance to run.
            await asyncio.sleep(0)

    asyncio.run(_go())

    names = [name for name, _ in captured]
    assert "aggregation-scheduler" in names
    assert "analysis-scheduler" in names
    assert "context-review-scheduler" in names
    assert "topic-intelligence-scheduler" in names


def test_topic_scheduler_receives_stop_event_set_on_shutdown(monkeypatch):
    captured: list[tuple[str, asyncio.Event]] = []
    _install_fake_schedulers(monkeypatch, captured)

    async def _go():
        async with main_mod.lifespan(main_mod.app):
            await asyncio.sleep(0)
            # Inside the lifespan body: the stop event is not yet set.
            events = {name: ev for name, ev in captured}
            topic_ev = events.get("topic-intelligence-scheduler")
            assert topic_ev is not None
            assert topic_ev.is_set() is False
        # After lifespan exit: every captured event must be set.
        for name, ev in captured:
            assert ev.is_set() is True, f"{name} stop event not set on exit"

    asyncio.run(_go())
