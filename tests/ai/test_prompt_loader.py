from app.ai.prompt_loader import load_prompt


def test_load_meeting_analysis_prompt():
    p = load_prompt("meeting_analysis.md")
    assert p.kind == "rolling"
    assert p.version == "0.4.0"
    assert "analysis engine of meetingsAI" in p.body
    assert "Return a single JSON object" in p.body or "Output JSON only" in p.body
