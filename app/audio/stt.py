"""faster-whisper speech-to-text wrapper. Local, no API cost.

Loaded once at startup; ``transcribe`` is blocking (CTranslate2) so callers
run it in a thread executor to keep the event loop free.
"""
from __future__ import annotations

import os

import numpy as np

from config import settings


class WhisperSTT:
    def __init__(self) -> None:
        from faster_whisper import WhisperModel

        # ctranslate2 threading is independent of torch; use most cores for speed.
        cpu_threads = max(1, min(8, (os.cpu_count() or 4) - 2))
        self.model = WhisperModel(
            settings.whisper_model,
            device=settings.whisper_device,
            compute_type=settings.whisper_compute_type,
            cpu_threads=cpu_threads,
        )

    def transcribe(self, audio_16k: np.ndarray) -> str:
        """Transcribe a float32 16kHz mono utterance into trimmed text."""
        if audio_16k.size == 0:
            return ""
        segments, _ = self.model.transcribe(
            audio_16k,
            language="en",
            beam_size=settings.whisper_beam_size,
            vad_filter=False,
            condition_on_previous_text=False,
        )
        return " ".join(seg.text.strip() for seg in segments).strip()
