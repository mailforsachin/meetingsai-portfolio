"""Resilience tests for the live-pipeline: ingest + WS.

These tests are the invariant guard for the production bug where the
browser displayed "No input received" while the recorder was still
sending.  They assert:

  1. Ingest works with an open WS and the frame reaches the subscriber.
  2. Ingest works with no subscriber, does not raise, persists to DB.
  3. ws_subscribers in the meeting response reflects reality.
  4. Reconnecting does not duplicate transcript segments.
"""
from __future__ import annotations


def _create(client, title="ws-resilience"):
    r = client.post("/api/meetings", json={"title": title})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _batch(mid: str, n_start: int, n_count: int):
    return {"segments": [
        {"segment_id": f"{mid}-s{n_start + i}",
         "text": f"segment {n_start + i}",
         "model": "test"}
        for i in range(n_count)
    ]}


def test_ingest_reaches_open_ws_and_ws_subscribers_reflects_one(client):
    mid = _create(client)

    with client.websocket_connect(f"/ws/meetings/{mid}?token={_token(client)}") as ws:
        # First message is the hello frame.
        hello = ws.receive_json()
        assert hello.get("type") == "hello"

        # Ingest a batch.
        r = client.post(f"/api/meetings/{mid}/transcript", json=_batch(mid, 0, 2))
        assert r.status_code == 200, r.text
        assert r.json() == {"accepted": 2, "duplicates": 0}

        # WS receives at least one segment frame (the first of the two).
        msg = ws.receive_json()
        assert msg.get("type") == "segment"
        assert msg.get("segment_id").startswith(mid + "-s")

        # The meeting response reports one live subscriber.
        m = client.get(f"/api/meetings/{mid}").json()
        assert m["ws_subscribers"] == 1


def test_ingest_works_with_no_ws_subscriber(client):
    """The exact production scenario: recorder keeps POSTing, browser is not
    connected.  Ingest must succeed and persist, and ws_subscribers must be 0."""
    mid = _create(client)

    # No WS opened.
    r = client.post(f"/api/meetings/{mid}/transcript", json=_batch(mid, 10, 3))
    assert r.status_code == 200, r.text
    assert r.json() == {"accepted": 3, "duplicates": 0}

    # Meeting response shows zero subscribers.
    m = client.get(f"/api/meetings/{mid}").json()
    assert m["ws_subscribers"] == 0

    # And the segments are in the DB.
    t = client.get(f"/api/meetings/{mid}/transcript").json()
    assert t["total"] == 3


def test_reconnect_does_not_duplicate_transcript(client):
    """Open, ingest, close, ingest more, reopen, ingest more; verify transcript
    contains each segment_id exactly once."""
    mid = _create(client)
    tok = _token(client)

    with client.websocket_connect(f"/ws/meetings/{mid}?token={tok}") as ws:
        assert ws.receive_json().get("type") == "hello"
        r = client.post(f"/api/meetings/{mid}/transcript", json=_batch(mid, 0, 2))
        assert r.status_code == 200
        assert r.json()["accepted"] == 2
        # Drain the two segment frames.
        for _ in range(2):
            msg = ws.receive_json()
            assert msg.get("type") == "segment"
    # WS now closed.

    # Ingest while no subscriber.
    r = client.post(f"/api/meetings/{mid}/transcript", json=_batch(mid, 2, 2))
    assert r.status_code == 200
    assert r.json()["accepted"] == 2

    # Reopen WS.  Assert hello, then ingest a third batch.
    with client.websocket_connect(f"/ws/meetings/{mid}?token={tok}") as ws:
        assert ws.receive_json().get("type") == "hello"
        r = client.post(f"/api/meetings/{mid}/transcript", json=_batch(mid, 4, 1))
        assert r.status_code == 200
        assert r.json()["accepted"] == 1
        msg = ws.receive_json()
        assert msg.get("type") == "segment"

    # Now verify the transcript contains each segment_id exactly once.
    body = client.get(f"/api/meetings/{mid}/transcript").json()
    ids = [item["segment_id"] for item in body["items"]]
    assert len(ids) == len(set(ids)), "duplicate segment_id in transcript"
    assert body["total"] == 5


def _token(client) -> str:
    from app.core.security import COOKIE_NAME
    tok = client.cookies.get(COOKIE_NAME)
    assert tok, "no session cookie on test client"
    return tok
