"""Regression tests for the Context Review API surfaces added in G5."""
from __future__ import annotations

from app.models.context_gap import ContextGap
from app.models.meeting import Meeting
from app.models.thread import Thread


def _create_meeting(client, title="gap-api-test"):
    r = client.post("/api/meetings", json={"title": title})
    assert r.status_code == 201
    return r.json()["id"]


def _seed_gap(db, meeting_id: str, text: str, urgency: str = "high"):
    g = ContextGap(
        meeting_id=meeting_id,
        gap_kind="owner_gap",
        category="contextual",
        text=text,
        urgency=urgency,
        status="open",
        norm_hash=f"hash-{abs(hash(text))}",
    )
    db.add(g); db.commit(); db.refresh(g)
    return g


def test_state_includes_context_gaps(client, db_session):
    mid = _create_meeting(client)
    _seed_gap(db_session, mid, "Who owns the 22 SharePoint sites?", urgency="high")
    _seed_gap(db_session, mid, "Is the ~$100K estimate confirmed?", urgency="critical")

    r = client.get(f"/api/meetings/{mid}/state")
    assert r.status_code == 200
    body = r.json()
    assert body["context_gaps_total"] == 2
    assert len(body["context_gaps"]) == 2
    # Critical first, then high.
    assert body["context_gaps"][0]["urgency"] == "critical"


def test_context_gaps_endpoint_returns_open_by_default(client, db_session):
    mid = _create_meeting(client)
    _seed_gap(db_session, mid, "Gap A", urgency="medium")
    g = _seed_gap(db_session, mid, "Gap B", urgency="low")
    g.status = "resolved"; db_session.commit()

    r = client.get(f"/api/meetings/{mid}/context_gaps")
    assert r.status_code == 200
    body = r.json()
    assert body["total"] == 1
    assert body["items"][0]["text"] == "Gap A"


def test_context_gaps_endpoint_status_filter(client, db_session):
    mid = _create_meeting(client)
    _seed_gap(db_session, mid, "Open gap")
    g = _seed_gap(db_session, mid, "Resolved gap")
    g.status = "resolved"; db_session.commit()

    r = client.get(f"/api/meetings/{mid}/context_gaps?status=resolved")
    assert r.status_code == 200
    body = r.json()
    assert body["total"] == 1
    assert body["items"][0]["text"] == "Resolved gap"
