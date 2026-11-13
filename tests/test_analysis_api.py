from __future__ import annotations

import json

from app.models.state import AIAnalysisRun, Fact


def _create(client, **overrides):
    body = {"title": "analysis-test"}
    body.update(overrides)
    r = client.post("/api/meetings", json=body)
    assert r.status_code == 201, r.text
    return r.json()["id"]


def test_state_empty(client):
    mid = _create(client)
    r = client.get(f"/api/meetings/{mid}/state")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["meeting_id"] == mid
    assert body["facts"] == []
    assert body["decisions"] == []
    assert body["action_items"] == []
    assert body["risks"] == []
    assert body["questions_now"] == []
    assert body["questions_later"] == []
    assert body["last_snapshot_at"] is None
    assert body["last_analysis_run_id"] is None


def test_state_404_for_unknown_meeting(client):
    r = client.get("/api/meetings/does-not-exist/state")
    assert r.status_code == 404


def test_analysis_list_empty(client):
    mid = _create(client)
    r = client.get(f"/api/meetings/{mid}/analysis")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body == {"items": [], "total": 0, "limit": 50, "offset": 0}


def test_analysis_list_scoping_and_order(client, db_session):
    mid_a = _create(client, title="A")
    mid_b = _create(client, title="B")
    db_session.add_all([
        AIAnalysisRun(meeting_id=mid_a, kind="rolling", prompt_version="p",
                      model="m", status="ok"),
        AIAnalysisRun(meeting_id=mid_a, kind="rolling", prompt_version="p",
                      model="m", status="error"),
        AIAnalysisRun(meeting_id=mid_b, kind="rolling", prompt_version="p",
                      model="m", status="ok"),
    ])
    db_session.commit()

    r = client.get(f"/api/meetings/{mid_a}/analysis")
    assert r.status_code == 200
    body = r.json()
    assert body["total"] == 2
    assert all(item["meeting_id"] == mid_a for item in body["items"])
    # Newest first; both rows have the same started_at default, so id desc.
    assert body["items"][0]["id"] > body["items"][1]["id"]


def test_analysis_output_404_for_other_meeting(client, db_session):
    mid_a = _create(client, title="A")
    mid_b = _create(client, title="B")
    run = AIAnalysisRun(
        meeting_id=mid_a, kind="rolling", prompt_version="p", model="m",
        status="ok", output_json=json.dumps({"understanding": "x"}),
    )
    db_session.add(run); db_session.commit(); db_session.refresh(run)

    # Same run_id, wrong meeting_id → 404.
    r = client.get(f"/api/meetings/{mid_b}/analysis/{run.id}/output")
    assert r.status_code == 404

    # Correct meeting → 200 and parsed output.
    r = client.get(f"/api/meetings/{mid_a}/analysis/{run.id}/output")
    assert r.status_code == 200
    body = r.json()
    assert body["run_id"] == run.id
    assert body["meeting_id"] == mid_a
    assert body["output"] == {"understanding": "x"}


def test_analysis_output_409_when_output_json_missing(client, db_session):
    mid = _create(client)
    run = AIAnalysisRun(
        meeting_id=mid, kind="rolling", prompt_version="p", model="m",
        status="error", output_json=None,
    )
    db_session.add(run); db_session.commit(); db_session.refresh(run)

    r = client.get(f"/api/meetings/{mid}/analysis/{run.id}/output")
    assert r.status_code == 409


def test_endpoints_require_auth(anon_client):
    r = anon_client.get("/api/meetings/anything/state")
    assert r.status_code == 401
    r = anon_client.get("/api/meetings/anything/analysis")
    assert r.status_code == 401
    r = anon_client.get("/api/meetings/anything/analysis/1/output")
    assert r.status_code == 401


def test_state_reads_conversational_fields_from_latest_snapshot(client, db_session):
    """understanding / potential_response / lists come from the latest snapshot."""
    import json
    from app.models.state import MeetingStateSnapshot

    mid = _create(client, title="snapshot-enrichment")

    # No snapshot yet -> safe defaults.
    r = client.get(f"/api/meetings/{mid}/state")
    assert r.status_code == 200
    body = r.json()
    assert body["understanding"] is None
    assert body["potential_response"] is None
    assert body["key_discussion"] == []
    assert body["missing_information"] == []
    assert body["contradictions"] == []
    assert body["next_steps"] == []

    # Insert a snapshot with conversational fields.
    db_session.add(MeetingStateSnapshot(
        meeting_id=mid,
        run_id=None,
        state_json=json.dumps({
            "understanding": "the customer wants production by Oct 20",
            "key_discussion": ["go-live date", "customer access"],
            "potential_response": "for the POC we could...",
            "missing_information": ["who owns the deploy window"],
            "contradictions": ["Oct 30 mentioned earlier as go-live"],
            "next_steps": ["confirm rollout owner"],
        }),
    ))
    db_session.commit()

    r = client.get(f"/api/meetings/{mid}/state")
    assert r.status_code == 200
    body = r.json()
    assert body["understanding"] == "the customer wants production by Oct 20"
    assert body["potential_response"] == "for the POC we could..."
    assert body["key_discussion"] == ["go-live date", "customer access"]
    assert body["missing_information"] == ["who owns the deploy window"]
    assert body["contradictions"] == ["Oct 30 mentioned earlier as go-live"]
    assert body["next_steps"] == ["confirm rollout owner"]


def test_state_tolerates_malformed_snapshot(client, db_session):
    """A malformed snapshot must not break /state."""
    from app.models.state import MeetingStateSnapshot

    mid = _create(client, title="malformed-snapshot")
    db_session.add(MeetingStateSnapshot(
        meeting_id=mid, run_id=None, state_json="{not valid json",
    ))
    db_session.commit()

    r = client.get(f"/api/meetings/{mid}/state")
    assert r.status_code == 200
    body = r.json()
    assert body["understanding"] is None
    assert body["key_discussion"] == []
    assert body["contradictions"] == []
