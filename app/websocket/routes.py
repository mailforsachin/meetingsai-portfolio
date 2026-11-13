from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from app.core.security import COOKIE_NAME, verify_session_token
from app.websocket.manager import manager

log = logging.getLogger("meetingsai.ws")

router = APIRouter()

HEARTBEAT_SECONDS = 15


def _extract_token(websocket: WebSocket) -> str | None:
    """Session token from cookie first, then ?token= query param.

    Browsers normally send the cookie on WS upgrade; the query-param fallback
    exists for environments/tests that do not, using the same signed token.
    """
    tok = websocket.cookies.get(COOKIE_NAME)
    if tok:
        return tok
    return websocket.query_params.get("token")


@router.websocket("/ws/meetings/{meeting_id}")
async def ws_meeting(websocket: WebSocket, meeting_id: str) -> None:
    if not verify_session_token(_extract_token(websocket)):
        await websocket.close(code=4401)
        return

    await manager.connect(meeting_id, websocket)
    log.info("ws connect meeting=%s size=%d", meeting_id, manager.room_size(meeting_id))
    try:
        await websocket.send_json({"type": "hello", "meeting_id": meeting_id})
        while True:
            try:
                await asyncio.wait_for(websocket.receive_text(), timeout=HEARTBEAT_SECONDS)
            except asyncio.TimeoutError:
                await websocket.send_json({"type": "heartbeat"})
    except WebSocketDisconnect:
        pass
    except Exception as e:
        log.warning("ws error meeting=%s err=%s", meeting_id, e)
    finally:
        await manager.disconnect(meeting_id, websocket)
        log.info("ws disconnect meeting=%s size=%d", meeting_id, manager.room_size(meeting_id))
