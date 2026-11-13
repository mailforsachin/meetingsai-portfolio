from __future__ import annotations

import logging

import numpy as np
import sounddevice as sd

log = logging.getLogger("meetingsai.capture")


def auto_gain(audio: np.ndarray) -> np.ndarray:
    peak = int(np.max(np.abs(audio))) if audio.size else 0
    if peak == 0:
        return audio
    gain = min(20000 / peak, 3.0)
    out = audio.astype(np.float32) * gain
    return np.clip(out, -32768, 32767).astype(np.int16)


def downmix_to_mono(audio: np.ndarray) -> np.ndarray:
    """audio shape (N,) or (N, C) -> (N,) int16."""
    if audio.ndim == 1:
        return audio.astype(np.int16)
    if audio.shape[1] == 1:
        return audio[:, 0].astype(np.int16)
    # mean across channels, in float to avoid overflow
    return np.mean(audio.astype(np.float32), axis=1).astype(np.int16)


class ChunkCapturer:
    """Captures fixed-length mono int16 chunks from an input device.

    Opens the device in its native configuration (whatever channels the device
    exposes at the requested samplerate), then downmixes to mono.
    """

    def __init__(self, device_index: int, samplerate: int, chunk_seconds: int):
        self.device_index = device_index
        self.samplerate = samplerate
        self.frames_per_chunk = int(samplerate * chunk_seconds)

        info = sd.query_devices(device_index)
        self.channels = max(1, int(info.get("max_input_channels", 1)))

    def read_chunk(self) -> np.ndarray:
        rec = sd.rec(
            self.frames_per_chunk,
            samplerate=self.samplerate,
            channels=self.channels,
            dtype="int16",
            device=self.device_index,
        )
        sd.wait()
        mono = downmix_to_mono(rec)
        return auto_gain(mono)

    @staticmethod
    def peak(audio: np.ndarray) -> int:
        return int(np.max(np.abs(audio))) if audio.size else 0

    @staticmethod
    def is_silent(audio: np.ndarray, threshold: int) -> bool:
        return ChunkCapturer.peak(audio) < threshold
