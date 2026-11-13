from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status

from app.core.config import get_settings
from app.core.deps import require_user
from app.core.rate_limit import login_limiter
from app.core.security import (
    COOKIE_NAME,
    SESSION_MAX_AGE,
    issue_session_token,
    verify_password,
)
from app.schemas.auth import LoginRequest, LoginResponse, MeResponse

router = APIRouter(prefix="/api", tags=["auth"])


def _client_key(request: Request) -> str:
    # Prefer X-Forwarded-For when behind nginx, fall back to peer.
    xff = request.headers.get("x-forwarded-for")
    if xff:
        return xff.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


@router.post("/auth/login", response_model=LoginResponse)
def login(payload: LoginRequest, request: Request, response: Response) -> LoginResponse:
    key = _client_key(request)
    if login_limiter.is_blocked(key):
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="too many attempts, try again later",
        )

    s = get_settings()
    if not s.auth_password_hash or not verify_password(payload.password, s.auth_password_hash):
        login_limiter.register_failure(key)
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid credentials")

    login_limiter.reset(key)
    token = issue_session_token()
    response.set_cookie(
        key=COOKIE_NAME,
        value=token,
        max_age=SESSION_MAX_AGE,
        httponly=True,
        secure=True,
        samesite="lax",
        path="/",
    )
    return LoginResponse(ok=True)


@router.post("/auth/logout", response_model=LoginResponse)
def logout(response: Response) -> LoginResponse:
    response.delete_cookie(key=COOKIE_NAME, path="/")
    return LoginResponse(ok=True)


@router.get("/me", response_model=MeResponse)
def me(_: bool = Depends(require_user)) -> MeResponse:
    return MeResponse(authenticated=True)
