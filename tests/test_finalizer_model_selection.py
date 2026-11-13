"""Tests for the finalizer model selection split.

The finalizer uses:

    model=(settings.final_analysis_model or settings.analysis_model)

to allow an explicit FINAL_ANALYSIS_MODEL override, falling back to
ANALYSIS_MODEL when unset.

These tests exercise the resolution expression and confirm the finalizer
wiring uses it.  No network, no DB, no env file, no Settings() with no
arguments.
"""
from __future__ import annotations

import inspect

from app.core.config import Settings


def test_explicit_final_analysis_model_wins():
    s = Settings(
        final_analysis_model="gpt-4o",
        analysis_model="gpt-4o-mini",
    )
    assert (s.final_analysis_model or s.analysis_model) == "gpt-4o"


def test_empty_final_analysis_model_falls_back():
    s = Settings(
        final_analysis_model="",
        analysis_model="gpt-4o-mini",
    )
    assert (s.final_analysis_model or s.analysis_model) == "gpt-4o-mini"


def test_finalizer_uses_resolution_expression():
    """Structural check: the finalizer must reference the resolution
    expression in its model= arguments, and must not contain a bare
    `model=settings.analysis_model,` anywhere.

    This is the guard against a future edit reverting the split.
    """
    from app.ai import finalizer as fmod
    src = inspect.getsource(fmod)

    # The resolution expression must appear (once per model= site).
    assert "final_analysis_model or settings.analysis_model" in src

    # The old bare form must not remain anywhere in the file.
    assert "model=settings.analysis_model," not in src
