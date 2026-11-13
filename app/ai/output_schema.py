from __future__ import annotations

import json
from typing import Any

from pydantic import ValidationError

from app.schemas.ai_output import FinalizationOutput, RollingAnalysisOutput


class AISchemaError(ValueError):
    """Raised when raw model output cannot be parsed or validated."""


# Phase H3: tolerance layer for known-fragile fields.
#
# The model reliably produces output that almost matches the schema but
# occasionally returns a richer shape than the declared type:
#   - a string field arrives as a dict
#   - a list-of-strings arrives as a list of dicts (each with "text")
#
# Rejecting the entire output for one field-level divergence wasted several
# production finalizations in the H2 phase.  These coercions run before
# pydantic validation and normalize the shape without changing semantics.
#
# Scope is deliberately narrow: only the fields observed to fail in
# production.  Expand the list when a new field fails; do not write a
# general recursive coercion.

def _coerce_to_str(v):
    """Accept a str, or serialize a dict/list to JSON text.  None passes through."""
    if v is None or isinstance(v, str):
        return v
    try:
        return json.dumps(v, ensure_ascii=False, default=str)
    except Exception:
        return str(v)


def _coerce_list_of_str(v):
    """Accept list[str], or list[dict] by keeping each item's text field.

    For a dict item, prefer "text", then "value", then "summary".  If none
    of those exist, JSON-serialize the whole item.  Non-list values pass
    through untouched so pydantic can produce a meaningful error.
    """
    if not isinstance(v, list):
        return v
    out = []
    for item in v:
        if isinstance(item, str):
            out.append(item)
        elif isinstance(item, dict):
            text = item.get("text") or item.get("value") or item.get("summary")
            if text is None:
                text = json.dumps(item, ensure_ascii=False, default=str)
            out.append(str(text))
        else:
            out.append(str(item))
    return out


_FIELD_COERCIONS = {
    "understanding":        _coerce_to_str,
    "potential_response":   _coerce_to_str,
    "what_changed_proposal": _coerce_to_str,
    "key_discussion":       _coerce_list_of_str,
    "missing_information":  _coerce_list_of_str,
    "contradictions":       _coerce_list_of_str,
    "next_steps":           _coerce_list_of_str,
}


_VALID_RELEVANCE = {"critical", "relevant", "contextual", "incidental"}

# The model sometimes confuses MeetingRelevance with RiskSeverity
# ({"low","medium","high","critical"}) or emits integers from an
# adjacent field.  Map known confusions; default anything else to
# "relevant".  Values already in the literal pass through unchanged.
_RELEVANCE_ALIASES = {
    "high": "critical",
    "medium": "relevant",
    "low": "contextual",
    "very high": "critical",
    "very low": "incidental",
}


def _coerce_relevance_value(v):
    """Return a valid MeetingRelevance string, or None if v is None."""
    if v is None:
        return None
    if isinstance(v, str):
        key = v.strip().lower()
        if key in _VALID_RELEVANCE:
            return key
        if key in _RELEVANCE_ALIASES:
            return _RELEVANCE_ALIASES[key]
    # Anything else (int, dict, unknown string) defaults to "relevant".
    return "relevant"


_ITEM_LIST_FIELDS = (
    "facts",
    "decisions",
    "action_items",
    "risks",
    "questions_now",
    "questions_later",
    "threads",
    "discussion_annotations",
)


def _coerce_relevance_in_lists(data: dict) -> dict:
    """Normalize meeting_relevance on every item in every known item list."""
    for key in _ITEM_LIST_FIELDS:
        items = data.get(key)
        if not isinstance(items, list):
            continue
        for item in items:
            if isinstance(item, dict) and "meeting_relevance" in item:
                item["meeting_relevance"] = _coerce_relevance_value(
                    item["meeting_relevance"]
                )
    return data


def _coerce_facts(data: dict) -> dict:
    """Backfill key/value/status on facts that arrived as {'text': '...'}.

    The model sometimes returns facts in the same shape as the other
    item types.  When key or value is empty and text is present, derive
    them from text.  Also default status to 'tentative' when absent.
    """
    items = data.get("facts")
    if not isinstance(items, list):
        return data
    for item in items:
        if not isinstance(item, dict):
            continue
        text = item.get("text")
        if not text:
            continue
        key = item.get("key") or ""
        value = item.get("value") or ""
        if not key and not value:
            head = text.strip().split()
            item["key"] = " ".join(head[:6])[:255] if head else "fact"
            item["value"] = text
        elif not value:
            item["value"] = text
        elif not key:
            item["key"] = text[:255]
        if not item.get("status"):
            item["status"] = "tentative"
    return data


_VALID_MODES = {
    "action_tracker", "discovery", "technical_workshop",
    "architecture_review", "decision", "governance",
    "training", "demo", "general",
}


def _coerce_mode(data: dict) -> dict:
    """Map unknown meeting_mode_proposal values to 'general'.

    The model invents modes not in the Literal (observed: 'training').
    Unknown values default to 'general' rather than failing validation.
    """
    v = data.get("meeting_mode_proposal")
    if v is None:
        return data
    if isinstance(v, str) and v.strip().lower() in _VALID_MODES:
        data["meeting_mode_proposal"] = v.strip().lower()
    else:
        data["meeting_mode_proposal"] = "general"
    return data


def _coerce_threads(data: dict) -> dict:
    """Accept threads as a dict keyed by id, or a list.  Unwrap dict values.

    The model sometimes returns threads as {'98': {...}, '99': {...}}
    where the schema expects a list.  Convert dict to list of values.
    """
    v = data.get("threads")
    if isinstance(v, dict):
        data["threads"] = list(v.values())
    return data


def _apply_field_coercions(data: dict) -> dict:
    for key, coerce in _FIELD_COERCIONS.items():
        if key in data:
            data[key] = coerce(data[key])
    data = _coerce_relevance_in_lists(data)
    data = _coerce_facts(data)
    data = _coerce_mode(data)
    data = _coerce_threads(data)
    return data


def parse_and_validate(
    raw: str | dict[str, Any],
    schema: type = RollingAnalysisOutput,
):
    """Turn raw model output (JSON text or dict) into a validated object.

    `schema` defaults to RollingAnalysisOutput for the rolling pipeline.
    The finalization pipeline passes FinalizationOutput.
    """
    if isinstance(raw, str):
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as e:
            raise AISchemaError(f"invalid JSON from model: {e}") from e
    else:
        data = raw
    if isinstance(data, dict):
        data = _apply_field_coercions(data)
    try:
        return schema.model_validate(data)
    except ValidationError as e:
        raise AISchemaError(f"schema validation failed: {e}") from e
