"""Audio codec + resampling between Telnyx (μ-law 8kHz) and the internal
PCM formats used by VAD/STT (16kHz) and produced by TTS (24kHz).

Uses the stdlib ``audioop`` for μ-law conversion and rate conversion. It is
fast, dependency-free, and handles the integer-ratio rates we need (8/16/24kHz).
``audioop`` is deprecated in 3.13 but present and supported on this 3.10 runtime.
"""
from __future__ import annotations

import audioop
import base64

import numpy as np

_WIDTH = 2  # 16-bit PCM


# --- Telnyx inbound: μ-law 8kHz -> float32 16kHz mono --------------------------
def ulaw8k_to_float16k(ulaw_bytes: bytes) -> np.ndarray:
    """Decode caller audio to the float32 16kHz array VAD/Whisper want."""
    pcm8k = audioop.ulaw2lin(ulaw_bytes, _WIDTH)
    pcm16k, _ = audioop.ratecv(pcm8k, _WIDTH, 1, 8000, 16000, None)
    return pcm16_bytes_to_float32(pcm16k)


# --- Telnyx outbound: TTS PCM -> μ-law 8kHz ------------------------------------
def float_to_ulaw8k(audio: np.ndarray, src_rate: int) -> bytes:
    """Convert a float32 mono TTS waveform (any rate) to μ-law 8kHz bytes."""
    pcm = float32_to_pcm16_bytes(audio)
    if src_rate != 8000:
        pcm, _ = audioop.ratecv(pcm, _WIDTH, 1, src_rate, 8000, None)
    return audioop.lin2ulaw(pcm, _WIDTH)


# --- PCM <-> float helpers -----------------------------------------------------
def pcm16_bytes_to_float32(pcm: bytes) -> np.ndarray:
    arr = np.frombuffer(pcm, dtype=np.int16).astype(np.float32)
    return arr / 32768.0


def float32_to_pcm16_bytes(audio: np.ndarray) -> bytes:
    clipped = np.clip(audio, -1.0, 1.0)
    return (clipped * 32767.0).astype(np.int16).tobytes()


# --- base64 framing (Telnyx wire format) --------------------------------------
def b64_decode(payload: str) -> bytes:
    return base64.b64decode(payload)


def b64_encode(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def chunk_ulaw(ulaw: bytes, frame_bytes: int = 160) -> list[bytes]:
    """Split a μ-law buffer into fixed 20ms (160-byte) frames for paced sending."""
    return [ulaw[i : i + frame_bytes] for i in range(0, len(ulaw), frame_bytes)]
