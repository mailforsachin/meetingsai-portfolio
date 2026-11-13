"""Tests for TopicDetectionOutput schema and detection parser.

Covers valid output, empty topics, enum validation, malformed entries,
invalid top-level JSON types (list, scalar), and isolation from the
rolling-analysis coercion layer.
"""
from __future__ import annotations

import pytest

from app.ai.topic_intelligence.detection_parsing import (
    TopicDetectionSchemaError,
    parse_and_validate_detection,
)
from app.schemas.topic_detection_output import (
    ProposedTopic,
    TopicDetectionOutput,
)


def test_valid_detection_output():
    out = parse_and_validate_detection({
        "topics": [
            {"name": "kafka", "topic_type": "technology", "relevance": "high"},
        ]
    })
    assert isinstance(out, TopicDetectionOutput)
    assert len(out.topics) == 1
    assert isinstance(out.topics[0], ProposedTopic)
    assert out.topics[0].name == "kafka"
    assert out.topics[0].topic_type == "technology"
    assert out.topics[0].relevance == "high"


def test_empty_topics_is_valid():
    out = parse_and_validate_detection({"topics": []})
    assert out.topics == []


def test_more_than_five_topics_still_valid():
    """The five-topic cap is prompt-level, not schema-level."""
    topics = [
        {"name": f"topic{i}", "topic_type": "concept", "relevance": "low"}
        for i in range(8)
    ]
    out = parse_and_validate_detection({"topics": topics})
    assert len(out.topics) == 8


def test_invalid_topic_type_rejected():
    with pytest.raises(TopicDetectionSchemaError):
        parse_and_validate_detection({
            "topics": [
                {"name": "kafka", "topic_type": "gadget", "relevance": "high"},
            ]
        })


def test_invalid_relevance_rejected():
    with pytest.raises(TopicDetectionSchemaError):
        parse_and_validate_detection({
            "topics": [
                {"name": "kafka", "topic_type": "technology", "relevance": "urgent"},
            ]
        })


def test_malformed_topic_entry_rejected():
    with pytest.raises(TopicDetectionSchemaError):
        parse_and_validate_detection({"topics": ["not-an-object"]})


def test_missing_name_rejected():
    with pytest.raises(TopicDetectionSchemaError):
        parse_and_validate_detection({
            "topics": [{"topic_type": "technology", "relevance": "high"}]
        })


def test_bad_json_rejected():
    with pytest.raises(TopicDetectionSchemaError):
        parse_and_validate_detection("{not json")


def test_json_array_top_level_rejected():
    """Correction 2: a JSON array is valid JSON but not a detection object."""
    with pytest.raises(TopicDetectionSchemaError):
        parse_and_validate_detection("[1, 2, 3]")


def test_json_scalar_top_level_rejected():
    """Correction 2: a JSON scalar is valid JSON but not a detection object."""
    with pytest.raises(TopicDetectionSchemaError):
        parse_and_validate_detection("42")


def test_python_list_top_level_rejected():
    """Correction 3: non-dict input does not leak an unexpected exception."""
    with pytest.raises(TopicDetectionSchemaError):
        parse_and_validate_detection([1, 2, 3])


def test_python_none_top_level_rejected():
    with pytest.raises(TopicDetectionSchemaError):
        parse_and_validate_detection(None)


def test_does_not_apply_rolling_coercions():
    """Correction 4: detection parsing must not touch rolling coercions."""
    out = parse_and_validate_detection({
        "topics": [],
        "understanding": {"nested": "obj"},
    })
    assert out.topics == []
