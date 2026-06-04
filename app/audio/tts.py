"""Text-to-speech wrapper. Kokoro primary, Piper fallback. Local, no API cost.

``synthesize`` returns (float32 mono waveform, sample_rate). It is blocking, so
callers run it in a thread executor. The engine is chosen by ``TTS_ENGINE`` and
falls back to Piper automatically if Kokoro fails to import/load.
"""
from __future__ import annotations

import numpy as np

from config import settings


class _KokoroEngine:
    def __init__(self) -> None:
        import torch
        from kokoro import KPipeline

        device = "cuda" if torch.cuda.is_available() else "cpu"
        print(f"[tts] Kokoro on {device}")
        self.pipeline = KPipeline(lang_code=settings.kokoro_lang, device=device)
        self.voice = settings.kokoro_voice
        self.sample_rate = settings.kokoro_sample_rate

    def synthesize(self, text: str) -> tuple[np.ndarray, int]:
        chunks = []
        for _, _, audio in self.pipeline(text, voice=self.voice, speed=1):
            chunks.append(np.asarray(audio, dtype=np.float32))
        if not chunks:
            return np.zeros(0, dtype=np.float32), self.sample_rate
        return np.concatenate(chunks), self.sample_rate


class _PiperEngine:
    def __init__(self) -> None:
        from piper import PiperVoice

        if not settings.piper_model_path:
            raise RuntimeError("PIPER_MODEL_PATH not set")
        self.voice = PiperVoice.load(settings.piper_model_path)
        self.sample_rate = self.voice.config.sample_rate

    def synthesize(self, text: str) -> tuple[np.ndarray, int]:
        # piper>=1.3 yields AudioChunk objects with a float32 array per chunk.
        chunks = [c.audio_float_array for c in self.voice.synthesize(text)]
        if not chunks:
            return np.zeros(0, dtype=np.float32), self.sample_rate
        return np.concatenate(chunks).astype(np.float32), self.sample_rate


class TTS:
    """Picks an engine at construction with graceful fallback."""

    def __init__(self) -> None:
        self.engine = self._build()

    def _build(self):
        if settings.tts_engine == "piper":
            return _PiperEngine()
        try:
            return _KokoroEngine()
        except Exception as exc:  # noqa: BLE001 - want any import/load failure
            print(f"[tts] Kokoro unavailable ({exc!r}); falling back to Piper")
            return _PiperEngine()

    @property
    def sample_rate(self) -> int:
        return self.engine.sample_rate

    def synthesize(self, text: str) -> tuple[np.ndarray, int]:
        text = text.strip()
        if not text:
            return np.zeros(0, dtype=np.float32), self.sample_rate
        return self.engine.synthesize(text)
