from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Cookie, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from app.core.security import COOKIE_NAME, verify_session_token

router = APIRouter(tags=["web"])

TEMPLATES_DIR = Path(__file__).resolve().parents[2] / "web" / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


def _authed(session: str | None) -> bool:
    return verify_session_token(session)


@router.get("/", response_class=HTMLResponse)
def login_page(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(request, "login.html", {})


@router.get("/app", response_class=HTMLResponse)
def meetings_page(
    request: Request,
    session: str | None = Cookie(default=None, alias=COOKIE_NAME),
) -> HTMLResponse:
    if not _authed(session):
        return RedirectResponse(url="/", status_code=303)
    return templates.TemplateResponse(request, "meetings.html", {})


@router.get("/app/meeting/{meeting_id}", response_class=HTMLResponse)
def meeting_page(
    request: Request,
    meeting_id: str,
    session: str | None = Cookie(default=None, alias=COOKIE_NAME),
) -> HTMLResponse:
    if not _authed(session):
        return RedirectResponse(url="/", status_code=303)
    return templates.TemplateResponse(
        request, "meeting.html", {"meeting_id": meeting_id}
    )
