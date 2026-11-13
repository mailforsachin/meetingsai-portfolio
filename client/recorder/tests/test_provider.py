from __future__ import annotations

import numpy as np

from meetingsai_recorder.provider import ProviderError, TranscriptionProvider


class FakeProvider(TranscriptionProvider):
    name = "fake"

    def __init__(self, reply: str):
        self.reply = reply

    def transcribe(self, audio_int16_mono: np.ndarray, samplerate: int) -> str:
        return self.reply


def test_fake_provider_returns_text():
    p = FakeProvider("hello world")
    out = p.transcribe(np.zeros(1600, dtype=np.int16), 16000)
    assert out == "hello world"


def test_provider_is_abstract():
    try:
        TranscriptionProvider()  # type: ignore[abstract]
    except TypeError:
        return
    raise AssertionError("TranscriptionProvider should be abstract")


def test_openai_provider_rejects_missing_key():
    from meetingsai_recorder.provider import OpenAITranscriptionProvider
    try:
        OpenAITranscriptionProvider(api_key="", model="x")
    except ProviderError:
        return
    raise AssertionError("expected ProviderError on empty API key")


def test_openai_provider_rejects_placeholder_key():
    from meetingsai_recorder.provider import OpenAITranscriptionProvider
    try:
        OpenAITranscriptionProvider(api_key="__SET_ME__", model="x")
    except ProviderError:
        return
    raise AssertionError("expected ProviderError on placeholder API key")
