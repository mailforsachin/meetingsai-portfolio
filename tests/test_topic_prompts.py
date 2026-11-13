"""Structural tests for the Topic Intelligence prompts.

Prompt content is prose; these tests only guard file structure, frontmatter
parsing, and the presence of required sections.  Effectiveness is validated
in Stage C with real-meeting trials, not here.
"""
from __future__ import annotations

from app.ai.prompt_loader import load_prompt


def test_topic_detection_prompt_loads():
    p = load_prompt("topic_detection.md")
    assert p.version == "0.1.0"
    assert p.kind == "topic_detection"
    assert "Output" in p.body
    assert "Worked examples" in p.body


def test_topic_brief_prompt_loads():
    p = load_prompt("topic_brief.md")
    assert p.version == "0.1.0"
    assert p.kind == "topic_brief"
    assert "Output" in p.body
    assert "Worked examples" in p.body


def test_topic_brief_prompt_warns_against_url_fabrication():
    p = load_prompt("topic_brief.md")
    assert "Do NOT invent URLs" in p.body
    assert "unverified" in p.body
    assert '"sources": []' in p.body


def test_topic_detection_prompt_caps_topics():
    p = load_prompt("topic_detection.md")
    assert "FIVE" in p.body or "five" in p.body


def test_topic_detection_prompt_has_empty_example():
    p = load_prompt("topic_detection.md")
    assert '"topics": []' in p.body
