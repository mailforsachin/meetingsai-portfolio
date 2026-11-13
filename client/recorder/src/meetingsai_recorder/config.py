from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


ENV_PATH = Path(os.path.expanduser("~")) / ".meetingsai" / ".env"

PLACEHOLDER = "__PLACEHOLDER__"
PLACEHOLDER_VALUES = {"", "__SET_ME__", "__DRY_RUN__", PLACEHOLDER}


class ConfigError(RuntimeError):
    pass


@dataclass(frozen=True)
class Settings:
    openai_api_key: str
    transcription_model: str
    base_url: str
    password: str
    meeting_id: str
    meeting_id_is_placeholder: bool
    vb_cable_device_name: str
    samplerate: int
    chunk_seconds: int
    silence_threshold: int
    silence_timeout_seconds: int


def _need(name: str) -> str:
    v = os.environ.get(name, "").strip()
    if not v or v == "__SET_ME__":
        raise ConfigError(f"missing or placeholder value for {name} (in {ENV_PATH})")
    return v


def _optional_meeting_id() -> tuple[str, bool]:
    v = os.environ.get("MEETINGSAI_MEETING_ID", "").strip()
    if v in PLACEHOLDER_VALUES:
        return PLACEHOLDER, True
    return v, False


def _int(name: str, default: int) -> int:
    v = os.environ.get(name, "").strip()
    return int(v) if v else default


def load_settings() -> Settings:
    if not ENV_PATH.exists():
        raise ConfigError(f"env file not found at {ENV_PATH}")
    load_dotenv(ENV_PATH, override=False)

    mid, is_ph = _optional_meeting_id()

    return Settings(
        openai_api_key=_need("OPENAI_API_KEY"),
        transcription_model=os.environ.get(
            "TRANSCRIPTION_MODEL", "gpt-4o-mini-transcribe"
        ).strip(),
        base_url=os.environ.get(
            "MEETINGSAI_BASE_URL", "https://meetingsai.omchat.ovh"
        ).rstrip("/"),
        password=_need("MEETINGSAI_PASSWORD"),
        meeting_id=mid,
        meeting_id_is_placeholder=is_ph,
        vb_cable_device_name=os.environ.get(
            "VB_CABLE_DEVICE_NAME", "CABLE Output"
        ).strip(),
        samplerate=_int("SAMPLERATE", 48000),
        chunk_seconds=_int("CHUNK_SECONDS", 6),
        silence_threshold=_int("SILENCE_THRESHOLD", 200),
        silence_timeout_seconds=_int("SILENCE_TIMEOUT_SECONDS", 20),
    )
