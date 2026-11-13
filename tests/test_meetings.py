from __future__ import annotations


def _create(client, **overrides):
    body = {
        "title": "Customer discovery",
        "objective": "Understand requirements",
        "organization": "Acme",
        "role": "TPMO Manager",
        "notes": "kickoff",
    }
    body.update(overrides)
    r = client.post("/api/meetings", json=body)
    assert r.status_code == 201, r.text
    return r.json()


def test_create_meeting(client):
    m = _create(client)
    assert m["id"]
    assert m["title"] == "Customer discovery"
    assert m["status"] == "created"
    assert m["started_at"] is None
    assert m["ended_at"] is None


def test_list_meetings(client):
    _create(client, title="A")
    _create(client, title="B")
    r = client.get("/api/meetings")
    assert r.status_code == 200
    body = r.json()
    assert body["total"] == 2
    assert len(body["items"]) == 2


def test_get_meeting(client):
    m = _create(client)
    r = client.get(f"/api/meetings/{m['id']}")
    assert r.status_code == 200
    assert r.json()["id"] == m["id"]


def test_get_meeting_404(client):
    r = client.get("/api/meetings/does-not-exist")
    assert r.status_code == 404


def test_start_meeting(client):
    m = _create(client)
    r = client.post(f"/api/meetings/{m['id']}/start")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "active"
    assert body["started_at"] is not None


def test_start_idempotent(client):
    m = _create(client)
    client.post(f"/api/meetings/{m['id']}/start")
    r = client.post(f"/api/meetings/{m['id']}/start")
    assert r.status_code == 200
    assert r.json()["status"] == "active"


def test_stop_meeting(client):
    m = _create(client)
    client.post(f"/api/meetings/{m['id']}/start")
    r = client.post(f"/api/meetings/{m['id']}/stop")
    assert r.status_code == 200
    body = r.json()
    # Phase 11.5: with FINALIZE_SYNC=1 (test mode) /stop runs finalization
    # synchronously and the meeting transitions to 'completed' before returning.
    # In production the response is 'finalizing' and the background task
    # transitions the meeting to 'completed' shortly after.
    assert body["status"] == "completed"
    assert body["ended_at"] is not None


def test_stop_before_start(client):
    m = _create(client)
    r = client.post(f"/api/meetings/{m['id']}/stop")
    assert r.status_code == 200
    # Phase 11.5: with FINALIZE_SYNC=1 (test mode) the meeting is completed
    # by the time /stop returns.
    assert r.json()["status"] == "completed"


def test_start_after_end_conflict(client):
    m = _create(client)
    client.post(f"/api/meetings/{m['id']}/stop")
    r = client.post(f"/api/meetings/{m['id']}/start")
    assert r.status_code == 409


def test_meetings_require_auth(anon_client):
    r = anon_client.get("/api/meetings")
    assert r.status_code == 401
    r = anon_client.post("/api/meetings", json={"title": "x"})
    assert r.status_code == 401
