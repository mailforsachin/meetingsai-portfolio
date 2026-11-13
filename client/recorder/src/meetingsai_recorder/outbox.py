from __future__ import annotations

import logging
import os
import sqlite3
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, Optional


log = logging.getLogger("meetingsai.outbox")


DEFAULT_DB_PATH = Path(os.path.expanduser("~")) / ".meetingsai" / "outbox.sqlite3"


SCHEMA = """
CREATE TABLE IF NOT EXISTS segments (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    segment_id    TEXT    NOT NULL UNIQUE,
    meeting_id    TEXT    NOT NULL,
    started_at    TEXT    NOT NULL,     -- ISO 8601 UTC
    ended_at      TEXT    NOT NULL,     -- ISO 8601 UTC
    text          TEXT    NOT NULL,
    model         TEXT,
    speaker_id    TEXT,
    speaker_name  TEXT,
    created_at    TEXT    NOT NULL,
    attempts      INTEGER NOT NULL DEFAULT 0,
    last_error    TEXT,
    acked_at      TEXT,
    leased_until  REAL
);
CREATE INDEX IF NOT EXISTS ix_segments_pending
    ON segments (acked_at, leased_until);
"""


@dataclass(frozen=True)
class Segment:
    segment_id: str
    meeting_id: str
    started_at: str
    ended_at: str
    text: str
    model: Optional[str] = None
    speaker_id: Optional[str] = None
    speaker_name: Optional[str] = None


def new_segment_id() -> str:
    return str(uuid.uuid4())


class Outbox:
    """Durable SQLite queue of transcript segments waiting to be pushed."""

    def __init__(self, db_path: Optional[Path] = None) -> None:
        self.db_path = Path(db_path) if db_path else DEFAULT_DB_PATH
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        c = sqlite3.connect(str(self.db_path), timeout=15.0)
        try:
            c.execute("PRAGMA journal_mode=WAL;")
            c.execute("PRAGMA synchronous=NORMAL;")
            c.execute("PRAGMA foreign_keys=ON;")
            yield c
            c.commit()
        finally:
            c.close()

    def _init_schema(self) -> None:
        with self._conn() as c:
            c.executescript(SCHEMA)
        log.info("outbox ready at %s", self.db_path)

    def enqueue(self, seg: Segment) -> bool:
        """Return True if inserted, False if the segment_id already exists."""
        with self._conn() as c:
            try:
                c.execute(
                    """
                    INSERT INTO segments
                      (segment_id, meeting_id, started_at, ended_at,
                       text, model, speaker_id, speaker_name, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))
                    """,
                    (
                        seg.segment_id,
                        seg.meeting_id,
                        seg.started_at,
                        seg.ended_at,
                        seg.text,
                        seg.model,
                        seg.speaker_id,
                        seg.speaker_name,
                    ),
                )
                return True
            except sqlite3.IntegrityError:
                return False

    def lease_batch(self, limit: int = 50, lease_seconds: int = 60) -> list[Segment]:
        """Return up to `limit` unacked segments and mark them leased for `lease_seconds`."""
        now = time.time()
        deadline = now + lease_seconds
        with self._conn() as c:
            rows = c.execute(
                """
                SELECT segment_id, meeting_id, started_at, ended_at, text,
                       model, speaker_id, speaker_name
                FROM segments
                WHERE acked_at IS NULL
                  AND (leased_until IS NULL OR leased_until < ?)
                ORDER BY id ASC
                LIMIT ?
                """,
                (now, limit),
            ).fetchall()
            ids = [r[0] for r in rows]
            if ids:
                c.executemany(
                    "UPDATE segments SET leased_until = ? WHERE segment_id = ?",
                    [(deadline, sid) for sid in ids],
                )
        return [
            Segment(
                segment_id=r[0], meeting_id=r[1], started_at=r[2],
                ended_at=r[3], text=r[4], model=r[5],
                speaker_id=r[6], speaker_name=r[7],
            )
            for r in rows
        ]

    def ack(self, segment_ids: list[str]) -> None:
        if not segment_ids:
            return
        with self._conn() as c:
            c.executemany(
                "UPDATE segments SET acked_at = datetime('now'), leased_until = NULL "
                "WHERE segment_id = ?",
                [(sid,) for sid in segment_ids],
            )

    def nack(self, segment_ids: list[str], error: str) -> None:
        if not segment_ids:
            return
        with self._conn() as c:
            c.executemany(
                "UPDATE segments SET attempts = attempts + 1, "
                "last_error = ?, leased_until = NULL WHERE segment_id = ?",
                [(error[:500], sid) for sid in segment_ids],
            )

    def stats(self) -> dict:
        with self._conn() as c:
            pending = c.execute(
                "SELECT COUNT(*) FROM segments WHERE acked_at IS NULL"
            ).fetchone()[0]
            acked = c.execute(
                "SELECT COUNT(*) FROM segments WHERE acked_at IS NOT NULL"
            ).fetchone()[0]
            total = c.execute("SELECT COUNT(*) FROM segments").fetchone()[0]
        return {"pending": pending, "acked": acked, "total": total}
