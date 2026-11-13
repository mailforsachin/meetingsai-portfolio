from __future__ import annotations

import hmac
import os
import secrets
import time
from dataclasses import dataclass
from hashlib import sha256
from typing import Optional

from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError, VerificationError, InvalidHashError

from app.core.config import get_settings

_ph = PasswordHasher(time_cost=3, memory_cost=65536, parallelism=2)

COOKIE_NAME = "meetingsai_session"
SESSION_MAX_AGE = 60 * 60 * 12  # 12 hours


def hash_password(plaintext: str) -> str:
    return _ph.hash(plaintext)


def verify_password(plaintext: str, hashed: str) -> bool:
    try:
        return _ph.verify(hashed, plaintext)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


def _sign(value: str, secret: str) -> str:
    return hmac.new(secret.encode(), value.encode(), sha256).hexdigest()


def issue_session_token() -> str:
    """Return a signed token: <issued_at>.<nonce>.<hmac>."""
    s = get_settings()
    issued_at = str(int(time.time()))
    nonce = secrets.token_hex(16)
    body = f"{issued_at}.{nonce}"
    sig = _sign(body, s.session_secret)
    return f"{body}.{sig}"


def verify_session_token(token: Optional[str]) -> bool:
    if not token:
        return False
    s = get_settings()
    parts = token.split(".")
    if len(parts) != 3:
        return False
    issued_at, nonce, sig = parts
    body = f"{issued_at}.{nonce}"
    expected = _sign(body, s.session_secret)
    if not hmac.compare_digest(expected, sig):
        return False
    try:
        ts = int(issued_at)
    except ValueError:
        return False
    if time.time() - ts > SESSION_MAX_AGE:
        return False
    return True
