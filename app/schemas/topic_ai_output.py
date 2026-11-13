"""Pydantic schemas for Topic Intelligence LLM output.

Stage A: model_only provider.  The model produces a summary and may
propose sources, but with no web search there is no way to verify them.
Proposed sources are accepted structurally; T6 marks any run that
carries sources without citations as model_only, not verified.
"""
from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field


class ProposedSource(BaseModel):
    """A source the model claims supports the brief.

    Stage A caveat: with model_only, these are unverifiable.  The URL may
    be hallucinated.  They are stored for transparency and must not be
    presented as verified provenance.  Stage B (responses provider) fills
    these from real url_citations instead.
    """
    url: Optional[str] = None
    title: Optional[str] = None
    snippet: Optional[str] = None


class TopicBriefOutput(BaseModel):
    """Top-level output the model must produce for one topic brief."""
    summary: str = Field(min_length=1)
    sources: list[ProposedSource] = Field(default_factory=list)
    notes: Optional[str] = None
