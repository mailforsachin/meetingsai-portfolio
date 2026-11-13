"""Tests for finalize-on-stop behavior.

We monkeypatch AIAnalysisProvider.run to return a canned, valid
FinalizationOutput so no real LLM call is made.
"""
from __future__ import annotations

import json
import time

import pytest

from app.ai.provider import AnalysisResult
from app.schemas.ai_output import FinalizationOutput


def _stub_result(prompt_version: str = "0.1.1") -> AnalysisResult:
    out = FinalizationOutput.model_validate({
        "understanding": "stub understanding",
        "key_discussion": ["k1", "k2"],
        "facts": [{
            "key": "stub_fact", "value": "stub value",
            "status": "confirmed", "confidence": 0.9,
        }],
        "decisions": [],
        "action_items": [],
        "risks": [],
        "questions_now": [],
        "questions_later": [],
        "potential_response": None,
        "missing_information": [],
        "contradictions": [],
        "next_steps": [],
        "cleaned_transcript": "stub cleaned transcript",
        "executive_summary": "stub executive summary",
        "pm_notes_markdown": "# Stub\n- item",
    })
    return AnalysisResult(
        output=out,
        output_json=out.model_dump_json(),
        raw_content=out.model_dump_json(),
        model="gpt-4o-mini",
        prompt_version=prompt_version,
        input_tokens=10,
        output_tokens=20,
        latency_ms=1,
    )


@pytest.fixture
def stub_provider(monkeypatch):
    from app.ai import provider as provider_mod
    def _fake_run(self, prompt, user_payload, schema=None):
        return _stub_result(prompt_version=getattr(prompt, "version", "stub"))
    monkeypatch.setattr(provider_mod.AIAnalysisProvider, "run", _fake_run, raising=True)
    yield


def _create(client, title="stop-finalize"):
    r = client.post("/api/meetings", json={"title": title})
    assert r.status_code == 201
    return r.json()["id"]


def test_stop_triggers_background_finalization(client, stub_provider):
    """POST /stop returns finalizing; background task runs; meeting becomes completed."""
    mid = _create(client)
    # Insert a discussion segment so /stop has something to aggregate.
    client.post(f"/api/meetings/{mid}/transcript", json={"segments": [{
        "segment_id": f"{mid}-s1", "text": "hello world", "model": "test",
    }]})

    r = client.post(f"/api/meetings/{mid}/stop")
    assert r.status_code == 200
    # Under FINALIZE_SYNC=1 (test mode) the response is the post-finalization
    # state; the meeting should already be 'completed'.
    assert r.json()["status"] == "completed", r.json()

    # Confirm the meeting row itself is completed too.
    rr = client.get(f"/api/meetings/{mid}")
    assert rr.json()["status"] == "completed"

    # Finalization row exists and is ok.
    r = client.get(f"/api/meetings/{mid}/finalization")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "ok"
    assert body["executive_summary"] == "stub executive summary"
    assert body["pm_notes"].startswith("# Stub")
    assert body["cleaned_transcript"] == "stub cleaned transcript"


def test_stop_is_idempotent(client, stub_provider):
    """Calling /stop twice does not crash and does not create duplicate finalizations."""
    mid = _create(client)
    client.post(f"/api/meetings/{mid}/transcript", json={"segments": [{
        "segment_id": f"{mid}-s1", "text": "hello", "model": "test",
    }]})

    r1 = client.post(f"/api/meetings/{mid}/stop")
    assert r1.status_code == 200
    assert r1.json()["status"] == "completed"

    r2 = client.post(f"/api/meetings/{mid}/stop")
    assert r2.status_code == 200
    assert r2.json()["status"] == "completed"

def test_finalize_endpoint_reuses_recent_ok(client, stub_provider):
    """Calling /finalize when a very recent ok row exists reuses it."""
    mid = _create(client)
    client.post(f"/api/meetings/{mid}/transcript", json={"segments": [{
        "segment_id": f"{mid}-s1", "text": "hello", "model": "test",
    }]})
    client.post(f"/api/meetings/{mid}/stop")

    r1 = client.get(f"/api/meetings/{mid}/finalization")
    assert r1.status_code == 200
    first_id = r1.json()["id"]

    r2 = client.post(f"/api/meetings/{mid}/finalize")
    assert r2.status_code == 200
    assert r2.json()["finalization_id"] == first_id, r2.json()

    r3 = client.get(f"/api/meetings/{mid}/finalization")
    assert r3.json()["id"] == first_id
