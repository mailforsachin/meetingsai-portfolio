import pytest

from app.ai.output_schema import AISchemaError, parse_and_validate


def _minimal():
    return {
        "understanding": "x", "key_discussion": [],
        "facts": [], "decisions": [], "action_items": [], "risks": [],
        "questions_now": [], "questions_later": [],
        "potential_response": None,
        "missing_information": [], "contradictions": [], "next_steps": [],
    }


def test_minimal_valid():
    out = parse_and_validate(_minimal())
    assert out.understanding == "x"


def test_valid_with_fact():
    d = _minimal()
    d["facts"] = [{
        "key": "production_go_live", "value": "October 30",
        "status": "tentative", "confidence": 0.6,
        "source_discussion_id": 1, "source_text": "go-live date is October 30",
    }]
    out = parse_and_validate(d)
    assert out.facts[0].key == "production_go_live"
    assert out.facts[0].status == "tentative"


def test_invalid_json_string():
    with pytest.raises(AISchemaError):
        parse_and_validate("{not json")


def test_invalid_status():
    d = _minimal()
    d["facts"] = [{"key": "x", "value": "y", "status": "nonsense"}]
    with pytest.raises(AISchemaError):
        parse_and_validate(d)


def test_confidence_bounds():
    d = _minimal()
    d["facts"] = [{"key": "x", "value": "y", "status": "tentative", "confidence": 1.5}]
    with pytest.raises(AISchemaError):
        parse_and_validate(d)
