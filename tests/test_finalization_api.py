from __future__ import annotations

from app.models.meeting import Meeting


def _create_meeting(client, **overrides):
    body = {"title": "finalization-test"}
    body.update(overrides)
    r = client.post("/api/meetings", json=body)
    assert r.status_code == 201, r.text
    return r.json()["id"]


def test_routes_registered(client):
    from app.main import app
    paths = {getattr(r, "path", None) for r in app.routes}
    assert "/api/meetings/{meeting_id}/finalize" in paths
    assert "/api/meetings/{meeting_id}/finalization" in paths
    assert "/api/meetings/{meeting_id}/export.json" in paths
    assert "/api/meetings/{meeting_id}/export.md" in paths


def test_finalization_404_when_absent(client):
    mid = _create_meeting(client)
    r = client.get(f"/api/meetings/{mid}/finalization")
    assert r.status_code == 404


def test_finalization_404_for_unknown_meeting(client):
    r = client.get("/api/meetings/does-not-exist/finalization")
    assert r.status_code == 404


def test_export_json_404_for_unknown_meeting(client):
    r = client.get("/api/meetings/does-not-exist/export.json")
    assert r.status_code == 404


def test_export_md_404_for_unknown_meeting(client):
    r = client.get("/api/meetings/does-not-exist/export.md")
    assert r.status_code == 404


def test_export_json_basic(client):
    mid = _create_meeting(client, title="Export Test", organization="Acme")
    r = client.get(f"/api/meetings/{mid}/export.json")
    assert r.status_code == 200
    body = r.json()
    assert body["meeting"]["id"] == mid
    assert body["meeting"]["title"] == "Export Test"
    assert body["finalization"] is None
    assert "state" in body
    assert "raw_transcript" in body
    assert "discussion_segments" in body


def test_export_md_basic(client):
    mid = _create_meeting(client, title="Export MD", objective="Check format")
    r = client.get(f"/api/meetings/{mid}/export.md")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/plain")
    text = r.text
    assert text.startswith("# Export MD")
    assert "**Objective:** Check format" in text


def test_endpoints_require_auth(anon_client):
    for path in (
        "/api/meetings/anything/finalize",
        "/api/meetings/anything/finalization",
        "/api/meetings/anything/export.json",
        "/api/meetings/anything/export.md",
    ):
        r = anon_client.get(path) if path != "/api/meetings/anything/finalize" else anon_client.post(path)
        assert r.status_code == 401, f"{path} expected 401 got {r.status_code}"
