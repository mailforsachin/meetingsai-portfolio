"""Stage C: MariaDB/InnoDB integration tests for the durable provider budget.

These tests require Docker.  They spin up an isolated mariadb:11.4
container, exercise the reservation algorithm under real row locking,
and tear the container down.  They never touch the production database.

When Docker is unavailable, the whole module is skipped.
"""
from __future__ import annotations

import atexit
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

pytest.importorskip("docker", reason="Docker SDK not installed")

CONTAINER = "meetingsai-budget-test"
DB_USER = "testuser"
DB_PASSWORD = "testpass"
DB_NAME = "budget_test"


def _docker_available() -> bool:
    try:
        r = subprocess.run(
            ["docker", "info"], capture_output=True, text=True, timeout=5,
        )
        return r.returncode == 0
    except Exception:
        return False


pytestmark = pytest.mark.skipif(
    not _docker_available(), reason="Docker daemon not available",
)


@pytest.fixture(scope="module")
def mariadb_url():
    subprocess.run(["docker", "rm", "-f", CONTAINER], capture_output=True)
    subprocess.run([
        "docker", "run", "-d", "--name", CONTAINER,
        "-e", f"MARIADB_USER={DB_USER}",
        "-e", f"MARIADB_PASSWORD={DB_PASSWORD}",
        "-e", f"MARIADB_DATABASE={DB_NAME}",
        "-e", "MARIADB_RANDOM_ROOT_PASSWORD=1",
        "-p", "127.0.0.1::3306", "mariadb:11.4",
    ], check=True, capture_output=True)
    atexit.register(
        lambda: subprocess.run(["docker", "stop", CONTAINER], capture_output=True)
    )

    port_out = subprocess.run(
        ["docker", "port", CONTAINER, "3306/tcp"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    port = int(port_out.splitlines()[-1].rsplit(":", 1)[-1])

    import pymysql
    deadline = time.monotonic() + 60
    while True:
        try:
            pymysql.connect(
                host="127.0.0.1", port=port,
                user=DB_USER, password=DB_PASSWORD, database=DB_NAME,
            ).close()
            break
        except Exception:
            if time.monotonic() > deadline:
                raise RuntimeError("MariaDB did not become ready in 60s")
            time.sleep(0.5)

    yield f"mysql+pymysql://{DB_USER}:{DB_PASSWORD}@127.0.0.1:{port}/{DB_NAME}?charset=utf8mb4"


@pytest.fixture
def engine(mariadb_url):
    from sqlalchemy import create_engine, text
    eng = create_engine(mariadb_url, future=True)
    with eng.begin() as c:
        c.execute(text("DROP TABLE IF EXISTS topic_provider_budgets"))
        c.execute(text("""
            CREATE TABLE topic_provider_budgets (
                meeting_id VARCHAR(36) PRIMARY KEY,
                used_calls INT NOT NULL DEFAULT 0,
                created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
            ) ENGINE=InnoDB
        """))
    yield eng
    eng.dispose()


def _reserve(engine, meeting_id, cap):
    """Replicate the helper's transaction exactly, in its own session."""
    from sqlalchemy import text
    with engine.begin() as conn:
        conn.execute(text(
            "INSERT INTO topic_provider_budgets (meeting_id, used_calls) "
            "VALUES (:mid, 0) "
            "ON DUPLICATE KEY UPDATE meeting_id = meeting_id"
        ), {"mid": meeting_id})
        r = conn.execute(text(
            "UPDATE topic_provider_budgets "
            "SET used_calls = used_calls + 1 "
            "WHERE meeting_id = :mid AND used_calls < :cap"
        ), {"mid": meeting_id, "cap": cap})
        return r.rowcount


def _final_count(engine, meeting_id):
    from sqlalchemy import text
    with engine.begin() as conn:
        return conn.execute(text(
            "SELECT used_calls FROM topic_provider_budgets WHERE meeting_id = :m"
        ), {"m": meeting_id}).scalar()


def test_cap_one_two_concurrent_workers_yield_one_success(engine):
    with ThreadPoolExecutor(max_workers=2) as ex:
        results = list(ex.map(lambda _: _reserve(engine, "m1", 1), range(2)))
    assert sorted(results) == [0, 1], results
    assert _final_count(engine, "m1") == 1


def test_cap_two_two_concurrent_workers_yield_two_successes(engine):
    with ThreadPoolExecutor(max_workers=2) as ex:
        results = list(ex.map(lambda _: _reserve(engine, "m1", 2), range(2)))
    assert sorted(results) == [1, 1], results
    assert _final_count(engine, "m1") == 2


def test_count_persists_across_new_engine(engine, mariadb_url):
    """Simulates a restart: a fresh engine reads the persisted counter."""
    from sqlalchemy import create_engine
    _reserve(engine, "m1", 5)
    _reserve(engine, "m1", 5)
    eng2 = create_engine(mariadb_url, future=True)
    try:
        assert _final_count(eng2, "m1") == 2
    finally:
        eng2.dispose()


def test_idempotent_row_initialization(engine):
    """Two init inserts do not error or double-insert."""
    assert _reserve(engine, "m1", 5) == 1
    assert _reserve(engine, "m1", 5) == 1
    assert _final_count(engine, "m1") == 2
