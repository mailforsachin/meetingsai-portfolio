from __future__ import annotations

from app.core.security import COOKIE_NAME


def _create(client):
    r = client.post("/api/meetings", json={"title": "T"})
    assert r.status_code == 201
    return r.json()["id"]


def _seg(sid: str, text: str = "hello"):
    return {"segment_id": sid, "text": text, "model": "test"}


def _token(client) -> str:
    tok = client.cookies.get(COOKIE_NAME)
    assert tok, "no session cookie on test client"
    return tok


def test_ingest_batch(client):
    mid = _create(client)
    body = {"segments": [_seg("s1"), _seg("s2"), _seg("s3")]}
    r = client.post(f"/api/meetings/{mid}/transcript", json=body)
    assert r.status_code == 200, r.text
    assert r.json() == {"accepted": 3, "duplicates": 0}


def test_ingest_idempotent(client):
    mid = _create(client)
    client.post(f"/api/meetings/{mid}/transcript", json={"segments": [_seg("s1"), _seg("s2")]})
    r = client.post(
        f"/api/meetings/{mid}/transcript",
        json={"segments": [_seg("s1"), _seg("s2"), _seg("s3")]},
    )
    assert r.status_code == 200
    assert r.json() == {"accepted": 1, "duplicates": 2}


def test_ingest_unknown_meeting(client):
    r = client.post(
        "/api/meetings/does-not-exist/transcript",
        json={"segments": [_seg("s1")]},
    )
    assert r.status_code == 404


def test_list_transcript(client):
    mid = _create(client)
    client.post(
        f"/api/meetings/{mid}/transcript",
        json={"segments": [_seg("a", "one"), _seg("b", "two")]},
    )
    r = client.get(f"/api/meetings/{mid}/transcript")
    assert r.status_code == 200
    body = r.json()
    assert body["total"] == 2
    texts = [x["text"] for x in body["items"]]
    assert texts == ["one", "two"]


def test_transcript_requires_auth(anon_client):
    r = anon_client.get("/api/meetings/anything/transcript")
    assert r.status_code == 401
    r = anon_client.post("/api/meetings/anything/transcript", json={"segments": [_seg("s1")]})
    assert r.status_code == 401


def test_ws_broadcasts_new_segment(client):
    mid = _create(client)
    tok = _token(client)

    with client.websocket_connect(f"/ws/meetings/{mid}?token={tok}") as ws:
        hello = ws.receive_json()
        assert hello.get("type") == "hello", hello
        assert hello.get("meeting_id") == mid

        r = client.post(
            f"/api/meetings/{mid}/transcript",
            json={"segments": [_seg("ws-1", "hello world")]},
        )
        assert r.status_code == 200
        assert r.json() == {"accepted": 1, "duplicates": 0}

        msg = ws.receive_json()
        assert msg.get("type") == "segment", msg
        assert msg.get("meeting_id") == mid
        assert msg.get("segment_id") == "ws-1"
        assert msg.get("text") == "hello world"


def test_ws_does_not_broadcast_duplicates(client):
    mid = _create(client)
    tok = _token(client)

    client.post(
        f"/api/meetings/{mid}/transcript",
        json={"segments": [_seg("dup-1", "first")]},
    )

    with client.websocket_connect(f"/ws/meetings/{mid}?token={tok}") as ws:
        hello = ws.receive_json()
        assert hello.get("type") == "hello"

        # Duplicate: no broadcast expected.
        r = client.post(
            f"/api/meetings/{mid}/transcript",
            json={"segments": [_seg("dup-1", "first")]},
        )
        assert r.status_code == 200
        assert r.json() == {"accepted": 0, "duplicates": 1}

        # Fresh: broadcast expected.
        r = client.post(
            f"/api/meetings/{mid}/transcript",
            json={"segments": [_seg("dup-2", "second")]},
        )
        assert r.status_code == 200
        assert r.json() == {"accepted": 1, "duplicates": 0}

        first = ws.receive_json()
        assert first.get("type") == "segment"
        assert first.get("segment_id") == "dup-2"
