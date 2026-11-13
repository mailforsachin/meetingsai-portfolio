from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app.ai.analysis_scheduler import run_analysis_scheduler
from app.ai.context_review_scheduler import run_context_review_scheduler
from app.ai.topic_intelligence.scheduler import run_topic_intelligence_scheduler
from app.api.analysis import router as analysis_router
from app.api.auth import router as auth_router
from app.api.finalization import router as finalization_router
from app.api.health import router as health_router
from app.api.meetings import router as meetings_router
from app.api.topics import router as topics_router
from app.api.protected_ping import router as ping_router
from app.api.web import router as web_router
from app.core.config import get_settings
from app.core.logging import configure_logging
from app.services.scheduler import run_scheduler
from app.websocket.routes import router as ws_router

configure_logging()
settings = get_settings()

_docs_kwargs = (
    {"docs_url": None, "redoc_url": None, "openapi_url": None}
    if settings.app_env == "production"
    else {}
)

log = logging.getLogger("meetingsai.app")


@asynccontextmanager
async def lifespan(app: FastAPI):
    stop_event = asyncio.Event()
    if stop_event.is_set():
        # Shutdown was requested before startup completed.  Do not start
        # any scheduler tasks; fall straight through to yield/cleanup.
        yield
        return
    agg_task = asyncio.create_task(run_scheduler(stop_event), name="aggregation-scheduler")
    ai_task = asyncio.create_task(run_analysis_scheduler(stop_event), name="analysis-scheduler")
    ctx_task = asyncio.create_task(run_context_review_scheduler(stop_event), name="context-review-scheduler")
    topic_task = asyncio.create_task(run_topic_intelligence_scheduler(stop_event), name="topic-intelligence-scheduler")
    try:
        yield
    finally:
        stop_event.set()
        for t in (agg_task, ai_task, ctx_task, topic_task):
            try:
                await asyncio.wait_for(t, timeout=5)
            except asyncio.TimeoutError:
                t.cancel()
            except Exception as e:
                log.warning("scheduler shutdown error: %s", e)


app = FastAPI(title="meetingsAI", version="0.9.0", lifespan=lifespan, **_docs_kwargs)

_static_dir = Path(__file__).resolve().parents[1] / "web" / "static"
app.mount("/static", StaticFiles(directory=str(_static_dir)), name="static")

app.include_router(health_router)
app.include_router(auth_router)
app.include_router(meetings_router)
app.include_router(topics_router)
app.include_router(analysis_router)
app.include_router(finalization_router)
app.include_router(ping_router)
app.include_router(web_router)
app.include_router(ws_router)
