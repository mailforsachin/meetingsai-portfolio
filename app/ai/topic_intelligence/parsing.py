"""Topic Intelligence parsing with its own coercion list.

Deliberately separate from app.ai.output_schema.parse_and_validate:
that function's _FIELD_COERCIONS is keyed to rolling/finalization field
names and must not be applied to topic brief fields.

Policy, copied from output_schema.py: scope is deliberately narrow.
Add a coercion only when a field has actually failed in production.
Do not write a general recursive coercion.
"""
from __future__ import annotations

import json
from typing import Any

from pydantic import ValidationError

from app.schemas.topic_ai_output import TopicBriefOutput


class TopicSchemaError(ValueError):
    """Raised when raw topic-brief output cannot be parsed or validated."""


# Empty in Stage A.  Expand per-field when a topic brief field fails.
_TOPIC_FIELD_COERCIONS: dict[str, Any] = {}


def _apply_topic_coercions(data: dict) -> dict:
    for key, coerce in _TOPIC_FIELD_COERCIONS.items():
        if key in data:
            data[key] = coerce(data[key])
    return data


def parse_and_validate_topic(raw: str | dict[str, Any]) -> TopicBriefOutput:
    if isinstance(raw, str):
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as e:
            raise TopicSchemaError(f"invalid JSON from model: {e}") from e
    else:
        data = raw
    if isinstance(data, dict):
        data = _apply_topic_coercions(data)
    try:
        return TopicBriefOutput.model_validate(data)
    except ValidationError as e:
        raise TopicSchemaError(f"schema validation failed: {e}") from e
