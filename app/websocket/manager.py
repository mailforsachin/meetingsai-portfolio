from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from typing import Any

from fastapi import WebSocket

log = logging.getLogger("meetingsai.ws")


class ConnectionManager:
    """In-process broadcast registry. One process, one instance."""

    def __init__(self) -> None:
        self._rooms: dict[str, set[WebSocket]] = defaultdict(set)
        self._lock = asyncio.Lock()

    async def connect(self, meeting_id: str, ws: WebSocket) -> None:
        await ws.accept()
        async with self._lock:
            self._rooms[meeting_id].add(ws)
            size = len(self._rooms[meeting_id])
        log.info("ws.connect meeting=%s subscribers=%d", meeting_id, size)

    async def disconnect(self, meeting_id: str, ws: WebSocket) -> None:
        async with self._lock:
            room = self._rooms.get(meeting_id)
            if room is not None:
                room.discard(ws)
                size = len(room)
                if not room:
                    self._rooms.pop(meeting_id, None)
            else:
                size = 0
        log.info("ws.disconnect meeting=%s subscribers=%d", meeting_id, size)

    async def broadcast_json(self, meeting_id: str, payload: dict[str, Any]) -> None:
        async with self._lock:
            targets = list(self._rooms.get(meeting_id, ()))
        n_targets = len(targets)
        if n_targets == 0:
            # No subscribers: this is the case where the browser is not connected.
            log.info("broadcast.no_subscribers meeting=%s type=%s",
                     meeting_id, payload.get("type"))
            return

        n_ok = 0
        n_failed = 0
        for ws in targets:
            try:
                await ws.send_json(payload)
                n_ok += 1
            except Exception as e:
                n_failed += 1
                log.info("broadcast.send_failed meeting=%s type=%s err=%s",
                         meeting_id, payload.get("type"), type(e).__name__)
                await self.disconnect(meeting_id, ws)

        log.info("broadcast.done meeting=%s type=%s targets=%d ok=%d failed=%d",
                 meeting_id, payload.get("type"), n_targets, n_ok, n_failed)

    def subscriber_count(self, meeting_id: str) -> int:
        return len(self._rooms.get(meeting_id, ()))

    def room_size(self, meeting_id: str) -> int:
        return self.subscriber_count(meeting_id)


manager = ConnectionManager()
