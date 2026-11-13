from __future__ import annotations

import logging
import os
import tempfile
import wave
from abc import ABC, abstractmethod
from typing import Optional

import numpy as np


log = logging.getLogger("meetingsai.provider")


class ProviderError(RuntimeError):
    pass


class TranscriptionProvider(ABC):
    """Interface for turning a short audio chunk into text."""

    name: str = "abstract"

    @abstractmethod
    def transcribe(self, audio_int16_mono: np.ndarray, samplerate: int) -> str:
        """Return the transcript text for this chunk, or '' if nothing was said."""
        raise NotImplementedError


class OpenAITranscriptionProvider(TranscriptionProvider):
    name = "openai"

    def __init__(
        self,
        api_key: str,
        model: str,
        timeout_seconds: int = 30,
    ) -> None:
        from openai import OpenAI  # imported lazily so tests do not need it

        if not api_key or api_key == "__SET_ME__":
            raise ProviderError("OPENAI_API_KEY is not set")
        self._model = model
        self._timeout = timeout_seconds
        self._client = OpenAI(api_key=api_key)

    @property
    def model(self) -> str:
        return self._model

    def transcribe(self, audio_int16_mono: np.ndarray, samplerate: int) -> str:
        fd, tmp_path = tempfile.mkstemp(prefix="meetingsai_", suffix=".wav")
        os.close(fd)
        try:
            with wave.open(tmp_path, "wb") as wf:
                wf.setnchannels(1)
                wf.setsampwidth(2)          # int16
                wf.setframerate(samplerate)
                wf.writeframes(audio_int16_mono.tobytes())

            with open(tmp_path, "rb") as f:
                try:
                    result = self._client.audio.transcriptions.create(
                        model=self._model,
                        file=f,
                        timeout=self._timeout,
                    )
                    text = (getattr(result, "text", "") or "").strip()
                    return text
                except Exception as e:
                    # We deliberately do not swallow the error silently:
                    # the caller decides whether to retry.
                    raise ProviderError(f"transcription failed: {e}") from e
        finally:
            try:
                os.remove(tmp_path)
            except OSError:
                pass
