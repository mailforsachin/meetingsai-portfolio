from __future__ import annotations

import os

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

# Force test settings before any app module is imported by the test session.
os.environ.setdefault("AUTH_PASSWORD_HASH", "")
os.environ.setdefault("SESSION_SECRET", "test-secret-not-for-production")

from app.core import config as cfg_mod  # noqa: E402
from app.core.rate_limit import login_limiter  # noqa: E402
from app.core.security import hash_password  # noqa: E402
from app.db.base import Base  # noqa: E402  (imports models via app.models)
import app.models  # noqa: E402,F401
from app.db.session import get_db  # noqa: E402


TEST_PASSWORD = "correct-horse"
TEST_SECRET = "test-secret-not-for-production"
TEST_BASE_URL = "https://testserver"


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("AUTH_PASSWORD_HASH", hash_password(TEST_PASSWORD))
    monkeypatch.setenv("SESSION_SECRET", TEST_SECRET)
    monkeypatch.setenv("APP_ENV", "development")
    # Force /stop's finalization to run synchronously so tests can observe
    # its effects deterministically.
    monkeypatch.setenv("FINALIZE_SYNC", "1")
    cfg_mod.get_settings.cache_clear()
    login_limiter.reset("testclient")
    yield
    cfg_mod.get_settings.cache_clear()
    login_limiter.reset("testclient")


@pytest.fixture
def engine(monkeypatch):
    # Single shared in-memory SQLite engine.
    eng = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
        future=True,
    )
    Base.metadata.create_all(eng)

    # Bind app.db.session.SessionLocal to the test engine so any code path
    # that opens a session directly (schedulers, background tasks, etc.)
    # sees the same database as the request handlers.
    import app.db.session as session_mod
    TestSession = sessionmaker(bind=eng, autoflush=False, autocommit=False, future=True)
    monkeypatch.setattr(session_mod, "SessionLocal", TestSession, raising=True)

    yield eng
    Base.metadata.drop_all(eng)
    eng.dispose()


@pytest.fixture
def db_session(engine):
    SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)
    s = SessionLocal()
    try:
        yield s
    finally:
        s.close()


@pytest.fixture
def client(engine):
    from app.main import app

    def _override_get_db():
        SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)
        db = SessionLocal()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = _override_get_db
    c = TestClient(app, base_url=TEST_BASE_URL)
    # Log in so Secure cookie is stored in the client's jar.
    r = c.post("/api/auth/login", json={"password": TEST_PASSWORD})
    assert r.status_code == 200, r.text
    yield c
    app.dependency_overrides.clear()


@pytest.fixture
def anon_client(engine):
    """A client with no login cookie."""
    from app.main import app

    def _override_get_db():
        SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)
        db = SessionLocal()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = _override_get_db
    c = TestClient(app, base_url=TEST_BASE_URL)
    yield c
    app.dependency_overrides.clear()
