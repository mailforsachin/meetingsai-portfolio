"""Topic detection parsing with its own coercion list.

Parallel to app/ai/topic_intelligence/parsing.py (brief parsing).
Deliberately separate from app.ai.output_schema.parse_and_validate:
that function's _FIELD_COERCIONS is keyed to rolling/finalization field
names and must not be applied to detection output.

Policy: scope is deliberately narrow.  Add a coercion only when a field
has actually failed in production.  Do not write a general recursive
coercion.
"""
from __future__ import annotations

import json
from typing import Any

from pydantic import ValidationError

from app.schemas.topic_detection_output import TopicDetectionOutput


class TopicDetectionSchemaError(ValueError):
    """Raised when raw detection output cannot be parsed or validated."""


# Empty in Stage A.  Expand per-field when a detection field fails.
_DETECTION_FIELD_COERCIONS: dict[str, Any] = {}


def _apply_detection_coercions(data: dict) -> dict:
    for key, coerce in _DETECTION_FIELD_COERCIONS.items():
        if key in data:
            data[key] = coerce(data[key])
    return data


def parse_and_validate_detection(
    raw: str | dict[str, Any],
) -> TopicDetectionOutput:
    if isinstance(raw, str):
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as e:
            raise TopicDetectionSchemaError(
                f"invalid JSON from model: {e}"
            ) from e
    else:
        data = raw

    # Top-level must be a JSON object.  A list or scalar is a valid JSON
    # document but not a valid detection output.  Fail explicitly rather
    # than relying on pydantic's error text.
    if not isinstance(data, dict):
        raise TopicDetectionSchemaError(
            f"detection output must be a JSON object, got {type(data).__name__}"
        )

    data = _apply_detection_coercions(data)

    try:
        return TopicDetectionOutput.model_validate(data)
    except ValidationError as e:
        raise TopicDetectionSchemaError(
            f"schema validation failed: {e}"
        ) from e
