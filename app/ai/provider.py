from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from typing import Any

from openai import OpenAI

from app.ai.output_schema import parse_and_validate
from app.ai.prompt_loader import Prompt
from app.schemas.ai_output import RollingAnalysisOutput

log = logging.getLogger("meetingsai.ai.provider")


class AIProviderError(RuntimeError):
    pass


@dataclass(frozen=True)
class RawModelCall:
    """Result of one low-level model call. No parsing, no coercion, no schema.

    Shared by both pipelines:
      - AIAnalysisProvider.run()   -> rolling analysis, Context Review, finalization
      - ModelOnlyProvider.research -> Topic Intelligence

    Carries only what the model returned and what it cost.  Schema
    validation is the caller's responsibility.
    """
    content: str
    model: str
    input_tokens: int | None
    output_tokens: int | None
    latency_ms: int


@dataclass(frozen=True)
class AnalysisResult:
    output: object          # validated pydantic object (rolling or finalization)
    output_json: str        # canonical validated JSON
    raw_content: str        # exact string the model returned
    model: str
    prompt_version: str
    input_tokens: int | None
    output_tokens: int | None
    latency_ms: int


class AIAnalysisProvider:
    """Calls the model with a versioned prompt and a JSON-object response format.

    Never writes to the database.  The scheduler is responsible for audit
    rows and reducer invocation.
    """

    def __init__(
        self,
        api_key: str,
        model: str,
        timeout_seconds: int = 60,
        max_completion_tokens: int | None = None,
    ) -> None:
        if not api_key or api_key == "__SET_ME__":
            raise AIProviderError("OPENAI_API_KEY is not set for analysis")
        self._client = OpenAI(api_key=api_key)
        self._model = model
        self._timeout = timeout_seconds
        self._max_completion_tokens = max_completion_tokens

    @property
    def model(self) -> str:
        return self._model

    def call_model(self, prompt: Prompt, user_payload: dict[str, Any]) -> RawModelCall:
        """Low-level model call. Shared by all pipelines.

        This is the single place that talks to the SDK.  It serializes the
        prompt and payload, calls chat.completions.create with JSON-object
        response format, times the call, extracts usage, and returns a
        RawModelCall.

        It does NOT parse, validate, or coerce the response.  Callers are
        responsible for applying the appropriate schema and coercion list:
          - run()                       -> parse_and_validate (rolling/finalization)
          - ModelOnlyProvider.research  -> parse_and_validate_topic (Topic Intelligence)

        Raises AIProviderError on transport failure or malformed response.
        """
        system = prompt.body
        user = json.dumps(user_payload, ensure_ascii=False, default=str)

        t0 = time.monotonic()
        try:
            kwargs = {
                "model": self._model,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                "response_format": {"type": "json_object"},
                "temperature": 0.2,
                "timeout": self._timeout,
            }
            if self._max_completion_tokens is not None:
                kwargs["max_completion_tokens"] = self._max_completion_tokens
            resp = self._client.chat.completions.create(**kwargs)
        except Exception as e:
            raise AIProviderError(f"model call failed: {e}") from e
        latency_ms = int((time.monotonic() - t0) * 1000)

        try:
            content = resp.choices[0].message.content or ""
        except Exception as e:
            raise AIProviderError(f"model response malformed: {e}") from e

        usage = getattr(resp, "usage", None)
        input_tokens = getattr(usage, "prompt_tokens", None) if usage else None
        output_tokens = getattr(usage, "completion_tokens", None) if usage else None

        log.info(
            "ai call ok model=%s prompt=%s latency_ms=%d in=%s out=%s",
            self._model, prompt.name, latency_ms, input_tokens, output_tokens,
        )
        return RawModelCall(
            content=content,
            model=self._model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            latency_ms=latency_ms,
        )

    def run(self, prompt: Prompt, user_payload: dict[str, Any], schema: type | None = None) -> AnalysisResult:
        """Run one analysis call.

        prompt.body is the system message.
        user_payload is serialized as the user message.
        """
        call = self.call_model(prompt, user_payload)

        _schema = schema if schema is not None else RollingAnalysisOutput
        output = parse_and_validate(call.content, schema=_schema)
        canonical_json = output.model_dump_json()

        return AnalysisResult(
            output=output,
            output_json=canonical_json,
            raw_content=call.content,
            model=call.model,
            prompt_version=prompt.version,
            input_tokens=call.input_tokens,
            output_tokens=call.output_tokens,
            latency_ms=call.latency_ms,
        )
