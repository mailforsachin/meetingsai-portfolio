"""Auth tests.

Notes:
- The session cookie is set with Secure=True (production behavior).
  TestClient is therefore constructed with base_url="https://testserver"
  so httpx stores and replays the cookie.
- Each test gets its own TestClient so cookie jars are isolated.
- The login rate limiter is reset per test.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.core import config as cfg_mod
from app.core.rate_limit import login_limiter
from app.core.security import COOKIE_NAME, hash_password


TEST_PASSWORD = "correct-horse"
BASE_URL = "https://testserver"


@pytest.fixture(autouse=True)
def _test_env(monkeypatch):
    monkeypatch.setenv("AUTH_PASSWORD_HASH", hash_password(TEST_PASSWORD))
    monkeypatch.setenv("SESSION_SECRET", "test-secret-not-for-production")
    cfg_mod.get_settings.cache_clear()
    login_limiter.reset("testclient")
    yield
    cfg_mod.get_settings.cache_clear()


@pytest.fixture
def client():
    from app.main import app
    return TestClient(app, base_url=BASE_URL)


def test_login_ok(client):
    r = client.post("/api/auth/login", json={"password": TEST_PASSWORD})
    assert r.status_code == 200
    assert r.json() == {"ok": True}
    assert COOKIE_NAME in r.cookies


def test_login_wrong(client):
    r = client.post("/api/auth/login", json={"password": "wrong"})
    assert r.status_code == 401


def test_lockout_after_five(client):
    for _ in range(5):
        client.post("/api/auth/login", json={"password": "wrong"})
    r = client.post("/api/auth/login", json={"password": TEST_PASSWORD})
    assert r.status_code == 429


def test_me_requires_auth(client):
    r = client.get("/api/me")
    assert r.status_code == 401


def test_me_with_cookie(client):
    r = client.post("/api/auth/login", json={"password": TEST_PASSWORD})
    assert r.status_code == 200
    r = client.get("/api/me")
    assert r.status_code == 200
    assert r.json() == {"authenticated": True}


def test_ping_requires_auth(client):
    r = client.get("/api/_ping")
    assert r.status_code == 401


def test_ping_with_cookie(client):
    r = client.post("/api/auth/login", json={"password": TEST_PASSWORD})
    assert r.status_code == 200
    r = client.get("/api/_ping")
    assert r.status_code == 200
    assert r.json() == {"pong": True}


def test_logout_clears_cookie(client):
    client.post("/api/auth/login", json={"password": TEST_PASSWORD})
    r = client.post("/api/auth/logout")
    assert r.status_code == 200
    assert r.json() == {"ok": True}
    r = client.get("/api/me")
    assert r.status_code == 401


def test_cookie_flags():
    """Sanity check: the cookie must be HttpOnly, Secure, SameSite=Lax."""
    from app.main import app
    c = TestClient(app, base_url=BASE_URL)
    r = c.post("/api/auth/login", json={"password": TEST_PASSWORD})
    set_cookie = r.headers.get("set-cookie", "")
    assert "HttpOnly" in set_cookie
    assert "Secure" in set_cookie
    assert "SameSite=lax" in set_cookie or "SameSite=Lax" in set_cookie
