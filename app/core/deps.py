from __future__ import annotations

from fastapi import Cookie, HTTPException, status

from app.core.security import COOKIE_NAME, verify_session_token


def require_user(session: str | None = Cookie(default=None, alias=COOKIE_NAME)) -> bool:
    if not verify_session_token(session):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="unauthorized")
    return True
