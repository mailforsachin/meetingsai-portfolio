"""Stage C: opt-in max_completion_tokens on AIAnalysisProvider.

Three assertions:
  1. When max_completion_tokens is set, it reaches the SDK call kwargs.
  2. When it is not set, the SDK call kwargs do not contain the parameter.
  3. The three non-TI consumers do not pass it, so their behavior is
     unchanged by the Stage C edits.

No network, no DB, no .env.  The SDK client is replaced with a MagicMock.
"""
from __future__ import annotations

import inspect
from unittest.mock import MagicMock

from app.ai.provider import AIAnalysisProvider
from app.ai.prompt_loader import Prompt


def _fake_response(content: str = "{}"):
    resp = MagicMock()
    resp.choices = [MagicMock()]
    resp.choices[0].message.content = content
    resp.usage = MagicMock(prompt_tokens=1, completion_tokens=1)
    return resp


def _prompt() -> Prompt:
    return Prompt(name="test", version="0.0.1", kind="test", body="system body")


def test_max_completion_tokens_passed_when_set():
    p = AIAnalysisProvider(api_key="x", model="m", max_completion_tokens=100)
    p._client = MagicMock()
    p._client.chat.completions.create.return_value = _fake_response()
    p.call_model(_prompt(), {"k": "v"})
    kwargs = p._client.chat.completions.create.call_args.kwargs
    assert kwargs["max_completion_tokens"] == 100


def test_max_completion_tokens_absent_by_default():
    p = AIAnalysisProvider(api_key="x", model="m")
    p._client = MagicMock()
    p._client.chat.completions.create.return_value = _fake_response()
    p.call_model(_prompt(), {"k": "v"})
    kwargs = p._client.chat.completions.create.call_args.kwargs
    assert "max_completion_tokens" not in kwargs


def test_non_ti_consumers_do_not_pass_max_completion_tokens():
    """Regression guard: rolling analysis, finalization, and context review
    must not pass the new parameter, so their behavior is unchanged."""
    from app.ai import analysis_scheduler, finalizer, context_review_scheduler

    for mod in (analysis_scheduler, finalizer, context_review_scheduler):
        src = inspect.getsource(mod)
        assert "max_completion_tokens" not in src, (
            f"{mod.__name__} references max_completion_tokens; "
            "Stage C intended this to be opt-in only"
        )


def test_rolling_analysis_provider_constructs_without_param():
    """Direct construction check: this mirrors analysis_scheduler's call site."""
    p = AIAnalysisProvider(api_key="x", model="gpt-4o-mini")
    assert p._max_completion_tokens is None
