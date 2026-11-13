from __future__ import annotations

from fastapi.testclient import TestClient

from app.core.security import COOKIE_NAME
from app.models.meeting import Meeting
from app.websocket.manager import manager


def _client() -> TestClient:
    """Create an authenticated TestClient from the session cookie of the shared `client` fixture."""
    from app.main import app
    return TestClient(app, base_url="https://testserver")


def test_ws_receives_state_update_broadcast(client, db_session):
    # Create a meeting with a database id matching what the scheduler will broadcast for.
    m = Meeting(title="ws-state-test", status="active")
    db_session.add(m)
    db_session.commit()
    db_session.refresh(m)
    mid = m.id

    tok = client.cookies.get(COOKIE_NAME)
    assert tok, "shared client has no session cookie"

    with client.websocket_connect(f"/ws/meetings/{mid}?token={tok}") as ws:
        hello = ws.receive_json()
        assert hello.get("type") == "hello"
        assert hello.get("meeting_id") == mid

        # Simulate what the analysis scheduler does: broadcast state_update
        # through the manager for this meeting.
        import asyncio

        async def _emit():
            await manager.broadcast_json(mid, {
                "type": "state_update",
                "meeting_id": mid,
                "run_id": 1,
            })

        asyncio.run(_emit())

        msg = ws.receive_json()
        assert msg.get("type") == "state_update"
        assert msg.get("meeting_id") == mid
        assert msg.get("run_id") == 1
