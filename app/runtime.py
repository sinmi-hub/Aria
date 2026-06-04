"""Process-wide engine singletons.

The VAD, STT and TTS models are expensive to load, so they are built once at
server startup (FastAPI lifespan) and shared across all calls. Importing this
module is cheap; ``engines.load()`` does the heavy work.
"""
from __future__ import annotations

import time


class Engines:
    def __init__(self) -> None:
        self.vad = None
        self.stt = None
        self.tts = None
        self.slm = None  # SlmBrain when BRAIN_BACKEND=slm (Phase 7, GPU pod only)
        self.fast_ack_ulaw = b""
        self.micro_acks: list[tuple[str, bytes]] = []  # (text, μ-law) pre-rendered ack pool
        self.last_metrics = {}
        self.loaded = False

    def load(self) -> None:
        if self.loaded:
            return
        from app.audio.stt import WhisperSTT
        from app.audio.tts import TTS
        from app.audio.vad import SileroVad

        t0 = time.time()
        print("[runtime] loading Silero VAD...")
        self.vad = SileroVad()
        print("[runtime] loading faster-whisper...")
        self.stt = WhisperSTT()
        print("[runtime] loading TTS...")
        self.tts = TTS()
        self._load_slm()
        self._warmup()
        self.loaded = True
        print(f"[runtime] engines ready in {time.time() - t0:.1f}s")

    def _load_slm(self) -> None:
        """Load the local Qwen brain when selected (Phase 7). Needs the GPU pod; on the
        dev box BRAIN_BACKEND stays 'claude' so this is skipped."""
        from config import settings

        if settings.brain_backend != "slm":
            return
        from app.agent.slm_brain import SlmBrain

        print(f"[runtime] loading SLM brain ({settings.slm_model})...")
        self.slm = SlmBrain()
        self.slm.load()

    def _warmup(self) -> None:
        """Run one tiny inference per heavy model so the first real call isn't
        paying cold-start (graph build / lazy init) cost on top of latency."""
        import numpy as np

        from app.audio import codec
        from config import FAST_ACKNOWLEDGEMENT, settings

        print("[runtime] warming up STT + TTS...")
        warmup_text = FAST_ACKNOWLEDGEMENT or "Ready."
        audio, sr = self.tts.synthesize(warmup_text)
        if audio.size:
            if FAST_ACKNOWLEDGEMENT:
                self.fast_ack_ulaw = codec.float_to_ulaw8k(audio, sr)
            self.stt.transcribe(np.zeros(1600, dtype=np.float32))

        # Pre-render the latency-triggered micro-ack pool so firing one mid-call adds
        # zero TTS latency (warmup already paid the cost).
        if settings.micro_ack_enabled:
            for phrase in settings.micro_ack_phrases:
                a, s = self.tts.synthesize(phrase)
                if a.size:
                    self.micro_acks.append((phrase, codec.float_to_ulaw8k(a, s)))
            print(f"[runtime] pre-rendered {len(self.micro_acks)} micro-acks")


engines = Engines()
